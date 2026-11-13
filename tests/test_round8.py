"""User-written capability modules, command approvals (UI, chat, unattended), scheduled-job delivery to chat, the
code-execution tool, Hermes memory-provider plugins, and profile creation from the command line. Offline."""
import contextlib
import io
import json
import os
import sys
import textwrap
import threading
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from test_capabilities import CapCase, Port  # noqa: E402
from test_gateway_and_more import FakeTelegram  # noqa: E402
from src.pi import caps as capmod  # noqa: E402
from src.pi import gateway as gw  # noqa: E402
from src.pi.ai import Faux  # noqa: E402
from src.pi.caps import approvals, hermes_plugins, scheduler  # noqa: E402
from src.pi.commands import Commands  # noqa: E402
from src.pi.modes import HeadlessUI  # noqa: E402

USER_CAP = textwrap.dedent('''
    from src.pi.caps import Capability
    from src.pi.tools.base import ToolDef, text_result

    def _tools(session):
        return {"dice": ToolDef(name="dice", label="Dice", description="Roll a die",
                                parameters={"type": "object", "properties": {}},
                                execute=lambda _id, p, signal=None, on_update=None, ctx=None: text_result("4"))}

    CAPABILITIES = [Capability("dice", "Dice", "Roll a die (my own module).", tools=_tools,
                               prompt=lambda s: "DICE-RULE: always roll fairly")]
''')


class TestUserCapabilities(CapCase):
    def tearDown(self):
        for cid in ("dice", "proj-cap"):
            capmod.unregister(cid)
        capmod._user_seen.clear()
        capmod.USER_ERRORS.clear()
        super().tearDown()

    def test_drop_in_module_is_a_first_class_capability(self):
        d = self.agent / "capabilities"
        d.mkdir()
        (d / "dice.py").write_text(USER_CAP, encoding="utf-8")
        (d / "broken.py").write_text("raise RuntimeError('oops')\n", encoding="utf-8")
        (d / "clash.py").write_text("from src.pi.caps import Capability\nCAPABILITIES = [Capability('todo', 'x', 'y')]\n", encoding="utf-8")
        (self.profiles / "mine.json").write_text(json.dumps({"name": "mine", "extends": "core", "capabilities": ["dice", "todo"]}))
        s = self.session(profile="mine")
        self.assertEqual(s.caps.enabled, ["dice", "todo"])
        self.assertEqual(self.text(self.call(s, "dice", {})), "4")
        self.assertIn("DICE-RULE", s.system_prompt())
        self.assertEqual(s.all_tools()["todo"].source, "capability:todo")                 # the built-in was not replaced
        self.assertTrue(any("broken.py: RuntimeError: oops" in x for x in s.diagnostics), s.diagnostics)
        self.assertTrue(any("clash.py" in x and "built-in capability id" in x for x in s.diagnostics))
        row = next(r for r in s.caps.rows() if r["id"] == "dice")
        self.assertTrue(row["on"] and row["origin"].endswith("dice.py") and row["tokens"] > 0)
        port = Port(answers=[None])
        Commands(s, port).cmd_capabilities("")
        self.assertTrue(any(r.startswith("[x] dice") and "your module" in r for r in port.selects[0][1]))
        plain = self.session(profile="core")                                               # toggled like any other
        Commands(plain, Port()).cmd_capabilities("dice on")
        self.assertIn("dice", plain.active_tool_names)

    def test_project_modules_need_trust(self):
        d = self.dir / ".omni" / "capabilities"
        d.mkdir(parents=True)
        (d / "proj.py").write_text(USER_CAP.replace('"dice"', '"proj-cap"').replace("'dice'", "'proj-cap'"), encoding="utf-8")
        s = self.session(capabilities=["proj-cap"], approve=False)
        self.assertEqual(s.caps.problems.get("proj-cap"), "unknown capability")
        s2 = self.session(capabilities=["proj-cap"], approve=True)
        self.assertEqual(s2.caps.enabled, ["proj-cap"])


