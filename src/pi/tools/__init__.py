"""Pi's built-in tools. `create_all_tools(cwd)` returns them keyed by name, like Pi's createAllToolDefinitions."""
from __future__ import annotations

from typing import Callable, Dict, Optional

from .base import ToolDef, ToolError, ToolContext, text_result  # noqa: F401
from .files import make_edit, make_ls, make_read, make_write
from .search import make_find, make_grep
from .shell import make_bash, run_shell, format_bash_output, find_shell  # noqa: F401

ALL_TOOL_NAMES = ["read", "bash", "edit", "write", "grep", "find", "ls"]
DEFAULT_TOOLS = ["read", "bash", "edit", "write"]
READ_ONLY_TOOLS = ["read", "grep", "find", "ls"]


def create_all_tools(cwd: str, settings_getter: Optional[Callable] = None, env_getter: Optional[Callable] = None) -> Dict[str, ToolDef]:
    return {
        "read": make_read(cwd),
        "bash": make_bash(cwd, settings_getter, env_getter),
        "edit": make_edit(cwd),
        "write": make_write(cwd),
        "grep": make_grep(cwd),
        "find": make_find(cwd),
        "ls": make_ls(cwd),
    }


def resolve_tool_list(spec, current=None):
    """Pi's defaultTools / --tools semantics: plain names replace, `+name` adds, `-name` removes."""
    if spec is None:
        return list(current or DEFAULT_TOOLS)
    items = [s.strip() for s in (spec.split(",") if isinstance(spec, str) else spec) if s and s.strip()]
    if items and all(s[0] in "+-" for s in items):
        out = list(current or DEFAULT_TOOLS)
        for s in items:
            name = s[1:]
            if s[0] == "+" and name not in out:
                out.append(name)
            elif s[0] == "-" and name in out:
                out.remove(name)
        return out
    return [s.lstrip("+") for s in items if not s.startswith("-")]
