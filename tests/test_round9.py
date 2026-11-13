"""Heartbeats and signals in the scheduler, self-evolution proposals, and evaluations. Offline."""
import contextlib
import io
import json
import sys
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from test_capabilities import CapCase, Port  # noqa: E402
from src.pi import evals, evolve  # noqa: E402
from src.pi import gateway as gw  # noqa: E402
from src.pi.ai import Faux  # noqa: E402
from src.pi.caps import scheduler  # noqa: E402
from src.pi.commands import Commands  # noqa: E402


class TestHeartbeatAndSignals(CapCase):
    def setUp(self):
        super().setUp()
        (self.profiles / "sc.json").write_text(json.dumps({"name": "sc", "extends": "core", "capabilities": ["scheduler"]}))

    def test_heartbeat_is_silent_unless_something_needs_attention(self):
        s = self.session(profile="sc")
        (self.dir / "HEARTBEAT.md").write_text("Check that a.txt still has three lines.", encoding="utf-8")
        r = self.call(s, "cronjob", {"action": "create", "name": "pulse", "prompt": "Anything to report?", "schedule": "every 30m",
                                     "heartbeat": True})
        jid = r["details"]["id"]
        Faux.queue = [{"text": "HEARTBEAT_OK"}]
        res = scheduler.run_due(self.agent, now=time.time() + 2000)
        self.assertEqual((res[0]["status"], res[0]["file"], res[0]["delivered"]), ("idle", "", False))
        self.assertFalse((self.agent / "cron" / "output" / jid).exists())
        prompt = Faux.requests[0]["messages"][0]["content"]
        self.assertIn("Standing instructions (HEARTBEAT.md):\nCheck that a.txt still has three lines.", prompt)
        self.assertIn("reply with exactly HEARTBEAT_OK", prompt)
        Faux.queue = [{"text": "a.txt now has five lines - someone changed it."}]
        res = scheduler.run_due(self.agent, now=time.time() + 4000)
        self.assertEqual(res[0]["status"], "ok")
        self.assertTrue(Path(res[0]["file"]).is_file())
        job = next(j for j in scheduler.load_jobs(self.agent) if j["id"] == jid)
        self.assertEqual((job["runs"], job["last_status"]), (2, "ok"))

    def test_jobs_wait_for_signals(self):
        s = self.session(profile="sc")
        one = self.call(s, "cronjob", {"action": "create", "name": "after-deploy", "prompt": "Run the smoke tests.", "schedule": "on deploy-done"})
        many = self.call(s, "cronjob", {"action": "create", "name": "log-it", "prompt": "Note the deploy.", "schedule": "on deploy-done repeat"})
        self.assertIsNone(next(j for j in scheduler.load_jobs(self.agent) if j["id"] == one["details"]["id"])["next_run"])
        self.assertEqual(scheduler.run_due(self.agent, now=time.time() + 10 ** 7), [])        # no signal, however long
        sig = self.call(s, "cronjob", {"action": "signal", "name": "deploy-done", "payload": "version 2.4.1 is live"})
        self.assertIn("2 job(s) are waiting", self.text(sig))
        Faux.queue = [{"text": "smoke ok"}, {"text": "noted"}]
        res = scheduler.run_due(self.agent)
        self.assertEqual(sorted(r["name"] for r in res), ["after-deploy", "log-it"])        # one signal wakes both
        self.assertTrue(Faux.requests[0]["messages"][0]["content"].endswith("The event you were waiting for has arrived: version 2.4.1 is live"))
        jobs = {j["name"]: j for j in scheduler.load_jobs(self.agent)}
        self.assertEqual((jobs["after-deploy"]["enabled"], jobs["log-it"]["enabled"]), (False, True))
        self.assertEqual(scheduler.run_due(self.agent), [])                                  # consumed
        scheduler.send_signal(self.agent, "deploy-done", "")
        Faux.queue = [{"text": "noted again"}]
        self.assertEqual([r["name"] for r in scheduler.run_due(self.agent)], ["log-it"])
        with self.assertRaises(ValueError):
            scheduler.send_signal(self.agent, "bad name!")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(scheduler.cli(["signal", "nobody-listens", "hello"], self.agent), 0)
        self.assertIn("0 job(s) are waiting", out.getvalue())

    def test_signal_over_the_gateway_http_channel(self):
        gw.save_config(self.agent, {"profile": "sc", "model": "faux/faux-1", "cwd": str(self.dir),
                                    "channels": {"http": {"port": 0, "token": "tok"}}})
        scheduler.create_job(self.agent, "hooked", "React to the webhook.", "on ci-finished", str(self.dir), "sc", "faux/faux-1")
        g = gw.Gateway(self.agent)
        try:
            port = g.channels["http"].port

            def post(body, token="tok"):
                req = urllib.request.Request(f"http://127.0.0.1:{port}/signal", data=json.dumps(body).encode(),
                                             headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
                try:
                    with urllib.request.urlopen(req, timeout=10) as r:
                        return r.status, json.loads(r.read())
                except urllib.error.HTTPError as e:
                    return e.code, json.loads(e.read())
            self.assertEqual(post({"name": "ci-finished", "payload": "build 81 passed"}), (200, {"queued": True, "waiting_jobs": 1}))
            self.assertEqual(post({"name": "x"}, token="nope")[0], 401)
            self.assertEqual(post({"payload": "no name"})[0], 400)
        finally:
            g.stop()
        Faux.queue = [{"text": "reacted"}]
        self.assertEqual([r["text"] for r in scheduler.run_due(self.agent)], ["reacted"])


class TestSelfEvolution(CapCase):
    def past_session(self):
        old = self.session(no_session=False)
        Faux.queue = [{"tool": "bash", "args": {"command": "echo deploying"}},
                      {"text": "Deployed: I ran `make build`, then `make ship ENV=staging`, then checked /health. " * 4}]
        old.prompt("Deploy the service to staging. We always build first, then ship, then check the health endpoint. " * 3)
        return old

    def test_proposals_are_reviewed_then_applied(self):
        self.past_session()
        s = self.session(capabilities=["self-evolve"], no_session=False)
        port = Port()
        cmds = Commands(s, port)
        cmds.run("evolve", "")
        self.assertIn("No proposals", port.notes[-1][1])
        Faux.queue = [{"text": json.dumps({"proposals": [
            {"kind": "new_skill", "name": "deploy-staging", "description": "Deploy the service to staging", "content": "1. make build\n2. make ship ENV=staging\n3. curl /health",
             "reason": "worked out by hand in a past session"},
            {"kind": "memory", "target": "user", "content": "Always builds before shipping", "reason": "stated twice"},
            {"kind": "patch_skill", "name": "not-there", "content": "x", "reason": "r"},
            {"kind": "new_skill", "name": "Bad Name", "content": "x"}, {"kind": "nonsense", "content": "x"}]})}]
        cmds.run("evolve", "run")
        out = port.notes[-1][1]
        self.assertIn("3 proposal(s) to review", out)
        req = Faux.requests[-1]
        self.assertIn("EXISTING SKILLS:", req["messages"][0]["content"])
        self.assertIn("make ship ENV=staging", req["messages"][0]["content"])               # it read the past session
        self.assertIn("[tools: bash]", req["messages"][0]["content"])
        pending = [p for p in evolve.load(self.agent) if p["status"] == "pending"]
        self.assertEqual(sorted(p["kind"] for p in pending), ["memory", "new_skill", "new_skill"])   # unknown patch -> new skill
        skill = next(p for p in pending if p.get("name") == "deploy-staging")
        self.assertFalse((self.agent / "skills" / "deploy-staging").exists())               # nothing applied yet
        cmds.run("evolve", f"show {skill['id']}")
        self.assertIn("Why: worked out by hand", port.notes[-1][1])
        cmds.run("evolve", f"apply {skill['id']}")
        text = (self.agent / "skills" / "deploy-staging" / "SKILL.md").read_text(encoding="utf-8")
        self.assertTrue(text.startswith('---\nname: deploy-staging\ndescription: "Deploy the service to staging"\n---'))
        self.assertIn("make ship ENV=staging", text)
        self.assertIn("deploy-staging", [k.name for k in s.skills])                          # usable at once
        mem = next(p for p in pending if p["kind"] == "memory")
        cmds.run("evolve", f"apply {mem['id']}")
        self.assertEqual((self.agent / "memories" / "USER.md").read_text(encoding="utf-8"), "Always builds before shipping")
        cmds.run("evolve", f"apply {skill['id']}")
        self.assertIn("already applied", port.notes[-1][1])
        other = next(p for p in pending if p.get("name") == "not-there")
        cmds.run("evolve", f"discard {other['id']}")
        cmds.run("evolve", "list")
        self.assertIn("No proposals", port.notes[-1][1])
        # a later run can correct the skill; the old text is kept beside it
        Faux.queue = [{"text": json.dumps({"proposals": [{"kind": "patch_skill", "name": "deploy-staging",
                                                             "content": "1. make build\n2. make ship ENV=staging\n3. wait 30s\n4. curl /health", "reason": "health check raced"}]})}]
        cmds.run("evolve", "run")
        patch = next(p for p in evolve.load(self.agent) if p["status"] == "pending")
        cmds.run("evolve", f"apply {patch['id']}")
        new = (self.agent / "skills" / "deploy-staging" / "SKILL.md").read_text(encoding="utf-8")
        self.assertTrue(new.startswith("---\nname: deploy-staging"))                         # frontmatter kept
        self.assertIn("wait 30s", new)
        self.assertTrue((self.agent / "skills" / "deploy-staging" / "SKILL.md.bak").is_file())

    def test_nothing_to_learn_and_bad_replies(self):
        s = self.session(capabilities=["self-evolve"])
        self.assertIn("No past sessions", evolve.run(s))
        self.assertEqual(evolve.parse("not json"), [])
        self.assertEqual(evolve.parse('{"proposals": [{"kind": "memory", "content": "x", "target": "weird"}]}')[0]["target"], "memory")
        self.assertIn("usage:", self.cli_out(["bogus"]))
        self.assertIn("No proposal zz", self.cli_out(["show", "zz"]))

    def cli_out(self, argv):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            evolve.cli(argv, self.agent)
        return out.getvalue()


class TestEvals(CapCase):
    def dataset(self):
        f = self.dir / "cases.jsonl"
        f.write_text("\n".join([
            "# comment",
            json.dumps({"id": "sum", "prompt": "17*23?", "expect": "391"}),
            json.dumps({"id": "read", "prompt": "read a.txt", "expect": {"contains": ["alpha"], "not_contains": ["zzz"], "tools": ["read"]}}),
            json.dumps({"id": "date", "prompt": "date?", "expect": {"regex": r"\d{4}-\d{2}-\d{2}"}}),
            json.dumps({"prompt": "just timing"}),
        ]), encoding="utf-8")
        return str(f)

    def test_compare_two_profiles(self):
        ds = self.dataset()
        answers = [{"text": "It is 391."}, {"tool": "read", "args": {"path": "a.txt"}}, {"text": "alpha beta gamma"}, {"text": "2026-11-13"}, {"text": "ok"},
                   {"text": "about 400"}, {"text": "alpha, I guess"}, {"text": "today"}, {"text": "ok"}]
        Faux.queue = list(answers)
        seen = []
        report = evals.run(self.agent, ds, profiles=["tcoder", "core"], models=["faux/faux-1"], cwd=str(self.dir),
                           on_case=lambda label, r: seen.append((label, r["id"], r["passed"])))
        a, b = report["variants"]
        self.assertEqual((a["label"], a["passed"], b["label"], b["passed"]), ("tcoder · faux/faux-1", 4, "core · faux/faux-1", 1))
        self.assertEqual([r["why"] for r in b["results"]], ["missing '391'", "did not call read", "does not match /\\d{4}-\\d{2}-\\d{2}/", ""])
        self.assertEqual(a["results"][1]["tools"], ["read"])
        self.assertEqual(a["results"][1]["turns"], 2)
        self.assertGreater(a["input"], 0)
        self.assertEqual(len(seen), 8)
        self.assertEqual(json.loads(Path(report["file"]).read_text(encoding="utf-8"))["cases"], 4)
        text = evals.format_report(report)
        self.assertIn("tcoder · faux/faux-1   4/4", text)
        self.assertIn("core · faux/faux-1 — failed:\n  sum: missing '391'  (answer: 'about 400')", text)

    def test_checks_and_cli_errors(self):
        self.assertEqual(evals.check("Yes", "well, yes.", []), (True, ""))
        self.assertEqual(evals.check({"not_contains": ["error"]}, "An ERROR occurred", []), (False, "contains 'error'"))
        self.assertEqual(evals.check({"regex": "("}, "x", [])[0], False)
        self.assertEqual(evals.check(None, "", []), (True, ""))
        bad = self.dir / "bad.jsonl"
        bad.write_text('{"id": "x"}\n', encoding="utf-8")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(evals.cli([str(bad)], self.agent), 2)
            self.assertEqual(evals.cli([], self.agent), 2)
            self.assertEqual(evals.cli([str(self.dir / "missing.jsonl")], self.agent), 2)
        self.assertIn("each case needs a `prompt`", out.getvalue())


if __name__ == "__main__":
    unittest.main()
