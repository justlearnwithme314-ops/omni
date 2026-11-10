"""OAuth for streamable-HTTP MCP servers: protected-resource / authorization-server discovery, dynamic client
registration, authorization code + PKCE with a loopback redirect, refresh. Tokens live in `<agent-dir>/mcp-auth.json`.
"""
from __future__ import annotations

import base64
import hashlib
import http.server
import json
import os
import secrets
import threading
import time
import urllib.parse
import urllib.request
import webbrowser
from pathlib import Path
from typing import Any, Dict, Optional


def _store(agent_dir: Path) -> Path:
    return Path(agent_dir) / "mcp-auth.json"


def _load(agent_dir: Path) -> Dict[str, Any]:
    try:
        return json.loads(_store(agent_dir).read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save(agent_dir: Path, data: Dict[str, Any]) -> None:
    p = _store(agent_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, indent=2), encoding="utf-8")
    try:
        os.chmod(p, 0o600)
    except OSError:
        pass


def _get_json(url: str) -> Optional[Dict[str, Any]]:
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers={"Accept": "application/json"}), timeout=15) as r:
            return json.loads(r.read().decode())
    except Exception:
        return None


def _post_form(url: str, data: Dict[str, str]) -> Dict[str, Any]:
    req = urllib.request.Request(url, data=urllib.parse.urlencode(data).encode(), method="POST",
                                 headers={"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode())


def discover(server_url: str) -> Dict[str, Any]:
    u = urllib.parse.urlparse(server_url)
    origin = f"{u.scheme}://{u.netloc}"
    prm = _get_json(f"{origin}/.well-known/oauth-protected-resource{u.path}") or _get_json(f"{origin}/.well-known/oauth-protected-resource")
    issuer = ((prm or {}).get("authorization_servers") or [origin])[0]
    iu = urllib.parse.urlparse(issuer)
    meta = (_get_json(f"{iu.scheme}://{iu.netloc}/.well-known/oauth-authorization-server{iu.path.rstrip('/')}")
            or _get_json(f"{iu.scheme}://{iu.netloc}/.well-known/openid-configuration")
            or {"authorization_endpoint": f"{issuer.rstrip('/')}/authorize", "token_endpoint": f"{issuer.rstrip('/')}/token",
                "registration_endpoint": f"{issuer.rstrip('/')}/register"})
    meta["resource"] = (prm or {}).get("resource", server_url)
    return meta


def login(agent_dir: Path, name: str, cfg: Dict[str, Any], open_browser=webbrowser.open, notify=print) -> None:
    oauth = cfg.get("oauth") or {}
    meta = discover(cfg["url"])
    # loopback redirect
    result: Dict[str, str] = {}
    done = threading.Event()

    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            q = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(self.path).query))
            result.update(q)
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(b"<html><body style='font-family:sans-serif'>Signed in. You can close this tab.</body></html>")
            done.set()

        def log_message(self, *a):
            pass

    srv = http.server.HTTPServer(("127.0.0.1", int(oauth.get("redirectPort") or 0)), H)
    redirect = f"http://127.0.0.1:{srv.server_address[1]}/callback"
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        client_id = oauth.get("clientId")
        client_secret = oauth.get("clientSecret")
        store = _load(agent_dir)
        prev = store.get(name) or {}
        if not client_id and prev.get("client_id") and prev.get("redirect_uri") == redirect:
            client_id, client_secret = prev["client_id"], prev.get("client_secret")
        if not client_id:
            if not meta.get("registration_endpoint"):
                raise RuntimeError("server does not support dynamic client registration; set oauth.clientId in mcp.json")
            req = urllib.request.Request(meta["registration_endpoint"], method="POST", headers={"Content-Type": "application/json"},
                                         data=json.dumps({"client_name": "pi", "redirect_uris": [redirect],
                                                          "grant_types": ["authorization_code", "refresh_token"],
                                                          "response_types": ["code"], "token_endpoint_auth_method": "none"}).encode())
            with urllib.request.urlopen(req, timeout=30) as r:
                reg = json.loads(r.read().decode())
            client_id, client_secret = reg["client_id"], reg.get("client_secret")
        verifier = base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode()
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        state = secrets.token_urlsafe(16)
        params = {"response_type": "code", "client_id": client_id, "redirect_uri": redirect, "code_challenge": challenge,
                  "code_challenge_method": "S256", "state": state, "resource": meta["resource"]}
        if oauth.get("scopes"):
            params["scope"] = " ".join(oauth["scopes"]) if isinstance(oauth["scopes"], list) else str(oauth["scopes"])
        url = meta["authorization_endpoint"] + "?" + urllib.parse.urlencode(params)
        notify(f"Open this URL to sign in to {name}:\n{url}")
        try:
            open_browser(url)
        except Exception:
            pass
        if not done.wait(300):
            raise RuntimeError("timed out waiting for the browser sign-in")
        if result.get("state") != state:
            raise RuntimeError("state mismatch in OAuth callback")
        if "code" not in result:
            raise RuntimeError(result.get("error_description") or result.get("error") or "no authorization code returned")
        data = {"grant_type": "authorization_code", "code": result["code"], "redirect_uri": redirect, "client_id": client_id,
                "code_verifier": verifier, "resource": meta["resource"]}
        if client_secret:
            data["client_secret"] = client_secret
        tok = _post_form(meta["token_endpoint"], data)
        store = _load(agent_dir)
        store[name] = {"url": cfg["url"], "client_id": client_id, "client_secret": client_secret, "redirect_uri": redirect,
                       "token_endpoint": meta["token_endpoint"], "resource": meta["resource"],
                       "access_token": tok["access_token"], "refresh_token": tok.get("refresh_token"),
                       "expires_at": time.time() + float(tok.get("expires_in") or 3600)}
        _save(agent_dir, store)
    finally:
        srv.shutdown()


def logout(agent_dir: Path, name: str) -> bool:
    store = _load(agent_dir)
    if name in store:
        del store[name]
        _save(agent_dir, store)
        return True
    return False


def token_for(agent_dir: Path, name: str, cfg: Dict[str, Any]) -> Optional[str]:
    store = _load(agent_dir)
    t = store.get(name)
    if not t or t.get("url") != cfg.get("url"):
        return None
    if t.get("expires_at", 0) - 60 > time.time():
        return t["access_token"]
    if not t.get("refresh_token"):
        return None
    try:
        data = {"grant_type": "refresh_token", "refresh_token": t["refresh_token"], "client_id": t["client_id"], "resource": t.get("resource")}
        if t.get("client_secret"):
            data["client_secret"] = t["client_secret"]
        tok = _post_form(t["token_endpoint"], {k: v for k, v in data.items() if v})
    except Exception:
        return None
    t.update(access_token=tok["access_token"], refresh_token=tok.get("refresh_token", t["refresh_token"]),
             expires_at=time.time() + float(tok.get("expires_in") or 3600))
    store[name] = t
    _save(agent_dir, store)
    return t["access_token"]