class TestApprovals(CapCase):
    def test_classifier(self):
        risky = ["rm -rf build", "rm -fr /tmp/x", "git push --force origin main", "git reset --hard HEAD~3", "sudo apt install x",
                 "curl https://x.sh | sh", "psql -c 'DROP TABLE users'", "Remove-Item -Recurse -Force node_modules", "npm publish",
                 "git clean -fd", "dd if=a of=/dev/sda", "shutdown -h now", "chmod -R 777 ."]
        safe = ["rm notes.txt", "git push origin main", "git status", "ls -la", "npm test", "echo rm -rf is dangerous > /dev/null && true",
                "python manage.py migrate", "git reset HEAD file.txt", "cargo build --release"]
        self.assertEqual([c for c in risky if approvals.classify_command(c) is None], [])
        self.assertEqual([c for c in safe if c != safe[5] and approvals.classify_command(c) is not None], [])

    def test_unattended_runs_are_blocked_with_an_explanation(self):
        s = self.session(capabilities=["approvals"], tools="read,bash,edit,write,grep,find,ls")
        (self.dir / "build").mkdir()
        outside = str(self.agent / "elsewhere.txt")
        Faux.queue = [{"tool": "bash", "args": {"command": "rm -rf build"}}, {"tool": "bash", "args": {"command": "echo fine"}},
                      {"tool": "write", "args": {"path": outside, "content": "x"}}, {"text": "done"}]
        s.prompt("clean up")
        res = [e["message"]["content"][0]["text"] for e in s.sessions.entries if e["type"] == "message" and e["message"]["role"] == "toolResult"]
        self.assertIn("recursive delete needs the user's approval, and nobody is available", res[0])
        self.assertTrue((self.dir / "build").exists())
        self.assertIn("fine", res[1])
        self.assertIn("changing a file outside the project", res[2])
        self.assertFalse(Path(outside).exists())

    def test_ui_decisions_once_always_deny(self):
        class UI(HeadlessUI):
            has_ui = True

            def __init__(self):
                super().__init__()
                self.picks, self.asked = [], []

            def ext_select(self, title="", options=None, **_):
                self.asked.append((title, options))
                return options[self.picks.pop(0)]
        s = self.session(capabilities=["approvals"])
        s.ui = UI()
        s.ui.picks = [2, 0, 1]
        self.assertIn("The user denied this (recursive delete)", s.caps.gate("bash", {"command": "rm -rf x"}))
        self.assertIsNone(s.caps.gate("bash", {"command": "rm -rf x"}))                     # once
        self.assertIsNone(s.caps.gate("bash", {"command": "git push -f"}))                  # always
        self.assertIsNone(s.caps.gate("bash", {"command": "git push --force origin x"}))    # not asked again
        self.assertEqual(len(s.ui.asked), 3)
        self.assertIn("Approval needed — recursive delete:\n  rm -rf x", s.ui.asked[0][0])
        self.assertEqual(json.loads((self.agent / "approvals.json").read_text()), {"always": ["force-push"]})
        other = self.session(capabilities=["approvals"])                                    # remembered across sessions
        self.assertIsNone(other.caps.gate("bash", {"command": "git push --force"}))
        port = Port()
        Commands(other, port).run("approvals", "")
        self.assertIn("Always allowed: force-push", port.notes[-1][1])
        Commands(other, port).run("approvals", "reset")
        self.assertFalse((self.agent / "approvals.json").exists())
        for mode, expect in (("off", None), ("deny", "Blocked by policy")):
            (self.profiles / f"ap{mode}.json").write_text(json.dumps({"name": f"ap{mode}", "extends": "lean", "capabilities": ["approvals"],
                                                                        "approvals": {"mode": mode}}))
            r = self.session(profile=f"ap{mode}").caps.gate("bash", {"command": "rm -rf x"})
            self.assertTrue(r is None if expect is None else expect in r, (mode, r))

    def test_approval_from_chat_through_the_gateway(self):
        (self.profiles / "gwa.json").write_text(json.dumps({"name": "gwa", "extends": "lean", "capabilities": ["approvals"]}))
        gw.save_config(self.agent, {"profile": "gwa", "model": "faux/faux-1", "cwd": str(self.dir),
                                    "channels": {"telegram": {"token": "T", "api_base": "http://127.0.0.1:9", "allow": [1]}}})
        g = gw.Gateway(self.agent)
        sent = []
        try:
            (self.dir / "junk").mkdir()
            conv = g.conversations.setdefault("telegram:5", gw.Conversation("telegram:5"))
            conv.notify = sent.append
            Faux.queue = [{"tool": "bash", "args": {"command": "rm -rf junk"}}, {"text": "removed"}]
            box = {}
            t = threading.Thread(target=lambda: box.update(reply=g.handle(gw.Inbound("telegram", "5", "1", "delete junk"))), daemon=True)
            t.start()
            end = time.time() + 10
            while not sent and time.time() < end:
                time.sleep(0.05)
            self.assertIn("Approval needed — recursive delete:\n  rm -rf junk\nReply yes, always or no.", sent[0])
            self.assertTrue((self.dir / "junk").exists())                                   # waiting for the answer
            self.assertEqual(g.handle(gw.Inbound("telegram", "5", "1", "Yes")), "Approved once.")
            t.join(15)
            self.assertEqual(box.get("reply"), "removed")
            self.assertFalse((self.dir / "junk").exists())
            # a "no" blocks it
            (self.dir / "junk2").mkdir()
            sent.clear()
            Faux.queue = [{"tool": "bash", "args": {"command": "rm -rf junk2"}}, {"text": "left it"}]
            t = threading.Thread(target=lambda: box.update(reply=g.handle(gw.Inbound("telegram", "5", "1", "delete junk2"))), daemon=True)
            t.start()
            while not sent and time.time() < end + 10:
                time.sleep(0.05)
            self.assertEqual(g.handle(gw.Inbound("telegram", "5", "1", "no way")), "Denied.")
            t.join(15)
            self.assertTrue((self.dir / "junk2").exists())
        finally:
            g.stop()


