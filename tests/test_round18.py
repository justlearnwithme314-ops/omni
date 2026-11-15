"""Personas, /commit and automatic session titles. Offline."""
import json
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from test_capabilities import CapCase  # noqa: E402
from src.pi.ai import Faux  # noqa: E402
from src.pi.caps import polish  # noqa: E402


class Round18(CapCase):
    def profile(self, caps, **extra):
        (self.profiles / "r18.json").write_text(json.dumps({"name": "r18", "extends": "lean", "capabilities": caps, **extra}))
        return self.session(profile="r18")


class TestPersona(Round18):
    def test_switching_and_custom_files(self):
        (self.agent / "personas").mkdir()
        (self.agent / "personas" / "pirate.md").write_text("Talk like a pirate.\nSecond line.", encoding="utf-8")
        (self.agent / "personas" / "concise.md").write_text("My own concise.", encoding="utf-8")
        s = self.profile(["persona"])
        cmd = s.caps.commands()["persona"][1]
        self.assertEqual(s.caps.prompt_section(), "")                 # none by default: costs nothing
        listing = cmd("")
        for name in ("concise", "teacher", "reviewer", "pair", "pirate"):
            self.assertIn(name, listing)
        self.assertEqual(cmd("Pirate"), "Persona: pirate.")
        self.assertIn("Persona (pirate): Talk like a pirate.", s.system_prompt())
        self.assertIn("* pirate", cmd("list"))
        cmd("concise")
        self.assertIn("Persona (concise): My own concise.", s.system_prompt())   # a file replaces the built-in
        self.assertIn("No persona 'ghost'", cmd("ghost"))
        self.assertEqual(cmd("off"), "Persona off.")
        self.assertNotIn("Persona (", s.system_prompt())

    def test_profile_default(self):
        s = self.profile(["persona"], persona="teacher")
        self.assertIn("Persona (teacher): Explain as you go", s.system_prompt())
        s.caps.commands()["persona"][1]("off")
        self.assertNotIn("Persona (", s.system_prompt())


class TestCommit(Round18):
    def git(self, s, *args):
        return subprocess.run(["git", *args], cwd=s.cwd, capture_output=True, text=True)

    def repo(self):
        s = self.profile(["commit"])
        if self.git(s, "init", "-q").returncode != 0:
            self.skipTest("git is not available")
        for k, v in (("user.email", "t@example.test"), ("user.name", "Test"), ("commit.gpgsign", "false")):
            self.git(s, "config", k, v)
        (Path(s.cwd) / "app.py").write_text("print('one')\n", encoding="utf-8")
        self.git(s, "add", "-A")
        self.git(s, "commit", "-q", "-m", "feat: first version")
        return s, s.caps.commands()["commit"][1]

    def test_proposes_then_commits(self):
        s, cmd = self.repo()
        self.assertIn("Nothing to commit", cmd(""))
        (Path(s.cwd) / "app.py").write_text("print('two')\n", encoding="utf-8")
        Faux.queue = [{"text": "```\nfix: print two instead of one\n```"}]
        out = cmd("closes the ticket")
        self.assertIn("Proposed message for all changes to tracked files", out)
        self.assertIn("fix: print two instead of one", out)
        sent = Faux.requests[-1]["messages"][0]["content"]
        self.assertIn("feat: first version", sent)                    # the repository's own style is shown to the model
        self.assertIn("NOTE FROM THE AUTHOR: closes the ticket", sent)
        self.assertIn("+print('two')", sent)
        self.assertEqual(self.git(s, "log", "--oneline").stdout.count("\n"), 1)      # nothing committed yet
        Faux.queue = [{"text": "fix: print two instead of one\n\nThe greeting was wrong."}]
        self.assertIn("Committed all changes to tracked files", cmd("--yes"))
        self.assertEqual(self.git(s, "log", "-1", "--pretty=%B").stdout.strip(), "fix: print two instead of one\n\nThe greeting was wrong.")

    def test_staged_only_and_errors(self):
        s, cmd = self.repo()
        (Path(s.cwd) / "app.py").write_text("print('three')\n", encoding="utf-8")
        (Path(s.cwd) / "new.py").write_text("x = 1\n", encoding="utf-8")
        self.git(s, "add", "new.py")
        Faux.queue = [{"text": "feat: add new module"}]
        self.assertIn("Committed the staged changes", cmd("--yes"))
        self.assertNotIn("three", Faux.requests[-1]["messages"][0]["content"])       # unstaged work is not part of it
        self.assertIn(" M app.py", self.git(s, "status", "--short").stdout)
        Faux.queue = [{"error": "down", "status": 400}]
        self.assertIn("Could not write a commit message", cmd("--yes"))
        plain = self.session(profile="r18", cwd=str(self.agent))
        self.assertEqual(plain.caps.commands()["commit"][1](""), "This folder is not a git repository.")


class TestAutoTitle(Round18):
    def test_names_the_session_once(self):
        s = self.profile(["auto-title"])
        Faux.queue = [{"text": "Sure, here is the fix."}, {"text": '"Fix the login redirect bug."'}, {"text": "more"}]
        s.prompt("the login page redirects to itself, please fix it")
        self.assertEqual(s.sessions.session_name(), "Fix the login redirect bug")
        self.assertEqual(Faux.requests[1]["systemPrompt"], polish.TITLE_SYSTEM)
        self.assertEqual(Faux.requests[1]["options"].get("maxTokens"), 24)
        s.prompt("thanks")
        self.assertEqual(len(Faux.requests), 3)                       # no second title call

    def test_keeps_a_name_you_gave(self):
        s = self.profile(["auto-title"])
        s.set_session_name("my name")
        Faux.queue = [{"text": "ok"}]
        s.prompt("hello there")
        self.assertEqual(s.sessions.session_name(), "my name")
        self.assertEqual(len(Faux.requests), 1)


if __name__ == "__main__":
    unittest.main()
