"""MCP client following Pi's docs/mcp.md: `mcp.json` (user + trusted project), stdio and streamable HTTP, tools named
`mcp__<server>__<tool>`, exposure (codemode | deferred | direct | hidden) with per-tool `toolExposure`, resource tools,
20 KB middle-truncation, logging to `<agent-dir>/mcp.log`, extension-registered servers, and OAuth (DCR + PKCE).
"""
from __future__ import annotations

import fnmatch
import hashlib
import json
import os
import queue
import re
import shutil
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from .ai.models import resolve_config_value
from .tools.base import ToolDef, ToolError

PROTOCOL_VERSION = "2025-06-18"
CLIENT_INFO = {"name": "pi", "version": "0.78.1"}
EXPOSURES = ("codemode", "deferred", "direct", "hidden")
TEXT_LIMIT = 20 * 1024
NAME_RE = re.compile(r"^[A-Za-z0-9_-]+$")


def sanitize(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9_]", "_", s)


def server_key(name: str) -> str:
    return name.replace("-", "_").lower()


# ================================================================================================ config
def _expand(v: Any) -> Any:
    if isinstance(v, str):
        return resolve_config_value(v) or ""
    return v


def load_config(agent_dir: Path, cwd: str, trusted: bool) -> Tuple[Dict[str, Dict[str, Any]], List[str], Dict[str, Any]]:
    """Returns (servers by name, errors, top-level options such as autoEnableCodemode)."""
    errors: List[str] = []
    servers: Dict[str, Dict[str, Any]] = {}
    top: Dict[str, Any] = {}
    files = [(Path(agent_dir) / "mcp.json", "user")]
    if trusted:
        files.append((Path(cwd) / ".pi" / "mcp.json", "project"))
    for path, scope in files:
        if not path.is_file():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception as e:
            errors.append(f"{path}: {e}")
            continue
        for k in ("autoEnableCodemode",):
            if k in data:
                top[k] = data[k]
        for name, cfg in (data.get("mcpServers") or {}).items():
            if not isinstance(cfg, dict):
                errors.append(f"{path}: server '{name}' must be an object")
                continue
            if scope == "project" and name in servers and not any(k in cfg for k in ("command", "url", "type")):
                servers[name] = {**servers[name], **{k: cfg[k] for k in ("enabled", "exposure", "toolExposure") if k in cfg},
                                 "_source": f"{servers[name]['_source']} + {path}"}
                continue
            servers[name] = {**cfg, "_source": str(path), "_scope": scope}
    valid: Dict[str, Dict[str, Any]] = {}
    seen_keys: Dict[str, str] = {}
    for name, cfg in servers.items():
        err = validate(name, cfg)
        if err:
            errors.append(f"mcp server '{name}': {err}")
            continue
        k = server_key(name)
        if k in seen_keys:
            errors.append(f"mcp server '{name}' collides with '{seen_keys[k]}' (names differing only in - and _ are the same)")
            continue
        seen_keys[k] = name
        valid[name] = cfg
    return valid, errors, top


def validate(name: str, cfg: Dict[str, Any]) -> Optional[str]:
    if not NAME_RE.match(name):
        return "names may contain only letters, digits, _ and -"
    t = cfg.get("type")
    if t == "sse":
        return "the SSE transport is not supported; use streamable HTTP (often /mcp instead of /sse)"
    if t and t not in ("stdio", "http", "streamable-http"):
        return f"unknown type '{t}'"
    if not cfg.get("command") and not cfg.get("url"):
        return "needs `command` (stdio) or `url` (streamable HTTP)"
    exp = cfg.get("exposure", "codemode")
    if exp == "codemode-deferred":
        cfg["exposure"] = "codemode"
    elif exp not in EXPOSURES:
        return f"unknown exposure '{exp}'"
    return None


