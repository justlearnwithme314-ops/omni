"""ChatGPT/Codex and GitHub Copilot sign-in (src/pi/ai/oauth.py) and their stream adapters (src/pi/ai/stream.py).

Offline: a local fake server plays auth.openai.com / github.com / the Copilot API / the Codex backend, and a
simulated browser follows the authorize URL's redirect_uri (or the user "approves" a device code).
"""
import base64
import hashlib
import importlib
import json
import os
import socket
import sys
import threading
import time
import unittest
import urllib.parse
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from test_oauth import FakeBrowser, FakeServer, OAuthCase, free_port  # noqa: E402
from src.pi.ai import oauth  # noqa: E402
from src.pi.ai.auth import AuthStorage  # noqa: E402

S = importlib.import_module("src.pi.ai.stream")
M = importlib.import_module("src.pi.ai.models")


def b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def jwt(payload) -> str:
    return "eyJhbGciOiJSUzI1NiJ9." + b64url(json.dumps(payload).encode()) + ".sig"


ACCESS = jwt({"https://api.openai.com/auth": {"chatgpt_account_id": "acct-42", "chatgpt_plan_type": "Pro"},
              "https://api.openai.com/profile": {"email": "Me@Example.com"}})
CODEX_TOKEN = {"access_token": ACCESS, "refresh_token": "rt-codex-1", "expires_in": 864000, "id_token": jwt({})}


class DeviceUser(FakeBrowser):
    """Approves device codes on its own; `give_up=True` closes the waiting prompt instead (cancel)."""

    def __init__(self, method=0, domain=None, give_up=False):
        super().__init__(method=method, follow=False, paste=domain)
        self.give_up = give_up

    def wait_cancel(self, message, done):
        self.notes.append(message)
        if self.give_up:
            return True
        done.wait(10)
        return False


class Case(OAuthCase):
    def setUp(self):
        super().setUp()
        self.saved2 = {k: getattr(oauth, k) for k in ("CODEX_AUTH_BASE", "CODEX_PORT", "GITHUB_WEB", "GITHUB_API",
                                                      "COPILOT_DEFAULT_BASE_URL", "DEVICE_MIN_INTERVAL",
                                                      "DEVICE_SLOW_DOWN_STEP", "_copilot_enable_models")}
        oauth.CODEX_AUTH_BASE = self.fake.url
        oauth.CODEX_PORT = free_port()
        oauth.GITHUB_WEB = self.fake.url + "/web/{domain}"
        oauth.GITHUB_API = self.fake.url + "/api/{domain}"
        oauth.COPILOT_DEFAULT_BASE_URL = self.fake.url + "/capi"
        oauth.DEVICE_MIN_INTERVAL = 0.02
        oauth.DEVICE_SLOW_DOWN_STEP = 0.02

    def tearDown(self):
        for k, v in self.saved2.items():
            setattr(oauth, k, v)
        super().tearDown()

    def reqs(self, path):
        return [r for r in self.fake.requests if r["path"] == path]


