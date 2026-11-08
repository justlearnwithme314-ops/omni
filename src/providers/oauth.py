"""
OAuth Manager & Import Engine for OmniHarness
Supports importing existing OAuth tokens from Oh My Pi (~/.omp/agent/agent.db),
direct Google OAuth refresh token exchange, Device-Code OAuth flows (GitHub Copilot),
and Local Callback Server PKCE flows.
"""

from __future__ import annotations

import asyncio
from http.server import HTTPServer, BaseHTTPRequestHandler
import json
import logging
import os
import sqlite3
import time
from typing import Any, Dict, Optional
import urllib.error
import urllib.parse
import urllib.request
import webbrowser

from .keyring import Keyring

logger = logging.getLogger(__name__)

GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"

# Default Google OAuth Client Credentials (Google Antigravity & Gemini CLI)
DEFAULT_GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID", "")
DEFAULT_GOOGLE_CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET", "")

GEMINI_CLI_CLIENT_ID = os.environ.get("GEMINI_CLI_CLIENT_ID", "")
GEMINI_CLI_CLIENT_SECRET = os.environ.get("GEMINI_CLI_CLIENT_SECRET", "")


class OAuthManager:
    def __init__(self, keyring: Keyring):
        self.keyring = keyring

    def import_from_omp(self, omp_db_path: Optional[str] = None) -> list[Dict[str, Any]]:
        """
        Discovers and imports active OAuth credentials from Oh My Pi (~/.omp/agent/agent.db).
        """
        if omp_db_path is None:
            omp_db_path = os.path.expanduser("~/.omp/agent/agent.db")

        if not os.path.exists(omp_db_path):
            return []

        imported = []
        try:
            with sqlite3.connect(omp_db_path) as conn:
                cursor = conn.cursor()
                cursor.execute(
                    """
                    SELECT id, provider, credential_type, data
                    FROM auth_credentials
                    WHERE credential_type = 'oauth'
                    """
                )
                rows = cursor.fetchall()
                for row_id, provider, cred_type, data_raw in rows:
                    try:
                        data = json.loads(data_raw)
                        access_token = data.get("access") or data.get("access_token")
                        refresh_token = data.get("refresh") or data.get("refresh_token")
                        email = data.get("email", "")
                        expires = data.get("expires")

                        if access_token:
                            key_id = f"omp_oauth_{provider}_{row_id}"
                            # Add to OmniHarness keyring
                            self.keyring.add_key(
                                provider=provider,
                                api_key=access_token,
                                key_id=key_id,
                                priority=50,  # High priority for OAuth
                            )
                            # Record metadata in keyring DB
                            with sqlite3.connect(self.keyring.db_path) as kconn:
                                kcur = kconn.cursor()
                                kcur.execute(
                                    """
                                    CREATE TABLE IF NOT EXISTS oauth_metadata (
                                        key_id TEXT PRIMARY KEY,
                                        provider TEXT,
                                        refresh_token TEXT,
                                        expires_at REAL,
                                        email TEXT,
                                        raw_json TEXT
                                    )
                                    """
                                )
                                kcur.execute(
                                    """
                                    INSERT OR REPLACE INTO oauth_metadata
                                    (key_id, provider, refresh_token, expires_at, email, raw_json)
                                    VALUES (?, ?, ?, ?, ?, ?)
                                    """,
                                    (key_id, provider, refresh_token, expires, email, data_raw),
                                )
                                kcur.execute(
                                    "DELETE FROM auth_credential_blocks WHERE provider = ? AND key_id = ?",
                                    (provider, key_id),
                                )
                                kconn.commit()

                            imported.append({
                                "provider": provider,
                                "email": email,
                                "key_id": key_id,
                                "expires": expires,
                            })
                    except Exception as err:
                        logger.warning("Failed to parse omp credential row %s: %s", row_id, err)
        except Exception as e:
            logger.error("Failed to read from omp database: %s", e)

        return imported

    def unblock_key(self, provider: str, key_id: str):
        with sqlite3.connect(self.keyring.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute(
                "DELETE FROM auth_credential_blocks WHERE provider = ? AND key_id = ?",
                (provider, key_id),
            )
            conn.commit()

    def refresh_google_token(
        self,
        refresh_token: str,
        client_id: Optional[str] = None,
        client_secret: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Posts to https://oauth2.googleapis.com/token with grant_type=refresh_token,
        returning new access_token, updated expires_at, and token details.
        """
        if not client_id:
            client_id = DEFAULT_GOOGLE_CLIENT_ID
        if not client_secret:
            client_secret = DEFAULT_GOOGLE_CLIENT_SECRET

        params = {
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": client_id,
            "client_secret": client_secret,
        }
        data = urllib.parse.urlencode(params).encode("utf-8")
        req = urllib.request.Request(
            GOOGLE_TOKEN_URL,
            data=data,
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "User-Agent": "OmniHarness/1.0",
            },
            method="POST",
        )

        try:
            with urllib.request.urlopen(req, timeout=30.0) as resp:
                body = resp.read().decode("utf-8")
                res = json.loads(body)
        except urllib.error.HTTPError as e:
            err_body = e.read().decode("utf-8", errors="replace")
            logger.error("Google OAuth token refresh failed (HTTP %s): %s", e.code, err_body)
            raise RuntimeError(f"Google OAuth token refresh failed ({e.code}): {err_body}") from e
        except Exception as e:
            logger.error("Google OAuth token refresh network error: %s", e)
            raise RuntimeError(f"Google OAuth token refresh request failed: {e}") from e

        access_token = res.get("access_token")
        if not access_token:
            raise RuntimeError(f"No access_token returned by Google token endpoint: {res}")

        expires_in = int(res.get("expires_in", 3600))
        now_ms = time.time() * 1000
        expires_at = now_ms + (expires_in * 1000)

        updated_refresh = res.get("refresh_token") or refresh_token

        return {
            "access_token": access_token,
            "expires_at": expires_at,
            "expires_in": expires_in,
            "refresh_token": updated_refresh,
            "token_type": res.get("token_type", "Bearer"),
        }

    def sync_if_expired(
        self,
        provider: str = "google-antigravity",
        omp_db_path: Optional[str] = None,
    ) -> bool:
        """
        Checks if token is near expiration (<120s).
        Attempts to sync from OMP database if available; if OMP DB is not available
        or returns no fresh credentials, directly renews the token via Google's
        token endpoint using refresh_token.
        """
        if omp_db_path is None:
            omp_db_path = os.path.expanduser("~/.omp/agent/agent.db")

        with sqlite3.connect(self.keyring.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT expires_at, key_id, refresh_token, raw_json
                FROM oauth_metadata
                WHERE provider = ?
                """,
                (provider,),
            )
            rows = cursor.fetchall()
            if not rows:
                return False

            now_ms = time.time() * 1000
            any_refreshed = False

            for expires_at_val, key_id, refresh_token, raw_json in rows:
                if not expires_at_val:
                    continue

                expires_at_ms = expires_at_val * 1000 if expires_at_val < 10_000_000_000 else expires_at_val

                # If expired or expiring within 120 seconds
                if expires_at_ms - now_ms < 120_000:
                    omp_synced = False
                    # 1. Attempt sync from OMP database if file exists
                    if os.path.exists(omp_db_path):
                        try:
                            imported = self.import_from_omp(omp_db_path)
                            if imported:
                                self.unblock_key(provider, key_id)
                                any_refreshed = True
                                omp_synced = True
                        except Exception as e:
                            logger.warning("Failed importing from OMP database: %s", e)

                    # 2. If OMP DB not available or didn't sync, directly renew token via Google token endpoint
                    if not omp_synced and refresh_token:
                        try:
                            client_id = None
                            client_secret = None
                            if raw_json:
                                try:
                                    raw_data = json.loads(raw_json)
                                    client_id = raw_data.get("client_id") or raw_data.get("clientId")
                                    client_secret = raw_data.get("client_secret") or raw_data.get("clientSecret")
                                except Exception:
                                    pass

                            if not client_id and provider == "google-gemini-cli":
                                client_id = GEMINI_CLI_CLIENT_ID
                                client_secret = GEMINI_CLI_CLIENT_SECRET

                            refreshed = self.refresh_google_token(
                                refresh_token=refresh_token,
                                client_id=client_id,
                                client_secret=client_secret,
                            )
                            new_access = refreshed["access_token"]
                            new_expires_at = refreshed["expires_at"]
                            new_refresh = refreshed.get("refresh_token") or refresh_token

                            # Update keyring credential
                            self.keyring.add_key(
                                provider=provider,
                                api_key=new_access,
                                key_id=key_id,
                                priority=50,
                            )

                            # Update oauth_metadata table
                            cursor.execute(
                                """
                                UPDATE oauth_metadata
                                SET refresh_token = ?, expires_at = ?
                                WHERE key_id = ?
                                """,
                                (new_refresh, new_expires_at, key_id),
                            )
                            conn.commit()

                            self.unblock_key(provider, key_id)
                            logger.info("Directly refreshed Google OAuth token for '%s' (key_id: %s)", provider, key_id)
                            any_refreshed = True
                        except Exception as err:
                            logger.error("Direct Google OAuth refresh failed for '%s' (key_id: %s): %s", provider, key_id, err)

            return any_refreshed

    def login_device_code(self, provider: str = "github-copilot") -> Dict[str, Any]:
        """
        Interactive Device Code flow (used by GitHub Copilot and other device-code providers).
        """
        client_id = "Iv1.b507a08c87ecfe81"  # Standard GitHub Copilot CLI client ID
        url = "https://github.com/login/device/code"
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": "OmniHarness/1.0",
        }
        payload = json.dumps({"client_id": client_id, "scope": "read:user"}).encode("utf-8")

        req = urllib.request.Request(url, data=payload, headers=headers)
        try:
            with urllib.request.urlopen(req) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except Exception as e:
            raise RuntimeError(f"Failed to initiate device code flow: {e}")

        device_code = data["device_code"]
        user_code = data["user_code"]
        verification_uri = data["verification_uri"]
        interval = data.get("interval", 5)
        expires_in = data.get("expires_in", 900)

        print("\n" + "=" * 60)
        print("  DEVICE CODE OAUTH LOGIN")
        print("=" * 60)
        print(f"1. Open your browser to: {verification_uri}")
        print(f"2. Enter code: {user_code}")
        print("=" * 60 + "\n")

        # Try to open browser automatically
        try:
            webbrowser.open(verification_uri)
        except Exception:
            pass

        print(f"Waiting for authorization (timeout {expires_in}s)...", end="", flush=True)
        start_time = time.time()
        token_url = "https://github.com/login/oauth/access_token"

        while time.time() - start_time < expires_in:
            time.sleep(interval)
            print(".", end="", flush=True)

            poll_payload = json.dumps({
                "client_id": client_id,
                "device_code": device_code,
                "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
            }).encode("utf-8")

            poll_req = urllib.request.Request(token_url, data=poll_payload, headers=headers)
            try:
                with urllib.request.urlopen(poll_req) as poll_resp:
                    token_data = json.loads(poll_resp.read().decode("utf-8"))
                    if "access_token" in token_data:
                        print("\n[✓] Authorization successful!")
                        access_token = token_data["access_token"]
                        key_id = f"oauth_{provider}_{int(time.time())}"
                        self.keyring.add_key(provider, access_token, key_id=key_id, priority=50)
                        return {
                            "status": "success",
                            "provider": provider,
                            "key_id": key_id,
                        }
                    error = token_data.get("error")
                    if error in ("authorization_pending", "slow_down"):
                        continue
                    if error:
                        raise RuntimeError(f"OAuth error: {error} - {token_data.get('error_description')}")
            except Exception as e:
                if "authorization_pending" in str(e):
                    continue
                raise

        raise TimeoutError("Device authorization timed out.")

    def login_browser_callback(self, provider: str, auth_url: str, port: int = 47890) -> Dict[str, Any]:
        """
        Generic PKCE/Browser callback server flow on http://127.0.0.1:port/callback.
        """
        code_holder = {"code": None, "error": None}

        class OAuthCallbackHandler(BaseHTTPRequestHandler):
            def do_GET(self):
                parsed = urllib.parse.urlparse(self.path)
                params = urllib.parse.parse_qs(parsed.query)
                if "code" in params:
                    code_holder["code"] = params["code"][0]
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.end_headers()
                    self.wfile.write(b"<h1>OmniHarness Authorization Successful!</h1><p>You can close this tab now.</p>")
                else:
                    code_holder["error"] = params.get("error", ["Unknown error"])[0]
                    self.send_response(400)
                    self.end_headers()
                    self.wfile.write(b"<h1>Authorization Failed</h1>")

            def log_message(self, format, *args):
                pass  # Silence HTTP server logs

        server = HTTPServer(("127.0.0.1", port), OAuthCallbackHandler)
        server.timeout = 180.0

        print(f"\n[*] Opening browser for {provider} OAuth login...")
        print(f"[*] If browser does not open, visit: {auth_url}")
        try:
            webbrowser.open(auth_url)
        except Exception:
            pass

        server.handle_request()
        server.server_close()

        if code_holder["code"]:
            return {"status": "code_received", "code": code_holder["code"]}
        raise RuntimeError(f"OAuth callback failed: {code_holder['error'] or 'No code returned'}")
