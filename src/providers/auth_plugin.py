"""Config-driven provider login (API key, OAuth PKCE with localhost callback or manual code, device code).

Providers live in plugins/auth/providers.json, merged with ~/.omni/auth/providers.json (same id = override).
Tokens: access token goes in the keyring (so the router uses it); refresh data in ~/.omni/auth/tokens.json.
"""
import base64
import hashlib
import http.server
import json
import os
import secrets
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from ..paths import PROJECT_ROOT, user_data_dir
from .keyring import Keyring


class LoginError(RuntimeError):
    pass


def load_providers() -> Dict[str, Dict[str, Any]]:
    out: Dict[str, Dict[str, Any]] = {}
    for f in (PROJECT_ROOT / "plugins" / "auth" / "providers.json", user_data_dir() / "auth" / "providers.json"):
        if f.is_file():
            try:
                for p in json.loads(f.read_text(encoding="utf-8")).get("providers", []):
                    out[p["id"]] = {**out.get(p["id"], {}), **p}
            except (ValueError, KeyError) as e:
                raise LoginError(f"Bad provider file {f}: {e}")
    return out


def _tokens_file() -> Path:
    return user_data_dir() / "auth" / "tokens.json"


def _save_token(pid: str, data: dict) -> None:
    f = _tokens_file()
    f.parent.mkdir(parents=True, exist_ok=True)
    cur = json.loads(f.read_text(encoding="utf-8")) if f.is_file() else {}
    cur[pid] = data
    f.write_text(json.dumps(cur, indent=2), encoding="utf-8")
    try:
        os.chmod(f, 0o600)
    except OSError:
        pass


def pkce_pair() -> tuple:
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


def build_authorize_url(p: dict, redirect_uri: str, challenge: str, state: str) -> str:
    q = {"response_type": "code", "client_id": p["client_id"], "redirect_uri": redirect_uri, "scope": p.get("scopes", ""),
         "code_challenge": challenge, "code_challenge_method": "S256", "state": state, **p.get("extra_params", {})}
    return p["authorize_url"] + "?" + urllib.parse.urlencode(q)


