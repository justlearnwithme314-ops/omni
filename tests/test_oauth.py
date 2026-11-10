"""OAuth sign-in (src/pi/ai/oauth.py) against a local fake authorization / token / Cloud Code server.

Offline: a thread plays the browser by following the authorize URL's redirect_uri with a code.
"""
import base64
import hashlib
import http.server
import json
import os
import shutil
import socket
import sys
import tempfile
import threading
import time
import unittest
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("OMNI_AUTH_DB", os.path.join(tempfile.mkdtemp(prefix="omni-oauth-db-"), "agent_auth.db"))

from src.pi.ai import oauth  # noqa: E402
from src.pi.ai.auth import AuthStorage  # noqa: E402


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class FakeServer:
    """Records requests; `routes[path]` is a dict response or a callable(body) -> dict. A response may also be
    `(status, dict)` or `{"__sse__": [event, ...]}` (served as a text/event-stream)."""

    def __init__(self):
        self.requests, self.routes = [], {}
        outer = self

        class H(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _handle(self):
                n = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(n).decode() if n else ""
                ctype = self.headers.get("Content-Type", "")
                body = json.loads(raw) if "json" in ctype and raw else dict(urllib.parse.parse_qsl(raw))
                path = urllib.parse.urlparse(self.path).path
                outer.requests.append({"path": path, "body": body, "method": self.command,
                                       "headers": {k.lower(): v for k, v in self.headers.items()}})
                r = outer.routes.get(path)
                r = r(body) if callable(r) else r
                status = 200 if r is not None else 404
                if isinstance(r, tuple):
                    status, r = r
                if isinstance(r, dict) and "__sse__" in r:
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream")
                    self.end_headers()
                    for ev in r["__sse__"]:
                        self.wfile.write(f"data: {json.dumps(ev)}\n\n".encode())
                    self.wfile.write(b"data: [DONE]\n\n")
                    return
                data = json.dumps(r or {}).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(data)

            do_GET = do_POST = _handle

        self.srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.srv.server_address[1]}"

    def close(self):
        self.srv.shutdown()
        self.srv.server_close()


class FakeBrowser(oauth.Interaction):
    """Follows the authorize URL's redirect_uri (like a user approving in the browser), or pastes a code."""

    def __init__(self, code="the-code", paste=None, method=0, follow=True, tamper_state=False):
        self.code, self.paste, self.method, self.follow, self.tamper = code, paste, method, follow, tamper_state
        self.notes, self.urls, self.status = [], [], []

    def notify(self, text, level="info"):
        self.notes.append(text)

    def select(self, title, options):
        return self.method

    def open_url(self, url):
        self.urls.append(url)
        if not self.follow:
            return
        q = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(url).query))
        state = "evil" if self.tamper else q["state"]
        target = f"{q['redirect_uri']}?code={self.code}&state={state}"

        def go():
            time.sleep(0.3)
            try:
                self.status.append(urllib.request.urlopen(target.replace("localhost", "127.0.0.1"), timeout=5).status)
            except urllib.error.HTTPError as e:
                self.status.append(e.code)
        threading.Thread(target=go, daemon=True).start()

    def manual(self, message, placeholder, cancel=None):
        if self.paste is not None:
            return self.paste(self) if callable(self.paste) else self.paste
        if cancel is not None:
            cancel.wait(10)
        return None


class TestHelpers(unittest.TestCase):
    def test_pkce_matches_pi(self):
        v, c = oauth.generate_pkce()
        self.assertEqual(len(v), 43)
        self.assertEqual(c, base64.urlsafe_b64encode(hashlib.sha256(v.encode()).digest()).decode().rstrip("="))

    def test_parse_authorization_input(self):
        p = oauth.parse_authorization_input
        self.assertEqual(p("http://localhost:1/cb?code=abc&state=xyz"), ("abc", "xyz"))
        self.assertEqual(p("abc#xyz"), ("abc", "xyz"))
        self.assertEqual(p("code=abc&state=xyz"), ("abc", "xyz"))
        self.assertEqual(p("  abc "), ("abc", None))
        self.assertEqual(p(""), (None, None))


