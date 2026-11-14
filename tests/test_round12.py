"""Budget limits, the github tool, finish notifications, workflow commands, parallel subagents, per-tool retries and
browser key presses. Offline."""
import json
import os
import sys
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from test_capabilities import CapCase, Port  # noqa: E402
from test_capabilities_infra import FakeRelay  # noqa: E402
from src.pi import caps as capmod  # noqa: E402
from src.pi.ai import Faux  # noqa: E402
from src.pi.caps import browser, github, notify  # noqa: E402
from src.pi.commands import Commands  # noqa: E402
from src.pi.modes import HeadlessUI  # noqa: E402
from src.pi.tools.base import ToolDef, ToolError, text_result  # noqa: E402

FAKE_GH = r'''
import json, sys
a = sys.argv[1:]
open(sys.argv[0] + ".log", "a").write(json.dumps(a) + "\n")
if a[:2] == ["pr", "view"] and a[2] == "404":
    sys.stderr.write("GraphQL: Could not resolve to a PullRequest with the number of 404.\n"); sys.exit(1)
if a[:2] == ["pr", "checks"]:
    print("build\tfail\t1m\nlint\tpass\t10s"); sys.exit(8)
print("OUT " + " ".join(a))
'''


class TestBudget(CapCase):
    def test_run_stops_at_the_tool_call_limit_and_can_be_extended(self):
        (self.profiles / "bd.json").write_text(json.dumps({"name": "bd", "extends": "lean", "capabilities": ["budget"],
                                                             "budget": {"run_tool_calls": 2}}))
        s = self.session(profile="bd")
        notices = []
        s.subscribe(lambda ev: notices.append(ev.get("message")) if ev.get("type") == "notice" else None)
        Faux.queue = [{"tool": "read", "args": {"path": "a.txt"}}, {"tool": "ls", "args": {}}, {"tool": "read", "args": {"path": "a.txt"}},
                      {"tool": "ls", "args": {}}, {"text": "Stopped: two steps done, one left."}]
        s.prompt("do a lot")
        res = [e["message"] for e in s.sessions.entries if e["type"] == "message" and e["message"]["role"] == "toolResult"]
        self.assertFalse(res[0].get("isError") or res[1].get("isError"))
        self.assertIn("Budget reached: 2 tool calls in this run (limit 2). Do not call more tools.", res[2]["content"][0]["text"])
        self.assertIn("Budget reached", res[3]["content"][0]["text"])
        self.assertEqual(sum("Budget reached" in str(n) for n in notices), 1)             # one notice, not one per call
        port = Port()
        cmds = Commands(s, port)
        cmds.run("budget", "")
        self.assertIn("tool calls this run: 2 / 2", port.notes[-1][1])
        self.assertIn("Reached:", port.notes[-1][1])
        cmds.run("budget", "extend")
        self.assertIsNone(s.caps.gate("read", {"path": "a.txt"}))
        Faux.queue = [{"tool": "read", "args": {"path": "a.txt"}}, {"text": "ok"}]
        s.prompt("a new message starts a new run")
        self.assertFalse([e["message"] for e in s.sessions.entries if e["type"] == "message" and e["message"]["role"] == "toolResult"][-1].get("isError"))

    def test_time_and_cost_limits(self):
        (self.profiles / "bt.json").write_text(json.dumps({"name": "bt", "extends": "lean", "capabilities": ["budget"],
                                                             "budget": {"run_minutes": 0.0001, "session_usd": 0.5}}))
        s = self.session(profile="bt")
        time.sleep(0.05)
        self.assertIn("minutes (limit 0.0001)", s.caps.gate("read", {}))
        s._budget["limits"] = {"session_usd": 0.5}
        self.assertIsNone(s.caps.gate("read", {}))
        s.sessions.append_message({"role": "assistant", "content": [{"type": "text", "text": "x"}], "timestamp": 1,
                                   "usage": {"cost": {"total": 0.75}}})
        self.assertIn("the session has cost $0.75 (limit $0.50)", s.caps.gate("read", {}))
        Commands(s, Port()).run("budget", "extend")
        self.assertIsNone(s.caps.gate("read", {}))                                           # limit is now $1.00
        plain = self.session(capabilities=["budget"])                                        # defaults when unset
        self.assertEqual(plain._budget["limits"], {"run_tool_calls": 80, "run_minutes": 30})


