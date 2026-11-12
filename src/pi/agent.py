"""AgentSession: Pi's agent loop, queues, compaction, retries and session operations.

Event names and ordering follow docs/extensions.md "Lifecycle Overview":
  input -> before_agent_start -> agent_start -> [turn_start -> context -> before_provider_request -> message_* ->
  (tool_execution_start -> tool_call -> tool_execution_update* -> tool_result -> tool_execution_end)* -> turn_end]* ->
  agent_end -> agent_settled
Every event goes to UI subscribers and to the extension host (which runs real Pi extension handlers).
"""
from __future__ import annotations

import copy
import json
import os
import threading
import time
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from . import resources as res
from .ai import AbortSignal, AbortedError, AuthStorage, ModelRegistry, ProviderError, THINKING_LEVELS, clamp_thinking, empty_usage, model_key
from .ai import stream as ai_stream
from .ai.stream import complete as ai_complete
from .prompt import PromptOptions, build_sections, render
from .session import SessionManager, convert_to_llm, now_ms
from .tools import ALL_TOOL_NAMES, DEFAULT_TOOLS, ToolContext, ToolDef, ToolError, create_all_tools, resolve_tool_list, run_shell
from .tools.truncate import truncate_tail

SUMMARIZATION_SYSTEM_PROMPT = ("You are a context summarization assistant. Your task is to read a conversation between a user and an AI "
                               "assistant, then produce a structured summary following the exact format specified.\n\nDo NOT continue "
                               "the conversation. Do NOT respond to any questions in the conversation. ONLY output the structured summary.")
SUMMARIZATION_PROMPT = """The messages above are a conversation to summarize. Create a structured context checkpoint summary that another LLM will use to continue the work.

Use this EXACT format:

## Goal
[What is the user trying to accomplish? Can be multiple items if the session covers different tasks.]

## Constraints & Preferences
- [Any constraints, preferences, or requirements mentioned by user]
- [Or "(none)" if none were mentioned]

## Progress
### Done
- [x] [Completed tasks/changes]

### In Progress
- [ ] [Current work]

### Blocked
- [Issues preventing progress, if any]

## Key Decisions
- **[Decision]**: [Brief rationale]

## Next Steps
1. [Ordered list of what should happen next]

## Critical Context
- [Any data, examples, or references needed to continue]
- [Or "(none)" if not applicable]

Keep each section concise. Preserve exact file paths, function names, and error messages."""
UPDATE_SUMMARIZATION_PROMPT = ("The messages above are NEW conversation messages to incorporate into the existing summary provided in "
                               "<previous-summary> tags.\n\nUpdate the existing structured summary with new information. PRESERVE all "
                               "existing information, ADD new progress, decisions and context, move finished items to Done, and update "
                               "Next Steps. Use the same EXACT format as the previous summary. Preserve exact file paths, function "
                               "names, and error messages.")


