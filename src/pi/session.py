"""JSONL session tree, file-compatible with Pi (version 3). Port of core/session-manager.ts essentials.

File:   <agent-dir>/sessions/--<cwd with / \\ : replaced by ->--/<timestamp>_<uuid>.jsonl
Line 1: {"type":"session","version":3,"id","timestamp","cwd"[,"parentSession"]}
Then entries with {type,id,parentId,timestamp,...}: message, model_change, thinking_level_change, compaction,
branch_summary, custom, custom_message, label, session_info, context_edit, usage.
"""
from __future__ import annotations

import json
import os
import secrets
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

SESSION_VERSION = 3

COMPACTION_SUMMARY_PREFIX = "The conversation history before this point was compacted into the following summary:\n\n<summary>\n"
COMPACTION_SUMMARY_SUFFIX = "\n</summary>"
BRANCH_SUMMARY_PREFIX = "The following is a summary of a branch that this conversation came back from:\n\n<summary>\n"
BRANCH_SUMMARY_SUFFIX = "</summary>"


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.") + f"{int(time.time() * 1000) % 1000:03d}Z"


def now_ms() -> int:
    return int(time.time() * 1000)


def session_dir_for(cwd: str, agent_dir: Path) -> Path:
    resolved = os.path.abspath(cwd)
    safe = "--" + resolved.lstrip("/\\").replace("/", "-").replace("\\", "-").replace(":", "-") + "--"
    return Path(agent_dir) / "sessions" / safe


def bash_execution_to_text(m: Dict[str, Any]) -> str:
    text = f"Ran `{m['command']}`\n"
    text += f"```\n{m['output']}\n```" if m.get("output") else "(no output)"
    if m.get("cancelled"):
        text += "\n\n(command cancelled)"
    elif m.get("exitCode") not in (None, 0):
        text += f"\n\nCommand exited with code {m['exitCode']}"
    if m.get("truncated") and m.get("fullOutputPath"):
        text += f"\n\n[Output truncated. Full output: {m['fullOutputPath']}]"
    return text


