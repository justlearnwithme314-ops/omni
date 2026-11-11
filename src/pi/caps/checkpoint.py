"""`checkpoint` / `rewind`: explore freely, then keep only the conclusion (Oh My Pi's context checkpoints).

    checkpoint(goal)      mark the start of exploratory work
    ... reads, greps, dead ends ...
    rewind(report)        drop everything since the checkpoint from the model's context; the report stays

The dropped messages stay in the session file and in the transcript you see; they are only removed from what is
sent to the model (Pi `context_edit` entries), so the saving survives resume and compaction. Unlike `task`, the
exploration runs in this conversation with all its tools and knowledge.
"""
from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from ..tools.base import ToolDef, ToolError, text_result
from . import Capability


def active_checkpoint(session: Any) -> Optional[Dict[str, Any]]:
    """The open checkpoint entry on the current branch, if any."""
    for e in reversed(session.sessions.get_branch()):
        if e.get("type") == "custom" and e.get("customType") == "rewind":
            return None
        if e.get("type") == "custom" and e.get("customType") == "checkpoint":
            return e
    return None


def _tool_call_ids(entry: Dict[str, Any]) -> List[str]:
    m = entry.get("message") or {}
    return [b.get("id") for b in m.get("content") or [] if isinstance(b, dict) and b.get("type") == "toolCall"]


def _tools(session: Any) -> Dict[str, ToolDef]:
    def checkpoint(_id, p, signal=None, on_update=None, ctx=None):
        goal = str(p.get("goal") or "").strip()
        if not goal:
            raise ToolError("goal is required: what you are about to investigate")
        if active_checkpoint(session) is not None:
            raise ToolError("a checkpoint is already active; call rewind(report) before starting another")
        session.sessions.append_custom("checkpoint", {"goal": goal, "toolCallId": _id})
        return text_result(f"Checkpoint started: {goal}\nExplore as needed, then call rewind(report) with the findings "
                           "you want to keep. Everything in between will leave your context.", {"goal": goal})

    def rewind(_id, p, signal=None, on_update=None, ctx=None):
        report = str(p.get("report") or "").strip()
        if not report:
            raise ToolError("report is required: the findings to keep (paths, line numbers, conclusions)")
        cp = active_checkpoint(session)
        if cp is None:
            raise ToolError("no active checkpoint; call checkpoint(goal) before exploring")
        branch = session.sessions.get_branch()
        i = next(k for k, e in enumerate(branch) if e["id"] == cp["id"])
        opener = next((e for e in reversed(branch[:i]) if e.get("type") == "message"
                       and (e["message"] or {}).get("role") == "assistant"), None)
        keep_results = set(_tool_call_ids(opener)) if opener else set()
        closer = next((k for k in range(len(branch) - 1, i, -1) if branch[k].get("type") == "message"
                       and _id in _tool_call_ids(branch[k])), len(branch))
        dropped, saved = 0, 0
        for e in branch[i + 1:closer]:
            if e.get("type") == "message" and (e["message"] or {}).get("role") == "user":
                continue  # something the user said mid-exploration is never dropped
            if e.get("type") == "custom_message" or (e.get("type") == "message" and not (
                    (e["message"] or {}).get("role") == "toolResult" and e["message"].get("toolCallId") in keep_results)):
                session.sessions._append({"type": "context_edit", "targetId": e["id"], "replacement": None})
                dropped += 1
                saved += len(json.dumps(e.get("message") or e.get("content") or "", default=str)) // 4
        session.sessions.append_custom("rewind", {"goal": (cp.get("data") or {}).get("goal"), "report": report,
                                                  "dropped": dropped, "savedTokens": saved})
        return text_result(f"Rewound to the checkpoint ({dropped} messages, ~{saved} tokens left your context). "
                           f"Report kept:\n\n{report}", {"dropped": dropped, "savedTokens": saved})

    return {
        "checkpoint": ToolDef(
            name="checkpoint", label="Checkpoint",
            description="Start a context checkpoint before exploratory work with many intermediate tool calls "
                        "(reading several files, searching, trial runs). Later call `rewind` to drop that "
                        "exploration from your context and keep only a concise report. Never nest checkpoints, and "
                        "always rewind before you finish the turn.",
            parameters={"type": "object", "properties": {"goal": {"type": "string"}}, "required": ["goal"]},
            execute=checkpoint, prompt_snippet="Mark the start of exploration you will later rewind"),
        "rewind": ToolDef(
            name="rewind", label="Rewind",
            description="End the active checkpoint: the messages since it are removed from your context and replaced "
                        "by `report`. Put everything you still need in the report (exact paths, line numbers, "
                        "decisions); you will not see the exploration again.",
            parameters={"type": "object", "properties": {"report": {"type": "string"}}, "required": ["report"]},
            execute=rewind),
    }


CAPABILITIES = [Capability("checkpoint", "Context checkpoints", "Explore, then rewind: the exploration leaves the "
                           "context and only your report stays.", "Oh My Pi checkpoint / rewind", tools=_tools)]
