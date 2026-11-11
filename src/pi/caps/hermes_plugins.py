"""`hermes-plugins`: run Hermes plugins unchanged, with their hooks, on this core.

A Hermes plugin is a folder with `plugin.yaml` and an `__init__.py` exposing `register(ctx)`. This host loads them
in-process from

    ~/.hermes/plugins/<name>/          the user's Hermes plugins
    <project>/.hermes/plugins/<name>/  project plugins (trusted projects only)
    <omni>/plugins/hermes/<name>/      plugins shipped with Omni
    profile "hermes_plugin_dirs": [...]

and gives each a `ctx` with Hermes' `PluginContext` surface:

    register_tool(name, toolset, schema, handler, check_fn=, requires_env=, is_async=, description=, ...)
    register_hook(hook_name, callback)        register_command(name, handler, description=, args_hint=)
    register_system_prompt_section(id, content)   get_config / set_config   dispatch_tool   inject_message
    session_cwd, profile_name, plugin_id, manifest, on_unload

Hooks are mapped onto the core's pipeline:

    pre_tool_call           tool gate: `{"action": "block", "message": ...}` vetoes the call
    post_tool_call          observer after each tool
    transform_tool_result   may return replacement text for a tool result
    transform_terminal_output  same, for shell commands only
    pre_llm_call            once per user message; `{"context": ...}` (or a string) is appended to the message
    post_llm_call           observer when the agent finishes a turn
    on_session_start / on_session_end

Anything else a plugin registers (platforms, memory providers, image generators, ...) is accepted and recorded as
"not supported yet" instead of crashing the plugin; `/doctor` lists it.
"""
from __future__ import annotations

import asyncio
import importlib.util
import inspect
import json
import os
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable, Dict, List, Optional

from ..tools.base import ToolDef, ToolError, text_result
from . import Capability

SUPPORTED_HOOKS = {"pre_tool_call", "post_tool_call", "transform_tool_result", "transform_terminal_output",
                   "pre_llm_call", "post_llm_call", "on_session_start", "on_session_end"}
PROMPT_SECTION_MAX = 4000


def _run(value: Any) -> Any:
    """Resolve a coroutine from a sync context (plugins may register async handlers and hooks)."""
    if not inspect.isawaitable(value):
        return value
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(value)
    import threading
    box: Dict[str, Any] = {}
    t = threading.Thread(target=lambda: box.update(v=asyncio.run(value)), daemon=True)
    t.start()
    t.join()
    return box.get("v")


def _call(fn: Callable, *args: Any, **kwargs: Any) -> Any:
    """Call with only the keyword arguments the callback accepts (Hermes passes many; plugins declare a few)."""
    try:
        sig = inspect.signature(fn)
        if not any(p.kind == p.VAR_KEYWORD for p in sig.parameters.values()):
            kwargs = {k: v for k, v in kwargs.items() if k in sig.parameters}
    except (TypeError, ValueError):
        pass
    return _run(fn(*args, **kwargs))


def _parse_manifest(path: Path) -> Dict[str, Any]:
    text = path.read_text(encoding="utf-8")
    try:
        import yaml  # type: ignore
        data = yaml.safe_load(text) or {}
        return data if isinstance(data, dict) else {}
    except Exception:
        out: Dict[str, Any] = {}
        for line in text.splitlines():
            if ":" in line and not line.startswith((" ", "\t", "#")):
                k, v = line.split(":", 1)
                out[k.strip()] = v.strip().strip("\"'")
        return out


