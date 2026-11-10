"""OAuth sign-in for subscription providers, stored in Pi's `auth.json` format.

Ported from Pi (`pi-mono/packages/ai/src/auth/oauth/*`) and Oh My Pi (`oh-my-pi/packages/ai/src/registry/oauth/*`
plus the declarative rules in `packages/catalog/src/compat/rules/auth/*.kdl`), stdlib only:

    anthropic            Claude Pro/Max     PKCE, loopback callback :54545/callback (free port if busy),
                                            or paste the redirect URL / `code#state`; "copy code" for headless use
    google-antigravity   Gemini + Claude    Google PKCE, loopback :51121/oauth-callback, then Cloud Code Assist
                         via Antigravity    project discovery / free-tier onboarding (projectId kept with the token)
    openai-codex         ChatGPT Plus/Pro   PKCE on the fixed :1455/auth/callback (OpenAI allowlists only that URI; if
                                            busy, paste the redirect URL), or device code; accountId from the JWT
    github-copilot       GitHub Copilot     GitHub device code (github.com or Enterprise), then the GitHub token is
                                            exchanged for a ~30 min Copilot token: `access` = Copilot token,
                                            `refresh` = GitHub token, so a refresh is simply a new exchange

A credential is `{"type": "oauth", "access", "refresh", "expires" (ms), ...extra}` - the same shape Pi writes, so
`auth.json` stays interchangeable with Pi. `AuthStorage.get_api_key` refreshes it when it is about to expire.

Google's installed-app client for Antigravity is not bundled here. It is read from `OMNI_ANTIGRAVITY_CLIENT_ID` /
`OMNI_ANTIGRAVITY_CLIENT_SECRET`, else from an Oh My Pi checkout (`OMNI_OMP_DIR`, or `oh-my-pi` next to Omni),
the same values OMP's own login uses.
"""
from __future__ import annotations

import base64
import hashlib
import html
import http.server
import json
import os
import re
import secrets
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple


class OAuthError(RuntimeError):
    pass


class LoginCancelled(OAuthError):
    pass


# ============================================================================================ helpers
def b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def generate_pkce() -> Tuple[str, str]:
    """(verifier, S256 challenge) exactly as Pi/OMP build them: 32 random bytes, base64url without padding."""
    verifier = b64url(secrets.token_bytes(32))
    return verifier, b64url(hashlib.sha256(verifier.encode("ascii")).digest())


def parse_authorization_input(value: str) -> Tuple[Optional[str], Optional[str]]:
    """A pasted redirect URL, `code#state`, a query string, or the bare code -> (code, state)."""
    v = (value or "").strip()
    if not v:
        return None, None
    if re.match(r"^https?://", v):
        q = urllib.parse.parse_qs(urllib.parse.urlparse(v).query)
        return (q.get("code") or [None])[0], (q.get("state") or [None])[0]
    if "#" in v:
        code, state = v.split("#", 1)
        return code or None, state or None
    if "code=" in v:
        q = urllib.parse.parse_qs(v.lstrip("?"))
        return (q.get("code") or [None])[0], (q.get("state") or [None])[0]
    return v, None


def http_request(url: str, body: Optional[Dict[str, Any]] = None, *, form: bool = False, method: Optional[str] = None,
                 headers: Optional[Dict[str, str]] = None, timeout: float = 30.0) -> Tuple[int, Any, str]:
    """(status, parsed JSON or None, raw text) without raising on HTTP errors - device-code polling needs the status."""
    data = None
    h = {"Accept": "application/json"}
    if body is not None:
        if form:
            data = urllib.parse.urlencode(body).encode()
            h["Content-Type"] = "application/x-www-form-urlencoded"
        else:
            data = json.dumps(body).encode()
            h["Content-Type"] = "application/json"
    h.update(headers or {})
    req = urllib.request.Request(url, data=data, headers=h, method=method or ("POST" if data is not None else "GET"))
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            status, text = r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        status, text = e.code, e.read().decode("utf-8", "replace")
    except urllib.error.URLError as e:
        raise OAuthError(f"request to {url} failed: {e.reason}") from None
    try:
        parsed = json.loads(text) if text.strip() else None
    except ValueError:
        parsed = None
    return status, parsed, text


def http_json(url: str, body: Optional[Dict[str, Any]] = None, *, form: bool = False, method: Optional[str] = None,
              headers: Optional[Dict[str, str]] = None, timeout: float = 30.0) -> Dict[str, Any]:
    data = None
    h = {"Accept": "application/json"}
    if body is not None:
        if form:
            data = urllib.parse.urlencode(body).encode()
            h["Content-Type"] = "application/x-www-form-urlencoded"
        else:
            data = json.dumps(body).encode()
            h["Content-Type"] = "application/json"
    h.update(headers or {})
    req = urllib.request.Request(url, data=data, headers=h, method=method or ("POST" if data is not None else "GET"))
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            text = r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:800]
        raise OAuthError(f"HTTP {e.code} from {url}: {detail}") from None
    except urllib.error.URLError as e:
        raise OAuthError(f"request to {url} failed: {e.reason}") from None
    try:
        return json.loads(text) if text.strip() else {}
    except ValueError:
        raise OAuthError(f"invalid JSON from {url}: {text[:300]}") from None


def now_ms() -> int:
    return int(time.time() * 1000)


def _page(title: str, detail: str = "", ok: bool = True) -> bytes:
    color = "#16a34a" if ok else "#dc2626"
    return (f"<!doctype html><meta charset=utf-8><title>Omni sign-in</title>"
            f"<body style='font-family:system-ui;background:#0b0f17;color:#e5e7eb;display:grid;place-items:center;height:90vh'>"
            f"<div style='text-align:center'><h2 style='color:{color}'>{html.escape(title)}</h2>"
            f"<p>{html.escape(detail)}</p></div>").encode()