# ============================================================================================ codex
class TestCodexLogin(Case):
    def test_browser_login_on_fixed_port_with_pkce(self):
        self.fake.routes["/oauth/token"] = CODEX_TOKEN
        ia = FakeBrowser()
        cred = oauth.login("openai-codex", ia)
        q = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(ia.urls[0]).query))
        self.assertTrue(ia.urls[0].startswith(self.fake.url + "/oauth/authorize?"))
        self.assertEqual(q["redirect_uri"], f"http://localhost:{oauth.CODEX_PORT}/auth/callback")
        self.assertEqual((q["client_id"], q["response_type"], q["code_challenge_method"]),
                         (oauth.CODEX_CLIENT_ID, "code", "S256"))
        self.assertEqual((q["codex_cli_simplified_flow"], q["id_token_add_organizations"], q["originator"]),
                         ("true", "true", "pi"))
        self.assertIn("offline_access", q["scope"])
        body = self.reqs("/oauth/token")[-1]["body"]
        self.assertEqual((body["grant_type"], body["code"], body["redirect_uri"], body["client_id"]),
                         ("authorization_code", "the-code", q["redirect_uri"], oauth.CODEX_CLIENT_ID))
        self.assertEqual(b64url(hashlib.sha256(body["code_verifier"].encode()).digest()), q["code_challenge"])
        self.assertEqual((cred["type"], cred["access"], cred["refresh"], cred["accountId"], cred["email"], cred["orgName"]),
                         ("oauth", ACCESS, "rt-codex-1", "acct-42", "me@example.com", "pro"))
        self.assertGreater(cred["expires"], time.time() * 1000 + 9 * 86400 * 1000)
        self.assertEqual(ia.status, [200])

    def test_busy_port_does_not_move_redirect_and_paste_works(self):
        self.fake.routes["/oauth/token"] = CODEX_TOKEN
        blocker = socket.socket()
        blocker.bind(("127.0.0.1", oauth.CODEX_PORT))
        blocker.listen(1)
        try:
            def paste(ia):
                q = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(ia.urls[0]).query))
                return f"{q['redirect_uri']}?code=pasted&state={q['state']}"
            ia = FakeBrowser(follow=False, paste=paste)
            oauth.login("openai-codex", ia)
        finally:
            blocker.close()
        q = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(ia.urls[0]).query))
        self.assertEqual(q["redirect_uri"], f"http://localhost:{oauth.CODEX_PORT}/auth/callback")
        self.assertTrue(any("busy" in n for n in ia.notes), ia.notes)
        self.assertEqual(self.reqs("/oauth/token")[-1]["body"]["code"], "pasted")
        with self.assertRaisesRegex(oauth.OAuthError, "state mismatch"):
            oauth.login("openai-codex", FakeBrowser(follow=False, paste="http://localhost/x?code=c&state=evil"))

    def test_device_code_login(self):
        polls = []

        def token(body):
            polls.append(body)
            if len(polls) == 1:
                return 403, {"error": {"code": "deviceauth_authorization_pending"}}
            return {"authorization_code": "dev-code", "code_verifier": "dev-verifier"}
        self.fake.routes.update({
            "/api/accounts/deviceauth/usercode": {"device_auth_id": "da-1", "user_code": "ABCD-1234", "interval": "0"},
            "/api/accounts/deviceauth/token": token,
            "/oauth/token": CODEX_TOKEN,
        })
        ia = DeviceUser(method=1)
        cred = oauth.login("openai-codex", ia)
        self.assertEqual(self.reqs("/api/accounts/deviceauth/usercode")[0]["body"], {"client_id": oauth.CODEX_CLIENT_ID})
        self.assertEqual(polls, [{"device_auth_id": "da-1", "user_code": "ABCD-1234"}] * 2)
        self.assertEqual(ia.urls, [self.fake.url + "/codex/device"])
        self.assertTrue(any("ABCD-1234" in n for n in ia.notes))
        body = self.reqs("/oauth/token")[-1]["body"]
        self.assertEqual((body["code"], body["code_verifier"], body["redirect_uri"]),
                         ("dev-code", "dev-verifier", self.fake.url + "/deviceauth/callback"))
        self.assertEqual(cred["accountId"], "acct-42")

    def test_device_code_cancel_and_failure(self):
        self.fake.routes.update({
            "/api/accounts/deviceauth/usercode": {"device_auth_id": "da-1", "user_code": "X", "interval": 0},
            "/api/accounts/deviceauth/token": (403, {}),
        })
        with self.assertRaises(oauth.LoginCancelled):
            oauth.login("openai-codex", DeviceUser(method=1, give_up=True))
        self.fake.routes["/api/accounts/deviceauth/token"] = (500, {"error": "boom"})
        with self.assertRaisesRegex(oauth.OAuthError, "device auth failed \\(500\\)"):
            oauth.login("openai-codex", DeviceUser(method=1))
        self.fake.routes["/api/accounts/deviceauth/usercode"] = None  # 404
        with self.assertRaisesRegex(oauth.OAuthError, "not enabled"):
            oauth.login("openai-codex", DeviceUser(method=1))

    def test_token_without_account_is_rejected(self):
        self.fake.routes["/oauth/token"] = dict(CODEX_TOKEN, access_token=jwt({"sub": "x"}), id_token=None)
        with self.assertRaisesRegex(oauth.OAuthError, "account id"):
            oauth.login("openai-codex", FakeBrowser())

    def test_auth_storage_refreshes_and_keeps_identity(self):
        new_access = jwt({"sub": "no-claims"})
        self.fake.routes["/oauth/token"] = {"access_token": new_access, "refresh_token": "rt-2", "expires_in": 3600}
        store = AuthStorage(self.dir)
        store.set_oauth("openai-codex", {"access": ACCESS, "refresh": "rt-1", "expires": 1, "accountId": "acct-42",
                                         "email": "me@example.com"})
        self.assertEqual(store.get_api_key("openai-codex"), new_access)
        self.assertEqual(self.reqs("/oauth/token")[-1]["body"],
                         {"grant_type": "refresh_token", "refresh_token": "rt-1", "client_id": oauth.CODEX_CLIENT_ID})
        saved = json.loads((self.dir / "auth.json").read_text())["openai-codex"]
        self.assertEqual((saved["type"], saved["refresh"], saved["accountId"], saved["email"]),
                         ("oauth", "rt-2", "acct-42", "me@example.com"))
        self.assertEqual(store.credential_extra("openai-codex")["accountId"], "acct-42")