def block_images(messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """`images.blockImages`: no image ever reaches the model; each is replaced by a short text note."""
    out = []
    for m in messages:
        c = m.get("content")
        if isinstance(c, list) and any(isinstance(b, dict) and b.get("type") == "image" for b in c):
            c = [{"type": "text", "text": "[Image omitted: images.blockImages is enabled]"} if b.get("type") == "image" else b for b in c]
            m = dict(m, content=c)
        out.append(m)
    return out


def estimate_tokens(obj: Any) -> int:
    return max(1, len(json.dumps(obj, ensure_ascii=False)) // 4)


def text_of(content: Any) -> str:
    if isinstance(content, str):
        return content
    return "".join(b.get("text", "") for b in content or [] if isinstance(b, dict) and b.get("type") == "text")


def serialize_conversation(messages: List[Dict[str, Any]]) -> str:
    parts = []
    for m in messages:
        if m["role"] == "user":
            t = text_of(m["content"])
            if t:
                parts.append(f"[User]: {t}")
        elif m["role"] == "assistant":
            calls = [f"{b['name']}(" + ", ".join(f"{k}={json.dumps(v)}" for k, v in (b.get("arguments") or {}).items()) + ")"
                     for b in m["content"] if b.get("type") == "toolCall"]
            if any(b.get("type") == "text" for b in m["content"]):
                parts.append(f"[Assistant]: {text_of(m['content'])}")
            if calls:
                parts.append(f"[Assistant tool calls]: {'; '.join(calls)}")
        elif m["role"] == "toolResult":
            t = text_of(m["content"])
            if t:
                parts.append(f"[Tool result]: {t[:2000] + ('...' if len(t) > 2000 else '')}")
    return "\n\n".join(parts)


class NullHost:
    """Extension host stand-in when extensions are disabled or Node is unavailable."""
    tools: Dict[str, ToolDef] = {}
    commands: Dict[str, Dict[str, Any]] = {}
    command_list: List[Dict[str, Any]] = []
    shortcuts: Dict[str, Dict[str, Any]] = {}
    flags: Dict[str, Any] = {}
    extensions: List[Dict[str, Any]] = []
    mcp_servers: Dict[str, Dict[str, Any]] = {}
    features: Dict[str, Any] = {"terminalInput": 0, "autocomplete": False, "triggerCharacters": [], "editor": False,
                                "markdownTransformers": 0}
    available = False

    def terminal_input(self, data: str):
        return data

    def markdown_transform(self, markdown: str, *a, **k) -> str:
        return markdown

    def emit(self, event: str, payload: Dict[str, Any]) -> Any:
        return None

    def has(self, event: str) -> bool:
        return False

    def run_command(self, name: str, args: str, id: Optional[str] = None) -> None:
        raise KeyError(name)

    def shutdown(self) -> None:
        pass


@dataclass
class SessionOptions:
    cwd: str
    model: Optional[str] = None
    provider: Optional[str] = None
    api_key: Optional[str] = None
    thinking: Optional[str] = None
    tools: Optional[str] = None             # --tools list
    exclude_tools: List[str] = field(default_factory=list)
    no_builtin_tools: bool = False
    no_tools: bool = False
    no_session: bool = False
    session: Optional[str] = None           # path or id
    continue_recent: bool = False
    session_dir: Optional[str] = None
    session_name: Optional[str] = None
    extensions: List[str] = field(default_factory=list)
    no_extensions: bool = False
    skills: List[str] = field(default_factory=list)
    no_skills: bool = False
    prompt_templates: List[str] = field(default_factory=list)
    no_prompt_templates: bool = False
    no_context_files: bool = False
    system_prompt: Optional[str] = None
    append_system_prompt: Optional[str] = None
    approve: Optional[bool] = None          # -a / -na
    mode: str = "interactive"               # interactive | print | json | rpc
    omni_plugin_dir: Optional[str] = None   # Omni's plugins/pi folder (extra extension root)
    agent_dir: Optional[str] = None
    profile: Optional[str] = None           # profiles/<name>.json; None -> "pi"
    no_mcp: bool = False                    # --no-mcp
    capabilities: Optional[List[str]] = None  # explicit capability list (overrides the profile and the user overlay)


class AgentSession:
    def __init__(self, opts: SessionOptions, ui: Any = None):
        self.opts = opts
        self.cwd = os.path.abspath(opts.cwd)
        self.agent_dir = Path(opts.agent_dir) if opts.agent_dir else res.agent_dir()
        self.agent_dir.mkdir(parents=True, exist_ok=True)
        self.ui = ui  # object implementing ui.* methods used by extensions (select/confirm/input/notify/...)
        from .caps import CapabilityManager
        self._no_extensions_flag = opts.no_extensions  # the user's own -ne, kept apart from what a profile decides
        self.base_profile: Dict[str, Any] = {}
        self.profile: Dict[str, Any] = {}
        self._resolve_profile(opts.profile)
        self.caps = CapabilityManager(self)
        if not self.profile.get("pi_extensions", True):
            opts.no_extensions = True
        if not self.profile.get("omni_plugins_pi", True):
            opts.omni_plugin_dir = None
        self.omni_tools: Dict[str, ToolDef] = {}
        self.gate = None
        self.listeners: List[Callable[[Dict[str, Any]], None]] = []
        self.trust_store = res.TrustStore(self.agent_dir)
        self.trusted = self._resolve_trust()
        self.settings = res.Settings(self.agent_dir, self.cwd, self.trusted)
        self.registry = ModelRegistry(self.agent_dir)
        self.auth = AuthStorage(self.agent_dir, self.registry)
        if opts.api_key and opts.provider:
            self.auth.set_runtime_api_key(opts.provider, opts.api_key)
        sdir = opts.session_dir or os.environ.get("PI_CODING_AGENT_SESSION_DIR") or self.settings.get("sessionDir")
        self.sessions = SessionManager(self.cwd, self.agent_dir, Path(sdir) if sdir else None, persist=not opts.no_session)
        self.diagnostics: List[str] = list(self.registry.errors)
        self.ext: Any = NullHost()
        self.builtin_tools: Dict[str, ToolDef] = create_all_tools(self.cwd, lambda: self.settings.data, self._bash_env)
        from .codemode import Codemode
        from .toolsearch import make_tool_search
        self.codemode = Codemode(self)
        self.builtin_tools["codemode"] = self.codemode.tool          # registered inactive, like Pi's built-in extensions
        self.builtin_tools["tool_search"] = make_tool_search(self)
        self.mcp: Any = None
        self._mcp_auto: set = set()
        self.active_tool_names: List[str] = []
        self.model: Optional[Dict[str, Any]] = None
        self.thinking_level: str = "off"
        self.scoped_models: List[Dict[str, Any]] = []
        self.signal: Optional[AbortSignal] = None
        self.is_streaming = False
        self._lock = threading.RLock()
        self.steer_queue: List[Dict[str, Any]] = []
        self.follow_queue: List[Dict[str, Any]] = []
        self.next_turn_messages: List[Dict[str, Any]] = []
        self.skills: List[res.Skill] = []
        self.templates: List[res.PromptTemplate] = []
        self.shadowed_skills: List[res.Skill] = []
        self.shadowed_templates: List[res.PromptTemplate] = []
        self.context_files: List[Dict[str, str]] = []
        self.forced_system_prompt: Optional[str] = None
        self.last_system_prompt = ""
        self._idle = threading.Event()
        self._idle.set()
        self.extra_prompt_guidelines: List[str] = []
        self._ready = threading.Event()   # set once extensions / plugin hosts are loaded
        self._ready.set()
        self.loading = False
        self._early_model: Optional[Dict[str, Any]] = None
        self.startup_seconds = 0.0

    # ================================================================================== startup
    def _resolve_trust(self) -> bool:
        if self.opts.approve is not None:
            return self.opts.approve
        if not res.project_has_resources(self.cwd):
            return True  # nothing trust-gated to load
        decision = self.trust_store.decision(self.cwd)
        if decision is not None:
            return decision
        default = res._read_json(self.agent_dir / "settings.json").get("defaultProjectTrust", "ask")
        if default == "always":
            return True
        if default == "never":
            return False
        if self.ui is not None and hasattr(self.ui, "confirm_trust"):
            ok = self.ui.confirm_trust(self.cwd)
            if ok is not None:
                return ok
        return False

    def start(self, defer_hosts: bool = False) -> None:
        """Load resources, extensions, model and session. Emits session_start.

        `defer_hosts=True` (the terminal UI) returns as soon as the session, model and built-in tools are usable -
        well under a second - and leaves the slow part (extension host, plugin registry, MCP, `session_start`
        handlers) to `finish_start()`, which the caller runs on a background thread. `prompt()` waits for it."""
        self._ready.clear()
        self.load_resources()
        if defer_hosts:
            self.caps.load()
            self._open_initial_session()
            self._select_initial_model()
            self._early_model = self.model
            self._init_tools()
            self.last_system_prompt = self.system_prompt()
            self.loading = True
            return
        try:
            if not self.opts.no_extensions:
                self._start_extensions()
            self._load_profile_capabilities()
            self._open_initial_session()
            self._select_initial_model()
            self._init_tools()
            self._start_mcp()
            self.last_system_prompt = self.system_prompt()
            self.emit("session_start", {"reason": "startup"})
            self.emit("resources_discover", {"reason": "startup"})
        finally:
            self._ready.set()

    def finish_start(self) -> None:
        """Second half of a deferred start. Safe to call from a worker thread."""
        t0 = time.time()
        try:
            if not self.opts.no_extensions:
                self._start_extensions()
            self._load_profile_capabilities()
            if self.model is self._early_model:  # extension providers may own the saved / default model
                self._select_initial_model()
            self._init_tools()
            self._start_mcp()
            self.last_system_prompt = self.system_prompt()
            self.emit("session_start", {"reason": "startup"})
            self.emit("resources_discover", {"reason": "startup"})
        except Exception as e:  # the window is already up: report, never crash it
            self.diagnostics.append(f"startup: {type(e).__name__}: {e}")
        finally:
            self.loading = False
            self.startup_seconds = time.time() - t0
            self._ready.set()
            self._notify({"type": "startup_complete", "seconds": self.startup_seconds})

    def wait_ready(self, timeout: Optional[float] = None) -> bool:
        return self._ready.wait(timeout)

    def _resolve_profile(self, name: Optional[str]) -> None:
        """Profile file (through `extends`) + the user's capability overlay; `--capabilities` replaces both."""
        from .caps import effective_profile
        from .profiles import load_profile
        self.base_profile = load_profile(name)
        explicit = self.opts.capabilities
        self.profile = effective_profile(self.base_profile, self.agent_dir, use_overlay=explicit is None)
        if explicit is not None:
            self.profile["capabilities"] = list(explicit)

    def _load_profile_capabilities(self) -> None:
        self.caps.load()
        reg = self.profile.get("omni_registry") or {}
        self.omni_tools = {}
        if reg.get("enabled") and not self.opts.no_tools:
            from .omni_bridge import load_registry_tools
            self.omni_tools = load_registry_tools(self.cwd, list(reg.get("sources") or []), self.diagnostics)
        self.gate = None
        if self.profile.get("permission_gate"):
            from .omni_bridge import PermissionGateMiddleware
            self.gate = PermissionGateMiddleware(self.cwd, self._ask_permission if getattr(self.ui, "has_ui", False) else None)

    def _ask_permission(self, title: str, reason: str) -> Optional[str]:
        options = ["Allow once", "Always allow (this session)", "Deny"]
        try:
            pick = self.ui.ext_select(title=f"{title}  -  {reason}", options=options)
        except Exception:
            return None
        return {"Allow once": "once", "Always allow (this session)": "always"}.get(pick or "", None)

    def load_resources(self) -> None:
        diag: List[str] = []
        from . import packages as pk
        temp = [e for e in self.opts.extensions if pk.is_package_spec(e)]
        try:
            self.package_resources = pk.collect(self.agent_dir, self.cwd, self.trusted, diag, temporary=temp)
        except Exception as e:  # never let a broken package stop the session
            diag.append(f"packages: {e}")
            self.package_resources = {k: [] for k in pk.RESOURCE_TYPES}
        pkg_skills = [str(p) for p in self.package_resources["skills"]]
        pkg_prompts = [str(p) for p in self.package_resources["prompts"]]
        try:
            from .tui.theme import load_custom_themes
            dirs = [self.agent_dir / "themes"] + ([Path(self.cwd) / ".pi" / "themes"] if self.trusted else [])
            load_custom_themes(dirs + list(self.package_resources["themes"]))
        except Exception as e:
            diag.append(f"themes: {e}")
        self.shadowed_skills: List[res.Skill] = []
        self.shadowed_templates: List[res.PromptTemplate] = []
        cap_roots = [str(p) for p in self.caps.skill_roots()]  # other harnesses' skill folders (skills-universal)
        self.skills = [] if self.opts.no_skills else res.discover_skills(self.cwd, self.agent_dir, self.trusted,
                                                                         list(self.settings.get("skills") or []) + pkg_skills + self.opts.skills + cap_roots, diag,
                                                                         self.shadowed_skills)
        self.skills = self.caps.filter_skills(self.skills, diag)
        if self.opts.no_skills and self.opts.skills:
            self.skills = res.discover_skills(self.cwd, Path("/nonexistent"), False, self.opts.skills, diag)
        self.templates = [] if self.opts.no_prompt_templates else res.discover_templates(
            self.cwd, self.agent_dir, self.trusted, list(self.settings.get("prompts") or []) + pkg_prompts + self.opts.prompt_templates,
            self.shadowed_templates)
        self.context_files = [] if self.opts.no_context_files else res.load_context_files(self.cwd, self.agent_dir)
        self.diagnostics.extend(diag)

    def _start_extensions(self) -> None:
        plugin_dir = Path(self.opts.omni_plugin_dir) if self.opts.omni_plugin_dir else None
        from .packages import is_package_spec
        extra = [e for e in self.opts.extensions if not is_package_spec(e)]
        extra += [str(p) for p in getattr(self, "package_resources", {}).get("extensions", [])]
        paths = res.discover_extensions(self.cwd, self.agent_dir, self.trusted, self.settings, extra, plugin_dir)
        if not paths:
            return
        try:
            from .extensions.bridge import ExtensionHost
            host = ExtensionHost(self)
            host.load([str(p) for p in paths])
            self.ext = host
        except Exception as e:  # Node missing etc. - Pi profile still works without extensions
            self.diagnostics.append(f"extensions disabled: {e}")

    def _open_initial_session(self) -> None:
        o = self.opts
        if o.session:
            path = o.session
            if not os.path.exists(path):
                match = [s for s in SessionManager.list_all_sessions(self.agent_dir) if (s["id"] or "").startswith(path)]
                if not match:
                    raise SystemExit(f"Session not found: {o.session}")
                path = match[0]["path"]
            self.sessions.open(path)
        elif o.continue_recent:
            recent = SessionManager.list_sessions(self.sessions.session_dir)
            if recent:
                self.sessions.open(recent[0]["path"])
            else:
                self.sessions.new_session()
        else:
            self.sessions.new_session()
        if o.session_name:
            self.sessions.append_session_info(o.session_name)

    def _select_initial_model(self) -> None:
        ctx = self.sessions.build_context()
        m = None
        if self.opts.model:
            m = self.registry.resolve(self.opts.model, self.opts.provider)
            if not m:
                self.diagnostics.append(f"Unknown model '{self.opts.model}'")
        if not m and ctx.get("model") and ctx["model"].get("provider"):
            m = self.registry.find(ctx["model"]["provider"], ctx["model"]["modelId"]) or self.registry.resolve(
                f"{ctx['model']['provider']}/{ctx['model']['modelId']}")
        if not m and self.settings.get("defaultModel"):
            m = self.registry.resolve(self.settings.get("defaultModel"), self.settings.get("defaultProvider"))
        if not m:
            m = self._first_authenticated_model()
        self.model = m
        level = (m or {}).get("_thinking") or self.opts.thinking or ctx.get("thinkingLevel") or self.settings.get("defaultThinkingLevel", "medium")
        self.thinking_level = clamp_thinking(m, level)
        patterns = self.settings.get("enabledModels") or []
        self.scoped_models = [x for x in (self.registry.resolve(p) for p in patterns) if x] if patterns else []

    def _first_authenticated_model(self) -> Optional[Dict[str, Any]]:
        try:
            from ..paths import PROJECT_ROOT  # omni default model, if configured
            cfg = json.loads((PROJECT_ROOT / "harness.config.json").read_text(encoding="utf-8"))
            if cfg.get("default_model"):
                m = self.registry.resolve(cfg["default_model"])
                if m and self.auth.has_auth(m["provider"]):
                    return m
        except Exception:
            pass
        for prov in ("anthropic", "openai", "google", "google-antigravity", "openrouter", "deepseek", "groq", "xai", "mistral"):
            if self.auth.has_auth(prov):
                cands = [m for m in self.registry.all() if m["provider"] == prov]
                if cands:
                    return cands[0]
        return None

    def _start_mcp(self) -> None:
        if self.opts.no_mcp or self.opts.no_tools or self.profile.get("mcp") is False:
            return
        from .mcp import McpManager
        self.mcp = McpManager(self.agent_dir, self.cwd, self.trusted, on_change=self._on_mcp_change)
        for name, cfg in getattr(self.ext, "mcp_servers", {}).items():
            self.mcp.registered[name] = {**cfg, "_source": "extension", "_scope": "extension"}
        self.mcp.load()
        self.diagnostics.extend(self.mcp.errors)
        if not self.mcp.servers:
            return
        self.mcp.start()
        self.mcp.wait_direct(10.0)

    def _on_mcp_change(self) -> None:
        """Activate codemode / tool_search / direct tools as servers connect (Pi docs/mcp.md "Control tool exposure")."""
        if self.mcp is None:
            return
        auto_cm = self.mcp.options.get("autoEnableCodemode", True) is not False
        names = list(self.active_tool_names)
        for name, (td, exp, _ns) in self.mcp.tool_defs().items():
            if exp == "direct" and name not in names and name not in self._mcp_auto:
                names.append(name)
                self._mcp_auto.add(name)
            elif exp == "codemode" and auto_cm and "codemode" not in names and "codemode" not in self._mcp_auto:
                names.append("codemode")
                self._mcp_auto.add("codemode")
            elif exp == "deferred" and "tool_search" not in names and "tool_search" not in self._mcp_auto:
                names.append("tool_search")
                self._mcp_auto.add("tool_search")
        self.active_tool_names = names
        self._notify({"type": "mcp_status"})

    def _init_tools(self) -> None:
        o = self.opts
        if o.no_tools:
            names: List[str] = []
        elif o.no_builtin_tools:
            names = []
        else:
            profile_tools = self.profile.get("tools") if self.profile.get("name") != "core" else None
            names = resolve_tool_list(o.tools or profile_tools or self.settings.get("defaultTools"), DEFAULT_TOOLS)
            names = [n for n in names if n not in o.exclude_tools]
        if not o.no_tools:
            for name in self.caps.tools:
                if name not in names and name not in o.exclude_tools:
                    names.append(name)
            for name in self.omni_tools:
                if name not in names and name not in o.exclude_tools:
                    names.append(name)
            for name, t in self.ext.tools.items():  # extension tools are active by default unless defaultActive false
                if t.__dict__.get("default_active", True) and name not in names and name not in o.exclude_tools:
                    names.append(name)
        for e in reversed(self.sessions.get_branch()):  # tools loaded by tool_search stay declared on this branch
            if e.get("type") == "custom" and e.get("customType") == "active-tools":
                for n in (e.get("data") or {}).get("tools") or []:
                    if n not in names:
                        names.append(n)
                break
        self.active_tool_names = names

    # ================================================================================== events
    def subscribe(self, fn: Callable[[Dict[str, Any]], None]) -> Callable[[], None]:
        self.listeners.append(fn)
        return lambda: self.listeners.remove(fn) if fn in self.listeners else None

    def _notify(self, ev: Dict[str, Any]) -> None:
        for fn in list(self.listeners):
            try:
                fn(ev)
            except Exception:
                traceback.print_exc()

    def emit(self, name: str, payload: Optional[Dict[str, Any]] = None, to_ui: bool = True) -> Any:
        ev = {"type": name, **(payload or {})}
        if to_ui:
            self._notify(ev)
        try:
            return self.ext.emit(name, ev)
        except Exception as e:
            self._notify({"type": "extension_error", "event": name, "error": str(e)})
            return None

    # ================================================================================== tools
    def all_tools(self) -> Dict[str, ToolDef]:
        tools = dict(self.builtin_tools)
        tools.update(self.caps.tools)
        tools.update(self.omni_tools)
        if self.mcp is not None:
            tools.update({n: v[0] for n, v in self.mcp.tool_defs().items()})
        tools.update(self.ext.tools)  # extensions may override built-ins (Pi: "Overriding Built-in Tools")
        return tools

    def exposure_of(self, tool: ToolDef) -> str:
        return tool.__dict__.get("exposure") or "direct"

    def indirect_tools(self) -> Dict[str, Any]:
        """Tools reachable only through codemode / tool_search: name -> (ToolDef, exposure, namespace)."""
        out: Dict[str, Any] = {}
        if self.mcp is not None:
            out.update(self.mcp.tool_defs())
        for n, t in self.ext.tools.items():
            exp = self.exposure_of(t)
            if exp in ("codemode", "deferred"):
                out[n] = (t, exp, t.__dict__.get("namespace"))
        return out

    def codemode_callable(self) -> Dict[str, Any]:
        """What scripts may call: active direct tools plus every codemode/deferred tool (never codemode/tool_search)."""
        out: Dict[str, Any] = {}
        for t in self.active_tools():
            if t.name in ("codemode", "tool_search") or self.exposure_of(t) == "model-only":
                continue
            out[t.name] = (t, "direct", t.__dict__.get("namespace"))
        for n, v in self.indirect_tools().items():
            if n in out and n in self.active_tool_names and v[1] == "deferred":
                continue  # loaded by tool_search: callable either way
            out.setdefault(n, v)
        return out

    def codemode_store(self) -> Dict[str, Any]:
        store: Dict[str, Any] = {}
        for e in self.sessions.get_branch():
            if e.get("type") == "custom" and e.get("customType") == "codemode-store":
                d = e.get("data") or {}
                store.update(d.get("set") or {})
                for k in d.get("delete") or []:
                    store.pop(k, None)
        return store

    def active_tools(self) -> List[ToolDef]:
        allt = self.all_tools()
        return [allt[n] for n in self.active_tool_names if n in allt]

    def set_active_tools(self, names: List[str], record: bool = False) -> None:
        allt = self.all_tools()
        self.active_tool_names = [n for n in dict.fromkeys(names) if n in allt]
        if record:
            self.sessions.append_custom("active-tools", {"tools": list(self.active_tool_names)})

    def _bash_env(self) -> Dict[str, str]:
        m = self.model or {}
        return {"PI_SESSION_ID": self.sessions.session_id, "PI_SESSION_FILE": str(self.sessions.file or ""),
                "PI_PROVIDER": m.get("provider", ""), "PI_MODEL": m.get("id", ""), "PI_THINKING_LEVEL": self.thinking_level,
                "PI_CWD": self.cwd, "PI_CODING_AGENT": "true"}

    # ================================================================================== prompt
    def prompt_options(self) -> PromptOptions:
        custom = self.opts.system_prompt
        if custom and os.path.isfile(custom):
            custom = Path(custom).read_text(encoding="utf-8")
        if not custom:
            custom = res.load_system_md(self.cwd, self.agent_dir, self.trusted, "SYSTEM.md")
        append = self.opts.append_system_prompt
        if append and os.path.isfile(append):
            append = Path(append).read_text(encoding="utf-8")
        if not append:
            append = res.load_system_md(self.cwd, self.agent_dir, self.trusted, "APPEND_SYSTEM.md") or ""
        if self.profile.get("prompt_append"):
            append = (append + "\n\n" if append else "") + self.profile["prompt_append"]
        if self.mcp is not None:
            sec = self.mcp.prompt_section()
            if sec:
                append = (append + "\n\n" if append else "") + sec
        sec = self.caps.prompt_section()
        if sec:
            append = (append + "\n\n" if append else "") + sec
        skills = self.skills
        if self.profile.get("skills_budget_chars"):  # keep a large shared skill library inside a context budget
            from .caps.skills import fit_skills
            skills = fit_skills(skills, int(self.profile["skills_budget_chars"]))
        tools = self.active_tools()
        return PromptOptions(
            cwd=self.cwd, custom_prompt=custom, selected_tools=[t.name for t in tools],
            tool_snippets={t.name: t.prompt_snippet for t in tools if t.prompt_snippet},
            tool_guidelines={t.name: list(t.prompt_guidelines) for t in tools if t.prompt_guidelines},
            prompt_guidelines=list(self.extra_prompt_guidelines), append_system_prompt=append,
            context_files=self.context_files, skills=skills)

    def system_prompt(self) -> str:
        if self.forced_system_prompt is not None:
            return self.forced_system_prompt
        return render(build_sections(self.prompt_options()))

    # ================================================================================== input
    def expand_input(self, text: str) -> str:
        if text.startswith("/skill:"):
            return res.expand_skill_command(text, self.skills)
        if text.startswith("/"):
            name, _, args = text[1:].partition(" ")
            t = next((t for t in self.templates if t.name == name), None)
            if t:
                return res.expand_template(t, args)
        return text

    def prompt(self, text: str, images: Optional[List[Dict[str, Any]]] = None, source: str = "interactive") -> None:
        """Blocking: run one prompt to completion. Call from a worker thread in interactive mode."""
        self._ready.wait()  # a message typed while extensions are still loading runs as soon as they are in
        if text.startswith("/"):
            # `/name` or `/<source-prefix>:name`; a duplicate name uses the remembered source, else Pi's order
            from .command_sources import CommandSources
            cs = CommandSources(self, include_builtins=False)
            hit = cs.resolve(text, kinds={"extension", "prompt", "skill"})
            if hit.entry is not None:
                if hit.entry.kind == "extension":
                    self.ext.run_command(hit.entry.name, hit.args, (hit.entry.ref or {}).get("id"))
                    return
                text = cs.prompt_text(hit.entry, hit.args, unique=len(hit.candidates) == 1 and not hit.explicit)
        r = self.emit("input", {"text": text, "images": images or [], "source": source}, to_ui=False)
        if isinstance(r, dict):
            if r.get("action") == "handled":
                return
            if r.get("action") == "transform":
                text = r.get("text", text)
                images = r.get("images", images)
        text = self.expand_input(text)
        extra, blocked = self.caps.on_prompt(text)  # prompt hooks: Hermes pre_llm_call, shell UserPromptSubmit
        if blocked:
            self._notify({"type": "notice", "level": "warning", "message": f"Message blocked by a hook: {blocked}"})
            return
        if extra:
            text = f"{text}\n\n{extra}"
        content: Any = text if not images else [{"type": "text", "text": text}] + list(images)
        user_msg = {"role": "user", "content": content, "timestamp": now_ms()}
        self.run([user_msg])

    def steer(self, text: str) -> None:
        with self._lock:
            self.steer_queue.append({"role": "user", "content": self.expand_input(text), "timestamp": now_ms()})
        self._notify({"type": "queue_update", "steering": len(self.steer_queue), "followUp": len(self.follow_queue)})

    def follow_up(self, text: str) -> None:
        with self._lock:
            self.follow_queue.append({"role": "user", "content": self.expand_input(text), "timestamp": now_ms()})
        self._notify({"type": "queue_update", "steering": len(self.steer_queue), "followUp": len(self.follow_queue)})

    def dequeue_all(self) -> List[str]:
        with self._lock:
            items = [text_of(m["content"]) for m in self.steer_queue + self.follow_queue]
            self.steer_queue, self.follow_queue = [], []
        self._notify({"type": "queue_update", "steering": 0, "followUp": 0})
        return items

    def send_custom_message(self, message: Dict[str, Any], options: Dict[str, Any]) -> None:
        """pi.sendMessage: custom message that participates in context; optionally triggers a turn."""
        m = {"role": "custom", "customType": message.get("customType", "extension"), "content": message.get("content", ""),
             "display": message.get("display", True), "details": message.get("details"), "timestamp": now_ms()}
        deliver = options.get("deliverAs") or "steer"
        if self.is_streaming:
            with self._lock:
                (self.follow_queue if deliver == "followUp" else self.next_turn_messages if deliver == "nextTurn" else self.steer_queue).append(m)
            return
        if options.get("triggerTurn"):
            threading.Thread(target=self.run, args=([m],), daemon=True).start()
        else:
            self._append_message(m)

    def send_user_message(self, content: Any, options: Dict[str, Any]) -> None:
        text = content if isinstance(content, str) else text_of(content)
        if self.is_streaming:
            (self.follow_up if options.get("deliverAs") == "followUp" else self.steer)(text)
            return
        threading.Thread(target=self.prompt, args=(text, None, "extension"), daemon=True).start()

    def abort(self) -> None:
        if self.signal is not None:
            self.signal.abort()

    def wait_for_idle(self, timeout: Optional[float] = None) -> bool:
        return self._idle.wait(timeout)

    def _take(self, queue: List[Dict[str, Any]], mode: str) -> List[Dict[str, Any]]:
        with self._lock:
            if not queue:
                return []
            if mode == "all":
                items = list(queue)
                queue.clear()
                return items
            return [queue.pop(0)]

    # ================================================================================== the loop
    def run(self, new_messages: List[Dict[str, Any]]) -> None:
        with self._lock:
            if self.is_streaming:
                for m in new_messages:
                    self.steer_queue.append(m)
                return
            self.is_streaming = True
            self._idle.clear()
            self.signal = AbortSignal()
        outcome = "completed"
        try:
            r = self.emit("before_agent_start", {"prompt": text_of(new_messages[0]["content"]) if new_messages else "",
                                                 "systemPrompt": self.system_prompt(),
                                                 "systemPromptOptions": self.prompt_options().to_json()}, to_ui=False)
            extra: List[Dict[str, Any]] = []
            if isinstance(r, dict):
                if r.get("systemPrompt"):
                    self.forced_system_prompt = r["systemPrompt"]
                for cm in r.get("messages") or ([r["message"]] if r.get("message") else []):
                    extra.append({"role": "custom", "customType": cm.get("customType", "extension"), "content": cm.get("content", ""),
                                  "display": cm.get("display", True), "details": cm.get("details"), "timestamp": now_ms()})
            for m in new_messages + extra + self.next_turn_messages:
                self._append_message(m)
            self.next_turn_messages = []
            self._request_reason = "user" if any(m.get("role") == "user" for m in new_messages) else "continuation"
            while True:
                self.emit("agent_start", {})
                outcome = self._run_turns()
                self.emit("agent_end", {"messages": [], "outcome": outcome})
                if outcome != "completed":
                    break
                if self._should_compact():
                    self.compact(reason="threshold")
                follow = self._take(self.follow_queue, self.settings.get("followUpMode", "one-at-a-time"))
                if not follow:
                    break
                for m in follow:
                    self._append_message(m)
        except Exception as e:  # never leave the UI stuck in "working"
            self._notify({"type": "error", "message": f"{type(e).__name__}: {e}"})
            outcome = "error"
        finally:
            self.forced_system_prompt = None
            with self._lock:
                self.is_streaming = False
                self.signal = None
            self._idle.set()
            self.emit("agent_settled", {"outcome": outcome})

    def _append_message(self, m: Dict[str, Any]) -> Dict[str, Any]:
        self.emit("message_start", {"message": m})
        r = self.emit("message_end", {"message": m})
        if isinstance(r, dict) and isinstance(r.get("message"), dict):
            m = r["message"]
        return self.sessions.append_message(m)

    def _run_turns(self) -> str:
        assert self.signal is not None
        turn = 0
        while True:
            if self.signal.aborted:
                return "aborted"
            turn += 1
            self.emit("turn_start", {"turnIndex": turn, "timestamp": now_ms()})
            msg = self._request_assistant()
            if msg is None:
                return "aborted" if self.signal.aborted else "error"
            calls = [b for b in msg["content"] if b.get("type") == "toolCall"]
            results: List[Dict[str, Any]] = []
            if msg["stopReason"] in ("error", "aborted"):
                self.emit("turn_end", {"turnIndex": turn, "message": msg, "toolResults": []})
                return "aborted" if msg["stopReason"] == "aborted" else "error"
            done_ids: set = set()
            for batch in self._parallel_batches(calls):
                if self.signal.aborted:
                    for call in batch:
                        tr = self._tool_result_msg(call, "Operation aborted", True)
                        self._append_message(tr)
                        results.append(tr)
                        done_ids.add(call["id"])
                    continue
                if len(batch) == 1:
                    msgs = [self._run_tool(batch[0])]
                else:
                    from concurrent.futures import ThreadPoolExecutor
                    with ThreadPoolExecutor(max_workers=min(8, len(batch)), thread_name_prefix="pi-tool") as pool:
                        msgs = list(pool.map(self._run_tool, batch))
                for tr in msgs:  # results keep the order of the calls
                    self._append_message(tr)
                    results.append(tr)
                done_ids.update(c["id"] for c in batch)
                steer = self._take(self.steer_queue, self.settings.get("steeringMode", "one-at-a-time"))
                if steer:
                    for rest in calls:
                        if rest["id"] not in done_ids:
                            skipped = self._tool_result_msg(rest, "Skipped due to queued user message.", True)
                            self._append_message(skipped)
                    for m in steer:
                        self._append_message(m)
                    self._request_reason = "user"
                    break
            r = self.emit("turn_end", {"turnIndex": turn, "message": msg, "toolResults": results})
            if isinstance(r, dict):
                for d in r.get("entries") or []:
                    self._apply_boundary_entry(d)
            if self.signal.aborted:
                return "aborted"
            if calls:
                if self._should_compact():
                    self.compact(reason="threshold")
                continue
            steer = self._take(self.steer_queue, self.settings.get("steeringMode", "one-at-a-time"))
            if steer:
                for m in steer:
                    self._append_message(m)
                self._request_reason = "user"
                continue
            if isinstance(r, dict) and r.get("continue"):
                continue
            return "completed"

    def _apply_boundary_entry(self, d: Dict[str, Any]) -> None:
        t = d.get("type")
        if t == "custom":
            self.sessions.append_custom(d.get("customType", "extension"), d.get("data"))
        elif t == "custom_message":
            self.sessions.append_custom_message(d.get("customType", "extension"), d.get("content", ""), d.get("display", True), d.get("details"))

    def llm_context(self) -> Dict[str, Any]:
        ctx = self.sessions.build_context()
        messages = ctx["messages"]
        r = self.emit("context", {"messages": messages}, to_ui=False)
        if isinstance(r, dict) and isinstance(r.get("messages"), list):
            messages = r["messages"]
        llm = convert_to_llm(messages)
        if self.settings.get("images.blockImages", False):
            llm = block_images(llm)
        return {"systemPrompt": self.system_prompt(), "messages": llm, "tools": self.tool_declarations()}

    def tool_declarations(self) -> List[Dict[str, Any]]:
        """Declarations sent to the model. With codemode active its description lists the callable tools, and
        `codemode.mode` decides whether declared tools also say how scripts call them (on) or are hidden (only)."""
        tools = self.active_tools()
        names = {t.name for t in tools}
        if "codemode" not in names or self.all_tools().get("codemode") is not self.codemode.tool:
            return [t.declaration() for t in tools]
        from .codemode import describe_script_call
        self.codemode.refresh_description()
        mode = self.codemode.mode()
        callable_ = self.codemode.callable()
        out = []
        for t in tools:
            d = t.declaration()
            if t.name in callable_ and t.name != "codemode":
                if mode == "only" and callable_[t.name][1] == "direct" and t.name not in self._loaded_by_search():
                    continue
                if mode == "on":
                    d = dict(d, description=describe_script_call(t))
            out.append(d)
        return out

    def _loaded_by_search(self) -> set:
        ind = self.indirect_tools()
        return {n for n in self.active_tool_names if n in ind}

    def _request_assistant(self) -> Optional[Dict[str, Any]]:
        assert self.signal is not None
        if not self.model:
            err = self._error_message("No model selected. Use /model or /login to configure a provider.")
            self._append_message(err)
            return err
        context = self.llm_context()
        self.last_system_prompt = context["systemPrompt"]
        reason = getattr(self, "_request_reason", "continuation")
        self._request_reason = "continuation"
        routed = self._route(context, reason)
        if isinstance(routed, str):
            err = self._error_message(routed)
            self._append_message(err)
            return err
        model, level = routed
        r = self.emit("before_provider_request", {"payload": context, "model": model}, to_ui=False)
        if isinstance(r, dict) and isinstance(r.get("payload"), dict) and "messages" in r["payload"]:
            context = r["payload"]
        retry = self.settings.get("retry") or {}
        max_retries = int(retry.get("maxRetries", 3)) if retry.get("enabled", True) else 0
        attempt = 0
        while True:
            api_key = self.auth.get_api_key(model["provider"])
            if not api_key:
                err = self._error_message(f"No API key found for {model['provider']}. Use /login or set the provider's API key.")
                self._append_message(err)
                return err
            partial = {"role": "assistant", "content": [], "provider": model["provider"], "model": model["id"],
                       "api": model.get("api"), "usage": empty_usage(), "stopReason": "stop", "timestamp": now_ms()}
            started = False
            try:
                options = {"apiKey": api_key, "reasoning": level if model.get("reasoning") else None,
                           "signal": self.signal, "onResponse": self._on_provider_response, "headers": model.get("headers")}
                if model.get("api") == "google-antigravity":  # the Cloud Code project found at sign-in
                    options["projectId"] = self.auth.credential_extra(model["provider"]).get("projectId")
                elif model.get("api") in ("openai-codex-responses", "github-copilot"):  # ChatGPT workspace / GHE host
                    extra = self.auth.credential_extra(model["provider"])
                    options.update({k: extra[k] for k in ("accountId", "enterpriseUrl") if extra.get(k)})
                for ev in ai_stream(model, context, options):
                    if ev["type"] == "start":
                        self.emit("message_start", {"message": partial})
                        started = True
                    elif ev["type"] == "done":
                        msg = ev["message"]
                        msg["thinkingLevel"] = level
                        r2 = self.emit("message_end", {"message": msg})
                        if isinstance(r2, dict) and isinstance(r2.get("message"), dict):
                            msg = r2["message"]
                        self.sessions.append_message(msg)
                        return msg
                    else:
                        if ev["type"] == "text_delta":
                            if not partial["content"] or partial["content"][-1]["type"] != "text":
                                partial["content"].append({"type": "text", "text": ""})
                            partial["content"][-1]["text"] += ev["delta"]
                        elif ev["type"] == "thinking_delta":
                            if not partial["content"] or partial["content"][-1]["type"] != "thinking":
                                partial["content"].append({"type": "thinking", "thinking": ""})
                            partial["content"][-1]["thinking"] += ev["delta"]
                        elif ev["type"] == "toolcall_end":
                            partial["content"].append(ev["toolCall"])
                        self.emit("message_update", {"message": partial, "assistantMessageEvent": ev})
                return None
            except AbortedError:
                partial["stopReason"] = "aborted"
                partial["errorMessage"] = "Request aborted"
                if not started:
                    self.emit("message_start", {"message": partial})
                self.emit("message_end", {"message": partial})
                self.sessions.append_message(partial)
                return partial
            except ProviderError as e:
                if e.status in (429, 401, 402, 403):
                    self.auth.block_keyring_key(model["provider"], 3600 if e.status == 402 else 60, f"http_{e.status}")
                if attempt < max_retries and (e.transient or e.status == 429) and not self.signal.aborted and not partial["content"]:
                    attempt += 1
                    delay = min(float(retry.get("baseDelayMs", 2000)) * (2 ** (attempt - 1)) / 1000,
                                float(retry.get("maxAgentDelayMs", 60000)) / 1000)
                    if e.retry_after:
                        delay = min(max(delay, e.retry_after), float(retry.get("maxAgentDelayMs", 60000)) / 1000)
                    self._notify({"type": "auto_retry_start", "attempt": attempt, "maxAttempts": max_retries,
                                  "delayMs": int(delay * 1000), "errorMessage": str(e)})
                    end = time.time() + delay
                    while time.time() < end and not self.signal.aborted:
                        time.sleep(0.1)
                    self._notify({"type": "auto_retry_end", "attempt": attempt})
                    if self.signal.aborted:
                        return None
                    if self.model and self.model.get("virtual"):
                        failed = {"model": model, "thinkingLevel": level, "message": dict(partial, stopReason="error", errorMessage=str(e))}
                        rr = self._route(context, "retry", failed)
                        if isinstance(rr, str):
                            err = self._error_message(rr)
                            self._append_message(err)
                            return err
                        model, level = rr
                    continue
                err = dict(partial, stopReason="error", errorMessage=str(e))
                if not started:
                    self.emit("message_start", {"message": err})
                self.emit("message_end", {"message": err})
                self.sessions.append_message(err)
                return err

    def _route(self, context: Dict[str, Any], reason: str, failed: Optional[Dict[str, Any]] = None):
        """Selected model -> (physical model, thinking level). Virtual models ask their extension router
        (Pi docs/virtual-models.md); router state is kept on the branch as `virtual-model-state` entries."""
        sel = self.model
        assert sel is not None
        if not sel.get("virtual"):
            return sel, self.thinking_level
        key = f"{sel['provider']}/{sel['id']}"
        previous, state = None, None
        for e in reversed(self.sessions.get_branch()):
            if previous is None and e.get("type") == "message" and e["message"].get("role") == "assistant" \
                    and e["message"].get("stopReason") not in ("error", "aborted"):
                m = e["message"]
                pm = self.registry.find(m.get("provider"), m.get("model")) or {"provider": m.get("provider"), "id": m.get("model")}
                previous = {"model": pm, "thinkingLevel": m.get("thinkingLevel")}
            if state is None and e.get("type") == "custom" and e.get("customType") == "virtual-model-state" \
                    and (e.get("data") or {}).get("model") == key:
                state = (e.get("data") or {}).get("state")
            if previous is not None and state is not None:
                break
        request: Dict[str, Any] = {"model": {k: v for k, v in sel.items() if not k.startswith("_")}, "thinkingLevel": self.thinking_level,
                                   "reason": reason, "previous": previous, "state": state if reason != "direct" else None,
                                   "messages": [{"role": "system", "content": context.get("systemPrompt", "")}] + list(context.get("messages") or [])}
        if failed:
            request["failed"] = failed
        if not hasattr(self.ext, "route_virtual"):
            return f"Virtual model {key} has no router (its extension is not loaded)."
        try:
            r = self.ext.route_virtual(sel["provider"], sel["id"], request)
        except Exception as e:
            return f"Virtual model {key} failed to route: {e}"
        rm = (r or {}).get("model") or {}
        phys = self.registry.find(rm.get("provider"), rm.get("id")) or self.registry.resolve(f"{rm.get('provider')}/{rm.get('id')}")
        if not phys or phys.get("virtual"):
            return f"Virtual model {key} routed to {rm.get('provider')}/{rm.get('id')}, which is not a physical model in the catalog."
        if not self.auth.has_auth(phys["provider"]):
            return f"Virtual model {key} routed to {phys['provider']}/{phys['id']}, but {phys['provider']} has no credentials."
        if r.get("hasState") and reason != "direct":
            self.sessions.append_custom("virtual-model-state", {"model": key, "state": r.get("state")})
        level = clamp_thinking(phys, r.get("thinkingLevel") or "medium")
        self._notify({"type": "model_routed", "model": phys, "thinkingLevel": level})
        self.routed = (phys, level)
        return phys, level

    def direct_model(self) -> Dict[str, Any]:
        """Physical model for requests outside the agent loop (compaction, summaries): a virtual selection is routed
        with reason `direct`, falling back to the physical model that answered last."""
        m = self.model or {}
        if not m.get("virtual"):
            return m
        r = self._route({"systemPrompt": "", "messages": []}, "direct")
        if not isinstance(r, str):
            return r[0]
        return getattr(self, "routed", (None,))[0] or m

    def _on_provider_response(self, status: int, headers: Dict[str, str]) -> None:
        self.emit("after_provider_response", {"status": status, "headers": headers}, to_ui=False)

    def _error_message(self, text: str) -> Dict[str, Any]:
        m = self.model or {}
        return {"role": "assistant", "content": [], "provider": m.get("provider"), "model": m.get("id"), "api": m.get("api"),
                "usage": empty_usage(), "stopReason": "error", "errorMessage": text, "timestamp": now_ms()}

    def _tool_result_msg(self, call: Dict[str, Any], text: str, is_error: bool, details: Any = None) -> Dict[str, Any]:
        return {"role": "toolResult", "toolCallId": call["id"], "toolName": call["name"],
                "content": [{"type": "text", "text": text}], "details": details, "isError": is_error, "timestamp": now_ms()}

    def _execute_tool(self, call: Dict[str, Any]) -> Dict[str, Any]:
        msg = self._run_tool(call)
        self._append_message(msg)
        return msg

    def _run_tool(self, call: Dict[str, Any], tools: Optional[Dict[str, ToolDef]] = None, nested: bool = False) -> Dict[str, Any]:
        """The tool pipeline: lookup, argument validation, permission gate, tool_call hooks, execute, tool_result hooks.
        Returns the toolResult message (not yet appended)."""
        assert self.signal is not None or nested
        tools = tools if tools is not None else {t.name: t for t in self.active_tools()}
        tool = tools.get(call["name"])
        args = call.get("arguments") or {}
        signal = self.signal
        self.emit("tool_execution_start", {"toolCallId": call["id"], "toolName": call["name"], "args": args})
        result: Dict[str, Any]
        if tool is None:
            result = {"content": [{"type": "text", "text": f"Tool {call['name']} not found"}], "isError": True, "details": None}
        else:
            if tool.prepare_arguments:
                args = tool.prepare_arguments(args)
            missing = [k for k in (tool.parameters.get("required") or []) if k not in (args or {})]
            if missing:
                result = {"content": [{"type": "text", "text": "Validation failed for tool \"%s\":\n%s" % (
                    call["name"], "\n".join(f"- {k}: missing" for k in missing))}], "isError": True, "details": None}
            else:
                r = None
                why = self.caps.gate(call["name"], args)  # capability gates (e.g. plan mode is read-only)
                if why:
                    r = {"block": True, "reason": why}
                if r is None and self.gate is not None:
                    ok, why = self.gate.check(call["name"], args)
                    if not ok:
                        r = {"block": True, "reason": why}
                if r is None:
                    r = self.emit("tool_call", {"toolCallId": call["id"], "toolName": call["name"], "input": args}, to_ui=False)
                if isinstance(r, dict) and isinstance(r.get("input"), dict):
                    args = r["input"]
                if isinstance(r, dict) and r.get("block"):
                    result = {"content": [{"type": "text", "text": r.get("reason") or "Tool call blocked by extension"}],
                              "isError": True, "details": None, "blocked": True}
                else:
                    def on_update(partial: Dict[str, Any]) -> None:
                        self.emit("tool_execution_update", {"toolCallId": call["id"], "toolName": call["name"], "args": args,
                                                            "partialResult": partial})
                    ctx = ToolContext(cwd=self.cwd, signal=signal, model=self.model, session=self)
                    try:
                        if nested and call.get("_raw") and tool.__dict__.get("raw_execute"):
                            raw = tool.__dict__["raw_execute"](args, signal)
                            result = self.mcp.result_for_model(raw, tool.name) if self.mcp else {"content": []}
                            result["raw"] = raw
                        else:
                            result = tool.execute(call["id"], args, signal, on_update, ctx)
                        result.setdefault("details", None)
                        if tool.name != "read" and any(b.get("type") == "image" for b in result.get("content") or []):
                            from .images import normalize_tool_result_images
                            result["content"] = normalize_tool_result_images(
                                result["content"], self.settings.get("images.autoResize", True) is not False)
                    except ToolError as e:
                        result = {"content": [{"type": "text", "text": str(e)}], "isError": True, "details": None}
                    except Exception as e:
                        result = {"content": [{"type": "text", "text": f"{type(e).__name__}: {e}"}], "isError": True, "details": None}
                    result = self.caps.after_tool(call["name"], args, result)
                    r2 = self.emit("tool_result", {"toolCallId": call["id"], "toolName": call["name"], "input": args,
                                                   "content": result.get("content"), "details": result.get("details"),
                                                   "isError": bool(result.get("isError"))}, to_ui=False)
                    if isinstance(r2, dict):
                        for k in ("content", "details", "isError"):
                            if k in r2 and r2[k] is not None:
                                result[k] = r2[k]
        msg = {"role": "toolResult", "toolCallId": call["id"], "toolName": call["name"], "content": result.get("content") or [],
               "details": result.get("details"), "isError": bool(result.get("isError")), "timestamp": now_ms()}
        self.emit("tool_execution_end", {"toolCallId": call["id"], "toolName": call["name"], "args": args, "result": result,
                                         "isError": msg["isError"]})
        if nested:
            msg["_raw"] = result.get("raw")
            msg["_blocked"] = bool(result.get("blocked"))
        return msg

    def execute_nested(self, call_id: str, name: str, args: Dict[str, Any], signal=None, script_value: bool = True):
        """A codemode script's tool call. Returns (ok, value for the script, error text)."""
        callable_ = self.codemode_callable()
        tools = {n: v[0] for n, v in callable_.items()}
        if name not in tools:
            return False, None, f"Tool {name} not found"
        tool = tools[name]
        msg = self._run_tool({"id": call_id, "name": name, "arguments": args, "_raw": True}, tools, nested=True)
        text = "\n".join(b.get("text", "") for b in msg["content"] if b.get("type") == "text")
        if tool.__dict__.get("mcp"):
            raw = msg.get("_raw")
            if raw is not None and not msg.get("_blocked"):
                return True, raw, None
            return False, None, text or "MCP tool call failed"
        if name == "bash":
            d = msg.get("details") or {}
            if msg["isError"] and not d.get("exitCode"):
                return False, None, text
            full = d.get("fullOutputPath")
            output = text
            if full and os.path.exists(full):
                try:
                    data = Path(full).read_text(encoding="utf-8", errors="replace")
                    lim = 1024 * 1024
                    output = data if len(data) <= lim else data[:lim // 2] + "\n...[output omitted]...\n" + data[-lim // 2:]
                except OSError:
                    pass
            v = {"output": output, "truncated": bool(d.get("truncation")) or bool(full), "exit_code": d.get("exitCode"),
                 "wall_time_seconds": d.get("wallTimeSeconds", 0)}
            if full:
                v["full_output_path"] = full
            return True, v, None
        if msg["isError"]:
            return False, None, text or "Tool call failed"
        if name == "read":
            imgs = [b for b in msg["content"] if b.get("type") == "image"]
            if imgs:
                return True, {"type": "image", "data": imgs[0]["data"], "mimeType": imgs[0]["mimeType"], "note": text}, None
        if tool.__dict__.get("output_schema") and isinstance(msg.get("details"), dict) and "structuredContent" in msg["details"]:
            return True, msg["details"]["structuredContent"], None
        return True, text, None

    def _parallel_batches(self, calls: List[Dict[str, Any]]) -> List[List[Dict[str, Any]]]:
        """Consecutive read-only calls form one batch that runs concurrently; any other call runs alone."""
        if not self.settings.get("parallelReadOnlyTools", True):
            return [[c] for c in calls]
        tools = {t.name: t for t in self.active_tools()}
        out: List[List[Dict[str, Any]]] = []
        for c in calls:
            t = tools.get(c["name"])
            ro = bool(t and t.read_only)
            if ro and out and out[-1][0].get("_ro"):
                out[-1].append(c)
            else:
                out.append([c])
            c["_ro"] = ro
        for b in out:
            for c in b:
                c.pop("_ro", None)
        return out

    # ================================================================================== context usage
    def context_usage(self) -> Dict[str, Any]:
        window = int((self.model or {}).get("contextWindow") or 0)
        ctx = self.sessions.build_context()
        tokens = None
        msgs = ctx["messages"]
        last_idx = None
        for i in range(len(msgs) - 1, -1, -1):
            m = msgs[i]
            if m.get("role") == "assistant" and m.get("usage") and m.get("stopReason") not in ("error", "aborted"):
                u = m["usage"]
                tokens = u.get("input", 0) + u.get("cacheRead", 0) + u.get("cacheWrite", 0) + u.get("output", 0)
                last_idx = i
                break
        trailing = msgs[last_idx + 1:] if last_idx is not None else msgs
        est = (tokens or estimate_tokens(self.system_prompt())) + sum(estimate_tokens(m) for m in trailing)
        return {"tokens": est, "contextWindow": window, "percent": (est / window * 100) if window else None}

    def totals(self) -> Dict[str, Any]:
        t = {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0, "cost": 0.0, "messages": 0, "toolCalls": 0}
        for e in self.sessions.entries:
            if e.get("type") == "message":
                m = e["message"]
                t["messages"] += 1
                if m.get("role") == "assistant":
                    u = m.get("usage") or {}
                    for k in ("input", "output", "cacheRead", "cacheWrite"):
                        t[k] += u.get(k, 0) or 0
                    t["cost"] += (u.get("cost") or {}).get("total", 0) or 0
                    t["toolCalls"] += sum(1 for b in m.get("content") or [] if b.get("type") == "toolCall")
            elif e.get("type") in ("compaction", "branch_summary", "usage") and e.get("usage"):
                u = e["usage"]
                t["cost"] += (u.get("cost") or {}).get("total", 0) or 0
        return t

    # ================================================================================== compaction
    def _should_compact(self) -> bool:
        c = self.settings.get("compaction") or {}
        if not c.get("enabled", True) or not self.model:
            return False
        u = self.context_usage()
        return bool(u["contextWindow"]) and u["tokens"] > u["contextWindow"] - int(c.get("reserveTokens", 16384))

    def compact(self, custom_instructions: Optional[str] = None, reason: str = "manual") -> Optional[Dict[str, Any]]:
        ctx = self.sessions.build_context()
        entries = [e for e in ctx["entries"] if e["type"] != "compaction"]
        prev = next((e for e in ctx["entries"] if e["type"] == "compaction"), None)
        keep = int((self.settings.get("compaction") or {}).get("keepRecentTokens", 20000))
        acc, cut_idx = 0, None
        for i in range(len(entries) - 1, -1, -1):
            e = entries[i]
            acc += estimate_tokens(e)
            is_turn_start = e["type"] == "message" and e["message"].get("role") == "user"
            if acc >= keep and is_turn_start:
                cut_idx = i
                break
        if cut_idx is None:
            # keep the last user turn at minimum
            users = [i for i, e in enumerate(entries) if e["type"] == "message" and e["message"].get("role") == "user"]
            cut_idx = users[-1] if users else None
        if cut_idx is None or cut_idx == 0:
            self._notify({"type": "notice", "level": "info", "message": "Nothing to compact yet."})
            return None
        to_summarize = entries[:cut_idx]
        first_kept = entries[cut_idx]["id"]
        tokens_before = self.context_usage()["tokens"]
        r = self.emit("session_before_compact", {"preparation": {"firstKeptEntryId": first_kept, "tokensBefore": tokens_before,
                                                                 "reason": reason}, "customInstructions": custom_instructions},
                      to_ui=False)
        if isinstance(r, dict) and r.get("cancel"):
            return None
        self._notify({"type": "auto_compaction_start" if reason != "manual" else "compaction_start", "reason": reason})
        summary, usage, from_hook = None, None, False
        if isinstance(r, dict) and isinstance(r.get("compaction"), dict):
            summary = r["compaction"].get("summary")
            first_kept = r["compaction"].get("firstKeptEntryId", first_kept)
            from_hook = True
        if summary is None:
            msgs = convert_to_llm([m for e in to_summarize for m in SessionManager.entry_to_messages(e)])
            prompt = f"<conversation>\n{serialize_conversation(msgs)}\n</conversation>\n\n"
            if prev:
                prompt += f"<previous-summary>\n{prev.get('summary', '')}\n</previous-summary>\n\n"
            prompt += UPDATE_SUMMARIZATION_PROMPT if prev else SUMMARIZATION_PROMPT
            if custom_instructions:
                prompt += f"\n\nAdditional focus: {custom_instructions}"
            try:
                dm = self.direct_model()
                reply = ai_complete(dm, {"systemPrompt": SUMMARIZATION_SYSTEM_PROMPT,
                                         "messages": [{"role": "user", "content": prompt, "timestamp": now_ms()}], "tools": []},
                                    {"apiKey": self.auth.get_api_key(dm["provider"]), "maxTokens": 8192,
                                     "signal": self.signal})
            except Exception as e:
                self.emit("session_compact_failed", {"error": str(e)})
                self._notify({"type": "notice", "level": "error", "message": f"Compaction failed: {e}"})
                return None
            if reply.get("stopReason") in ("error", "length"):
                self.emit("session_compact_failed", {"error": reply.get("errorMessage") or "summary incomplete"})
                self._notify({"type": "notice", "level": "error", "message": f"Compaction failed: {reply.get('errorMessage') or 'token cap'}"})
                return None
            summary = text_of(reply["content"]).strip()
            usage = reply.get("usage")
        files_read, files_mod = set(), set()
        for e in to_summarize:
            m = e.get("message") or {}
            for b in m.get("content") or [] if isinstance(m.get("content"), list) else []:
                if b.get("type") == "toolCall":
                    p = (b.get("arguments") or {}).get("path")
                    if p:
                        (files_read if b["name"] == "read" else files_mod if b["name"] in ("write", "edit") else set()).add(p)
        entry = self.sessions.append_compaction(summary, first_kept, tokens_before,
                                                {"readFiles": sorted(files_read), "modifiedFiles": sorted(files_mod)}, from_hook, usage)
        self.emit("session_compact", {"compactionEntry": entry, "fromExtension": from_hook})
        self._notify({"type": "compaction_end", "entry": entry})
        return entry

    # ================================================================================== model / thinking
    def set_model(self, model: Dict[str, Any]) -> None:
        prev = self.model
        self.model = model
        new_level = clamp_thinking(model, self.thinking_level if self.thinking_level != "off" else self.settings.get("defaultThinkingLevel", "medium"))
        if new_level != self.thinking_level:
            self.set_thinking_level(new_level)
        self.sessions.append_model_change(model["provider"], model["id"])
        self.emit("model_select", {"model": model, "previousModel": prev, "source": "set"})

    def set_thinking_level(self, level: str) -> None:
        level = clamp_thinking(self.model, level)
        prev = self.thinking_level
        self.thinking_level = level
        self.sessions.append_thinking_level_change(level)
        self.emit("thinking_level_select", {"level": level, "previousLevel": prev})

    def cycle_thinking(self) -> str:
        if not self.model or not self.model.get("reasoning"):
            return self.thinking_level
        levels = THINKING_LEVELS
        self.set_thinking_level(levels[(levels.index(self.thinking_level) + 1) % len(levels)])
        return self.thinking_level

    def cycle_model(self, direction: int = 1) -> Optional[Dict[str, Any]]:
        pool = self.scoped_models or [m for m in self.registry.all() if self.auth.has_auth(m["provider"]) and m["provider"] != "faux"]
        if not pool:
            return None
        keys = [model_key(m) for m in pool]
        cur = model_key(self.model)
        idx = (keys.index(cur) + direction) % len(pool) if cur in keys else 0
        self.set_model(pool[idx])
        return pool[idx]

    # ================================================================================== session ops
    def _replace_session(self, reason: str, fn: Callable[[], None]) -> bool:
        before = {"new": "session_before_switch", "resume": "session_before_switch", "fork": "session_before_fork"}.get(reason)
        if before:
            r = self.emit(before, {"reason": reason}, to_ui=False)
            if isinstance(r, dict) and r.get("cancel"):
                return False
        self.emit("session_shutdown", {"reason": reason})
        fn()
        ctx = self.sessions.build_context()
        if ctx.get("model") and ctx["model"].get("provider"):
            m = self.registry.resolve(f"{ctx['model']['provider']}/{ctx['model']['modelId']}")
            if m:
                self.model = m
        if ctx.get("thinkingLevel"):
            self.thinking_level = clamp_thinking(self.model, ctx["thinkingLevel"])
        self.emit("session_start", {"reason": reason})
        self._notify({"type": "session_replaced", "reason": reason})
        return True

    def new_session(self) -> bool:
        return self._replace_session("new", lambda: self.sessions.new_session())

    def resume(self, path: str) -> bool:
        return self._replace_session("resume", lambda: self.sessions.open(path))

    def fork(self, entry_id: str, position: str = "before") -> bool:
        return self._replace_session("fork", lambda: self.sessions.fork_to_new_file(entry_id, position))

    def clone(self) -> bool:
        return self._replace_session("fork", lambda: self.sessions.clone())

    def navigate_tree(self, target_id: Optional[str], summarize: bool = False, label: Optional[str] = None) -> bool:
        r = self.emit("session_before_tree", {"targetId": target_id}, to_ui=False)
        if isinstance(r, dict) and r.get("cancel"):
            return False
        old_leaf = self.sessions.leaf_id
        target = self.sessions.get_entry(target_id) if target_id else None
        # selecting a user message puts its text back in the editor and continues from its parent (Pi behaviour)
        new_leaf = target.get("parentId") if target and target["type"] == "message" and target["message"].get("role") == "user" else target_id
        if summarize and old_leaf and self.model:
            abandoned = [e for e in self.sessions.get_branch(old_leaf) if e["id"] not in {x["id"] for x in self.sessions.get_branch(new_leaf)}]
            if abandoned:
                msgs = convert_to_llm([m for e in abandoned for m in SessionManager.entry_to_messages(e)])
                try:
                    dm = self.direct_model()
                    reply = ai_complete(dm, {"systemPrompt": SUMMARIZATION_SYSTEM_PROMPT, "messages": [{
                        "role": "user", "content": f"<conversation>\n{serialize_conversation(msgs)}\n</conversation>\n\nSummarize this "
                        f"abandoned branch briefly: goal, what was tried, results, and anything the next branch should know.",
                        "timestamp": now_ms()}], "tools": []}, {"apiKey": self.auth.get_api_key(dm["provider"]), "maxTokens": 4096})
                    summary = text_of(reply["content"]).strip()
                    if summary:
                        self.sessions.set_leaf(new_leaf)
                        self.sessions.append_branch_summary(new_leaf, old_leaf, summary)
                        new_leaf = self.sessions.leaf_id
                except Exception as e:
                    self._notify({"type": "notice", "level": "warning", "message": f"Branch summary failed: {e}"})
        self.sessions.set_leaf(new_leaf)
        if label and target_id:
            self.sessions.append_label(target_id, label)
        self.emit("session_tree", {"newLeafId": new_leaf, "oldLeafId": old_leaf})
        self._notify({"type": "session_replaced", "reason": "tree"})
        return True

    def set_session_name(self, name: str) -> None:
        self.sessions.append_session_info(name)
        self.emit("session_info_changed", {"name": name})

    # ================================================================================== user bash
    def user_bash(self, command: str, exclude_from_context: bool = False) -> Dict[str, Any]:
        r = self.emit("user_bash", {"command": command, "excludeFromContext": exclude_from_context, "cwd": self.cwd}, to_ui=False)
        if isinstance(r, dict) and isinstance(r.get("result"), dict):
            out = r["result"]
            result = {"output": out.get("output", ""), "exitCode": out.get("exitCode"), "cancelled": bool(out.get("cancelled")),
                      "truncated": bool(out.get("truncated")), "fullOutputPath": out.get("fullOutputPath")}
        else:
            sig = AbortSignal()
            self.signal = self.signal or sig
            self._notify({"type": "bash_start", "command": command})
            raw = run_shell(command, self.cwd, signal=sig, on_output=lambda t: self._notify({"type": "bash_output", "chunk": t}),
                            shell_path=self.settings.get("shellPath"), prefix=self.settings.get("shellCommandPrefix"), env=self._bash_env())
            tr = truncate_tail(raw["output"])
            result = {"output": tr["content"], "exitCode": raw["exitCode"], "cancelled": raw["cancelled"],
                      "truncated": raw["truncated"], "fullOutputPath": raw["fullOutputPath"]}
            if self.signal is sig:
                self.signal = None
        msg = {"role": "bashExecution", "command": command, **result, "timestamp": now_ms()}
        if exclude_from_context:
            msg["excludeFromContext"] = True
        if self.is_streaming:
            self.next_turn_messages.append(msg)
        else:
            self._append_message(msg)
        return msg

    # ================================================================================== shutdown
    def shutdown(self, reason: str = "quit") -> None:
        self.abort()
        self.emit("session_shutdown", {"reason": reason})
        self.caps.unload()
        try:
            self.ext.shutdown()
        except Exception:
            pass
        if self.mcp is not None:
            self.mcp.close()

    def switch_profile(self, name: str) -> None:
        """Apply another profile in place (tools, hosts, gate, prompt) and reload resources."""
        self._resolve_profile(name)
        self.opts.profile = self.profile["name"]
        self.opts.tools = None
        self.opts.no_extensions = not self.profile.get("pi_extensions", True)
        if not self.profile.get("omni_plugins_pi", True):
            self.opts.omni_plugin_dir = None
        elif self.opts.omni_plugin_dir is None:
            from .profiles import PROJECT_ROOT
            cand = PROJECT_ROOT / "plugins" / "pi"
            self.opts.omni_plugin_dir = str(cand) if cand.is_dir() else None
        self.reload()

    def reload(self) -> None:
        self._ready.wait()
        self.emit("session_shutdown", {"reason": "reload"})
        try:
            self.ext.shutdown()
        except Exception:
            pass
        self.ext = NullHost()
        self.settings.reload()
        self.registry.reload()
        self._resolve_profile(self.opts.profile)  # picks up capability toggles
        self.opts.no_extensions = self._no_extensions_flag or not self.profile.get("pi_extensions", True)
        self.load_resources()
        if not self.opts.no_extensions:
            self._start_extensions()
        self._load_profile_capabilities()
        self._init_tools()
        if self.mcp is not None:
            self.mcp.close()
            self.mcp = None
        self._mcp_auto = set()
        self._start_mcp()
        self.emit("session_start", {"reason": "reload"})
        self.emit("resources_discover", {"reason": "reload"})
