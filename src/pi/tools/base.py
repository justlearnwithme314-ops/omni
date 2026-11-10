"""Tool definition shape shared by built-in tools, extension tools and MCP tools (Pi's `ToolDefinition`)."""
from __future__ import annotations

import os
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional


class ToolError(Exception):
    """Raised by a tool; becomes an `isError` tool result whose text is the message."""


@dataclass
class ToolContext:
    cwd: str
    signal: Any = None
    model: Optional[Dict[str, Any]] = None
    session: Any = None  # AgentSession (for extension ctx)


@dataclass
class ToolDef:
    name: str
    label: str
    description: str
    parameters: Dict[str, Any]
    execute: Callable[..., Dict[str, Any]]  # (tool_call_id, params, signal, on_update, ctx) -> {content, details, isError?}
    prompt_snippet: Optional[str] = None
    prompt_guidelines: List[str] = field(default_factory=list)
    source: str = "builtin"          # builtin | extension:<path> | mcp:<server>
    prepare_arguments: Optional[Callable[[Any], Any]] = None
    has_renderer: bool = False       # extension supplies renderCall/renderResult
    read_only: bool = False

    def declaration(self) -> Dict[str, Any]:
        return {"name": self.name, "description": self.description, "parameters": self.parameters}


def text_result(text: str, details: Any = None, is_error: bool = False) -> Dict[str, Any]:
    r: Dict[str, Any] = {"content": [{"type": "text", "text": text}], "details": details}
    if is_error:
        r["isError"] = True
    return r


def expand_path(p: str) -> str:
    p = p.strip()
    if p.startswith("@"):  # models sometimes copy the @file mention syntax
        p = p[1:]
    if p == "~" or p.startswith("~/") or p.startswith("~\\"):
        p = str(Path.home()) + p[1:]
    return p


def resolve_to_cwd(p: str, cwd: str) -> str:
    p = expand_path(p)
    return os.path.normpath(p if os.path.isabs(p) else os.path.join(cwd, p))


def check_abort(signal) -> None:
    if signal is not None and getattr(signal, "aborted", False):
        raise ToolError("Operation aborted")


_mutation_locks: Dict[str, threading.Lock] = {}
_mutation_guard = threading.Lock()


def file_mutation_lock(path: str) -> threading.Lock:
    """Pi's withFileMutationQueue: serialize writes/edits to the same file."""
    key = os.path.normcase(os.path.abspath(path))
    with _mutation_guard:
        return _mutation_locks.setdefault(key, threading.Lock())
