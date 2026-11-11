"""Skills across harnesses.

`skills-universal`  also discover skills installed for other agents (Claude Code, Hermes, OpenClaw, Oh My Pi, Codex)
                    and honour their gating metadata, so one skill library serves every harness:
                      OpenClaw  metadata.openclaw.{os, requires.bins / anyBins / env, always}
                      Hermes    platforms: [macos, linux, windows]
`skill-authoring`   the `skill_manage` tool: the agent writes and maintains its own skills (Hermes' procedural
                    memory). Skills land in `<agent-dir>/skills/<name>/SKILL.md`, the standard Agent Skills layout.

Skills cost context only for their name + description line; the body is read on demand. Profiles can cap the listing
with `skills_budget_chars` (see `fit_skills`).
"""
from __future__ import annotations

import json
import os
import re
import shutil
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..tools.base import ToolDef, ToolError, text_result
from . import Capability

_NAME_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_OS_OPENCLAW = {"win32": "win32", "darwin": "darwin", "linux": "linux"}
_OS_HERMES = {"win32": "windows", "darwin": "macos", "linux": "linux"}


def _platform() -> str:
    return "win32" if sys.platform.startswith("win") else "darwin" if sys.platform == "darwin" else "linux"


def _universal_roots(session: Any) -> List[Path]:
    home = Path.home()
    roots = [home / ".claude" / "skills", home / ".hermes" / "skills", home / ".openclaw" / "skills",
             home / ".openclaw" / "workspace" / "skills", home / ".omp" / "agent" / "skills", home / ".codex" / "skills"]
    if session.trusted:
        roots += [Path(session.cwd) / ".claude" / "skills", Path(session.cwd) / ".openclaw" / "skills",
                  Path(session.cwd) / ".hermes" / "skills"]
    extra = session.profile.get("skill_dirs")
    if isinstance(extra, list):
        roots += [Path(os.path.expanduser(str(e))) for e in extra]
    return [r for r in roots if r.is_dir()]


def _meta(fm: Dict[str, Any], *keys: str) -> Dict[str, Any]:
    md = fm.get("metadata")
    if isinstance(md, str):  # OpenClaw writes metadata as single-line JSON
        try:
            md = json.loads(md)
        except ValueError:
            md = {}
    if not isinstance(md, dict):
        return {}
    for k in keys:
        if isinstance(md.get(k), dict):
            return md[k]
    return {}


def gate_reason(fm: Dict[str, Any], env: Optional[Dict[str, str]] = None, which=shutil.which,
                platform: Optional[str] = None) -> Optional[str]:
    """Why a skill should be hidden on this machine (None = show it)."""
    env = os.environ if env is None else env
    plat = platform or _platform()
    platforms = fm.get("platforms")
    if isinstance(platforms, list) and platforms and _OS_HERMES[plat] not in [str(p).lower() for p in platforms]:
        return f"for {', '.join(map(str, platforms))} only"
    oc = _meta(fm, "openclaw", "clawdbot", "clawdis")
    if not oc or oc.get("always") is True:
        return None
    oses = oc.get("os")
    if isinstance(oses, list) and oses and _OS_OPENCLAW[plat] not in [str(o).lower() for o in oses]:
        return f"for {', '.join(map(str, oses))} only"
    req = oc.get("requires") if isinstance(oc.get("requires"), dict) else {}
    missing = [b for b in req.get("bins") or [] if not which(str(b))]
    if missing:
        return f"needs {', '.join(missing)} on PATH"
    any_bins = [str(b) for b in req.get("anyBins") or []]
    if any_bins and not any(which(b) for b in any_bins):
        return f"needs one of {', '.join(any_bins)} on PATH"
    no_env = [e for e in req.get("env") or [] if not env.get(str(e))]
    if no_env:
        return f"needs {', '.join(map(str, no_env))} set"
    return None


