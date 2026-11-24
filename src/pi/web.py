"""`omni web`: the agent in a browser tab (Hermes' dashboard, OpenClaw's control UI).

    omni web [--port N] [--profile NAME] [--model PATTERN] [--no-open]

Starts a small web server on this machine only (127.0.0.1) and opens a chat page: streamed answers, tool calls
with their results folded away, slash commands (`/model`, `/capabilities`, `/board` ... type `/` for the list),
pickers and confirmations as dialogs, an interrupt button, and the same sessions, profiles, memory and tools as the
terminal. The header has a session list (switch to an earlier conversation of this folder, or start a new one) and
a model picker listing the models you are signed in to. Images can be pasted or dropped into the message box
(at most 6 per message, 8 MB each) for models that accept them. The page address carries a random access key; without it every request is refused, so other programs
and other users of the machine cannot drive your agent. It is one page with no external scripts or fonts.

It is meant for one person on their own machine. To reach it from elsewhere, forward the port through SSH or a
VPN rather than binding it to a public address; `--host` exists for that case and prints a warning.
"""
from __future__ import annotations

import http.server
import json
import queue
import secrets
import threading
import time
import webbrowser
from typing import Any, Callable, Dict, List, Optional
from urllib.parse import parse_qs, urlparse

MAX_BODY = 60_000_000          # room for a few images
MAX_IMAGES, MAX_IMAGE_BYTES = 6, 8_000_000
IMAGE_TYPES = ("image/png", "image/jpeg", "image/gif", "image/webp")
ASK_TIMEOUT = 300.0


def _text(content: Any) -> str:
    if isinstance(content, str):
        return content
    return "".join(b.get("text", "") for b in content or [] if isinstance(b, dict) and b.get("type") == "text")


class Hub:
    """Fans session events out to every open page, and carries dialog questions to the page and answers back."""

    def __init__(self) -> None:
        self.clients: List["queue.Queue[Dict[str, Any]]"] = []
        self.lock = threading.Lock()
        self.pending: Dict[str, Dict[str, Any]] = {}

    def publish(self, ev: Dict[str, Any]) -> None:
        with self.lock:
            for q in self.clients:
                q.put(ev)

    def join(self) -> "queue.Queue[Dict[str, Any]]":
        q: "queue.Queue[Dict[str, Any]]" = queue.Queue()
        with self.lock:
            self.clients.append(q)
        return q

    def leave(self, q: "queue.Queue[Dict[str, Any]]") -> None:
        with self.lock:
            if q in self.clients:
                self.clients.remove(q)

    def ask(self, kind: str, title: str, **extra: Any) -> Any:
        """Show a dialog in the page and wait for the answer; None when nobody answers (no page open, timeout)."""
        with self.lock:
            if not self.clients:
                return None
        qid = secrets.token_hex(6)
        entry = {"event": threading.Event(), "answer": None}
        self.pending[qid] = entry
        self.publish({"type": "ask", "id": qid, "kind": kind, "title": title, **extra})
        entry["event"].wait(ASK_TIMEOUT)
        self.pending.pop(qid, None)
        self.publish({"type": "ask_done", "id": qid})
        return entry["answer"]

    def answer(self, qid: str, value: Any) -> bool:
        entry = self.pending.get(qid)
        if entry is None:
            return False
        entry["answer"] = value
        entry["event"].set()
        return True


class WebPort:
    """What slash commands talk to (the terminal app's counterpart): notices, lists, pickers, confirmations."""

    def __init__(self, hub: Hub):
        self.hub = hub

    def notify(self, text: str, level: str = "info") -> None:
        self.hub.publish({"type": "notice", "level": level, "message": str(text)})

    def show(self, title: str, lines: List[str]) -> None:
        self.hub.publish({"type": "panel", "title": str(title), "text": "\n".join(str(x) for x in lines)})

    def select(self, title: str, options: List[str], initial: Optional[int] = None) -> Optional[int]:
        r = self.hub.ask("select", str(title), options=[str(o) for o in options], initial=initial)
        return r if isinstance(r, int) and 0 <= r < len(options) else None

    def confirm(self, title: str, message: str = "") -> bool:
        return self.hub.ask("confirm", str(title), message=str(message)) is True

    def input(self, title: str, placeholder: str = "", prefill: str = "", secret: bool = False) -> Optional[str]:
        r = self.hub.ask("input", str(title), placeholder=placeholder, prefill=prefill, secret=bool(secret))
        return r if isinstance(r, str) else None

    def reset_transcript(self) -> None:
        self.hub.publish({"type": "reset"})

    def set_editor_text(self, text: str) -> None:
        self.hub.publish({"type": "editor", "text": str(text)})

    def copy(self, text: str) -> None:
        self.hub.publish({"type": "copy", "text": str(text)})

    def __getattr__(self, name: str) -> Callable[..., None]:      # apply_theme, quit, screen_dump: nothing to do in a page
        return lambda *a, **k: None


