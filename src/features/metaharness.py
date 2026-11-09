"""
Metaharness Benchmark & Evaluation Manager (packages/metaharness in Oh My Pi).
Unifies repository benchmarks (Harbor, SWE-bench, TS edit, SnapCompact) under the
same experiment -> run -> trace model, SQLite persistence store, and normalized metric scoring.
"""

from dataclasses import dataclass, field
import json
import logging
import os
import sqlite3
import time
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


@dataclass
class EvaluationRun:
    id: str
    experiment_id: str
    task_id: str
    model: str
    status: str  # "running", "resolved", "failed"
    score: float = 0.0
    tokens_in: int = 0
    tokens_out: int = 0
    cost_usd: float = 0.0
    duration_s: float = 0.0
    patch_diff: str = ""
    created_at: float = field(default_factory=time.time)


class MetaHarnessManager:
    def __init__(self, db_path: Optional[str] = None):
        self.db_path = db_path or os.path.join(
            os.path.dirname(os.path.dirname(os.path.dirname(__file__))), ".omni", "benchmarks.sqlite"
        )
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        self._init_db()

    def _init_db(self):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("PRAGMA journal_mode=WAL;")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS experiments (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    benchmark_type TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_at REAL NOT NULL
                );
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS runs (
                    id TEXT PRIMARY KEY,
                    experiment_id TEXT NOT NULL,
                    task_id TEXT NOT NULL,
                    model TEXT NOT NULL,
                    status TEXT NOT NULL,
                    score REAL NOT NULL,
                    tokens_in INTEGER NOT NULL,
                    tokens_out INTEGER NOT NULL,
                    cost_usd REAL NOT NULL,
                    duration_s REAL NOT NULL,
                    patch_diff TEXT,
                    created_at REAL NOT NULL,
                    FOREIGN KEY(experiment_id) REFERENCES experiments(id)
                );
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS traces (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id TEXT NOT NULL,
                    step INTEGER NOT NULL,
                    event_type TEXT NOT NULL,
                    tool_name TEXT,
                    payload TEXT,
                    timestamp REAL NOT NULL,
                    FOREIGN KEY(run_id) REFERENCES runs(id)
                );
                """
            )

    def create_experiment(self, name: str, benchmark_type: str = "harbor") -> str:
        """Create a new benchmark experiment suite."""
        exp_id = f"exp_{os.urandom(4).hex()}"
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "INSERT INTO experiments (id, name, benchmark_type, status, created_at) VALUES (?, ?, ?, ?, ?)",
                (exp_id, name, benchmark_type, "active", time.time()),
            )
        return exp_id

    def record_run(self, run: EvaluationRun):
        """Record completed task trial execution and metrics."""
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO runs 
                (id, experiment_id, task_id, model, status, score, tokens_in, tokens_out, cost_usd, duration_s, patch_diff, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run.id,
                    run.experiment_id,
                    run.task_id,
                    run.model,
                    run.status,
                    run.score,
                    run.tokens_in,
                    run.tokens_out,
                    run.cost_usd,
                    run.duration_s,
                    run.patch_diff,
                    run.created_at,
                ),
            )

    def record_trace_step(self, run_id: str, step: int, event_type: str, tool_name: Optional[str], payload: Dict[str, Any]):
        """Record normalized trajectory step."""
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "INSERT INTO traces (run_id, step, event_type, tool_name, payload, timestamp) VALUES (?, ?, ?, ?, ?, ?)",
                (run_id, step, event_type, tool_name, json.dumps(payload), time.time()),
            )

    def execute_mock_trial(self, exp_id: str, task_id: str, model: str = "google/gemini-2.5-flash") -> EvaluationRun:
        """Simulates and scores a benchmark task execution trial."""
        t0 = time.perf_counter()
        run_id = f"run_{task_id}_{os.urandom(3).hex()}"

        # Record start trace
        self.record_trace_step(run_id, 1, "task_start", None, {"task_id": task_id, "model": model})
        self.record_trace_step(run_id, 2, "tool_execution", "ast_grep", {"pattern": "def solve(): ..."})

        duration = round(time.perf_counter() - t0, 3)
        run = EvaluationRun(
            id=run_id,
            experiment_id=exp_id,
            task_id=task_id,
            model=model,
            status="resolved",
            score=1.0,
            tokens_in=1250,
            tokens_out=340,
            cost_usd=0.00042,
            duration_s=duration,
            patch_diff="--- a/solution.py\n+++ b/solution.py\n@@ -1,2 +1,2 @@\n-def solve(): pass\n+def solve(): return 42\n",
        )
        self.record_run(run)
        return run

    def get_experiment_summary(self, exp_id: str) -> Dict[str, Any]:
        """Aggregate scores, total token spend, and resolution rates."""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT COUNT(*), AVG(score), SUM(tokens_in + tokens_out), SUM(cost_usd) FROM runs WHERE experiment_id = ?", (exp_id,))
            row = cursor.fetchone()
            total_runs, avg_score, total_tokens, total_cost = row
            return {
                "experiment_id": exp_id,
                "total_runs": total_runs or 0,
                "resolve_rate": round((avg_score or 0.0) * 100, 1),
                "total_tokens": total_tokens or 0,
                "total_cost_usd": round(total_cost or 0.0, 5),
            }
