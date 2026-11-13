"""`omni gateway`: talk to Omni from chat apps (OpenClaw's gateway, Hermes' messaging platforms).

The gateway is a long-running frontend. Every conversation (one Telegram chat, one HTTP client id) gets its own
`AgentSession` with its own session file, so context survives restarts and conversations never mix.

Configuration: `<agent-dir>/gateway.json`

    {
      "profile": "assistant",                    profile for gateway sessions (default: the active profile)
      "cwd": "D:/work/notes",                    working directory for them (default: where the gateway was started)
      "channels": {
        "telegram": {"token": "$TELEGRAM_BOT_TOKEN", "allow": [123456789]},
        "http":     {"port": 18790, "token": "choose-a-secret"}
      }
    }

Access: only senders in `allow` are answered. Anyone else gets a one-time pairing code; `omni gateway approve
<code>` on this machine adds them (OpenClaw's DM pairing). The HTTP channel requires its bearer token and listens
on 127.0.0.1 only.

In a chat: `/new` starts a fresh conversation, `/status` shows model, profile and context use, `/stop` interrupts
the current run. Everything else goes to the agent.

Channels are pluggable: `register_channel("name", factory)` adds one; a channel implements `poll()` and `send()`.
Commands: `omni gateway` (run), `omni gateway status`, `omni gateway approve <code>`.
"""
from __future__ import annotations

import http.server
import json
import os
import queue
import secrets
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

MAX_CHUNK = 3900
PAIRING_TTL = 3600


@dataclass
class Inbound:
    channel: str
    chat: str                 # conversation id within the channel
    sender: str               # who wrote (for the allowlist)
    text: str
    name: str = ""
    reply: Optional[Callable[[str], None]] = None   # channels that answer in the same request (HTTP) set this


class Channel:
    name = "channel"

    def poll(self, timeout: float) -> List[Inbound]:
        raise NotImplementedError

    def send(self, chat: str, text: str) -> None:
        raise NotImplementedError

    def typing(self, chat: str) -> None:
        pass

    def close(self) -> None:
        pass


def resolve_secret(value: Any) -> str:
    v = str(value or "")
    return os.environ.get(v[1:], "") if v.startswith("$") else v


