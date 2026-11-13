"""`diagnostics`: tell the model at once when an edit broke a file (Oh My Pi's LSP feedback after edits).

After every `write` / `edit`, the changed file gets a quick syntax check and any problem is appended to the tool
result, so the model fixes it in the next step instead of discovering it three tool calls later.

    .py                  Python's own compiler            .json            json
    .js .mjs .cjs        `node --check` (if Node is on PATH)                .toml            tomllib
    .yaml .yml           PyYAML (if installed)

These are syntax checks, not a language server: no type errors, no unresolved imports. They cost nothing when the
file is fine, and nothing at all for other file types.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from typing import Any, Dict, List, Optional

from ..tools.base import resolve_to_cwd
from . import Capability

EDIT_TOOLS = {"write", "edit"}
MAX_FILE = 2_000_000


def check_file(path: str) -> Optional[str]:
    """One-line description of a syntax problem in `path`, or None."""
    ext = os.path.splitext(path)[1].lower()
    if ext not in (".py", ".json", ".js", ".mjs", ".cjs", ".toml", ".yaml", ".yml"):
        return None
    try:
        if os.path.getsize(path) > MAX_FILE:
            return None
        with open(path, "rb") as f:
            raw = f.read()
    except OSError:
        return None
    text = raw.decode("utf-8", "replace")
    try:
        if ext == ".py":
            compile(text, path, "exec", dont_inherit=True)
        elif ext == ".json":
            json.loads(text) if text.strip() else None
        elif ext == ".toml":
            import tomllib
            tomllib.loads(text)
        elif ext in (".yaml", ".yml"):
            try:
                import yaml  # type: ignore
            except ImportError:
                return None
            list(yaml.safe_load_all(text))
        else:
            node = shutil.which("node")
            if not node:
                return None
            p = subprocess.run([node, "--check", path], capture_output=True, text=True, timeout=15, encoding="utf-8", errors="replace")
            if p.returncode != 0:
                lines = [l for l in (p.stderr or "").splitlines() if l.strip()]
                where = next((l for l in lines if path in l or l.startswith("file:")), "")
                line_no = where.rsplit(":", 1)[-1] if where.rsplit(":", 1)[-1].isdigit() else "?"
                msg = next((l for l in lines if "Error" in l), lines[-1] if lines else "syntax error")
                return f"line {line_no}: {msg.strip()}"
    except SyntaxError as e:
        return f"line {e.lineno}: {e.msg}" + (f" — `{(e.text or '').strip()[:80]}`" if e.text else "")
    except json.JSONDecodeError as e:
        return f"line {e.lineno}, column {e.colno}: {e.msg}"
    except subprocess.TimeoutExpired:
        return None
    except Exception as e:  # tomllib / yaml parse errors carry their own position text
        return str(e).replace("\n", " ")[:200]
    return None


def _paths(session: Any, args: Dict[str, Any], result: Dict[str, Any]) -> List[str]:
    out: List[str] = []
    if isinstance(args.get("path"), str):
        out.append(resolve_to_cwd(args["path"], session.cwd))
    details = result.get("details")
    if isinstance(details, dict) and isinstance(details.get("files"), list):  # hashline patches touch several files
        out += [str(f) for f in details["files"]]
    return list(dict.fromkeys(out))


def _after_tool(session: Any, tool_name: str, args: Dict[str, Any], result: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    if tool_name not in EDIT_TOOLS or result.get("isError"):
        return None
    problems = []
    for path in _paths(session, args, result):
        why = check_file(path) if os.path.isfile(path) else None
        if why:
            problems.append(f"{os.path.relpath(path, session.cwd)}: {why}")
    if not problems:
        return None
    note = "\n\n[diagnostics] The file was saved, but it no longer parses:\n" + "\n".join(problems) + "\nFix this before moving on."
    return {**result, "content": list(result.get("content") or []) + [{"type": "text", "text": note}],
            "details": {**(result.get("details") or {}), "diagnostics": problems} if isinstance(result.get("details"), (dict, type(None))) else result.get("details")}


CAPABILITIES = [Capability("diagnostics", "Edit diagnostics", "After each edit, check the file's syntax and report problems "
                           "in the same tool result.", "Oh My Pi LSP diagnostics", after_tool=_after_tool)]
