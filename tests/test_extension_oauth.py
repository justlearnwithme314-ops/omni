"""Extension-registered OAuth providers (Pi's `registerProvider(id, {oauth: {login, refreshToken, getApiKey}})`,
e.g. pi-antigravity) through the real Node extension host, and the `/model` picker.
"""
import json
import sys
import textwrap
import unittest
import webbrowser
from pathlib import Path
from types import SimpleNamespace as NS

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from test_pi_runtime import PiTestCase, _node_ok  # noqa: E402
from src.pi.ai import oauth  # noqa: E402
from src.pi.commands import Commands  # noqa: E402

EXT = textwrap.dedent('''
    export default function (pi) {
      pi.registerProvider("demo-sub", {
        name: "Demo Sub", baseUrl: "https://demo.invalid", api: "demo-api",
        models: [{ id: "demo-1", name: "Demo 1", reasoning: false, input: ["text"], contextWindow: 1000, maxTokens: 100,
                   cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 } }],
        async *streamSimple(model, context, options) {
          const msg = { role: "assistant", content: [{ type: "text", text: "key=" + options.apiKey }], stopReason: "stop",
                        provider: model.provider, model: model.id, usage: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, totalTokens: 0,
                        cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, total: 0 } }, timestamp: Date.now() };
          yield { type: "text_delta", delta: msg.content[0].text };
          yield { type: "done", message: msg };
        },
        oauth: {
          name: "Demo Subscription",
          async login(cb) {
            cb.onAuth({ url: "https://demo.invalid/auth?x=1", instructions: "Sign in to Demo" });
            cb.onProgress?.("checking account");
            const code = await cb.onPrompt({ message: "Paste the demo code", placeholder: "code" });
            if (code === "boom") throw new Error("bad code");
            return { access: "acc-" + code, refresh: "ref-1", expires: Date.now() + 3600e3, projectId: "proj-9" };
          },
          async refreshToken(c) { return { ...c, access: "acc-refreshed", expires: Date.now() + 3600e3 }; },
          getApiKey(c) { return JSON.stringify({ token: c.access, projectId: c.projectId }); },
        },
      });
    }
''')


class Port:
    def __init__(self, answers=None, paste="abc"):
        self.notes, self.selects, self.answers, self.paste = [], [], list(answers or []), paste

    def notify(self, text, level="info"):
        self.notes.append((level, text))

    def select(self, title, options, initial=None):
        self.selects.append((title, list(options)))
        return self.answers.pop(0) if self.answers else None

    def input_cancellable(self, title, placeholder="", cancel=None):
        self.notes.append(("prompt", title))
        return self.paste