class PluginContext:
    def __init__(self, host: "HermesHost", manifest: Dict[str, Any], path: Path):
        self._host, self._path = host, path
        self.manifest = SimpleNamespace(**{"name": path.name, "version": "0", "description": "", **manifest})
        self.plugin_id = str(manifest.get("name") or path.name)
        self.record: Dict[str, Any] = {"name": self.plugin_id, "path": str(path), "ok": False, "error": None, "tools": [],
                                       "hooks": [], "commands": [], "unsupported": []}
        self._unload: List[Callable[[], None]] = []

    # --- identity / environment
    @property
    def session_cwd(self) -> str:
        return self._host.session.cwd

    @property
    def profile_name(self) -> str:
        return str(self._host.session.profile.get("name") or "default")

    @property
    def session_surface(self) -> str:
        return "cli"

    def has_plugin(self, plugin_id: str) -> bool:
        return any(p["name"] == plugin_id and p["ok"] for p in self._host.plugins)

    # --- config (kept per plugin in <agent-dir>/hermes-plugins.json)
    def get_config(self, key: str, default: Any = None) -> Any:
        return (self._host.config().get(self.plugin_id) or {}).get(key, default)

    def set_config(self, key: str, value: Any) -> None:
        cfg = self._host.config()
        cfg.setdefault(self.plugin_id, {})[key] = value
        self._host.save_config(cfg)

    # --- registration
    def register_tool(self, name: str, *args: Any, **kw: Any) -> None:
        # Hermes: (name, toolset, schema, handler, ...); older / simplified plugins: (name, schema, handler)
        if args and isinstance(args[0], dict):
            toolset, schema, handler, rest = "plugin", args[0], args[1] if len(args) > 1 else kw.get("handler"), args[2:]
        else:
            toolset = args[0] if args else kw.get("toolset", "plugin")
            schema = args[1] if len(args) > 1 else kw.get("schema") or {}
            handler = args[2] if len(args) > 2 else kw.get("handler")
            rest = args[3:]
        check_fn = rest[0] if rest else kw.get("check_fn")
        requires_env = (rest[1] if len(rest) > 1 else kw.get("requires_env")) or []
        if not callable(handler):
            raise TypeError(f"register_tool({name!r}): handler must be callable")
        missing = [e for e in requires_env if not os.environ.get(str(e))]
        if missing:
            self.record["unsupported"].append(f"tool {name} (needs {', '.join(map(str, missing))})")
            return
        if callable(check_fn):
            try:
                if not check_fn():
                    self.record["unsupported"].append(f"tool {name} (its availability check failed)")
                    return
            except Exception:
                return
        fn_schema = schema.get("function") if isinstance(schema.get("function"), dict) else schema
        self._host.add_tool(self, name, toolset, fn_schema, handler, kw.get("description") or "")
        self.record["tools"].append(name)

    def register_hook(self, hook_name: str, callback: Callable, **_kw: Any) -> None:
        if hook_name not in SUPPORTED_HOOKS:
            self.record["unsupported"].append(f"hook {hook_name}")
            return
        self._host.hooks.setdefault(hook_name, []).append((self.plugin_id, callback))
        self.record["hooks"].append(hook_name)

    def register_command(self, name: str, handler: Callable, description: str = "", args_hint: str = "", **_kw: Any) -> None:
        clean = name.lower().strip().lstrip("/").replace(" ", "-")
        if clean:
            self._host.commands[clean] = (description or f"{self.plugin_id} plugin command",
                                          lambda raw, h=handler: _call(h, raw))
            self.record["commands"].append(clean)

    def register_system_prompt_section(self, id: str, content: Any, **kw: Any) -> None:
        limit = min(int(kw.get("max_chars") or PROMPT_SECTION_MAX), PROMPT_SECTION_MAX)
        self._host.prompt_sections[f"{self.plugin_id}:{id}"] = (content, limit)

    def on_unload(self, callback: Callable[[], None]) -> None:
        self._unload.append(callback)

    # --- acting on the session
    def dispatch_tool(self, tool_name: str, args: dict, **_kw: Any) -> str:
        s = self._host.session
        tool = s.all_tools().get(tool_name)
        if tool is None:
            return json.dumps({"error": f"unknown tool {tool_name}"})
        from ..tools.base import ToolContext
        try:
            r = tool.execute(f"plugin-{int(time.time() * 1000)}", args or {}, None, None, ToolContext(cwd=s.cwd, session=s))
        except ToolError as e:
            return json.dumps({"error": str(e)})
        return "".join(b.get("text", "") for b in r.get("content") or [] if b.get("type") == "text")

    def inject_message(self, content: str = "", role: str = "user", **_kw: Any) -> bool:
        s = self._host.session
        try:
            s.next_turn_messages.append({"role": "user", "content": str(content), "timestamp": int(time.time() * 1000)})
            return True
        except Exception:
            return False

    post_message = inject_message

    def __getattr__(self, name: str) -> Any:
        if name.startswith("register_"):  # a plugin kind this core has no slot for yet: accept, record, carry on
            def stub(*_a: Any, **_k: Any) -> None:
                self.record["unsupported"].append(name[len("register_"):])
            return stub
        raise AttributeError(name)


