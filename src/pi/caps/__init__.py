"""Capabilities: the toggleable modules a profile is made of.

A capability is one self-contained feature - a few tools, an optional prompt section, optional extra skill roots, or
a switch for one of the hosts (extensions, MCP, plugin registries, permission gate). Nothing here loads unless a
profile (or the user's overlay) turns it on, so the `core` profile stays exactly Pi-sized.

    profile  "capabilities": ["todo", "ask"]         replaces the inherited list
             "capabilities": ["+memory", "-ask"]      edits the inherited list
    overlay  <agent-dir>/omni-capabilities.json       {"memory": true, "mcp": false}   (`/capabilities`, `omni toggle`)

Every capability reports what it costs in context tokens (tool schemas + prompt text), so "how light is this
profile?" has a number.

Adding one: write a module with `CAPABILITY = Capability(...)` (or call `register`) and list it in `_BUILTIN_MODULES`;
third-party Python can call `register()` from an Omni plugin.
"""
from __future__ import annotations

import importlib
import importlib.util
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from ..tools.base import ToolDef

OVERLAY_FILE = "omni-capabilities.json"


@dataclass
class Capability:
    id: str
    title: str
    description: str
    inspired_by: str = ""                                    # which harness(es) the behaviour follows
    tools: Optional[Callable[[Any], Dict[str, ToolDef]]] = None        # session -> tools (active while enabled)
    prompt: Optional[Callable[[Any], str]] = None                       # session -> system prompt section ("" = none)
    skill_roots: Optional[Callable[[Any], List[Path]]] = None           # session -> extra skill folders
    skill_filter: Optional[Callable[[Any, Any], Optional[str]]] = None  # (session, skill) -> reason to hide it
    template_roots: Optional[Callable[[Any], List[Path]]] = None        # session -> extra prompt-template (slash command) folders
    agent_roots: Optional[Callable[[Any], List[Path]]] = None           # session -> extra named-agent folders (for `task`)
    hook_files: Optional[Callable[[Any], List[Any]]] = None             # session -> [(hooks file, plugin root or None)]
    extension_paths: Optional[Callable[[Any], List[Path]]] = None       # session -> extra entries for the Node extension host
    mcp_files: Optional[Callable[[Any], List[Path]]] = None             # session -> extra MCP config files ({"mcpServers": ...})
    available: Optional[Callable[[Any], Optional[str]]] = None          # session -> why it cannot run here
    live_prompt: Optional[Callable[[Any], str]] = None                  # like `prompt`, but re-read every request
    gate: Optional[Callable[[Any, str, Dict[str, Any]], Optional[str]]] = None  # (session, tool, args) -> reason to block
    # (session, tool, args, result) -> replacement result or None; runs after the tool, before extensions see it
    after_tool: Optional[Callable[[Any, str, Dict[str, Any], Dict[str, Any]], Optional[Dict[str, Any]]]] = None
    # (session, text) -> extra context to append to the user's message, or a {"block": reason} dict
    on_prompt: Optional[Callable[[Any, str], Any]] = None
    # session -> {command name: (description, handler(args) -> text or None)}: slash commands
    commands: Optional[Callable[[Any], Dict[str, Any]]] = None
    unload: Optional[Callable[[Any], None]] = None                      # session -> None, on reload / shutdown
    # switch-type capabilities edit the profile instead of adding tools: (profile dict, on) -> None
    apply: Optional[Callable[[Dict[str, Any], bool], None]] = None
    is_on: Optional[Callable[[Dict[str, Any]], bool]] = None            # switch-type: read state from a profile
    group: str = "feature"                                              # feature | host
    requires: List[str] = field(default_factory=list)                   # capability ids this one builds on
    origin: str = "builtin"                                             # "builtin" or the file a user module came from


CAPABILITIES: Dict[str, Capability] = {}
_BUILTIN_MODULES = ["todo", "ask", "fetch", "memory", "task", "skills", "checkpoint", "hashline", "ast", "plan", "search",
                    "hooks", "hermes_plugins", "tracing", "archive", "durable", "sandbox", "browser", "scheduler", "claude_compat", "openclaw_plugins", "diagnostics", "reflect", "approvals", "code_exec", "evolution", "code_nav", "rules", "budget", "github", "notify", "workflows", "process", "output", "inspect_file", "extras", "board", "guard", "review", "image_gen", "lsp", "nodes", "hosts"]
_loaded = False


def register(cap: Capability) -> Capability:
    CAPABILITIES[cap.id] = cap
    return cap


