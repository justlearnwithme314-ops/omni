"""Command sources (prefix manager): duplicate /commands from extensions, prompt templates and skills.

Offline. The two-extension test needs Node >= 22.13 and is skipped otherwise.
"""
import os
import shutil
import sys
import tempfile
import textwrap
import time
import unittest
from pathlib import Path
from types import SimpleNamespace as NS

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
_AGENT = os.environ.get("OMNI_PI_AGENT_DIR") or tempfile.mkdtemp(prefix="omni-pi-agent-")
os.environ["OMNI_PI_AGENT_DIR"] = _AGENT
os.environ.setdefault("OMNI_AUTH_DB", os.path.join(_AGENT, "agent_auth.db"))

from src.pi.agent import AgentSession, SessionOptions  # noqa: E402
from src.pi.ai import Faux  # noqa: E402
from src.pi.command_sources import CommandSources  # noqa: E402
from src.pi.commands import Commands  # noqa: E402
from src.pi.extensions.bridge import MIN_NODE, node_executable, node_version  # noqa: E402
from src.pi.modes import HeadlessUI  # noqa: E402
from src.pi.resources import PromptTemplate, Skill  # noqa: E402


class FakeSettings(dict):
    def get(self, k, d=None):
        return dict.get(self, k, d)

    def set_global(self, k, v):
        self[k] = v


class FakePort:
    def __init__(self, answers=()):
        self.answers = list(answers)
        self.notes, self.shown = [], []

    def notify(self, text, level="info"):
        self.notes.append((level, text))

    def select(self, title, options, initial=None):
        return self.answers.pop(0) if self.answers else None

    def input(self, title, placeholder="", prefill="", secret=False):
        return self.answers.pop(0) if self.answers else None

    def show(self, title, lines):
        self.shown.append((title, lines))


def fake_session():
    ext = NS(commands={}, command_list=[
        {"id": "/x/code-tools/index.ts::review", "name": "review", "extension": "code-tools", "source": "/x/code-tools/index.ts"},
        {"id": "/y/lint-kit.ts::review", "name": "review", "extension": "lint-kit", "source": "/y/lint-kit.ts"},
        {"id": "/y/lint-kit.ts::lint", "name": "lint", "extension": "lint-kit", "source": "/y/lint-kit.ts"},
    ])
    return NS(settings=FakeSettings(), ext=ext,
              templates=[PromptTemplate("review", "project review", "PROJECT $@", "/p/.pi/prompts/review.md", None, "project")],
              shadowed_templates=[PromptTemplate("review", "user review", "USER $@", "/h/prompts/review.md", None, "user")],
              skills=[Skill("pdf", "pdf skill", "/s/pdf/SKILL.md", "/s/pdf", "user")],
              shadowed_skills=[Skill("pdf", "other pdf", "/t/pdf/SKILL.md", "/t/pdf", "project")])


