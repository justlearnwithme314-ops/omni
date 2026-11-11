"""`todo`: a task list the model keeps for multi-step work (Hermes `todo`, Oh My Pi `todo_write`).

State lives in the session file as `custom` entries, so it follows resume, fork and branch navigation, and the
compact list in each tool result keeps it visible after compaction without growing the system prompt.
"""
from __future__ import annotations

from typing import Any, Dict, List

from ..tools.base import ToolDef, ToolError, text_result
from . import Capability

STATUSES = ("pending", "in_progress", "completed", "cancelled")
MARK = {"pending": "[ ]", "in_progress": "[>]", "completed": "[x]", "cancelled": "[-]"}


def current(session: Any) -> List[Dict[str, Any]]:
    for e in reversed(session.sessions.get_branch()):
        if e.get("type") == "custom" and e.get("customType") == "todo":
            return list((e.get("data") or {}).get("todos") or [])
    return []


def render(todos: List[Dict[str, Any]]) -> str:
    if not todos:
        return "(todo list is empty)"
    done = sum(1 for t in todos if t["status"] == "completed")
    return "\n".join(f"{MARK[t['status']]} {t['id']}. {t['content']}" for t in todos) + f"\n({done}/{len(todos)} completed)"


def _tools(session: Any) -> Dict[str, ToolDef]:
    def execute(_id, p, signal=None, on_update=None, ctx=None):
        todos = current(session)
        incoming = p.get("todos")
        if incoming is None:
            return text_result(render(todos), {"todos": todos})
        if not isinstance(incoming, list):
            raise ToolError("todos must be a list")
        by_id = {t["id"]: t for t in todos} if p.get("merge") else {}
        order = [t["id"] for t in todos] if p.get("merge") else []
        for raw in incoming:
            if not isinstance(raw, dict):
                raise ToolError("each todo must be an object")
            tid = str(raw.get("id") or len(order) + 1)
            old = by_id.get(tid, {})
            status = raw.get("status") or old.get("status") or "pending"
            content = str(raw.get("content") or old.get("content") or "").strip()
            if status not in STATUSES:
                raise ToolError(f"status must be one of: {', '.join(STATUSES)}")
            if not content:
                raise ToolError(f"todo {tid} needs content")
            if tid not in by_id:
                order.append(tid)
            by_id[tid] = {"id": tid, "content": content, "status": status}
        todos = [by_id[i] for i in order]
        if sum(1 for t in todos if t["status"] == "in_progress") > 1:
            raise ToolError("only one todo may be in_progress at a time")
        session.sessions.append_custom("todo", {"todos": todos})
        session._notify({"type": "todo_changed", "todos": todos})
        return text_result(render(todos), {"todos": todos})

    return {"todo": ToolDef(
        name="todo", label="Todo",
        description="Track multi-step work. Call with `todos` to write the list (merge=true updates items by id and "
                    "keeps the rest); call with no arguments to read it. Keep exactly one item in_progress.",
        parameters={"type": "object", "properties": {
            "todos": {"type": "array", "items": {"type": "object", "properties": {
                "id": {"type": "string"}, "content": {"type": "string"},
                "status": {"type": "string", "enum": list(STATUSES)}}, "required": ["id"]}},
            "merge": {"type": "boolean"}}},
        execute=execute, prompt_snippet="Plan and track multi-step tasks",
        prompt_guidelines=["Use todo for tasks with 3+ steps; mark each item completed as soon as it is done."])}


CAPABILITIES = [Capability("todo", "Todo list", "A task list the model maintains for multi-step work.",
                           "Hermes todo, Oh My Pi todo_write", tools=_tools)]