class OAuthCase(unittest.TestCase):
    def setUp(self):
        self.fake = FakeServer()
        self.saved = {k: getattr(oauth, k) for k in ("ANTHROPIC_TOKEN_URL", "ANTHROPIC_PORT", "GOOGLE_TOKEN_URL",
                                                     "GOOGLE_USERINFO_URL", "CLOUD_CODE_ASSIST", "ANTIGRAVITY_PORT")}
        oauth.ANTHROPIC_TOKEN_URL = self.fake.url + "/v1/oauth/token"
        oauth.ANTHROPIC_PORT = free_port()
        oauth.GOOGLE_TOKEN_URL = self.fake.url + "/token"
        oauth.GOOGLE_USERINFO_URL = self.fake.url + "/userinfo"
        oauth.CLOUD_CODE_ASSIST = self.fake.url
        oauth.ANTIGRAVITY_PORT = free_port()
        self.env = {k: os.environ.get(k) for k in ("OMNI_ANTIGRAVITY_CLIENT_ID", "OMNI_ANTIGRAVITY_CLIENT_SECRET")}
        os.environ["OMNI_ANTIGRAVITY_CLIENT_ID"] = "test-client.apps.googleusercontent.com"
        os.environ["OMNI_ANTIGRAVITY_CLIENT_SECRET"] = "test-secret"
        self.dir = Path(tempfile.mkdtemp(prefix="omni-oauth-"))

    def tearDown(self):
        for k, v in self.saved.items():
            setattr(oauth, k, v)
        for k, v in self.env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        self.fake.close()
        shutil.rmtree(self.dir, ignore_errors=True)