def _post(url: str, data: dict, as_json: bool = False) -> dict:
    body = json.dumps(data).encode() if as_json else urllib.parse.urlencode(data).encode()
    req = urllib.request.Request(url, data=body, headers={
        "Content-Type": "application/json" if as_json else "application/x-www-form-urlencoded", "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        raise LoginError(f"{url} -> HTTP {e.code}: {e.read().decode(errors='replace')[:200]}")


def _wait_callback(port: int, path: str, state: str, timeout: float) -> str:
    box: Dict[str, str] = {}

    class H(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            u = urllib.parse.urlparse(self.path)
            q = urllib.parse.parse_qs(u.query)
            ok = u.path == path and q.get("state", [""])[0] == state and "code" in q
            if ok:
                box["code"] = q["code"][0]
            elif u.path == path:
                box["error"] = q.get("error_description", q.get("error", ["state mismatch"]))[0]
            self.send_response(200 if ok else 400)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(("<h3>Omni: logged in. You can close this tab.</h3>" if ok else "<h3>Login failed.</h3>").encode())

    try:
        srv = http.server.HTTPServer(("127.0.0.1", port), H)
    except OSError as e:
        raise LoginError(f"Cannot listen on 127.0.0.1:{port} for the login callback: {e}")
    srv.timeout = 1.0
    end = time.monotonic() + timeout
    try:
        while time.monotonic() < end and not box:
            srv.handle_request()
    finally:
        srv.server_close()
    if "error" in box:
        raise LoginError(f"Provider returned an error: {box['error']}")
    if "code" not in box:
        raise LoginError("Timed out waiting for the browser login.")
    return box["code"]


class AuthPlugin:
    def __init__(self, keyring: Optional[Keyring] = None, opener: Callable[[str], Any] = webbrowser.open,
                 ask: Callable[[str], str] = input, say: Callable[[str], None] = print):
        self.kr, self.open, self.ask, self.say = keyring or Keyring(), opener, ask, say
        self.providers = load_providers()

    def status(self) -> List[str]:
        stored = {}
        for k in self.kr.list_keys():
            stored[k["provider"]] = stored.get(k["provider"], 0) + 1
        rows = []
        for pid, p in self.providers.items():
            env = [e for e in p.get("env", []) if os.environ.get(e)]
            st = f"logged in ({stored[pid]} key)" if pid in stored else (f"env ${env[0]}" if env else "-")
            rows.append(f"  {pid:<16} {p['flow']:<11} {st:<22} {p['name']}")
        return rows

    def login(self, pid: str, api_key: Optional[str] = None) -> str:
        p = self.providers.get(pid)
        if not p:
            raise LoginError(f"Unknown provider '{pid}'. Known: {', '.join(self.providers)}. Add one in ~/.omni/auth/providers.json")
        flow = p["flow"]
        if flow == "api_key":
            return self._api_key(pid, p, api_key)
        if flow == "oauth_pkce":
            return self._pkce(pid, p)
        if flow == "device_code":
            return self._device(pid, p)
        raise LoginError(f"Unsupported flow '{flow}'")

    def _api_key(self, pid, p, key):
        src = "argument"
        if not key:
            env = next((e for e in p.get("env", []) if os.environ.get(e)), None)
            if env:
                key, src = os.environ[env], f"${env}"
            else:
                if p.get("key_url"):
                    self.say(f"Create a key at {p['key_url']}")
                key, src = self.ask(f"{p['name']} (input is visible; empty to cancel): ").strip(), "prompt"
        if not key:
            return "Cancelled."
        self.kr.add_key(pid, key)
        if p.get("base_url"):
            try:
                self.kr.set_endpoint(pid, p["base_url"], p.get("default_model", ""))
            except TypeError:
                pass
        return f"Stored {pid} key from {src}"

    def _store_tokens(self, pid, p, tok):
        access = tok.get("access_token")
        if not access:
            raise LoginError(f"No access_token in response: {str(tok)[:150]}")
        self.kr.add_key(pid, access, key_id=f"oauth_{pid}")
        _save_token(pid, {"refresh_token": tok.get("refresh_token"), "expires_at": time.time() + int(tok.get("expires_in", 3600)),
                          "token_url": p["token_url"], "client_id": p["client_id"]})
        return f"Logged in to {pid}" + (" (refresh token saved)" if tok.get("refresh_token") else "")

    def _pkce(self, pid, p):
        verifier, challenge = pkce_pair()
        state = secrets.token_urlsafe(16)
        manual = p.get("manual_code")
        redirect = p.get("redirect_uri") or f"http://localhost:{p['redirect_port']}{p.get('redirect_path', '/callback')}"
        url = build_authorize_url(p, redirect, challenge, state)
        self.say(f"Opening your browser to sign in to {p['name']}...\nIf it does not open, visit:\n{url}")
        self.open(url)
        if manual:
            raw = self.ask("Paste the code shown after login: ").strip()
            code, _, st = raw.partition("#")
            if st and st != state:
                raise LoginError("State mismatch: possible tampering; login aborted.")
        else:
            code = _wait_callback(p["redirect_port"], p.get("redirect_path", "/callback"), state, float(p.get("timeout", 180)))
        data = {"grant_type": "authorization_code", "code": code, "redirect_uri": redirect, "client_id": p["client_id"],
                "code_verifier": verifier}
        if p.get("token_format") == "json":
            data["state"] = state
        return self._store_tokens(pid, p, _post(p["token_url"], data, as_json=p.get("token_format") == "json"))

    def _device(self, pid, p):
        d = _post(p["device_url"], {"client_id": p["client_id"], "scope": p.get("scopes", "")})
        self.say(f"Go to {d.get('verification_uri')} and enter code: {d.get('user_code')}")
        self.open(d.get("verification_uri", ""))
        end, wait = time.monotonic() + int(d.get("expires_in", 900)), int(d.get("interval", 5))
        while time.monotonic() < end:
            time.sleep(wait)
            t = _post(p["token_url"], {"client_id": p["client_id"], "device_code": d["device_code"],
                                        "grant_type": "urn:ietf:params:oauth:grant-type:device_code"})
            if "access_token" in t:
                return self._store_tokens(pid, p, t)
            err = t.get("error")
            if err == "slow_down":
                wait += 5
            elif err != "authorization_pending":
                raise LoginError(f"Device login failed: {err}")
        raise LoginError("Device code expired.")


def refresh(pid: str, keyring: Optional[Keyring] = None) -> bool:
    """Refresh an OAuth token if it expires within 2 minutes. Returns True if refreshed."""
    f = _tokens_file()
    if not f.is_file():
        return False
    t = json.loads(f.read_text(encoding="utf-8")).get(pid)
    if not t or not t.get("refresh_token") or t["expires_at"] - time.time() > 120:
        return False
    tok = _post(t["token_url"], {"grant_type": "refresh_token", "refresh_token": t["refresh_token"], "client_id": t["client_id"]})
    (keyring or Keyring()).add_key(pid, tok["access_token"], key_id=f"oauth_{pid}")
    _save_token(pid, {**t, "refresh_token": tok.get("refresh_token", t["refresh_token"]),
                      "expires_at": time.time() + int(tok.get("expires_in", 3600))})
    return True
