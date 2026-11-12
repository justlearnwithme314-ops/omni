"""Capabilities (src/pi/caps): the toggleable modules profiles are built from, and the modules themselves.

Offline, with the `faux` provider and a local HTTP server.
"""
import http.server
import json
import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from test_pi_runtime import PiTestCase  # noqa: E402
from src.pi import caps as capmod  # noqa: E402
from src.pi.agent import AgentSession, SessionOptions  # noqa: E402
from src.pi.ai import Faux  # noqa: E402
from src.pi.caps import skills as skillcap  # noqa: E402
from src.pi.caps.fetch import html_to_text  # noqa: E402
from src.pi.commands import Commands  # noqa: E402
from src.pi.modes import HeadlessUI  # noqa: E402
from src.pi.profiles import list_profiles, load_profile  # noqa: E402


class Port:
    def __init__(self, answers=None):
        self.notes, self.selects, self.answers = [], [], list(answers or [])

    def notify(self, text, level="info"):
        self.notes.append((level, text))

    def select(self, title, options, initial=None):
        self.selects.append((title, list(options)))
        return self.answers.pop(0) if self.answers else None

    def input(self, title, placeholder="", secret=False):
        return self.answers.pop(0) if self.answers else None

    def reset_transcript(self):
        pass


class CapCase(PiTestCase):
    """Each test gets its own agent dir, so overlays, memories and skills never leak between tests."""

    def setUp(self):
        super().setUp()
        self.agent = Path(tempfile.mkdtemp(prefix="omni-caps-agent-"))
        self.profiles = Path(tempfile.mkdtemp(prefix="omni-caps-profiles-"))
        self._env = os.environ.get("OMNI_PROFILES_DIR")
        os.environ["OMNI_PROFILES_DIR"] = str(self.profiles)
        # a fixed profile for tests, so shipped profiles can grow without breaking them
        (self.profiles / "tcoder.json").write_text(json.dumps(
            {"name": "tcoder", "extends": "lean", "capabilities": ["todo", "task", "ask", "fetch"]}))

    def tearDown(self):
        if self._env is None:
            os.environ.pop("OMNI_PROFILES_DIR", None)
        else:
            os.environ["OMNI_PROFILES_DIR"] = self._env
        super().tearDown()

    def session(self, **kw) -> AgentSession:
        kw.setdefault("agent_dir", str(self.agent))
        return super().session(**kw)

    def call(self, s, tool, args):
        t = s.all_tools()[tool]
        from src.pi.tools.base import ToolContext, ToolError
        try:
            return t.execute("id1", args, None, None, ToolContext(cwd=s.cwd, session=s))
        except ToolError as e:
            return {"content": [{"type": "text", "text": str(e)}], "isError": True}

    @staticmethod
    def text(r):
        return r["content"][0]["text"]