# ------------------------------------------------------------------------------------------- telegram
class TelegramChannel(Channel):
    name = "telegram"

    def __init__(self, cfg: Dict[str, Any]):
        self.token = resolve_secret(cfg.get("token"))
        if not self.token:
            raise ValueError("telegram: token is missing (set it, or \"$TELEGRAM_BOT_TOKEN\" with the variable defined)")
        self.base = str(cfg.get("api_base") or "https://api.telegram.org").rstrip("/")
        self.offset = 0

    def _call(self, method: str, params: Dict[str, Any], timeout: float = 30.0) -> Any:
        req = urllib.request.Request(f"{self.base}/bot{self.token}/{method}", data=json.dumps(params).encode(),
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = json.loads(r.read().decode("utf-8"))
        if not data.get("ok"):
            raise RuntimeError(f"telegram {method}: {data.get('description')}")
        return data.get("result")

    def poll(self, timeout: float) -> List[Inbound]:
        try:
            updates = self._call("getUpdates", {"offset": self.offset, "timeout": int(timeout), "allowed_updates": ["message"]},
                                 timeout=timeout + 10)
        except (urllib.error.URLError, OSError, RuntimeError, ValueError):
            time.sleep(min(timeout, 2.0))
            return []
        out = []
        for u in updates or []:
            self.offset = max(self.offset, int(u.get("update_id", 0)) + 1)
            m = u.get("message") or {}
            text = m.get("text") or m.get("caption")
            if not text or not m.get("chat"):
                continue
            who = m.get("from") or {}
            out.append(Inbound("telegram", str(m["chat"]["id"]), str(who.get("id") or m["chat"]["id"]), text,
                               name=who.get("username") or who.get("first_name") or ""))
        return out

    def send(self, chat: str, text: str) -> None:
        for i in range(0, len(text) or 1, MAX_CHUNK):
            self._call("sendMessage", {"chat_id": chat, "text": text[i:i + MAX_CHUNK] or "(empty)"})

    def typing(self, chat: str) -> None:
        try:
            self._call("sendChatAction", {"chat_id": chat, "action": "typing"}, timeout=10)
        except Exception:
            pass


# ------------------------------------------------------------------------------------------- http
class HttpChannel(Channel):
    """POST /message {"chat": "...", "text": "..."} with `Authorization: Bearer <token>` -> {"reply": "..."}."""
    name = "http"

    def __init__(self, cfg: Dict[str, Any]):
        self.token = resolve_secret(cfg.get("token"))
        self.agent_dir = Path(cfg.get("_agent_dir") or ".")
        if not self.token:
            raise ValueError("http: a token is required (requests must send it as a bearer token)")
        self.inbox: "queue.Queue[Inbound]" = queue.Queue()
        outer = self

        class H(http.server.BaseHTTPRequestHandler):
            def log_message(self, *_a: Any) -> None:
                pass

            def _json(self, status: int, obj: Dict[str, Any]) -> None:
                body = json.dumps(obj).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self) -> None:  # noqa: N802
                self._json(200, {"status": "ok", "app": "omni-gateway"}) if self.path == "/health" else self._json(404, {"error": "not found"})

            def do_POST(self) -> None:  # noqa: N802
                if self.path not in ("/message", "/signal"):
                    return self._json(404, {"error": "not found"})
                if not secrets.compare_digest(self.headers.get("Authorization", ""), f"Bearer {outer.token}"):
                    return self._json(401, {"error": "bad or missing bearer token"})
                if self.path == "/signal":  # wake jobs waiting `on <name>` (webhooks, CI, other programs)
                    try:
                        n = int(self.headers.get("Content-Length") or 0)
                        data = json.loads(self.rfile.read(n).decode("utf-8")) if n else {}
                        from .caps.scheduler import send_signal
                        waiting = send_signal(outer.agent_dir, str(data["name"]), str(data.get("payload") or ""))
                    except (ValueError, KeyError, TypeError) as e:
                        return self._json(400, {"error": f"body must be JSON with a valid `name`: {e}"})
                    return self._json(200, {"queued": True, "waiting_jobs": waiting})
                try:
                    n = int(self.headers.get("Content-Length") or 0)
                    data = json.loads(self.rfile.read(n).decode("utf-8")) if n else {}
                    text, chat = str(data["text"]), str(data.get("chat") or "default")
                except (ValueError, KeyError, TypeError):
                    return self._json(400, {"error": "body must be JSON with a `text` field"})
                done, box = threading.Event(), {}

                def reply(t: str) -> None:
                    box["reply"] = t
                    done.set()
                outer.inbox.put(Inbound("http", chat, "http", text, reply=reply))
                if not done.wait(float(data.get("timeout") or 600)):
                    return self._json(504, {"error": "the agent did not answer in time"})
                self._json(200, {"reply": box.get("reply", "")})

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", int(cfg.get("port") or 18790)), H)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True, name="omni-gateway-http").start()

    def poll(self, timeout: float) -> List[Inbound]:
        try:
            first = self.inbox.get(timeout=timeout)
        except queue.Empty:
            return []
        out = [first]
        while not self.inbox.empty():
            out.append(self.inbox.get_nowait())
        return out

    def send(self, chat: str, text: str) -> None:
        pass  # replies travel back in the HTTP response

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


CHANNEL_TYPES: Dict[str, Callable[[Dict[str, Any]], Channel]] = {"telegram": TelegramChannel, "http": HttpChannel}


def register_channel(name: str, factory: Callable[[Dict[str, Any]], Channel]) -> None:
    CHANNEL_TYPES[name] = factory


# ------------------------------------------------------------------------------------------- gateway
def config_path(agent_dir: Path) -> Path:
    return Path(agent_dir) / "gateway.json"


