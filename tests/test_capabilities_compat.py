"""Claude Code compatibility (commands, plugins, .mcp.json), scheduled jobs, labeled memory blocks, model roles for
subagents, and the profile wizard. Offline."""
import json
import os
import sys
import textwrap
import time
import unittest
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from test_capabilities import CapCase, Port  # noqa: E402
from src.pi.ai import Faux  # noqa: E402
from src.pi.caps import claude_compat, scheduler  # noqa: E402
from src.pi.commands import Commands  # noqa: E402
from src.pi.profiles import load_profile  # noqa: E402


class TestClaudeCompat(CapCase):
    def make_plugin(self):
        p = self.agent / "plugins" / "review-kit"
        (p / ".claude-plugin").mkdir(parents=True)
        (p / ".claude-plugin" / "plugin.json").write_text(json.dumps({"name": "review-kit", "version": "1.2.0", "description": "Review helpers"}))
        (p / "commands" / "git").mkdir(parents=True)
        (p / "commands" / "review.md").write_text("---\ndescription: Review a file\nargument-hint: <file>\n---\nReview $ARGUMENTS for bugs.\n")
        (p / "commands" / "git" / "changelog.md").write_text("Write a changelog since $1.\n")
        (p / "agents").mkdir()
        (p / "agents" / "sec.md").write_text("---\nname: sec\ndescription: Security reviewer\ntools: Read, Grep\n---\nFind vulnerabilities only.\n")
        (p / "skills" / "release-notes").mkdir(parents=True)
        (p / "skills" / "release-notes" / "SKILL.md").write_text("---\nname: release-notes\ndescription: Write release notes\n---\nSteps...\n")
        (p / "hooks").mkdir()
        guard = p / "hooks" / "guard.py"
        guard.write_text("import json,sys\nd=json.load(sys.stdin)\nif 'prod' in json.dumps(d['tool_input']):\n    sys.stderr.write('plugin says no'); sys.exit(2)\n")
        (p / "hooks" / "hooks.json").write_text(json.dumps({"hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [
            {"type": "command", "command": f'"{sys.executable}" "${{CLAUDE_PLUGIN_ROOT}}/hooks/guard.py"'}]}]}}))
        (p / ".mcp.json").write_text(json.dumps({"mcpServers": {"kit-docs": {"command": "node", "args": ["server.js"], "enabled": False}}}))
        return p

    def test_plugin_contributes_commands_agents_skills_hooks_and_mcp(self):
        plugin = self.make_plugin()
        (self.dir / ".claude" / "commands").mkdir(parents=True)
        (self.dir / ".claude" / "commands" / "ship.md").write_text("Ship it: $ARGUMENTS\n")
        (self.dir / ".mcp.json").write_text(json.dumps({"mcpServers": {"proj-db": {"url": "http://127.0.0.1:1/mcp", "enabled": False}}}))
        (self.agent / "prompts").mkdir()
        (self.agent / "prompts" / "review.md").write_text("MY OWN review of $@\n")           # the user's own wins
        s = self.session(capabilities=["claude-compat", "task", "hooks"], tools="read,bash,edit,write,grep,find,ls", approve=True)
        names = {t.name: t for t in s.templates}
        self.assertTrue({"review", "changelog", "ship"} <= set(names))
        self.assertEqual(s.expand_input("/review a.py"), "MY OWN review of a.py\n")
        self.assertEqual(s.expand_input("/changelog v1.0").strip(), "Write a changelog since v1.0.")
        self.assertEqual(s.expand_input("/ship now please").strip(), "Ship it: now please")
        self.assertIn("release-notes", [k.name for k in s.skills])
        self.assertIn("sec: Security reviewer", s.all_tools()["task"].description)
        self.assertEqual(s.caps.gate("bash", {"command": "deploy prod"}), "Blocked by a PreToolUse hook: plugin says no")
        self.assertIsNone(s.caps.gate("bash", {"command": "ls"}))
        from src.pi import mcp
        cfgs, errors, _ = mcp.load_config(s.agent_dir, s.cwd, True, s.caps.collect("mcp_files"))
        self.assertEqual(sorted(cfgs), ["kit-docs", "proj-db"], errors)
        port = Port()
        self.assertTrue(Commands(s, port).run("plugins", ""))
        out = port.notes[-1][1]
        self.assertIn("review-kit 1.2.0 — 2 command(s), 1 agent(s), 1 skill(s), hooks, MCP servers", out)
        self.assertIn(str(plugin), out)
        core = self.session(profile="core", approve=True)                                 # nothing leaks into core
        self.assertNotIn("ship", [t.name for t in core.templates])
        self.assertNotIn("release-notes", [k.name for k in core.skills])

    def test_untrusted_project_and_no_plugins(self):
        (self.dir / ".claude" / "commands").mkdir(parents=True)
        (self.dir / ".claude" / "commands" / "ship.md").write_text("Ship\n")
        (self.dir / ".mcp.json").write_text(json.dumps({"mcpServers": {"x": {"command": "evil"}}}))
        s = self.session(capabilities=["claude-compat"], approve=False)
        self.assertNotIn("ship", [t.name for t in s.templates])
        self.assertEqual([p for p in s.caps.collect("mcp_files") if str(self.dir) in str(p)], [])
        mine = [p for p in claude_compat.discover_plugins(s) if str(self.agent) in str(p["path"])]
        self.assertEqual(mine, [])