class WebUI:
    """The session's UI object: permission questions and extension dialogs go to the page."""
    has_ui = True
    width, height = 100, 30

    def __init__(self, hub: Hub):
        self.hub = hub
        self.port = WebPort(hub)

    def select(self, title: str, options: List[str], initial: Optional[int] = None) -> Optional[int]:
        return self.port.select(title, options, initial)

    def confirm(self, title: str, message: str = "") -> bool:
        return self.port.confirm(title, message)

    def input(self, title: str, placeholder: str = "", prefill: str = "", secret: bool = False) -> Optional[str]:
        return self.port.input(title, placeholder, prefill, secret)

    def notify(self, text: str, level: str = "info") -> None:
        self.port.notify(text, level)

    def confirm_trust(self, cwd: str) -> Optional[bool]:
        return None

    # what the agent loop, capabilities and extensions call
    def ext_select(self, title: str = "", options: Optional[List[str]] = None, opts: Any = None, **_: Any) -> Optional[str]:
        items = [str(o) for o in options or []]
        i = self.port.select(title, items)
        return items[i] if i is not None else None

    def ext_confirm(self, title: str = "", message: str = "", opts: Any = None, **_: Any) -> bool:
        return self.port.confirm(title, message)

    def ext_input(self, title: str = "", placeholder: str = "", opts: Any = None, **_: Any) -> Optional[str]:
        return self.port.input(title, placeholder or "")

    def ext_editor(self, title: str = "", prefill: str = "", **_: Any) -> Optional[str]:
        return self.port.input(title, "", prefill or "")

    def ext_notify(self, message: str = "", type: str = "info", **_: Any) -> None:
        self.port.notify(message, {"warning": "warning", "error": "error"}.get(type, "info"))

    def ext_pasteToEditor(self, text: str = "", **_: Any) -> None:
        self.port.set_editor_text(text)

    def __getattr__(self, name: str) -> Callable[..., None]:
        if name.startswith("_"):
            raise AttributeError(name)
        return lambda *a, **k: None


def clean_images(images: Any) -> List[Dict[str, Any]]:
    """Images from the page as the session takes them; raises ValueError with a message for the user."""
    import base64
    import binascii
    if not images:
        return []
    if not isinstance(images, list) or len(images) > MAX_IMAGES:
        raise ValueError(f"at most {MAX_IMAGES} images per message")
    out = []
    for img in images:
        if not isinstance(img, dict) or img.get("mimeType") not in IMAGE_TYPES or not isinstance(img.get("data"), str):
            raise ValueError("images must be PNG, JPEG, GIF or WebP")
        try:
            size = len(base64.b64decode(img["data"], validate=True))
        except (binascii.Error, ValueError):
            raise ValueError("an image could not be read") from None
        if size > MAX_IMAGE_BYTES:
            raise ValueError(f"an image is larger than {MAX_IMAGE_BYTES // 1_000_000} MB")
        out.append({"type": "image", "data": img["data"], "mimeType": img["mimeType"]})
    return out


def history(session: Any) -> List[Dict[str, Any]]:
    """The current branch as the page shows it."""
    rows: List[Dict[str, Any]] = []
    for e in session.sessions.get_branch():
        if e.get("type") != "message":
            continue
        m = e["message"]
        role = m.get("role")
        if role == "user":
            c = m.get("content")
            pics = sum(1 for b in c if isinstance(b, dict) and b.get("type") == "image") if isinstance(c, list) else 0
            rows.append({"type": "user", "text": _text(c), "images": pics})
        elif role == "assistant":
            text = _text(m.get("content"))
            if text:
                rows.append({"type": "assistant", "text": text})
            for b in m.get("content") or []:
                if isinstance(b, dict) and b.get("type") == "toolCall":
                    rows.append({"type": "tool_start", "id": b.get("id"), "name": b.get("name"), "args": b.get("arguments") or {}})
            if m.get("stopReason") == "error":
                rows.append({"type": "notice", "level": "error", "message": m.get("errorMessage") or "error"})
        elif role == "toolResult":
            rows.append({"type": "tool_end", "id": m.get("toolCallId"), "name": m.get("toolName"), "ok": not m.get("isError"),
                         "text": _text(m.get("content"))[:20_000]})
    return rows


def state(session: Any) -> Dict[str, Any]:
    from .ai import model_key
    u = session.context_usage()
    return {"model": model_key(session.model) if session.model else "", "profile": session.profile.get("name", ""),
            "cwd": session.cwd, "busy": bool(session.is_streaming), "name": session.sessions.session_name() or "",
            "context": {"tokens": u.get("tokens"), "window": u.get("contextWindow"), "percent": u.get("percent")},
            "thinking": session.thinking_level}