def load_config(agent_dir: Path) -> Dict[str, Any]:
    try:
        d = json.loads(config_path(agent_dir).read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def save_config(agent_dir: Path, cfg: Dict[str, Any]) -> None:
    p = config_path(agent_dir)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    os.replace(tmp, p)


@dataclass
class Conversation:
    key: str
    session: Any = None
    lock: threading.Lock = field(default_factory=threading.Lock)
    notify: Optional[Callable[[str], None]] = None      # send an extra message into this conversation (not for HTTP)
    pending: Optional["queue.Queue[str]"] = None        # an approval question waiting for the user's reply


class Gateway:
    def __init__(self, agent_dir: Path, cwd: Optional[str] = None, config: Optional[Dict[str, Any]] = None):
        self.agent_dir = Path(agent_dir)
        self.config = config if config is not None else load_config(self.agent_dir)
        self.cwd = str(self.config.get("cwd") or cwd or os.getcwd())
        self.channels: Dict[str, Channel] = {}
        self.errors: List[str] = []
        for name, cfg in (self.config.get("channels") or {}).items():
            factory = CHANNEL_TYPES.get(str(cfg.get("type") or name)) if isinstance(cfg, dict) else None
            if factory is None:
                self.errors.append(f"channel '{name}': unknown type")
                continue
            if cfg.get("enabled") is False:
                continue
            try:
                self.channels[name] = factory(dict(cfg, _agent_dir=str(self.agent_dir)))
            except Exception as e:
                self.errors.append(f"channel '{name}': {e}")
        self.conversations: Dict[str, Conversation] = {}
        self._state_dir = self.agent_dir / "gateway"
        self._stop = threading.Event()
        self._threads: List[threading.Thread] = []
        self._guard = threading.Lock()

    # --- persistence of conversation -> session file, and pairing codes
    def _load_state(self, name: str) -> Dict[str, Any]:
        try:
            d = json.loads((self._state_dir / name).read_text(encoding="utf-8"))
            return d if isinstance(d, dict) else {}
        except (OSError, ValueError):
            return {}

    def _save_state(self, name: str, data: Dict[str, Any]) -> None:
        self._state_dir.mkdir(parents=True, exist_ok=True)
        tmp = self._state_dir / (name + ".tmp")
        tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
        os.replace(tmp, self._state_dir / name)

    # --- access control
    def allowed(self, msg: Inbound) -> bool:
        if msg.channel == "http":
            return True  # already authenticated by its bearer token
        allow = [str(x) for x in ((self.config.get("channels") or {}).get(msg.channel) or {}).get("allow") or []]
        return msg.sender in allow or "*" in allow

    def pairing_code(self, msg: Inbound) -> str:
        with self._guard:
            pending = {c: p for c, p in self._load_state("pairing.json").items() if time.time() - p.get("at", 0) < PAIRING_TTL}
            code = next((c for c, p in pending.items() if p["channel"] == msg.channel and p["sender"] == msg.sender), None)
            if code is None:
                code = secrets.token_hex(3).upper()
                pending[code] = {"channel": msg.channel, "sender": msg.sender, "name": msg.name, "at": time.time()}
            self._save_state("pairing.json", pending)
        return code

    # --- sessions
    def session_for(self, key: str, fresh: bool = False) -> Any:
        from .agent import AgentSession, SessionOptions
        from .modes import HeadlessUI
        conv = self.conversations.setdefault(key, Conversation(key))
        if conv.session is not None and not fresh:
            return conv.session
        if conv.session is not None:
            try:
                conv.session.shutdown()
            except Exception:
                pass
        files = self._load_state("conversations.json")
        path = None if fresh else files.get(key)
        if path and not os.path.isfile(path):
            path = None
        opts = SessionOptions(
            cwd=self.cwd, mode="print", profile=self.config.get("profile") or None, model=self.config.get("model") or None,
            agent_dir=str(self.agent_dir), session=path, session_name=f"gateway {key}", approve=True,
            append_system_prompt="You are being used through a chat app. Keep answers short and plain (no tables, minimal "
                                 "markdown). Nobody can answer interactive prompts; if you need a decision, ask in your reply.")
        s = AgentSession(opts, ui=HeadlessUI())
        s.approver = lambda question, c=conv: self._ask_in_chat(c, question)   # used by the `approvals` capability
        s.gateway_target = key                                                  # scheduled jobs created here report back here
        s.start()
        conv.session = s
        files[key] = str(s.sessions.file or "")
        self._save_state("conversations.json", files)
        return s

    def _ask_in_chat(self, conv: Conversation, question: str, timeout: float = 300.0) -> str:
        if conv.notify is None:
            return "unattended"  # nowhere to ask (HTTP request/response, tests calling handle directly)
        conv.pending = queue.Queue()
        try:
            conv.notify(question + "\nReply yes, always or no.")
            return conv.pending.get(timeout=timeout)
        except queue.Empty:
            return "deny"
        finally:
            conv.pending = None

    # --- handling
    def handle(self, msg: Inbound) -> str:
        """Process one inbound message and return the reply text (also used directly by tests)."""
        if not self.allowed(msg):
            code = self.pairing_code(msg)
            return (f"This assistant is private. To get access, ask its owner to run:\n  omni gateway approve {code}\n"
                    "(the code is valid for one hour)")
        key = f"{msg.channel}:{msg.chat}"
        conv = self.conversations.setdefault(key, Conversation(key))
        cmd = msg.text.strip().split()[0].lower() if msg.text.strip().startswith("/") else ""
        cmd = cmd.split("@")[0]
        if conv.pending is not None and not cmd:  # this message answers an approval question
            word = msg.text.strip().lower().rstrip(".!")
            answer = "once" if word in ("yes", "y", "ok", "approve", "allow") else "always" if word == "always" else "deny"
            conv.pending.put(answer)
            return {"once": "Approved once.", "always": "Approved; I will not ask about this kind again.", "deny": "Denied."}[answer]
        if cmd == "/stop":
            if conv.session is not None and conv.session.is_streaming:
                conv.session.abort()
                return "Stopped."
            return "Nothing is running."
        with conv.lock:
            if cmd in ("/new", "/start", "/reset"):
                self.session_for(key, fresh=True)
                return "Started a new conversation."
            s = self.session_for(key)
            if cmd == "/status":
                from .ai.models import model_key
                u = s.context_usage()
                pct = f"{u.get('percent', 0):.0f}%" if isinstance(u, dict) and u.get("percent") is not None else "?"
                return (f"model {model_key(s.model)} · profile {s.profile.get('name')} · context {pct} · "
                        f"{len(s.active_tool_names)} tools · session {s.sessions.session_id[:8]}")
            before = len(s.sessions.entries)
            try:
                s.prompt(msg.text, source=f"gateway:{msg.channel}")
            except Exception as e:
                return f"Something went wrong: {type(e).__name__}: {e}"
            new = [e["message"] for e in s.sessions.entries[before:] if e.get("type") == "message" and e["message"].get("role") == "assistant"]
            if not new:
                return "(no answer)"
            last = new[-1]
            text = "".join(b.get("text", "") for b in last.get("content") or [] if b.get("type") == "text").strip()
            if last.get("stopReason") == "error":
                return f"The model returned an error: {last.get('errorMessage') or 'unknown'}"
            return text or "(done, with nothing to say)"

    def _dispatch(self, channel: Channel, msg: Inbound) -> None:
        stop_typing = threading.Event()

        def keep_typing() -> None:
            while not stop_typing.wait(4.0):
                channel.typing(msg.chat)
        if msg.reply is None:
            self.conversations.setdefault(f"{msg.channel}:{msg.chat}", Conversation(f"{msg.channel}:{msg.chat}")).notify = \
                lambda text, c=channel, chat=msg.chat: c.send(chat, text)
        channel.typing(msg.chat)
        threading.Thread(target=keep_typing, daemon=True).start()
        try:
            reply = self.handle(msg)
        finally:
            stop_typing.set()
        try:
            msg.reply(reply) if msg.reply is not None else channel.send(msg.chat, reply)
        except Exception as e:
            self.errors.append(f"{channel.name}: could not deliver a reply: {e}")

    def _loop(self, channel: Channel) -> None:
        while not self._stop.is_set():
            try:
                batch = channel.poll(20.0 if channel.name != "http" else 1.0)
            except Exception as e:
                self.errors.append(f"{channel.name}: {e}")
                time.sleep(2.0)
                continue
            for msg in batch:
                threading.Thread(target=self._dispatch, args=(channel, msg), daemon=True, name=f"omni-gw-{channel.name}").start()

    def start(self) -> None:
        for ch in self.channels.values():
            t = threading.Thread(target=self._loop, args=(ch,), daemon=True, name=f"omni-gateway-{ch.name}")
            t.start()
            self._threads.append(t)

    def stop(self) -> None:
        self._stop.set()
        for ch in self.channels.values():
            try:
                ch.close()
            except Exception:
                pass
        for conv in self.conversations.values():
            if conv.session is not None:
                try:
                    conv.session.shutdown()
                except Exception:
                    pass


def deliver(agent_dir: Path, target: str, text: str) -> Optional[str]:
    """Send `text` to a conversation ("telegram:<chat id>") using the channel settings in gateway.json.
    Returns None on success, else why it could not be delivered. Used for scheduled-job results."""
    name, _, chat = str(target).partition(":")
    cfg = (load_config(agent_dir).get("channels") or {}).get(name)
    if not isinstance(cfg, dict) or not chat:
        return f"no channel '{name}' in gateway.json"
    factory = CHANNEL_TYPES.get(str(cfg.get("type") or name))
    if factory is None or name == "http":
        return f"channel '{name}' cannot receive unprompted messages"
    try:
        ch = factory(cfg)
        try:
            ch.send(chat, text)
        finally:
            ch.close()
    except Exception as e:
        return f"{type(e).__name__}: {e}"
    return None


def approve(agent_dir: Path, code: str) -> str:
    state = Path(agent_dir) / "gateway" / "pairing.json"
    try:
        pending = json.loads(state.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        pending = {}
    p = pending.get(code.strip().upper())
    if not p or time.time() - p.get("at", 0) > PAIRING_TTL:
        return f"No pending pairing request with code {code}."
    cfg = load_config(agent_dir)
    ch = cfg.setdefault("channels", {}).setdefault(p["channel"], {})
    allow = [str(x) for x in ch.get("allow") or []]
    if p["sender"] not in allow:
        allow.append(p["sender"])
    ch["allow"] = allow
    save_config(agent_dir, cfg)
    del pending[code.strip().upper()]
    state.write_text(json.dumps(pending, indent=2), encoding="utf-8")
    return f"Approved {p.get('name') or p['sender']} on {p['channel']}. Restart the gateway if it is running."


def cli(argv: List[str], agent_dir: Path) -> int:
    sub = argv[0] if argv else "run"
    if sub == "approve" and len(argv) > 1:
        print(approve(agent_dir, argv[1]))
        return 0
    cfg = load_config(agent_dir)
    if sub == "status":
        chans = cfg.get("channels") or {}
        print(f"config: {config_path(agent_dir)}" + ("" if chans else "  (no channels configured)"))
        for name, c in chans.items():
            print(f"  {name}: {'on' if c.get('enabled') is not False else 'off'} · allow {c.get('allow') or '[]'}")
        return 0
    if sub != "run":
        print("usage: omni gateway [run] | status | approve <code>")
        return 2
    if not cfg.get("channels"):
        print(f"No channels configured. Create {config_path(agent_dir)}, for example:\n" + json.dumps(
            {"profile": "assistant", "channels": {"telegram": {"token": "$TELEGRAM_BOT_TOKEN", "allow": []}}}, indent=2))
        return 1
    gw = Gateway(agent_dir)
    for e in gw.errors:
        print(f"warning: {e}")
    if not gw.channels:
        print("No channel could be started.")
        return 1
    gw.start()
    print(f"omni gateway running · channels: {', '.join(gw.channels)} · working directory {gw.cwd} · Ctrl+C to stop")
    try:
        while True:
            time.sleep(1.0)
    except KeyboardInterrupt:
        pass
    finally:
        gw.stop()
    return 0
