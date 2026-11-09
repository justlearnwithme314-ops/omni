"""
Conversation Timeline DAG & Branching Engine (Hermes Architecture Standout)
Allows conversation history to fork into alternative timeline branches,
rewind to prior turns, and render visual ASCII branch DAGs without losing history.
"""

import json
import os
from pathlib import Path
import sqlite3
import time
from typing import Any, Dict, List, Optional, Tuple


class TimelineBranchManager:
    def __init__(self, db_path: Optional[str] = None):
        self.db_path = db_path or os.path.join(
            os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
            "timeline_branches.sqlite",
        )
        self._init_db()

    def _init_db(self):
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS branches (
                    branch_id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    name TEXT NOT NULL,
                    parent_branch TEXT,
                    parent_turn INTEGER DEFAULT 0,
                    created_at REAL
                )
                """
            )
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS turn_nodes (
                    node_id TEXT PRIMARY KEY,
                    branch_id TEXT NOT NULL,
                    turn_index INTEGER NOT NULL,
                    role TEXT NOT NULL,
                    content TEXT NOT NULL,
                    tool_calls_json TEXT,
                    timestamp REAL,
                    FOREIGN KEY(branch_id) REFERENCES branches(branch_id)
                )
                """
            )
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_branch_session ON branches(session_id)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_turn_branch ON turn_nodes(branch_id)")
            conn.commit()

    def create_root_branch(self, session_id: str, name: str = "main") -> str:
        branch_id = f"{session_id}_branch_{name}"
        now = time.time()
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                INSERT OR IGNORE INTO branches (branch_id, session_id, name, parent_branch, parent_turn, created_at)
                VALUES (?, ?, ?, NULL, 0, ?)
                """,
                (branch_id, session_id, name, now),
            )
            conn.commit()
        return branch_id

    def fork_branch(
        self,
        session_id: str,
        parent_branch_name: str,
        fork_turn_index: int,
        new_branch_name: str,
    ) -> str:
        """Forks an existing branch at turn N into a new named timeline branch."""
        parent_branch_id = f"{session_id}_branch_{parent_branch_name}"
        new_branch_id = f"{session_id}_branch_{new_branch_name}"
        now = time.time()

        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                INSERT INTO branches (branch_id, session_id, name, parent_branch, parent_turn, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (new_branch_id, session_id, new_branch_name, parent_branch_name, fork_turn_index, now),
            )

            # Copy turn nodes up to fork_turn_index from parent branch
            cursor.execute(
                """
                SELECT turn_index, role, content, tool_calls_json
                FROM turn_nodes
                WHERE branch_id = ? AND turn_index <= ?
                ORDER BY turn_index ASC
                """,
                (parent_branch_id, fork_turn_index),
            )
            parent_nodes = cursor.fetchall()
            for idx, r, c, tc in parent_nodes:
                node_id = f"{new_branch_id}_node_{idx}_{int(now*1000)}"
                cursor.execute(
                    """
                    INSERT INTO turn_nodes (node_id, branch_id, turn_index, role, content, tool_calls_json, timestamp)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (node_id, new_branch_id, idx, r, c, tc, now),
                )
            conn.commit()
        return new_branch_id

    def record_turn(
        self,
        branch_id: str,
        turn_index: int,
        role: str,
        content: str,
        tool_calls: Optional[List[Dict[str, Any]]] = None,
    ):
        """Records a conversation turn node in the active branch."""
        now = time.time()
        node_id = f"{branch_id}_node_{turn_index}_{int(now*1000)}"
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                INSERT INTO turn_nodes (node_id, branch_id, turn_index, role, content, tool_calls_json, timestamp)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (node_id, branch_id, turn_index, role, content, json.dumps(tool_calls or []), now),
            )
            conn.commit()

    def list_branches(self, session_id: str) -> List[Dict[str, Any]]:
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute(
                "SELECT branch_id, name, parent_branch, parent_turn, created_at FROM branches WHERE session_id = ?",
                (session_id,),
            )
            return [dict(r) for r in cursor.fetchall()]

    def render_ascii_dag(self, session_id: str) -> str:
        """Renders an ASCII tree representation of the conversation branches."""
        branches = self.list_branches(session_id)
        if not branches:
            return f"[Session {session_id[:8]}: Single linear timeline (no branches)]"

        lines = [f"=== Conversation Timeline DAG for {session_id} ==="]
        for b in branches:
            if not b["parent_branch"]:
                lines.append(f"  ● [main] (root timeline)")
            else:
                lines.append(f"  └── ⑂ [{b['name']}] (forked from '{b['parent_branch']}' at turn {b['parent_turn']})")
        return "\n".join(lines)