class TestGithubNotifyWorkflows(CapCase):
    def test_github_tool_maps_actions_to_gh(self):
        fake = self.dir / "fake_gh.py"
        fake.write_text(FAKE_GH, encoding="utf-8")
        old = os.environ.get("OMNI_GH")
        os.environ["OMNI_GH"] = f'"{sys.executable}" "{fake}"'
        try:
            s = self.session(capabilities=["github"])
            g = lambda **kw: self.call(s, "github", kw)  # noqa: E731
            self.assertEqual(self.text(g(action="pr_list", limit=5, state="all")), "OUT pr list --limit 5 --state all")
            self.assertEqual(self.text(g(action="pr_diff", number="12", repo="o/r")), "OUT pr diff 12 --repo o/r")
            self.assertEqual(self.text(g(action="run_view", number="9", failed_log=True)), "OUT run view 9 --log-failed")
            self.assertEqual(self.text(g(action="search_issues", query="crash on start")), "OUT search issues crash on start --limit 20")
            self.assertIn("build\tfail", self.text(g(action="pr_checks", number="3")))          # failing checks are still a result
            self.assertIn("Could not resolve to a PullRequest", self.text(g(action="pr_view", number="404")))
            self.assertIn("number is required", self.text(g(action="issue_view")))
            self.assertTrue(g(action="pr_merge", number="1").get("isError"))                     # no write actions
            self.assertTrue(s.all_tools()["github"].read_only)
        finally:
            os.environ.pop("OMNI_GH", None)
            if old is not None:
                os.environ["OMNI_GH"] = old
        real = github.shutil.which
        github.shutil.which = lambda _n: None
        try:
            self.assertIn("GitHub CLI is not installed", self.session(capabilities=["github"]).caps.problems["github"])
        finally:
            github.shutil.which = real

    def test_notification_only_for_long_runs_in_the_ui(self):
        sent = []
        real = notify.emit
        notify.emit = sent.append
        try:
            (self.profiles / "nt.json").write_text(json.dumps({"name": "nt", "extends": "lean", "capabilities": ["notify"],
                                                                 "notify": {"min_seconds": 0}}))
            s = self.session(profile="nt")
            Faux.queue = [{"text": "done"}]
            s.prompt("hi")
            self.assertEqual(sent, [])                                                        # headless: silent

            class UI(HeadlessUI):
                has_ui = True
            s2 = self.session(profile="nt")
            s2.ui = UI()
            Faux.queue = [{"text": "done"}]
            s2.prompt("hi")
            self.assertEqual(len(sent), 1)
            self.assertIn("Omni finished", sent[0])
            quick = self.session(capabilities=["notify"])                                     # default threshold: 20 s
            quick.ui = UI()
            Faux.queue = [{"text": "done"}]
            quick.prompt("hi")
            self.assertEqual(len(sent), 1)
        finally:
            notify.emit = real

    def test_workflow_commands(self):
        s = self.session(capabilities=["workflows"])
        names = {t.name for t in s.templates}
        self.assertTrue({"review", "commit", "explain", "test", "fix", "catchup"} <= names)
        self.assertIn("Explain how the session loader works", s.expand_input("/explain the session loader"))
        self.assertIn("Do not change any files.", s.expand_input("/review"))
        (self.agent / "prompts").mkdir()
        (self.agent / "prompts" / "review.md").write_text("MY REVIEW $@\n")
        mine = self.session(capabilities=["workflows"])
        self.assertEqual(mine.expand_input("/review x"), "MY REVIEW x\n")                     # yours replaces the bundled one
        self.assertNotIn("commit", {t.name for t in self.session(profile="core").templates})


