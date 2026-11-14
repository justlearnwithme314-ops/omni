"""`advisor` and `watch-pr`: a second opinion, and eyes on a pull request.

`advisor` (OpenClaw's advisor): every few turns a separate, short model call reads the recent conversation as a
reviewer and looks for trouble the working agent tends to miss: drifting from what was asked, repeating a failing
approach, claiming success without checking, growing scope. If it finds something, the advice is shown to you and
handed to the agent with your next message; if all is well it says nothing. `/advise` runs a review now.

    "advisor": {"every": 6, "model": "provider/model-id"}     # profile options; both optional

It costs one model call per review (up to about 10,000 characters of transcript in, at most 400 tokens out).

`watch-pr` (Letta's watch-PR): `watch_pr(pr)` follows a pull request in the background through the GitHub CLI and
wakes the agent when something changes: checks failing or all passing, a review decision, new comments, merge or
close. The agent then continues in this conversation with the change described. It polls once a minute while the
session is open and stops when the PR is merged or closed. Needs `gh` (or `OMNI_GH`).
"""
from __future__ import annotations

import json
import subprocess
import threading
import time
from typing import Any, Dict, List, Optional

from ..tools.base import ToolDef, ToolError, text_result
from . import Capability

ADVISOR = """You are a senior reviewer looking over an AI agent's shoulder. Read the recent conversation between the USER and the agent (ASSISTANT) and decide whether the agent needs a course correction.

Look for: drifting from what the user asked; repeating an approach that already failed; claiming something works without having checked it; doing more than was asked; missing an obvious simpler path; ignoring an instruction or an error.

If the work is on track, reply with exactly: OK
Otherwise reply with at most three short, concrete sentences addressed to the agent, saying what to do differently. No praise, no summary."""
MAX_TRANSCRIPT = 10_000


def _options(session: Any) -> Dict[str, Any]:
    o = session.profile.get("advisor")
    return o if isinstance(o, dict) else {}


def advise(session: Any) -> str:
    """One review. Returns the advice, or '' when the work is on track or no review was possible."""
    from ..ai.stream import complete
    from .reflect import transcript
    model = session.model
    want = _options(session).get("model")
    if want and "/" in str(want):
        provider, _, mid = str(want).partition("/")
        try:
            model = session.registry.find(provider, mid) or model
        except Exception:
            pass
    if model is None:
        return ""
    convo = transcript(session, MAX_TRANSCRIPT)
    if len(convo) < 200:
        return ""
    msg = complete(model, {"systemPrompt": ADVISOR, "tools": [],
                           "messages": [{"role": "user", "content": convo, "timestamp": int(time.time() * 1000)}]},
                   {"apiKey": session.auth.get_api_key(model["provider"]), "maxTokens": 400})
    if msg.get("stopReason") == "error":
        return ""
    text = "".join(b.get("text", "") for b in msg.get("content") or [] if b.get("type") == "text").strip()
    if not text or text.upper().rstrip(".!") == "OK":
        return ""
    session.sessions.append_custom("advisor", {"advice": text[:1000]})
    return text[:1000]


def _advisor_setup(session: Any) -> str:
    _advisor_unload(session)
    every = max(2, int(_options(session).get("every") or 6))
    state = {"turns": 0}
    session._advice = None

    def on_event(ev: Dict[str, Any]) -> None:
        if ev.get("type") != "agent_end":
            return
        state["turns"] += 1
        if state["turns"] % every:
            return
        try:
            text = advise(session)
        except Exception:
            return
        if text:
            session._advice = text
            session._notify({"type": "notice", "level": "info", "message": f"Advisor: {text}"})
    session._advisor_unsub = session.subscribe(on_event)
    return ""


def _advisor_unload(session: Any) -> None:
    unsub = getattr(session, "_advisor_unsub", None)
    if unsub:
        unsub()
    session._advisor_unsub = None


def _advisor_on_prompt(session: Any, text: str) -> Optional[str]:
    advice = getattr(session, "_advice", None)
    if not advice or text.lstrip().startswith("/"):
        return None
    session._advice = None
    return f"[Advisor review of your recent work - take it into account] {advice}"