def translate(ev: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """A session event -> the smaller event the page understands (None: not shown)."""
    t = ev.get("type")
    if t == "message_update":
        d = ev.get("assistantMessageEvent") or {}
        if d.get("type") == "text_delta":
            return {"type": "delta", "text": d.get("delta", "")}
        if d.get("type") == "thinking_delta":
            return {"type": "thinking", "text": d.get("delta", "")}
        return None
    if t == "message_end":
        m = ev.get("message") or {}
        if m.get("role") == "assistant":
            return {"type": "message_end", "error": m.get("errorMessage") if m.get("stopReason") == "error" else None}
        return None
    if t == "tool_execution_start":
        return {"type": "tool_start", "id": ev.get("toolCallId"), "name": ev.get("toolName"), "args": ev.get("args") or {}}
    if t == "tool_execution_end":
        return {"type": "tool_end", "id": ev.get("toolCallId"), "name": ev.get("toolName"), "ok": not ev.get("isError"),
                "text": _text((ev.get("result") or {}).get("content"))[:20_000]}
    if t == "agent_end":
        return {"type": "done"}
    if t == "notice":
        return {"type": "notice", "level": ev.get("level", "info"), "message": str(ev.get("message", ""))}
    if t in ("auto_retry_start", "queue_update", "session_info_changed", "model_select"):
        return {"type": "state"}
    return None


class WebApp:
    def __init__(self, session: Any, hub: Hub, token: str):
        from .commands import Commands
        self.session, self.hub, self.token = session, hub, token
        self.commands = Commands(session, WebPort(hub))
        self.worker_lock = threading.Lock()
        session.subscribe(self._on_event)

    def _on_event(self, ev: Dict[str, Any]) -> None:
        out = translate(ev)
        if out is not None:
            self.hub.publish(out)

    def submit(self, text: str, images: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
        text = text.strip()
        try:
            images = clean_images(images)
        except ValueError as e:
            return {"ok": False, "error": str(e)}
        if not text and not images:
            return {"ok": False, "error": "empty message"}
        s = self.session
        if images and (text.startswith("/") or s.is_streaming):
            return {"ok": False, "error": "images can only be sent with a new message, not with a command or while the agent works"}
        if images and s.model and "image" not in (s.model.get("input") or ["text"]):
            return {"ok": False, "error": f"{s.model.get('id')} does not accept images; pick another model"}
        if text.startswith("/"):
            name, _, args = text[1:].partition(" ")

            def run_command() -> None:
                try:
                    if not self.commands.run(name, args.strip()):
                        self._prompt(text)          # an extension command, prompt template or skill
                except Exception as e:
                    self.hub.publish({"type": "notice", "level": "error", "message": f"/{name} failed: {type(e).__name__}: {e}"})
                self.hub.publish({"type": "state"})
            threading.Thread(target=run_command, daemon=True, name="omni-web-command").start()
            return {"ok": True, "kind": "command"}
        if s.is_streaming:
            s.steer(text)
            self.hub.publish({"type": "user", "text": text, "steer": True})
            return {"ok": True, "kind": "steer"}
        threading.Thread(target=self._prompt, args=(text, images), daemon=True, name="omni-web-prompt").start()
        return {"ok": True, "kind": "prompt"}

    def _prompt(self, text: str, images: Optional[List[Dict[str, Any]]] = None) -> None:
        with self.worker_lock:
            self.hub.publish({"type": "user", "text": text, "images": len(images or [])})
            try:
                self.session.prompt(text, images=images or None)
            except Exception as e:
                self.hub.publish({"type": "notice", "level": "error", "message": f"{type(e).__name__}: {e}"})
            self.hub.publish({"type": "done"})
            self.hub.publish({"type": "state"})

    def sessions(self) -> List[Dict[str, Any]]:
        from .session import SessionManager
        current = str(self.session.sessions.file or "")
        rows = SessionManager.list_sessions(self.session.sessions.session_dir)[:100]
        return [{"path": r["path"], "name": r.get("name") or "", "first": r.get("firstMessage") or "", "messages": r.get("messageCount", 0),
                 "mtime": r.get("mtime"), "current": r["path"] == current}
                for r in rows if r.get("messageCount") or r["path"] == current]     # skip conversations that never started

    def switch_session(self, action: str, path: str = "") -> Dict[str, Any]:
        s = self.session
        deadline = time.time() + 3                 # "done" reaches the page a moment before the turn has settled
        while s.is_streaming and time.time() < deadline:
            time.sleep(0.05)
        if s.is_streaming:
            return {"ok": False, "error": "the agent is working; stop it first"}
        if action == "new":
            ok = s.new_session()
        elif action == "resume":
            known = {r["path"] for r in self.sessions()}
            if path not in known:                  # only this folder's own sessions, never an arbitrary file
                return {"ok": False, "error": "no such session in this folder"}
            ok = s.resume(path)
        else:
            return {"ok": False, "error": "action must be new or resume"}
        if ok:
            self.hub.publish({"type": "reset"})
        return {"ok": bool(ok)}

    def models(self) -> Dict[str, Any]:
        from .ai import model_key
        s, signed = self.session, {}
        rows = []
        for m in s.registry.all():
            prov = m["provider"]
            if prov not in signed:
                try:
                    signed[prov] = bool(s.auth.get_api_key(prov))
                except Exception:
                    signed[prov] = False
            if signed[prov]:
                rows.append({"key": model_key(m), "name": m.get("name") or m["id"], "provider": prov,
                             "images": "image" in (m.get("input") or ["text"]), "reasoning": bool(m.get("reasoning"))})
        return {"current": model_key(s.model) if s.model else "", "models": rows}

    def choose_model(self, key: str) -> Dict[str, Any]:
        provider, _, mid = str(key).partition("/")
        m = self.session.registry.find(provider, mid)
        if m is None or not self.session.auth.get_api_key(provider):
            return {"ok": False, "error": f"unknown model, or not signed in: {key}"}
        self.session.set_model(m)
        self.hub.publish({"type": "state"})
        return {"ok": True}

    def command_list(self) -> List[Dict[str, str]]:
        try:
            return [{"name": c["name"], "description": c.get("description") or ""} for c in self.commands.all_commands()]
        except Exception:
            return []


def make_handler(app: WebApp) -> type:
    class Handler(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *_a: Any) -> None:
            pass

        def _send(self, status: int, body: bytes, ctype: str = "application/json") -> None:
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, status: int, obj: Any) -> None:
            self._send(status, json.dumps(obj).encode("utf-8"))

        def _allowed(self, query: Dict[str, List[str]]) -> bool:
            given = self.headers.get("X-Omni-Key") or (query.get("k") or [""])[0]
            # the Host check stops a web page elsewhere from reaching this server through a rebound DNS name
            host = (self.headers.get("Host") or "").split(":")[0]
            return secrets.compare_digest(given, app.token) and host in ("127.0.0.1", "localhost", "[::1]", app_host(self))

        def do_GET(self) -> None:  # noqa: N802
            u = urlparse(self.path)
            q = parse_qs(u.query)
            if u.path == "/health":
                return self._json(200, {"status": "ok", "app": "omni-web"})
            if not self._allowed(q):
                return self._send(403, b"Forbidden: open the address printed by `omni web`, including its key.", "text/plain; charset=utf-8")
            if u.path == "/":
                return self._send(200, PAGE.encode("utf-8"), "text/html; charset=utf-8")
            if u.path == "/api/state":
                return self._json(200, {**state(app.session), "history": history(app.session), "commands": app.command_list()})
            if u.path == "/api/events":
                return self._events()
            if u.path == "/api/sessions":
                return self._json(200, {"sessions": app.sessions()})
            if u.path == "/api/models":
                return self._json(200, app.models())
            self._json(404, {"error": "not found"})

        def _events(self) -> None:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "close")
            self.end_headers()
            q = app.hub.join()
            try:
                self.wfile.write(b": connected\n\n")
                self.wfile.flush()
                while True:
                    try:
                        ev = q.get(timeout=15)
                        if ev.get("type") == "state":
                            ev = {"type": "state", **state(app.session)}
                        data = f"data: {json.dumps(ev)}\n\n".encode("utf-8")
                    except queue.Empty:
                        data = b": ping\n\n"
                    self.wfile.write(data)
                    self.wfile.flush()
            except (OSError, ValueError):
                pass
            finally:
                app.hub.leave(q)
                self.close_connection = True

        def do_POST(self) -> None:  # noqa: N802
            u = urlparse(self.path)
            if not self._allowed(parse_qs(u.query)):
                return self._json(403, {"error": "forbidden"})
            try:
                n = int(self.headers.get("Content-Length") or 0)
                if n > MAX_BODY:
                    return self._json(413, {"error": "too large"})
                data = json.loads(self.rfile.read(n).decode("utf-8")) if n else {}
                if not isinstance(data, dict):
                    raise ValueError("body must be an object")
            except (ValueError, UnicodeDecodeError) as e:
                return self._json(400, {"error": f"bad JSON: {e}"})
            if u.path == "/api/prompt":
                r = app.submit(str(data.get("text") or ""), data.get("images"))
                return self._json(200 if r["ok"] else 400, r)
            if u.path == "/api/session":
                r = app.switch_session(str(data.get("action") or ""), str(data.get("path") or ""))
                return self._json(200 if r["ok"] else 400, r)
            if u.path == "/api/model":
                r = app.choose_model(str(data.get("key") or ""))
                return self._json(200 if r["ok"] else 400, r)
            if u.path == "/api/abort":
                app.session.abort()
                return self._json(200, {"ok": True})
            if u.path == "/api/answer":
                return self._json(200, {"ok": app.hub.answer(str(data.get("id") or ""), data.get("value"))})
            self._json(404, {"error": "not found"})

    def app_host(handler: Any) -> str:
        return getattr(handler.server, "omni_host", "127.0.0.1")

    return Handler


