"""`openclaw-plugins`: load OpenClaw plugins in the Node extension host.

An OpenClaw plugin is a package with an `openclaw.plugin.json` manifest and an entry built with
`definePluginEntry({ id, register(api) })` from `openclaw/plugin-sdk`. Its agent tools have the same shape as
Pi's, so the extension host runs them directly:

    api.registerTool(tool | factory)        -> a tool the model can call
    api.registerCommand({name, handler})    -> /name
    api.on("before_tool_call")              -> may block or rewrite a tool call
    api.on("after_tool_call" | "tool_result"), "before_prompt_build" (adds prompt context), "agent_end",
           "session_start", "session_end", "message_received"

Everything else an OpenClaw plugin can register - channels, model / search / speech providers, services, gateway
methods, CLI commands - has no slot in this core yet. Such calls are accepted and listed under the plugin in
`/extensions` as "not yet supported", so a plugin that mixes tools with those still contributes its tools.

Plugins are found in `~/.openclaw/extensions/*`, the project's `.openclaw/extensions/*` (trusted projects),
Omni's `plugins/openclaw/*`, and the profile's `"openclaw_plugin_dirs"`. Needs the `extensions` host (Node).
`openclaw/plugin-sdk/*` imports resolve to a small shim: the entry helpers plus the common parameter and result
helpers; plugins that deep-import other SDK modules get recorded stubs for them.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import Capability


def _entries(d: Path) -> List[Path]:
    """Entry files of one plugin folder: package.json `openclaw.extensions`, else index.ts / index.js."""
    out: List[Path] = []
    try:
        pkg = json.loads((d / "package.json").read_text(encoding="utf-8"))
        for rel in (pkg.get("openclaw") or {}).get("extensions") or []:
            f = (d / str(rel)).resolve()
            if f.is_file() and d.resolve() in f.parents:
                out.append(f)
    except (OSError, ValueError):
        pass
    if not out:
        out = [f for f in (d / n for n in ("index.ts", "index.js", "index.mjs", "dist/index.js")) if f.is_file()][:1]
    return out


def discover(session: Any) -> List[Dict[str, Any]]:
    from ..profiles import PROJECT_ROOT
    roots = [Path.home() / ".openclaw" / "extensions", PROJECT_ROOT / "plugins" / "openclaw"]
    if session.trusted:
        roots.append(Path(session.cwd) / ".openclaw" / "extensions")
    extra = session.profile.get("openclaw_plugin_dirs")
    if isinstance(extra, list):
        roots = [Path(os.path.expanduser(str(e))) for e in extra] + (roots if session.profile.get("openclaw_default_dirs", True) else [])
    plugins: List[Dict[str, Any]] = []
    seen = set()
    for r in roots:
        if not r.is_dir():
            continue
        for d in sorted(p for p in r.iterdir() if p.is_dir()):
            mf = d / "openclaw.plugin.json"
            if not mf.is_file():
                continue
            try:
                manifest = json.loads(mf.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                manifest = {}
            pid = str(manifest.get("id") or d.name)
            if pid in seen:
                continue
            seen.add(pid)
            plugins.append({"id": pid, "name": str(manifest.get("name") or pid), "path": d, "entries": _entries(d),
                            "description": str(manifest.get("description") or ""),
                            "channels": list(manifest.get("channels") or []),
                            "tools": list((manifest.get("contracts") or {}).get("tools") or [])})
    return plugins


def _extension_paths(session: Any) -> List[Path]:
    return [e for p in discover(session) for e in p["entries"]]


def _available(session: Any) -> Optional[str]:
    if session.opts.no_extensions:
        return "needs the extension host (the `extensions` capability, Node >= 22.13)"
    return None


def describe(session: Any) -> str:
    plugins = discover(session)
    if not plugins:
        return "No OpenClaw plugins found (looked in ~/.openclaw/extensions, .openclaw/extensions and Omni's plugins/openclaw)."
    loaded = {str(Path(e.get("path", ""))): e for e in (session.ext.report() if hasattr(session.ext, "report") else [])}
    rows = []
    for p in plugins:
        states = []
        for entry in p["entries"]:
            e = loaded.get(str(entry))
            if e is None:
                states.append("not loaded")
            elif not e.get("ok"):
                states.append(f"failed: {str(e.get('error'))[:120]}")
            else:
                gaps = sorted(m for m in e.get("missing") or [] if m.startswith("openclaw"))
                states.append(f"loaded, tools: {', '.join(e.get('tools') or []) or 'none'}" +
                              (f"; not supported yet: {', '.join(g.split('.', 1)[1] for g in gaps)[:160]}" if gaps else ""))
        rows.append(f"{p['id']} — {'; '.join(states) or 'no entry file'}\n   {p['path']}")
    return "\n".join(rows)


CAPABILITIES = [Capability(
    "openclaw-plugins", "OpenClaw plugin host", "Load OpenClaw plugins (openclaw.plugin.json + definePluginEntry): their "
    "tools, commands and core hooks.", "OpenClaw", extension_paths=_extension_paths, available=_available,
    commands=lambda s: {"openclaw": ("List OpenClaw plugins and what loaded", lambda _a: describe(s))}, group="host",
    requires=["extensions"])]