def convert_to_llm(messages: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Pi's convertToLlm: custom roles become user messages; system messages are dropped (prompt is sent separately)."""
    out = []
    for m in messages:
        role = m.get("role")
        if role == "bashExecution":
            if m.get("excludeFromContext"):
                continue
            out.append({"role": "user", "content": [{"type": "text", "text": bash_execution_to_text(m)}], "timestamp": m.get("timestamp")})
        elif role == "custom":
            c = m.get("content")
            out.append({"role": "user", "content": [{"type": "text", "text": c}] if isinstance(c, str) else c, "timestamp": m.get("timestamp")})
        elif role == "branchSummary":
            out.append({"role": "user", "content": [{"type": "text", "text": BRANCH_SUMMARY_PREFIX + m["summary"] + BRANCH_SUMMARY_SUFFIX}],
                        "timestamp": m.get("timestamp")})
        elif role == "compactionSummary":
            out.append({"role": "user", "content": [{"type": "text", "text": COMPACTION_SUMMARY_PREFIX + m["summary"] + COMPACTION_SUMMARY_SUFFIX}],
                        "timestamp": m.get("timestamp")})
        elif role in ("user", "assistant", "toolResult"):
            out.append(m)
    return out


class SessionManager:
    """Append-only session tree. `persist=False` keeps everything in memory (`--no-session`)."""

    def __init__(self, cwd: str, agent_dir: Path, session_dir: Optional[Path] = None, persist: bool = True):
        self.cwd = os.path.abspath(cwd)
        self.agent_dir = Path(agent_dir)
        self.session_dir = Path(session_dir) if session_dir else session_dir_for(self.cwd, self.agent_dir)
        self.persist = persist
        self.header: Dict[str, Any] = {}
        self.entries: List[Dict[str, Any]] = []
        self.by_id: Dict[str, Dict[str, Any]] = {}
        self.leaf_id: Optional[str] = None
        self.file: Optional[Path] = None
        self._flushed = False
        self.listeners: List[Any] = []  # callables(event, payload) - extension host mirror

    # ---------------------------------------------------------------------------------------- lifecycle
    def new_session(self, parent_session: Optional[str] = None, session_id: Optional[str] = None) -> None:
        sid = session_id or str(uuid.uuid4())
        ts = now_iso()
        self.header = {"type": "session", "version": SESSION_VERSION, "id": sid, "timestamp": ts, "cwd": self.cwd}
        if parent_session:
            self.header["parentSession"] = parent_session
        self.entries, self.by_id, self.leaf_id = [], {}, None
        stamp = ts.replace(":", "-").replace(".", "-")
        self.file = self.session_dir / f"{stamp}_{sid}.jsonl" if self.persist else None
        self._flushed = False
        self._notify("reset", None)

    def open(self, path: str) -> None:
        p = Path(path)
        lines = [l for l in p.read_text(encoding="utf-8").splitlines() if l.strip()]
        items = []
        for l in lines:
            try:
                items.append(json.loads(l))
            except json.JSONDecodeError:
                continue
        if not items or items[0].get("type") != "session":
            raise ValueError(f"Not a Pi session file: {path}")
        self.header = items[0]
        self.entries = items[1:]
        self._migrate()
        self.by_id = {e["id"]: e for e in self.entries if "id" in e}
        self.leaf_id = self.entries[-1]["id"] if self.entries else None
        self.file = p
        self.cwd = self.header.get("cwd", self.cwd)
        self._flushed = True
        self._notify("reset", None)

    def _migrate(self) -> None:
        """v1 (linear) -> v2 (tree) -> v3 (hookMessage role renamed to custom)."""
        prev = None
        for e in self.entries:
            if "id" not in e:
                e["id"] = secrets.token_hex(4)
            if "parentId" not in e:
                e["parentId"] = prev
            prev = e["id"]
            msg = e.get("message")
            if isinstance(msg, dict) and msg.get("role") == "hookMessage":
                msg["role"] = "custom"
        self.header["version"] = SESSION_VERSION

    def _new_id(self) -> str:
        while True:
            i = secrets.token_hex(4)
            if i not in self.by_id:
                return i

    def _write(self, entry: Dict[str, Any]) -> None:
        if not self.persist or not self.file:
            return
        self.file.parent.mkdir(parents=True, exist_ok=True)
        # Pi writes the file once the first assistant message exists; we write on first entry (simpler, same format)
        with open(self.file, "a", encoding="utf-8") as f:
            if not self._flushed:
                f.write(json.dumps(self.header, ensure_ascii=False) + "\n")
                self._flushed = True
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")

    def _append(self, entry: Dict[str, Any]) -> Dict[str, Any]:
        entry.setdefault("id", self._new_id())
        entry.setdefault("parentId", self.leaf_id)
        entry.setdefault("timestamp", now_iso())
        self.entries.append(entry)
        self.by_id[entry["id"]] = entry
        self.leaf_id = entry["id"]
        self._write(entry)
        self._notify("append", entry)
        return entry

    def _notify(self, kind: str, payload: Any) -> None:
        for fn in list(self.listeners):
            try:
                fn(kind, payload)
            except Exception:
                pass

    # ------------------------------------------------------------------------------------------ append
    def append_message(self, message: Dict[str, Any]) -> Dict[str, Any]:
        return self._append({"type": "message", "message": message})

    def append_model_change(self, provider: str, model_id: str) -> Dict[str, Any]:
        return self._append({"type": "model_change", "provider": provider, "modelId": model_id})

    def append_thinking_level_change(self, level: str) -> Dict[str, Any]:
        return self._append({"type": "thinking_level_change", "thinkingLevel": level})

    def append_compaction(self, summary: str, first_kept_id: str, tokens_before: int, details: Any = None,
                          from_hook: bool = False, usage: Any = None) -> Dict[str, Any]:
        e = {"type": "compaction", "summary": summary, "firstKeptEntryId": first_kept_id, "tokensBefore": tokens_before}
        if details is not None:
            e["details"] = details
        if from_hook:
            e["fromHook"] = True
        if usage:
            e["usage"] = usage
        return self._append(e)

    def append_branch_summary(self, parent_id: Optional[str], from_id: str, summary: str, details: Any = None) -> Dict[str, Any]:
        e = {"type": "branch_summary", "parentId": parent_id, "fromId": from_id, "summary": summary}
        if details is not None:
            e["details"] = details
        return self._append(e)

    def append_custom(self, custom_type: str, data: Any = None) -> Dict[str, Any]:
        return self._append({"type": "custom", "customType": custom_type, "data": data})

    def append_custom_message(self, custom_type: str, content: Any, display: bool = True, details: Any = None) -> Dict[str, Any]:
        e = {"type": "custom_message", "customType": custom_type, "content": content, "display": display}
        if details is not None:
            e["details"] = details
        return self._append(e)

    def append_label(self, target_id: str, label: Optional[str]) -> Dict[str, Any]:
        return self._append({"type": "label", "targetId": target_id, "label": label})

    def append_session_info(self, name: Optional[str]) -> Dict[str, Any]:
        return self._append({"type": "session_info", "name": name})

    # ------------------------------------------------------------------------------------------- query
    @property
    def session_id(self) -> str:
        return self.header.get("id", "")

    def get_entry(self, entry_id: str) -> Optional[Dict[str, Any]]:
        return self.by_id.get(entry_id)

    def get_children(self, entry_id: Optional[str]) -> List[Dict[str, Any]]:
        return [e for e in self.entries if e.get("parentId") == entry_id]

    def get_branch(self, leaf_id: Optional[str] = None) -> List[Dict[str, Any]]:
        leaf = self.leaf_id if leaf_id is None else leaf_id
        path = []
        cur = self.by_id.get(leaf) if leaf else None
        guard = 0
        while cur is not None and guard < 1_000_000:
            path.append(cur)
            cur = self.by_id.get(cur.get("parentId")) if cur.get("parentId") else None
            guard += 1
        path.reverse()
        return path

    def session_name(self) -> Optional[str]:
        name = None
        for e in self.entries:
            if e.get("type") == "session_info":
                name = e.get("name")
        return name

    def label_for(self, entry_id: str) -> Optional[str]:
        label = None
        for e in self.entries:
            if e.get("type") == "label" and e.get("targetId") == entry_id:
                label = e.get("label")
        return label

    def set_leaf(self, entry_id: Optional[str]) -> None:
        self.leaf_id = entry_id

    def build_context(self, leaf_id: Optional[str] = None) -> Dict[str, Any]:
        """Messages on the active branch with the latest compaction applied, plus model/thinking settings."""
        path = self.get_branch(leaf_id)
        thinking, model = None, None
        for e in path:
            if e["type"] == "thinking_level_change":
                thinking = e.get("thinkingLevel")
            elif e["type"] == "model_change":
                model = {"provider": e.get("provider"), "modelId": e.get("modelId")}
            elif e["type"] == "message" and e["message"].get("role") == "assistant":
                model = {"provider": e["message"].get("provider"), "modelId": e["message"].get("model")}
        compaction = None
        for e in path:
            if e["type"] == "compaction":
                compaction = e
        entries = path
        if compaction:
            idx = path.index(compaction)
            kept, found = [compaction], False
            for e in path[:idx]:
                if e["id"] == compaction.get("firstKeptEntryId"):
                    found = True
                if found and not (e["type"] == "message" and e["message"].get("role") == "system"):
                    kept.append(e)
            entries = kept + path[idx + 1:]
        edits = {e["targetId"]: e for e in entries if e["type"] == "context_edit"}
        messages: List[Dict[str, Any]] = []
        for i, e in enumerate(entries):
            msgs = self.entry_to_messages(e) if not (e["type"] == "compaction" and i > 0) else []
            ed = edits.get(e["id"])
            if ed is not None:
                if ed.get("replacement") is None:
                    msgs = []
                else:
                    msgs = [dict(m, content=ed["replacement"]["content"]) for m in msgs]
            messages.extend(msgs)
        return {"messages": messages, "thinkingLevel": thinking, "model": model, "entries": entries}

    @staticmethod
    def entry_to_messages(e: Dict[str, Any]) -> List[Dict[str, Any]]:
        t = e.get("type")
        if t == "message":
            m = e["message"]
            if m.get("content") is None:
                m = dict(m, content="" if m.get("role") == "system" else [])
            return [m]
        if t == "custom_message":
            return [{"role": "custom", "customType": e.get("customType"), "content": e.get("content") or [],
                     "display": e.get("display", True), "details": e.get("details"), "timestamp": now_ms()}]
        if t == "branch_summary" and e.get("summary"):
            return [{"role": "branchSummary", "summary": e["summary"], "fromId": e.get("fromId"), "timestamp": now_ms()}]
        if t == "compaction":
            return [{"role": "compactionSummary", "summary": e.get("summary", ""), "tokensBefore": e.get("tokensBefore", 0),
                     "timestamp": now_ms()}]
        return []

    # --------------------------------------------------------------------------------------- branching
    def fork_to_new_file(self, entry_id: Optional[str], position: str = "before") -> None:
        """Copy the path up to entry_id (exclusive when position='before') into a new session file."""
        path = self.get_branch(entry_id) if entry_id else []
        if position == "before" and path:
            path = path[:-1]
        parent = str(self.file) if self.file else None
        old = [dict(e) for e in path]
        self.new_session(parent_session=parent)
        for e in old:
            self._append(e)

    def clone(self) -> None:
        self.fork_to_new_file(self.leaf_id, position="at")

    def tree_rows(self) -> List[Dict[str, Any]]:
        """Depth-first flattened tree for /tree rendering: [{entry, depth, isLeafPath, label}]."""
        on_path = {e["id"] for e in self.get_branch()}
        rows: List[Dict[str, Any]] = []
        children: Dict[Optional[str], List[Dict[str, Any]]] = {}
        for e in self.entries:
            children.setdefault(e.get("parentId"), []).append(e)

        def visit(pid: Optional[str], depth: int) -> None:
            kids = children.get(pid, [])
            for k in kids:
                rows.append({"entry": k, "depth": depth, "onPath": k["id"] in on_path, "label": self.label_for(k["id"]),
                             "branches": len(kids)})
                visit(k["id"], depth + (1 if len(kids) > 1 else 0))
        visit(None, 0)
        return rows

    # ----------------------------------------------------------------------------------------- listing
    @staticmethod
    def list_sessions(session_dir: Path) -> List[Dict[str, Any]]:
        out = []
        if not session_dir.exists():
            return out
        for p in session_dir.glob("*.jsonl"):
            try:
                with open(p, encoding="utf-8") as f:
                    header = json.loads(f.readline())
                    first_user, name, count = None, None, 0
                    for line in f:
                        try:
                            e = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        if e.get("type") == "message":
                            count += 1
                            m = e["message"]
                            if m.get("role") == "user" and first_user is None:
                                c = m.get("content")
                                first_user = c if isinstance(c, str) else "".join(b.get("text", "") for b in c or [] if b.get("type") == "text")
                        elif e.get("type") == "session_info":
                            name = e.get("name")
                out.append({"path": str(p), "id": header.get("id"), "timestamp": header.get("timestamp"), "cwd": header.get("cwd"),
                            "name": name, "firstMessage": (first_user or "").strip().replace("\n", " ")[:120], "messageCount": count,
                            "mtime": p.stat().st_mtime})
            except Exception:
                continue
        out.sort(key=lambda s: s["mtime"], reverse=True)
        return out

    @staticmethod
    def list_all_sessions(agent_dir: Path) -> List[Dict[str, Any]]:
        root = Path(agent_dir) / "sessions"
        out: List[Dict[str, Any]] = []
        if root.exists():
            for d in root.iterdir():
                if d.is_dir():
                    out.extend(SessionManager.list_sessions(d))
        out.sort(key=lambda s: s["mtime"], reverse=True)
        return out

    def export_jsonl(self, path: str) -> None:
        with open(path, "w", encoding="utf-8") as f:
            f.write(json.dumps(self.header, ensure_ascii=False) + "\n")
            for e in self.entries:
                f.write(json.dumps(e, ensure_ascii=False) + "\n")