class TestProfilesAndToggles(CapCase):
    def test_core_is_untouched_and_profiles_compose(self):
        s = self.session(profile="core")
        self.assertEqual((s.active_tool_names, s.caps.enabled, s.caps.prompt_section()), (["read", "bash", "edit", "write"], [], ""))
        coder, bal, auto, full = (load_profile(n)["capabilities"] for n in ("coder", "balanced", "autonomous", "full"))
        self.assertTrue({"todo", "task", "ask", "fetch", "checkpoint", "plan"} <= set(coder))
        self.assertEqual(auto[:len(bal)], bal)                       # `+x` entries extend the parent's list
        self.assertTrue({"task", "memory", "skills-universal"} <= set(auto) - set(bal))
        self.assertTrue(set(auto) < set(full) and "skill-authoring" in full)
        self.assertEqual(len(full), len(set(full)))
        self.assertNotIn("hashline", load_profile("full")["capabilities"])  # changes `edit`: opt-in only
        self.assertEqual(capmod.merge_capability_lists(["a", "b"], ["+c", "-a"]), ["b", "c"])
        self.assertEqual(capmod.merge_capability_lists(["a", "b"], ["x"]), ["x"])
        self.assertEqual(capmod.merge_capability_lists(["a"], []), [])
        for p in list_profiles():  # every shipped profile names only real capabilities
            self.assertEqual([c for c in p["capabilities"] if c not in capmod.load_builtin()], [], p["name"])

    def test_coder_profile_tools_and_cost(self):
        s = self.session(profile="tcoder")
        self.assertEqual(s.active_tool_names, ["read", "bash", "edit", "write", "grep", "find", "ls", "todo", "task", "ask", "fetch"])
        rows = {r["id"]: r for r in s.caps.rows()}
        self.assertTrue(rows["todo"]["on"] and rows["todo"]["tokens"] > 20)
        self.assertEqual(rows["todo"]["tools"], ["todo"])
        self.assertFalse(rows["memory"]["on"])
        self.assertTrue(rows["extensions"]["on"] and rows["mcp"]["on"] and not rows["permission-gate"]["on"])
        self.assertIn("Plan and track multi-step tasks", s.system_prompt())
        self.assertEqual(s.all_tools()["todo"].source, "capability:todo")

    def test_toggle_overlay_and_explicit_list(self):
        s = self.session(profile="core")
        port = Port()
        cmds = Commands(s, port)
        cmds.cmd_capabilities("memory on")
        self.assertIn("memory", s.active_tool_names)
        self.assertEqual(json.loads((self.agent / "omni-capabilities.json").read_text()), {"memory": True})
        cmds.cmd_capabilities("permission-gate on")
        self.assertTrue(s.profile["permission_gate"])
        self.assertIsNotNone(s.gate)
        s2 = self.session(profile="lean")  # the overlay follows the user across profiles
        self.assertIn("memory", s2.caps.enabled)
        cmds.cmd_capabilities("memory off")  # back to the profile's own value: override removed
        cmds.cmd_capabilities("permission-gate off")
        self.assertFalse((self.agent / "omni-capabilities.json").exists())
        self.assertNotIn("memory", s.active_tool_names)
        cmds.cmd_capabilities("nope")
        self.assertTrue(any(l == "error" and "Unknown capability" in t for l, t in port.notes))
        s3 = self.session(profile="tcoder", capabilities=["fetch"])  # --capabilities replaces profile + overlay
        self.assertEqual(s3.caps.enabled, ["fetch"])
        s4 = self.session(profile="tcoder", capabilities=[])
        self.assertEqual(s4.caps.enabled, [])

    def test_picker_lists_everything_with_costs_and_saves_custom_profile(self):
        s = self.session(profile="tcoder")
        port = Port(answers=[None])
        cmds = Commands(s, port)
        cmds.cmd_capabilities("")
        title, rows = port.selects[0]
        self.assertIn("profile tcoder", title)
        self.assertIn("tokens per request", title)
        self.assertTrue(any(r.startswith("[x] todo") and "tok)" in r for r in rows))
        self.assertTrue(any(r.startswith("[ ] memory") for r in rows))
        self.assertTrue(any(r.startswith("[x] extensions") for r in rows))
        cmds.cmd_capabilities("ask off")
        cmds.cmd_capabilities("memory on")
        cmds.cmd_profile("save My-Setup")
        saved = json.loads((self.profiles / "my-setup.json").read_text())
        self.assertEqual((saved["extends"], saved["capabilities"]), ("tcoder", ["todo", "task", "fetch", "memory"]))
        (self.agent / "omni-capabilities.json").unlink()
        s2 = self.session(profile="my-setup")
        self.assertEqual(s2.caps.enabled, ["todo", "task", "fetch", "memory"])
        self.assertIn("grep", s2.active_tool_names)  # inherited from tcoder -> lean
        cmds.cmd_profile("save core")
        self.assertTrue(any(l == "error" and "built-in" in t for l, t in port.notes))

    def test_cli_toggle_without_a_session(self):
        import contextlib
        import io
        from src.pi import launcher, profiles, resources
        real, real_active = resources.agent_dir, profiles.active_profile_name
        resources.agent_dir = lambda: self.agent
        profiles.active_profile_name = lambda: "core"
        out = io.StringIO()
        try:
            with contextlib.redirect_stdout(out):
                self.assertTrue(launcher.capability_command(["toggle", "todo", "on"]))
                self.assertEqual(capmod.read_overlay(self.agent), {"todo": True})
                self.assertTrue(launcher.capability_command(["capabilities"]))
                self.assertTrue(launcher.capability_command(["toggle", "todo", "off"]))
                self.assertFalse(launcher.capability_command(["run", "x"]))
        finally:
            resources.agent_dir, profiles.active_profile_name = real, real_active
        self.assertIn("todo: on", out.getvalue())
        self.assertIn("skills-universal", out.getvalue())
        self.assertEqual(capmod.read_overlay(self.agent), {})

    def test_broken_capability_is_reported_not_fatal(self):
        def boom(_s):
            raise RuntimeError("kaput")
        capmod.register(capmod.Capability("broken", "Broken", "raises", tools=boom))
        try:
            s = self.session(capabilities=["broken", "todo", "ghost"])
            self.assertEqual(s.caps.enabled, ["todo"])
            self.assertIn("kaput", s.caps.problems["broken"])
            self.assertEqual(s.caps.problems["ghost"], "unknown capability")
            self.assertTrue(any("capability 'broken' not loaded" in d for d in s.diagnostics))
        finally:
            capmod.unregister("broken")