class TestRegistry(unittest.TestCase):
    def setUp(self):
        self.s = fake_session()
        self.cs = CommandSources(self.s)

    def test_every_source_gets_a_prefix(self):
        q = {e.qualified for e in self.cs.entries()}
        self.assertTrue({"pi:model", "omni:profile", "code-tools:review", "lint-kit:review", "lint-kit:lint",
                         "project:review", "prompt:review", "skill:pdf", "skill-project:pdf"} <= q)

    def test_conflicts_and_pi_order(self):
        c = self.cs.conflicts()
        self.assertEqual([e.qualified for e in c["review"]],
                         ["code-tools:review", "lint-kit:review", "project:review", "prompt:review"])
        self.assertEqual([e.qualified for e in c["skill:pdf"]], ["skill:pdf", "skill-project:pdf"])
        r = self.cs.resolve("/review a b")
        self.assertEqual(len(r.candidates), 4)
        self.assertFalse(r.remembered)
        self.assertEqual(r.entry.qualified, "code-tools:review")  # no chooser: extension before prompt template
        self.assertEqual(r.args, "a b")

    def test_qualified_name_runs_that_source(self):
        r = self.cs.resolve("/lint-kit:review now")
        self.assertTrue(r.explicit)
        self.assertEqual(r.entry.ref["id"], "/y/lint-kit.ts::review")
        self.assertEqual(self.cs.prompt_text(self.cs.resolve("/prompt:review x").entry, "x"), "USER x")
        self.assertEqual(self.cs.prompt_text(self.cs.resolve("/project:review x").entry, "x"), "PROJECT x")
        self.assertEqual(self.cs.resolve("/pi:model").entry.kind, "builtin")
        self.assertEqual(self.cs.resolve("/nope").candidates, [])

    def test_unique_name_stays_raw_for_pi_expansion(self):
        e = self.cs.resolve("/lint").entry
        self.assertEqual(e.kind, "extension")
        s2 = fake_session()
        s2.ext.command_list, s2.shadowed_templates = [], []
        cs2 = CommandSources(s2)
        r = cs2.resolve("/review x")
        self.assertEqual(cs2.prompt_text(r.entry, r.args, unique=True), "/review x")

    def test_chooser_and_remembered_choice(self):
        asked = []
        r = self.cs.resolve("/review", choose=lambda n, c: asked.append(n) or c[2])
        self.assertEqual((asked, r.entry.qualified), (["review"], "project:review"))
        self.cs.remember("review", "prompt:review")
        r = self.cs.resolve("/review", choose=lambda n, c: self.fail("must not ask"))
        self.assertTrue(r.remembered)
        self.assertEqual(r.entry.qualified, "prompt:review")

    def test_prefix_rename_follows_preferences_and_rejects_clashes(self):
        self.cs.remember("review", "lint-kit:review")
        self.assertEqual(self.cs.set_prefix("ext:lint-kit", "LK"), "lk")
        self.assertEqual(self.cs.preferences()["review"], "lk:review")
        self.assertEqual(self.cs.resolve("/lk:lint").entry.name, "lint")
        with self.assertRaises(ValueError):
            self.cs.set_prefix("ext:code-tools", "pi")
        self.cs.set_prefix("ext:lint-kit", None)
        self.assertEqual(self.cs.preferences()["review"], "lint-kit:review")

    def test_completions_offer_each_source(self):
        items = self.cs.completions("rev")
        self.assertEqual([i["insert"] for i in items],
                         ["/code-tools:review ", "/lint-kit:review ", "/project:review ", "/prompt:review "])
        self.assertTrue(all(i["label"].startswith("/review  [") for i in items))
        self.cs.remember("review", "project:review")
        self.assertTrue(next(i for i in self.cs.completions("rev") if i["qualified"] == "project:review")["desc"].startswith("★"))
        self.assertEqual([i["insert"] for i in self.cs.completions("lint-kit:")], ["/lint-kit:lint ", "/lint-kit:review "])
        self.assertEqual(self.cs.completions("mod")[0]["insert"], "/model ")  # unique names insert the bare name

    def test_commands_prefix_manager(self):
        port = FakePort()
        c = Commands(self.s, port)
        c.cmd_commands("prefer review project:review")
        self.assertEqual(self.s.settings["commandSources"], {"review": "project:review"})
        c.cmd_commands("conflicts")
        self.assertIn("/review   -> runs /project:review", port.shown[-1][1])
        c.cmd_commands("prefix lint-kit kit")
        self.assertEqual(self.s.settings["commandPrefixes"], {"ext:lint-kit": "kit"})
        c.cmd_commands("forget review")
        self.assertEqual(self.s.settings["commandSources"], {})
        port.answers = [0, 0, 1]  # menu: duplicates -> /review -> second source
        c.cmd_commands("")
        self.assertEqual(self.s.settings["commandSources"], {"review": "kit:review"})
        names = [x["qualified"] for x in c.all_commands()]
        self.assertIn("kit:review", names)
        self.assertIn("omni:commands", names)