@unittest.skipUnless(_node_ok(), "Node >= 22.13 required for the extension host")
class TestExtensionOAuth(PiTestCase):
    def setUp(self):
        super().setUp()
        self.opened = []
        self.real_open = webbrowser.open
        webbrowser.open = lambda url, *a, **k: self.opened.append(url)

    def tearDown(self):
        webbrowser.open = self.real_open
        oauth.unregister_extension_provider("demo-sub")
        super().tearDown()

    def test_login_refresh_and_api_key_run_in_the_extension(self):
        ext = self.dir / "demo.ts"
        ext.write_text(EXT, encoding="utf-8")
        s = self.session(no_extensions=False, extensions=[str(ext)])
        self.assertTrue(s.ext.extensions[0]["ok"], s.ext.extensions[0].get("error"))
        p = oauth.PROVIDERS.get("demo-sub")
        self.assertIsNotNone(p)
        self.assertEqual((p.name, p.source), ("Demo Subscription", "extension"))

        port = Port()
        Commands(s, port).cmd_login("")  # menu lists it ...
        self.assertTrue(any("Sign in: Demo Subscription   [demo-sub, from an extension]" in o for _t, opts in port.selects for o in opts))
        s.auth.remove("demo-sub")
        port = Port(answers=[0])
        Commands(s, port).cmd_login("demo-sub")  # ... and /login demo-sub runs the extension's flow
        self.assertEqual(self.opened, ["https://demo.invalid/auth?x=1"])
        texts = [t for _l, t in port.notes]
        self.assertTrue(any("Sign in to Demo" in t for t in texts), texts)
        self.assertIn("checking account", texts)
        self.assertIn(("prompt", "Paste the demo code"), port.notes)
        entry = s.auth.entry("demo-sub")
        self.assertEqual((entry["type"], entry["access"], entry["refresh"], entry["projectId"]), ("oauth", "acc-abc", "ref-1", "proj-9"))
        self.assertTrue(any("Signed in to Demo Subscription" in t for t in texts), texts)

        self.assertEqual(json.loads(s.auth.get_api_key("demo-sub")), {"token": "acc-abc", "projectId": "proj-9"})
        s.auth.set_oauth("demo-sub", dict(entry, expires=1))  # expired -> the extension's refreshToken
        self.assertEqual(json.loads(s.auth.get_api_key("demo-sub"))["token"], "acc-refreshed")
        self.assertEqual(s.auth.entry("demo-sub")["projectId"], "proj-9")

        s.set_model(s.registry.find("demo-sub", "demo-1"))  # the extension's stream gets the getApiKey() value
        s.prompt("hi")
        last = [e["message"] for e in s.sessions.entries if e["type"] == "message"][-1]
        self.assertEqual(json.loads(last["content"][0]["text"][4:])["token"], "acc-refreshed")

        port = Port(paste="boom")
        Commands(s, port).cmd_login("demo-sub")
        self.assertTrue(any(l == "error" and "bad code" in t for l, t in port.notes), port.notes)
        port = Port(paste=None)  # prompt closed = cancel
        Commands(s, port).cmd_login("demo-sub")
        self.assertIn(("info", "Login cancelled."), port.notes)

    def test_core_ids_are_not_taken_over(self):
        self.assertFalse(oauth.register_extension_provider("anthropic", "x", lambda ia: {}, lambda c: c))
        self.assertEqual(oauth.PROVIDERS["anthropic"].source, "core")


class TestModelPicker(PiTestCase):
    def test_provider_then_model(self):
        s = self.session()
        providers = {m["provider"] for m in s.registry.all()}
        ga = sorted(m["id"] for m in s.registry.all() if m["provider"] == "google-antigravity")
        port = Port()
        cmds = Commands(s, port)
        # every provider is one row; the current one first
        port.answers = [None]
        cmds.cmd_model("")
        rows = port.selects[0][1]
        self.assertEqual(len(rows), len(providers))
        self.assertTrue(rows[0].startswith("● faux   1 model · signed in"), rows[0])
        gi = next(i for i, r in enumerate(rows) if r.strip().startswith("google-antigravity "))
        self.assertIn(f"{len(ga)} models", rows[gi])
        self.assertIn("Google Antigravity", rows[gi])
        # provider -> its models (all listed) -> "back" -> provider again -> pick Opus
        port.selects.clear()
        opus = sorted(s.registry.all(), key=lambda m: m.get("name") or m["id"])
        opus = [m for m in opus if m["provider"] == "google-antigravity"]
        oi = next(i for i, m in enumerate(opus) if m["id"] == "claude-opus-5-5")
        port.answers = [gi, len(opus), gi, oi]
        cmds.cmd_model("")
        self.assertEqual(port.selects[1][0].split(" (")[0], "google-antigravity models")
        self.assertEqual(sorted(r.split()[0] for r in port.selects[1][1][:-1]), ga)
        self.assertIn("back to providers", port.selects[1][1][-1])
        self.assertEqual((s.model["provider"], s.model["id"]), ("google-antigravity", "claude-opus-5-5"))
        self.assertIn(("success", "Model: google-antigravity/claude-opus-5-5"), port.notes)


if __name__ == "__main__":
    unittest.main()
