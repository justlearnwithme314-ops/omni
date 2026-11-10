import os as _os, sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))
import _isolation  # noqa: F401

import http.server, json, socket, threading, time, unittest, urllib.parse, urllib.request
from src.providers import auth_plugin as A
from src.providers.keyring import Keyring
from src.paths import user_data_dir


def port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0)); return s.getsockname()[1]


class Tok(http.server.BaseHTTPRequestHandler):
    seen = []
    def log_message(self, *a): pass
    def do_POST(self):
        body = self.rfile.read(int(self.headers["Content-Length"])).decode()
        Tok.seen.append(body)
        form = dict(urllib.parse.parse_qsl(body)) if "=" in body and not body.startswith("{") else json.loads(body)
        out = {"access_token": "AT-" + form.get("grant_type", "x"), "refresh_token": "RT1", "expires_in": 3600}
        if "device_code" in form:
            out = {"access_token": "AT-device"}
        b = json.dumps(out).encode()
        self.send_response(200); self.send_header("Content-Type", "application/json"); self.end_headers(); self.wfile.write(b)


class TestAuth(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tp = port()
        cls.srv = http.server.ThreadingHTTPServer(("127.0.0.1", cls.tp), Tok)
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown(); cls.srv.server_close()

    def mk(self, **kw):
        ap = A.AuthPlugin(keyring=Keyring(db_path=_os.path.join(_isolation.TMP, f"k{time.time_ns()}.db")), say=lambda s: None, **kw)
        return ap

    def test_default_providers_load_and_user_override(self):
        self.assertIn("openai-codex", A.load_providers())
        d = user_data_dir() / "auth"; d.mkdir(parents=True, exist_ok=True)
        (d / "providers.json").write_text(json.dumps({"providers": [{"id": "mine", "name": "Mine", "flow": "api_key"}]}))
        ps = A.load_providers(); self.assertIn("mine", ps); self.assertIn("openai", ps)
        (d / "providers.json").unlink()

    def test_api_key_arg_env_and_prompt(self):
        ap = self.mk(ask=lambda q: "typed-key")
        self.assertIn("argument", ap.login("openai", "sk-1"))
        _os.environ["GROQ_API_KEY"] = "gq"
        self.assertIn("$GROQ_API_KEY", ap.login("groq"))
        _os.environ.pop("GROQ_API_KEY")
        self.assertIn("prompt", ap.login("xai"))
        self.assertEqual({k["provider"] for k in ap.kr.list_keys()}, {"openai", "groq", "xai"})

    def test_unknown_provider(self):
        with self.assertRaises(A.LoginError): self.mk().login("nope")

    def test_pkce_localhost_callback_full_flow(self):
        cp = port()
        prov = {"id": "t", "name": "T", "flow": "oauth_pkce", "client_id": "cid", "authorize_url": "http://x/authorize",
                "token_url": f"http://127.0.0.1:{self.tp}/token", "scopes": "a b", "redirect_port": cp, "redirect_path": "/cb"}
        seen = {}
        def browser(url):
            q = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(url).query)); seen.update(q)
            threading.Thread(target=lambda: (time.sleep(0.3), urllib.request.urlopen(f"{q['redirect_uri']}?code=CODE1&state={q['state']}").read()), daemon=True).start()
        ap = self.mk(opener=browser); ap.providers = {"t": prov}
        Tok.seen.clear()
        self.assertIn("Logged in", ap.login("t"))
        self.assertEqual((seen["code_challenge_method"], seen["client_id"]), ("S256", "cid"))
        self.assertIn("code_verifier=", Tok.seen[0]); self.assertIn("code=CODE1", Tok.seen[0])
        self.assertEqual(ap.kr.list_keys()[0]["provider"], "t")
        tokens = json.loads((user_data_dir() / "auth" / "tokens.json").read_text()); self.assertEqual(tokens["t"]["refresh_token"], "RT1")

    def test_pkce_rejects_wrong_state(self):
        cp = port()
        prov = {"id": "t", "name": "T", "flow": "oauth_pkce", "client_id": "c", "authorize_url": "http://x/a", "token_url": "http://x/t",
                "redirect_port": cp, "redirect_path": "/cb", "timeout": 3}
        def browser(url):
            q = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(url).query))
            threading.Thread(target=lambda: (time.sleep(0.3), urllib.request.urlopen(f"{q['redirect_uri']}?code=X&state=WRONG").read() if False else _try(q)), daemon=True).start()
        def _try(q):
            try: urllib.request.urlopen(f"{q['redirect_uri']}?code=X&state=WRONG")
            except Exception: pass
        ap = self.mk(opener=browser); ap.providers = {"t": prov}
        with self.assertRaises(A.LoginError): ap.login("t")

    def test_manual_code_flow(self):
        prov = {"id": "m", "name": "M", "flow": "oauth_pkce", "client_id": "c", "authorize_url": "http://x/a",
                "token_url": f"http://127.0.0.1:{self.tp}/token", "redirect_uri": "http://x/cb", "manual_code": True, "token_format": "json"}
        box = {}
        ap = self.mk(opener=lambda u: box.update(dict(urllib.parse.parse_qsl(urllib.parse.urlparse(u).query))),
                     ask=lambda q: f"MANUALCODE#{box['state']}")
        ap.providers = {"m": prov}
        self.assertIn("Logged in", ap.login("m"))

    def test_device_code_flow(self):
        prov = {"id": "d", "name": "D", "flow": "device_code", "client_id": "c",
                "device_url": f"http://127.0.0.1:{self.tp}/device", "token_url": f"http://127.0.0.1:{self.tp}/token"}
        real = A._post
        A._post = lambda url, data, as_json=False: ({"device_code": "dc", "user_code": "ABCD", "verification_uri": "http://x", "interval": 0, "expires_in": 5} if "device" in url else real(url, data, as_json))
        try:
            ap = self.mk(opener=lambda u: None); ap.providers = {"d": prov}
            self.assertIn("Logged in", ap.login("d"))
        finally:
            A._post = real

    def test_refresh(self):
        kr = Keyring(db_path=_os.path.join(_isolation.TMP, "kr.db"))
        A._save_token("r", {"refresh_token": "RT1", "expires_at": time.time() + 10, "token_url": f"http://127.0.0.1:{self.tp}/token", "client_id": "c"})
        self.assertTrue(A.refresh("r", kr))
        self.assertFalse(A.refresh("r", kr))


if __name__ == "__main__":
    unittest.main()