def save_server(agent_dir: Path, cwd: str, name: str, cfg: Optional[Dict[str, Any]], local: bool = False) -> Path:
    path = (Path(cwd) / ".pi" / "mcp.json") if local else (Path(agent_dir) / "mcp.json")
    try:
        data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    except Exception:
        data = {}
    servers = data.setdefault("mcpServers", {})
    if cfg is None:
        servers.pop(name, None)
    else:
        servers[name] = cfg
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    return path


def update_server_fields(path: str, name: str, fields: Dict[str, Any]) -> None:
    """Change `enabled` / `exposure` in the file that defines the server, keeping unrelated content."""
    p = Path(path)
    data = json.loads(p.read_text(encoding="utf-8"))
    data.setdefault("mcpServers", {}).setdefault(name, {}).update(fields)
    p.write_text(json.dumps(data, indent=2), encoding="utf-8")


# ============================================================================================ transports
class McpError(Exception):
    pass


class StdioTransport:
    def __init__(self, cfg: Dict[str, Any], cwd: str, on_message: Callable[[Dict[str, Any]], None]):
        cmd = os.path.expanduser(str(cfg["command"]))
        args = [os.path.expanduser(str(a)) for a in cfg.get("args") or []]
        env = dict(os.environ)
        env.update({k: str(_expand(v)) for k, v in (cfg.get("env") or {}).items()})
        wd = cfg.get("cwd")
        wd = os.path.expanduser(str(wd)) if wd else cwd
        if wd and not os.path.isabs(wd):
            wd = os.path.join(cwd, wd)
        exe = shutil.which(cmd, path=env.get("PATH")) or cmd
        kwargs: Dict[str, Any] = {}
        if os.name == "nt":
            kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) | getattr(subprocess, "CREATE_NO_WINDOW", 0)
            if exe.lower().endswith((".cmd", ".bat")):  # npx.cmd, uvx.cmd
                args = ["/d", "/s", "/c", exe] + args
                exe = os.environ.get("COMSPEC", "cmd.exe")
        else:
            kwargs["start_new_session"] = True
        self.proc = subprocess.Popen([exe] + args, cwd=wd, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=subprocess.PIPE, **kwargs)
        self.on_message = on_message
        self.stderr_tail: List[str] = []
        self.closed = False
        threading.Thread(target=self._read, daemon=True).start()
        threading.Thread(target=self._read_err, daemon=True).start()
        self._lock = threading.Lock()

    def _read(self) -> None:
        assert self.proc.stdout is not None
        for raw in self.proc.stdout:
            line = raw.decode("utf-8", "replace").strip()
            if not line:
                continue
            try:
                self.on_message(json.loads(line))
            except json.JSONDecodeError:
                continue
        self.closed = True
        self.on_message({"__closed": True})

    def _read_err(self) -> None:
        assert self.proc.stderr is not None
        for raw in self.proc.stderr:
            self.stderr_tail.append(raw.decode("utf-8", "replace").rstrip())
            self.stderr_tail = self.stderr_tail[-40:]

    def send(self, msg: Dict[str, Any]) -> None:
        data = (json.dumps(msg) + "\n").encode("utf-8")
        with self._lock:
            if self.proc.poll() is not None:
                raise McpError("server process exited" + (": " + self.stderr_tail[-1] if self.stderr_tail else ""))
            self.proc.stdin.write(data)  # type: ignore[union-attr]
            self.proc.stdin.flush()  # type: ignore[union-attr]

    def close(self) -> None:
        self.closed = True
        try:
            self.proc.stdin.close()  # type: ignore[union-attr]
        except Exception:
            pass
        try:
            self.proc.terminate()
            self.proc.wait(timeout=2)
        except Exception:
            try:
                if os.name == "nt":
                    subprocess.run(["taskkill", "/T", "/F", "/PID", str(self.proc.pid)], capture_output=True)
                else:
                    import signal as _s
                    os.killpg(os.getpgid(self.proc.pid), _s.SIGKILL)
            except Exception:
                pass