# =========================================================================================== copilot
COPILOT_TOKEN = "tid=abc;exp=9999999999;sku=copilot_individual;8kp=1"


class TestCopilotLogin(Case):
    def routes(self, domain="github.com", pending=2):
        polls = []

        def access(body):
            polls.append(body)
            if len(polls) == 1:
                return {"error": "authorization_pending"}
            if len(polls) == 2 and pending > 1:
                return {"error": "slow_down", "interval": 0}
            return {"access_token": "gho_github", "token_type": "bearer", "scope": "read:user"}
        exp = int(time.time()) + 1800
        self.fake.routes.update({
            f"/web/{domain}/login/device/code": {"device_code": "dc-1", "user_code": "WXYZ-0001",
                                                 "verification_uri": "https://github.com/login/device",
                                                 "interval": 0, "expires_in": 900},
            f"/web/{domain}/login/oauth/access_token": access,
            f"/api/{domain}/copilot_internal/v2/token": {"token": COPILOT_TOKEN, "expires_at": exp},
        })
        for m in M.BUILTIN_MODELS:
            if m["provider"] == "github-copilot":
                self.fake.routes[f"/capi/models/{m['id']}/policy"] = {}
        return polls, exp

    def test_device_login_exchanges_for_copilot_token_and_enables_models(self):
        polls, exp = self.routes()
        ia = DeviceUser()
        cred = oauth.login("github-copilot", ia)
        dev = self.reqs("/web/github.com/login/device/code")[0]
        self.assertEqual(dev["body"], {"client_id": oauth.COPILOT_CLIENT_ID, "scope": "read:user"})
        self.assertEqual(len(polls), 3)
        self.assertEqual(polls[0], {"client_id": oauth.COPILOT_CLIENT_ID, "device_code": "dc-1",
                                    "grant_type": "urn:ietf:params:oauth:grant-type:device_code"})
        self.assertEqual(ia.urls, ["https://github.com/login/device"])
        self.assertTrue(any("WXYZ-0001" in n for n in ia.notes))
        ex = self.reqs("/api/github.com/copilot_internal/v2/token")[0]
        self.assertEqual((ex["method"], ex["headers"]["authorization"], ex["headers"]["copilot-integration-id"]),
                         ("GET", "Bearer gho_github", "vscode-chat"))
        self.assertEqual((cred["type"], cred["access"], cred["refresh"]), ("oauth", COPILOT_TOKEN, "gho_github"))
        self.assertEqual(cred["expires"], exp * 1000 - 5 * 60 * 1000)
        self.assertNotIn("enterpriseUrl", cred)
        policies = [r for r in self.fake.requests if r["path"].endswith("/policy")]
        self.assertEqual(len(policies), sum(1 for m in M.BUILTIN_MODELS if m["provider"] == "github-copilot"))
        self.assertEqual((policies[0]["body"], policies[0]["headers"]["authorization"]),
                         ({"state": "enabled"}, f"Bearer {COPILOT_TOKEN}"))

    def test_enterprise_domain(self):
        self.routes("company.ghe.com", pending=1)
        oauth._copilot_enable_models = lambda *a, **k: []  # would hit https://copilot-api.company.ghe.com
        cred = oauth.login("github-copilot", DeviceUser(method=1, domain="https://Company.GHE.com/"))
        self.assertEqual(cred["enterpriseUrl"], "company.ghe.com")
        self.assertTrue(self.reqs("/api/company.ghe.com/copilot_internal/v2/token"))
        with self.assertRaisesRegex(oauth.OAuthError, "Invalid GitHub Enterprise"):
            oauth.login("github-copilot", DeviceUser(method=1, domain="not a domain"))

    def test_denied_and_cancelled(self):
        self.routes()
        with self.assertRaises(oauth.LoginCancelled):
            oauth.login("github-copilot", DeviceUser(give_up=True))
        self.fake.routes["/web/github.com/login/oauth/access_token"] = {"error": "access_denied",
                                                                       "error_description": "user said no"}
        with self.assertRaisesRegex(oauth.OAuthError, "access_denied: user said no"):
            oauth.login("github-copilot", DeviceUser())

    def test_auth_storage_refresh_is_a_new_exchange(self):
        self.routes()
        store = AuthStorage(self.dir)
        store.set_oauth("github-copilot", {"access": "tid=old", "refresh": "gho_github", "expires": 1})
        self.assertEqual(store.get_api_key("github-copilot"), COPILOT_TOKEN)
        saved = json.loads((self.dir / "auth.json").read_text())["github-copilot"]
        self.assertEqual((saved["refresh"], saved["access"]), ("gho_github", COPILOT_TOKEN))
        self.assertGreater(saved["expires"], time.time() * 1000)
        self.fake.routes["/api/github.com/copilot_internal/v2/token"] = (401, {"message": "Bad credentials"})
        store.set_oauth("github-copilot", {"access": "tid=old", "refresh": "gho_revoked", "expires": 1})
        store._keyring_failed = True
        os.environ.pop("COPILOT_GITHUB_TOKEN", None)
        self.assertIsNone(store.get_api_key("github-copilot"))
        self.assertIn("refused", store.last_refresh_error)

    def test_base_url_from_token(self):
        self.assertEqual(oauth.copilot_base_url("tid=1;proxy-ep=proxy.business.githubcopilot.com;x=1"),
                         "https://api.business.githubcopilot.com")
        self.assertEqual(oauth.copilot_base_url("tid=1", "company.ghe.com"), "https://copilot-api.company.ghe.com")