class TestTodoAskMemory(CapCase):
    def test_todo_round_trip_persists_in_session(self):
        s = self.session(capabilities=["todo"])
        Faux.queue = [{"tool": "todo", "args": {"todos": [{"id": "1", "content": "write tests", "status": "in_progress"},
                                                          {"id": "2", "content": "ship"}]}}, {"text": "planned"}]
        s.prompt("plan")
        result = [e["message"] for e in s.sessions.entries if e["type"] == "message" and e["message"]["role"] == "toolResult"][-1]
        self.assertEqual(self.text(result), "[>] 1. write tests\n[ ] 2. ship\n(0/2 completed)")
        r = self.call(s, "todo", {"todos": [{"id": "1", "status": "completed"}], "merge": True})
        self.assertIn("[x] 1. write tests", self.text(r))
        self.assertIn("(1/2 completed)", self.text(self.call(s, "todo", {})))
        bad = self.call(s, "todo", {"todos": [{"id": "1", "content": "a", "status": "in_progress"},
                                              {"id": "2", "content": "b", "status": "in_progress"}]})
        self.assertTrue(bad.get("isError"))
        self.assertTrue(self.call(s, "todo", {"todos": [{"id": "9", "status": "nope", "content": "x"}]}).get("isError"))

    def test_ask_with_and_without_a_user(self):
        s = self.session(capabilities=["ask"])
        r = self.call(s, "ask", {"question": "Which DB?", "options": ["sqlite", "postgres"]})
        self.assertIn("No interactive user", self.text(r))

        class UI(HeadlessUI):
            has_ui = True

            def __init__(self, pick, typed=None):
                super().__init__()
                self.pick, self.typed, self.seen = pick, typed, []

            def ext_select(self, title="", options=None, **_):
                self.seen.append(options)
                return self.pick

            def ext_input(self, title="", placeholder="", **_):
                return self.typed
        s.ui = UI("postgres")
        self.assertEqual(self.text(self.call(s, "ask", {"question": "Which DB?", "options": ["sqlite", "postgres"]})),
                         "User answered: postgres")
        self.assertEqual(s.ui.seen[0][-1], "Other (type an answer)")
        s.ui = UI("Other (type an answer)", "duckdb")
        self.assertEqual(self.text(self.call(s, "ask", {"question": "Which DB?", "options": ["sqlite"]})), "User answered: duckdb")
        s.ui = UI(None, None)
        self.assertIn("dismissed", self.text(self.call(s, "ask", {"question": "Free text?"})))

    def test_memory_is_bounded_hermes_compatible_and_snapshotted(self):
        s = self.session(capabilities=["memory"])
        self.assertNotIn("MEMORY (your personal notes)", s.system_prompt())  # empty store costs nothing
        self.assertIn("add ok", self.text(self.call(s, "memory", {"action": "add", "target": "memory", "content": "Tests run with python tests/run_all.py"})))
        self.call(s, "memory", {"action": "add", "target": "memory", "content": "Windows box; Git Bash available"})
        self.call(s, "memory", {"action": "add", "target": "user", "content": "Prefers short answers"})
        mem = (self.agent / "memories" / "MEMORY.md").read_text(encoding="utf-8")
        self.assertEqual(mem, "Tests run with python tests/run_all.py\n§\nWindows box; Git Bash available")  # Hermes' format
        self.assertIn("Already stored", self.text(self.call(s, "memory", {"action": "add", "target": "user", "content": "Prefers short answers"})))
        self.call(s, "memory", {"action": "replace", "target": "memory", "old_text": "Git Bash", "content": "Windows 11; Git Bash + PowerShell"})
        self.assertTrue(self.call(s, "memory", {"action": "remove", "target": "memory", "old_text": "nothing like this"}).get("isError"))
        self.assertTrue(self.call(s, "memory", {"action": "replace", "target": "memory", "old_text": "s", "content": "x"}).get("isError"))  # ambiguous
        full = self.call(s, "memory", {"action": "add", "target": "user", "content": "x" * 1400})
        self.assertTrue(full.get("isError"))
        self.assertIn("/1375 chars", self.text(full))
        self.assertNotIn("PowerShell", s.system_prompt())  # snapshot: this session's prompt (and cache) is unchanged
        s2 = self.session(capabilities=["memory"])
        sp = s2.system_prompt()
        self.assertIn("Windows 11; Git Bash + PowerShell", sp)
        self.assertIn("USER PROFILE (who the user is)", sp)
        self.assertIn("/2200 chars]", sp)
        self.assertGreater({r["id"]: r for r in s2.caps.rows()}["memory"]["tokens"], 60)


