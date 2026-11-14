"""`board` and `wake`: work that outlives one conversation.

`board` (Hermes kanban, OpenClaw workboard) is a small task board shared by every session of a project. Unlike
`todo`, which is one conversation's checklist, cards stay until someone finishes them: across restarts, between the
terminal and the gateway, and between scheduled jobs and you.

    add(title, body?)            list(status?, everywhere?)        show(id)
    move(id, status, reason?)    comment(id, text)                 remove(id)

Statuses: todo · doing · blocked (with a reason) · review · done. Cards belong to the project folder they were
created in; `everywhere: true` lists all projects. Stored in `<agent-dir>/board.sqlite`. The prompt carries one
line with the number of open cards, nothing more. `/board` shows the board.

`wake` (Letta's Wake) lets the agent schedule its own next turn in this conversation: "check the deploy again in
ten minutes" becomes `wake(in: "10m", note: "check the deploy")`, and when the time comes the note arrives as a new
message with the conversation's full context. It needs a session that stays open (the terminal window); for
unattended or long delays use `cronjob`, which survives restarts.
"""
from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..tools.base import ToolDef, ToolError, text_result
from . import Capability

STATUSES = ("todo", "doing", "blocked", "review", "done")
MARK = {"todo": "[ ]", "doing": "[>]", "blocked": "[!]", "review": "[?]", "done": "[x]"}


def _db(session: Any) -> sqlite3.Connection:
    c = sqlite3.connect(str(Path(session.agent_dir) / "board.sqlite"), timeout=10)
    c.execute("CREATE TABLE IF NOT EXISTS cards (id INTEGER PRIMARY KEY, project TEXT, title TEXT, body TEXT, status TEXT, "
              "reason TEXT, created REAL, updated REAL)")
    c.execute("CREATE TABLE IF NOT EXISTS comments (id INTEGER PRIMARY KEY, card INTEGER, at REAL, text TEXT)")
    return c


def cards(session: Any, status: Optional[str] = None, everywhere: bool = False, include_done: bool = False) -> List[Dict[str, Any]]:
    sql, args = "SELECT id, project, title, body, status, reason, updated FROM cards WHERE 1=1", []
    if not everywhere:
        sql += " AND project = ?"
        args.append(session.cwd)
    if status:
        sql += " AND status = ?"
        args.append(status)
    elif not include_done:
        sql += " AND status != 'done'"
    with _db(session) as c:
        rows = c.execute(sql + " ORDER BY CASE status WHEN 'doing' THEN 0 WHEN 'blocked' THEN 1 WHEN 'review' THEN 2 WHEN 'todo' THEN 3 ELSE 4 END, id", args).fetchall()
    return [dict(zip(("id", "project", "title", "body", "status", "reason", "updated"), r)) for r in rows]


def render(items: List[Dict[str, Any]], everywhere: bool = False) -> str:
    if not items:
        return "The board is empty."
    rows = []
    for c in items:
        rows.append(f"{MARK[c['status']]} #{c['id']} {c['title']}" + (f"  — blocked: {c['reason']}" if c["status"] == "blocked" and c["reason"] else "")
                    + (f"  ({c['project']})" if everywhere else ""))
    return "\n".join(rows)