# ===================================================================================== callback server
class CallbackServer:
    """Loopback redirect receiver. `wait(cancel)` returns the code, or None when cancelled (e.g. code pasted)."""

    def __init__(self, provider: str, port: int, path: str, state: Optional[str], redirect_host: str = "localhost",
                 allow_fallback: bool = True):
        self.provider, self.path, self.state = provider, path, state
        self.code: Optional[str] = None
        self.error: Optional[str] = None
        self.done = threading.Event()
        self.servers: List[http.server.HTTPServer] = []
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *_a: Any) -> None:
                pass

            def do_GET(self) -> None:  # noqa: N802
                u = urllib.parse.urlparse(self.path)
                q = urllib.parse.parse_qs(u.query)
                if u.path != outer.path:
                    return self._send(404, _page("Not found", "", False))
                if outer.state is not None and (q.get("state") or [None])[0] != outer.state:
                    return self._send(400, _page("State mismatch", "Start the sign-in again from Omni.", False))
                if outer.done.is_set():
                    return self._send(409, _page("Already handled", "This sign-in was already completed.", False))
                if q.get("error"):
                    outer.error = (q.get("error_description") or q.get("error"))[0]
                    outer.done.set()
                    return self._send(400, _page(f"{outer.provider} authorization failed", outer.error, False))
                code = (q.get("code") or [None])[0]
                if not code:
                    return self._send(400, _page("Missing authorization code", "", False))
                outer.code = code
                outer.done.set()
                self._send(200, _page(f"Signed in to {outer.provider}", "You can close this tab and return to Omni."))

            def _send(self, status: int, body: bytes) -> None:
                self.send_response(status)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)

        hosts = ["127.0.0.1", "::1"] if redirect_host == "localhost" else [redirect_host]
        bound = None
        for p in ([port, 0] if allow_fallback else [port]):
            try:
                srv = self._bind(hosts[0], p, Handler)
                bound = srv.server_address[1]
                self.servers.append(srv)
                break
            except OSError:
                continue
        if bound is None:
            raise OAuthError(f"port {port} is busy (another sign-in or app is using it); close it and retry")
        for h in hosts[1:]:  # `localhost` may resolve to ::1 in the browser
            try:
                self.servers.append(self._bind(h, bound, Handler))
            except OSError:
                pass
        for srv in self.servers:
            threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.2}, daemon=True).start()
        host = f"[{redirect_host}]" if ":" in redirect_host else redirect_host
        self.redirect_uri = f"http://{host}:{bound}{path}"

    @staticmethod
    def _bind(host: str, port: int, handler) -> http.server.HTTPServer:
        cls = http.server.ThreadingHTTPServer
        if ":" in host:
            class V6(cls):  # type: ignore[valid-type,misc]
                address_family = socket.AF_INET6
            cls = V6
        srv = cls((host, port), handler)
        srv.daemon_threads = True
        return srv

    def wait(self, cancel: Optional[threading.Event] = None, timeout: float = 300.0) -> Optional[str]:
        deadline = time.time() + timeout
        while not self.done.wait(0.2):
            if cancel is not None and cancel.is_set():
                return None
            if time.time() > deadline:
                raise OAuthError(f"{self.provider} sign-in timed out")
        if self.error:
            raise OAuthError(f"{self.provider} authorization failed: {self.error}")
        return self.code

    def close(self) -> None:
        for srv in self.servers:
            try:
                srv.shutdown()
                srv.server_close()
            except Exception:
                pass


# ======================================================================================= interaction
class Interaction:
    """What a login flow needs from the UI. The default is a plain terminal (stdin/stdout)."""

    def notify(self, text: str, level: str = "info") -> None:
        print(text)

    def open_url(self, url: str) -> None:
        try:
            import webbrowser
            webbrowser.open(url)
        except Exception:
            pass

    def select(self, title: str, options: List[str]) -> Optional[int]:
        print(title)
        for i, o in enumerate(options, 1):
            print(f"  {i}. {o}")
        try:
            v = input("> ").strip()
        except EOFError:
            return None
        return int(v) - 1 if v.isdigit() and 0 < int(v) <= len(options) else (0 if not v else None)

    def manual(self, message: str, placeholder: str, cancel: Optional[threading.Event] = None) -> Optional[str]:
        """Ask for a pasted code / redirect URL. Must return None soon after `cancel` is set."""
        box: Dict[str, Optional[str]] = {}
        done = threading.Event()

        def read() -> None:
            try:
                box["v"] = input(f"{message}\n> ")
            except EOFError:
                box["v"] = None
            done.set()

        threading.Thread(target=read, daemon=True).start()
        while not done.wait(0.2):
            if cancel is not None and cancel.is_set():
                return None
        return box.get("v")

    def wait_cancel(self, message: str, done: threading.Event) -> bool:
        """Show `message` until `done` is set (a device code is being approved). True if the user gave up first."""
        import sys
        try:
            tty = bool(sys.stdin) and sys.stdin.isatty()
        except (ValueError, OSError):
            tty = False
        if not tty:  # nothing to read a "cancel" from (EOF would look like one): just wait
            done.wait()
            return False
        v = self.manual(f"{message}\n(press Enter to cancel)", "", done)
        return v is not None and not done.is_set()


def wait_callback_or_paste(ia: Interaction, cb: Optional[CallbackServer], message: str, placeholder: str
                           ) -> Tuple[str, Optional[str]]:
    """Pi's waitForCallbackOrManualInput: whichever arrives first, the browser redirect or a pasted code.
    Returns ("callback", code) or ("manual", pasted text)."""
    if cb is None:
        v = ia.manual(message, placeholder)
        if not v:
            raise LoginCancelled("Login cancelled")
        return "manual", v
    got_callback = threading.Event()
    pasted: Dict[str, Optional[str]] = {}
    pasted_evt = threading.Event()

    def ask() -> None:
        pasted["v"] = ia.manual(message, placeholder, got_callback)
        pasted_evt.set()

    threading.Thread(target=ask, daemon=True, name="oauth-paste").start()
    code = cb.wait(pasted_evt)
    if code is not None:
        got_callback.set()
        return "callback", code
    if pasted.get("v"):
        return "manual", pasted["v"]  # type: ignore[return-value]
    raise LoginCancelled("Login cancelled")