class TestScheduler(CapCase):
    def test_schedule_parsing_and_next_run(self):
        base = datetime(2026, 11, 13, 10, 30).timestamp()                                 # a Friday
        ps, nr = scheduler.parse_schedule, scheduler.next_run
        self.assertEqual(nr(ps("every 2h"), base), base + 7200)
        self.assertEqual(datetime.fromtimestamp(nr(ps("0 9 * * 1-5"), base)), datetime(2026, 11, 16, 9, 0))   # next weekday 09:00
        self.assertEqual(datetime.fromtimestamp(nr(ps("*/15 * * * *"), base)), datetime(2026, 11, 13, 10, 45))
        self.assertEqual(datetime.fromtimestamp(nr(ps("30 8 1 * *"), base)), datetime(2026, 12, 1, 8, 30))
        self.assertEqual(datetime.fromtimestamp(nr(ps("0 0 * * 0"), base)), datetime(2026, 11, 15, 0, 0))     # Sunday
        once = ps("2026-12-24T18:00")
        self.assertEqual((once["kind"], datetime.fromtimestamp(nr(once, base))), ("once", datetime(2026, 12, 24, 18, 0)))
        self.assertIsNone(nr(ps("2020-01-01T00:00"), base))
        for bad in ("every 10s", "61 * * * *", "soon", "* * *"):
            with self.assertRaises(ValueError):
                ps(bad)

    def test_jobs_run_when_due_in_a_fresh_session(self):
        (self.profiles / "sch.json").write_text(json.dumps({"name": "sch", "extends": "core", "capabilities": ["scheduler", "todo"]}))
        s = self.session(profile="sch", no_session=False)
        r = self.call(s, "cronjob", {"action": "create", "name": "status", "prompt": "Summarise a.txt", "schedule": "every 1h"})
        self.assertIn("Scheduled job", self.text(r))
        job_id = r["details"]["id"]
        once = self.call(s, "cronjob", {"action": "create", "prompt": "One time", "schedule": "in 5m"})
        self.assertTrue(self.call(s, "cronjob", {"action": "create", "prompt": "x", "schedule": "whenever"}).get("isError"))
        self.assertIn("every 1h", self.text(self.call(s, "cronjob", {"action": "list"})))
        self.assertEqual(scheduler.run_due(self.agent, now=time.time()), [])               # nothing due yet
        Faux.queue = [{"tool": "read", "args": {"path": "a.txt"}}, {"text": "It has three lines."}, {"text": "did the one-off"}]
        results = scheduler.run_due(self.agent, now=time.time() + 3700)
        self.assertEqual([(x["name"], x["status"]) for x in results], [("status", "ok"), ("One time", "ok")])
        self.assertEqual(results[0]["text"], "It has three lines.")
        self.assertIn("status: ok", Path(results[0]["file"]).read_text(encoding="utf-8"))
        first = Faux.requests[0]
        self.assertIn("scheduled, unattended run", first["systemPrompt"])
        self.assertNotIn("cronjob", first["tools"])                                         # jobs cannot schedule jobs
        self.assertIn("todo", first["tools"])
        jobs = {j["id"]: j for j in scheduler.load_jobs(self.agent)}
        self.assertEqual((jobs[job_id]["runs"], jobs[job_id]["last_status"]), (1, "ok"))
        self.assertGreater(jobs[job_id]["next_run"], time.time() + 3700)
        self.assertEqual((jobs[once["details"]["id"]]["enabled"], jobs[once["details"]["id"]]["next_run"]), (False, None))
        self.assertNotIn("running_since", jobs[job_id])
        self.call(s, "cronjob", {"action": "pause", "id": "status"})
        self.assertEqual(scheduler.run_due(self.agent, now=time.time() + 99999), [])
        self.assertIn("Removed job", self.text(self.call(s, "cronjob", {"action": "remove", "id": job_id})))
        self.assertTrue(self.call(s, "cronjob", {"action": "remove", "id": "nope"}).get("isError"))
        port = Port()
        Commands(s, port).run("cron", "")
        self.assertIn("One time", port.notes[-1][1])

    def test_cli(self):
        import contextlib
        import io
        scheduler.create_job(self.agent, "nightly", "do it", "0 3 * * *", str(self.dir), "lean", "faux/faux-1")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(scheduler.cli(["list"], self.agent), 0)
            self.assertEqual(scheduler.cli(["run"], self.agent), 0)
            self.assertEqual(scheduler.cli(["bogus"], self.agent), 2)
        self.assertIn("nightly", out.getvalue())
        self.assertIn("Nothing is due.", out.getvalue())