class TestTaskAndFetch(CapCase):
    def test_subagent_runs_in_a_fresh_context_and_returns_only_its_report(self):
        s = self.session(capabilities=["task", "todo"], tools="read,bash,edit,write,grep,find,ls")
        Faux.queue = [
            {"tool": "task", "args": {"description": "find beta", "prompt": "Read a.txt and report its second line.", "read_only": True}},
            {"tool": "read", "args": {"path": "a.txt"}},     # subagent turn 1
            {"text": "The second line is beta."},              # subagent final report
            {"text": "Done: beta."},                           # parent continues
        ]
        s.prompt("delegate it")
        msgs = [e["message"] for e in s.sessions.entries if e["type"] == "message"]
        self.assertEqual([m["role"] for m in msgs], ["user", "assistant", "toolResult", "assistant"])
        self.assertEqual(self.text(msgs[2]), "The second line is beta.")
        self.assertEqual((msgs[2]["details"]["toolCalls"], msgs[2]["details"]["turns"]), (1, 2))
        self.assertNotIn("alpha", json.dumps(msgs))  # the file content stayed in the subagent's context
        sub = Faux.requests[1]
        self.assertEqual(sub["tools"], ["read", "grep", "find", "ls"])  # read_only, and no task/todo inside
        self.assertIn("You are a subagent", sub["systemPrompt"])
        self.assertEqual(len(sub["messages"]), 1)
        self.assertTrue(self.call(s, "task", {"prompt": " "}).get("isError"))

    def test_fetch_converts_pages_and_pages_through_long_ones(self):
        page = ("<html><head><title>Docs</title><style>x{}</style></head><body><script>bad()</script><h1>Intro</h1>"
                "<p>Hello <a href='https://example.com/x'>link</a></p><ul><li>one</li><li>two</li></ul>" + "<p>" + "word " * 2000 + "</p></body></html>")

        class H(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                if self.path == "/api":
                    body, ctype = b'{"a":[1,2]}', "application/json"
                elif self.path == "/bin":
                    body, ctype = b"\x00\x01", "application/octet-stream"
                elif self.path == "/missing":
                    self.send_response(404)
                    self.end_headers()
                    return
                else:
                    body, ctype = page.encode(), "text/html; charset=utf-8"
                self.send_response(200)
                self.send_header("Content-Type", ctype)
                self.end_headers()
                self.wfile.write(body)
        srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{srv.server_address[1]}"
        try:
            s = self.session(capabilities=["fetch"])
            r = self.call(s, "fetch", {"url": base + "/", "max_chars": 600})
            t = self.text(r)
            self.assertTrue(t.startswith("# Docs\n\n# Intro\n\nHello link (https://example.com/x)\n\n- one\n- two\n\nword word"), t[:120])
            self.assertNotIn("bad()", t)
            self.assertIn("Call fetch again with offset=600", t)
            self.assertEqual(r["details"]["returned"], 600)
            r2 = self.call(s, "fetch", {"url": base + "/", "max_chars": 600, "offset": 600})
            self.assertIn("word word", self.text(r2))
            self.assertEqual(r2["details"]["offset"], 600)
            self.assertEqual(self.text(self.call(s, "fetch", {"url": base + "/api"})), '{\n "a": [\n  1,\n  2\n ]\n}')
            self.assertIn("reads text only", self.text(self.call(s, "fetch", {"url": base + "/bin"})))
            self.assertIn("HTTP 404", self.text(self.call(s, "fetch", {"url": base + "/missing"})))
            self.assertIn("only supports http", self.text(self.call(s, "fetch", {"url": "file:///etc/passwd"})))
            self.assertTrue(s.all_tools()["fetch"].read_only)
        finally:
            srv.shutdown()
            srv.server_close()
        self.assertEqual(html_to_text("<p>a &amp; b</p><p>c</p>"), "a & b\n\nc")


class TestSkills(CapCase):
    def write_skill(self, root, name, extra_fm="", desc=None):
        d = Path(root) / name
        d.mkdir(parents=True)
        (d / "SKILL.md").write_text(f"---\nname: {name}\ndescription: {desc or 'Use for ' + name}\n{extra_fm}---\nBody of {name}\n", encoding="utf-8")

    def test_gating_rules(self):
        g = skillcap.gate_reason
        have = lambda b: "/bin/" + b if b in ("git", "jq") else None  # noqa: E731
        self.assertIsNone(g({}, {}, have, "linux"))
        self.assertIn("only", g({"platforms": ["macos"]}, {}, have, "win32"))
        self.assertIsNone(g({"platforms": ["macos", "Windows"]}, {}, have, "win32"))
        oc = lambda d: {"metadata": {"openclaw": d}}  # noqa: E731
        self.assertIn("needs gh on PATH", g(oc({"requires": {"bins": ["git", "gh"]}}), {}, have, "linux"))
        self.assertIsNone(g(oc({"requires": {"bins": ["git"], "anyBins": ["rg", "jq"]}}), {}, have, "linux"))
        self.assertIn("one of", g(oc({"requires": {"anyBins": ["rg", "fd"]}}), {}, have, "linux"))
        self.assertIn("needs API_KEY set", g(oc({"requires": {"env": ["API_KEY"]}}), {}, have, "linux"))
        self.assertIsNone(g(oc({"requires": {"env": ["API_KEY"]}}), {"API_KEY": "x"}, have, "linux"))
        self.assertIn("darwin only", g(oc({"os": ["darwin"]}), {}, have, "win32"))
        self.assertIsNone(g(oc({"always": True, "os": ["darwin"]}), {}, have, "win32"))
        self.assertIn("needs gh", g({"metadata": '{"openclaw": {"requires": {"bins": ["gh"]}}}'}, {}, have, "linux"))  # JSON string

    def test_universal_roots_filter_and_budget(self):
        shared = self.dir / "shared-skills"
        self.write_skill(shared, "pdf-tools")
        self.write_skill(shared, "mac-only", "platforms: [macos]\n" if not sys.platform == "darwin" else "platforms: [windows]\n")
        self.write_skill(shared, "wordy", desc="Long " + "description words " * 40)
        prof = {"name": "x", "extends": "lean", "capabilities": ["skills-universal"], "skill_dirs": [str(shared)],
                "skills_budget_chars": 200}
        (self.profiles / "x.json").write_text(json.dumps(prof))
        s = self.session(profile="x")
        names = sorted(k.name for k in s.skills)
        self.assertIn("pdf-tools", names)
        self.assertNotIn("mac-only", names)
        self.assertTrue(any("skill 'mac-only' hidden" in d for d in s.diagnostics))
        sp = s.system_prompt()
        self.assertIn("<name>pdf-tools</name>", sp)
        self.assertIn("…</description>", sp)                      # shortened to fit the budget
        self.assertNotIn("description words " * 10, sp)
        self.assertGreater(len(next(k for k in s.skills if k.name == "wordy").description), 500)  # the skill itself is intact
        core = self.session(profile="core")
        self.assertNotIn("pdf-tools", [k.name for k in core.skills])  # core never looks at other harnesses' folders

    def test_skill_authoring_lifecycle(self):
        s = self.session(capabilities=["skill-authoring"])
        r = self.call(s, "skill_manage", {"action": "create", "name": "deploy-staging", "description": "Deploy to staging: use when asked",
                                           "content": "1. run make deploy\n2. check /health"})
        self.assertIn("Created skill", self.text(r))
        f = self.agent / "skills" / "deploy-staging" / "SKILL.md"
        self.assertTrue(f.read_text(encoding="utf-8").startswith('---\nname: deploy-staging\ndescription: "Deploy to staging: use when asked"\n---'))
        self.assertIn("deploy-staging", [k.name for k in s.skills])            # discoverable right away
        self.assertIn("<name>deploy-staging</name>", s.system_prompt())
        self.call(s, "skill_manage", {"action": "patch", "name": "deploy-staging", "old_text": "make deploy", "new_text": "make deploy ENV=staging"})
        self.assertIn("ENV=staging", f.read_text(encoding="utf-8"))
        self.call(s, "skill_manage", {"action": "write_file", "name": "deploy-staging", "file": "scripts/check.sh", "content": "curl /health"})
        self.assertTrue((f.parent / "scripts" / "check.sh").is_file())
        for bad in ({"action": "create", "name": "Bad Name", "description": "d", "content": "c"},
                    {"action": "create", "name": "deploy-staging", "description": "d", "content": "c"},
                    {"action": "write_file", "name": "deploy-staging", "file": "../../escape.txt", "content": "x"},
                    {"action": "patch", "name": "deploy-staging", "old_text": "absent", "new_text": "y"},
                    {"action": "delete", "name": "missing-skill"}):
            self.assertTrue(self.call(s, "skill_manage", bad).get("isError"), bad)
        self.assertFalse((self.agent / "escape.txt").exists())
        self.call(s, "skill_manage", {"action": "delete", "name": "deploy-staging"})
        self.assertFalse(f.exists())
        self.assertNotIn("deploy-staging", [k.name for k in s.skills])


if __name__ == "__main__":
    unittest.main()