def _advise_now(session: Any) -> str:
    text = advise(session)
    if not text:
        return "Advisor: nothing to correct (or not enough conversation yet)."
    session._advice = text
    return f"Advisor: {text}\n(The agent gets this with your next message.)"


# ------------------------------------------------------------------------------------------- watch-pr
FIELDS = "number,title,state,mergeable,reviewDecision,statusCheckRollup,comments,url"
POLL_SECONDS = 60
_FAILED = ("FAILURE", "ERROR", "CANCELLED", "TIMED_OUT", "ACTION_REQUIRED", "STARTUP_FAILURE")


def summarize(pr: Dict[str, Any]) -> Dict[str, Any]:
    checks = pr.get("statusCheckRollup") or []
    failed = sorted({str(c.get("name") or c.get("context") or "check") for c in checks
                     if str(c.get("conclusion") or c.get("state") or "").upper() in _FAILED})
    pending = sum(1 for c in checks if str(c.get("status") or c.get("state") or "").upper() in ("QUEUED", "IN_PROGRESS", "PENDING", "WAITING", "EXPECTED")
                  and not c.get("conclusion"))
    return {"state": str(pr.get("state") or "").upper(), "review": str(pr.get("reviewDecision") or ""),
            "mergeable": str(pr.get("mergeable") or ""), "failed": failed,
            "checks": "failing" if failed else "pending" if pending else "passing" if checks else "none",
            "comments": len(pr.get("comments") or [])}


def describe(old: Optional[Dict[str, Any]], new: Dict[str, Any], pr: Dict[str, Any]) -> List[str]:
    if old is None:
        return []
    out = []
    if new["state"] != old["state"]:
        out.append(f"it is now {new['state'].lower()}")
    if new["checks"] != old["checks"] or new["failed"] != old["failed"]:
        out.append("checks are failing: " + ", ".join(new["failed"][:6]) if new["failed"] else f"checks are {new['checks']}")
    if new["review"] != old["review"] and new["review"]:
        out.append(f"review decision: {new['review'].lower().replace('_', ' ')}")
    if new["mergeable"] != old["mergeable"] and new["mergeable"] == "CONFLICTING":
        out.append("it has merge conflicts")
    if new["comments"] > old["comments"]:
        latest = (pr.get("comments") or [])[-1]
        who = (latest.get("author") or {}).get("login") or "someone"
        out.append(f"{new['comments'] - old['comments']} new comment(s), latest from {who}: {str(latest.get('body') or '').strip()[:300]}")
    return out