class HttpTransport:
    """Streamable HTTP: JSON-RPC POSTs answered with JSON or an SSE stream; `Mcp-Session-Id` kept per server."""

    def __init__(self, cfg: Dict[str, Any], on_message: Callable[[Dict[str, Any]], None], token_getter=None):
        self.url = cfg["url"]
        self.headers = {k: str(_expand(v)) for k, v in (cfg.get("headers") or {}).items()}
        self.on_message = on_message
        self.session_id: Optional[str] = None
        self.protocol_version: Optional[str] = None
        self.token_getter = token_getter
        self.stderr_tail: List[str] = []
        self.closed = False

    def send(self, msg: Dict[str, Any], timeout: float = 60) -> None:
        threading.Thread(target=self._post, args=(msg, timeout), daemon=True).start()

    def _post(self, msg: Dict[str, Any], timeout: float, attempt: int = 0) -> None:
        headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream", **self.headers}
        if self.session_id:
            headers["Mcp-Session-Id"] = self.session_id
        if self.protocol_version:
            headers["MCP-Protocol-Version"] = self.protocol_version
        if self.token_getter and "Authorization" not in headers:
            tok = self.token_getter()
            if tok:
                headers["Authorization"] = f"Bearer {tok}"
        req = urllib.request.Request(self.url, data=json.dumps(msg).encode(), headers=headers, method="POST")
        try:
            resp = urllib.request.urlopen(req, timeout=timeout)
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "replace")[:500]
            if e.code in (408, 429, 500, 502, 503, 504) and attempt < 2 and "id" in msg:
                time.sleep(0.5 * (attempt + 1))
                return self._post(msg, timeout, attempt + 1)
            if "id" in msg:
                self.on_message({"jsonrpc": "2.0", "id": msg["id"], "error": {"code": e.code, "message": f"HTTP {e.code}: {body}",
                                                                              "www": e.headers.get("WWW-Authenticate", "")}})
            return
        except Exception as e:
            if attempt < 2 and "id" in msg:
                time.sleep(0.5 * (attempt + 1))
                return self._post(msg, timeout, attempt + 1)
            if "id" in msg:
                self.on_message({"jsonrpc": "2.0", "id": msg["id"], "error": {"code": -32000, "message": str(e)}})
            return
        sid = resp.headers.get("Mcp-Session-Id")
        if sid:
            self.session_id = sid
        ctype = resp.headers.get("Content-Type", "")
        if resp.status == 202 or "id" not in msg:
            resp.close()
            return
        if "text/event-stream" in ctype:
            data: List[str] = []
            for raw in resp:
                line = raw.decode("utf-8", "replace").rstrip("\r\n")
                if line.startswith("data:"):
                    data.append(line[5:].lstrip())
                elif line == "" and data:
                    try:
                        payload = json.loads("\n".join(data))
                    except json.JSONDecodeError:
                        payload = None
                    data = []
                    if payload is not None:
                        for m in payload if isinstance(payload, list) else [payload]:
                            self.on_message(m)
                        if any(isinstance(m, dict) and m.get("id") == msg["id"] and ("result" in m or "error" in m)
                               for m in (payload if isinstance(payload, list) else [payload])):
                            break
            resp.close()
        else:
            try:
                payload = json.loads(resp.read().decode("utf-8") or "null")
            finally:
                resp.close()
            for m in payload if isinstance(payload, list) else [payload] if payload else []:
                self.on_message(m)

    def close(self) -> None:
        self.closed = True
        if self.session_id:
            try:
                req = urllib.request.Request(self.url, method="DELETE", headers={"Mcp-Session-Id": self.session_id, **self.headers})
                urllib.request.urlopen(req, timeout=5).close()
            except Exception:
                pass


