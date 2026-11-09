import os as _os, sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))
import _isolation  # noqa: F401

import asyncio, json, os, tempfile, unittest

from src.adapters.registry import MasterToolRegistry
from src.features.prompt_templates import expand
from src.kernel.commands import CommandRegistry
from src.kernel.commands_executor import CommandExecutor
from src.kernel.config import HarnessConfig
from src.kernel.events import EventBus
from src.kernel.session_store import SessionStore
from src.kernel.state import Message, SessionState
from src.paths import user_data_dir, workspace_root


def run(ex, st, line):
    return asyncio.run(ex.execute(line, st))[1]


class TestPiCore(unittest.TestCase):
    def setUp(self):
        self.reg = MasterToolRegistry(config=HarnessConfig(), event_bus=EventBus())
        self.reg.discover_all()
        self.ex = CommandExecutor(command_registry=CommandRegistry(active_profile="full"), tool_registry=self.reg,
                                  config=HarnessConfig())
        self.st = SessionState()
        self.st.messages += [Message(role="user", content="one"), Message(role="assistant", content="a"),
                             Message(role="user", content="two")]

    def test_pi_commands_all_handled(self):
        names = self.ex.handled_names()
        pi = "settings model thinking scoped-models login logout new resume name session tree fork clone compact import copy export share bug trust mcp reload hotkeys changelog".split()
        self.assertEqual([c for c in pi if c not in names], [])

    def test_session_resume_fork_roundtrip(self):
        run(self.ex, self.st, "/name demo")
        sid = self.st.session_id
        SessionStore().save(self.st)
        self.assertIn("1.", run(self.ex, self.st, "/tree"))
        self.assertIn("Forked", run(self.ex, self.st, "/fork 2"))
        self.assertEqual(len(self.st.messages), 2)
        run(self.ex, self.st, f"/resume {sid}")
        self.assertEqual((self.st.session_id, len(self.st.messages)), (sid, 3))

    def test_import_pi_format_and_export(self):
        f = os.path.join(tempfile.mkdtemp(), "pi.jsonl")
        with open(f, "w") as fh:
            fh.write(json.dumps({"type": "session", "id": "pi1"}) + "\n")
            fh.write(json.dumps({"type": "message", "message": {"role": "user", "content": [{"type": "text", "text": "hey"}]}}) + "\n")
            fh.write(json.dumps({"type": "message", "message": {"role": "assistant", "content": [{"type": "text", "text": "yo"}]}}) + "\n")
        run(self.ex, self.st, f"/import {f}")
        self.assertEqual([m.content for m in self.st.messages], ["hey", "yo"])
        out = os.path.join(tempfile.mkdtemp(), "s.html")
        run(self.ex, self.st, f"/export {out}")
        self.assertIn("yo", open(out, encoding="utf-8").read())

    def test_prompt_templates_and_skill_commands(self):
        d = user_data_dir() / "prompts"
        d.mkdir(parents=True, exist_ok=True)
        (d / "review.md").write_text("---\ndescription: x\n---\nReview $1 focusing on ${@:2}", encoding="utf-8")
        self.assertEqual(expand("/review a.py security perf"), "Review a.py focusing on security perf")
        sk = workspace_root() / ".omni" / "skills" / "demo"
        sk.mkdir(parents=True, exist_ok=True)
        (sk / "SKILL.md").write_text("---\nname: demo\ndescription: d\n---\nDo the demo.", encoding="utf-8")
        self.assertIn("Do the demo.", expand("/skill:demo now"))
        self.assertIsNone(expand("/nonexistent"))

    def test_pi_profile_is_barebone(self):
        run(self.ex, self.st, "/profile pi")
        on = {t.name for t in self.reg.list_tools() if t.enabled}
        self.assertEqual(on, {"read", "write", "edit", "bash", "grep", "find", "ls"})
        run(self.ex, self.st, "/profile full")
        self.assertGreater(len([t for t in self.reg.list_tools() if t.enabled]), 7)

    def test_core_tools_work(self):
        d = tempfile.mkdtemp()
        f = os.path.join(d, "a.txt")
        ex = lambda n, a: asyncio.run(self.reg.execute_tool(n, a))
        ex("write", {"path": f, "content": "l1\nl2\nl3\n"})
        self.assertEqual(ex("read", {"path": f, "offset": 2, "limit": 1}).splitlines()[0], "l2")
        ex("edit", {"path": f, "oldText": "l2", "newText": "L2"})
        self.assertIn("L2", open(f).read())
        self.assertIn("hello", ex("bash", {"command": "echo hello"}))
        self.assertIn("a.txt", ex("ls", {"path": d}))


if __name__ == "__main__":
    unittest.main()
