"""`memory-archive`: unbounded long-term memory that is searched, not carried (Letta's archival memory).

Letta (MemGPT) splits memory in tiers. Omni maps them like this:

    core memory     small, always in the prompt, edited by the agent       ->  `memory` capability (MEMORY.md / USER.md)
    recall memory   the conversation history, searched on demand           ->  `session-search`
    archival memory unlimited facts and documents, searched on demand      ->  this capability

`archival_memory_insert` stores a passage (with optional tags); `archival_memory_search` finds passages by full-text
query. Nothing from the archive is in the prompt except one line with the passage count, so it can grow without
costing context. Storage: `<agent-dir>/memories/archive.sqlite` (SQLite FTS5, ranked by BM25).
"""
from __future__ import annotations

import re
import sqlite3
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..tools.base import ToolDef, ToolError, text_result
from . import Capability

MAX_PASSAGE = 8000


def db_path(session: Any) -> Path:
    return Path(session.agent_dir) / "memories" / "archive.sqlite"


def connect(session: Any) -> sqlite3.Connection:
    p = db_path(session)
    p.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(str(p), timeout=10)
    c.execute("CREATE TABLE IF NOT EXISTS passages (id INTEGER PRIMARY KEY, created REAL, tags TEXT, source TEXT, content TEXT)")
    try:
        c.execute("CREATE VIRTUAL TABLE IF NOT EXISTS passages_fts USING fts5(content, tags, content='passages', content_rowid='id')")
        c.execute("CREATE TRIGGER IF NOT EXISTS passages_ai AFTER INSERT ON passages BEGIN "
                  "INSERT INTO passages_fts(rowid, content, tags) VALUES (new.id, new.content, new.tags); END")
        c.execute("CREATE TRIGGER IF NOT EXISTS passages_ad AFTER DELETE ON passages BEGIN "
                  "INSERT INTO passages_fts(passages_fts, rowid, content, tags) VALUES ('delete', old.id, old.content, old.tags); END")
    except sqlite3.OperationalError:
        pass  # SQLite built without FTS5: search falls back to LIKE
    return c


def count(session: Any) -> int:
    if not db_path(session).exists():
        return 0
    try:
        with connect(session) as c:
            return int(c.execute("SELECT COUNT(*) FROM passages").fetchone()[0])
    except sqlite3.Error:
        return 0


def insert(session: Any, content: str, tags: Optional[List[str]] = None, source: str = "") -> int:
    with connect(session) as c:
        dup = c.execute("SELECT id FROM passages WHERE content = ?", (content,)).fetchone()
        if dup:
            return -int(dup[0])
        cur = c.execute("INSERT INTO passages (created, tags, source, content) VALUES (?, ?, ?, ?)",
                        (time.time(), " ".join(tags or []), source, content))
        return int(cur.lastrowid)


def search(session: Any, query: str, limit: int = 5, tag: Optional[str] = None) -> List[Dict[str, Any]]:
    words = re.findall(r"[\w\-]+", query, re.UNICODE)
    if not words:
        return []
    with connect(session) as c:
        rows: List[Any] = []
        try:
            match = " OR ".join('"' + w.replace('"', "") + '"' for w in words)
            sql = ("SELECT p.id, p.created, p.tags, p.content, bm25(passages_fts) AS score FROM passages_fts "
                   "JOIN passages p ON p.id = passages_fts.rowid WHERE passages_fts MATCH ? ")
            args: List[Any] = [match]
            if tag:
                sql += "AND (' ' || p.tags || ' ') LIKE ? "
                args.append(f"% {tag} %")
            rows = c.execute(sql + "ORDER BY score LIMIT ?", args + [limit]).fetchall()
        except sqlite3.OperationalError:
            like = " AND ".join("content LIKE ?" for _ in words)
            rows = c.execute(f"SELECT id, created, tags, content, 0 FROM passages WHERE {like} ORDER BY created DESC LIMIT ?",
                             [f"%{w}%" for w in words] + [limit]).fetchall()
    return [{"id": r[0], "created": r[1], "tags": r[2], "content": r[3]} for r in rows]


def _tools(session: Any) -> Dict[str, ToolDef]:
    def do_insert(_id, p, signal=None, on_update=None, ctx=None):
        content = str(p.get("content") or "").strip()
        if not content:
            raise ToolError("content is required")
        if len(content) > MAX_PASSAGE:
            raise ToolError(f"a passage is at most {MAX_PASSAGE} characters; split it ({len(content)} given)")
        tags = [str(t).strip().lower().replace(" ", "-") for t in p.get("tags") or [] if str(t).strip()]
        rid = insert(session, content, tags, source=session.cwd)
        if rid < 0:
            return text_result(f"Already archived as passage {-rid}.", {"id": -rid, "duplicate": True})
        return text_result(f"Archived as passage {rid} ({len(content)} chars" + (f", tags: {', '.join(tags)}" if tags else "") + ").",
                           {"id": rid})

    def do_search(_id, p, signal=None, on_update=None, ctx=None):
        query = str(p.get("query") or "").strip()
        if not query:
            raise ToolError("query is required")
        hits = search(session, query, max(1, min(int(p.get("limit") or 5), 20)), p.get("tag"))
        if not hits:
            return text_result(f"Nothing in archival memory matches: {query}", {"results": 0})
        rows = []
        for h in hits:
            when = time.strftime("%Y-%m-%d", time.localtime(h["created"]))
            body = h["content"] if len(h["content"]) <= 1200 else h["content"][:1200] + "…"
            rows.append(f"[{h['id']}] {when}" + (f" #{h['tags'].replace(' ', ' #')}" if h["tags"] else "") + f"\n{body}")
        return text_result("\n\n".join(rows), {"results": len(hits)})

    return {
        "archival_memory_insert": ToolDef(
            name="archival_memory_insert", label="Archive",
            description="Store a passage in long-term archival memory: detailed facts, decisions with their "
                        "reasons, reference material, things too long for core memory. Write it self-contained "
                        "(it will be read without this conversation). Add a few `tags`.",
            parameters={"type": "object", "properties": {
                "content": {"type": "string"}, "tags": {"type": "array", "items": {"type": "string"}}}, "required": ["content"]},
            execute=do_insert, prompt_snippet="Store long-term facts in archival memory"),
        "archival_memory_search": ToolDef(
            name="archival_memory_search", label="Archive search",
            description="Search archival memory by keywords (full-text, best matches first). Use it before "
                        "answering questions about past decisions, preferences or project facts you do not see "
                        "in the conversation.",
            parameters={"type": "object", "properties": {
                "query": {"type": "string"}, "limit": {"type": "integer"}, "tag": {"type": "string"}}, "required": ["query"]},
            execute=do_search, read_only=True),
    }


def _prompt(session: Any) -> str:
    n = count(session)
    return f"Archival memory holds {n} passage(s); search it with archival_memory_search when something may have been stored." if n else ""


CAPABILITIES = [Capability("memory-archive", "Archival memory", "Unbounded long-term memory that is searched on demand "
                           "instead of carried in the prompt.", "Letta (MemGPT) archival memory", tools=_tools, prompt=_prompt)]