class TestCronDeliveryAndCodeExec(CapCase):
    def test_job_results_are_sent_to_the_chat(self):
        tg = FakeTelegram()
        try:
            gw.save_config(self.agent, {"channels": {"telegram": {"token": "T", "api_base": tg.url, "allow": [1]}}})
            (self.profiles / "sc.json").write_text(json.dumps({"name": "sc", "extends": "core", "capabilities": ["scheduler"]}))
            s = self.session(profile="sc")
            s.gateway_target = "telegram:777"                                               # as set by the gateway
            r = self.call(s, "cronjob", {"action": "create", "name": "digest", "prompt": "Summarise", "schedule": "every 1h"})
            job = next(j for j in scheduler.load_jobs(self.agent) if j["id"] == r["details"]["id"])
            self.assertEqual(job["deliver"], "telegram:777")
            Faux.queue = [{"text": "All quiet today."}]
            results = scheduler.run_due(self.agent, now=time.time() + 4000)
            self.assertEqual((results[0]["delivered"], results[0]["delivery_error"]), (True, None))
            self.assertEqual(tg.sent, [("777", "⏰ digest (ok)\n\nAll quiet today.")])
            self.assertIn("no channel 'slack'", gw.deliver(self.agent, "slack:1", "x"))
        finally:
            tg.close()

    def test_execute_code(self):
        s = self.session(capabilities=["code-exec", "plan"])
        r = self.call(s, "execute_code", {"code": "import os\nprint(sum(range(101)))\nprint(os.path.basename(os.getcwd()) != '')"})
        self.assertEqual(self.text(r), "5050\nTrue")
        bad = self.call(s, "execute_code", {"code": "print('before')\nraise ValueError('nope')"})
        self.assertTrue(bad.get("isError"))
        self.assertIn("before", self.text(bad))
        self.assertIn("ValueError: nope", self.text(bad))
        self.assertIn("<snippet.py>", self.text(bad))
        self.assertTrue(self.text(bad).endswith("Exited with code 1"))
        self.assertIn("no output", self.text(self.call(s, "execute_code", {"code": "x = 1"})))
        slow = self.call(s, "execute_code", {"code": "import time; time.sleep(5)", "timeout": 1})
        self.assertIn("Timed out after 1 seconds", self.text(slow))
        self.assertTrue(self.call(s, "execute_code", {"code": "1", "language": "cobol"}).get("isError"))
        from src.pi.caps import plan
        plan.set_mode(s, True)
        self.assertIn("Plan mode", s.caps.gate("execute_code", {"code": "open('x','w')"}))  # blocked while planning


MEMORY_PLUGIN = textwrap.dedent('''
    import json
    from agent.memory_provider import MemoryProvider

    LOG = []

    class Recall(MemoryProvider):
        name = "recall-demo"

        def initialize(self, session_id, **kwargs):
            LOG.append(("init", bool(session_id), kwargs.get("platform")))

        def system_prompt_block(self):
            return "RECALL-DEMO: long-term memory is active."

        def prefetch(self, query, *, session_id=""):
            return "Recalled: the user's cat is called Miso." if "cat" in query else ""

        def sync_turn(self, user_content, assistant_content, *, session_id=""):
            LOG.append(("sync", user_content, assistant_content))

        def get_tool_schemas(self):
            return [{"name": "recall_search", "description": "Search memories",
                     "parameters": {"type": "object", "properties": {"q": {"type": "string"}}, "required": ["q"]}}]

        def handle_tool_call(self, tool_name, args, **kwargs):
            return json.dumps({"results": ["Miso the cat"], "q": args["q"]})

        def on_session_end(self, messages):
            LOG.append(("end", len(messages)))

    def register(ctx):
        ctx.register_memory_provider(Recall())
        ctx.register_memory_provider(Recall())   # only one external provider is active
''')