def fetch_pr(session: Any, number: str) -> Dict[str, Any]:
    from .github import INSTALL, gh
    exe = gh()
    if exe is None:
        raise ToolError(INSTALL)
    try:
        r = subprocess.run(exe + ["pr", "view"] + ([number] if number else []) + ["--json", FIELDS], cwd=session.cwd,
                           capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise ToolError(f"gh failed: {e}") from None
    if r.returncode != 0:
        raise ToolError((r.stderr or r.stdout or "gh pr view failed").strip()[:400])
    try:
        return json.loads(r.stdout)
    except ValueError:
        raise ToolError("gh returned something that is not JSON") from None


def check_once(session: Any, watch: Dict[str, Any]) -> Optional[str]:
    """Poll one watched PR. Returns the message sent to the agent when something changed."""
    pr = fetch_pr(session, watch["number"])
    new = summarize(pr)
    changes = describe(watch.get("last"), new, pr)
    watch["last"] = new
    if new["state"] in ("MERGED", "CLOSED"):
        watch["done"] = True
    if not changes:
        return None
    text = f"[Pull request #{pr.get('number', watch['number'])} \"{str(pr.get('title') or '')[:80]}\" changed] " + "; ".join(changes) + "."
    if watch.get("note"):
        text += f" You asked to: {watch['note']}"
    try:
        if getattr(session, "is_streaming", False):
            session.follow_up(text)
        else:
            session.prompt(text, source="watch-pr")
    except Exception as e:
        session.diagnostics.append(f"watch-pr: {type(e).__name__}: {e}")
    return text


def _watch_tools(session: Any) -> Dict[str, ToolDef]:
    watches: Dict[str, Dict[str, Any]] = {}
    stop = threading.Event()
    state = session._pr_watch = {"watches": watches, "stop": stop, "thread": None}

    def loop() -> None:
        while not stop.wait(POLL_SECONDS):
            for w in [w for w in list(watches.values()) if not w.get("done")]:
                try:
                    check_once(session, w)
                except ToolError as e:
                    w["error"] = str(e)
            if all(w.get("done") for w in watches.values()):
                break
        state["thread"] = None

    def execute(_id, p, signal=None, on_update=None, ctx=None):
        action = str(p.get("action") or "start").lower()
        number = str(p.get("pr") or "").lstrip("#").strip()
        if action == "list":
            rows = [f"#{w['shown']}  {'finished' if w.get('done') else 'watching'}  checks {w['last']['checks']}, {w['last']['state'].lower()}"
                    + (f"  (last error: {w['error']})" if w.get("error") else "") for w in watches.values()]
            return text_result("\n".join(rows) or "Not watching any pull request.", {"count": len(rows)})
        if action == "stop":
            hit = [k for k in watches if not number or k == number]
            for k in hit:
                del watches[k]
            return text_result(f"Stopped watching {len(hit)} pull request(s).", {"stopped": len(hit)})
        if action != "start":
            raise ToolError("action must be start, list or stop")
        pr = fetch_pr(session, number)
        key = str(pr.get("number") or number)
        watch = {"number": key, "shown": key, "note": str(p.get("note") or "").strip()[:300], "last": summarize(pr)}
        watch["done"] = watch["last"]["state"] in ("MERGED", "CLOSED")
        if watch["done"]:
            return text_result(f"#{key} is already {watch['last']['state'].lower()}; nothing to watch.", {"watching": False})
        watches[key] = watch
        interactive = getattr(session.ui, "has_ui", False) or getattr(session, "allow_wake", False)
        if interactive and state["thread"] is None:
            t = threading.Thread(target=loop, daemon=True, name="omni-watch-pr")
            state["thread"] = t
            t.start()
        last = watch["last"]
        return text_result(f"Watching #{key} \"{str(pr.get('title') or '')[:80]}\": checks {last['checks']}"
                           + (f" ({', '.join(last['failed'][:4])})" if last["failed"] else "")
                           + (f", review {last['review'].lower()}" if last["review"] else "") + "."
                           + ("" if interactive else " This session is not interactive, so nothing will poll it; use a cronjob instead."),
                           {"watching": interactive, "checks": last["checks"]})

    return {"watch_pr": ToolDef(
        name="watch_pr", label="Watch PR",
        description="Follow a GitHub pull request in the background and be woken in this conversation when its checks fail "
                    "or pass, a review arrives, someone comments, or it is merged or closed. watch_pr(pr?: number, default "
                    "the current branch's PR; note?: what to do when it changes). Also action=list, action=stop. Use it "
                    "after opening a PR instead of polling.",
        parameters={"type": "object", "properties": {
            "action": {"type": "string", "enum": ["start", "list", "stop"]}, "pr": {"type": "string"}, "note": {"type": "string"}}},
        execute=execute, prompt_snippet="Watch a pull request for changes")}


def _watch_unload(session: Any) -> None:
    st = getattr(session, "_pr_watch", None)
    if st:
        st["stop"].set()
    session._pr_watch = None


CAPABILITIES = [
    Capability("advisor", "Advisor", "A periodic second-opinion review of the agent's recent work; advice reaches the agent with "
               "your next message.", "OpenClaw advisor", prompt=_advisor_setup, on_prompt=_advisor_on_prompt, unload=_advisor_unload,
               commands=lambda s: {"advise": ("Review the recent work now", lambda _a: _advise_now(s))}),
    Capability("watch-pr", "Pull request watcher", "Follow a pull request and wake the agent when checks, reviews or comments "
               "change.", "Letta watch-PR", tools=_watch_tools, unload=_watch_unload),
]
