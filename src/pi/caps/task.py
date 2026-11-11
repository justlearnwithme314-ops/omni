"""`task`: hand a self-contained piece of work to a subagent (Oh My Pi `task`, Hermes `delegate_task`).

The subagent is a fresh `AgentSession` with its own, empty context: it gets only the prompt you write, works with
the core file/shell tools, and returns one final report. Its intermediate tool output never enters the parent's
context, which is the main reason to use it - a wide search or a noisy build costs the parent a few hundred tokens
instead of tens of thousands.

Subagents cannot spawn subagents, run without extensions or MCP, keep no session file, and inherit the parent's
model, credentials, project trust and permission gate.
"""
from __future__ import annotations

from typing import Any, Dict, List

from ..tools.base import ToolDef, ToolError, text_result
from . import Capability

READ_ONLY_TOOLS = ["read", "grep", "find", "ls"]
WORK_TOOLS = ["read", "bash", "edit", "write", "grep", "find", "ls"]
MAX_REPORT_CHARS = 12_000
SUBAGENT_PROMPT = ("You are a subagent working on one delegated task. You have no memory of the parent conversation: "
                   "everything you need is in the task below. Do the work, then end with a concise final report "
                   "(findings, files changed with paths, anything left undone). The report is the only thing the "
                   "parent agent sees.")


def run_subagent(parent: Any, prompt: str, read_only: bool, signal: Any = None, on_update: Any = None) -> Dict[str, Any]:
    from ..agent import AgentSession, SessionOptions
    from ..ai.models import model_key
    from ..modes import HeadlessUI
    if parent.model is None:
        raise ToolError("no model selected")
    opts = SessionOptions(
        cwd=parent.cwd, model=model_key(parent.model), thinking=parent.thinking_level, mode="print",
        tools=",".join(READ_ONLY_TOOLS if read_only else WORK_TOOLS), no_session=True, no_extensions=True, no_mcp=True,
        no_prompt_templates=True, approve=parent.trusted, agent_dir=str(parent.agent_dir), profile="core",
        capabilities=[], append_system_prompt=SUBAGENT_PROMPT)
    child = AgentSession(opts, ui=HeadlessUI())
    child.start()
    child.gate = parent.gate  # the parent's permission gate also guards the subagent
    steps: List[str] = []

    def watch(ev: Dict[str, Any]) -> None:
        if ev.get("type") == "tool_execution_start" and on_update:
            args = ev.get("args") or {}
            hint = args.get("path") or args.get("command") or args.get("pattern") or ""
            steps.append(f"{ev.get('toolName')} {str(hint)[:60]}".strip())
            on_update({"content": [{"type": "text", "text": "\n".join(steps[-8:])}], "details": {"steps": len(steps)}})
    child.subscribe(watch)
    remove = signal.add_listener(child.abort) if signal is not None and hasattr(signal, "add_listener") else (lambda: None)
    try:
        child.prompt(prompt, source="subagent")
    finally:
        remove()
    messages = [e["message"] for e in child.sessions.entries if e.get("type") == "message"]
    last = next((m for m in reversed(messages) if m.get("role") == "assistant"), None)
    usage = {"input": 0, "output": 0, "cost": 0.0}
    for m in messages:
        u = m.get("usage") or {}
        usage["input"] += (u.get("input") or 0) + (u.get("cacheRead") or 0)
        usage["output"] += u.get("output") or 0
        usage["cost"] += (u.get("cost") or {}).get("total") or 0.0
    if last is None:
        raise ToolError("the subagent produced no response")
    text = "".join(b.get("text", "") for b in last.get("content") or [] if b.get("type") == "text").strip()
    if last.get("stopReason") in ("error", "aborted"):
        raise ToolError(f"subagent {last.get('stopReason')}: {last.get('errorMessage') or text or 'no details'}")
    return {"text": text, "usage": usage, "toolCalls": len(steps), "turns": sum(1 for m in messages if m.get("role") == "assistant")}


def _tools(session: Any) -> Dict[str, ToolDef]:
    def execute(_id, p, signal=None, on_update=None, ctx=None):
        prompt = str(p.get("prompt") or "").strip()
        if not prompt:
            raise ToolError("prompt is required: a complete, self-contained description of the task")
        r = run_subagent(session, prompt, bool(p.get("read_only")), signal, on_update)
        text = r["text"] or "(the subagent finished without a written report)"
        if len(text) > MAX_REPORT_CHARS:
            text = text[:MAX_REPORT_CHARS] + f"\n\n[report truncated: {len(r['text']) - MAX_REPORT_CHARS} more characters]"
        return text_result(text, {"description": p.get("description"), "usage": r["usage"], "toolCalls": r["toolCalls"],
                                  "turns": r["turns"]})

    return {"task": ToolDef(
        name="task", label="Task",
        description="Delegate a self-contained task to a subagent with a fresh context and get back only its final "
                    "report. Use it for wide searches, investigations and independent subtasks whose intermediate "
                    "output you do not need. The subagent knows nothing about this conversation: put all context, "
                    "paths and the expected output in `prompt`. Set read_only=true for pure research.",
        parameters={"type": "object", "properties": {
            "description": {"type": "string", "description": "3-6 word label"},
            "prompt": {"type": "string"}, "read_only": {"type": "boolean"}}, "required": ["prompt"]},
        execute=execute, prompt_snippet="Delegate a self-contained task to a subagent")}


def _available(session: Any):
    return "subagents cannot spawn subagents" if getattr(session.opts, "capabilities", None) == [] and \
        session.opts.append_system_prompt == SUBAGENT_PROMPT else None


CAPABILITIES = [Capability("task", "Subagents", "Delegate work to a fresh-context subagent and receive only its report.",
                           "Oh My Pi task, Hermes delegate_task", tools=_tools, available=_available)]
