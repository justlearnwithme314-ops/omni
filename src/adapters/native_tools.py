"""Pi-compatible core tools: edit, grep, find (names/params match Pi)."""
import fnmatch
import json
import os
import re
from pathlib import Path

from .base import CanonicalTool
from ..paths import workspace_root

_SKIP = {".git", "node_modules", "__pycache__", ".venv", "venv", "dist", "build"}


def _resolve(p: str) -> Path:
    path = Path(p or ".").expanduser()
    return path if path.is_absolute() else workspace_root() / path


def _walk(root: Path):
    if root.is_file():
        yield root
        return
    for dp, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d not in _SKIP]
        for f in files:
            yield Path(dp) / f


async def edit(args: dict, context: dict = None) -> str:
    path = _resolve(args.get("path", ""))
    old, new = args.get("oldText"), args.get("newText")
    if old is None or new is None:
        raise ValueError("'oldText' and 'newText' are required")
    if not path.is_file():
        raise FileNotFoundError(f"File not found: {path}")
    text = path.read_text(encoding="utf-8")
    n = text.count(old)
    if n == 0:
        raise ValueError("oldText not found in file (it must match exactly, including whitespace)")
    if n > 1 and not args.get("replaceAll"):
        raise ValueError(f"oldText matches {n} places; add more context or set replaceAll")
    path.write_text(text.replace(old, new), encoding="utf-8")
    return f"Edited {path} ({n} replacement{'s' if n > 1 else ''})"


async def grep(args: dict, context: dict = None) -> str:
    flags = re.I if args.get("ignoreCase") else 0
    rx = re.compile(args["pattern"], flags)
    glob, limit, out = args.get("glob"), int(args.get("limit", 100)), []
    for f in _walk(_resolve(args.get("path", "."))):
        if glob and not fnmatch.fnmatch(f.name, glob):
            continue
        try:
            for i, line in enumerate(f.read_text(encoding="utf-8").splitlines(), 1):
                if rx.search(line):
                    out.append(f"{f}:{i}: {line.strip()[:200]}")
                    if len(out) >= limit:
                        return "\n".join(out) + f"\n[limit {limit} reached]"
        except (UnicodeDecodeError, OSError):
            continue
    return "\n".join(out) or "(no matches)"


async def find(args: dict, context: dict = None) -> str:
    pat, limit, out = args.get("pattern", "*"), int(args.get("limit", 200)), []
    root = _resolve(args.get("path", "."))
    for f in _walk(root):
        if fnmatch.fnmatch(f.name, pat) or fnmatch.fnmatch(str(f.relative_to(root)) if root.is_dir() else f.name, pat):
            out.append(str(f))
            if len(out) >= limit:
                break
    return "\n".join(out) or "(no files)"


def _tool(name, desc, props, required, fn):
    return CanonicalTool(name=name, description=desc, handler=fn, source="native", enabled=True,
                         parameters={"type": "object", "properties": props, "required": required})


def register(registry) -> None:
    s = lambda d: {"type": "string", "description": d}
    registry.register_tool(_tool("edit", "Edit a file by replacing exact text (oldText -> newText).",
        {"path": s("File path"), "oldText": s("Exact text to replace"), "newText": s("Replacement text"),
         "replaceAll": {"type": "boolean"}}, ["path", "oldText", "newText"], edit))
    registry.register_tool(_tool("grep", "Search file contents with a regex.",
        {"pattern": s("Regex"), "path": s("Directory or file"), "glob": s("Filename glob, e.g. *.py"),
         "ignoreCase": {"type": "boolean"}, "limit": {"type": "integer"}}, ["pattern"], grep))
    registry.register_tool(_tool("find", "Find files by glob pattern.",
        {"pattern": s("Glob, e.g. *.ts"), "path": s("Directory"), "limit": {"type": "integer"}}, ["pattern"], find))
