"""Profiles as data: `profiles/<name>.json` (+ `~/.omni/profiles/`), resolved through `extends`.

A profile says which parts of the harness load, from the barebones `core` up to `full`:
    tools              tool list spec ("read,bash,edit,write", "+grep", ...)
    capabilities       native modules to switch on (see `src/pi/caps`): ["todo", "ask"] replaces the inherited list,
                       ["+memory", "-ask"] edits it
    pi_extensions      load Pi / Oh My Pi extensions through the Node host
    omni_plugins_pi    also load extensions from Omni's plugins/pi folder
    omni_registry      {"enabled": bool, "sources": [mcp, hermes, openclaw, omni]} - Omni's plugin registry
    permission_gate    confirm destructive tool calls
    mcp                false = do not start MCP servers
    skills_budget_chars  cap on the skills listing in the prompt (descriptions are shortened to fit)
    prompt_append      extra system prompt text
    accent / description  UI chrome

Built-in profiles are named for what they are; the names of the harnesses that inspired them still work as aliases
(`pi` -> core, `omp` -> coder, `hermes` -> assistant, `openclaw` -> autonomous).
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_PROFILE = "core"

BUILTIN: Dict[str, Dict[str, Any]] = {
    "core": {"name": "core", "description": "Barebones core.", "accent": "#7aa2ff", "tools": "read,bash,edit,write",
             "pi_extensions": True, "omni_plugins_pi": True, "omni_registry": {"enabled": False}, "permission_gate": False,
             "capabilities": [], "prompt_append": ""},
}
ALIASES = {"default": "core", "minimal": "core", "barebones": "core", "pi": "core", "omp": "coder", "ohmypi": "coder",
           "hermes": "assistant", "openclaw": "autonomous"}
ORDER = ["core", "lean", "coder", "assistant", "balanced", "autonomous", "full", "evolve"]


def canonical(name: Optional[str]) -> str:
    n = (name or DEFAULT_PROFILE).lower()
    return ALIASES.get(n, n)


def user_profiles_dir() -> Path:
    """Where `/profile save` writes: `OMNI_PROFILES_DIR` when set, else `~/.omni/profiles`."""
    extra = os.environ.get("OMNI_PROFILES_DIR")
    return Path(extra) if extra else Path.home() / ".omni" / "profiles"


def profile_dirs() -> List[Path]:
    dirs = [PROJECT_ROOT / "profiles", Path.home() / ".omni" / "profiles"]
    extra = os.environ.get("OMNI_PROFILES_DIR")
    if extra:
        dirs.append(Path(extra))
    return dirs


def _raw(name: str) -> Optional[Dict[str, Any]]:
    found = None
    for d in profile_dirs():
        p = d / f"{name}.json"
        if p.is_file():
            try:
                found = json.loads(p.read_text(encoding="utf-8"))  # later dirs override earlier ones
            except Exception:
                continue
    return found or BUILTIN.get(name)


def _merge(base: Dict[str, Any], over: Dict[str, Any]) -> Dict[str, Any]:
    out = dict(base)
    for k, v in over.items():
        out[k] = _merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


def load_profile(name: Optional[str]) -> Dict[str, Any]:
    name = ALIASES.get((name or DEFAULT_PROFILE).lower(), (name or DEFAULT_PROFILE).lower())
    chain: List[Dict[str, Any]] = []
    cur: Optional[str] = name
    seen = set()
    while cur and cur not in seen:
        seen.add(cur)
        raw = _raw(cur)
        if raw is None:
            break
        chain.insert(0, raw)
        cur = raw.get("extends")
    if not chain:
        chain = [BUILTIN[DEFAULT_PROFILE]]
        name = DEFAULT_PROFILE
    from .caps import merge_capability_lists
    prof: Dict[str, Any] = {}
    caps: List[str] = []
    for c in chain:
        prof = _merge(prof, {k: v for k, v in c.items() if k not in ("extends", "capabilities")})
        if isinstance(c.get("capabilities"), list):
            caps = merge_capability_lists(caps, [str(x) for x in c["capabilities"]])
    prof["capabilities"] = caps
    prof["name"] = name
    return prof


def list_profiles() -> List[Dict[str, Any]]:
    names = set(BUILTIN)
    for d in profile_dirs():
        if d.is_dir():
            names.update(p.stem for p in d.glob("*.json"))
    return [load_profile(n) for n in sorted(names, key=lambda n: (ORDER.index(n) if n in ORDER else 99, n))]


def delete_profile(name: str) -> None:
    name = (name or "").strip().lower()
    f = user_profiles_dir() / f"{name}.json"
    if not name or not f.is_file():
        raise ValueError(f"'{name}' is not a custom profile (built-in profiles cannot be deleted)")
    f.unlink()


def describe_profile(name: Optional[str]) -> List[str]:
    """What a profile is made of, for `/profile show`."""
    from .caps import load_builtin
    prof = load_profile(name)
    caps = load_builtin()
    custom = (user_profiles_dir() / f"{prof['name']}.json").is_file()
    lines = [f"{prof['name']}{' (custom)' if custom else ''} — {prof.get('description', '')}", f"  tools: {prof.get('tools', '')}", "  modules:"]
    lines += [f"    {c:<19} {caps[c].description if c in caps else '(unknown)'}" for c in prof.get("capabilities") or []] or ["    (none)"]
    on = [c.id for c in caps.values() if c.is_on is not None and c.is_on(prof)]
    lines.append(f"  hosts on: {', '.join(sorted(on)) or '(none)'}")
    for k in ("skills_budget_chars", "memory", "tracing", "sandbox", "browser", "hooks", "model_roles"):
        if prof.get(k):
            lines.append(f"  {k}: {json.dumps(prof[k])}")
    return lines


def save_profile(name: str, extends: str, capabilities: List[str], description: str = "",
                 extra: Optional[Dict[str, Any]] = None, overwrite: bool = False) -> Path:
    """Write a custom profile (`/profile save <name>`): the parent profile plus an exact capability list."""
    import re
    name = name.strip().lower()
    if not re.match(r"^[a-z0-9][a-z0-9_-]{0,31}$", name):
        raise ValueError("profile names use lowercase letters, numbers, - and _ (max 32)")
    if name in ALIASES or (PROJECT_ROOT / "profiles" / f"{name}.json").is_file() or name in BUILTIN:
        raise ValueError(f"'{name}' is a built-in profile name; pick another")
    d = user_profiles_dir()
    d.mkdir(parents=True, exist_ok=True)
    data = {"name": name, "extends": extends, "description": description or f"Custom profile based on {extends}.",
            "capabilities": list(capabilities), **(extra or {})}
    path = d / f"{name}.json"
    if path.exists() and not overwrite:
        raise ValueError(f"a custom profile '{name}' already exists; use /profile edit {name} or pick another name")
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    return path


def _config_path() -> Path:
    return PROJECT_ROOT / "omni.config.json"


def active_profile_name() -> str:
    """omni.config.json `command_profile`, falling back to harness.config.json `active_mode`, then `pi`."""
    names = {p.stem for d in profile_dirs() if d.is_dir() for p in d.glob("*.json")} | set(BUILTIN)
    for path, key in ((_config_path(), "command_profile"), (PROJECT_ROOT / "harness.config.json", "active_mode")):
        try:
            val = json.loads(path.read_text(encoding="utf-8")).get(key)
        except (OSError, ValueError):
            continue
        if isinstance(val, str):
            val = ALIASES.get(val.lower(), val.lower())
            if val in names:
                return val
    return DEFAULT_PROFILE


def set_active_profile(name: str) -> None:
    path = _config_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    data["command_profile"] = name
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")
