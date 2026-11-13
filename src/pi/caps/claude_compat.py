"""`claude-compat`: use Claude Code's slash commands, plugins and MCP config as they are.

What is picked up (project paths only in trusted projects):

    slash commands   ~/.claude/commands/**.md, <project>/.claude/commands/**.md      -> /name  (`$ARGUMENTS`, `$1` work)
    MCP servers      <project>/.mcp.json                                             -> same servers as in mcp.json
    plugins          a folder with `.claude-plugin/plugin.json`, found in
                       <agent-dir>/plugins/*, <project>/.omni/plugins/*, <project>/.claude/plugins/*,
                       ~/.claude/plugins (installed_plugins.json and a scan), profile "plugin_dirs"
                     each plugin contributes
                       commands/*.md        slash commands
                       agents/*.md          named agents for `task`
                       skills/*/SKILL.md    skills
                       hooks/hooks.json     shell hooks (needs the `hooks` capability; ${CLAUDE_PLUGIN_ROOT} works)
                       .mcp.json            MCP servers

Your own prompts, skills and mcp.json always win over a plugin's entry with the same name. Not supported:
`allowed-tools` in command frontmatter, inline `!` shell execution in commands, output styles, LSP servers and
marketplaces (`/plugins` only lists what is already on disk).
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import Capability

MAX_SCAN_DEPTH = 4


def _manifest(d: Path) -> Optional[Dict[str, Any]]:
    f = d / ".claude-plugin" / "plugin.json"
    if not f.is_file():
        return None
    try:
        data = json.loads(f.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _scan(root: Path, depth: int = 0) -> List[Path]:
    """Plugin folders under `root` (a plugin's own subfolders are not searched)."""
    if not root.is_dir() or depth > MAX_SCAN_DEPTH:
        return []
    if (root / ".claude-plugin" / "plugin.json").is_file():
        return [root]
    out: List[Path] = []
    try:
        children = sorted(p for p in root.iterdir() if p.is_dir() and p.name not in ("node_modules", ".git"))
    except OSError:
        return []
    for c in children:
        out += _scan(c, depth + 1)
    return out


def discover_plugins(session: Any) -> List[Dict[str, Any]]:
    cache = getattr(session, "_claude_plugins", None)
    if cache is not None:
        return cache
    roots: List[Path] = [Path(session.agent_dir) / "plugins"]
    extra = session.profile.get("plugin_dirs")
    if isinstance(extra, list):
        roots += [Path(os.path.expanduser(str(e))) for e in extra]
    if session.trusted:
        roots += [Path(session.cwd) / ".omni" / "plugins", Path(session.cwd) / ".claude" / "plugins"]
    home = Path.home() / ".claude" / "plugins"
    dirs: List[Path] = []
    try:  # Claude Code's own record of installed plugins
        installed = json.loads((home / "installed_plugins.json").read_text(encoding="utf-8"))
        entries = installed.get("plugins") if isinstance(installed, dict) else None
        for versions in (entries or {}).values():
            for v in versions if isinstance(versions, list) else [versions]:
                if isinstance(v, dict) and v.get("installPath"):
                    dirs.append(Path(v["installPath"]))
    except (OSError, ValueError):
        pass
    for r in roots + [home]:
        dirs += _scan(r)
    plugins, seen = [], set()
    for d in dirs:
        m = _manifest(d)
        key = str(d.resolve()) if d.exists() else str(d)
        if m is None or key in seen:
            continue
        seen.add(key)
        name = str(m.get("name") or d.name)
        if any(p["name"] == name for p in plugins):
            continue  # first one found wins (your own folders are searched before ~/.claude)
        plugins.append({"name": name, "version": str(m.get("version") or ""), "description": str(m.get("description") or ""),
                        "path": d, "commands": d / "commands", "agents": d / "agents", "skills": d / "skills",
                        "hooks": d / "hooks" / "hooks.json", "mcp": d / ".mcp.json"})
    session._claude_plugins = plugins
    return plugins


def _command_dirs(base: Path) -> List[Path]:
    """`commands/` and its subfolders (Claude Code namespaces commands by folder)."""
    if not base.is_dir():
        return []
    out = [base]
    for dirpath, dirnames, _files in os.walk(base):
        dirnames[:] = sorted(d for d in dirnames if not d.startswith("."))
        out += [Path(dirpath) / d for d in dirnames]
    return out


def _template_roots(session: Any) -> List[Path]:
    roots = _command_dirs(Path.home() / ".claude" / "commands")
    if session.trusted:
        roots += _command_dirs(Path(session.cwd) / ".claude" / "commands")
    for p in discover_plugins(session):
        roots += _command_dirs(p["commands"])
    return roots


def _skill_roots(session: Any) -> List[Path]:
    session._claude_plugins = None  # skills are discovered first on every (re)load: refresh the plugin list here
    return [p["skills"] for p in discover_plugins(session) if p["skills"].is_dir()]


def _agent_roots(session: Any) -> List[Path]:
    return [p["agents"] for p in discover_plugins(session) if p["agents"].is_dir()]


def _hook_files(session: Any) -> List[Any]:
    return [(p["hooks"], str(p["path"])) for p in discover_plugins(session) if p["hooks"].is_file()]


def _mcp_files(session: Any) -> List[Path]:
    files = [p["mcp"] for p in discover_plugins(session) if p["mcp"].is_file()]
    if session.trusted and (Path(session.cwd) / ".mcp.json").is_file():
        files.append(Path(session.cwd) / ".mcp.json")
    return files


def describe(session: Any) -> str:
    plugins = discover_plugins(session)
    if not plugins:
        return ("No Claude Code plugins found. Put one (a folder with .claude-plugin/plugin.json) in "
                f"{Path(session.agent_dir) / 'plugins'} or the project's .omni/plugins.")
    rows = []
    for p in plugins:
        parts = []
        n = sum(len(list(d.glob("*.md"))) for d in _command_dirs(p["commands"]))
        if n:
            parts.append(f"{n} command(s)")
        if p["agents"].is_dir():
            parts.append(f"{len(list(p['agents'].glob('*.md')))} agent(s)")
        if p["skills"].is_dir():
            parts.append(f"{len(list(p['skills'].glob('*/SKILL.md')))} skill(s)")
        if p["hooks"].is_file():
            parts.append("hooks" + ("" if session.caps.is_enabled("hooks") else " (off: enable the `hooks` capability)"))
        if p["mcp"].is_file():
            parts.append("MCP servers")
        rows.append(f"{p['name']} {p['version']}".rstrip() + f" — {', '.join(parts) or 'nothing usable'}\n   {p['path']}"
                    + (f"\n   {p['description'][:140]}" if p["description"] else ""))
    return "\n".join(rows)


CAPABILITIES = [Capability(
    "claude-compat", "Claude Code compatibility", "Use Claude Code's slash commands, plugins (commands, agents, skills, "
    "hooks, MCP servers) and .mcp.json unchanged.", "Claude Code", template_roots=_template_roots, skill_roots=_skill_roots,
    agent_roots=_agent_roots, hook_files=_hook_files, mcp_files=_mcp_files,
    commands=lambda s: {"plugins": ("List the Claude Code plugins in use", lambda _a: describe(s))}, group="host")]