def _tools(session: Any) -> Dict[str, ToolDef]:
    def get(c: sqlite3.Connection, cid: Any) -> Any:
        try:
            row = c.execute("SELECT id, title, body, status, reason, project FROM cards WHERE id = ?", (int(str(cid).lstrip("#")),)).fetchone()
        except ValueError:
            row = None
        if row is None:
            raise ToolError(f"no card {cid}")
        return row

    def execute(_id, p, signal=None, on_update=None, ctx=None):
        action = str(p.get("action") or "list").lower()
        now = time.time()
        if action == "list":
            status = p.get("status")
            if status and status not in STATUSES:
                raise ToolError(f"status must be one of: {', '.join(STATUSES)}")
            items = cards(session, status, bool(p.get("everywhere")), include_done=status == "done")
            return text_result(render(items, bool(p.get("everywhere"))), {"cards": len(items)})
        if action == "add":
            title = str(p.get("title") or "").strip()
            if not title:
                raise ToolError("title is required")
            with _db(session) as c:
                cur = c.execute("INSERT INTO cards (project, title, body, status, reason, created, updated) VALUES (?, ?, ?, 'todo', '', ?, ?)",
                                (session.cwd, title[:200], str(p.get("body") or ""), now, now))
            return text_result(f"Added card #{cur.lastrowid}: {title[:200]}", {"id": cur.lastrowid})
        with _db(session) as c:
            row = get(c, p.get("id"))
            cid = row[0]
            if action == "show":
                notes = c.execute("SELECT at, text FROM comments WHERE card = ? ORDER BY id", (cid,)).fetchall()
                text = f"#{cid} {row[1]}  [{row[3]}]" + (f"  blocked: {row[4]}" if row[4] else "") + (f"\n{row[2]}" if row[2] else "")
                text += "".join(f"\n  {time.strftime('%Y-%m-%d %H:%M', time.localtime(at))}  {t}" for at, t in notes)
                return text_result(text, {"id": cid, "status": row[3]})
            if action == "move":
                status = str(p.get("status") or "")
                if status not in STATUSES:
                    raise ToolError(f"status must be one of: {', '.join(STATUSES)}")
                reason = str(p.get("reason") or "").strip()
                if status == "blocked" and not reason:
                    raise ToolError("a blocked card needs a reason: what is it waiting for?")
                c.execute("UPDATE cards SET status = ?, reason = ?, updated = ? WHERE id = ?", (status, reason if status == "blocked" else "", now, cid))
                return text_result(f"#{cid} {row[1]}: {row[3]} -> {status}" + (f" ({reason})" if reason else ""), {"id": cid, "status": status})
            if action == "comment":
                text = str(p.get("text") or "").strip()
                if not text:
                    raise ToolError("text is required")
                c.execute("INSERT INTO comments (card, at, text) VALUES (?, ?, ?)", (cid, now, text[:2000]))
                c.execute("UPDATE cards SET updated = ? WHERE id = ?", (now, cid))
                return text_result(f"Commented on #{cid}.", {"id": cid})
            if action == "remove":
                c.execute("DELETE FROM cards WHERE id = ?", (cid,))
                c.execute("DELETE FROM comments WHERE card = ?", (cid,))
                return text_result(f"Removed #{cid} {row[1]}.", {"id": cid})
        raise ToolError("action must be add, list, show, move, comment or remove")

    return {"board": ToolDef(
        name="board", label="Board",
        description="The project's task board, shared across sessions (use `todo` for this conversation's own steps). "
                    "Actions: add(title, body?), list(status?, everywhere?), show(id), move(id, status: todo|doing|blocked|"
                    "review|done, reason? required for blocked), comment(id, text), remove(id). Check it when starting "
                    "work on a project, and keep cards current as you finish or get stuck.",
        parameters={"type": "object", "properties": {
            "action": {"type": "string", "enum": ["add", "list", "show", "move", "comment", "remove"]},
            "id": {"type": "string"}, "title": {"type": "string"}, "body": {"type": "string"},
            "status": {"type": "string", "enum": list(STATUSES)}, "reason": {"type": "string"}, "text": {"type": "string"},
            "everywhere": {"type": "boolean"}}, "required": ["action"]},
        execute=execute, prompt_snippet="Track work on the project's shared task board")}


def _board_prompt(session: Any) -> str:
    try:
        items = cards(session)
    except sqlite3.Error:
        return ""
    if not items:
        return ""
    doing = [c for c in items if c["status"] == "doing"]
    return (f"Task board: {len(items)} open card(s) for this project" + (f", in progress: {'; '.join('#%d %s' % (c['id'], c['title'][:50]) for c in doing[:3])}" if doing else "")
            + ". See them with the board tool.")


# ------------------------------------------------------------------------------------------- wake
MAX_WAKES = 5


