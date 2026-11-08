"""
Keyring credential store backed by SQLite.
Stores, retrieves, rotates, and manages cooldown blocks for provider API keys.
"""

from __future__ import annotations

import logging
import os
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

DEFAULT_DB_PATH = os.environ.get(
    "OMNI_AUTH_DB",
    r"D:\Harness\omni-harness\agent_auth.db",
)

DEFAULT_ENDPOINTS = [
    ("openai", "https://api.openai.com/v1", "gpt-4o"),
    ("anthropic", "https://api.anthropic.com/v1", "claude-3-5-sonnet-20241022"),
    ("google", "https://generativelanguage.googleapis.com/v1beta", "gemini-1.5-flash"),
    ("local", "http://localhost:11434/v1", "llama3"),
    ("ollama", "http://localhost:11434/v1", "llama3"),
]

ENV_KEY_MAP = {
    "openai": ["OPENAI_API_KEY"],
    "anthropic": ["ANTHROPIC_API_KEY"],
    "google": ["GOOGLE_API_KEY", "GEMINI_API_KEY"],
    "local": ["LOCAL_API_KEY", "OLLAMA_API_KEY"],
    "ollama": ["OLLAMA_API_KEY", "LOCAL_API_KEY"],
}


class Keyring:
    """
    SQLite-backed keyring for managing provider credentials,
    endpoint configurations, and rate-limit cooldown blocks.
    """

    def __init__(self, db_path: str | Path | None = None) -> None:
        if db_path is None:
            self.db_path = str(DEFAULT_DB_PATH)
        else:
            self.db_path = str(db_path)

        # Ensure directory exists if not an in-memory db
        if self.db_path != ":memory:":
            parent_dir = os.path.dirname(os.path.abspath(self.db_path))
            if parent_dir:
                os.makedirs(parent_dir, exist_ok=True)

        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        """Create a new SQLite connection with row factory configured."""
        conn = sqlite3.connect(self.db_path, timeout=10.0)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        """Create necessary tables and seed default endpoints if not present."""
        with self._connect() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS auth_credentials (
                    provider TEXT,
                    key_id TEXT PRIMARY KEY,
                    api_key TEXT,
                    is_active INTEGER DEFAULT 1,
                    priority INTEGER DEFAULT 0,
                    created_at REAL
                );
                """
            )
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS auth_credential_blocks (
                    provider TEXT,
                    key_id TEXT,
                    reason TEXT,
                    cooldown_until REAL,
                    PRIMARY KEY (provider, key_id)
                );
                """
            )
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS provider_endpoints (
                    provider TEXT PRIMARY KEY,
                    base_url TEXT,
                    default_model TEXT
                );
                """
            )

            # Seed default endpoints if table is empty
            cursor.execute("SELECT COUNT(*) FROM provider_endpoints;")
            count = cursor.fetchone()[0]
            if count == 0:
                cursor.executemany(
                    """
                    INSERT OR IGNORE INTO provider_endpoints (provider, base_url, default_model)
                    VALUES (?, ?, ?);
                    """,
                    DEFAULT_ENDPOINTS,
                )
            conn.commit()

    def add_key(
        self,
        provider: str,
        api_key: str,
        key_id: str | None = None,
        priority: int = 0,
    ) -> str:
        """
        Add or update an API key for a provider.
        If key_id is not provided, a unique key_id is generated.
        Returns the key_id.
        """
        provider = provider.lower().strip()
        if not key_id:
            key_id = f"{provider}_{uuid.uuid4().hex[:8]}"

        now = time.time()
        with self._connect() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                INSERT INTO auth_credentials (provider, key_id, api_key, is_active, priority, created_at)
                VALUES (?, ?, ?, 1, ?, ?)
                ON CONFLICT(key_id) DO UPDATE SET
                    provider=excluded.provider,
                    api_key=excluded.api_key,
                    is_active=1,
                    priority=excluded.priority,
                    created_at=excluded.created_at;
                """,
                (provider, key_id, api_key, priority, now),
            )
            conn.commit()

        logger.info("Added credential for provider '%s' (key_id: %s, priority: %d)", provider, key_id, priority)
        return key_id

    def list_keys(self, provider: str | None = None) -> list[dict[str, Any]]:
        """
        List all keys, optionally filtered by provider.
        Includes block status if currently cooldown-blocked.
        """
        now = time.time()
        with self._connect() as conn:
            cursor = conn.cursor()
            if provider:
                cursor.execute(
                    """
                    SELECT c.provider, c.key_id, c.api_key, c.is_active, c.priority, c.created_at,
                           b.reason AS block_reason, b.cooldown_until,
                           CASE WHEN b.cooldown_until IS NOT NULL AND b.cooldown_until > ? THEN 1 ELSE 0 END AS is_blocked
                    FROM auth_credentials c
                    LEFT JOIN auth_credential_blocks b ON c.provider = b.provider AND c.key_id = b.key_id
                    WHERE c.provider = ?
                    ORDER BY c.priority DESC, c.created_at ASC;
                    """,
                    (now, provider.lower().strip()),
                )
            else:
                cursor.execute(
                    """
                    SELECT c.provider, c.key_id, c.api_key, c.is_active, c.priority, c.created_at,
                           b.reason AS block_reason, b.cooldown_until,
                           CASE WHEN b.cooldown_until IS NOT NULL AND b.cooldown_until > ? THEN 1 ELSE 0 END AS is_blocked
                    FROM auth_credentials c
                    LEFT JOIN auth_credential_blocks b ON c.provider = b.provider AND c.key_id = b.key_id
                    ORDER BY c.provider ASC, c.priority DESC, c.created_at ASC;
                    """,
                    (now,),
                )
            rows = cursor.fetchall()

        return [dict(row) for row in rows]

    def remove_key(self, provider: str, key_id: str) -> None:
        """
        Remove an API key from credentials and clear any active blocks.
        """
        provider = provider.lower().strip()
        with self._connect() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "DELETE FROM auth_credentials WHERE provider = ? AND key_id = ?;",
                (provider, key_id),
            )
            cursor.execute(
                "DELETE FROM auth_credential_blocks WHERE provider = ? AND key_id = ?;",
                (provider, key_id),
            )
            conn.commit()

        logger.info("Removed credential for provider '%s' (key_id: %s)", provider, key_id)

    def block_key(
        self,
        provider: str,
        key_id: str,
        cooldown_seconds: float,
        reason: str = "rate_limit",
    ) -> None:
        """
        Place a key on temporary cooldown block.
        """
        provider = provider.lower().strip()
        cooldown_until = time.time() + cooldown_seconds

        with self._connect() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                INSERT INTO auth_credential_blocks (provider, key_id, reason, cooldown_until)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(provider, key_id) DO UPDATE SET
                    reason=excluded.reason,
                    cooldown_until=excluded.cooldown_until;
                """,
                (provider, key_id, reason, cooldown_until),
            )
            conn.commit()

        logger.warning(
            "Blocked provider '%s' key '%s' for %.1fs (until %s). Reason: %s",
            provider,
            key_id,
            cooldown_seconds,
            time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(cooldown_until)),
            reason,
        )

    def unblock_expired(self) -> int:
        """
        Remove blocks whose cooldown duration has passed.
        Returns the number of unblocked keys.
        """
        now = time.time()
        with self._connect() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "DELETE FROM auth_credential_blocks WHERE cooldown_until <= ?;",
                (now,),
            )
            deleted_count = cursor.rowcount
            conn.commit()

        if deleted_count > 0:
            logger.info("Unblocked %d expired credential block(s)", deleted_count)
        return deleted_count

    def get_active_unblocked_keys(self, provider: str) -> list[dict[str, Any]]:
        """
        Retrieve active credentials that are not currently blocked,
        sorted by priority DESC, created_at ASC.
        Falls back to environment variables or local defaults if no DB keys exist.
        """
        provider = provider.lower().strip()
        self.unblock_expired()

        now = time.time()
        with self._connect() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT c.provider, c.key_id, c.api_key, c.is_active, c.priority, c.created_at
                FROM auth_credentials c
                LEFT JOIN auth_credential_blocks b ON c.provider = b.provider AND c.key_id = b.key_id
                WHERE c.provider = ?
                  AND c.is_active = 1
                  AND (b.cooldown_until IS NULL OR b.cooldown_until <= ?)
                ORDER BY c.priority DESC, c.created_at ASC;
                """,
                (provider, now),
            )
            rows = cursor.fetchall()

        keys = [dict(row) for row in rows]
        if keys:
            return keys

        # Environment variable fallback if no keys configured in DB
        env_vars = ENV_KEY_MAP.get(provider, [])
        for env_var in env_vars:
            val = os.environ.get(env_var)
            if val and val.strip():
                env_key_id = f"env_{provider}_{env_var.lower()}"
                # Verify this env key is not currently blocked
                if not self._is_key_blocked(provider, env_key_id, now):
                    return [
                        {
                            "provider": provider,
                            "key_id": env_key_id,
                            "api_key": val.strip(),
                            "is_active": 1,
                            "priority": 0,
                            "created_at": 0.0,
                        }
                    ]

        # For local/ollama providers, default to local if not blocked
        if provider in ("local", "ollama"):
            local_key_id = f"{provider}_default"
            if not self._is_key_blocked(provider, local_key_id, now):
                return [
                    {
                        "provider": provider,
                        "key_id": local_key_id,
                        "api_key": "local",
                        "is_active": 1,
                        "priority": 0,
                        "created_at": 0.0,
                    }
                ]

        return []

    def _is_key_blocked(self, provider: str, key_id: str, now: float) -> bool:
        """Check if a specific key is currently blocked."""
        with self._connect() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT 1 FROM auth_credential_blocks
                WHERE provider = ? AND key_id = ? AND cooldown_until > ?;
                """,
                (provider, key_id, now),
            )
            return cursor.fetchone() is not None

    def set_endpoint(
        self,
        provider: str,
        base_url: str,
        default_model: str | None = None,
    ) -> None:
        """Set or update base_url and default_model for a provider endpoint."""
        provider = provider.lower().strip()
        with self._connect() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                INSERT INTO provider_endpoints (provider, base_url, default_model)
                VALUES (?, ?, ?)
                ON CONFLICT(provider) DO UPDATE SET
                    base_url=excluded.base_url,
                    default_model=coalesce(excluded.default_model, provider_endpoints.default_model);
                """,
                (provider, base_url.rstrip("/"), default_model),
            )
            conn.commit()

    def get_endpoint(self, provider: str) -> dict[str, Any] | None:
        """Get endpoint details (base_url, default_model) for a provider."""
        provider = provider.lower().strip()
        with self._connect() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT provider, base_url, default_model FROM provider_endpoints WHERE provider = ?;",
                (provider,),
            )
            row = cursor.fetchone()
            return dict(row) if row else None

    def list_endpoints(self) -> list[dict[str, Any]]:
        """List all configured provider endpoints."""
        with self._connect() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT provider, base_url, default_model FROM provider_endpoints;")
            return [dict(r) for r in cursor.fetchall()]


# Export Keyring alias for compatibility
AuthKeyring = Keyring
