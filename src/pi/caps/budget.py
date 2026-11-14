"""`budget`: limits that stop a run before it gets expensive (Temporal's execution limits, Langfuse's cost view, as a
guard instead of a report).

    "budget": {"session_usd": 2.00, "run_tool_calls": 60, "run_minutes": 20}

- `session_usd`      total model cost of this session (from the provider's token counts and the catalog prices)
- `run_tool_calls`   tool calls since your last message
- `run_minutes`      wall-clock time since your last message

When a limit is reached, the next tool call is refused and the model is told to stop and report where it is, so it
ends with a summary rather than being cut off. You see a notice. `/budget` shows the counters; `/budget extend`
gives the current run a fresh allowance (and raises the session limit by its own amount).

Subscription sign-ins report zero cost per token, so `session_usd` only bites with API-key providers; the other
two limits work everywhere. Defaults when the capability is on without settings: 80 tool calls and 30 minutes per
run, no cost limit.
"""
from __future__ import annotations

import time
from typing import Any, Dict, Optional

from . import Capability

DEFAULTS = {"run_tool_calls": 80, "run_minutes": 30}


def _state(session: Any) -> Dict[str, Any]:
    st = getattr(session, "_budget", None)
    if st is None:
        opt = session.profile.get("budget") if isinstance(session.profile.get("budget"), dict) else None
        st = session._budget = {"limits": dict(opt if opt is not None else DEFAULTS), "calls": 0, "started": time.time(),
                                "extra_usd": 0.0, "notified": False, "unsub": None}
    return st


def session_cost(session: Any) -> float:
    total = 0.0
    for e in session.sessions.get_branch():
        if e.get("type") == "message" and e["message"].get("role") == "assistant":
            total += float(((e["message"].get("usage") or {}).get("cost") or {}).get("total") or 0.0)
    return total


def exceeded(session: Any) -> Optional[str]:
    st = _state(session)
    lim = st["limits"]
    if lim.get("run_tool_calls") and st["calls"] >= int(lim["run_tool_calls"]):
        return f"{st['calls']} tool calls in this run (limit {int(lim['run_tool_calls'])})"
    if lim.get("run_minutes") and time.time() - st["started"] >= float(lim["run_minutes"]) * 60:
        return f"this run has taken {(time.time() - st['started']) / 60:.0f} minutes (limit {float(lim['run_minutes']):g})"
    if lim.get("session_usd"):
        cost = session_cost(session)
        if cost >= float(lim["session_usd"]) + st["extra_usd"]:
            return f"the session has cost ${cost:.2f} (limit ${float(lim['session_usd']) + st['extra_usd']:.2f})"
    return None


def _gate(session: Any, tool_name: str, args: Dict[str, Any]) -> Optional[str]:
    st = _state(session)
    why = exceeded(session)
    if why is None:
        st["calls"] += 1
        return None
    if not st["notified"]:
        st["notified"] = True
        session._notify({"type": "notice", "level": "warning", "message": f"Budget reached: {why}. /budget extend to let it continue."})
    return (f"Budget reached: {why}. Do not call more tools. Stop now and tell the user what is done, what is left, and "
            "that they can run /budget extend to continue.")


def _setup(session: Any) -> str:
    _unload(session)
    st = _state(session)

    def on_event(ev: Dict[str, Any]) -> None:
        if ev.get("type") == "agent_start" and getattr(session, "_request_reason", "user") == "user":
            st.update(calls=0, started=time.time(), notified=False)  # a new message from you starts a new run
    st["unsub"] = session.subscribe(on_event)
    return ""


def _unload(session: Any) -> None:
    st = getattr(session, "_budget", None)
    if st and st.get("unsub"):
        st["unsub"]()
    session._budget = None


def _commands(session: Any) -> Dict[str, Any]:
    def run(args: str) -> str:
        st = _state(session)
        lim = st["limits"]
        if args.strip() == "extend":
            st.update(calls=0, started=time.time(), notified=False)
            if lim.get("session_usd"):
                st["extra_usd"] += float(lim["session_usd"])
            return "Extended: the run has a fresh allowance" + (f", and the session limit is now ${float(lim['session_usd']) + st['extra_usd']:.2f}." if lim.get("session_usd") else ".")
        rows = [f"tool calls this run: {st['calls']}" + (f" / {int(lim['run_tool_calls'])}" if lim.get("run_tool_calls") else " (no limit)"),
                f"run time: {(time.time() - st['started']) / 60:.1f} min" + (f" / {float(lim['run_minutes']):g}" if lim.get("run_minutes") else " (no limit)"),
                f"session cost: ${session_cost(session):.2f}" + (f" / ${float(lim['session_usd']) + st['extra_usd']:.2f}" if lim.get("session_usd") else " (no limit)")]
        why = exceeded(session)
        return "\n".join(rows) + (f"\nReached: {why}. /budget extend continues." if why else "")
    return {"budget": ("Show the run's limits, or `extend` them", run)}


CAPABILITIES = [Capability("budget", "Budget limits", "Stop a run at a cost, tool-call or time limit and have the model "
                           "report where it is.", "Temporal execution limits, Langfuse cost tracking", prompt=_setup,
                           gate=_gate, commands=_commands, unload=_unload)]
