"""
Autonomous Cron & Scheduled Task Engine (OpenClaw Architecture Standout)
"""

import asyncio
import json
import logging
import os
from pathlib import Path
import sqlite3
import time
from typing import Any, Dict, List, Optional
import uuid


class CronScheduler:
    def __init__(self, db_path: Optional[str] = None):
        self.db_path = db_path or os.path.join(
            os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
            "cron_tasks.sqlite",
        )
        self._init_db()

    def _init_db(self):
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS scheduled_tasks (
                    task_id TEXT PRIMARY KEY,
                    name TEXT,
                    schedule_expr TEXT,
                    prompt TEXT,
                    model TEXT,
                    mode TEXT,
                    enabled INTEGER DEFAULT 1,
                    interval_seconds REAL DEFAULT 3600.0,
                    last_run REAL,
                    next_run REAL,
                    created_at REAL
                )
                """
            )
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS task_runs (
                    run_id TEXT PRIMARY KEY,
                    task_id TEXT,
                    timestamp REAL,
                    status TEXT,
                    summary TEXT,
                    FOREIGN KEY(task_id) REFERENCES scheduled_tasks(task_id)
                )
                """
            )
            conn.commit()

    def _parse_schedule_interval(self, schedule_expr: str) -> float:
        expr = schedule_expr.lower().strip()
        if "daily" in expr:
            return 86400.0
        elif "hourly" in expr:
            return 3600.0
        elif "min" in expr or "m" in expr:
            nums = [int(s) for s in expr.split() if s.isdigit()]
            mins = nums[0] if nums else 30
            return float(mins * 60)
        elif "hour" in expr or "h" in expr:
            nums = [int(s) for s in expr.split() if s.isdigit()]
            hours = nums[0] if nums else 1
            return float(hours * 3600)
        return 3600.0

    def add_task(
        self,
        name: str,
        schedule_expr: str,
        prompt: str,
        model: str = "default",
        mode: str = "autonomous",
    ) -> str:
        task_id = f"cron_{uuid.uuid4().hex[:8]}"
        interval_secs = self._parse_schedule_interval(schedule_expr)
        now = time.time()
        next_run = now + interval_secs

        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                INSERT INTO scheduled_tasks
                (task_id, name, schedule_expr, prompt, model, mode, enabled, interval_seconds, last_run, next_run, created_at)
                VALUES (?, ?, ?, ?, ?, ?, 1, ?, NULL, ?, ?)
                """,
                (task_id, name, schedule_expr, prompt, model, mode, interval_secs, next_run, now),
            )
            conn.commit()
        return task_id

    def list_tasks(self) -> List[Dict[str, Any]]:
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM scheduled_tasks ORDER BY created_at DESC")
            return [dict(r) for r in cursor.fetchall()]

    def delete_task(self, task_id: str) -> bool:
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM scheduled_tasks WHERE task_id = ?", (task_id,))
            conn.commit()
            return cursor.rowcount > 0