DEVICE_MIN_INTERVAL = 1.0      # seconds; floor for a provider's polling interval (RFC 8628 default is 5)
DEVICE_SLOW_DOWN_STEP = 5.0    # RFC 8628: add 5s on every slow_down


def poll_device_code(ia: Interaction, poll: Callable[[], Tuple[str, Any]], interval: Optional[float],
                     expires_in: float, message: str) -> Any:
    """Pi's pollOAuthDeviceCodeFlow. `poll()` returns ("complete", value) | ("pending", _) | ("slow_down", secs|None)
    | ("failed", message). The UI shows `message` meanwhile; closing that prompt (Esc / Enter) cancels the login."""
    finished, cancelled = threading.Event(), threading.Event()

    def watch() -> None:
        fn = getattr(ia, "wait_cancel", None)
        if fn is not None:
            hit = fn(message, finished)
        else:  # a UI port adapter: its cancellable prompt returns early only when the user closes it
            hit = ia.manual(message, "", finished) is not None or not finished.is_set()
        if hit and not finished.is_set():
            cancelled.set()

    threading.Thread(target=watch, daemon=True, name="oauth-device-wait").start()
    deadline = time.time() + float(expires_in)
    wait = max(DEVICE_MIN_INTERVAL, float(interval if interval is not None else 5))
    slow_downs = 0
    try:
        while time.time() < deadline:
            if cancelled.wait(max(0.0, min(wait, deadline - time.time()))):
                raise LoginCancelled("Login cancelled")
            status, value = poll()
            if cancelled.is_set():
                raise LoginCancelled("Login cancelled")
            if status == "complete":
                return value
            if status == "failed":
                raise OAuthError(str(value))
            if status == "slow_down":
                slow_downs += 1
                wait = max(DEVICE_MIN_INTERVAL, float(value)) if value else wait + DEVICE_SLOW_DOWN_STEP
    finally:
        finished.set()
    if slow_downs:
        raise OAuthError("Device flow timed out after one or more slow_down responses. This is often caused by clock "
                         "drift in WSL or VM environments; sync the clock and try again.")
    raise OAuthError("Device flow timed out")


# ========================================================================================= providers
@dataclass
class OAuthProvider:
    id: str
    name: str
    login: Callable[[Interaction], Dict[str, Any]]
    refresh: Callable[[Dict[str, Any]], Dict[str, Any]]
    available: Callable[[], Optional[str]] = lambda: None  # reason it cannot be used, or None
    # credential -> the apiKey handed to the stream (Pi's `oauth.getApiKey`); None means `credential["access"]`
    api_key: Optional[Callable[[Dict[str, Any]], str]] = None
    source: str = "core"  # "core" or "extension"


# ---------------------------------------------------------------------------------------- anthropic
ANTHROPIC_CLIENT_ID = base64.b64decode("OWQxYzI1MGEtZTYxYi00NGQ5LTg4ZWQtNTk0NGQxOTYyZjVl").decode()
ANTHROPIC_AUTHORIZE_URL = "https://claude.ai/oauth/authorize"
ANTHROPIC_TOKEN_URL = "https://api.anthropic.com/v1/oauth/token"  # OMP; claude.ai tokens allow direct inference
ANTHROPIC_PORT, ANTHROPIC_PATH = 54545, "/callback"
ANTHROPIC_COPY_CODE_REDIRECT = "https://platform.claude.com/oauth/code/callback"
ANTHROPIC_SCOPES = "org:create_api_key user:profile user:inference user:sessions:claude_code user:mcp_servers user:file_upload"


