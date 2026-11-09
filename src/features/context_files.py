"""Project context for the system prompt: AGENTS.md/CLAUDE.md and an <available_skills> list.

Skills are instructions the model reads on demand (with read_file); they are never executed.
"""
import re
from pathlib import Path
from typing import List

from ..paths import PROJECT_ROOT, user_data_dir, workspace_root

CONTEXT_NAMES = ("AGENTS.md", "CLAUDE.md")


def project_context(max_chars: int = 12000) -> str:
    """First AGENTS.md (else CLAUDE.md) found walking up from the workspace, plus the user-level one."""
    parts: List[str] = []
    seen = set()
    candidates = [user_data_dir()] + [workspace_root(), *workspace_root().parents]
    for d in candidates:
        for name in CONTEXT_NAMES:
            f = d / name
            if f.is_file() and f not in seen:
                seen.add(f)
                parts.append(f"### {f}\n{f.read_text(encoding='utf-8', errors='replace').strip()}")
                break
    return "\n\n".join(parts)[:max_chars]


def skill_roots() -> List[Path]:
    ws = workspace_root()
    return [ws / ".omni" / "skills", ws / ".claude" / "skills", ws / ".agents" / "skills",
            user_data_dir() / "skills", Path.home() / ".claude" / "skills", Path.home() / ".agents" / "skills",
            PROJECT_ROOT / "skills", PROJECT_ROOT / "plugins" / "openclaw"]


def _frontmatter(text: str) -> dict:
    m = re.match(r"^---\s*\n(.*?)\n---", text, re.S)
    out = {}
    for line in (m.group(1).splitlines() if m else []):
        k, sep, v = line.partition(":")
        if sep and not line.startswith((" ", "\t")):
            out[k.strip()] = v.strip().strip("\"'")
    return out


def skills_block(limit: int = 60) -> str:
    rows, seen = [], set()
    for root in skill_roots():
        if not root.is_dir():
            continue
        for f in sorted(root.glob("*/SKILL.md")):
            fm = _frontmatter(f.read_text(encoding="utf-8", errors="replace")[:2000])
            name = fm.get("name") or f.parent.name
            if name in seen or fm.get("disable-model-invocation", "").lower() == "true":
                continue
            seen.add(name)
            rows.append(f"- {name}: {fm.get('description', '')[:160]} (read {f})")
    if not rows:
        return ""
    return ("## Skills\nWhen a task matches a skill below, read its SKILL.md with read_file and follow it.\n"
            "<available_skills>\n" + "\n".join(rows[:limit]) + "\n</available_skills>")