# =============================================================================================== client
class McpServer:
    def __init__(self, name: str, cfg: Dict[str, Any], manager: "McpManager"):
        self.name, self.cfg, self.mgr = name, cfg, manager
        self.state = "disabled" if cfg.get("enabled") is False else "pending"
        self.error: Optional[str] = None
        self.tools: List[Dict[str, Any]] = []
        self.instructions: str = ""
        self.capabilities: Dict[str, Any] = {}
        self.transport: Any = None
        self._ids = 0
        self._pending: Dict[int, Dict[str, Any]] = {}
        self._lock = threading.Lock()
        self.connected = threading.Event()
        self.needs_auth = False

    @property
    def exposure(self) -> str:
        return self.cfg.get("exposure", "codemode")

    @property
    def description(self) -> str:
        if self.cfg.get("description"):
            return str(self.cfg["description"])
        return (self.instructions or "").strip().split("\n")[0][:200]

    def tool_exposure(self, tool: str) -> str:
        te = self.cfg.get("toolExposure") or {}
        if tool in te:
            return te[tool]
        for pat, exp in te.items():
            if "*" in pat and fnmatch.fnmatchcase(tool, pat):
                return exp
        return self.exposure

    # ----------------------------------------------------------------------------------- protocol
    def _on_message(self, m: Dict[str, Any]) -> None:
        if m.get("__closed"):
            if self.state == "connected":
                self.state = "disconnected"
            for slot in list(self._pending.values()):
                slot["error"] = {"message": "connection closed"}
                slot["ev"].set()
            self.mgr.changed()
            return
        if "id" in m and ("result" in m or "error" in m):
            slot = self._pending.pop(m["id"], None)
            if slot:
                slot["result"], slot["error"] = m.get("result"), m.get("error")
                slot["ev"].set()
            return
        method = m.get("method")
        if method == "notifications/tools/list_changed":
            threading.Thread(target=self.refresh_tools, daemon=True).start()
        elif method == "notifications/message":
            p = m.get("params") or {}
            self.mgr.log(self.name, p.get("level", "info"), p.get("logger", ""), json.dumps(p.get("data")) if not isinstance(p.get("data"), str) else p["data"])
        elif method == "ping" and "id" in m:
            self._raw_send({"jsonrpc": "2.0", "id": m["id"], "result": {}})
        elif "id" in m and method:  # server->client requests we do not support (sampling, elicitation, roots)
            if method == "roots/list":
                self._raw_send({"jsonrpc": "2.0", "id": m["id"], "result": {"roots": [{"uri": Path(self.mgr.cwd).as_uri(), "name": "cwd"}]}})
            else:
                self._raw_send({"jsonrpc": "2.0", "id": m["id"], "error": {"code": -32601, "message": f"{method} not supported"}})

    def _raw_send(self, msg: Dict[str, Any]) -> None:
        try:
            self.transport.send(msg)
        except Exception:
            pass

    def request(self, method: str, params: Optional[Dict[str, Any]] = None, timeout: Optional[float] = None) -> Any:
        if self.transport is None or getattr(self.transport, "closed", False):
            self.connect()
        with self._lock:
            self._ids += 1
            rid = self._ids
        slot = {"ev": threading.Event(), "result": None, "error": None}
        self._pending[rid] = slot
        msg = {"jsonrpc": "2.0", "id": rid, "method": method}
        if params is not None:
            msg["params"] = params
        t = float(timeout or self.cfg.get("timeout") or 60)
        if isinstance(self.transport, HttpTransport):
            self.transport.send(msg, timeout=t)
        else:
            self.transport.send(msg)
        if not slot["ev"].wait(t):
            self._pending.pop(rid, None)
            raise McpError(f"{self.name}: {method} timed out after {t:.0f}s")
        if slot["error"]:
            err = slot["error"]
            if isinstance(err, dict) and (err.get("code") == 401 or "401" in str(err.get("message", ""))):
                self.needs_auth = True
            raise McpError(f"{self.name}: {err.get('message') if isinstance(err, dict) else err}")
        return slot["result"]

    def notify(self, method: str, params: Optional[Dict[str, Any]] = None) -> None:
        msg = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            msg["params"] = params
        self._raw_send(msg)

    # ---------------------------------------------------------------------------------- lifecycle
    def connect(self) -> None:
        if self.cfg.get("enabled") is False:
            self.state = "disabled"
            return
        self.state = "connecting"
        self.error = None
        try:
            if self.cfg.get("command"):
                self.transport = StdioTransport(self.cfg, self.mgr.cwd, self._on_message)
            else:
                from .mcp_oauth import token_for
                self.transport = HttpTransport(self.cfg, self._on_message,
                                               token_getter=lambda: token_for(self.mgr.agent_dir, self.name, self.cfg))
            res = self.request("initialize", {"protocolVersion": PROTOCOL_VERSION, "capabilities": {"roots": {"listChanged": False}},
                                              "clientInfo": CLIENT_INFO})
            if isinstance(self.transport, HttpTransport):
                self.transport.protocol_version = (res or {}).get("protocolVersion", PROTOCOL_VERSION)
            self.capabilities = (res or {}).get("capabilities") or {}
            self.instructions = (res or {}).get("instructions") or ""
            self.notify("notifications/initialized")
            self.refresh_tools()
            self.state = "connected"
            self.needs_auth = False
        except Exception as e:
            self.state = "needs-auth" if self.needs_auth else "failed"
            self.error = str(e)
            tail = getattr(self.transport, "stderr_tail", [])
            if tail:
                self.error += "\n" + "\n".join(tail[-8:])
            try:
                if self.transport:
                    self.transport.close()
            except Exception:
                pass
            self.transport = None
        finally:
            self.connected.set()
            self.mgr.changed()

    def refresh_tools(self) -> None:
        tools: List[Dict[str, Any]] = []
        cursor = None
        for _ in range(50):
            res = self.request("tools/list", {"cursor": cursor} if cursor else {}) or {}
            tools.extend(res.get("tools") or [])
            cursor = res.get("nextCursor")
            if not cursor:
                break
        self.tools = tools
        self.mgr.changed()

    def close(self) -> None:
        if self.transport is not None:
            try:
                self.transport.close()
            except Exception:
                pass
        self.transport = None
        if self.state == "connected":
            self.state = "disconnected"


