"""OpenClaw provider plugins (from their manifests) and the /context, /tools, /retry, /btw, /handoff, /loop commands. Offline."""
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from test_capabilities import CapCase, Port  # noqa: E402
from src.pi.ai import Faux  # noqa: E402
from src.pi.caps import openclaw_plugins  # noqa: E402
from src.pi.commands import Commands  # noqa: E402

MANIFEST = {
    "id": "fastcloud", "providers": ["fastcloud"],
    "modelCatalog": {"providers": {
        "fastcloud": {"baseUrl": "https://api.fastcloud.test/v1", "api": "openai-completions", "models": [
            {"id": "fc-large", "name": "FC Large", "reasoning": True, "contextWindow": 200000, "maxTokens": 8000,
             "cost": {"input": 1, "output": 2}}, {"id": "fc-small"}, {"name": "no id"}]},
        "weird": {"baseUrl": "https://w.test", "api": "some-private-protocol", "models": [{"id": "w1"}]},
        "anthropic": {"baseUrl": "https://elsewhere.test", "api": "anthropic-messages", "models": [{"id": "shadow"}]}}},
    "setup": {"providers": [{"id": "fastcloud", "envVars": ["FASTCLOUD_API_KEY", "FC_KEY"]}]},
}


class ShowPort(Port):
    def __init__(self):
        super().__init__()
        self.shown = {}

    def show(self, title, lines):
        self.shown[title] = list(lines)


class TestOpenClawProviders(CapCase):
    def make(self):
        plug = self.dir / "oc" / "fastcloud"
        plug.mkdir(parents=True)
        (plug / "openclaw.plugin.json").write_text(json.dumps(MANIFEST), encoding="utf-8")
        (plug / "index.ts").write_text("import { x } from 'openclaw/plugin-sdk/provider-entry';\nexport default x;\n", encoding="utf-8")
        (self.profiles / "oc.json").write_text(json.dumps({
            "name": "oc", "extends": "lean", "capabilities": ["extensions", "openclaw-plugins"],
            "openclaw_plugin_dirs": [str(self.dir / "oc")], "openclaw_default_dirs": False}))
        return self.session(profile="oc")

    def test_manifest_providers(self):
        got = openclaw_plugins.manifest_providers(MANIFEST)
        self.assertEqual(sorted(got), ["anthropic", "fastcloud"])                # the unknown wire format is skipped
        self.assertEqual([m["id"] for m in got["fastcloud"]["models"]], ["fc-large", "fc-small"])
        self.assertEqual(got["fastcloud"]["_env"], ["FASTCLOUD_API_KEY", "FC_KEY"])
        self.assertEqual(openclaw_plugins.manifest_providers({}), {})

    def test_declared_models_become_selectable(self):
        import os
        os.environ["FC_KEY"] = "secret-fc-key"
        try:
            s = self.make()
            if not s.caps.is_enabled("openclaw-plugins"):
                self.skipTest("extension host not available here")
            m = s.registry.find("fastcloud", "fc-large")
            self.assertIsNotNone(m)
            self.assertEqual((m["baseUrl"], m["api"], m["contextWindow"], m["reasoning"]),
                             ("https://api.fastcloud.test/v1", "openai-completions", 200000, True))
            self.assertIsNotNone(s.registry.find("fastcloud", "fc-small"))
            self.assertIsNone(s.registry.find("anthropic", "shadow"))            # a built-in provider is never replaced
            self.assertEqual(s.auth.get_api_key("fastcloud"), "secret-fc-key")   # the first variable that is set
            plugin = openclaw_plugins.discover(s)[0]
            self.assertEqual(plugin["entries"], [])                              # provider-only: its entry is not run
            self.assertIn("model providers: fastcloud (2 models)", openclaw_plugins.describe(s))
            openclaw_plugins._unload(s)
            self.assertIsNone(s.registry.find("fastcloud", "fc-large"))
        finally:
            os.environ.pop("FC_KEY", None)


class TestCommands(CapCase):
    LONG = "please refactor the parser module so that it handles nested brackets correctly " * 4

    def setup_cmds(self):
        s = self.session(profile="tcoder")
        port = ShowPort()
        return s, port, Commands(s, port)

    def users(self, s):
        return [e["message"]["content"] for e in s.sessions.entries if e["type"] == "message" and e["message"]["role"] == "user"]

    def test_context_and_tools(self):
        s, port, c = self.setup_cmds()
        self.assertTrue(c.run("context", ""))
        text = "\n".join(port.shown["context"])
        for part in ("in use", "system prompt", "modules", "tools (", "conversation"):
            self.assertIn(part, text)
        c.run("tools", "")
        self.assertTrue(any(line.startswith("read ") for line in port.shown["tools"]))
        self.assertTrue(any(line.startswith("todo ") for line in port.shown["tools"]))

    def test_retry_and_loop(self):
        s, port, c = self.setup_cmds()
        c.run("retry", "")
        self.assertEqual(port.notes[-1], ("info", "Nothing to retry yet."))
        Faux.queue = [{"text": "first"}, {"text": "second"}]
        s.prompt("say something")
        c.run("retry", "")
        self.assertEqual(self.users(s), ["say something", "say something"])
        c.run("loop", "many times")
        self.assertIn("Usage: /loop", port.notes[-1][1])
        Faux.queue = [{"text": "still working"}, {"text": "all tests pass. DONE"}, {"text": "never reached"}]
        c.run("loop", "5 fix the next failing test")
        self.assertEqual(self.users(s)[2:], ["fix the next failing test"] * 2)
        self.assertIn("DONE (2 of 5)", port.notes[-1][1])
        Faux.queue = [{"text": "a"}, {"text": "b"}]
        c.run("loop", "2 again")
        self.assertEqual(port.notes[-1], ("success", "Loop finished: 2 run(s)."))

    def test_btw_leaves_history_alone(self):
        s, port, c = self.setup_cmds()
        Faux.queue = [{"text": "working on it"}, {"text": "It is in parser.py."}]
        s.prompt(self.LONG)
        before = len(s.sessions.entries)
        c.run("btw", "which file was that?")
        self.assertEqual(port.shown["btw"], ["It is in parser.py."])
        self.assertEqual(len(s.sessions.entries), before)
        self.assertIn("SIDE QUESTION: which file was that?", Faux.requests[-1]["messages"][0]["content"])
        self.assertEqual(Faux.requests[-1]["tools"], [])

    def test_handoff_continues_in_a_new_session(self):
        s, port, c = self.setup_cmds()
        c.run("handoff", "")
        self.assertIn("Not enough conversation", port.notes[-1][1])
        Faux.queue = [{"text": "halfway through the refactor"}, {"text": "Goal: nested brackets. Next steps: add tests."},
                      {"text": "picking up: adding tests"}]
        s.prompt(self.LONG)
        old = s.sessions.session_id
        c.run("handoff", "the tests")
        self.assertNotEqual(s.sessions.session_id, old)
        self.assertEqual(self.users(s), ["Continue this work from the handoff note below.\n\nGoal: nested brackets. Next steps: add tests."])
        self.assertIn("Focus on: the tests.", Faux.requests[1]["systemPrompt"])


if __name__ == "__main__":
    unittest.main()
