"""
Mnemopi Semantic Triplestore Memory Graph (OMP Architecture Standout)
"""

import os
from pathlib import Path
import re
import sqlite3
import time
from typing import Any, Dict, List, Optional


class MemoryGraph:
    def __init__(self, db_path: Optional[str] = None):
        self.db_path = db_path or os.path.join(
            os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
            "memory_graph.sqlite",
        )
        self._init_db()

    def _init_db(self):
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS triples (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    subject TEXT NOT NULL,
                    predicate TEXT NOT NULL,
                    object TEXT NOT NULL,
                    confidence REAL DEFAULT 1.0,
                    source TEXT,
                    created_at REAL
                )
                """
            )
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_triples_sub ON triples(subject)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_triples_pred ON triples(predicate)")
            conn.commit()

    def add_triple(
        self,
        subject: str,
        predicate: str,
        object_val: str,
        confidence: float = 1.0,
        source: str = "conversation",
    ) -> int:
        now = time.time()
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                INSERT INTO triples (subject, predicate, object, confidence, source, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (subject.strip(), predicate.strip(), object_val.strip(), confidence, source, now),
            )
            conn.commit()
            return cursor.lastrowid

    def query_triples(
        self,
        subject: Optional[str] = None,
        predicate: Optional[str] = None,
        keyword: Optional[str] = None,
        limit: int = 20,
    ) -> List[Dict[str, Any]]:
        query = "SELECT subject, predicate, object, confidence, source, created_at FROM triples WHERE 1=1"
        params = []
        if subject:
            query += " AND subject LIKE ?"
            params.append(f"%{subject.strip()}%")
        if predicate:
            query += " AND predicate = ?"
            params.append(predicate.strip())
        if keyword:
            query += " AND (subject LIKE ? OR predicate LIKE ? OR object LIKE ?)"
            kw = f"%{keyword.strip()}%"
            params.extend([kw, kw, kw])
        query += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)

        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute(query, params)
            return [dict(r) for r in cursor.fetchall()]

    def extract_and_store_facts(self, text: str) -> List[Dict[str, str]]:
        extracted = []
        pattern_is = re.compile(r"\b(?:my|the)\s+([a-zA-Z0-9_\-\s]+?)\s+(?:is|are|uses|has)\s+([a-zA-Z0-9_\-\.\s]+)", re.IGNORECASE)
        for match in pattern_is.finditer(text):
            sub, obj = match.group(1).strip(), match.group(2).strip()
            if len(sub) < 30 and len(obj) < 60:
                self.add_triple(sub, "has_property", obj, confidence=0.9, source="user_statement")
                extracted.append({"subject": sub, "predicate": "has_property", "object": obj})

        pattern_pref = re.compile(r"\b(?:i\s+prefer|i\s+use|i\s+like)\s+([a-zA-Z0-9_\-\.\s]+)", re.IGNORECASE)
        for match in pattern_pref.finditer(text):
            obj = match.group(1).strip()
            if len(obj) < 50:
                self.add_triple("user", "prefers", obj, confidence=0.85, source="user_preference")
                extracted.append({"subject": "user", "predicate": "prefers", "object": obj})

        return extracted

    def recall_relevant_context(self, query: str, limit: int = 5) -> str:
        words = [re.sub(r"[^a-zA-Z0-9]", "", w).lower() for w in query.split() if len(re.sub(r"[^a-zA-Z0-9]", "", w)) >= 3]
        matched_triples = []
        seen = set()

        for w in words[:4]:
            rows = self.query_triples(keyword=w, limit=3)
            for r in rows:
                key = (r["subject"], r["predicate"], r["object"])
                if key not in seen:
                    seen.add(key)
                    matched_triples.append(r)
                    if len(matched_triples) >= limit:
                        break
            if len(matched_triples) >= limit:
                break

        if not matched_triples:
            return ""

        facts = [f"- {t['subject']} {t['predicate']} {t['object']}" for t in matched_triples]
        return "[Relevant Memory Graph Assertions]\n" + "\n".join(facts)