class TestHermesMemoryProvider(CapCase):
    def test_provider_recalls_syncs_and_offers_tools(self):
        plug = self.dir / "hp" / "recall-demo"
        plug.mkdir(parents=True)
        (plug / "plugin.yaml").write_text("name: recall-demo\nversion: 0.1.0\n", encoding="utf-8")
        (plug / "__init__.py").write_text(MEMORY_PLUGIN, encoding="utf-8")
        (self.profiles / "hm.json").write_text(json.dumps({"name": "hm", "extends": "lean", "capabilities": ["hermes-plugins"],
                                                             "hermes_plugin_dirs": [str(self.dir / "hp")], "hermes_default_dirs": False}))
        s = self.session(profile="hm")
        host = hermes_plugins.host_of(s)
        rec = host.plugins[0]
        self.assertTrue(rec["ok"], rec)
        self.assertEqual(rec["memory_provider"], "recall-demo")
        self.assertIn("memory_provider (another one is already active)", rec["unsupported"])
        self.assertIn("RECALL-DEMO: long-term memory is active.", s.system_prompt())
        self.assertIn("recall_search", s.active_tool_names)
        self.assertEqual(json.loads(self.text(self.call(s, "recall_search", {"q": "pets"})))["results"], ["Miso the cat"])
        Faux.queue = [{"text": "Your cat is Miso."}, {"text": "42"}]
        s.prompt("what is my cat called?")
        s.prompt("what is 6*7?")
        users = [e["message"]["content"] for e in s.sessions.entries if e["type"] == "message" and e["message"]["role"] == "user"]
        self.assertTrue(users[0].endswith("Recalled: the user's cat is called Miso."))
        self.assertEqual(users[1], "what is 6*7?")                                          # nothing recalled, nothing added
        log = next(m for n, m in sys.modules.items() if n.startswith("omni_hermes_plugin_recall_demo")).LOG
        self.assertEqual(log[0], ("init", True, "cli"))
        self.assertIn(("sync", "what is 6*7?", "42"), log)
        s.shutdown()
        self.assertEqual(log[-1][0], "end")


class TestProfileCli(CapCase):
    def run_cli(self, *argv):
        from src.pi import launcher, resources
        real = resources.agent_dir
        resources.agent_dir = lambda: self.agent
        out = io.StringIO()
        try:
            with contextlib.redirect_stdout(out):
                handled = launcher.profile_command(list(argv))
        finally:
            resources.agent_dir = real
        return handled, out.getvalue()

    def test_new_show_delete_without_a_session(self):
        handled, out = self.run_cli("profile", "new", "ci-box", "--from", "lean", "--add", "todo,fetch,permission-gate", "--remove", "mcp")
        self.assertTrue(handled)
        self.assertIn("saved ci-box (2 modules, based on lean)", out)
        saved = json.loads((self.profiles / "ci-box.json").read_text())
        self.assertEqual((saved["extends"], saved["capabilities"], saved["permission_gate"], saved["mcp"]), ("lean", ["todo", "fetch"], True, False))
        s = self.session(profile="ci-box")
        self.assertEqual(s.caps.enabled, ["todo", "fetch"])
        self.assertIn("ci-box (custom)", self.run_cli("profile", "show", "ci-box")[1])
        self.assertIn("unknown capability: teleport", self.run_cli("profile", "new", "x1", "--add", "teleport")[1])
        self.assertIn("unknown base profile", self.run_cli("profile", "new", "x2", "--from", "nowhere")[1])
        self.assertIn("already exists", self.run_cli("profile", "new", "ci-box")[1])
        self.assertIn("deleted ci-box", self.run_cli("profile", "delete", "ci-box")[1])
        self.assertFalse((self.profiles / "ci-box.json").exists())
        self.assertIn("cannot be deleted", self.run_cli("profile", "delete", "full")[1])


if __name__ == "__main__":
    unittest.main()