class TestMemoryBlocksAndRoles(CapCase):
    def test_extra_labeled_blocks(self):
        (self.profiles / "mb.json").write_text(json.dumps({"name": "mb", "extends": "lean", "capabilities": ["memory"], "memory": {
            "blocks": {"persona": 300, "project": {"limit": 500, "description": "facts about this codebase"}, "bad label!": 5}}}))
        s = self.session(profile="mb")
        self.assertEqual(s.all_tools()["memory"].parameters["properties"]["target"]["enum"], ["memory", "user", "persona", "project"])
        self.call(s, "memory", {"action": "add", "target": "project", "content": "Monorepo; services live under svc/"})
        self.call(s, "memory", {"action": "add", "target": "persona", "content": "Terse and direct"})
        self.assertEqual((self.agent / "memories" / "PROJECT.md").read_text(encoding="utf-8"), "Monorepo; services live under svc/")
        full = self.call(s, "memory", {"action": "add", "target": "persona", "content": "x" * 300})
        self.assertIn("/300 chars", self.text(full))
        self.assertIn("target must be one of: memory, user, persona, project", self.text(self.call(s, "memory", {"action": "add", "target": "nope", "content": "x"})))
        sp = self.session(profile="mb").system_prompt()
        self.assertIn("PROJECT (facts about this codebase) [", sp)
        self.assertIn("/500 chars]", sp)
        self.assertIn("PERSONA [", sp)
        plain = self.session(capabilities=["memory"])                                      # default profile: the two classic stores
        self.assertEqual(plain.all_tools()["memory"].parameters["properties"]["target"]["enum"], ["memory", "user"])

    def test_model_roles_for_subagents(self):
        (self.profiles / "mr.json").write_text(json.dumps({"name": "mr", "extends": "lean", "capabilities": ["task"],
                                                             "model_roles": {"smol": "faux/faux-1", "slow": "openai/gpt-5"}}))
        s = self.session(profile="mr")
        self.assertIn("Model roles (pass as `model`): slow, smol.", s.all_tools()["task"].description)
        Faux.queue = [{"tool": "task", "args": {"prompt": "look", "model": "smol", "read_only": True}}, {"text": "report"}, {"text": "ok"}]
        s.prompt("go")
        res = [e["message"] for e in s.sessions.entries if e["type"] == "message" and e["message"]["role"] == "toolResult"][0]
        self.assertEqual(res["content"][0]["text"], "report")
        os.environ.pop("OPENAI_API_KEY", None)
        s.auth._keyring_failed = True
        bad = self.call(s, "task", {"prompt": "x", "model": "slow"})                        # configured, but not signed in
        self.assertIn("is not a configured role or an available model", self.text(bad))
        self.assertTrue(self.call(s, "task", {"prompt": "x", "model": "ghost-model"}).get("isError"))


