"""Python side of the extension host: spawns `node host.mjs`, mirrors session state to it, and serves its requests
(ctx.ui.*, pi.sendMessage, setModel, ...) against the AgentSession and the active UI."""
from __future__ import annotations

import itertools
import json
import os
import shutil
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..tools.base import ToolDef, ToolError

HOST_DIR = Path(__file__).resolve().parent / "host"
MIN_NODE = (22, 13)  # module.stripTypeScriptTypes + module.register


def node_executable() -> Optional[str]:
    return os.environ.get("OMNI_NODE") or shutil.which("node")


def node_version(node: str) -> Optional[tuple]:
    try:
        out = subprocess.run([node, "--version"], capture_output=True, text=True, timeout=10).stdout.strip().lstrip("v")
        return tuple(int(x) for x in out.split(".")[:2])
    except Exception:
        return None


class HostError(Exception):
    pass


class ExtensionHost:
    available = True

    def __init__(self, session):
        self.session = session
        node = node_executable()
        if not node:
            raise HostError("Node.js not found (install Node >= 22.13 to run Pi extensions)")
        ver = node_version(node)
        if ver and ver < MIN_NODE:
            raise HostError(f"Node {'.'.join(map(str, ver))} is too old; Pi extensions need Node >= 22.13")
        log_path = Path(session.agent_dir) / "omni-extension-host.log"
        self.log_path = log_path
        self._log = open(log_path, "a", encoding="utf-8")
        self._log.write(f"\n--- host start {time.ctime()} ---\n")
        self._log.flush()
        env = dict(os.environ, NODE_NO_WARNINGS="1")
        self.proc = subprocess.Popen([node, "--no-warnings", str(HOST_DIR / "host.mjs")], cwd=session.cwd, stdin=subprocess.PIPE,
                                     stdout=subprocess.PIPE, stderr=self._log, env=env, bufsize=0)
        self._ids = itertools.count(1)
        self._pending: Dict[Any, Dict[str, Any]] = {}
        self._wlock = threading.Lock()
        self._pool = ThreadPoolExecutor(max_workers=8, thread_name_prefix="pi-ext")
        self.tools: Dict[str, ToolDef] = {}
        self.commands: Dict[str, Dict[str, Any]] = {}
        self.command_list: List[Dict[str, Any]] = []
        self.shortcuts: Dict[str, Dict[str, Any]] = {}
        self.flags: Dict[str, Any] = {}
        self.extensions: List[Dict[str, Any]] = []
        self.events: set = set()
        self.missing: set = set()
        self.tool_updates: Dict[str, Any] = {}
        self.provider_streams: Dict[str, Any] = {}
        self._logins: Dict[str, Any] = {}  # loginId -> (oauth.Interaction, finished Event) while an extension login runs
        self.mcp_servers: Dict[str, Dict[str, Any]] = {}   # registered by extensions
        self.features: Dict[str, Any] = {"terminalInput": 0, "autocomplete": False, "triggerCharacters": [],
                                         "editor": False, "markdownTransformers": 0}
        self._last_state = ""
        self._closed = False
        self._reader = threading.Thread(target=self._read_loop, name="pi-ext-reader", daemon=True)
        self._reader.start()
        session.sessions.listeners.append(self._on_session_change)

    # ------------------------------------------------------------------------------------------- protocol
    def _send(self, obj: Dict[str, Any]) -> None:
        data = (json.dumps(obj, ensure_ascii=False, default=str) + "\n").encode("utf-8")
        with self._wlock:
            if self.proc.poll() is not None:
                raise HostError("extension host exited (see " + str(self.log_path) + ")")
            self.proc.stdin.write(data)  # type: ignore[union-attr]
            self.proc.stdin.flush()  # type: ignore[union-attr]

    def call(self, method: str, params: Dict[str, Any], timeout: Optional[float] = None) -> Any:
        i = next(self._ids)
        slot = {"event": threading.Event(), "result": None, "error": None}
        self._pending[i] = slot
        self._send({"id": i, "method": method, "params": params})
        if not slot["event"].wait(timeout):
            self._pending.pop(i, None)
            raise TimeoutError(f"extension host: {method} timed out")
        if slot["error"]:
            raise HostError(slot["error"])
        return slot["result"]

    def notify(self, method: str, params: Dict[str, Any]) -> None:
        try:
            self._send({"method": method, "params": params})
        except Exception:
            pass

    def _read_loop(self) -> None:
        assert self.proc.stdout is not None
        for raw in self.proc.stdout:
            try:
                msg = json.loads(raw.decode("utf-8"))
            except Exception:
                continue
            if "method" not in msg:
                slot = self._pending.pop(msg.get("id"), None)
                if slot:
                    slot["result"], slot["error"] = msg.get("result"), msg.get("error")
                    slot["event"].set()
                continue
            self._pool.submit(self._dispatch, msg)
        self._closed = True
        for slot in list(self._pending.values()):
            slot["error"] = "extension host exited"
            slot["event"].set()

    def _dispatch(self, msg: Dict[str, Any]) -> None:
        method, params, mid = msg["method"], msg.get("params") or {}, msg.get("id")
        try:
            result = self._handle(method, params)
            if mid is not None:
                self._send({"id": mid, "result": result})
        except Exception as e:
            if mid is not None:
                try:
                    self._send({"id": mid, "error": f"{type(e).__name__}: {e}"})
                except Exception:
                    pass

    # ---------------------------------------------------------------------------------------------- state
    def state(self) -> Dict[str, Any]:
        s = self.session
        m = s.model
        usage = s.context_usage() if m else None
        return {
            "cwd": s.cwd, "agentDir": str(s.agent_dir), "mode": "tui" if s.opts.mode == "interactive" else s.opts.mode, "hasUI": bool(getattr(s.ui, "has_ui", False)),
            "model": m and {k: v for k, v in m.items() if not k.startswith("_")}, "thinkingLevel": s.thinking_level,
            "activeTools": list(s.active_tool_names),
            "allTools": [{"name": t.name, "description": t.description, "parameters": t.parameters, "source": t.source,
                          "promptSnippet": t.prompt_snippet, "promptGuidelines": t.prompt_guidelines} for t in s.all_tools().values()],
            "models": [{k: v for k, v in x.items() if not k.startswith("_")} for x in s.registry.all()],
            "sessionId": s.sessions.session_id, "sessionFile": str(s.sessions.file) if s.sessions.file else None,
            "sessionDir": str(s.sessions.session_dir), "sessionName": s.sessions.session_name(), "leafId": s.sessions.leaf_id,
            "isIdle": not s.is_streaming, "pendingMessages": bool(s.steer_queue or s.follow_queue), "trusted": s.trusted,
            "contextUsage": usage, "systemPrompt": s.last_system_prompt, "settings": s.settings.data,
            "commands": self._command_infos(), "flags": getattr(s.opts, "flags", {}) or {},
            "width": getattr(s.ui, "width", 100), "height": getattr(s.ui, "height", 30), "toolsExpanded": getattr(s.ui, "tools_expanded", False),
            "scopedModels": [{"model": x} for x in s.scoped_models],
            "mcpServers": [{"name": n, "state": x.state, "exposure": x.exposure, "tools": [t["name"] for t in x.tools],
                            "source": x.cfg.get("_source")} for n, x in (s.mcp.servers.items() if s.mcp else [])],
            "editorText": getattr(s.ui, "editor_text", lambda: "")() if callable(getattr(s.ui, "editor_text", None)) else "",
        }

    def _command_infos(self) -> List[Dict[str, Any]]:
        out = [{"name": c["name"], "description": c.get("description"), "source": "extension", "path": c.get("source")}
               for c in (self.command_list or [dict(c, name=n) for n, c in self.commands.items()])]
        out += [{"name": t.name, "description": t.description, "source": "prompt"} for t in self.session.templates]
        out += [{"name": f"skill:{k.name}", "description": k.description, "source": "skill"} for k in self.session.skills]
        return out

    def sync_state(self) -> None:
        st = self.state()
        js = json.dumps(st, sort_keys=True, default=str)
        if js != self._last_state:
            self._last_state = js
            self.notify("state", {"patch": st})

    def _on_session_change(self, kind: str, payload: Any) -> None:
        sm = self.session.sessions
        if kind == "reset":
            self.notify("session.reset", {"entries": sm.entries, "header": sm.header, "leafId": sm.leaf_id})
        elif kind == "append":
            self.notify("session.append", {"entry": payload})

    # ---------------------------------------------------------------------------------------------- load
    def load(self, paths: List[str]) -> None:
        from ..tui.theme import palette_for
        self.call("init", {"state": self.state(), "palette": palette_for(self.session.settings.get("theme", "omni"))}, timeout=30)
        self._on_session_change("reset", None)
        r = self.call("load", {"paths": paths}, timeout=120)
        self.extensions = r.get("extensions") or []
        self._set_tools(r.get("tools") or [])
        self._set_commands(r.get("commands") or [])
        self.shortcuts = {s["key"]: s for s in r.get("shortcuts") or []}
        self.flags = {f["name"]: f for f in r.get("flags") or []}
        self.events = set(r.get("events") or [])
        for e in self.extensions:
            if not e.get("ok"):
                self.session.diagnostics.append(f"extension {e['name']}: {e.get('error')}")

    def _set_tools(self, tools: List[Dict[str, Any]]) -> None:
        self.tools = {}
        for t in tools:
            self.tools[t["name"]] = self._tool_def(t)

    def _tool_def(self, t: Dict[str, Any]) -> ToolDef:
        name = t["name"]

        def execute(tool_call_id, params, signal=None, on_update=None, ctx=None):
            if on_update:
                self.tool_updates[tool_call_id] = on_update
            remove = signal.add_listener(lambda: self.notify_abort(tool_call_id)) if signal is not None else (lambda: None)
            try:
                self.sync_state()
                return self.call("tool_execute", {"name": name, "toolCallId": tool_call_id, "params": params})
            except HostError as e:
                raise ToolError(str(e))
            finally:
                remove()
                self.tool_updates.pop(tool_call_id, None)

        td = ToolDef(name=name, label=t.get("label") or name, description=t.get("description") or "",
                     parameters=t.get("parameters") or {"type": "object", "properties": {}}, execute=execute,
                     prompt_snippet=t.get("promptSnippet"), prompt_guidelines=list(t.get("promptGuidelines") or []),
                     source=f"extension:{t.get('source')}", has_renderer=bool(t.get("hasRenderer")))
        td.__dict__["default_active"] = t.get("defaultActive", True)
        return td

    def notify_abort(self, tool_call_id: str) -> None:
        try:
            self._send({"id": next(self._ids), "method": "tool_abort", "params": {"toolCallId": tool_call_id}})
        except Exception:
            pass

    # --------------------------------------------------------------------------------------------- API
    def has(self, event: str) -> bool:
        return event in self.events

    def emit(self, event: str, payload: Dict[str, Any]) -> Any:
        if event not in self.events or self._closed:
            return None
        self.sync_state()
        return self.call("emit", {"event": event, "payload": payload})

    def _set_commands(self, items: List[Dict[str, Any]]) -> None:
        """`command_list` keeps every registration (two extensions may both register `/name`);
        `commands` maps a name to its first registration, as Pi resolves a bare name."""
        self.command_list = list(items)
        self.commands = {}
        for c in self.command_list:
            self.commands.setdefault(c["name"], c)

    def run_command(self, name: str, args: str, id: Optional[str] = None) -> None:
        self.sync_state()
        self.call("command", {"name": name, "args": args, "id": id})

    def complete_command(self, name: str, prefix: str, id: Optional[str] = None) -> Optional[List[Dict[str, Any]]]:
        try:
            return self.call("command_complete", {"name": name, "prefix": prefix, "id": id}, timeout=2)
        except Exception:
            return None

    def run_shortcut(self, key: str) -> bool:
        self.sync_state()
        return bool(self.call("shortcut", {"key": key}))

    def render(self, payload: Dict[str, Any], timeout: float = 0.75) -> Optional[List[str]]:
        try:
            return self.call("render", payload, timeout=timeout)
        except Exception:
            return None

    def custom_input(self, cid: str, data: str) -> None:
        self.notify_call("custom_input", {"id": cid, "data": data})

    def custom_cancel(self, cid: str) -> None:
        self.notify_call("custom_cancel", {"id": cid})

    def notify_call(self, method: str, params: Dict[str, Any]) -> None:
        """Fire-and-forget request (response ignored) - safe to call from the UI thread."""
        try:
            self._send({"id": f"x{next(self._ids)}", "method": method, "params": params})
        except Exception:
            pass

    def report(self) -> List[Dict[str, Any]]:
        try:
            return (self.call("report", {}, timeout=5) or {}).get("extensions") or self.extensions
        except Exception:
            return self.extensions

    def shutdown(self) -> None:
        try:
            self.call("shutdown", {}, timeout=2)
        except Exception:
            pass
        try:
            self.proc.terminate()
        except Exception:
            pass
        self._pool.shutdown(wait=False, cancel_futures=True)
        try:
            self._log.close()
        except Exception:
            pass

    # ---------------------------------------------------------------------------- UI features from extensions
    def terminal_input(self, data: str):
        """Raw input through ctx.ui.onTerminalInput listeners. Returns the (possibly replaced) data, or None when consumed."""
        if not self.features["terminalInput"]:
            return data
        try:
            r = self.call("terminal_input", {"data": data}, timeout=1)
        except Exception:
            return data
        if r and r.get("consume"):
            return None
        return (r or {}).get("data", data)

    def autocomplete(self, lines, line, col, options=None):
        try:
            return self.call("autocomplete", {"lines": lines, "line": line, "col": col, "options": options or {}}, timeout=3)
        except Exception:
            return None

    def autocomplete_apply(self, lines, line, col, item, prefix):
        try:
            return self.call("autocomplete_apply", {"lines": lines, "line": line, "col": col, "item": item, "prefix": prefix}, timeout=2)
        except Exception:
            return None

    def editor_input(self, data: str):
        try:
            return self.call("editor_input", {"data": data}, timeout=2)
        except Exception:
            return None

    def editor_set_text(self, text: str):
        try:
            return self.call("editor_set_text", {"text": text}, timeout=2)
        except Exception:
            return None

    def markdown_transform(self, markdown: str, message_type: str, is_streaming: bool = False, width: int = 80,
                           message_id=None, timestamp=None) -> str:
        if not self.features["markdownTransformers"] or not markdown:
            return markdown
        cache = self.__dict__.setdefault("_md_cache", {})
        key = (markdown, message_type, is_streaming, width)
        if key in cache:
            return cache[key]
        try:
            out = self.call("markdown_transform", {"markdown": markdown, "messageType": message_type, "isStreaming": is_streaming,
                                                   "availableWidth": width, "messageId": message_id, "timestamp": timestamp}, timeout=1)
        except Exception:
            out = markdown
        out = out if isinstance(out, str) else markdown
        if len(cache) > 2000:
            cache.clear()
        cache[key] = out
        return out

    def route_virtual(self, provider: str, model_id: str, request: Dict[str, Any]) -> Dict[str, Any]:
        return self.call("vm_route", {"provider": provider, "id": model_id, "request": request}, timeout=120)

    # ------------------------------------------------------------------------------------ requests from JS
    def _handle(self, method: str, p: Dict[str, Any]) -> Any:
        s = self.session
        ui = s.ui
        if method == "ui.terminalInput":
            self.features["terminalInput"] = int(p.get("count") or 0)
        elif method == "ui.autocomplete":
            self.features["autocomplete"] = bool(p.get("active"))
            self.features["triggerCharacters"] = list(p.get("triggerCharacters") or [])
        elif method == "ui.markdownTransformers":
            self.features["markdownTransformers"] = int(p.get("count") or 0)
            if hasattr(self, "_md_cache"):
                self._md_cache.clear()
        elif method == "ui.editor_render":
            self.features["editor"] = bool(p.get("active"))
        if method.startswith("ui."):
            return self._ui(method[3:], p)
        if method in ("editor.action", "editor.submit"):
            return self._ui(method.replace(".", "_"), p)
        if method == "autocomplete.base":
            fn = getattr(ui, "ext_autocomplete_base", None)
            return fn(**p) if fn else None
        if method == "pi.registerVirtualModel":
            s.registry.register_virtual(p.get("model") or {})
            s._notify({"type": "models_changed"})
            return None
        if method == "pi.unregisterVirtualModel":
            s.registry.unregister_virtual(p.get("provider"), p.get("id"))
            s._notify({"type": "models_changed"})
            return None
        if method == "tool_update":
            fn = self.tool_updates.get(p.get("toolCallId"))
            if fn and p.get("partial"):
                fn(p["partial"])
            return None
        if method == "pi.sendMessage":
            s.send_custom_message(p.get("message") or {}, p.get("options") or {})
        elif method == "pi.sendUserMessage":
            s.send_user_message(p.get("content"), p.get("options") or {})
        elif method == "pi.appendEntry":
            s.sessions.append_custom(p.get("customType", "extension"), p.get("data"))
        elif method == "pi.setSessionName":
            s.set_session_name(p.get("name") or "")
        elif method == "pi.setLabel":
            s.sessions.append_label(p["entryId"], p.get("label"))
        elif method == "pi.setActiveTools":
            s.set_active_tools(p.get("names") or [])
            s._notify({"type": "tools_changed"})
        elif method == "pi.setModel":
            m = s.registry.find(p.get("provider"), p.get("id")) or s.registry.resolve(f"{p.get('provider')}/{p.get('id')}")
            if not m or not s.auth.has_auth(m["provider"]):
                return False
            s.set_model(m)
            s._notify({"type": "model_changed"})
            return True
        elif method == "pi.setThinkingLevel":
            s.set_thinking_level(p.get("level") or "off")
            s._notify({"type": "model_changed"})
        elif method == "pi.registerProvider":
            cfg = dict(p.get("config") or {})
            name = p["name"]
            if str(cfg.get("api", "")).startswith("ext:"):
                self._register_js_api(cfg["api"], name)
            oa = cfg.pop("oauth", None)
            if isinstance(oa, dict):
                self._register_js_oauth(name, oa)
            s.registry.register_provider(name, cfg)
        elif method == "pi.unregisterProvider":
            from ..ai import oauth
            oauth.unregister_extension_provider(p["name"])
            s.registry.unregister_provider(p["name"])
        elif method == "oauth.auth":
            ia = self._logins.get(p.get("loginId"), (None, None))[0]
            if ia is not None:
                url, how = p.get("url") or "", p.get("instructions") or "Complete the sign-in in your browser."
                ia.notify(f"{how}\n{url}" if url else how, "info")
                if url:
                    ia.open_url(url)
        elif method == "oauth.progress":
            ia = self._logins.get(p.get("loginId"), (None, None))[0]
            if ia is not None and p.get("message"):
                ia.notify(p["message"], "info")
        elif method == "oauth.prompt":
            ia, done = self._logins.get(p.get("loginId"), (None, None))
            if ia is None or done.is_set():
                raise RuntimeError("login is over")
            v = ia.manual(p.get("message") or "Paste the code", p.get("placeholder") or "", done)
            if v is None:
                raise RuntimeError("Login cancelled")
            return v
        elif method == "pi.registerMcpServer":
            name, cfg = p.get("name"), dict(p.get("config") or {})
            self.mcp_servers[name] = cfg
            if s.mcp is not None:
                try:
                    s.mcp.register(name, cfg)
                except ValueError as e:
                    s.diagnostics.append(f"mcp server '{name}' from an extension: {e}")
            elif not s.opts.no_mcp and s.active_tool_names is not None and getattr(s, "_mcp_started", False):
                s._start_mcp()
        elif method == "pi.unregisterMcpServer":
            self.mcp_servers.pop(p.get("name"), None)
            if s.mcp is not None:
                s.mcp.unregister(p.get("name"))
        elif method == "auth.setRuntimeApiKey":
            s.auth.set_runtime_api_key(p["provider"], p.get("key"))
        elif method == "auth.getApiKey":
            return s.auth.get_api_key(p.get("provider") or "")
        elif method == "auth.has":
            return s.auth.has_auth(p.get("provider") or "")
        elif method == "models.refresh":
            s.registry.reload()
        elif method == "ctx.abort":
            s.abort()
        elif method == "ctx.shutdown":
            if ui is not None and hasattr(ui, "request_quit"):
                ui.request_quit()
        elif method == "ctx.compact":
            threading.Thread(target=s.compact, args=(p.get("customInstructions"),), daemon=True).start()
        elif method == "ctx.waitForIdle":
            s.wait_for_idle()
        elif method == "ctx.newSession":
            return {"cancelled": not s.new_session()}
        elif method == "ctx.fork":
            return {"cancelled": not s.fork(p["entryId"], p.get("position", "before"))}
        elif method == "ctx.navigateTree":
            return {"cancelled": not s.navigate_tree(p.get("targetId"), bool(p.get("summarize")), p.get("label"))}
        elif method == "ctx.switchSession":
            return {"cancelled": not s.resume(p["sessionPath"])}
        elif method == "ctx.reload":
            threading.Thread(target=s.reload, daemon=True).start()
        elif method == "builtin.execute":
            tool = s.builtin_tools.get(p["name"])
            if not tool:
                raise ToolError(f"Tool {p['name']} not found")
            from ..tools.base import ToolContext
            try:
                return tool.execute(p.get("toolCallId", ""), p.get("params") or {}, s.signal, None, ToolContext(cwd=p.get("cwd") or s.cwd))
            except ToolError as e:
                return {"content": [{"type": "text", "text": str(e)}], "isError": True}
        elif method == "ai.complete":
            from ..ai.stream import complete
            m = p.get("model") or s.model
            if m and "api" not in m:
                m = s.registry.find(m.get("provider"), m.get("id")) or s.model
            return complete(m, p.get("context") or {}, {"apiKey": s.auth.get_api_key(m["provider"]), **(p.get("options") or {})})
        elif method == "tools_changed":
            self._set_tools(p.get("tools") or [])
            s._init_tools() if not s.active_tool_names else None
            s._notify({"type": "tools_changed"})
        elif method == "commands_changed":
            self._set_commands(p.get("commands") or [])
        elif method == "events_changed":
            self.events = set(p.get("events") or [])
        elif method == "extension_error":
            s._notify({"type": "extension_error", "path": p.get("path"), "event": p.get("event"), "error": p.get("error")})
        elif method == "missing":
            self.missing.add(p.get("label"))
        elif method == "provider_event":
            q = self.provider_streams.get(p.get("streamId"))
            if q is not None:
                q.append(p.get("event"))
        return None

    def _ui(self, name: str, p: Dict[str, Any]) -> Any:
        ui = self.session.ui
        if ui is None:
            return None if name not in ("confirm",) else False
        fn = getattr(ui, f"ext_{name}", None)
        if fn is None:
            return None
        return fn(**p)

    def _register_js_oauth(self, provider: str, info: Dict[str, Any]) -> None:
        """Expose an extension provider's `oauth` (login / refreshToken / getApiKey, run in the host) to `/login`
        and `AuthStorage`, the way Pi's /login lists custom OAuth providers."""
        from ..ai import oauth

        def login(ia):
            lid = f"login{next(self._ids)}"
            done = threading.Event()
            self._logins[lid] = (ia, done)
            try:
                cred = self.call("provider_oauth_login", {"name": provider, "loginId": lid})
            except HostError as e:
                if "cancel" in str(e).lower():
                    raise oauth.LoginCancelled(str(e)) from None
                raise oauth.OAuthError(str(e)) from None
            finally:
                done.set()  # closes a paste prompt the extension left open
                self._logins.pop(lid, None)
            if not isinstance(cred, dict) or not cred.get("access"):
                raise oauth.OAuthError(f"{provider} login returned no access token")
            return dict(cred, type="oauth")

        def refresh(cred):
            if not info.get("hasRefresh"):
                raise oauth.OAuthError(f"{provider} cannot refresh; sign in again with /login {provider}")
            plain = {k: v for k, v in cred.items() if k != "type"}
            try:
                fresh = self.call("provider_oauth_refresh", {"name": provider, "credentials": plain}, timeout=120)
            except (HostError, TimeoutError) as e:
                raise oauth.OAuthError(str(e)) from None
            if not isinstance(fresh, dict) or not fresh.get("access"):
                raise oauth.OAuthError(f"{provider} refresh returned no access token")
            return dict(cred, **fresh, type="oauth")

        def api_key(cred):
            plain = {k: v for k, v in cred.items() if k != "type"}
            return self.call("provider_oauth_api_key", {"name": provider, "credentials": plain}, timeout=30)

        oauth.register_extension_provider(provider, str(info.get("name") or provider), login, refresh,
                                          api_key if info.get("hasGetApiKey") else None)

    def _register_js_api(self, api: str, provider: str) -> None:
        from ..ai.stream import register_api, _Builder

        def run(model, context, options):
            sid = f"s{next(self._ids)}"
            events: List[Dict[str, Any]] = []
            self.provider_streams[sid] = events
            done = threading.Event()
            box: Dict[str, Any] = {}

            def worker():
                try:
                    box["msg"] = self.call("provider_stream", {"name": provider, "model": model, "context": context,
                                                               "options": {k: v for k, v in options.items() if k not in ("signal", "onResponse")}})
                except Exception as e:
                    box["err"] = e
                done.set()
            threading.Thread(target=worker, daemon=True).start()
            yield {"type": "start"}
            seen = 0
            while not done.is_set() or seen < len(events):
                while seen < len(events):
                    ev = events[seen]
                    seen += 1
                    if ev and ev.get("type") in ("text_delta", "thinking_delta"):
                        yield {"type": ev["type"], "delta": ev.get("delta", "")}
                time.sleep(0.02)
            self.provider_streams.pop(sid, None)
            if "err" in box:
                from ..ai.stream import ProviderError
                raise ProviderError(str(box["err"]))
            msg = box.get("msg") or _Builder(model).message()
            yield {"type": "done", "message": msg}

        register_api(api, run)