class TestSessionIntegration(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="omni-cs-ws-"))
        self.extra = self.dir / "pack" / "prompts"
        self.extra.mkdir(parents=True)
        (self.extra / "review.md").write_text("---\ndescription: pack review\n---\nPACK $@", encoding="utf-8")
        from src.pi.resources import agent_dir  # other test modules re-point OMNI_PI_AGENT_DIR at import time
        self.user_prompt = Path(agent_dir()) / "prompts" / "review.md"
        self.user_prompt.parent.mkdir(parents=True, exist_ok=True)
        self.user_prompt.write_text("---\ndescription: user review\n---\nUSER $@", encoding="utf-8")
        Faux.queue, Faux.requests = [], []

    def tearDown(self):
        self.user_prompt.unlink(missing_ok=True)
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_shadowed_template_selectable_in_print_mode(self):
        s = AgentSession(SessionOptions(cwd=str(self.dir), model="faux/faux-1", no_extensions=True, mode="print",
                                        prompt_templates=[str(self.extra)]), ui=HeadlessUI())
        s.start()
        try:
            self.assertEqual([t.file_path for t in s.templates], [str(self.extra / "review.md")])
            self.assertEqual([t.file_path for t in s.shadowed_templates], [str(self.user_prompt)])
            cs = CommandSources(s)
            self.assertEqual({e.qualified for e in cs.conflicts()["review"]}, {"pack-prompts:review", "prompt:review"})
            Faux.queue = [{"text": "ok"}, {"text": "ok"}, {"text": "ok"}]
            s.prompt("/review one")          # Pi's override: the later (package) template
            s.prompt("/prompt:review two")   # exact source
            cs.remember("review", "prompt:review")
            s.prompt("/review three")        # remembered choice
            users = [e["message"]["content"] for e in s.sessions.entries
                     if e["type"] == "message" and e["message"]["role"] == "user"]
            self.assertEqual(users, ["PACK one", "USER two", "USER three"])
        finally:
            s.settings.set_global("commandSources", {})
            s.shutdown()


def _node_ok() -> bool:
    n = node_executable()
    v = node_version(n) if n else None
    return bool(v and v >= MIN_NODE)


@unittest.skipUnless(_node_ok(), "Node >= 22.13 required for the extension host")
class TestTwoExtensionsSameCommand(unittest.TestCase):
    def test_both_registrations_survive_and_run_by_prefix(self):
        d = Path(tempfile.mkdtemp(prefix="omni-cs-ext-"))
        try:
            for name in ("alpha-tools", "beta-tools"):
                (d / f"{name}.ts").write_text(textwrap.dedent(f'''
                    export default function (pi) {{
                      pi.registerCommand("review", {{ description: "{name} review",
                        handler: async (args) => {{ pi.setSessionName("{name} " + args); }} }});
                    }}
                '''), encoding="utf-8")
            s = AgentSession(SessionOptions(cwd=str(d), model="faux/faux-1", mode="print", no_extensions=False,
                                            extensions=[str(d / "alpha-tools.ts"), str(d / "beta-tools.ts")]),
                             ui=HeadlessUI())
            s.start()
            try:
                self.assertEqual(len(s.ext.command_list), 2)
                cs = CommandSources(s)
                self.assertEqual([e.qualified for e in cs.conflicts()["review"]], ["alpha-tools:review", "beta-tools:review"])
                s.prompt("/beta-tools:review x")
                time.sleep(0.3)
                self.assertEqual(s.sessions.session_name(), "beta-tools x")
                s.prompt("/review y")  # Pi's order: first registration
                time.sleep(0.3)
                self.assertEqual(s.sessions.session_name(), "alpha-tools y")
            finally:
                s.shutdown()
        finally:
            shutil.rmtree(d, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
