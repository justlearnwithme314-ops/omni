"""Omni Session Persistence Module (OpenClaw Standard).

Provides durable SQLite session storage for conversation history,
tool outcomes, scratchpad state, and session metadata.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
import sqlite3
import time
from typing import Any, Dict, List, Optional, Union
import uuid

from ..kernel.state import Message, SessionState

logger = logging.getLogger("omni.features.persistence")

DEFAULT_DB_PATH = Path("sessions.sqlite")


class SessionStore:
    """Durable SQLite storage for Omni agent sessions.

    Manages the `saved_sessions` table with full serialization of SessionState
    (messages, token metrics, scratchpad) and metadata indexing.
    """

    def __init__(self, db_path: Optional[Union[str, Path]] = None) -> None:
        if db_path is None:
            env_db = os.environ.get("OMNI_SESSIONS_DB")
            self.db_path = Path(env_db) if env_db else DEFAULT_DB_PATH
        elif str(db_path) == ":memory:":
            self.db_path = ":memory:"
        else:
            self.db_path = Path(db_path).resolve()

        if self.db_path != ":memory:":
            parent_dir = Path(self.db_path).parent
            if parent_dir and not parent_dir.exists():
                parent_dir.mkdir(parents=True, exist_ok=True)
            self._mem_conn: Optional[sqlite3.Connection] = None
        else:
            # Keep open connection for in-memory DB so it doesn't vanish between calls
            self._mem_conn = sqlite3.connect(":memory:")
            self._mem_conn.row_factory = sqlite3.Row

        self._init_db()

    def _get_connection(self) -> sqlite3.Connection:
        """Provide configured SQLite connection."""
        if self._mem_conn is not None:
            return self._mem_conn
        conn = sqlite3.connect(str(self.db_path), timeout=10.0)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        """Create saved_sessions table and indexes if not existing."""
        conn = self._get_connection()
        try:
            with conn:
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS saved_sessions (
                        session_id TEXT PRIMARY KEY,
                        title TEXT,
                        model TEXT,
                        mode TEXT,
                        created_at REAL,
                        updated_at REAL,
                        messages_json TEXT,
                        scratchpad_json TEXT
                    )
                    """
                )
                conn.execute(
                    """
                    CREATE INDEX IF NOT EXISTS idx_saved_sessions_updated
                    ON saved_sessions(updated_at DESC)
                    """
                )
        finally:
            if self._mem_conn is None:
                conn.close()

    def save_session(
        self,
        state: SessionState,
        title: Optional[str] = None,
        model: Optional[str] = None,
        mode: Optional[str] = None,
    ) -> str:
        """Save or update a SessionState in the database.

        Args:
            state: The active SessionState instance.
            title: Optional title for the session. Derived from prompt if None.
            model: Optional model identifier.
            mode: Optional operating mode.

        Returns:
            str: The session_id of the persisted session.
        """
        session_id = state.session_id

        # 1. Serialize messages
        messages_data = [
            m.to_dict() if hasattr(m, "to_dict") else m
            for m in state.messages
        ]
        messages_json = json.dumps(messages_data, ensure_ascii=False)

        # 2. Serialize scratchpad
        scratchpad_data = state.scratchpad if isinstance(state.scratchpad, dict) else {}
        scratchpad_json = json.dumps(scratchpad_data, ensure_ascii=False)

        # 3. Timestamps & metadata resolution
        now = time.time()
        conn = self._get_connection()
        try:
            with conn:
                cursor = conn.execute(
                    "SELECT created_at, title, model, mode FROM saved_sessions WHERE session_id = ?",
                    (session_id,),
                )
                existing = cursor.fetchone()

                created_at = existing["created_at"] if existing else now
                updated_at = now

                # Determine title
                resolved_title = title
                if not resolved_title and existing and existing["title"]:
                    resolved_title = existing["title"]
                if not resolved_title:
                    # Infer from first user message
                    for msg in state.messages:
                        role = getattr(msg, "role", "")
                        content = getattr(msg, "content", "")
                        if role == "user" and content:
                            resolved_title = content.strip().split("\n")[0][:60]
                            break
                if not resolved_title:
                    resolved_title = "Untitled Session"

                # Determine model
                resolved_model = model
                if not resolved_model and existing and existing["model"]:
                    resolved_model = existing["model"]
                if not resolved_model:
                    resolved_model = scratchpad_data.get("model", "auto")

                # Determine mode
                resolved_mode = mode
                if not resolved_mode and existing and existing["mode"]:
                    resolved_mode = existing["mode"]
                if not resolved_mode:
                    resolved_mode = scratchpad_data.get("mode", "balanced")

                conn.execute(
                    """
                    INSERT OR REPLACE INTO saved_sessions (
                        session_id, title, model, mode, created_at, updated_at, messages_json, scratchpad_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        session_id,
                        resolved_title,
                        resolved_model,
                        resolved_mode,
                        created_at,
                        updated_at,
                        messages_json,
                        scratchpad_json,
                    ),
                )
        finally:
            if self._mem_conn is None:
                conn.close()

        return session_id

    def load_session(self, session_id: str) -> Optional[SessionState]:
        """Load and reconstruct SessionState from SQLite by session_id.

        Args:
            session_id: Target session identifier.

        Returns:
            Optional[SessionState]: Reconstructed session state or None if not found.
        """
        conn = self._get_connection()
        try:
            cursor = conn.execute(
                """
                SELECT session_id, title, model, mode, created_at, updated_at, messages_json, scratchpad_json
                FROM saved_sessions WHERE session_id = ?
                """,
                (session_id,),
            )
            row = cursor.fetchone()
            if row is None:
                return None

            # Deserialize messages
            raw_msgs = json.loads(row["messages_json"]) if row["messages_json"] else []
            messages: List[Message] = []
            for m in raw_msgs:
                if isinstance(m, dict):
                    messages.append(Message.from_dict(m))
                elif isinstance(m, Message):
                    messages.append(m)

            # Deserialize scratchpad
            scratchpad = json.loads(row["scratchpad_json"]) if row["scratchpad_json"] else {}
            if row["title"]:
                scratchpad.setdefault("title", row["title"])
            if row["model"]:
                scratchpad.setdefault("model", row["model"])
            if row["mode"]:
                scratchpad.setdefault("mode", row["mode"])

            state = SessionState(
                session_id=row["session_id"],
                messages=messages,
                scratchpad=scratchpad,
            )
            state.turn_index = sum(1 for m in messages if getattr(m, "role", None) == "user")
            if hasattr(state, "_recalculate_token_count"):
                state._recalculate_token_count()

            return state
        finally:
            if self._mem_conn is None:
                conn.close()

    def list_sessions(self, limit: int = 20) -> list[dict]:
        """List summary of saved sessions ordered by updated_at descending.

        Args:
            limit: Maximum number of session records to return (default: 20).

        Returns:
            list[dict]: List of session summary dictionaries.
        """
        conn = self._get_connection()
        try:
            cursor = conn.execute(
                """
                SELECT session_id, title, model, mode, created_at, updated_at, messages_json
                FROM saved_sessions
                ORDER BY updated_at DESC
                LIMIT ?
                """,
                (max(1, limit),),
            )
            rows = cursor.fetchall()
            results: List[Dict[str, Any]] = []
            for r in rows:
                msg_count = 0
                if r["messages_json"]:
                    try:
                        msg_count = len(json.loads(r["messages_json"]))
                    except Exception:
                        pass
                results.append({
                    "session_id": r["session_id"],
                    "title": r["title"] or "Untitled Session",
                    "model": r["model"] or "",
                    "mode": r["mode"] or "",
                    "created_at": r["created_at"],
                    "updated_at": r["updated_at"],
                    "message_count": msg_count,
                })
            return results
        finally:
            if self._mem_conn is None:
                conn.close()

    def delete_session(self, session_id: str) -> bool:
        """Delete a saved session from the database.

        Args:
            session_id: Target session identifier to delete.

        Returns:
            bool: True if deleted, False if not found.
        """
        conn = self._get_connection()
        try:
            with conn:
                cursor = conn.execute(
                    "DELETE FROM saved_sessions WHERE session_id = ?",
                    (session_id,),
                )
                return cursor.rowcount > 0
        finally:
            if self._mem_conn is None:
                conn.close()