class TestLoginMenu(Case):
    def test_both_providers_listed_in_slash_login(self):
        from src.pi.commands import Commands
        from types import SimpleNamespace as NS
        seen = []

        class Port:
            def notify(self, text, level="info"):
                pass

            def select(self, title, options, initial=None):
                seen.extend(options)
                return None
        Commands(NS(auth=AuthStorage(self.dir)), Port()).cmd_login("")
        self.assertIn("Sign in: ChatGPT Plus/Pro (Codex Subscription)", seen)
        self.assertIn("Sign in: GitHub Copilot", seen)

    def test_omni_login_subcommand_accepts_them(self):
        from src.pi.subcommands import _is_core_login
        self.assertTrue(_is_core_login(["openai-codex"]))
        self.assertTrue(_is_core_login(["github-copilot"]))


# ============================================================================================ streams
TOOLS = [{"name": "read", "description": "Read a file", "parameters": {"type": "object", "properties": {"path": {"type": "string"}}}}]


def run(model, context, options):
    events = list(S.stream(model, context, options))
    return events, events[-1]["message"]


class TestCodexStream(Case):
    def model(self):
        reg = M.ModelRegistry(self.dir)
        return dict(reg.find("openai-codex", "gpt-6.1-sol"), baseUrl=self.fake.url + "/backend-api")

    def test_text_reasoning_and_tool_call_then_replay(self):
        reasoning = {"type": "reasoning", "id": "rs_1", "summary": [{"type": "summary_text", "text": "Plan: read it"}],
                     "encrypted_content": "ENC=="}
        fcall = {"type": "function_call", "id": "fc_1", "call_id": "call_1", "name": "read", "arguments": '{"path":"a.txt"}'}
        self.fake.routes["/backend-api/codex/responses"] = {"__sse__": [
            {"type": "response.created", "response": {"id": "resp_1"}},
            {"type": "response.output_item.added", "output_index": 0, "item": {"type": "reasoning", "id": "rs_1"}},
            {"type": "response.reasoning_summary_part.added", "output_index": 0},
            {"type": "response.reasoning_summary_text.delta", "output_index": 0, "delta": "Plan: "},
            {"type": "response.reasoning_summary_text.delta", "output_index": 0, "delta": "read it"},
            {"type": "response.output_item.done", "output_index": 0, "item": reasoning},
            {"type": "response.output_item.added", "output_index": 1, "item": {"type": "message", "id": "msg_1"}},
            {"type": "response.output_text.delta", "output_index": 1, "delta": "Hel"},
            {"type": "response.output_text.delta", "output_index": 1, "delta": "lo"},
            {"type": "response.output_item.done", "output_index": 1, "item": {"type": "message"}},
            {"type": "response.output_item.added", "output_index": 2,
             "item": {"type": "function_call", "id": "fc_1", "call_id": "call_1", "name": "read", "arguments": ""}},
            {"type": "response.function_call_arguments.delta", "output_index": 2, "delta": '{"path":'},
            {"type": "response.function_call_arguments.delta", "output_index": 2, "delta": '"a.txt"}'},
            {"type": "response.output_item.done", "output_index": 2, "item": fcall},
            {"type": "response.completed", "response": {"status": "completed", "usage": {
                "input_tokens": 120, "input_tokens_details": {"cached_tokens": 20}, "output_tokens": 30}}},
        ]}
        model = self.model()
        ctx = {"systemPrompt": "You are Omni.", "tools": TOOLS,
               "messages": [{"role": "user", "content": "read a.txt", "timestamp": 1}]}
        events, msg = run(model, ctx, {"apiKey": ACCESS, "reasoning": "high"})
        self.assertEqual("".join(e["delta"] for e in events if e["type"] == "text_delta"), "Hello")
        self.assertEqual("".join(e["delta"] for e in events if e["type"] == "thinking_delta"), "Plan: read it")
        self.assertEqual([e["name"] for e in events if e["type"] == "toolcall_start"], ["read"])
        call = next(e["toolCall"] for e in events if e["type"] == "toolcall_end")
        self.assertEqual((call["id"], call["name"], call["arguments"]), ("call_1|fc_1", "read", {"path": "a.txt"}))
        self.assertEqual(msg["stopReason"], "toolUse")
        self.assertEqual((msg["usage"]["input"], msg["usage"]["cacheRead"], msg["usage"]["output"]), (100, 20, 30))
        self.assertEqual(json.loads(msg["content"][0]["thinkingSignature"])["encrypted_content"], "ENC==")

        req = self.reqs("/backend-api/codex/responses")[0]
        h, body = req["headers"], req["body"]
        self.assertEqual((h["authorization"], h["chatgpt-account-id"], h["openai-beta"], h["originator"]),
                         (f"Bearer {ACCESS}", "acct-42", "responses=experimental", "pi"))
        self.assertTrue(h.get("session_id"))
        self.assertEqual((body["model"], body["store"], body["stream"], body["instructions"]),
                         ("gpt-6.1-sol", False, True, "You are Omni."))
        self.assertEqual(body["input"], [{"role": "user", "content": [{"type": "input_text", "text": "read a.txt"}]}])
        self.assertEqual(body["tools"][0]["type"], "function")
        self.assertEqual(body["tools"][0]["name"], "read")
        self.assertEqual(body["reasoning"], {"effort": "high", "summary": "auto"})
        self.assertEqual(body["include"], ["reasoning.encrypted_content"])
        self.assertNotIn("max_output_tokens", body)

        # second turn: replays reasoning + call, sends the tool output
        self.fake.routes["/backend-api/codex/responses"] = {"__sse__": [
            {"type": "response.output_text.delta", "output_index": 0, "delta": "It says hi."},
            {"type": "response.completed", "response": {"status": "completed", "usage": {"input_tokens": 5, "output_tokens": 2}}},
        ]}
        ctx["messages"] += [msg, {"role": "toolResult", "toolCallId": "call_1|fc_1", "toolName": "read",
                                  "content": [{"type": "text", "text": "hi"}], "isError": False, "timestamp": 2}]
        _ev, msg2 = run(model, ctx, {"apiKey": ACCESS, "reasoning": "off"})
        self.assertEqual((msg2["content"][0]["text"], msg2["stopReason"]), ("It says hi.", "stop"))
        body2 = self.reqs("/backend-api/codex/responses")[1]["body"]
        items = body2["input"]
        self.assertEqual(items[1], reasoning)
        self.assertEqual(items[2], {"role": "assistant", "content": [{"type": "output_text", "text": "Hello"}]})
        self.assertEqual(items[3], {"type": "function_call", "call_id": "call_1", "name": "read",
                                    "arguments": '{"path": "a.txt"}', "id": "fc_1"})
        self.assertEqual(items[4], {"type": "function_call_output", "call_id": "call_1", "output": "hi"})
        self.assertEqual(body2["reasoning"], {"effort": "none"})

    def test_errors(self):
        model = self.model()
        ctx = {"messages": [{"role": "user", "content": "x"}]}
        self.fake.routes["/backend-api/codex/responses"] = {"__sse__": [
            {"type": "error", "code": "usage_limit_reached", "message": "You've hit your limit"}]}
        with self.assertRaises(S.ProviderError) as cm:
            run(model, ctx, {"apiKey": ACCESS})
        self.assertEqual(cm.exception.status, 429)
        self.fake.routes["/backend-api/codex/responses"] = {"__sse__": [
            {"type": "response.output_text.delta", "delta": "cut"}]}
        with self.assertRaisesRegex(S.ProviderError, "ended before"):
            run(model, ctx, {"apiKey": ACCESS})
        self.fake.routes["/backend-api/codex/responses"] = (401, {"error": {"message": "token expired"}})
        with self.assertRaisesRegex(S.ProviderError, "401 token expired"):
            run(model, ctx, {"apiKey": ACCESS})
        with self.assertRaisesRegex(S.ProviderError, "account id"):
            run(model, ctx, {"apiKey": "not-a-jwt"})
        self.assertEqual(S.codex_url("https://x/backend-api/"), "https://x/backend-api/codex/responses")


