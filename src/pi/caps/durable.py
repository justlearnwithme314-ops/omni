"""`durable`: runs that survive a crash (Temporal's durable-execution idea, applied to an agent run).

Temporal keeps an event history for every workflow, so after a crash it replays completed steps from the record and
only re-runs what never finished. A session file already is such a history (every message and tool result is
appended as it happens); this capability adds the two missing pieces:

1. **Run markers.** `started` / `ended` entries around each agent run. A session whose last marker is `started` was
   cut off mid-run (the terminal was closed, the machine slept, the process was killed).
2. **Resume.** Reopen the session (`omni -c`) and run `/resume-run`: the run continues from the record. Completed
   tool calls are never executed again, and each tool call that had started but has no recorded result gets a
   result saying so, so the model checks the real state before repeating anything with side effects. If you just
   type a new message instead, the record is repaired the same way first, so the conversation stays valid.

It also gives read-only tools an **activity retry policy**: a transient failure (timeout, connection error,
HTTP 429 / 5xx) is retried with backoff before the model ever sees it. Configure with
`"durable": {"retries": 2, "backoff": 0.5}`; tools with side effects are never retried automatically.
"""
from __future__ import annotations

import re
import time
from typing import Any, Dict, List, Optional

from . import Capability

TRANSIENT = re.compile(r"timed? ?out|temporar|connection (?:reset|refused|aborted|error)|unreachable|"
                       r"HTTP (?:429|500|502|503|504)|rate limit|try again|EAI_AGAIN|ECONNRESET", re.I)
INTERRUPTED = ("[interrupted] The process stopped while this tool call was running, before a result was recorded. "
               "Its effect is unknown: check the current state first, and repeat the call only if it is still needed.")


def _messages(session: Any) -> List[Dict[str, Any]]:
    return [e["message"] for e in session.sessions.get_branch() if e.get("type") == "message"]


def last_marker(session: Any) -> Optional[Dict[str, Any]]:
    for e in reversed(session.sessions.get_branch()):
        if e.get("type") == "custom" and e.get("customType") == "durable-run":
            return e.get("data") or {}
    return None


def pending_tool_calls(session: Any) -> List[Dict[str, Any]]:
    """Tool calls of the last assistant message that have no recorded result."""
    msgs = _messages(session)
    idx = next((i for i in range(len(msgs) - 1, -1, -1) if msgs[i].get("role") == "assistant"), None)
    if idx is None:
        return []
    calls = [b for b in msgs[idx].get("content") or [] if isinstance(b, dict) and b.get("type") == "toolCall"]
    done = {m.get("toolCallId") for m in msgs[idx + 1:] if m.get("role") == "toolResult"}
    return [c for c in calls if c.get("id") not in done]


def interrupted(session: Any) -> Optional[str]:
    """Why the last run looks unfinished (None = nothing to resume)."""
    if getattr(session, "is_streaming", False):
        return None
    marker = last_marker(session)
    if not marker or marker.get("state") != "started":
        return None
    pending = pending_tool_calls(session)
    if pending:
        return f"{len(pending)} tool call(s) had no result: {', '.join(c.get('name', '?') for c in pending)}"
    msgs = _messages(session)
    if msgs and msgs[-1].get("role") in ("user", "toolResult"):
        return "the model had not answered yet"
    return "the run never reached its end"


def repair(session: Any) -> int:
    """Close an interrupted run in the record: give every unanswered tool call an 'interrupted' result."""
    now = int(time.time() * 1000)
    pending = pending_tool_calls(session)
    for call in pending:
        session.sessions.append_message({"role": "toolResult", "toolCallId": call["id"], "toolName": call.get("name"),
                                         "content": [{"type": "text", "text": INTERRUPTED}], "isError": True,
                                         "details": {"interrupted": True}, "timestamp": now})
    session.sessions.append_custom("durable-run", {"state": "ended", "outcome": "interrupted", "repaired": len(pending)})
    return len(pending)


def resume(session: Any) -> str:
    why = interrupted(session)
    if why is None:
        return "Nothing to resume: the last run finished."
    repair(session)
    session.run([])
    return f"Resumed the interrupted run ({why})."