def fit_skills(skills: List[Any], budget_chars: int) -> List[Any]:
    """Keep the skills listing inside a context budget: shorten descriptions (longest first) to fit."""
    import dataclasses
    total = sum(len(s.description) for s in skills)
    if budget_chars <= 0 or total <= budget_chars or not skills:
        return skills
    per = max(60, budget_chars // len(skills))
    out = []
    for s in skills:
        d = s.description
        if len(d) > per:
            cut = d[:per].rsplit(" ", 1)[0].rstrip(",;: ")
            d = cut + "…"
        out.append(dataclasses.replace(s, description=d))
    return out


# ------------------------------------------------------------------------------------- skill authoring
def _skill_dir(session: Any, name: str) -> Path:
    if not _NAME_RE.match(name or "") or len(name) > 64:
        raise ToolError("name must be lowercase letters, numbers and hyphens (max 64), e.g. 'deploy-staging'")
    return Path(session.agent_dir) / "skills" / name


def _authoring_tools(session: Any) -> Dict[str, ToolDef]:
    def execute(_id, p, signal=None, on_update=None, ctx=None):
        action, name = p.get("action"), str(p.get("name") or "")
        d = _skill_dir(session, name)
        f = d / "SKILL.md"
        if action == "create":
            desc, body = str(p.get("description") or "").strip(), str(p.get("content") or "").strip()
            if not desc or not body:
                raise ToolError("create needs description (when to use the skill, one sentence) and content (the instructions)")
            if len(desc) > 1024:
                raise ToolError("description must be at most 1024 characters")
            if f.exists():
                raise ToolError(f"skill '{name}' already exists; use action=patch or edit")
            d.mkdir(parents=True, exist_ok=True)
            f.write_text(f"---\nname: {name}\ndescription: {json.dumps(desc, ensure_ascii=False)}\n---\n\n{body}\n", encoding="utf-8")
            msg = f"Created skill '{name}' at {f}"
        elif action == "edit":
            if not f.exists():
                raise ToolError(f"skill '{name}' does not exist")
            text = str(p.get("content") or "")
            if not text.lstrip().startswith("---"):
                raise ToolError("edit replaces the whole SKILL.md: content must start with the --- frontmatter block")
            f.write_text(text, encoding="utf-8")
            msg = f"Rewrote {f}"
        elif action == "patch":
            target = d / str(p.get("file") or "SKILL.md")
            if d.resolve() not in target.resolve().parents:
                raise ToolError("file must stay inside the skill folder")
            if not target.is_file():
                raise ToolError(f"{target} does not exist")
            old, new = str(p.get("old_text") or ""), str(p.get("new_text") or "")
            text = target.read_text(encoding="utf-8")
            if not old or text.count(old) != 1:
                raise ToolError(f"old_text must match exactly once (found {text.count(old) if old else 0})")
            target.write_text(text.replace(old, new), encoding="utf-8")
            msg = f"Patched {target}"
        elif action == "write_file":
            rel = str(p.get("file") or "")
            target = (d / rel)
            if not rel or d.resolve() not in target.resolve().parents:
                raise ToolError("file must be a relative path inside the skill folder, e.g. scripts/run.sh")
            if not f.exists():
                raise ToolError(f"skill '{name}' does not exist; create it first")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(str(p.get("content") or ""), encoding="utf-8")
            msg = f"Wrote {target}"
        elif action == "delete":
            if not f.exists():
                raise ToolError(f"skill '{name}' does not exist")
            shutil.rmtree(d)
            msg = f"Deleted skill '{name}'"
        else:
            raise ToolError("action must be create, edit, patch, write_file or delete")
        session.load_resources()  # the skill list in the prompt follows from the next turn
        return text_result(msg, {"action": action, "name": name, "path": str(f)})

    return {"skill_manage": ToolDef(
        name="skill_manage", label="Skill",
        description="Create and maintain your own skills: reusable instructions for a kind of task. After solving "
                    "something non-trivial that will recur, save the working procedure as a skill; patch a skill "
                    "when its steps turn out wrong or outdated. Actions: create (name, description, content), "
                    "patch (name, old_text, new_text, file?), edit (name, content = whole SKILL.md), "
                    "write_file (name, file, content), delete (name).",
        parameters={"type": "object", "properties": {
            "action": {"type": "string", "enum": ["create", "patch", "edit", "write_file", "delete"]},
            "name": {"type": "string"}, "description": {"type": "string"}, "content": {"type": "string"},
            "old_text": {"type": "string"}, "new_text": {"type": "string"}, "file": {"type": "string"}},
            "required": ["action", "name"]},
        execute=execute, prompt_snippet="Save reusable procedures as skills")}


CAPABILITIES = [
    Capability("skills-universal", "Universal skills", "Use skills installed for Claude Code, Hermes, OpenClaw, "
               "Oh My Pi and Codex, with their OS / binary / env gating.", "OpenClaw, Hermes, Agent Skills",
               skill_roots=_universal_roots, skill_filter=lambda s, sk: gate_reason(sk.frontmatter)),
    Capability("skill-authoring", "Skill authoring", "The agent writes and maintains its own skills (skill_manage).",
               "Hermes skill_manage, OpenClaw skill-workshop", tools=_authoring_tools),
]