class TestParallelTasksRetriesKeys(CapCase):
    def test_parallel_subagents(self):
        s = self.session(capabilities=["task"], tools="read,bash,edit,write,grep,find,ls")
        Faux.queue = [{"tool": "task", "args": {"tasks": [{"prompt": "look at A", "description": "area A", "read_only": True},
                                                          {"prompt": "look at B", "description": "area B", "read_only": True},
                                                          {"prompt": "look at C"}]}},
                      {"text": "report"}, {"text": "report"}, {"text": "report"}, {"text": "merged"}]
        s.prompt("investigate three areas")
        res = [e["message"] for e in s.sessions.entries if e["type"] == "message" and e["message"]["role"] == "toolResult"][0]
        text = res["content"][0]["text"]
        self.assertEqual(text, "## Task 1: area A\nreport\n\n## Task 2: area B\nreport\n\n## Task 3\nreport")
        self.assertEqual((res["details"]["tasks"], res["details"]["failed"]), (3, 0))
        subs = [r for r in Faux.requests if "You are a subagent" in (r.get("systemPrompt") or "")]
        self.assertEqual(sorted(r["messages"][0]["content"] for r in subs), ["look at A", "look at B", "look at C"])
        too_many = self.call(s, "task", {"tasks": [{"prompt": "x"}] * 7})
        self.assertIn("at most 6 tasks", self.text(too_many))
        Faux.queue = [{"text": "fine"}]
        mixed = self.call(s, "task", {"tasks": [{"prompt": "ok one"}, {"prompt": "  "}]})
        self.assertIn("## Task 2\nFAILED: prompt is required", self.text(mixed))
        self.assertEqual(mixed["details"]["failed"], 1)
        self.assertFalse(mixed.get("isError"))
        self.assertTrue(self.call(s, "task", {}).get("isError"))

    def test_per_tool_retry_settings(self):
        calls = {"n": 0}

        def tools(_s):
            def flaky(_id, p, signal=None, on_update=None, ctx=None):
                calls["n"] += 1
                if calls["n"] < 4:
                    raise ToolError("HTTP 503 from upstream")
                return text_result("ok")
            return {"flaky": ToolDef(name="flaky", label="f", description="f", parameters={"type": "object", "properties": {}},
                                     execute=flaky, read_only=True)}
        capmod.register(capmod.Capability("flaky4", "t", "t", tools=tools))
        (self.profiles / "dr.json").write_text(json.dumps({"name": "dr", "extends": "lean", "capabilities": ["durable", "flaky4"],
                                                             "durable": {"retries": 1, "backoff": 0.01, "tools": {"flaky": {"retries": 3}}}}))
        try:
            s = self.session(profile="dr")
            Faux.queue = [{"tool": "flaky", "args": {}}, {"text": "done"}]
            s.prompt("go")
            res = [e["message"] for e in s.sessions.entries if e["type"] == "message" and e["message"]["role"] == "toolResult"][0]
            self.assertEqual((res["content"][0]["text"], res["details"]), ("ok", {"retries": 3}))   # the tool's own setting won
        finally:
            capmod.unregister("flaky4")

    def test_browser_key(self):
        relay = FakeRelay()
        relay.connected = True
        make = browser.make_relay
        browser.make_relay = lambda _s: relay
        try:
            s = self.session(capabilities=["browser"])
            r = self.call(s, "browser", {"action": "key", "text": "Escape"})
            self.assertEqual(r["details"], {"action": "key", "key": "Escape"})
            self.assertTrue(any(e[0] == "js" for e in relay.log))
            self.assertIn("key must be one of", self.text(self.call(s, "browser", {"action": "key", "text": "F13"})))
        finally:
            browser.make_relay = make


if __name__ == "__main__":
    unittest.main()