class HermesHost:
    def __init__(self, session: Any):
        self.session = session
        self.plugins: List[Dict[str, Any]] = []
        self.tools: Dict[str, ToolDef] = {}
        self.hooks: Dict[str, List[Any]] = {}
        self.commands: Dict[str, Any] = {}
        self.prompt_sections: Dict[str, Any] = {}
        self._contexts: List[PluginContext] = []
        self._unsubscribe: Optional[Callable[[], None]] = None
        self._last_user = ""

    # --- config store
    def _config_path(self) -> Path:
        return Path(self.session.agent_dir) / "hermes-plugins.json"

    def config(self) -> Dict[str, Any]:
        try:
            d = json.loads(self._config_path().read_text(encoding="utf-8"))
            return d if isinstance(d, dict) else {}
        except (OSError, ValueError):
            return {}

    def save_config(self, cfg: Dict[str, Any]) -> None:
        self._config_path().write_text(json.dumps(cfg, indent=2), encoding="utf-8")

    # --- discovery / loading
    def plugin_dirs(self) -> List[Path]:
        s = self.session
        from ..profiles import PROJECT_ROOT
        roots = [Path.home() / ".hermes" / "plugins", PROJECT_ROOT / "plugins" / "hermes"]
        if s.trusted:
            roots.append(Path(s.cwd) / ".hermes" / "plugins")
        extra = s.profile.get("hermes_plugin_dirs")
        if isinstance(extra, list):
            roots = [Path(os.path.expanduser(str(e))) for e in extra] + (roots if s.profile.get("hermes_default_dirs", True) else [])
        out: List[Path] = []
        for r in roots:
            if r.is_dir():
                out += sorted(d for d in r.iterdir() if d.is_dir() and (d / "plugin.yaml").is_file() and (d / "__init__.py").is_file())
        return out

    def load(self) -> None:
        disabled = set(self.config().get("_disabled") or [])
        seen = set()
        for d in self.plugin_dirs():
            manifest = {}
            try:
                manifest = _parse_manifest(d / "plugin.yaml")
            except Exception:
                pass
            ctx = PluginContext(self, manifest, d)
            if ctx.plugin_id in seen:
                continue
            seen.add(ctx.plugin_id)
            self.plugins.append(ctx.record)
            if ctx.plugin_id in disabled:
                ctx.record["error"] = "disabled"
                continue
            try:
                mod_name = f"omni_hermes_plugin_{ctx.plugin_id.replace('-', '_')}_{abs(hash(str(d))) % 10**8}"
                spec = importlib.util.spec_from_file_location(mod_name, d / "__init__.py", submodule_search_locations=[str(d)])
                mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
                sys.modules[mod_name] = mod
                spec.loader.exec_module(mod)  # type: ignore[union-attr]
                if not callable(getattr(mod, "register", None)):
                    raise RuntimeError("no register(ctx) function")
                _run(mod.register(ctx))
                ctx.record["ok"] = True
                self._contexts.append(ctx)
            except Exception as e:
                ctx.record["error"] = f"{type(e).__name__}: {e}"
                self.session.diagnostics.append(f"hermes plugin {ctx.plugin_id}: {ctx.record['error']}")
        self._unsubscribe = self.session.subscribe(self._on_event)
        self.fire("on_session_start", session_id=self.session.sessions.session_id, model=(self.session.model or {}).get("id"),
                  platform="cli")

    def unload(self) -> None:
        self.fire("on_session_end", session_id=self.session.sessions.session_id, completed=True, interrupted=False,
                  model=(self.session.model or {}).get("id"), platform="cli")
        if self._unsubscribe:
            self._unsubscribe()
        for ctx in self._contexts:
            for cb in ctx._unload:
                try:
                    cb()
                except Exception:
                    pass

    # --- tools
    def add_tool(self, ctx: PluginContext, name: str, toolset: str, schema: Dict[str, Any], handler: Callable, description: str) -> None:
        session = self.session

        def execute(_id, p, signal=None, on_update=None, c=None):
            try:
                out = _call(handler, p or {}, task_id=session.sessions.session_id, session_id=session.sessions.session_id,
                            tool_call_id=_id, context=None)
            except ToolError:
                raise
            except Exception as e:
                raise ToolError(f"{type(e).__name__}: {e}") from None
            text = out if isinstance(out, str) else json.dumps(out, ensure_ascii=False, default=str)
            is_error = False
            try:  # Hermes tools report failures as {"error": ...} JSON
                parsed = json.loads(text) if text.lstrip().startswith("{") else None
                is_error = isinstance(parsed, dict) and bool(parsed.get("error")) and parsed.get("success") is not True
            except ValueError:
                pass
            return text_result(text, {"plugin": ctx.plugin_id, "toolset": toolset}, is_error)

        self.tools[name] = ToolDef(
            name=name, label=name, description=str(schema.get("description") or description or f"{ctx.plugin_id} tool"),
            parameters=schema.get("parameters") or {"type": "object", "properties": {}}, execute=execute)

    # --- hooks
    def fire(self, hook: str, **kwargs: Any) -> List[Any]:
        out = []
        for plugin_id, cb in self.hooks.get(hook, []):
            try:
                r = _call(cb, **kwargs)
            except Exception as e:
                self.session.diagnostics.append(f"hermes plugin {plugin_id} [{hook}]: {type(e).__name__}: {e}")
                continue
            if r is not None:
                out.append(r)
        return out

    def _ids(self) -> Dict[str, Any]:
        sid = self.session.sessions.session_id
        return {"session_id": sid, "task_id": sid}

    def gate(self, tool_name: str, args: Dict[str, Any]) -> Optional[str]:
        for r in self.fire("pre_tool_call", tool_name=tool_name, args=args, **self._ids()):
            if isinstance(r, dict) and r.get("action") == "block":
                return str(r.get("message") or f"{tool_name} blocked by a Hermes plugin")
        return None

    def after_tool(self, tool_name: str, args: Dict[str, Any], result: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        text = "".join(b.get("text", "") for b in result.get("content") or [] if b.get("type") == "text")
        kw = dict(tool_name=tool_name, args=args, result=text, status="error" if result.get("isError") else "ok",
                  duration_ms=0, **self._ids())
        self.fire("post_tool_call", **kw)
        new = None
        if tool_name == "bash":
            new = next((r for r in self.fire("transform_terminal_output", command=args.get("command", ""), output=text,
                                             returncode=0, **self._ids()) if isinstance(r, str)), None)
        new = next((r for r in self.fire("transform_tool_result", **{**kw, "result": new or text}) if isinstance(r, str)), new)
        if new is None or new == text:
            return None
        others = [b for b in result.get("content") or [] if b.get("type") != "text"]
        return {**result, "content": [{"type": "text", "text": new}] + others}

    def on_prompt(self, text: str) -> Optional[str]:
        self._last_user = text
        s = self.session
        history = [e["message"] for e in s.sessions.get_branch() if e.get("type") == "message"]
        parts = []
        for r in self.fire("pre_llm_call", user_message=text, conversation_history=history, is_first_turn=not history,
                           model=(s.model or {}).get("id"), platform="cli", **self._ids()):
            ctx_text = r.get("context") if isinstance(r, dict) else r if isinstance(r, str) else None
            if ctx_text:
                parts.append(str(ctx_text))
        return "\n\n".join(parts) or None

    def _on_event(self, ev: Dict[str, Any]) -> None:
        if ev.get("type") != "agent_end" or "post_llm_call" not in self.hooks:
            return
        s = self.session
        history = [e["message"] for e in s.sessions.get_branch() if e.get("type") == "message"]
        last = next((m for m in reversed(history) if m.get("role") == "assistant"), None)
        reply = "".join(b.get("text", "") for b in (last or {}).get("content") or [] if b.get("type") == "text")
        self.fire("post_llm_call", user_message=self._last_user, assistant_response=reply, conversation_history=history,
                  model=(s.model or {}).get("id"), platform="cli", **self._ids())

    def prompt(self) -> str:
        info = {"session_id": self.session.sessions.session_id, "cwd": self.session.cwd, "platform": "cli"}
        out = []
        for _id, (content, limit) in self.prompt_sections.items():
            try:
                text = content(info) if callable(content) else content
            except Exception:
                continue
            if text:
                out.append(str(text)[:limit])
        return "\n\n".join(out)


def host_of(session: Any) -> Optional[HermesHost]:
    return getattr(session, "_hermes_host", None)


def _tools(session: Any) -> Dict[str, ToolDef]:
    host = HermesHost(session)
    session._hermes_host = host
    host.load()
    return host.tools


def _unload(session: Any) -> None:
    host = host_of(session)
    if host is not None:
        host.unload()
        session._hermes_host = None


def _with(fn_name: str, default: Any = None):
    def call(session: Any, *args: Any) -> Any:
        host = host_of(session)
        return getattr(host, fn_name)(*args) if host is not None else default
    return call


CAPABILITIES = [Capability(
    "hermes-plugins", "Hermes plugin host", "Run Hermes plugins (plugin.yaml + register(ctx)) with their tools, hooks, "
    "commands and prompt sections.", "Hermes", tools=_tools, prompt=lambda s: host_of(s).prompt() if host_of(s) else "",
    gate=_with("gate"), after_tool=_with("after_tool"), on_prompt=_with("on_prompt"),
    commands=lambda s: dict(host_of(s).commands) if host_of(s) else {}, unload=_unload, group="host")]
