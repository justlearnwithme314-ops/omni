"""The advisor, the pull request watcher and Pi's keybindings.json. Offline."""
import json
import os
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from test_capabilities import CapCase  # noqa: E402
from src.pi.ai import Faux  # noqa: E402
from src.pi.caps import review  # noqa: E402
from src.pi.tui import keybindings  # noqa: E402

FAKE_GH = r'''
import json, sys
from pathlib import Path
state = json.loads((Path(__file__).parent / "pr.json").read_text())
if "error" in state:
    sys.stderr.write(state["error"]); sys.exit(1)
print(json.dumps(state))
'''


class Round15(CapCase):
    def profile(self, caps, **extra):
        (self.profiles / "r15.json").write_text(json.dumps({"name": "r15", "extends": "lean", "capabilities": caps, **extra}))
        return self.session(profile="r15")


class TestAdvisor(Round15):
    def test_reviews_every_n_turns_and_hands_advice_to_the_agent(self):
        s = self.profile(["advisor"], advisor={"every": 2})
        long = "please refactor the parser module so it handles nested brackets " * 4
        Faux.queue = [{"text": "done one"}, {"text": "done two"},
                      {"text": "You claimed the tests pass without running them. Run them."},     # the review after turn 2
                      {"text": "ok, running"}]
        s.prompt(long)
        self.assertIsNone(s._advice)
        s.prompt("and now add tests")
        self.assertEqual(s._advice, "You claimed the tests pass without running them. Run them.")
        self.assertEqual(Faux.requests[2]["systemPrompt"], review.ADVISOR)
        self.assertEqual(Faux.requests[2]["tools"], [])
        s.prompt("continue")
        users = [e["message"]["content"] for e in s.sessions.entries if e["type"] == "message" and e["message"]["role"] == "user"]
        self.assertIn("[Advisor review of your recent work - take it into account] You claimed", users[-1])
        self.assertIsNone(s._advice)                                 # delivered once

    def test_ok_means_silence_and_advise_command(self):
        s = self.profile(["advisor"])
        self.assertIn("nothing to correct", s.caps.commands()["advise"][1](""))       # too little conversation: no call
        Faux.queue = [{"text": "sure"}, {"text": "OK."}, {"text": "Stop widening the scope."}]
        s.prompt("rename the function parse_args to parse_cli_args everywhere in the repository " * 3)
        self.assertEqual(review.advise(s), "")
        self.assertIn("Stop widening the scope.", s.caps.commands()["advise"][1](""))
        self.assertEqual(s._advice, "Stop widening the scope.")
        self.assertIsNone(review._advisor_on_prompt(s, "/model"))    # commands do not consume it
        self.assertEqual(s._advice, "Stop widening the scope.")


class TestWatchPr(Round15):
    def setUp(self):
        super().setUp()
        (self.dir / "fake_gh.py").write_text(FAKE_GH, encoding="utf-8")
        self._gh = os.environ.get("OMNI_GH")
        os.environ["OMNI_GH"] = f'"{sys.executable}" "{self.dir / "fake_gh.py"}"'

    def tearDown(self):
        os.environ.pop("OMNI_GH", None)
        if self._gh is not None:
            os.environ["OMNI_GH"] = self._gh
        super().tearDown()

    def pr(self, **kw):
        base = {"number": 7, "title": "Add parser", "state": "OPEN", "mergeable": "MERGEABLE", "reviewDecision": "",
                "statusCheckRollup": [{"name": "build", "status": "IN_PROGRESS", "conclusion": ""}], "comments": [], "url": "u"}
        (self.dir / "pr.json").write_text(json.dumps({**base, **kw}))

    def test_changes_wake_the_agent(self):
        s = self.profile(["watch-pr"])
        self.pr()
        r = self.call(s, "watch_pr", {"pr": "#7", "note": "fix whatever fails"})
        self.assertIn('Watching #7 "Add parser": checks pending.', self.text(r))
        self.assertIn("not interactive", self.text(r))
        watch = s._pr_watch["watches"]["7"]
        self.assertIsNone(review.check_once(s, watch))                # nothing changed
        self.pr(statusCheckRollup=[{"name": "build", "status": "COMPLETED", "conclusion": "FAILURE"},
                                   {"name": "lint", "status": "COMPLETED", "conclusion": "SUCCESS"}],
                comments=[{"author": {"login": "ann"}, "body": "please add a test"}])
        Faux.queue = [{"text": "looking at the build"}]
        text = review.check_once(s, watch)
        self.assertEqual(text, '[Pull request #7 "Add parser" changed] checks are failing: build; 1 new comment(s), latest from ann: '
                               'please add a test. You asked to: fix whatever fails')
        users = [e["message"]["content"] for e in s.sessions.entries if e["type"] == "message" and e["message"]["role"] == "user"]
        self.assertEqual(users, [text])
        self.assertIn("#7  watching  checks failing, open", self.text(self.call(s, "watch_pr", {"action": "list"})))
        self.pr(state="MERGED", reviewDecision="APPROVED", statusCheckRollup=[{"name": "build", "conclusion": "SUCCESS"}])
        Faux.queue = [{"text": "merged, done"}]
        text = review.check_once(s, watch)
        self.assertIn("it is now merged; checks are passing; review decision: approved", text)
        self.assertTrue(watch["done"])
        self.assertEqual(self.call(s, "watch_pr", {"action": "stop"})["details"]["stopped"], 1)

    def test_errors_and_finished_prs(self):
        s = self.profile(["watch-pr"])
        (self.dir / "pr.json").write_text(json.dumps({"error": "no pull requests found for branch"}))
        r = self.call(s, "watch_pr", {})
        self.assertTrue(r.get("isError"))
        self.assertIn("no pull requests found", self.text(r))
        self.pr(state="CLOSED")
        self.assertIn("already closed", self.text(self.call(s, "watch_pr", {"pr": "7"})))
        self.assertIn("Not watching", self.text(self.call(s, "watch_pr", {"action": "list"})))
        s.allow_wake = True
        self.pr()
        self.assertTrue(self.call(s, "watch_pr", {"pr": "7"})["details"]["watching"])
        self.assertIsNotNone(s._pr_watch["thread"])
        stop = s._pr_watch["stop"]
        s.caps.toggle("watch-pr", False) if hasattr(s.caps, "toggle") else None
        review._watch_unload(s)
        self.assertTrue(stop.is_set())