def unregister(cap_id: str) -> None:
    CAPABILITIES.pop(cap_id, None)


def load_builtin() -> Dict[str, Capability]:
    global _loaded
    if not _loaded:
        _loaded = True
        for name in _BUILTIN_MODULES:
            mod = importlib.import_module(f"{__name__}.{name}")
            for cap in getattr(mod, "CAPABILITIES", []):
                CAPABILITIES.setdefault(cap.id, cap)
    return CAPABILITIES


USER_ERRORS: Dict[str, str] = {}   # module file -> why it did not load
_user_seen: Dict[str, float] = {}  # module file -> mtime it was loaded at


def user_capability_dirs(agent_dir: Path, cwd: Optional[str] = None, trusted: bool = False) -> List[Path]:
    dirs = [Path(agent_dir) / "capabilities"]
    if cwd and trusted:
        dirs.append(Path(cwd) / ".omni" / "capabilities")
    return dirs


def load_user(agent_dir: Path, cwd: Optional[str] = None, trusted: bool = False) -> List[str]:
    """Load capability modules you wrote yourself: `<agent-dir>/capabilities/*.py` and, in trusted projects,
    `.omni/capabilities/*.py`. A module defines `CAPABILITIES = [Capability(...)]` exactly like the built-in ones.
    Built-in ids cannot be replaced. Returns the ids loaded (or refreshed) by this call."""
    load_builtin()
    loaded: List[str] = []
    for d in user_capability_dirs(agent_dir, cwd, trusted):
        if not d.is_dir():
            continue
        for f in sorted(d.glob("*.py")):
            if f.name.startswith("_"):
                continue
            key = str(f)
            try:
                mtime = f.stat().st_mtime
            except OSError:
                continue
            if _user_seen.get(key) == mtime:
                continue
            _user_seen[key] = mtime
            USER_ERRORS.pop(key, None)
            try:
                spec = importlib.util.spec_from_file_location(f"omni_user_capability_{f.stem}_{abs(hash(key)) % 10 ** 8}", f)
                mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
                spec.loader.exec_module(mod)  # type: ignore[union-attr]
                found = [c for c in getattr(mod, "CAPABILITIES", []) if isinstance(c, Capability)]
                if not found:
                    raise ValueError("the module defines no CAPABILITIES list")
            except Exception as e:
                USER_ERRORS[key] = f"{type(e).__name__}: {e}"
                continue
            for cap in found:
                existing = CAPABILITIES.get(cap.id)
                if existing is not None and existing.origin == "builtin":
                    USER_ERRORS[key] = f"'{cap.id}' is a built-in capability id; pick another id"
                    continue
                cap.origin = key
                CAPABILITIES[cap.id] = cap
                loaded.append(cap.id)
    return loaded


# ------------------------------------------------------------------------------------------- resolution
def merge_capability_lists(base: List[str], over: List[str]) -> List[str]:
    """`["a", "b"]` replaces; `["+c", "-a"]` edits the inherited list. Mixed lists: plain entries replace first."""
    plain = [x for x in over if not x.startswith(("+", "-"))]
    out = list(dict.fromkeys(plain)) if plain or not over else list(base)
    for x in over:
        if x.startswith("+") and x[1:] not in out:
            out.append(x[1:])
        elif x.startswith("-") and x[1:] in out:
            out.remove(x[1:])
    return out


def read_overlay(agent_dir: Path) -> Dict[str, bool]:
    try:
        data = json.loads((Path(agent_dir) / OVERLAY_FILE).read_text(encoding="utf-8"))
        return {str(k): bool(v) for k, v in data.items()} if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def write_overlay(agent_dir: Path, overlay: Dict[str, bool]) -> None:
    p = Path(agent_dir) / OVERLAY_FILE
    if overlay:
        p.write_text(json.dumps(overlay, indent=2, sort_keys=True), encoding="utf-8")
    elif p.exists():
        p.unlink()


def effective_profile(profile: Dict[str, Any], agent_dir: Path, use_overlay: bool = True) -> Dict[str, Any]:
    """Profile with the user's overlay applied: feature ids land in `capabilities`, host switches edit their keys."""
    caps = load_builtin()
    prof = json.loads(json.dumps(profile))  # deep copy; profiles are plain data
    enabled = [c for c in prof.get("capabilities") or [] if isinstance(c, str)]
    overlay = read_overlay(agent_dir) if use_overlay else {}
    for cid, on in overlay.items():
        cap = caps.get(cid)
        if cap is None:
            continue
        if cap.apply is not None:
            cap.apply(prof, on)
        elif on and cid not in enabled:
            enabled.append(cid)
        elif not on and cid in enabled:
            enabled.remove(cid)
    prof["capabilities"] = enabled
    prof["_overlay"] = overlay
    return prof