def _state(session: Any) -> Dict[str, Any]:
    st = getattr(session, "_durable", None)
    if st is None:
        st = session._durable = {"unsub": None}
    return st


def _setup(session: Any) -> str:
    _unload(session)
    st = _state(session)

    def on_event(ev: Dict[str, Any]) -> None:
        t = ev.get("type")
        try:
            if t == "agent_start":
                session.sessions.append_custom("durable-run", {"state": "started", "at": time.time()})
            elif t == "agent_end":
                session.sessions.append_custom("durable-run", {"state": "ended", "outcome": ev.get("outcome")})
            elif t == "session_start":  # the session file is open now: was its last run cut off?
                why = interrupted(session)
                if why:
                    text = f"The last run in this session was interrupted ({why}). /resume-run continues it."
                    session.diagnostics.append(text)
                    session._notify({"type": "notice", "level": "warning", "message": text})
        except Exception:
            pass
    st["unsub"] = session.subscribe(on_event)
    return ""


def _on_prompt(session: Any, text: str) -> None:
    if interrupted(session):  # a new message on a cut-off run: make the record valid before continuing
        repair(session)
    return None


def _unload(session: Any) -> None:
    st = getattr(session, "_durable", None)
    if st and st.get("unsub"):
        st["unsub"]()
    session._durable = None


def _after_tool(session: Any, tool_name: str, args: Dict[str, Any], result: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    if not result.get("isError"):
        return None
    tool = session.all_tools().get(tool_name)
    if tool is None or not tool.read_only:
        return None  # only calls that are safe to repeat
    opt = session.profile.get("durable") if isinstance(session.profile.get("durable"), dict) else {}
    per_tool = (opt.get("tools") or {}).get(tool_name) if isinstance(opt.get("tools"), dict) else None
    per_tool = per_tool if isinstance(per_tool, dict) else {}   # "durable": {"tools": {"fetch": {"retries": 4, "backoff": 1}}}
    retries = int(per_tool.get("retries", opt.get("retries", 2)))
    backoff = float(per_tool.get("backoff", opt.get("backoff", 0.5)))
    text = "".join(b.get("text", "") for b in result.get("content") or [] if b.get("type") == "text")
    if retries <= 0 or not TRANSIENT.search(text):
        return None
    from ..tools.base import ToolContext, ToolError
    signal = getattr(session, "signal", None)
    for attempt in range(1, retries + 1):
        time.sleep(backoff * (2 ** (attempt - 1)))
        if signal is not None and getattr(signal, "aborted", False):
            return None
        try:
            r = tool.execute(f"retry-{attempt}-{int(time.time() * 1000)}", args, signal, None,
                             ToolContext(cwd=session.cwd, signal=signal, model=session.model, session=session))
        except ToolError as e:
            r = {"content": [{"type": "text", "text": str(e)}], "isError": True}
        except Exception as e:
            r = {"content": [{"type": "text", "text": f"{type(e).__name__}: {e}"}], "isError": True}
        if not r.get("isError"):
            r["details"] = {**(r.get("details") or {}), "retries": attempt} if isinstance(r.get("details"), (dict, type(None))) else r.get("details")
            return r
        text = "".join(b.get("text", "") for b in r.get("content") or [] if b.get("type") == "text")
        if not TRANSIENT.search(text):
            return r
    note = f"\n[failed after {retries} automatic retr{'y' if retries == 1 else 'ies'}]"
    return {**result, "content": list(result.get("content") or []) + [{"type": "text", "text": note}]}


def _commands(session: Any) -> Dict[str, Any]:
    return {"resume-run": ("Continue a run that was cut off (crash, closed terminal)", lambda _a: resume(session))}


CAPABILITIES = [Capability("durable", "Durable runs", "Detect a run that was cut off and resume it from the record; "
                           "retry transient failures of read-only tools.", "Temporal durable execution",
                           prompt=_setup, after_tool=_after_tool, on_prompt=_on_prompt, commands=_commands, unload=_unload, group="host")]
