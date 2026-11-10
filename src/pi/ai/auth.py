"""Credential resolution, in Pi's order:

1. runtime overrides (extensions: `ctx.modelRegistry.authStorage.setRuntimeApiKey`)  - not persisted
2. `<agent-dir>/auth.json` (Pi's own store; `/login` writes API keys and OAuth sign-ins here; OAuth access tokens
   are refreshed shortly before they expire - see `oauth.py`)
3. Omni's keyring (`agent_auth.db`: API keys + OAuth tokens imported from OMP, with refresh)
4. environment variables
"""
from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from .models import PROVIDERS, resolve_config_value


class AuthStorage:
    def __init__(self, agent_dir: Path, registry=None):
        self.path = Path(agent_dir) / "auth.json"
        self.runtime: Dict[str, str] = {}
        self.registry = registry
        self._keyring = None
        self._keyring_failed = False
        self._refresh_lock = threading.Lock()
        self.last_refresh_error: Optional[str] = None

    # -- auth.json -------------------------------------------------------------------------------------
    def _load(self) -> Dict[str, Any]:
        try:
            return json.loads(self.path.read_text(encoding="utf-8")) if self.path.exists() else {}
        except Exception:
            return {}

    def _save(self, data: Dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
        os.replace(tmp, self.path)
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass

    def set_api_key(self, provider: str, key: str) -> None:
        data = self._load()
        data[provider] = {"type": "api_key", "key": key}
        self._save(data)

    def set_oauth(self, provider: str, credential: Dict[str, Any]) -> None:
        data = self._load()
        data[provider] = dict(credential, type="oauth")
        self._save(data)

    def entry(self, provider: str) -> Optional[Dict[str, Any]]:
        e = self._load().get(provider)
        return e if isinstance(e, dict) else None

    def _oauth_access(self, provider: str, entry: Dict[str, Any]) -> Optional[str]:
        """Access token of an `auth.json` OAuth entry, refreshed (and saved) when it expires within a minute."""
        if entry.get("access") and (not entry.get("expires") or entry["expires"] > time.time() * 1000 + 60_000):
            return entry["access"]
        if not entry.get("refresh"):
            return None
        with self._refresh_lock:
            cur = self.entry(provider) or entry  # another thread may have refreshed it already
            if cur.get("access") and cur.get("expires", 0) > time.time() * 1000 + 60_000:
                return cur["access"]
            try:
                from .oauth import refresh
                fresh = refresh(provider, cur)
            except Exception as e:  # keep the stale token out; report why
                self.last_refresh_error = f"{provider}: {e}"
                return None
            self.set_oauth(provider, fresh)
            self.last_refresh_error = None
            return fresh.get("access")

    def credential_extra(self, provider: str) -> Dict[str, Any]:
        """Non-secret fields stored with an OAuth sign-in (e.g. Antigravity `projectId`, `email`)."""
        e = self.entry(provider)
        if e and e.get("type") == "oauth":
            return {k: v for k, v in e.items() if k not in ("type", "access", "refresh", "expires")}
        kr = self._omni_keyring()  # OAuth sessions imported from OMP keep their extras in raw_json
        if kr is None:
            return {}
        try:
            import sqlite3
            with sqlite3.connect(kr.db_path) as conn:
                row = conn.execute("SELECT raw_json FROM oauth_metadata WHERE provider = ? ORDER BY expires_at DESC LIMIT 1",
                                   (provider,)).fetchone()
            raw = json.loads(row[0]) if row and row[0] else {}
            return {k: raw[k] for k in ("projectId", "email") if raw.get(k)}
        except Exception:
            return {}

    def remove(self, provider: str) -> bool:
        data = self._load()
        if provider in data:
            del data[provider]
            self._save(data)
            return True
        return False

    def stored_providers(self) -> List[str]:
        return sorted(self._load().keys())

    def set_runtime_api_key(self, provider: str, key: Optional[str]) -> None:
        if key:
            self.runtime[provider] = key
        else:
            self.runtime.pop(provider, None)

    # -- omni keyring ----------------------------------------------------------------------------------
    def _omni_keyring(self):
        if self._keyring is None and not self._keyring_failed:
            try:
                from ...providers.keyring import Keyring  # type: ignore
                self._keyring = Keyring()
            except Exception:
                self._keyring_failed = True
        return self._keyring

    def _from_keyring(self, provider: str) -> Optional[str]:
        kr = self._omni_keyring()
        if kr is None:
            return None
        try:
            if provider in ("google-antigravity", "google", "anthropic", "openai-codex", "github-copilot"):
                try:  # refresh expiring OAuth tokens (antigravity tokens live ~1h)
                    from ...providers.oauth import OAuthManager  # type: ignore
                    OAuthManager(kr).sync_if_expired()
                except Exception:
                    pass
            keys = kr.get_active_unblocked_keys(provider)
            return str(keys[0]["api_key"]) if keys else None
        except Exception:
            return None

    def block_keyring_key(self, provider: str, seconds: float, reason: str) -> None:
        kr = self._omni_keyring()
        if kr is None:
            return
        try:
            keys = kr.get_active_unblocked_keys(provider)
            if keys:
                kr.block_key(provider, str(keys[0]["key_id"]), cooldown_seconds=seconds, reason=reason)
        except Exception:
            pass

    # -- resolution ------------------------------------------------------------------------------------
    def _oauth_api_key(self, provider: str, token: str) -> str:
        """Pi's `oauth.getApiKey(credential)`: some providers (extension ones, e.g. pi-antigravity) hand the stream a
        derived key such as `{"token", "projectId"}` JSON instead of the bare access token."""
        try:
            from .oauth import PROVIDERS as OAUTH
            p = OAUTH.get(provider)
            if p is None or p.api_key is None:
                return token
            cred = self.entry(provider) or {}
            return p.api_key(cred) or token
        except Exception as e:
            self.last_refresh_error = f"{provider}: getApiKey failed: {e}"
            return token

    def get_api_key(self, provider: str, transform: bool = True) -> Optional[str]:
        if provider in self.runtime:
            return self.runtime[provider]
        entry = self._load().get(provider)
        if isinstance(entry, dict):
            if entry.get("type") == "oauth":
                tok = self._oauth_access(provider, entry)
                if tok:
                    return self._oauth_api_key(provider, tok) if transform else tok
            elif entry.get("key"):
                return resolve_config_value(entry["key"])
        elif isinstance(entry, str):
            return entry
        if self.registry is not None:
            override = self.registry.provider_info(provider).get("apiKey")
            if override:
                val = resolve_config_value(override)
                if val:
                    return val
        key = self._from_keyring(provider)
        if key:
            return key
        for env in PROVIDERS.get(provider, {}).get("env", []):
            if os.environ.get(env):
                return os.environ[env]
        if PROVIDERS.get(provider, {}).get("noAuth"):
            return "local"
        return None

    def has_auth(self, provider: str) -> bool:
        reg = self.registry
        if reg is not None and any(k.split("/", 1)[0] == provider for k in getattr(reg, "virtual", {})) \
                and provider not in PROVIDERS and provider not in getattr(reg, "provider_overrides", {}):
            return True  # a virtual model under a provider ID no physical provider uses is always available
        return bool(self.get_api_key(provider, transform=False))
