"""Profiles as data: `profiles/<name>.json` (+ `~/.omni/profiles/`), resolved through `extends`.

A profile says which capabilities the core loads, from barebones `pi` up to `full`:
    tools            Pi tool list spec ("read,bash,edit,write", "+grep", ...)
    pi_extensions    load Pi/OMP extensions through the Node host
    omni_plugins_pi  also load extensions from Omni's plugins/pi folder
    omni_registry    {"enabled": bool, "sources": [mcp, hermes, openclaw, omni]} - Omni's legacy tool registry
    permission_gate  confirm destructive tool calls (Omni PermissionGate)
    prompt_append    extra system prompt text
    accent / description  UI chrome
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_PROFILE = "pi"

BUILTIN: Dict[str, Dict[str, Any]] = {
    "pi": {"name": "pi", "description": "Barebones Pi.", "accent": "#7aa2ff", "tools": "read,bash,edit,write",
           "pi_extensions": True, "omni_plugins_pi": True, "omni_registry": {"enabled": False}, "permission_gate": False,
           "prompt_append": ""},
}
ALIASES = {"default": "pi", "minimal": "pi", "barebones": "pi", "omp": "lean"}


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
    prof: Dict[str, Any] = {}
    for c in chain:
        prof = _merge(prof, {k: v for k, v in c.items() if k != "extends"})
    prof["name"] = name
    return prof


def list_profiles() -> List[Dict[str, Any]]:
    names = set(BUILTIN)
    for d in profile_dirs():
        if d.is_dir():
            names.update(p.stem for p in d.glob("*.json"))
    order = ["pi", "lean", "balanced", "autonomous", "full", "evolve"]
    return [load_profile(n) for n in sorted(names, key=lambda n: (order.index(n) if n in order else 99, n))]


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