class TestAnthropic(OAuthCase):
    TOKEN = {"access_token": "sk-ant-oat01-AAA", "refresh_token": "rt-1", "expires_in": 3600,
             "account": {"email_address": "me@example.com"}, "organization": {"name": "Acme"}}

    def test_browser_login_exchanges_code_with_pkce_and_state(self):
        self.fake.routes["/v1/oauth/token"] = self.TOKEN
        ia = FakeBrowser()
        cred = oauth.login("anthropic", ia)
        q = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(ia.urls[0]).query))
        self.assertEqual(q["redirect_uri"], f"http://localhost:{oauth.ANTHROPIC_PORT}/callback")
        self.assertEqual((q["code"], q["code_challenge_method"], q["response_type"]), ("true", "S256", "code"))
        self.assertIn("user:inference", q["scope"])
        body = self.fake.requests[-1]["body"]
        self.assertEqual(body["grant_type"], "authorization_code")
        self.assertEqual((body["code"], body["state"], body["redirect_uri"]), ("the-code", q["state"], q["redirect_uri"]))
        self.assertEqual(base64.urlsafe_b64encode(hashlib.sha256(body["code_verifier"].encode()).digest()).decode().rstrip("="),
                         q["code_challenge"])
        self.assertEqual(body["client_id"], oauth.ANTHROPIC_CLIENT_ID)
        self.assertEqual((cred["type"], cred["access"], cred["refresh"], cred["email"]), ("oauth", "sk-ant-oat01-AAA", "rt-1", "me@example.com"))
        self.assertGreater(cred["expires"], time.time() * 1000 + 50 * 60 * 1000)
        self.assertEqual(ia.status, [200])

    def test_busy_port_falls_back_to_free_port(self):
        self.fake.routes["/v1/oauth/token"] = self.TOKEN
        blocker = socket.socket()
        blocker.bind(("127.0.0.1", oauth.ANTHROPIC_PORT))
        blocker.listen(1)
        try:
            ia = FakeBrowser()
            oauth.login("anthropic", ia)
            redirect = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(ia.urls[0]).query))["redirect_uri"]
            self.assertNotIn(f":{oauth.ANTHROPIC_PORT}/", redirect)
        finally:
            blocker.close()

    def test_pasted_redirect_url_wins_and_state_is_checked(self):
        self.fake.routes["/v1/oauth/token"] = self.TOKEN

        def paste(ia):
            q = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(ia.urls[0]).query))
            return f"{q['redirect_uri']}?code=pasted&state={q['state']}"
        oauth.login("anthropic", FakeBrowser(follow=False, paste=paste))
        self.assertEqual(self.fake.requests[-1]["body"]["code"], "pasted")
        with self.assertRaisesRegex(oauth.OAuthError, "state mismatch"):
            oauth.login("anthropic", FakeBrowser(follow=False, paste="code#wrong-state"))

    def test_callback_with_wrong_state_is_rejected(self):
        ia = FakeBrowser(tamper_state=True, paste=None)
        t = threading.Thread(target=lambda: self.assertRaises(Exception, oauth.login, "anthropic", ia), daemon=True)
        t.start()
        deadline = time.time() + 5
        while not ia.status and time.time() < deadline:
            time.sleep(0.05)
        self.assertEqual(ia.status, [400])

    def test_copy_code_method(self):
        self.fake.routes["/v1/oauth/token"] = self.TOKEN

        def paste(ia):
            q = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(ia.urls[0]).query))
            self.assertEqual(q["redirect_uri"], oauth.ANTHROPIC_COPY_CODE_REDIRECT)
            return "copied#" + q["state"]
        oauth.login("anthropic", FakeBrowser(method=1, follow=False, paste=paste))
        self.assertEqual(self.fake.requests[-1]["body"]["redirect_uri"], oauth.ANTHROPIC_COPY_CODE_REDIRECT)

    def test_auth_storage_refreshes_expired_token(self):
        self.fake.routes["/v1/oauth/token"] = {"access_token": "sk-ant-oat01-NEW", "refresh_token": "rt-2", "expires_in": 3600}
        store = AuthStorage(self.dir)
        store.set_oauth("anthropic", {"access": "sk-ant-oat01-OLD", "refresh": "rt-1", "expires": 1, "email": "me@x"})
        self.assertEqual(store.get_api_key("anthropic"), "sk-ant-oat01-NEW")
        req = self.fake.requests[-1]
        self.assertEqual(req["body"], {"grant_type": "refresh_token", "client_id": oauth.ANTHROPIC_CLIENT_ID, "refresh_token": "rt-1"})
        self.assertEqual(req["headers"].get("anthropic-beta"), "oauth-2025-04-20")
        saved = json.loads((self.dir / "auth.json").read_text())["anthropic"]
        self.assertEqual((saved["access"], saved["refresh"], saved["email"]), ("sk-ant-oat01-NEW", "rt-2", "me@x"))
        n = len(self.fake.requests)
        self.assertEqual(store.get_api_key("anthropic"), "sk-ant-oat01-NEW")  # fresh now: no second refresh
        self.assertEqual(len(self.fake.requests), n)

    def test_failed_refresh_reports_and_falls_through(self):
        self.fake.routes["/v1/oauth/token"] = None  # 404
        store = AuthStorage(self.dir)
        store.set_oauth("anthropic", {"access": "old", "refresh": "rt", "expires": 1})
        os.environ.pop("ANTHROPIC_API_KEY", None)
        os.environ.pop("ANTHROPIC_OAUTH_TOKEN", None)
        store._keyring_failed = True
        self.assertIsNone(store.get_api_key("anthropic"))
        self.assertIn("HTTP 404", store.last_refresh_error)