def _anthropic_credential(tok: Dict[str, Any], previous: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    if not tok.get("access_token"):
        raise OAuthError(f"Anthropic returned no access token: {str(tok)[:200]}")
    cred = dict(previous or {})
    cred.update({"type": "oauth", "access": tok["access_token"], "refresh": tok.get("refresh_token") or cred.get("refresh"),
                 "expires": now_ms() + int(tok.get("expires_in", 3600)) * 1000 - 5 * 60 * 1000})
    acct, org = tok.get("account") or {}, tok.get("organization") or {}
    if acct.get("email_address"):
        cred["email"] = acct["email_address"]
    if org.get("name"):
        cred["orgName"] = org["name"]
    return cred


def _anthropic_exchange(code: str, state: str, verifier: str, redirect_uri: str) -> Dict[str, Any]:
    tok = http_json(ANTHROPIC_TOKEN_URL, {"grant_type": "authorization_code", "client_id": ANTHROPIC_CLIENT_ID,
                                          "code": code, "state": state, "redirect_uri": redirect_uri,
                                          "code_verifier": verifier})
    return _anthropic_credential(tok)


def _anthropic_authorize_url(redirect_uri: str, challenge: str, state: str) -> str:
    return ANTHROPIC_AUTHORIZE_URL + "?" + urllib.parse.urlencode({
        "code": "true", "client_id": ANTHROPIC_CLIENT_ID, "response_type": "code", "redirect_uri": redirect_uri,
        "scope": ANTHROPIC_SCOPES, "code_challenge": challenge, "code_challenge_method": "S256", "state": state})


def login_anthropic(ia: Interaction) -> Dict[str, Any]:
    method = ia.select("Anthropic (Claude Pro/Max) sign-in", ["Browser login (default)",
                                                             "Copy code login (headless / SSH)"])
    if method is None:
        raise LoginCancelled("Login cancelled")
    verifier, challenge = generate_pkce()
    if method == 1:
        url = _anthropic_authorize_url(ANTHROPIC_COPY_CODE_REDIRECT, challenge, verifier)
        ia.notify(f"Open this URL, sign in, then paste the code Anthropic shows:\n{url}", "info")
        ia.open_url(url)
        raw = ia.manual("Paste the code Anthropic shows after you sign in", "code#state")
        code, state = parse_authorization_input(raw or "")
        if state and state != verifier:
            raise OAuthError("OAuth state mismatch")
        if not code:
            raise LoginCancelled("Login cancelled")
        ia.notify("Exchanging authorization code for tokens...", "info")
        return _anthropic_exchange(code, state or verifier, verifier, ANTHROPIC_COPY_CODE_REDIRECT)
    cb: Optional[CallbackServer]
    try:
        cb = CallbackServer("Anthropic", ANTHROPIC_PORT, ANTHROPIC_PATH, verifier, "localhost")
    except OAuthError:
        cb = None
    redirect_uri = cb.redirect_uri if cb else f"http://localhost:{ANTHROPIC_PORT}{ANTHROPIC_PATH}"
    try:
        url = _anthropic_authorize_url(redirect_uri, challenge, verifier)
        ia.notify("Complete the sign-in in your browser. If it opened on another machine, paste the final "
                  f"redirect URL here.\n{url}", "info")
        ia.open_url(url)
        kind, value = wait_callback_or_paste(ia, cb, "Waiting for the browser... or paste the redirect URL / code",
                                             redirect_uri)
        state = verifier
        if kind == "manual":
            code, st = parse_authorization_input(value)
            if st and st != verifier:
                raise OAuthError("OAuth state mismatch")
            state = st or verifier
        else:
            code = value
        if not code:
            raise OAuthError("Missing authorization code")
        ia.notify("Exchanging authorization code for tokens...", "info")
        return _anthropic_exchange(code, state, verifier, redirect_uri)
    finally:
        if cb:
            cb.close()


def refresh_anthropic(cred: Dict[str, Any]) -> Dict[str, Any]:
    if not cred.get("refresh"):
        raise OAuthError("no refresh token; sign in again with /login anthropic")
    tok = http_json(ANTHROPIC_TOKEN_URL, {"grant_type": "refresh_token", "client_id": ANTHROPIC_CLIENT_ID,
                                          "refresh_token": cred["refresh"]},
                    headers={"anthropic-beta": "oauth-2025-04-20"})
    return _anthropic_credential(tok, cred)


# ------------------------------------------------------------------------------- google antigravity
GOOGLE_AUTHORIZE_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_USERINFO_URL = "https://www.googleapis.com/oauth2/v1/userinfo?alt=json"
ANTIGRAVITY_PORT, ANTIGRAVITY_PATH = 51121, "/oauth-callback"
ANTIGRAVITY_SCOPES = " ".join([
    "https://www.googleapis.com/auth/cloud-platform", "https://www.googleapis.com/auth/userinfo.email",
    "https://www.googleapis.com/auth/userinfo.profile", "https://www.googleapis.com/auth/cclog",
    "https://www.googleapis.com/auth/experimentsandconfigs", "https://www.googleapis.com/auth/aicode"])
CLOUD_CODE_ASSIST = "https://daily-cloudcode-pa.googleapis.com"
ANTIGRAVITY_UA = "antigravity/hub/2.26.0 (aidev_client; os_type=windows; arch=amd64; cl=1005016104)"
_ANTIGRAVITY_META = {"ideType": "ANTIGRAVITY"}


def _omp_dirs() -> List[Path]:
    here = Path(__file__).resolve()
    root = here.parents[3]  # .../omni
    out = []
    if os.environ.get("OMNI_OMP_DIR"):
        out.append(Path(os.path.expanduser(os.environ["OMNI_OMP_DIR"])))
    out += [root.parent / "oh-my-pi", root.parent / "references" / "oh-my-pi", Path.home() / "oh-my-pi"]
    return out


def _kdl_value(text: str, key: str) -> Optional[str]:
    m = re.search(rf'^\s*{re.escape(key)}\s+"([^"]+)"(\s+encoding="base64")?', text, re.M)
    if not m:
        return None
    return base64.b64decode(m.group(1)).decode() if m.group(2) else m.group(1)


def omp_client(provider: str) -> Tuple[Optional[str], Optional[str]]:
    """client id / secret from OMP's declarative auth rules (`catalog/src/compat/rules/auth/<provider>.kdl`)."""
    for d in _omp_dirs():
        f = d / "packages" / "catalog" / "src" / "compat" / "rules" / "auth" / f"{provider}.kdl"
        if f.is_file():
            try:
                t = f.read_text(encoding="utf-8")
            except OSError:
                continue
            cid, sec = _kdl_value(t, "client-id"), _kdl_value(t, "client-secret")
            if cid:
                return cid, sec
    return None, None


def antigravity_client() -> Tuple[Optional[str], Optional[str]]:
    cid = os.environ.get("OMNI_ANTIGRAVITY_CLIENT_ID") or os.environ.get("GOOGLE_CLIENT_ID")
    sec = os.environ.get("OMNI_ANTIGRAVITY_CLIENT_SECRET") or os.environ.get("GOOGLE_CLIENT_SECRET")
    if cid:
        return cid, sec
    return omp_client("google-antigravity")


def _antigravity_unavailable() -> Optional[str]:
    cid, _ = antigravity_client()
    return None if cid else ("needs the Antigravity OAuth client: set OMNI_ANTIGRAVITY_CLIENT_ID/_SECRET or "
                             "OMNI_OMP_DIR to an Oh My Pi checkout")


def _cloud_code(path: str, access: str, body: Optional[Dict[str, Any]] = None, method: str = "POST") -> Dict[str, Any]:
    return http_json(f"{CLOUD_CODE_ASSIST}/v1internal{path}", body, method=method,
                     headers={"Authorization": f"Bearer {access}", "User-Agent": ANTIGRAVITY_UA})


def discover_antigravity_project(access: str, notify: Callable[[str], None] = lambda _m: None) -> str:
    """OMP's googleAntigravityProjectHook: loadCodeAssist, onboard the free tier when needed, return the project."""
    def load() -> Dict[str, Any]:
        r = _cloud_code(":loadCodeAssist", access, {"metadata": _ANTIGRAVITY_META})
        pid = r.get("cloudaicompanionProject")
        if r.get("paidTier") is None and pid:
            r = _cloud_code(":loadCodeAssist", access, {"cloudaicompanionProject": pid, "metadata": _ANTIGRAVITY_META})
        return r

    notify("Checking Cloud Code Assist account status...")
    first = load()
    allowed = any(t.get("id") == "free-tier" for t in first.get("allowedTiers") or [])
    if not allowed:
        bad = next((t for t in first.get("ineligibleTiers") or [] if t.get("tierId") == "free-tier"), None)
        if bad and bad.get("reasonMessage"):
            raise OAuthError(bad["reasonMessage"] + (f"\n{bad['validationUrl']}" if bad.get("validationUrl") else ""))
    if first.get("currentTier") is None:
        notify("Provisioning the Antigravity free tier...")
        op = _cloud_code(":onboardUser", access, {"tierId": "free-tier", "metadata": _ANTIGRAVITY_META})
        deadline = time.time() + 30
        while not op.get("done"):
            if time.time() > deadline:
                raise OAuthError("onboardUser timed out after 30s")
            if not op.get("name"):
                raise OAuthError("onboardUser returned an operation without a name")
            time.sleep(1)
            op = _cloud_code(f"/{op['name']}", access, method="GET")
        if op.get("error"):
            raise OAuthError(f"OnboardUser operation failed: {op['error'].get('message') or op['error']}")
    notify("Refreshing Cloud Code Assist project...")
    pid = load().get("cloudaicompanionProject")
    if not pid:
        raise OAuthError("loadCodeAssist did not return a cloudaicompanionProject")
    return pid


def _google_credential(tok: Dict[str, Any], previous: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    if not tok.get("access_token"):
        raise OAuthError(f"Google returned no access token: {str(tok)[:200]}")
    cred = dict(previous or {})
    cred.update({"type": "oauth", "access": tok["access_token"], "refresh": tok.get("refresh_token") or cred.get("refresh"),
                 "expires": now_ms() + int(tok.get("expires_in", 3600)) * 1000 - 5 * 60 * 1000})
    return cred


def login_antigravity(ia: Interaction) -> Dict[str, Any]:
    cid, secret = antigravity_client()
    if not cid:
        raise OAuthError(_antigravity_unavailable() or "no client")
    verifier, challenge = generate_pkce()
    state = b64url(secrets.token_bytes(16))
    cb = CallbackServer("Google Antigravity", ANTIGRAVITY_PORT, ANTIGRAVITY_PATH, state, "127.0.0.1",
                        allow_fallback=False)
    try:
        url = GOOGLE_AUTHORIZE_URL + "?" + urllib.parse.urlencode({
            "client_id": cid, "response_type": "code", "redirect_uri": cb.redirect_uri, "scope": ANTIGRAVITY_SCOPES,
            "code_challenge": challenge, "code_challenge_method": "S256", "state": state,
            "access_type": "offline", "prompt": "consent"})
        ia.notify(f"Complete the sign-in in your browser.\n{url}", "info")
        ia.open_url(url)
        kind, value = wait_callback_or_paste(ia, cb, "Waiting for the browser... or paste the redirect URL",
                                             cb.redirect_uri)
        code = value
        if kind == "manual":
            code, st = parse_authorization_input(value)
            if st and st != state:
                raise OAuthError("OAuth state mismatch")
        if not code:
            raise OAuthError("Missing authorization code")
    finally:
        cb.close()
    ia.notify("Exchanging authorization code for tokens...", "info")
    body = {"grant_type": "authorization_code", "code": code, "client_id": cid, "redirect_uri": cb.redirect_uri,
            "code_verifier": verifier}
    if secret:
        body["client_secret"] = secret
    tok = http_json(GOOGLE_TOKEN_URL, body, form=True)
    if not tok.get("refresh_token"):
        raise OAuthError("No refresh token received. Please try again.")
    cred = _google_credential(tok)
    try:
        cred["email"] = http_json(GOOGLE_USERINFO_URL, headers={"Authorization": f"Bearer {cred['access']}"}).get("email")
    except OAuthError:
        pass
    cred["projectId"] = discover_antigravity_project(cred["access"], lambda m: ia.notify(m, "info"))
    return cred


def refresh_antigravity(cred: Dict[str, Any]) -> Dict[str, Any]:
    cid, secret = antigravity_client()
    if not cid or not cred.get("refresh"):
        raise OAuthError("cannot refresh: " + (_antigravity_unavailable() or "no refresh token"))
    body = {"grant_type": "refresh_token", "refresh_token": cred["refresh"], "client_id": cid}
    if secret:
        body["client_secret"] = secret
    return _google_credential(http_json(GOOGLE_TOKEN_URL, body, form=True), cred)  # keeps projectId / email


# ------------------------------------------------------------------------------------- openai codex
CODEX_CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
CODEX_AUTH_BASE = "https://auth.openai.com"
# OpenAI allowlists exactly http://localhost:1455/auth/callback (shared with the Codex CLI): no free-port fallback.
CODEX_PORT, CODEX_PATH = 1455, "/auth/callback"
CODEX_SCOPES = "openid profile email offline_access api.connectors.read api.connectors.invoke"
CODEX_ORIGINATOR = "pi"
CODEX_DEVICE_TIMEOUT = 15 * 60
JWT_AUTH_CLAIM = "https://api.openai.com/auth"
JWT_PROFILE_CLAIM = "https://api.openai.com/profile"


def decode_jwt(token: str) -> Optional[Dict[str, Any]]:
    try:
        parts = (token or "").split(".")
        if len(parts) != 3:
            return None
        raw = parts[1] + "=" * (-len(parts[1]) % 4)
        payload = json.loads(base64.urlsafe_b64decode(raw.encode("ascii")).decode("utf-8"))
        return payload if isinstance(payload, dict) else None
    except Exception:
        return None


def codex_account_id(access: str, id_token: Optional[str] = None) -> Optional[str]:
    for tok in (access, id_token):
        aid = ((decode_jwt(tok or "") or {}).get(JWT_AUTH_CLAIM) or {}).get("chatgpt_account_id")
        if isinstance(aid, str) and aid:
            return aid
    return None


def _codex_redirect_uri() -> str:
    return f"http://localhost:{CODEX_PORT}{CODEX_PATH}"


def _codex_credential(tok: Any, previous: Optional[Dict[str, Any]] = None, phase: str = "login") -> Dict[str, Any]:
    """OMP's openAICodexProfileHook: tokens + ChatGPT workspace (accountId), email and plan from the JWT claims."""
    tok = tok if isinstance(tok, dict) else {}
    prev = dict(previous or {})
    if not tok.get("access_token") or not (tok.get("refresh_token") or prev.get("refresh")) \
            or not isinstance(tok.get("expires_in"), (int, float)):
        raise OAuthError(f"OpenAI Codex token {phase} response missing fields: {str(tok)[:200]}")
    access, id_token = tok["access_token"], tok.get("id_token")
    claims = [decode_jwt(t) or {} for t in (access, id_token or "")]
    account = codex_account_id(access, id_token) or prev.get("accountId")
    if not account:
        raise OAuthError("Failed to extract the ChatGPT account id from the token")
    email = next((c.get(JWT_PROFILE_CLAIM, {}).get("email") for c in claims if c.get(JWT_PROFILE_CLAIM, {}).get("email")),
                 None) or next((c.get("email") for c in claims if c.get("email")), None)
    plan = next((c.get(JWT_AUTH_CLAIM, {}).get("chatgpt_plan_type") for c in claims
                 if c.get(JWT_AUTH_CLAIM, {}).get("chatgpt_plan_type")), None)
    prev.update({"type": "oauth", "access": access, "refresh": tok.get("refresh_token") or prev.get("refresh"),
                 "expires": now_ms() + int(tok["expires_in"] * 1000), "accountId": account})
    if email:
        prev["email"] = str(email).strip().lower()
    if plan:
        prev["orgName"] = str(plan).strip().lower()
    return prev


def _codex_token(body: Dict[str, str], phase: str, previous: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    status, data, text = http_request(f"{CODEX_AUTH_BASE}/oauth/token", dict(body, client_id=CODEX_CLIENT_ID),
                                      form=True, timeout=15)
    if status >= 400:
        err = data.get("error") if isinstance(data, dict) else None
        desc = data.get("error_description") if isinstance(data, dict) else None
        if isinstance(err, dict):
            err, desc = err.get("code") or err.get("type"), desc or err.get("message")
        detail = ": ".join(str(x) for x in (err, desc) if x) or text.strip()[:300]
        raise OAuthError(f"OpenAI Codex token {phase} failed ({status}): {detail}")
    return _codex_credential(data, previous, phase)


def _codex_exchange(code: str, verifier: str, redirect_uri: str) -> Dict[str, Any]:
    return _codex_token({"grant_type": "authorization_code", "code": code, "code_verifier": verifier,
                         "redirect_uri": redirect_uri}, "exchange")


def _codex_authorize_url(redirect_uri: str, challenge: str, state: str) -> str:
    return f"{CODEX_AUTH_BASE}/oauth/authorize?" + urllib.parse.urlencode({
        "response_type": "code", "client_id": CODEX_CLIENT_ID, "redirect_uri": redirect_uri, "scope": CODEX_SCOPES,
        "code_challenge": challenge, "code_challenge_method": "S256", "state": state,
        "id_token_add_organizations": "true", "codex_cli_simplified_flow": "true", "originator": CODEX_ORIGINATOR})


def _login_codex_browser(ia: Interaction) -> Dict[str, Any]:
    verifier, challenge = generate_pkce()
    state = secrets.token_hex(16)
    redirect_uri = _codex_redirect_uri()
    cb: Optional[CallbackServer]
    try:
        cb = CallbackServer("OpenAI", CODEX_PORT, CODEX_PATH, state, "localhost", allow_fallback=False)
    except OAuthError:  # usually the Codex CLI's own login; fall back to pasting the redirect URL
        cb = None
        ia.notify(f"Port {CODEX_PORT} is busy (another Codex sign-in?). After signing in, paste the URL the browser "
                  "ends on (it will fail to load) here.", "warning")
    try:
        url = _codex_authorize_url(redirect_uri, challenge, state)
        ia.notify(f"A browser window should open. Complete login to finish.\n{url}", "info")
        ia.open_url(url)
        kind, value = wait_callback_or_paste(
            ia, cb, "Complete login in your browser, or paste the authorization code / redirect URL here", redirect_uri)
        code = value
        if kind == "manual":
            code, st = parse_authorization_input(value)
            if st and st != state:
                raise OAuthError("OAuth state mismatch")
        if not code:
            raise OAuthError("Missing authorization code")
    finally:
        if cb:
            cb.close()
    ia.notify("Exchanging authorization code for tokens...", "info")
    return _codex_exchange(code, verifier, redirect_uri)


def _login_codex_device(ia: Interaction) -> Dict[str, Any]:
    ia.notify("Initiating device authorization...", "info")
    status, data, text = http_request(f"{CODEX_AUTH_BASE}/api/accounts/deviceauth/usercode",
                                      {"client_id": CODEX_CLIENT_ID}, timeout=15)
    if status == 404:
        raise OAuthError("OpenAI Codex device code login is not enabled for this account/server. Use browser login.")
    if status >= 400:
        raise OAuthError(f"OpenAI Codex device code request failed ({status}): {text.strip()[:300]}")
    data = data if isinstance(data, dict) else {}
    try:
        interval = float(str(data.get("interval", 5)).strip())
    except ValueError:
        interval = -1
    if not data.get("device_auth_id") or not data.get("user_code") or interval < 0:
        raise OAuthError(f"Invalid OpenAI Codex device code response: {str(data)[:200]}")
    device_id, user_code = data["device_auth_id"], data["user_code"]
    verify_url = f"{CODEX_AUTH_BASE}/codex/device"
    ia.notify(f"Open {verify_url} and enter the code: {user_code}", "info")
    ia.open_url(verify_url)

    def poll() -> Tuple[str, Any]:
        st, d, raw = http_request(f"{CODEX_AUTH_BASE}/api/accounts/deviceauth/token",
                                  {"device_auth_id": device_id, "user_code": user_code}, timeout=15)
        if st < 400:
            if not isinstance(d, dict) or not d.get("authorization_code") or not d.get("code_verifier"):
                return "failed", f"Invalid OpenAI Codex device auth token response: {str(d)[:200]}"
            return "complete", (d["authorization_code"], d["code_verifier"])
        if st in (403, 404):
            return "pending", None
        err = d.get("error") if isinstance(d, dict) else None
        code = err.get("code") if isinstance(err, dict) else err
        if code == "deviceauth_authorization_pending":
            return "pending", None
        if code == "slow_down":
            return "slow_down", None
        return "failed", f"OpenAI Codex device auth failed ({st}): {raw.strip()[:300]}"

    auth_code, verifier = poll_device_code(ia, poll, interval, CODEX_DEVICE_TIMEOUT,
                                           f"Waiting for browser authorization (code: {user_code})...")
    ia.notify("Exchanging authorization code for tokens...", "info")
    return _codex_exchange(auth_code, verifier, f"{CODEX_AUTH_BASE}/deviceauth/callback")


def login_openai_codex(ia: Interaction) -> Dict[str, Any]:
    method = ia.select("OpenAI Codex (ChatGPT Plus/Pro) sign-in", ["Browser login (default)",
                                                                   "Device code login (headless / SSH)"])
    if method is None:
        raise LoginCancelled("Login cancelled")
    return _login_codex_device(ia) if method == 1 else _login_codex_browser(ia)


def refresh_openai_codex(cred: Dict[str, Any]) -> Dict[str, Any]:
    if not cred.get("refresh"):
        raise OAuthError("no refresh token; sign in again with /login openai-codex")
    return _codex_token({"grant_type": "refresh_token", "refresh_token": cred["refresh"]}, "refresh", cred)


# ----------------------------------------------------------------------------------- github copilot
COPILOT_CLIENT_ID = base64.b64decode("SXYxLmI1MDdhMDhjODdlY2ZlOTg=").decode()  # Pi's (VS Code Copilot Chat app)
COPILOT_HEADERS = {"User-Agent": "GitHubCopilotChat/0.35.0", "Editor-Version": "vscode/1.107.0",
                   "Editor-Plugin-Version": "copilot-chat/0.35.0", "Copilot-Integration-Id": "vscode-chat"}
GITHUB_WEB = "https://{domain}"         # device-code endpoints  (tests point these at a fake server)
GITHUB_API = "https://api.{domain}"     # copilot_internal/v2/token exchange
COPILOT_DEFAULT_BASE_URL = "https://api.individual.githubcopilot.com"


def normalize_domain(value: str) -> Optional[str]:
    v = (value or "").strip()
    if not v:
        return None
    try:
        host = urllib.parse.urlparse(v if "://" in v else f"https://{v}").hostname
    except ValueError:
        return None
    return host.lower() if host and "." in host else None


def copilot_base_url(token: str = "", enterprise: Optional[str] = None) -> str:
    """API base for an exchanged Copilot token: its `proxy-ep=proxy.X` becomes `https://api.X`."""
    m = re.search(r"(?:^|;)\s*proxy-ep=([^;]+)", token or "")
    if m:
        return "https://" + re.sub(r"^proxy\.", "api.", m.group(1).strip())
    if enterprise:
        return f"https://copilot-api.{enterprise}"
    return COPILOT_DEFAULT_BASE_URL


def _copilot_exchange(github_token: str, enterprise: Optional[str], previous: Optional[Dict[str, Any]] = None
                      ) -> Dict[str, Any]:
    """GitHub OAuth token -> short-lived Copilot API token (Pi's refreshGitHubCopilotAccessToken)."""
    url = GITHUB_API.format(domain=enterprise or "github.com") + "/copilot_internal/v2/token"
    status, data, text = http_request(url, headers={"Authorization": f"Bearer {github_token}", **COPILOT_HEADERS})
    if status == 401 or status == 403:
        raise OAuthError(f"GitHub refused the Copilot token exchange ({status}); is Copilot enabled for this "
                         f"account? {text.strip()[:200]}")
    if status >= 400:
        raise OAuthError(f"Copilot token exchange failed ({status}): {text.strip()[:300]}")
    if not isinstance(data, dict) or not isinstance(data.get("token"), str) \
            or not isinstance(data.get("expires_at"), (int, float)):
        raise OAuthError("Invalid Copilot token response")
    cred = dict(previous or {})
    cred.update({"type": "oauth", "access": data["token"], "refresh": github_token,
                 "expires": int(data["expires_at"] * 1000) - 5 * 60 * 1000})
    if enterprise:
        cred["enterpriseUrl"] = enterprise
    else:
        cred.pop("enterpriseUrl", None)
    return cred


def _copilot_enable_models(token: str, enterprise: Optional[str], notify: Callable[[str], None]) -> List[str]:
    """Best effort: accept the model policy for catalog models that need it (Claude, Gemini, ...)."""
    from .models import BUILTIN_MODELS
    ids = [m["id"] for m in BUILTIN_MODELS if m["provider"] == "github-copilot"]
    if not ids:
        return []
    notify("Enabling models...")
    base, enabled = copilot_base_url(token, enterprise), []
    for mid in ids:
        try:
            st, _d, _t = http_request(f"{base}/models/{mid}/policy", {"state": "enabled"}, timeout=5, headers={
                "Authorization": f"Bearer {token}", **COPILOT_HEADERS, "openai-intent": "chat-policy",
                "x-interaction-type": "chat-policy"})
        except OAuthError:
            break  # offline / blocked: the models may still work, the policy can be accepted on github.com
        if st == 429:
            break
        if st < 400:
            enabled.append(mid)
    return enabled


def login_github_copilot(ia: Interaction) -> Dict[str, Any]:
    where = ia.select("GitHub Copilot sign-in", ["GitHub.com (default)", "GitHub Enterprise (custom domain)"])
    if where is None:
        raise LoginCancelled("Login cancelled")
    enterprise = None
    if where == 1:
        raw = ia.manual("GitHub Enterprise URL/domain", "company.ghe.com")
        if raw is None:
            raise LoginCancelled("Login cancelled")
        enterprise = normalize_domain(raw)
        if raw.strip() and not enterprise:
            raise OAuthError("Invalid GitHub Enterprise URL/domain")
        if enterprise in ("github.com", "www.github.com", "api.github.com"):
            enterprise = None
    domain = enterprise or "github.com"
    web = GITHUB_WEB.format(domain=domain)
    gh_headers = {"User-Agent": COPILOT_HEADERS["User-Agent"]}
    dev = http_json(f"{web}/login/device/code", {"client_id": COPILOT_CLIENT_ID, "scope": "read:user"}, form=True,
                    headers=gh_headers)
    verify = dev.get("verification_uri")
    if not isinstance(dev.get("device_code"), str) or not isinstance(dev.get("user_code"), str) \
            or not isinstance(verify, str) or not isinstance(dev.get("expires_in"), (int, float)):
        raise OAuthError("Invalid device code response")
    if urllib.parse.urlparse(verify).scheme not in ("https", "http"):
        raise OAuthError("Untrusted verification_uri in device code response")
    ia.notify(f"Open {verify} and enter the code: {dev['user_code']}", "info")
    ia.open_url(verify)

    def poll() -> Tuple[str, Any]:
        r = http_json(f"{web}/login/oauth/access_token", {"client_id": COPILOT_CLIENT_ID, "device_code": dev["device_code"],
                      "grant_type": "urn:ietf:params:oauth:grant-type:device_code"}, form=True, headers=gh_headers)
        if isinstance(r.get("access_token"), str):
            return "complete", r["access_token"]
        err = r.get("error")
        if err == "authorization_pending":
            return "pending", None
        if err == "slow_down":
            return "slow_down", r.get("interval") if isinstance(r.get("interval"), (int, float)) else None
        if err:
            return "failed", f"Device flow failed: {err}" + (f": {r['error_description']}" if r.get("error_description") else "")
        return "failed", "Invalid device token response"

    github_token = poll_device_code(ia, poll, dev.get("interval"), dev["expires_in"],
                                    f"Waiting for GitHub authorization (code: {dev['user_code']})...")
    ia.notify("Exchanging the GitHub token for a Copilot token...", "info")
    cred = _copilot_exchange(github_token, enterprise)
    _copilot_enable_models(cred["access"], enterprise, lambda m: ia.notify(m, "info"))
    return cred


def refresh_github_copilot(cred: Dict[str, Any]) -> Dict[str, Any]:
    if not cred.get("refresh"):
        raise OAuthError("no GitHub token; sign in again with /login github-copilot")
    return _copilot_exchange(cred["refresh"], normalize_domain(cred.get("enterpriseUrl") or ""), cred)


# ========================================================================================== registry
PROVIDERS: Dict[str, OAuthProvider] = {
    "anthropic": OAuthProvider("anthropic", "Anthropic (Claude Pro/Max)", login_anthropic, refresh_anthropic),
    "google-antigravity": OAuthProvider("google-antigravity", "Google Antigravity (Gemini, Claude via Google)",
                                        login_antigravity, refresh_antigravity, _antigravity_unavailable),
    "openai-codex": OAuthProvider("openai-codex", "ChatGPT Plus/Pro (Codex Subscription)", login_openai_codex,
                                  refresh_openai_codex),
    "github-copilot": OAuthProvider("github-copilot", "GitHub Copilot", login_github_copilot, refresh_github_copilot),
}


CORE_PROVIDERS = frozenset(PROVIDERS)


def register_extension_provider(pid: str, name: str, login_fn: Callable[[Interaction], Dict[str, Any]],
                                refresh_fn: Callable[[Dict[str, Any]], Dict[str, Any]],
                                api_key: Optional[Callable[[Dict[str, Any]], str]] = None) -> bool:
    """An extension's `registerProvider(id, {oauth: {...}})` (Pi's custom OAuth providers). Core ids stay core."""
    if pid in CORE_PROVIDERS:
        return False
    PROVIDERS[pid] = OAuthProvider(pid, name, login_fn, refresh_fn, api_key=api_key, source="extension")
    return True


def unregister_extension_provider(pid: str) -> None:
    p = PROVIDERS.get(pid)
    if p is not None and p.source == "extension":
        del PROVIDERS[pid]


def get_provider(pid: str) -> Optional[OAuthProvider]:
    return PROVIDERS.get(pid)


def login(pid: str, ia: Optional[Interaction] = None) -> Dict[str, Any]:
    p = PROVIDERS.get(pid)
    if p is None:
        raise OAuthError(f"no OAuth sign-in for '{pid}'. Available: {', '.join(PROVIDERS)}")
    why = p.available()
    if why:
        raise OAuthError(f"{p.name}: {why}")
    return p.login(ia or Interaction())


def refresh(pid: str, cred: Dict[str, Any]) -> Dict[str, Any]:
    p = PROVIDERS.get(pid)
    if p is None:
        raise OAuthError(f"no OAuth refresh for '{pid}'")
    return p.refresh(cred)
