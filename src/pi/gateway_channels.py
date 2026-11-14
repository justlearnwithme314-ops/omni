"""More gateway channels: Slack and Discord (by polling, no websocket library needed), and channel plugins.

    "channels": {
      "slack":   {"type": "slack",   "token": "$SLACK_BOT_TOKEN",   "channels": ["C0123456789"], "interval": 3},
      "discord": {"type": "discord", "token": "$DISCORD_BOT_TOKEN", "channels": ["1234567890"],  "interval": 3}
    }

Both read new messages in the listed channel ids every `interval` seconds through the services' HTTP APIs and
answer in the same channel. Only messages written after the gateway started are answered, and the bot's own
messages are skipped. The Slack bot needs `channels:history` (or `groups:history` / `im:history`) and `chat:write`;
the Discord bot needs the Message Content intent and permission to read and send in the channel. With
`"mention_only": true` the bot answers only messages that mention it. Polling costs one request per channel per
interval; for busy workspaces raise the interval. Not tried against the live services.

Channel plugins: every `<agent-dir>/channels/*.py` is imported when the gateway starts. A plugin either calls
`register_channel("name", factory)` (passed into its namespace) or defines `CHANNELS = {"name": factory}`, where
`factory(cfg)` returns an object with `poll(timeout) -> [Inbound]`, `send(chat, text)` and optionally `typing(chat)`
and `close()`. `Channel` and `Inbound` are in its namespace too. This is the slot for what Hermes calls platform
plugins and OpenClaw calls channel plugins; their own adapter classes are not loaded as they are.
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional

MAX_SLACK, MAX_DISCORD = 3500, 1900


def _chunks(text: str, size: int) -> List[str]:
    return [text[i:i + size] for i in range(0, len(text), size)] or ["(empty)"]


def _make(gw: Any) -> Dict[str, Any]:
    Channel, Inbound, resolve_secret = gw.Channel, gw.Inbound, gw.resolve_secret

    class PollingChannel(Channel):
        def __init__(self, cfg: Dict[str, Any], default_base: str):
            self.token = resolve_secret(cfg.get("token"))
            if not self.token:
                raise ValueError(f"{self.name}: token is missing")
            self.ids = [str(c) for c in cfg.get("channels") or []]
            if not self.ids:
                raise ValueError(f"{self.name}: list the channel ids to read in \"channels\"")
            self.base = str(cfg.get("api_base") or default_base).rstrip("/")
            self.interval = max(0.05, float(cfg.get("interval") or 3))
            self.mention_only = bool(cfg.get("mention_only"))
            self.cursor: Dict[str, str] = {}
            self.me: Optional[str] = None
            self.error = ""

        def _request(self, method: str, url: str, body: Optional[Dict[str, Any]] = None) -> Any:
            req = urllib.request.Request(url, data=json.dumps(body).encode() if body is not None else None, method=method,
                                         headers={"Authorization": self._auth(), "Content-Type": "application/json; charset=utf-8",
                                                  "User-Agent": "omni-gateway (https://github.com, 1.0)"})
            with urllib.request.urlopen(req, timeout=20) as r:
                raw = r.read().decode("utf-8")
            return json.loads(raw) if raw.strip() else None

        def poll(self, timeout: float) -> List[Any]:
            time.sleep(min(self.interval, max(0.05, timeout)))
            out: List[Any] = []
            for cid in self.ids:
                try:
                    out += self._read(cid)
                    self.error = ""
                except (urllib.error.URLError, OSError, RuntimeError, ValueError, KeyError) as e:
                    self.error = f"{cid}: {getattr(e, 'reason', e)}"
            return out

    class SlackChannel(PollingChannel):
        name = "slack"

        def __init__(self, cfg: Dict[str, Any]):
            super().__init__(cfg, "https://slack.com/api")
            self.started = f"{time.time():.6f}"

        def _auth(self) -> str:
            return f"Bearer {self.token}"

        def _api(self, method: str, http: str = "GET", body: Optional[Dict[str, Any]] = None, **query: Any) -> Any:
            url = f"{self.base}/{method}" + (f"?{urllib.parse.urlencode(query)}" if query else "")
            data = self._request(http, url, body)
            if not data or not data.get("ok"):
                raise RuntimeError(f"slack {method}: {(data or {}).get('error')}")
            return data

        def _read(self, cid: str) -> List[Any]:
            if self.me is None:
                self.me = str(self._api("auth.test").get("user_id") or "")
            data = self._api("conversations.history", channel=cid, oldest=self.cursor.get(cid, self.started), limit=50)
            out = []
            for m in sorted(data.get("messages") or [], key=lambda m: float(m.get("ts") or 0)):
                ts = str(m.get("ts") or "")
                if ts and float(ts) > float(self.cursor.get(cid, self.started)):
                    self.cursor[cid] = ts
                text = str(m.get("text") or "")
                if m.get("subtype") or m.get("bot_id") or not text or m.get("user") == self.me:
                    continue
                tag = f"<@{self.me}>"
                if self.mention_only and tag not in text:
                    continue
                out.append(Inbound("slack", cid, str(m.get("user") or ""), text.replace(tag, "").strip(), name=str(m.get("user") or "")))
            return out

        def send(self, chat: str, text: str) -> None:
            for part in _chunks(text, MAX_SLACK):
                self._api("chat.postMessage", "POST", {"channel": chat, "text": part})

    class DiscordChannel(PollingChannel):
        name = "discord"

        def __init__(self, cfg: Dict[str, Any]):
            super().__init__(cfg, "https://discord.com/api/v10")

        def _auth(self) -> str:
            return f"Bot {self.token}"

        def _read(self, cid: str) -> List[Any]:
            if self.me is None:
                self.me = str((self._request("GET", f"{self.base}/users/@me") or {}).get("id") or "")
            if cid not in self.cursor:      # first look: remember the newest message, answer nothing older
                latest = self._request("GET", f"{self.base}/channels/{cid}/messages?limit=1") or []
                self.cursor[cid] = str(latest[0]["id"]) if latest else "0"
                return []
            rows = self._request("GET", f"{self.base}/channels/{cid}/messages?limit=50&after={self.cursor[cid]}") or []
            out = []
            for m in sorted(rows, key=lambda m: int(m["id"])):
                self.cursor[cid] = str(max(int(self.cursor[cid]), int(m["id"])))
                author = m.get("author") or {}
                text = str(m.get("content") or "")
                if author.get("bot") or str(author.get("id")) == self.me or not text:
                    continue
                mentioned = any(str(u.get("id")) == self.me for u in m.get("mentions") or [])
                if self.mention_only and not mentioned:
                    continue
                for tag in (f"<@{self.me}>", f"<@!{self.me}>"):
                    text = text.replace(tag, "")
                out.append(Inbound("discord", cid, str(author.get("id") or ""), text.strip(), name=str(author.get("username") or "")))
            return out

        def send(self, chat: str, text: str) -> None:
            for part in _chunks(text, MAX_DISCORD):
                self._request("POST", f"{self.base}/channels/{chat}/messages", {"content": part})

        def typing(self, chat: str) -> None:
            try:
                self._request("POST", f"{self.base}/channels/{chat}/typing", {})
            except Exception:
                pass

    return {"slack": SlackChannel, "discord": DiscordChannel}


def install(gw: Any) -> None:
    """Called by gateway.py once its base classes exist."""
    for name, factory in _make(gw).items():
        gw.CHANNEL_TYPES.setdefault(name, factory)


def load_plugins(gw: Any, agent_dir: Any) -> List[str]:
    """Import <agent-dir>/channels/*.py. Returns problems (one line each)."""
    problems: List[str] = []
    folder = Path(agent_dir) / "channels"
    if not folder.is_dir():
        return problems
    for path in sorted(folder.glob("*.py")):
        if path.name.startswith("_"):
            continue
        ns: Dict[str, Any] = {"__name__": f"omni_channel_{path.stem}", "__file__": str(path), "register_channel": gw.register_channel,
                              "Channel": gw.Channel, "Inbound": gw.Inbound}
        try:
            exec(compile(path.read_text(encoding="utf-8"), str(path), "exec"), ns)
            table = ns.get("CHANNELS")
            if isinstance(table, dict):
                for name, factory in table.items():
                    gw.register_channel(str(name), factory)
        except Exception as e:
            problems.append(f"channel plugin {path.name}: {type(e).__name__}: {e}")
    return problems
