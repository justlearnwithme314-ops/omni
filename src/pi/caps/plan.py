"""`plan`: plan mode (Oh My Pi / Claude Code style). `/plan` makes the session read-only until a plan is approved.

While plan mode is on, a tool gate blocks everything that can change the machine (edit, write, non-read-only shell
commands, and any tool not marked read-only), and the prompt tells the model to investigate and then present its
plan with `exit_plan_mode`. Approving the plan switches plan mode off and the model starts implementing; choosing
"keep planning" returns your feedback to it. The state is stored in the session, so it survives resume.
"""
from __future__ import annotations

import re
from typing import Any, Dict, Optional

from ..tools.base import ToolDef, ToolError, text_result
from . import Capability

ALWAYS_ALLOWED = {"read", "grep", "find", "ls", "fetch", "todo", "ask", "exit_plan_mode", "checkpoint", "rewind",
                  "tool_search", "ast_grep"}
_SAFE_BASH = re.compile(
    r"^\s*(ls|dir|cat|head|tail|wc|pwd|which|where|type|file|stat|tree|du|df|rg|grep|find|fd|echo|date|whoami|uname|env|"
    r"printenv|git\s+(status|log|diff|show|branch|remote|rev-parse|ls-files|blame|describe|tag\s*$|stash\s+list)|"
    r"python\s+--version|node\s+--version|npm\s+(ls|list|view)|pip\s+(list|show|freeze)|cargo\s+(tree|metadata)|go\s+(list|version))\b")
_UNSAFE_SHELL = re.compile(r"(>>?|\btee\b|\brm\b|\bmv\b|\bcp\b|\bsed\s+-i|\bchmod\b|\bchown\b|\bmkdir\b|\btouch\b|\bgit\s+(commit|push|"
                           r"reset|checkout|merge|rebase|clean|add|rm|apply|restore|switch)\b|\bnpm\s+(i|install|run)\b|\bpip\s+install\b)")
PROMPT = ("PLAN MODE IS ACTIVE. You must not change anything: no file edits or writes, no commands with side effects. "
          "Investigate with read-only tools until you understand the task, then call `exit_plan_mode` with a concrete "
          "plan (what will change, in which files, in what order, and how it will be verified). Do not start "
          "implementing until the plan is approved.")


def is_on(session: Any) -> bool:
    for e in reversed(session.sessions.get_branch()):
        if e.get("type") == "custom" and e.get("customType") == "plan-mode":
            return bool((e.get("data") or {}).get("on"))
    return False


def set_mode(session: Any, on: bool) -> None:
    if is_on(session) != on:
        session.sessions.append_custom("plan-mode", {"on": on})
        session._notify({"type": "plan_mode", "on": on})


def bash_is_read_only(command: str) -> bool:
    parts = re.split(r"\s*(?:&&|\|\||;|\|)\s*", command.strip())
    return bool(parts) and not _UNSAFE_SHELL.search(command) and all(_SAFE_BASH.match(p) for p in parts if p)


def _gate(session: Any, tool_name: str, args: Dict[str, Any]) -> Optional[str]:
    if not is_on(session) or tool_name in ALWAYS_ALLOWED:
        return None
    if tool_name == "bash":
        if bash_is_read_only(str(args.get("command") or "")):
            return None
        return ("Plan mode: this command could change something. Only read-only commands run now (ls, cat, rg, "
                "git status/log/diff, ...). Present your plan with exit_plan_mode to get approval.")
    if tool_name == "task" and args.get("read_only"):
        return None
    tool = session.all_tools().get(tool_name)
    if tool is not None and tool.read_only:
        return None
    return f"Plan mode: `{tool_name}` is blocked until the plan is approved. Call exit_plan_mode with your plan."


def _tools(session: Any) -> Dict[str, ToolDef]:
    def execute(_id, p, signal=None, on_update=None, ctx=None):
        plan = str(p.get("plan") or "").strip()
        if not plan:
            raise ToolError("plan is required: the steps you propose, in markdown")
        if not is_on(session):
            return text_result("Plan mode is not active; no approval is needed. Continue.", {"approved": True})
        ui = session.ui
        session.sessions.append_custom("plan", {"plan": plan})
        if ui is None or not getattr(ui, "has_ui", False):
            set_mode(session, False)  # nobody to ask (print / json / rpc): the plan is the record
            return text_result("Plan recorded and plan mode is off (no interactive user to approve). Implement the plan.",
                               {"approved": True, "interactive": False})
        pick = ui.ext_select(title="Plan ready — approve it?\n\n" + plan, options=["Approve and start", "Keep planning"])
        if pick == "Approve and start":
            set_mode(session, False)
            return text_result("The user approved the plan. Plan mode is off: implement it now, step by step.",
                               {"approved": True})
        feedback = ui.ext_input(title="What should change in the plan?", placeholder="feedback (optional)") if pick else None
        return text_result("The user did not approve the plan yet." + (f" Feedback: {feedback}" if feedback else "") +
                           " Stay in plan mode, revise, and call exit_plan_mode again.", {"approved": False})

    return {"exit_plan_mode": ToolDef(
        name="exit_plan_mode", label="Plan",
        description="Only in plan mode: present your implementation plan for approval. Call it when you have "
                    "finished investigating; the user approves it or sends feedback.",
        parameters={"type": "object", "properties": {"plan": {"type": "string", "description": "markdown plan"}},
                    "required": ["plan"]},
        execute=execute)}


CAPABILITIES = [Capability("plan", "Plan mode", "/plan makes the session read-only until you approve the model's plan.",
                           "Oh My Pi plan mode, Claude Code", tools=_tools, gate=_gate,
                           live_prompt=lambda s: PROMPT if is_on(s) else "")]