class TestKeybindings(CapCase):
    def load(self, data):
        (self.agent / "keybindings.json").write_text(data if isinstance(data, str) else json.dumps(data))
        return keybindings.load(self.agent)

    def test_missing_and_broken_files(self):
        self.assertFalse(keybindings.load(self.agent))
        self.assertIn("keybindings.json:", self.load("{not json").problems[0])
        self.assertEqual(self.load([1]).problems, ["keybindings.json: expected an object of action -> key(s)"])

    def test_rebinding(self):
        km = self.load({"app.model.select": "Ctrl+K", "app.tools.expand": ["ctrl+o", "F2"], "app.thinking.toggle": [],
                        "app.model.cycleBackward": "shift+ctrl+p", "app.session.new": "ctrl+n", "/board all": "f3",
                        "app.tree.editLabel": "shift+l", "nonsense": "f9"})
        self.assertEqual(km.resolve("ctrl+k"), "ctrl+l")             # the new key acts as the action's handler key
        self.assertEqual(km.resolve("ctrl+l"), "")                   # the old default no longer does
        self.assertEqual(km.resolve("f2"), "ctrl+o")
        self.assertIsNone(km.resolve("ctrl+o"))                      # still bound, untouched
        self.assertEqual(km.resolve("ctrl+t"), "")                   # unbound
        self.assertEqual(km.resolve("ctrl+shift+p"), "alt+p")        # modifier order normalised
        self.assertEqual(km.resolve("alt+p"), "")
        self.assertEqual(km.resolve("ctrl+n"), "/new")
        self.assertEqual(km.resolve("f3"), "/board all")
        self.assertIsNone(km.resolve("enter"))
        self.assertEqual(km.problems, ["keybindings.json: unknown action 'nonsense'"])     # Pi actions we lack are ignored quietly
        text = keybindings.describe(km)
        self.assertIn("* app.model.select                   ctrl+k", text)
        self.assertIn("* app.thinking.toggle                (none)", text)
        self.assertIn("  app.exit                           ctrl+d", text)
        self.assertIn("* /board all", text)

    def test_shared_default_keys_survive(self):
        km = self.load({"tui.editor.cursorLineStart": "ctrl+b"})
        self.assertEqual(km.resolve("ctrl+b"), "home")
        self.assertEqual(km.resolve("home"), "")
        self.assertEqual(km.resolve("ctrl+a"), "")
        self.assertEqual(keybindings.normalize("Shift+Alt+Ctrl+X"), "ctrl+alt+shift+x")

    def test_app_applies_the_keymap(self):
        from src.pi.tui import app as tui_app
        src = Path(tui_app.__file__).read_text(encoding="utf-8")
        self.assertIn("km = self.keymap()", src)


if __name__ == "__main__":
    unittest.main()