class TestCopilotStream(Case):
    def model(self, mid="claude-sonnet-5.5"):
        return dict(M.ModelRegistry(self.dir).find("github-copilot", mid), baseUrl=self.fake.url + "/capi")

    def test_chat_completions_text_and_tool_call(self):
        self.fake.routes["/capi/chat/completions"] = {"__sse__": [
            {"choices": [{"index": 0, "delta": {"role": "assistant", "content": "Let me "}}]},
            {"choices": [{"index": 0, "delta": {"content": "look."}}]},
            {"choices": [{"index": 0, "delta": {"tool_calls": [{"index": 0, "id": "toolu_1", "type": "function",
                                                                  "function": {"name": "read", "arguments": ""}}]}}]},
            {"choices": [{"index": 0, "delta": {"tool_calls": [{"index": 0, "function": {"arguments": '{"path":"b"}'}}]}}]},
            {"choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}],
             "usage": {"prompt_tokens": 50, "completion_tokens": 9}},
        ]}
        ctx = {"systemPrompt": "sys", "tools": TOOLS, "messages": [{"role": "user", "content": "read b"}]}
        events, msg = run(self.model(), ctx, {"apiKey": COPILOT_TOKEN})
        self.assertEqual(msg["content"][0], {"type": "text", "text": "Let me look."})
        self.assertEqual((msg["content"][1]["id"], msg["content"][1]["arguments"]), ("toolu_1", {"path": "b"}))
        self.assertEqual((msg["stopReason"], msg["provider"], msg["usage"]["input"]), ("toolUse", "github-copilot", 50))
        req = self.reqs("/capi/chat/completions")[0]
        h = req["headers"]
        self.assertEqual((h["authorization"], h["copilot-integration-id"], h["editor-version"], h["x-initiator"],
                          h["openai-intent"]), (f"Bearer {COPILOT_TOKEN}", "vscode-chat", "vscode/1.107.0", "user",
                                                "conversation-edits"))
        self.assertTrue(h["user-agent"].startswith("GitHubCopilotChat/"))
        self.assertEqual(req["body"]["model"], "claude-sonnet-5.5")
        self.assertEqual(req["body"]["tools"][0]["function"]["name"], "read")

        ctx["messages"] += [msg, {"role": "toolResult", "toolCallId": "toolu_1", "toolName": "read",
                                  "content": [{"type": "text", "text": "data"}], "isError": False}]
        run(self.model(), ctx, {"apiKey": COPILOT_TOKEN})
        self.assertEqual(self.reqs("/capi/chat/completions")[1]["headers"]["x-initiator"], "agent")

    def test_responses_wire_model(self):
        self.fake.routes["/capi/responses"] = {"__sse__": [
            {"type": "response.output_item.added", "output_index": 0,
             "item": {"type": "function_call", "id": "fc_9", "call_id": "call_9", "name": "read"}},
            {"type": "response.function_call_arguments.done", "output_index": 0, "arguments": '{"path":"c"}'},
            {"type": "response.output_item.done", "output_index": 0,
             "item": {"type": "function_call", "id": "fc_9", "call_id": "call_9", "name": "read"}},
            {"type": "response.completed", "response": {"status": "completed", "usage": {"input_tokens": 3, "output_tokens": 4}}},
        ]}
        model = self.model("gpt-5.3-codex")
        _ev, msg = run(model, {"tools": TOOLS, "messages": [{"role": "user", "content": "go"}]},
                       {"apiKey": COPILOT_TOKEN, "reasoning": "medium"})
        self.assertEqual((msg["content"][0]["id"], msg["content"][0]["arguments"], msg["stopReason"]),
                         ("call_9|fc_9", {"path": "c"}, "toolUse"))
        req = self.reqs("/capi/responses")[0]
        self.assertEqual((req["headers"]["authorization"], req["headers"]["copilot-integration-id"]),
                         (f"Bearer {COPILOT_TOKEN}", "vscode-chat"))
        self.assertEqual(req["body"]["reasoning"], {"effort": "medium", "summary": "auto"})

    def test_raw_github_token_uses_cli_identity(self):
        seen = {}
        real = S.stream_openai

        def spy(model, context, options):
            seen.update(base=model["baseUrl"], headers=options["headers"])
            return iter([{"type": "done", "message": {}}])
        S.stream_openai = spy
        try:
            model = dict(M.ModelRegistry(self.dir).find("github-copilot", "gpt-4.1"))
            list(S.stream(model, {"messages": [{"role": "user", "content": "hi"}]}, {"apiKey": "gho_plain"}))
            self.assertEqual(seen["base"], S.COPILOT_RAW_TOKEN_BASE)
            self.assertEqual(seen["headers"]["Copilot-Integration-Id"], "copilot-developer-cli")
            list(S.stream(model, {"messages": [{"role": "user", "content": "hi"}]},
                          {"apiKey": "tid=1;proxy-ep=proxy.individual.githubcopilot.com;"}))
            self.assertEqual(seen["base"], "https://api.individual.githubcopilot.com")
        finally:
            S.stream_openai = real


if __name__ == "__main__":
    unittest.main()