class McpManager:
    """All MCP servers of a session; exposes core ToolDefs and the system-prompt section."""

    def __init__(self, agent_dir: Path, cwd: str, trusted: bool, on_change: Optional[Callable[[], None]] = None):
        self.agent_dir, self.cwd, self.trusted = Path(agent_dir), cwd, trusted
        self.servers: Dict[str, McpServer] = {}
        self.registered: Dict[str, Dict[str, Any]] = {}  # from extensions
        self.errors: List[str] = []
        self.options: Dict[str, Any] = {}
        self.on_change = on_change
        self._log_lock = threading.Lock()

    # ---------------------------------------------------------------------------------- config
    def load(self) -> None:
        cfgs, errors, top = load_config(self.agent_dir, self.cwd, self.trusted)
        self.errors, self.options = errors, top
        for name, cfg in self.registered.items():
            if any(server_key(n) == server_key(name) for n in cfgs):
                self.errors.append(f"mcp server '{name}' registered by an extension is overridden by mcp.json")
                continue
            cfgs[name] = cfg
        for name in list(self.servers):
            if name not in cfgs or self.servers[name].cfg != cfgs[name]:
                self.servers.pop(name).close()
        for name, cfg in cfgs.items():
            if name not in self.servers:
                self.servers[name] = McpServer(name, cfg, self)

    def start(self) -> None:
        """Connect every enabled server in the background."""
        for s in self.servers.values():
            if s.state in ("pending", "disconnected", "failed") and s.cfg.get("enabled") is not False:
                s.connected.clear()
                threading.Thread(target=s.connect, name=f"mcp-{s.name}", daemon=True).start()

    def wait_direct(self, timeout: float = 10.0) -> None:
        end = time.time() + timeout
        for s in self.servers.values():
            if s.cfg.get("enabled") is False:
                continue
            if s.exposure == "direct" or any(v == "direct" for v in (s.cfg.get("toolExposure") or {}).values()):
                s.connected.wait(max(0.0, end - time.time()))

    def wait_all(self, timeout: float = 30.0) -> None:
        end = time.time() + timeout
        for s in self.servers.values():
            if s.cfg.get("enabled") is not False:
                s.connected.wait(max(0.0, end - time.time()))

    def register(self, name: str, cfg: Dict[str, Any]) -> None:
        err = validate(name, dict(cfg))
        if err:
            raise ValueError(err)
        self.registered[name] = {**cfg, "_source": "extension", "_scope": "extension"}
        self.load()
        self.start()

    def unregister(self, name: str) -> None:
        self.registered.pop(name, None)
        if name in self.servers and self.servers[name].cfg.get("_scope") == "extension":
            self.servers.pop(name).close()
        self.changed()

    def close(self) -> None:
        for s in self.servers.values():
            s.close()

    def changed(self) -> None:
        if self.on_change:
            try:
                self.on_change()
            except Exception:
                pass

    def log(self, server: str, level: str, logger: str, message: str) -> None:
        path = self.agent_dir / "mcp.log"
        with self._log_lock:
            try:
                if path.exists() and path.stat().st_size > 5 * 1024 * 1024:
                    os.replace(path, path.with_suffix(".log.1"))
                with open(path, "a", encoding="utf-8") as f:
                    f.write(f"{time.strftime('%Y-%m-%dT%H:%M:%S')} [{server}] {level} {logger}: {message}\n")
            except OSError:
                pass

    # ---------------------------------------------------------------------------------- tools
    def tool_defs(self) -> Dict[str, Tuple[ToolDef, str, Dict[str, Any]]]:
        """name -> (ToolDef, exposure, namespace info). Hidden tools are omitted."""
        out: Dict[str, Tuple[ToolDef, str, Dict[str, Any]]] = {}
        for s in self.servers.values():
            if s.state != "connected" or s.exposure == "hidden" and not s.cfg.get("toolExposure"):
                continue
            names: Dict[str, List[Dict[str, Any]]] = {}
            for t in s.tools:
                names.setdefault(f"mcp__{sanitize(s.name)}__{sanitize(t['name'])}", []).append(t)
            ns = {"name": f"mcp__{sanitize(s.name)}", "description": s.description, "instructions": s.instructions}
            for full, group in names.items():
                for t in group:
                    exp = s.tool_exposure(t["name"])
                    if exp == "hidden":
                        continue
                    name = full if len(group) == 1 else f"{full}_{hashlib.sha1(t['name'].encode()).hexdigest()[:6]}"
                    out[name] = (self._wrap(s, t, name), exp, ns)
        res = self._resource_tools()
        out.update(res)
        return out

    def _wrap(self, s: McpServer, t: Dict[str, Any], name: str) -> ToolDef:
        def execute(tool_call_id, params, signal=None, on_update=None, ctx=None):
            raw = self.call(s.name, t["name"], params or {}, signal)
            return self.result_for_model(raw, name)

        ann = t.get("annotations") or {}
        td = ToolDef(name=name, label=f"{s.name}/{t['name']}", description=(t.get("description") or t.get("title") or t["name"]).strip(),
                     parameters=t.get("inputSchema") or {"type": "object", "properties": {}}, execute=execute,
                     source=f"mcp:{s.name}", read_only=bool(ann.get("readOnlyHint")))
        td.__dict__["mcp"] = {"server": s.name, "tool": t["name"], "annotations": ann}
        td.__dict__["raw_execute"] = lambda params, signal=None: self.call(s.name, t["name"], params or {}, signal)
        return td

    def call(self, server: str, tool: str, args: Dict[str, Any], signal=None) -> Dict[str, Any]:
        s = self.servers.get(server)
        if s is None:
            raise ToolError(f"MCP server {server} is not configured")
        if s.state != "connected":
            s.connected.wait(10)
            if s.state != "connected":
                s.connect()
            if s.state != "connected":
                raise ToolError(f"MCP server {server} is {s.state}: {s.error or ''}".strip())
        try:
            return s.request("tools/call", {"name": tool, "arguments": args}) or {}
        except McpError as e:
            raise ToolError(str(e))

    def result_for_model(self, raw: Dict[str, Any], name: str) -> Dict[str, Any]:
        content: List[Dict[str, Any]] = []
        for b in raw.get("content") or []:
            t = b.get("type")
            if t == "text":
                content.append({"type": "text", "text": self._limit(b.get("text", ""), name)})
            elif t == "image":
                content.append({"type": "image", "data": b.get("data", ""), "mimeType": b.get("mimeType", "image/png")})
            elif t == "resource":
                r = b.get("resource") or {}
                content.append({"type": "text", "text": self._limit(r.get("text") or f"[resource {r.get('uri')}]", name)})
            elif t == "resource_link":
                content.append({"type": "text", "text": f"[resource {b.get('uri')} on this server; read it with read_mcp_resource]"})
            elif t == "audio":
                content.append({"type": "text", "text": "[audio content omitted]"})
        if not content and raw.get("structuredContent") is not None:
            content.append({"type": "text", "text": self._limit(json.dumps(raw["structuredContent"], ensure_ascii=False), name)})
        r = {"content": content or [{"type": "text", "text": ""}], "details": {"structuredContent": raw.get("structuredContent")}}
        if raw.get("isError"):
            r["isError"] = True
        return r

    @staticmethod
    def _limit(text: str, name: str) -> str:
        if len(text) <= TEXT_LIMIT:
            return text
        path = os.path.join(tempfile.gettempdir(), f"pi-mcp-{sanitize(name)}-{int(time.time() * 1000)}.txt")
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
        half = TEXT_LIMIT // 2
        return f"{text[:half]}\n…{len(text) - TEXT_LIMIT} chars truncated…\n{text[-half:]}\n[Full output: {path}]"

    # ------------------------------------------------------------------------------ resources
    def _resource_servers(self) -> List[McpServer]:
        return [s for s in self.servers.values() if s.state == "connected" and "resources" in s.capabilities and s.exposure != "hidden"]

    def _resource_tools(self) -> Dict[str, Tuple[ToolDef, str, Dict[str, Any]]]:
        servers = self._resource_servers()
        if not servers:
            return {}
        rank = {"direct": 3, "codemode": 2, "deferred": 1}
        exp = max((s.exposure for s in servers), key=lambda e: rank.get(e, 0))
        ns = {"name": "mcp_resources", "description": "Resources offered by MCP servers", "instructions": ""}

        def list_res(_id, p, signal=None, on_update=None, ctx=None, templates=False):
            method, key = ("resources/templates/list", "resourceTemplates") if templates else ("resources/list", "resources")
            out: List[Dict[str, Any]] = []
            next_cursor = None
            targets = [s for s in servers if not p.get("server") or s.name == p["server"]]
            if p.get("server") and not targets:
                raise ToolError(f"Unknown MCP server with resources: {p['server']}")
            for s in targets:
                params = {"cursor": p["cursor"]} if p.get("cursor") else {}
                try:
                    res = s.request(method, params) or {}
                except McpError as e:
                    raise ToolError(str(e))
                for r in res.get(key) or []:
                    if str(r.get("uri", r.get("uriTemplate", ""))).startswith("ui://") or "mcp-app" in str(r.get("mimeType", "")):
                        continue
                    r = {k: v for k, v in r.items() if k != "icons"}
                    out.append({"server": s.name, **r})
                if p.get("server"):
                    next_cursor = res.get("nextCursor")
            body: Dict[str, Any] = {"server": p["server"]} if p.get("server") else {}
            body[key] = out
            if next_cursor:
                body["nextCursor"] = next_cursor
            return {"content": [{"type": "text", "text": json.dumps(body, ensure_ascii=False, indent=1)}], "details": None}

        def read_res(_id, p, signal=None, on_update=None, ctx=None):
            s = next((x for x in servers if x.name == p.get("server")), None)
            if not s:
                raise ToolError(f"Unknown MCP server with resources: {p.get('server')}")
            try:
                res = s.request("resources/read", {"uri": p["uri"]}) or {}
            except McpError as e:
                raise ToolError(str(e))
            content: List[Dict[str, Any]] = []
            for c in res.get("contents") or []:
                if "text" in c:
                    content.append({"type": "text", "text": self._limit(c["text"], "resource")})
                elif "blob" in c and str(c.get("mimeType", "")).startswith("image/"):
                    content.append({"type": "image", "data": c["blob"], "mimeType": c["mimeType"]})
                elif "blob" in c:
                    import base64
                    path = os.path.join(tempfile.gettempdir(), f"pi-mcp-resource-{int(time.time() * 1000)}")
                    Path(path).write_bytes(base64.b64decode(c["blob"]))
                    content.append({"type": "text", "text": f"Binary resource saved to {path} ({c.get('mimeType', 'unknown type')})"})
            return {"content": content or [{"type": "text", "text": "(empty resource)"}], "details": {"server": s.name, "uri": p["uri"]}}

        server_prop = {"type": "string", "description": "MCP server name"}
        cursor_prop = {"type": "string", "description": "Cursor from a previous page"}
        defs = [
            ToolDef("list_mcp_resources", "list_mcp_resources", "List resources offered by MCP servers.",
                    {"type": "object", "properties": {"server": server_prop, "cursor": cursor_prop}}, list_res, read_only=True),
            ToolDef("list_mcp_resource_templates", "list_mcp_resource_templates", "List resource URI templates offered by MCP servers.",
                    {"type": "object", "properties": {"server": server_prop, "cursor": cursor_prop}},
                    lambda *a, **k: list_res(*a, **k, templates=True), read_only=True),
            ToolDef("read_mcp_resource", "read_mcp_resource", "Read an MCP resource by server and URI.",
                    {"type": "object", "properties": {"server": server_prop, "uri": {"type": "string", "description": "Resource URI"}},
                     "required": ["server", "uri"]}, read_res, read_only=True),
        ]
        return {d.name: (d, exp, ns) for d in defs}

    # --------------------------------------------------------------------------------- prompt
    def prompt_section(self) -> str:
        lines = []
        for s in self.servers.values():
            if s.state != "connected" and not s.cfg.get("description"):
                continue
            exps = {s.tool_exposure(t["name"]) for t in s.tools} or {s.exposure}
            how = []
            if "codemode" in exps:
                how.append("codemode")
            if "deferred" in exps:
                how.append("tool_search")
            if not how:
                continue
            desc = s.description
            lines.append(f"- mcp__{sanitize(s.name)} (via {' or '.join(how)})" + (f": {desc}" if desc else ""))
        if not lines:
            return ""
        return "MCP servers whose tools are not declared directly:\n" + "\n".join(lines)

    def status_lines(self) -> List[str]:
        order = {"needs-auth": 0, "failed": 1, "disconnected": 2, "connecting": 3, "pending": 4, "connected": 5, "disabled": 6}
        out = []
        for s in sorted(self.servers.values(), key=lambda x: (order.get(x.state, 9), x.name)):
            out.append(f"{s.name:<20} {s.state:<12} {len(s.tools):>3} tools  exposure={s.exposure:<9} {s.cfg.get('_source', '')}")
            if s.error:
                out.extend("    " + l for l in s.error.split("\n")[:6])
        return out + [f"! {e}" for e in self.errors]
