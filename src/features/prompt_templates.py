"""Pi-style prompt templates (/name args) and /skill:name expansion."""
import re
import shlex
from pathlib import Path
from typing import Dict, Optional

from ..paths import PROJECT_ROOT, user_data_dir, workspace_root
from .context_files import skill_roots


def template_dirs():
    ws = workspace_root()
    return [Path.home() / ".pi" / "agent" / "prompts", ws / ".pi" / "prompts", user_data_dir() / "prompts",
            ws / ".omni" / "prompts", PROJECT_ROOT / "prompts"]


def load_templates() -> Dict[str, Path]:
    out: Dict[str, Path] = {}
    for d in template_dirs():
        if d.is_dir():
            for f in sorted(d.glob("*.md")):
                out[f.stem] = f
    return out


def _body(path: Path) -> str:
    t = path.read_text(encoding="utf-8", errors="replace")
    return re.sub(r"^---\s*\n.*?\n---\s*\n", "", t, count=1, flags=re.S)


def _fill(body: str, args: list) -> str:
    body = re.sub(r"\$\{@:(\d+)\}", lambda m: " ".join(args[int(m.group(1)) - 1:]), body)
    body = body.replace("$ARGUMENTS", " ".join(args)).replace("$@", " ".join(args))
    return re.sub(r"\$(\d)", lambda m: args[int(m.group(1)) - 1] if int(m.group(1)) <= len(args) else "", body)


def expand(line: str) -> Optional[str]:
    """Return the prompt text for '/template args' or '/skill:name args', else None."""
    name, _, rest = line.lstrip("/").partition(" ")
    try:
        args = shlex.split(rest)
    except ValueError:
        args = rest.split()
    if name.startswith("skill:"):
        sname = name[6:]
        for root in skill_roots():
            f = root / sname / "SKILL.md"
            if f.is_file():
                return f"Use this skill (from {f}):\n\n{_body(f)}\n\nTask: {rest}".strip()
        return None
    t = load_templates().get(name)
    return _fill(_body(t), args).strip() if t else None