def _wake_tools(session: Any) -> Dict[str, ToolDef]:
    timers: List[Dict[str, Any]] = []
    session._wakes = timers

    def fire(entry: Dict[str, Any]) -> None:
        if entry.get("cancelled"):
            return
        entry["fired"] = True
        text = f"[Wake-up you scheduled {entry['label']} ago] {entry['note']}"
        try:
            if getattr(session, "is_streaming", False):
                session.follow_up(text)
            else:
                session.prompt(text, source="wake")
        except Exception as e:
            session.diagnostics.append(f"wake: {type(e).__name__}: {e}")

    def execute(_id, p, signal=None, on_update=None, ctx=None):
        action = str(p.get("action") or "set").lower()
        pending = [t for t in timers if not t.get("fired") and not t.get("cancelled")]
        if action == "list":
            return text_result("\n".join(f"in {max(0, t['at'] - time.time()):.0f}s: {t['note']}" for t in pending) or "No wake-ups pending.",
                               {"pending": len(pending)})
        if action == "cancel":
            for t in pending:
                t["cancelled"] = True
                t["timer"].cancel()
            return text_result(f"Cancelled {len(pending)} wake-up(s).", {"cancelled": len(pending)})
        note = str(p.get("note") or "").strip()
        if not note:
            raise ToolError("note is required: what should you do when you wake up?")
        if not getattr(session.ui, "has_ui", False) and not getattr(session, "allow_wake", False):
            raise ToolError("wake needs a session that stays open (the terminal window). Here, schedule it with cronjob instead "
                            "(schedule: `in 10m`).")
        from .scheduler import parse_schedule
        when = str(p.get("in") or "").strip()
        try:
            sched = parse_schedule(f"in {when}" if not when.lower().startswith("in ") else when)
        except ValueError:
            raise ToolError("`in` must be a delay of at least one minute, such as `2m`, `15m` or `1h`") from None
        delay = max(1.0, sched["at"] - time.time())
        if delay > 6 * 3600:
            raise ToolError("wake is for delays up to 6 hours; use cronjob for longer ones (it survives restarts)")
        if len(pending) >= MAX_WAKES:
            raise ToolError(f"{MAX_WAKES} wake-ups are already pending; cancel one first")
        entry: Dict[str, Any] = {"note": note[:500], "at": time.time() + delay, "label": when.lower().replace("in ", "")}
        entry["timer"] = threading.Timer(delay, fire, args=(entry,))
        entry["timer"].daemon = True
        entry["timer"].start()
        timers.append(entry)
        return text_result(f"I will wake up in {entry['label']} to: {entry['note']}\n(Only while this window stays open.)", {"seconds": round(delay)})

    return {"wake": ToolDef(
        name="wake", label="Wake",
        description="Schedule your own next turn in this conversation: wake(in: '10m', note: 'check whether the deploy "
                    "finished'). When the time comes the note arrives as a new message and you continue with full context. "
                    "Use it instead of sleeping or polling in a loop. Also: action=list, action=cancel.",
        parameters={"type": "object", "properties": {
            "action": {"type": "string", "enum": ["set", "list", "cancel"]},
            "in": {"type": "string", "description": "delay: 2m, 15m, 1h (at least one minute)"}, "note": {"type": "string"}}},
        execute=execute, prompt_snippet="Schedule your own follow-up turn")}


def _wake_unload(session: Any) -> None:
    for t in getattr(session, "_wakes", None) or []:
        t["cancelled"] = True
        t["timer"].cancel()
    session._wakes = None


CAPABILITIES = [
    Capability("board", "Task board", "A project task board shared across sessions: todo, doing, blocked, review, done.",
               "Hermes kanban, OpenClaw workboard", tools=_tools, prompt=_board_prompt,
               commands=lambda s: {"board": ("Show the project's task board (`all` includes done and other projects)",
                                             lambda a: render(cards(s, everywhere=a.strip() == "all", include_done=a.strip() == "all"), a.strip() == "all"))}),
    Capability("wake", "Self-scheduled follow-ups", "The agent can schedule its own next turn in this conversation.",
               "Letta Wake", tools=_wake_tools, unload=_wake_unload),
]