class TestAntigravity(OAuthCase):
    def test_login_discovers_and_onboards_project_then_refresh_keeps_it(self):
        calls = {"load": 0}

        def load(body):
            calls["load"] += 1
            if calls["load"] == 1:
                return {"allowedTiers": [{"id": "free-tier"}], "currentTier": None}
            return {"currentTier": {"id": "free-tier"}, "paidTier": {"id": "x"}, "cloudaicompanionProject": "proj-123"}
        self.fake.routes.update({
            "/token": lambda b: {"access_token": "ya29.A" if b["grant_type"] == "authorization_code" else "ya29.B",
                                 "refresh_token": "1//r", "expires_in": 3599},
            "/userinfo": {"email": "me@gmail.com"},
            "/v1internal:loadCodeAssist": load,
            "/v1internal:onboardUser": {"name": "operations/op1", "done": False},
            "/v1internal/operations/op1": {"name": "operations/op1", "done": True, "response": {"@type": "x"}},
        })
        ia = FakeBrowser()
        cred = oauth.login("google-antigravity", ia)
        q = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(ia.urls[0]).query))
        self.assertEqual(q["redirect_uri"], f"http://127.0.0.1:{oauth.ANTIGRAVITY_PORT}/oauth-callback")
        self.assertEqual((q["access_type"], q["prompt"], q["client_id"]), ("offline", "consent", "test-client.apps.googleusercontent.com"))
        tok = next(r for r in self.fake.requests if r["path"] == "/token")["body"]
        self.assertEqual((tok["grant_type"], tok["code"], tok["client_secret"]), ("authorization_code", "the-code", "test-secret"))
        onboard = next(r for r in self.fake.requests if r["path"] == "/v1internal:onboardUser")
        self.assertEqual(onboard["body"], {"tierId": "free-tier", "metadata": {"ideType": "ANTIGRAVITY"}})
        self.assertEqual(onboard["headers"]["authorization"], "Bearer ya29.A")
        self.assertEqual((cred["projectId"], cred["email"], cred["access"]), ("proj-123", "me@gmail.com", "ya29.A"))

        store = AuthStorage(self.dir)
        store.set_oauth("google-antigravity", dict(cred, expires=1))
        self.assertEqual(store.get_api_key("google-antigravity"), "ya29.B")
        self.assertEqual(store.credential_extra("google-antigravity"), {"email": "me@gmail.com", "projectId": "proj-123"})

    def test_ineligible_account_message(self):
        self.fake.routes.update({
            "/token": {"access_token": "ya29.A", "refresh_token": "1//r", "expires_in": 3599},
            "/v1internal:loadCodeAssist": {"ineligibleTiers": [{"tierId": "free-tier", "reasonMessage": "Not available in your region",
                                                                "validationUrl": "https://example.com/verify"}]},
        })
        with self.assertRaisesRegex(oauth.OAuthError, "Not available in your region"):
            oauth.login("google-antigravity", FakeBrowser())

    def test_unavailable_without_client(self):
        os.environ.pop("OMNI_ANTIGRAVITY_CLIENT_ID")
        saved = oauth._omp_dirs
        oauth._omp_dirs = lambda: []
        try:
            with self.assertRaisesRegex(oauth.OAuthError, "OMNI_ANTIGRAVITY_CLIENT_ID"):
                oauth.login("google-antigravity", FakeBrowser())
        finally:
            oauth._omp_dirs = saved


class TestLoginCommand(OAuthCase):
    def test_slash_login_saves_oauth_and_picks_model(self):
        from src.pi.commands import Commands
        from types import SimpleNamespace as NS
        self.fake.routes["/v1/oauth/token"] = TestAnthropic.TOKEN
        notes = []
        answers = [0, 0]  # login menu: first row (Anthropic sign-in); method: browser

        class Port:
            def notify(self, text, level="info"):
                notes.append((level, text))

            def select(self, title, options, initial=None):
                return answers.pop(0)

            def input_cancellable(self, title, placeholder="", cancel=None):
                cancel.wait(10)
                return None

        store = AuthStorage(self.dir)
        models = [{"provider": "anthropic", "id": "claude-sonnet-5-5"}]
        picked = []
        sess = NS(auth=store, model=None, registry=NS(all=lambda: models), set_model=picked.append)
        import webbrowser
        real_open = webbrowser.open
        browser = FakeBrowser()
        webbrowser.open = browser.open_url
        try:
            Commands(sess, Port()).cmd_login("")
        finally:
            webbrowser.open = real_open
        self.assertEqual(store.entry("anthropic")["type"], "oauth")
        self.assertTrue(any("Signed in to Anthropic" in t and "me@example.com" in t for _l, t in notes), notes)
        self.assertEqual(picked, models)


if __name__ == "__main__":
    unittest.main()