def estimate_tokens(obj: Any) -> int:
    return len(obj if isinstance(obj, str) else json.dumps(obj, ensure_ascii=False)) // 4


class CapabilityManager:
    """Per-session view: which capabilities are on, their tools, prompt sections and skill roots."""

    def __init__(self, session: Any):
        self.session = session
        self.tools: Dict[str, ToolDef] = {}
        self.owner: Dict[str, str] = {}      # tool name -> capability id
        self.enabled: List[str] = []
        self.problems: Dict[str, str] = {}   # capability id -> why it did not load
        self._prompts: Dict[str, str] = {}

    def _wanted(self, cap_id: str) -> bool:
        """Is this capability asked for by the profile (whether or not it has loaded yet)?"""
        cap = load_builtin().get(cap_id)
        if cap is not None and cap.is_on is not None:
            return cap.is_on(self.session.profile)
        return cap_id in (self.session.profile.get("capabilities") or [])

    def is_enabled(self, cap_id: str) -> bool:
        cap = load_builtin().get(cap_id)
        if cap is not None and cap.is_on is not None:
            return cap.is_on(self.session.profile)
        return cap_id in self.enabled

    def load(self) -> None:
        load_user(self.session.agent_dir, self.session.cwd, bool(getattr(self.session, "trusted", False)))
        caps = load_builtin()
        s = self.session
        self.unload()  # hosts started by a previous load (plugin processes, listeners) go first
        self.tools, self.owner, self.enabled, self.problems, self._prompts = {}, {}, [], {}, {}
        quiet: set = set()
        for cid in s.profile.get("capabilities") or []:
            cap = caps.get(cid)
            if cap is None:
                self.problems[cid] = "unknown capability"
                continue
            why = cap.available(s) if cap.available else None
            lacking = [r for r in cap.requires if not self._wanted(r)]
            if lacking and not why:
                why = f"needs {', '.join(lacking)} (turn it on with /capabilities {lacking[0]} on)"
                quiet.discard(cid)
                self.problems[cid] = why
                continue
            if why:  # e.g. an external program is missing: shown in /capabilities, not worth a startup warning
                self.problems[cid] = why
                quiet.add(cid)
                continue
            try:
                tools = cap.tools(s) if cap.tools and not s.opts.no_tools else {}
                text = cap.prompt(s) if cap.prompt else ""
            except Exception as e:  # one broken module must not take the session down
                self.problems[cid] = f"{type(e).__name__}: {e}"
                continue
            self.enabled.append(cid)
            for name, t in tools.items():
                t.source = f"capability:{cid}"
                self.tools[name] = t
                self.owner[name] = cid
            if text:
                self._prompts[cid] = text.strip()
        for path, why in USER_ERRORS.items():
            note = f"capability module {path}: {why}"
            if note not in s.diagnostics:
                s.diagnostics.append(note)
        for cid, why in self.problems.items():
            if cid not in quiet:
                s.diagnostics.append(f"capability '{cid}' not loaded: {why}")

    def prompt_section(self) -> str:
        parts = [self._prompts[c] for c in self.enabled if c in self._prompts]
        for cid in self.enabled:
            cap = CAPABILITIES.get(cid)
            if cap is not None and cap.live_prompt is not None:
                try:
                    text = cap.live_prompt(self.session)
                except Exception:
                    text = ""
                if text:
                    parts.append(text.strip())
        return "\n\n".join(parts)

    def _each(self, attr: str):
        for cid in self.enabled:
            cap = CAPABILITIES.get(cid)
            fn = getattr(cap, attr, None) if cap is not None else None
            if fn is not None:
                yield cid, fn

    def after_tool(self, tool_name: str, args: Dict[str, Any], result: Dict[str, Any]) -> Dict[str, Any]:
        for cid, fn in self._each("after_tool"):
            try:
                r = fn(self.session, tool_name, args or {}, result)
            except Exception as e:
                self.session.diagnostics.append(f"capability '{cid}' after_tool: {type(e).__name__}: {e}")
                continue
            if isinstance(r, dict) and r.get("content") is not None:
                result = r
        return result

    def on_prompt(self, text: str):
        """(extra context to append, block reason). Hooks run in capability order; the first block wins."""
        extra: List[str] = []
        for cid, fn in self._each("on_prompt"):
            try:
                r = fn(self.session, text)
            except Exception as e:
                self.session.diagnostics.append(f"capability '{cid}' on_prompt: {type(e).__name__}: {e}")
                continue
            if isinstance(r, dict) and r.get("block"):
                return "", str(r["block"])
            if isinstance(r, str) and r.strip():
                extra.append(r.strip())
        return "\n\n".join(extra), None

    def commands(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {}
        for cid, fn in self._each("commands"):
            try:
                for name, spec in (fn(self.session) or {}).items():
                    out.setdefault(name, spec)
            except Exception:
                continue
        return out

    def unload(self) -> None:
        for _cid, fn in self._each("unload"):
            try:
                fn(self.session)
            except Exception:
                pass

    def gate(self, tool_name: str, args: Dict[str, Any]) -> Optional[str]:
        """First reason any enabled capability gives for blocking this tool call (None = allowed)."""
        for cid in self.enabled:
            cap = CAPABILITIES.get(cid)
            if cap is not None and cap.gate is not None:
                why = cap.gate(self.session, tool_name, args or {})
                if why:
                    return why
        return None

    def skill_roots(self) -> List[Path]:
        caps, out = load_builtin(), []
        for cid in self.session.profile.get("capabilities") or []:
            cap = caps.get(cid)
            if cap is not None and cap.skill_roots is not None:
                out += [p for p in cap.skill_roots(self.session) if p not in out]
        return out

    def collect(self, attr: str) -> List[Any]:
        """Everything the profile's capabilities contribute through a list-returning hook (works before `load`)."""
        caps, out = load_builtin(), []
        for cid in self.session.profile.get("capabilities") or []:
            fn = getattr(caps.get(cid), attr, None)
            if fn is not None:
                try:
                    out += [x for x in fn(self.session) if x not in out]
                except Exception as e:
                    self.session.diagnostics.append(f"capability '{cid}' {attr}: {type(e).__name__}: {e}")
        return out

    def filter_skills(self, skills: List[Any], diagnostics: List[str]) -> List[Any]:
        caps = load_builtin()
        filters = [caps[c].skill_filter for c in self.session.profile.get("capabilities") or []
                   if c in caps and caps[c].skill_filter is not None]
        if not filters:
            return skills
        kept = []
        for sk in skills:
            why = next((w for w in (f(self.session, sk) for f in filters) if w), None)
            if why:
                diagnostics.append(f"skill '{sk.name}' hidden: {why}")
            else:
                kept.append(sk)
        return kept

    def cost(self, cap_id: str) -> int:
        """Context tokens this capability adds to every request (tool schemas + prompt text), estimated."""
        decl = [t.declaration() for n, t in self.tools.items() if self.owner.get(n) == cap_id]
        return (estimate_tokens(decl) if decl else 0) + estimate_tokens(self._prompts.get(cap_id, ""))

    def rows(self) -> List[Dict[str, Any]]:
        """Everything `/capabilities` shows, lightest-to-understand first: features, then host switches."""
        s, out = self.session, []
        overlay = s.profile.get("_overlay") or {}
        for cid, cap in sorted(load_builtin().items(), key=lambda kv: (kv[1].group != "feature", kv[0])):
            on = self.is_enabled(cid)
            out.append({"id": cid, "title": cap.title, "description": cap.description, "inspired_by": cap.inspired_by,
                        "group": cap.group, "on": on, "tokens": self.cost(cid) if on and cap.apply is None else 0,
                        "tools": sorted(n for n, o in self.owner.items() if o == cid),
                        "problem": self.problems.get(cid), "overridden": cid in overlay, "origin": cap.origin})
        return out

    def toggle(self, cap_id: str, on: Optional[bool] = None) -> bool:
        """Flip (or set) a capability in the user's overlay. Returns the new state. Caller reloads the session."""
        if cap_id not in load_builtin():
            raise KeyError(cap_id)
        s = self.session
        new = (not self.is_enabled(cap_id)) if on is None else bool(on)
        overlay = read_overlay(s.agent_dir)
        base = effective_profile(s.base_profile, s.agent_dir, use_overlay=False)
        cap = CAPABILITIES[cap_id]
        base_on = cap.is_on(base) if cap.is_on is not None else cap_id in (base.get("capabilities") or [])
        if new == base_on:
            overlay.pop(cap_id, None)  # same as the profile: no override needed
        else:
            overlay[cap_id] = new
        write_overlay(s.agent_dir, overlay)
        return new
