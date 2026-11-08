"""
Telemetry and Trajectory Recorder (Metaharness Schema)
Normalizes agent runs, turn steps, tool traces, token usage, and outcomes into SQLite.
"""

import json
import sqlite3
import time
from typing import Any, Dict, List, Optional


class TrajectoryTracer:
    def __init__(self, db_path: str = "telemetry.sqlite"):
        self.db_path = db_path
        self._init_db()

    def _init_db(self):
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS sessions (
                    session_id TEXT PRIMARY KEY,
                    mode TEXT,
                    model TEXT,
                    start_time REAL,
                    end_time REAL,
                    success INTEGER,
                    total_tokens INTEGER DEFAULT 0,
                    cost REAL DEFAULT 0.0
                )
                """
            )
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS turn_traces (
                    trace_id TEXT PRIMARY KEY,
                    session_id TEXT,
                    turn_index INTEGER,
                    user_input TEXT,
                    assistant_response TEXT,
                    tool_calls_json TEXT,
                    tool_results_json TEXT,
                    error TEXT,
                    timestamp REAL,
                    FOREIGN KEY(session_id) REFERENCES sessions(session_id)
                )
                """
            )
            conn.commit()

    def log_session_start(self, session_id: str, mode: str, model: str = "auto"):
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                INSERT OR REPLACE INTO sessions (session_id, mode, model, start_time, end_time, success, total_tokens, cost)
                VALUES (?, ?, ?, ?, NULL, 0, 0, 0.0)
                """,
                (session_id, mode, model, time.time()),
            )
            conn.commit()

    def log_turn(
        self,
        session_id: str,
        turn_index: int,
        user_input: str,
        assistant_response: str,
        tool_calls: Optional[List[Dict[str, Any]]] = None,
        tool_results: Optional[List[Dict[str, Any]]] = None,
        error: Optional[str] = None,
    ):
        trace_id = f"{session_id}_turn_{turn_index}_{int(time.time() * 1000)}"
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                INSERT INTO turn_traces (
                    trace_id, session_id, turn_index, user_input,
                    assistant_response, tool_calls_json, tool_results_json, error, timestamp
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    trace_id,
                    session_id,
                    turn_index,
                    user_input,
                    assistant_response,
                    json.dumps(tool_calls or []),
                    json.dumps(tool_results or []),
                    error,
                    time.time(),
                ),
            )
            conn.commit()

    def log_session_end(
        self,
        session_id: str,
        success: bool = True,
        total_tokens: int = 0,
        cost: float = 0.0,
    ):
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                UPDATE sessions
                SET end_time = ?, success = ?, total_tokens = ?, cost = ?
                WHERE session_id = ?
                """,
                (time.time(), 1 if success else 0, total_tokens, cost, session_id),
            )
            conn.commit()

    def get_failed_trajectories(self, limit: int = 10) -> List[Dict[str, Any]]:
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT s.session_id, s.mode, s.model, s.start_time, s.end_time,
                       t.turn_index, t.user_input, t.assistant_response,
                       t.tool_calls_json, t.tool_results_json, t.error
                FROM sessions s
                JOIN turn_traces t ON s.session_id = t.session_id
                WHERE s.success = 0 OR t.error IS NOT NULL
                ORDER BY t.timestamp DESC
                LIMIT ?
                """,
                (limit,),
            )
            rows = cursor.fetchall()
            return [dict(r) for r in rows]

    def get_session_stats(self) -> Dict[str, Any]:
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT COUNT(*), SUM(success), SUM(total_tokens), SUM(cost)
                FROM sessions
                """
            )
            total_runs, successful_runs, total_tokens, total_cost = cursor.fetchone()
            return {
                "total_sessions": total_runs or 0,
                "successful_sessions": successful_runs or 0,
                "total_tokens": total_tokens or 0,
                "total_cost": total_cost or 0.0,
                "success_rate": (
                    (successful_runs / total_runs * 100.0) if total_runs else 0.0
                ),
            }