class TestProfileWizard(CapCase):
    def test_new_show_edit_delete(self):
        from src.pi.caps import load_builtin
        from src.pi.profiles import BUILTIN, PROJECT_ROOT, list_profiles
        s = self.session(profile="core")
        port = Port()
        cmds = Commands(s, port)
        bases = [p["name"] for p in list_profiles() if p["name"] in BUILTIN or (PROJECT_ROOT / "profiles" / f"{p['name']}.json").is_file()]
        caps = load_builtin()
        order = sorted(caps.values(), key=lambda c: (c.is_on is not None, c.group != "feature", c.id))
        ix = lambda cid: next(i for i, c in enumerate(order) if c.id == cid)  # noqa: E731
        # base lean -> toggle todo, memory, permission-gate on -> continue -> name
        port.answers = [bases.index("lean"), ix("todo"), ix("memory"), ix("permission-gate"), len(order), "Research Box"]
        cmds.cmd_profile("new")
        self.assertTrue(any(l == "error" for l, _t in port.notes))                          # spaces are not allowed in a name
        port.notes.clear()
        port.answers = [bases.index("lean"), ix("todo"), ix("memory"), ix("permission-gate"), len(order), "research-box"]
        cmds.cmd_profile("new")
        self.assertTrue(any("Saved profile 'research-box' (2 modules, based on lean)" in t for _l, t in port.notes), port.notes)
        title2 = port.selects[-1][0]
        self.assertIn("2/3: modules on top of lean", title2)
        self.assertTrue(any(r.startswith("[x] todo") for r in port.selects[-1][1]))
        saved = json.loads((self.profiles / "research-box.json").read_text())
        self.assertEqual((saved["extends"], saved["capabilities"], saved["permission_gate"]), ("lean", ["todo", "memory"], True))
        prof = load_profile("research-box")
        self.assertEqual((prof["tools"], prof["capabilities"], prof["permission_gate"]), ("read,bash,edit,write,grep,find,ls", ["todo", "memory"], True))
        cmds.cmd_profile("research-box")
        self.assertEqual((s.profile["name"], s.caps.enabled), ("research-box", ["todo", "memory"]))
        self.assertIsNotNone(s.gate)
        # show
        port.shown = []
        port.show = lambda title, lines: port.shown.append((title, lines))
        cmds.cmd_profile("show research-box")
        text = "\n".join(port.shown[-1][1])
        self.assertIn("research-box (custom)", text)
        self.assertIn("memory", text)
        self.assertIn("permission-gate", text)
        # edit: drop todo, add fetch; the running session follows
        port.answers = [ix("todo"), ix("fetch"), len(order)]
        cmds.cmd_profile("edit research-box")
        self.assertEqual(json.loads((self.profiles / "research-box.json").read_text())["capabilities"], ["memory", "fetch"])
        self.assertEqual(s.caps.enabled, ["memory", "fetch"])
        self.assertTrue(json.loads((self.profiles / "research-box.json").read_text())["permission_gate"])   # kept
        # guards
        port.notes.clear()
        port.answers = [bases.index("core"), len(order), "research-box"]
        cmds.cmd_profile("new")
        self.assertTrue(any(l == "error" and "already exists" in t for l, t in port.notes))
        cmds.cmd_profile("edit full")
        self.assertTrue(any("built-in profiles cannot be edited" in t for _l, t in port.notes))
        cmds.cmd_profile("delete full")
        self.assertTrue(any("cannot be deleted" in t for _l, t in port.notes))
        cmds.cmd_profile("delete research-box")
        self.assertFalse((self.profiles / "research-box.json").exists())
        port.answers = [None]                                                               # cancelling changes nothing
        cmds.cmd_profile("new")
        self.assertEqual(sorted(p.name for p in self.profiles.glob("*.json")), ["tcoder.json"])


if __name__ == "__main__":
    unittest.main()