def serve(session: Any, host: str = "127.0.0.1", port: int = 0, token: Optional[str] = None) -> Any:
    """Start the server for an already started session whose `ui` is a WebUI. Returns the server (`.url`, `.app`)."""
    hub = session.ui.hub if isinstance(getattr(session, "ui", None), WebUI) else Hub()
    token = token or secrets.token_urlsafe(18)
    app = WebApp(session, hub, token)
    server = http.server.ThreadingHTTPServer((host, port), make_handler(app))
    server.daemon_threads = True
    server.omni_host = host
    server.app = app
    server.token = token
    shown = "127.0.0.1" if host in ("0.0.0.0", "") else host
    server.url = f"http://{shown}:{server.server_address[1]}/?k={token}"
    threading.Thread(target=server.serve_forever, daemon=True, name="omni-web").start()
    return server


def cli(argv: List[str], make_session: Callable[..., Any]) -> int:
    """`make_session(profile, model, ui)` returns a started session."""
    host, port, profile, model, open_browser = "127.0.0.1", 8787, None, None, True
    it = iter(argv)
    for a in it:
        try:
            if a == "--port":
                port = int(next(it))
            elif a == "--host":
                host = next(it)
            elif a == "--profile":
                profile = next(it)
            elif a == "--model":
                model = next(it)
            elif a == "--no-open":
                open_browser = False
            elif a in ("-h", "--help"):
                print("usage: omni web [--port N] [--profile NAME] [--model PATTERN] [--no-open] [--host ADDRESS]")
                return 0
            else:
                print(f"omni web: unknown option {a}")
                return 2
        except (StopIteration, ValueError):
            print(f"omni web: {a} needs a value")
            return 2
    ui = WebUI(Hub())
    session = make_session(profile, model, ui)
    try:
        server = serve(session, host, port)
    except OSError as e:
        print(f"omni web: could not listen on {host}:{port}: {e}")
        return 1
    print(f"\n  Omni is running at {server.url}\n  (the address includes your access key; do not share it)  ·  Ctrl+C to stop\n", flush=True)
    if host not in ("127.0.0.1", "localhost", "::1"):
        print("  Warning: listening beyond this machine over plain HTTP. Prefer an SSH tunnel or VPN.\n")
    if open_browser:
        try:
            webbrowser.open(server.url)
        except Exception:
            pass
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    server.shutdown()
    server.server_close()
    try:
        session.shutdown()
    except Exception:
        pass
    return 0


PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Omni</title>
<style>
:root{--bg:#f7f7f5;--panel:#fff;--ink:#1c1c1a;--dim:#6b6b66;--line:#e2e1dc;--accent:#2f6f5e;--user:#eef3f1;--err:#b3261e;--code:#f0efea}
@media (prefers-color-scheme:dark){:root{--bg:#161614;--panel:#1f1f1c;--ink:#ecebe6;--dim:#9a9992;--line:#33332f;--accent:#6fbfa6;--user:#232b28;--err:#f2867f;--code:#262622}}
*{box-sizing:border-box}html,body{height:100%}
body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.55 system-ui,-apple-system,"Segoe UI",sans-serif;display:flex;flex-direction:column}
header{display:flex;gap:12px;align-items:center;padding:10px 16px;border-bottom:1px solid var(--line);background:var(--panel);flex-wrap:wrap}
header b{font-size:16px}header span{color:var(--dim);font-size:13px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;max-width:44ch}
#bar{flex:1}#ctx{font-variant-numeric:tabular-nums}
main{flex:1;overflow-y:auto;padding:16px}#log{max-width:860px;margin:0 auto;display:flex;flex-direction:column;gap:10px}
.msg{padding:10px 14px;border-radius:10px;white-space:pre-wrap;overflow-wrap:anywhere}
.user{background:var(--user);align-self:flex-end;max-width:85%}.assistant{background:var(--panel);border:1px solid var(--line)}
.thinking{color:var(--dim);font-style:italic;font-size:13px;border-left:2px solid var(--line);padding:2px 10px;white-space:pre-wrap}
.notice{color:var(--dim);font-size:13px;padding:2px 4px;white-space:pre-wrap}.notice.error{color:var(--err)}.notice.warning{color:var(--err)}
details.tool{border:1px solid var(--line);border-radius:8px;background:var(--panel);font-size:13px}
details.tool summary{cursor:pointer;padding:6px 10px;font-family:ui-monospace,Consolas,monospace;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
details.tool.fail summary{color:var(--err)}details.tool pre{margin:0;padding:8px 10px;border-top:1px solid var(--line);overflow:auto;max-height:340px;background:var(--code)}
.panel{border:1px solid var(--line);border-radius:8px;background:var(--panel)}.panel h4{margin:0;padding:6px 10px;border-bottom:1px solid var(--line);font-size:13px;color:var(--dim)}
.panel pre{margin:0;padding:8px 10px;overflow:auto;max-height:420px;font-size:13px}
pre,code{font-family:ui-monospace,Consolas,monospace}
footer{border-top:1px solid var(--line);background:var(--panel);padding:10px 16px}
#form{max-width:860px;margin:0 auto;display:flex;gap:8px;align-items:flex-end;position:relative}
textarea{flex:1;resize:none;min-height:44px;max-height:220px;padding:10px 12px;border:1px solid var(--line);border-radius:10px;background:var(--bg);color:var(--ink);font:inherit}
button{font:inherit;padding:10px 16px;border-radius:10px;border:1px solid var(--line);background:var(--panel);color:var(--ink);cursor:pointer}
button.primary{background:var(--accent);border-color:var(--accent);color:#fff}button:disabled{opacity:.5;cursor:default}
textarea:focus,button:focus-visible{outline:2px solid var(--accent);outline-offset:1px}
#hints{position:absolute;bottom:100%;left:0;right:0;margin-bottom:6px;background:var(--panel);border:1px solid var(--line);border-radius:10px;max-height:260px;overflow:auto;display:none}
#hints div{padding:6px 12px;cursor:pointer;display:flex;gap:10px}#hints div.on,#hints div:hover{background:var(--user)}#hints small{color:var(--dim);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
dialog{border:1px solid var(--line);border-radius:12px;background:var(--panel);color:var(--ink);max-width:min(560px,92vw);padding:18px}
dialog::backdrop{background:rgba(0,0,0,.4)}dialog h3{margin:0 0 10px;font-size:16px}dialog p{white-space:pre-wrap;color:var(--dim);margin:0 0 12px;max-height:40vh;overflow:auto}
dialog .opts{display:flex;flex-direction:column;gap:6px;max-height:50vh;overflow:auto}dialog .opts button{text-align:left}
.hb{padding:4px 10px;font-size:13px;border-radius:8px}.hb span{color:var(--dim)}
#pics{display:none;gap:6px;position:absolute;bottom:100%;left:0;margin-bottom:6px}#pics.on{display:flex}
#pics img{height:56px;border-radius:6px;border:1px solid var(--line);cursor:pointer}
main.drop{outline:2px dashed var(--accent);outline-offset:-8px}
.list{display:flex;flex-direction:column;gap:4px;max-height:60vh;overflow:auto}.list button{text-align:left;display:flex;flex-direction:column;gap:2px}
.list button small{color:var(--dim)}.list button.cur{border-color:var(--accent)}
dialog input.filter{margin-bottom:8px}
dialog .row{display:flex;gap:8px;justify-content:flex-end;margin-top:12px}dialog input{width:100%;padding:9px 10px;border:1px solid var(--line);border-radius:8px;background:var(--bg);color:var(--ink);font:inherit}
</style></head><body>
<header><b>Omni</b><button type="button" class="hb" id="pickModel" title="Choose model"><span id="model"></span></button>
<span id="cwd"></span><span id="bar"></span><span id="ctx"></span><button type="button" class="hb" id="pickSession">Sessions</button></header>
<main id="main"><div id="log" aria-live="polite"></div></main>
<footer><form id="form"><div id="hints" role="listbox"></div>
<div id="pics"></div><textarea id="box" rows="1" placeholder="Message Omni   ·   / for commands   ·   Enter to send, Shift+Enter for a new line" aria-label="Message"></textarea>
<button type="button" id="stop" hidden>Stop</button><button class="primary" id="send">Send</button></form></footer>
<dialog id="dlg"></dialog>
<script>
const key=new URLSearchParams(location.search).get('k')||'';
const $=id=>document.getElementById(id), log=$('log'), box=$('box'), main=$('main');
let cur=null, think=null, tools={}, commands=[], busy=false, hintIndex=0;
const api=(path,body)=>fetch(path,{method:body?'POST':'GET',headers:{'X-Omni-Key':key,'Content-Type':'application/json'},body:body?JSON.stringify(body):undefined}).then(r=>r.json());
function el(tag,cls,text){const e=document.createElement(tag);if(cls)e.className=cls;if(text!==undefined)e.textContent=text;return e}
function add(e){const near=main.scrollHeight-main.scrollTop-main.clientHeight<120;log.appendChild(e);if(near)main.scrollTop=main.scrollHeight;return e}
function stick(){if(main.scrollHeight-main.scrollTop-main.clientHeight<160)main.scrollTop=main.scrollHeight}
function setBusy(b){busy=b;$('stop').hidden=!b;$('send').textContent=b?'Steer':'Send'}
function showState(s){if(s.model!==undefined)$('model').textContent=s.model+(s.profile?' · '+s.profile:'');if(s.cwd)$('cwd').textContent=s.cwd;
 if(s.context&&s.context.window){$('ctx').textContent=Math.round(s.context.percent||0)+'% of context'}if(s.busy!==undefined)setBusy(s.busy);if(s.name)document.title=s.name+' · Omni'}
function argLine(a){try{const s=JSON.stringify(a);return s.length>140?s.slice(0,140)+'…':s}catch(e){return ''}}
function handle(ev){
 if(ev.type==='user'){cur=null;think=null;add(el('div','msg user',ev.text+(ev.images?'  ['+ev.images+' image'+(ev.images>1?'s':'')+']':'')));if(!ev.history)setBusy(true)}
 else if(ev.type==='assistant'){add(el('div','msg assistant',ev.text))}
 else if(ev.type==='delta'){if(!cur){cur=add(el('div','msg assistant',''));think=null}cur.textContent+=ev.text;stick()}
 else if(ev.type==='thinking'){if(!think){think=add(el('div','thinking',''));cur=null}think.textContent+=ev.text;stick()}
 else if(ev.type==='message_end'){cur=null;think=null;if(ev.error)add(el('div','notice error',ev.error))}
 else if(ev.type==='tool_start'){cur=null;think=null;const d=el('details','tool');d.appendChild(el('summary','',ev.name+' '+argLine(ev.args)+'  …'));tools[ev.id]=add(d)}
 else if(ev.type==='tool_end'){let d=tools[ev.id];if(!d){d=el('details','tool');d.appendChild(el('summary','',ev.name));add(d)}
  const s=d.querySelector('summary');s.textContent=s.textContent.replace(/  …$/,'')+(ev.ok?'  ✓':'  ✗');if(!ev.ok)d.classList.add('fail');
  if(!d.querySelector('pre'))d.appendChild(el('pre','',ev.text||'(no output)'));delete tools[ev.id]}
 else if(ev.type==='notice'){add(el('div','notice '+(ev.level||''),ev.message))}
 else if(ev.type==='panel'){const p=el('div','panel');p.appendChild(el('h4','',ev.title));p.appendChild(el('pre','',ev.text));add(p)}
 else if(ev.type==='done'){cur=null;think=null;setBusy(false)}
 else if(ev.type==='state'){showState(ev)}
 else if(ev.type==='reset'){log.textContent='';tools={};cur=null;load()}
 else if(ev.type==='editor'){box.value=ev.text;box.focus()}
 else if(ev.type==='copy'){navigator.clipboard&&navigator.clipboard.writeText(ev.text).catch(()=>{})}
 else if(ev.type==='ask'){ask(ev)}
 else if(ev.type==='ask_done'){const d=$('dlg');if(d.open&&d.dataset.id===ev.id)d.close()}
}
function ask(ev){const d=$('dlg');d.textContent='';d.dataset.id=ev.id;d.appendChild(el('h3','',ev.title));
 const reply=v=>{api('/api/answer',{id:ev.id,value:v});d.close()};
 if(ev.kind==='select'){const o=el('div','opts');ev.options.forEach((t,i)=>{const b=el('button','',t);b.type='button';b.onclick=()=>reply(i);o.appendChild(b)});d.appendChild(o);
  const r=el('div','row'),c=el('button','','Cancel');c.type='button';c.onclick=()=>reply(null);r.appendChild(c);d.appendChild(r)}
 else if(ev.kind==='confirm'){if(ev.message)d.appendChild(el('p','',ev.message));const r=el('div','row'),n=el('button','','No'),y=el('button','primary','Yes');
  n.type=y.type='button';n.onclick=()=>reply(false);y.onclick=()=>reply(true);r.appendChild(n);r.appendChild(y);d.appendChild(r)}
 else{const i=el('input');i.type=ev.secret?'password':'text';i.placeholder=ev.placeholder||'';i.value=ev.prefill||'';i.setAttribute('aria-label',ev.title);d.appendChild(i);
  const r=el('div','row'),c=el('button','','Cancel'),k=el('button','primary','OK');c.type=k.type='button';c.onclick=()=>reply(null);k.onclick=()=>reply(i.value);
  i.onkeydown=e=>{if(e.key==='Enter'){e.preventDefault();reply(i.value)}};r.appendChild(c);r.appendChild(k);d.appendChild(r)}
 d.oncancel=e=>{e.preventDefault();reply(ev.kind==='confirm'?false:null)};d.showModal()}
function hints(){const h=$('hints'),v=box.value;if(!v.startsWith('/')||v.includes(' ')||v.includes('\n')){h.style.display='none';return[]}
 const q=v.slice(1).toLowerCase(),m=commands.filter(c=>c.name.toLowerCase().startsWith(q)).slice(0,40);h.textContent='';if(!m.length){h.style.display='none';return[]}
 hintIndex=Math.min(hintIndex,m.length-1);m.forEach((c,i)=>{const r=el('div',i===hintIndex?'on':'');r.setAttribute('role','option');r.appendChild(el('span','','/'+c.name));r.appendChild(el('small','',c.description));
  r.onmousedown=e=>{e.preventDefault();box.value='/'+c.name+' ';h.style.display='none';box.focus()};h.appendChild(r)});h.style.display='block';return m}
let pics=[];
function showPics(){const p=$('pics');p.textContent='';pics.forEach((x,i)=>{const im=el('img');im.src='data:'+x.mimeType+';base64,'+x.data;
 im.alt='attached image '+(i+1)+' (click to remove)';im.title='Click to remove';im.onclick=()=>{pics.splice(i,1);showPics()};p.appendChild(im)});p.classList.toggle('on',pics.length>0)}
function addFiles(files){[...files].filter(f=>/^image\/(png|jpeg|gif|webp)$/.test(f.type)).slice(0,6-pics.length).forEach(f=>{
 if(f.size>8e6){handle({type:'notice',level:'error',message:f.name+' is larger than 8 MB'});return}
 const r=new FileReader();r.onload=()=>{pics.push({mimeType:f.type,data:String(r.result).split(',')[1]});showPics()};r.readAsDataURL(f)})}
box.addEventListener('paste',e=>{const f=e.clipboardData&&e.clipboardData.files;if(f&&f.length){e.preventDefault();addFiles(f)}});
main.addEventListener('dragover',e=>{e.preventDefault();main.classList.add('drop')});main.addEventListener('dragleave',()=>main.classList.remove('drop'));
main.addEventListener('drop',e=>{e.preventDefault();main.classList.remove('drop');addFiles(e.dataTransfer.files)});
function send(){const t=box.value.trim();if(!t&&!pics.length)return;const body={text:t};if(pics.length)body.images=pics;
 api('/api/prompt',body).then(r=>{if(!r.ok){handle({type:'notice',level:'error',message:r.error||'could not send'});return}
  box.value='';box.style.height='auto';pics=[];showPics()});$('hints').style.display='none'}
function chooser(title,rows,pick,filterText){const d=$('dlg');d.textContent='';d.appendChild(el('h3','',title));
 const f=el('input','filter');f.placeholder=filterText;f.setAttribute('aria-label',filterText);const l=el('div','list');
 const draw=()=>{l.textContent='';const q=f.value.toLowerCase();rows.filter(r=>(r.label+' '+r.sub).toLowerCase().includes(q)).slice(0,200).forEach(r=>{
  const b=el('button',r.cur?'cur':'');b.type='button';b.appendChild(el('span','',r.label));if(r.sub)b.appendChild(el('small','',r.sub));
  b.onclick=()=>{d.close();pick(r)};l.appendChild(b)})};f.oninput=draw;d.appendChild(f);d.appendChild(l);
 const row=el('div','row'),c=el('button','','Close');c.type='button';c.onclick=()=>d.close();row.appendChild(c);d.appendChild(row);
 d.oncancel=null;draw();d.showModal();f.focus()}
$('pickModel').onclick=()=>api('/api/models').then(r=>chooser('Model',r.models.map(m=>({key:m.key,label:m.name,cur:m.key===r.current,
 sub:m.key+(m.images?' · images':'')+(m.reasoning?' · reasoning':'')})),m=>api('/api/model',{key:m.key}).then(x=>{if(!x.ok)handle({type:'notice',level:'error',message:x.error})}),'Filter models'));
$('pickSession').onclick=()=>api('/api/sessions').then(r=>chooser('Sessions',[{new:1,label:'+ New session',sub:''}].concat(r.sessions.map(x=>({path:x.path,cur:x.current,
 label:x.name||x.first||'(empty)',sub:new Date(x.mtime*1000).toLocaleString()+' · '+x.messages+' messages'}))),
 x=>api('/api/session',x.new?{action:'new'}:{action:'resume',path:x.path}).then(y=>{if(!y.ok)handle({type:'notice',level:'error',message:y.error||'could not switch'})}),'Filter sessions'));
$('form').onsubmit=e=>{e.preventDefault();send()};$('stop').onclick=()=>api('/api/abort',{});
box.oninput=()=>{box.style.height='auto';box.style.height=Math.min(box.scrollHeight,220)+'px';hintIndex=0;hints()};
box.onkeydown=e=>{const m=$('hints').style.display==='block'?hints():[];
 if(m.length&&(e.key==='ArrowDown'||e.key==='ArrowUp')){e.preventDefault();hintIndex=(hintIndex+(e.key==='ArrowDown'?1:-1)+m.length)%m.length;hints();return}
 if(m.length&&e.key==='Tab'){e.preventDefault();box.value='/'+m[hintIndex].name+' ';$('hints').style.display='none';return}
 if(e.key==='Escape'){if(m.length)$('hints').style.display='none';else if(busy)api('/api/abort',{});return}
 if(e.key==='Enter'&&!e.shiftKey&&!e.isComposing){e.preventDefault();send()}};
function load(){return api('/api/state').then(s=>{commands=s.commands||[];showState(s);(s.history||[]).forEach(h=>handle(Object.assign({history:1},h)));setBusy(!!s.busy);main.scrollTop=main.scrollHeight})}
function listen(){const es=new EventSource('/api/events?k='+encodeURIComponent(key));es.onmessage=m=>{try{handle(JSON.parse(m.data))}catch(e){}};
 es.onerror=()=>{es.close();setTimeout(listen,1500)}}
load().then(listen);box.focus();
</script></body></html>
"""
