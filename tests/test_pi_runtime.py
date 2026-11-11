"""Pi profile runtime: tools, sessions, prompt, agent loop, commands, modes and the extension host.

Runs offline with the `faux` provider. Extension-host tests need Node >= 22.13 and are skipped otherwise.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
_AGENT = tempfile.mkdtemp(prefix="omni-pi-agent-")
os.environ["OMNI_PI_AGENT_DIR"] = _AGENT
os.environ.setdefault("OMNI_AUTH_DB", os.path.join(_AGENT, "agent_auth.db"))

from src.pi.agent import AgentSession, SessionOptions  # noqa: E402
from src.pi.ai import Faux  # noqa: E402
from src.pi.extensions.bridge import MIN_NODE, node_executable, node_version  # noqa: E402
from src.pi.modes import HeadlessUI  # noqa: E402
from src.pi.prompt import PromptOptions, build_system_prompt  # noqa: E402
from src.pi.resources import expand_template, PromptTemplate, split_args  # noqa: E402
from src.pi.session import SessionManager  # noqa: E402
from src.pi.tools import create_all_tools, resolve_tool_list  # noqa: E402


def _node_ok() -> bool:
    n = node_executable()
    v = node_version(n) if n else None
    return bool(v and v >= MIN_NODE)


class PiTestCase(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="omni-pi-ws-"))
        (self.dir / "a.txt").write_text("alpha\nbeta\ngamma\n", encoding="utf-8")
        (self.dir / "src").mkdir()
        (self.dir / "src" / "m.py").write_text("def f():\n    return 1\n", encoding="utf-8")
        Faux.queue, Faux.requests = [], []

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def session(self, **kw) -> AgentSession:
        opts = dict(cwd=str(self.dir), model="faux/faux-1", no_extensions=True, mode="print")
        opts.update(kw)
        s = AgentSession(SessionOptions(**opts), ui=HeadlessUI())
        s.start()
        return s


class TestTools(PiTestCase):
    def test_tool_set_and_schemas_match_pi(self):
        tools = create_all_tools(str(self.dir))
        self.assertEqual(sorted(tools), ["bash", "edit", "find", "grep", "ls", "read", "write"])
        self.assertEqual(tools["edit"].parameters["required"], ["path", "edits"])
        self.assertIn("oldText", tools["edit"].parameters["properties"]["edits"]["items"]["properties"])
        self.assertEqual(tools["read"].prompt_snippet, "Read file contents")

    def test_read_offset_limit_messages(self):
        r = create_all_tools(str(self.dir))["read"].execute("1", {"path": "a.txt", "limit": 1})
        self.assertEqual(r["content"][0]["text"], "alpha\n\n[2 more lines in file. Use offset=2 to continue.]")
        from src.pi.tools import ToolError
        with self.assertRaisesRegex(ToolError, "beyond end of file"):
            create_all_tools(str(self.dir))["read"].execute("1", {"path": "a.txt", "offset": 99})

    def test_edit_exact_fuzzy_and_errors(self):
        t = create_all_tools(str(self.dir))["edit"]
        r = t.execute("1", {"path": "a.txt", "edits": [{"oldText": "beta", "newText": "BETA"}]})
        self.assertIn("Successfully replaced 1 block(s)", r["content"][0]["text"])
        self.assertIn("+BETA", r["details"]["diff"])
        from src.pi.tools import ToolError
        with self.assertRaisesRegex(ToolError, "Found 2 occurrences"):
            (self.dir / "b.txt").write_text("x\nx\n")
            t.execute("1", {"path": "b.txt", "edits": [{"oldText": "x", "newText": "y"}]})
        legacy = t.execute("1", {"path": "a.txt", "oldText": "gamma", "newText": "G"})  # legacy single-edit shape
        self.assertIn("Successfully", legacy["content"][0]["text"])

    def test_grep_find_ls_bash(self):
        tools = create_all_tools(str(self.dir))
        self.assertEqual(tools["grep"].execute("1", {"pattern": "return"})["content"][0]["text"], "src/m.py:2:     return 1")
        self.assertIn("src/m.py", tools["find"].execute("1", {"pattern": "*.py"})["content"][0]["text"])
        self.assertIn("src/", tools["ls"].execute("1", {})["content"][0]["text"])
        r = tools["bash"].execute("1", {"command": "echo hi && exit 3"})
        self.assertTrue(r["isError"])
        self.assertTrue(r["content"][0]["text"].endswith("Command exited with code 3"))

    def test_tool_list_semantics(self):
        self.assertEqual(resolve_tool_list("+grep,-write"), ["read", "bash", "edit", "grep"])
        self.assertEqual(resolve_tool_list("read,ls"), ["read", "ls"])


class TestPromptAndResources(PiTestCase):
    def test_system_prompt_sections(self):
        p = build_system_prompt(PromptOptions(cwd="/x", tool_snippets={"read": "Read file contents"}, selected_tools=["read", "bash"],
                                              context_files=[{"path": "/x/AGENTS.md", "content": "be nice"}]))
        self.assertTrue(p.startswith("You are a coding assistant running inside pi"))
        self.assertIn('<project_instructions path="/x/AGENTS.md">', p)
        self.assertIn("- Use bash for file operations like ls, rg, find", p)
        self.assertTrue(p.rstrip().endswith("<cwd>\n/x\n</cwd>"))

    def test_agents_md_and_skills_reach_the_prompt(self):
        (self.dir / "AGENTS.md").write_text("Always answer in haiku.")
        skill = self.dir / ".agents" / "skills" / "pdf-tools"
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_text("---\nname: pdf-tools\ndescription: Work with PDFs.\n---\nUse pdftotext.\n")
        (self.dir / ".git").mkdir()
        s = self.session()
        sp = s.system_prompt()
        self.assertIn("Always answer in haiku.", sp)
        self.assertIn("<name>pdf-tools</name>", sp)
        self.assertIn("Use pdftotext.", s.expand_input("/skill:pdf-tools now"))
        self.assertTrue(s.expand_input("/skill:pdf-tools now").endswith("now"))

    def test_templates(self):
        self.assertEqual(split_args('one "two words" three\'s'), ["one", "two words", "three's"])
        t = PromptTemplate("review", "", "Review ${1:-everything} then $2 | $@", "x")
        self.assertEqual(expand_template(t, "auth tests"), "Review auth then tests | auth tests")
        self.assertEqual(expand_template(t, ""), "Review everything then  | ")


class TestAgentLoopAndSessions(PiTestCase):
    def test_tool_round_trip_and_events(self):
        s = self.session()
        seen = []
        s.subscribe(lambda e: seen.append(e["type"]))
        Faux.queue = [{"tool": "read", "args": {"path": "a.txt"}}, {"text": "done"}]
        s.prompt("read it")
        roles = [e["message"]["role"] for e in s.sessions.entries if e["type"] == "message"]
        self.assertEqual(roles, ["user", "assistant", "toolResult", "assistant"])
        for ev in ("agent_start", "turn_start", "tool_execution_start", "tool_execution_end", "turn_end", "agent_end", "agent_settled"):
            self.assertIn(ev, seen)
        self.assertIn("alpha", Faux.requests[-1]["messages"][-1]["content"][0]["text"])

    def test_session_file_is_pi_v3_and_resumable(self):
        s = self.session()
        s.prompt("hello")
        path = s.sessions.file
        header = json.loads(path.read_text().splitlines()[0])
        self.assertEqual((header["type"], header["version"]), ("session", 3))
        self.assertTrue(path.parent.name.startswith("--") and path.parent.name.endswith("--"))
        s2 = self.session(session=str(path))
        self.assertEqual(len(s2.sessions.build_context()["messages"]), 2)
        s3 = self.session(continue_recent=True)
        self.assertEqual(s3.sessions.session_id, header["id"])

    def test_fork_tree_and_compaction(self):
        s = self.session()
        s.prompt("first")
        s.prompt("second")
        users = [e for e in s.sessions.entries if e["type"] == "message" and e["message"]["role"] == "user"]
        s.navigate_tree(users[1]["id"])  # continue from before "second" -> a new branch
        s.prompt("second, take two")
        self.assertEqual(sum(1 for r in s.sessions.tree_rows() if r["entry"]["type"] == "message"
                             and r["entry"]["message"]["role"] == "user"), 3)
        Faux.queue = [{"text": "## Goal\nsummary"}]
        s.settings.data["compaction"]["keepRecentTokens"] = 1
        e = s.compact()
        self.assertIsNotNone(e)
        ctx = s.sessions.build_context()
        self.assertEqual(ctx["messages"][0]["role"], "compactionSummary")
        old_file = s.sessions.file
        self.assertTrue(s.fork(users[0]["id"]))
        self.assertNotEqual(s.sessions.file, old_file)
        self.assertEqual(s.sessions.header.get("parentSession"), str(old_file))

    def test_user_bash_goes_into_context(self):
        s = self.session()
        s.user_bash("echo from-shell")
        s.user_bash("echo hidden", exclude_from_context=True)
        s.prompt("what did it print?")
        texts = json.dumps(Faux.requests[-1]["messages"])
        self.assertIn("from-shell", texts)
        self.assertNotIn("hidden", texts)

    def test_retry_then_error_message(self):
        s = self.session()
        s.settings.data["retry"] = {"enabled": True, "maxRetries": 1, "baseDelayMs": 1, "maxAgentDelayMs": 5}
        Faux.queue = [{"error": "overloaded", "status": 529}, {"text": "recovered"}]
        s.prompt("hi")
        last = [e["message"] for e in s.sessions.entries if e["type"] == "message"][-1]
        self.assertEqual(last["content"][0]["text"], "recovered")


class TestCliModes(PiTestCase):
    def run_cli(self, *args, stdin=""):
        env = dict(os.environ, OMNI_PI_AGENT_DIR=_AGENT, PYTHONPATH=str(ROOT))
        return subprocess.run([sys.executable, "-m", "src.pi", *args], cwd=str(self.dir), env=env, input=stdin,
                              capture_output=True, text=True, timeout=60)

    def test_print_json_rpc(self):
        r = self.run_cli("-p", "--model", "faux/faux-1", "-ne", "--no-omni-plugins", "ping")
        self.assertEqual(r.stdout.strip(), "faux: ping", r.stderr)
        r = self.run_cli("--mode", "json", "--model", "faux/faux-1", "-ne", "--no-omni-plugins", "x")
        types = [json.loads(l)["type"] for l in r.stdout.splitlines() if l.strip()]
        self.assertIn("agent_settled", types)
        r = self.run_cli("--mode", "rpc", "--model", "faux/faux-1", "-ne", "--no-omni-plugins",
                         stdin='{"id":1,"type":"get_state"}\n{"id":2,"type":"prompt","message":"hi"}\n')
        resp = [json.loads(l) for l in r.stdout.splitlines() if '"response"' in l]
        self.assertTrue(resp[0]["success"] and resp[0]["data"]["model"]["id"] == "faux-1")

    def test_omni_routes_to_core(self):
        from src.pi.launcher import core_argv
        self.assertEqual(core_argv(["pi", "-c"]), ["--profile", "pi", "-c"])
        self.assertEqual(core_argv(["--profile", "lean", "-p", "x"]), ["--profile", "lean", "-p", "x"])
        self.assertEqual(core_argv([])[0], "--profile")
        self.assertIn("-p", core_argv(["run", "hello"]))
        self.assertIsNone(core_argv(["models"]))
        self.assertIsNone(core_argv(["legacy"]))


class TestProfiles(PiTestCase):
    def test_profiles_resolve_through_extends(self):
        from src.pi.profiles import list_profiles, load_profile
        names = [p["name"] for p in list_profiles()]
        self.assertEqual(names[:5], ["core", "lean", "coder", "assistant", "balanced"])
        bal = load_profile("balanced")
        self.assertTrue(bal["permission_gate"] and bal["omni_registry"]["enabled"])
        self.assertEqual(bal["tools"], "read,bash,edit,write,grep,find,ls")  # inherited from lean
        self.assertEqual(load_profile("nonsense")["name"], "core")
        self.assertEqual([load_profile(a)["name"] for a in ("pi", "omp", "hermes", "openclaw")],
                         ["core", "coder", "assistant", "autonomous"])

    def test_profile_changes_tools_and_gate(self):
        s = self.session(profile="lean")
        self.assertEqual(s.active_tool_names[:7], ["read", "bash", "edit", "write", "grep", "find", "ls"])
        s = self.session(profile="pi")
        self.assertEqual(s.active_tool_names, ["read", "bash", "edit", "write"])
        s.profile["permission_gate"] = True
        s._load_profile_capabilities()
        if s.gate.gate is None:
            self.skipTest("Omni PermissionGate not importable")
        Faux.queue = [{"tool": "bash", "args": {"command": "rm -rf ./src"}}, {"text": "ok"}]
        s.prompt("delete")
        self.assertTrue((self.dir / "src").exists())

    def test_directory_extension_entries(self):
        from src.pi.resources import discover_extension_dir
        pkg = self.dir / "exts" / "pkg"
        (pkg / "extensions").mkdir(parents=True)
        (pkg / "package.json").write_text('{"pi": {"extensions": ["./extensions"]}}')
        (pkg / "extensions" / "a.ts").write_text("export default function () {}")
        (pkg / "extensions" / "b.ts").write_text("export default function () {}")
        self.assertEqual(sorted(p.name for p in discover_extension_dir(self.dir / "exts")), ["a.ts", "b.ts"])


@unittest.skipUnless(_node_ok(), "Node >= 22.13 required for the extension host")
class TestExtensionHost(PiTestCase):
    EXT = textwrap.dedent('''
        import { Type } from "@earendil-works/pi-ai";
        import { Text, matchesKey, Key } from "@earendil-works/pi-tui";
        import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
        import { notARealExport } from "@earendil-works/pi-coding-agent";

        enum Mode { A = "a" }
        class Box2 { constructor(private n: number) {} get v() { return this.n; } }

        export default function (pi: ExtensionAPI) {
          pi.registerTool({
            name: "shout", label: "Shout", description: "Uppercase text",
            parameters: Type.Object({ text: Type.String(), loud: Type.Optional(Type.Boolean()) }),
            async execute(_id, p, _s, onUpdate) { onUpdate?.({ content: [{ type: "text", text: "..." }] });
              return { content: [{ type: "text", text: p.text.toUpperCase() + Mode.A + new Box2(2).v }], details: { n: 1 } }; },
            renderCall: (args, theme) => new Text(theme.fg("accent", "shout " + args.text), 0, 0),
          });
          pi.on("tool_call", async (e) => { if (e.toolName === "write") return { block: true, reason: "no writes" }; if (e.toolName === "shout") e.input.text = e.input.text + "!"; });
          pi.on("tool_result", async (e) => e.toolName === "read" ? { content: [{ type: "text", text: "patched" }] } : undefined);
          pi.on("input", async (e) => e.text === "ping" ? { action: "handled" } : e.text.startsWith("?") ? { action: "transform", text: "Q: " + e.text.slice(1) } : { action: "continue" });
          pi.on("before_agent_start", async (e) => ({ systemPrompt: e.systemPrompt + "\\nEXTRA-RULE" }));
          pi.registerCommand("hello", { description: "say hi", handler: async (args, ctx) => { pi.setSessionName("named " + args); pi.appendEntry("x", { ok: matchesKey("\\r", Key.enter) }); } });
          pi.on("session_start", async (_e, ctx) => { ctx.ui.setStatus("demo", "ready"); });
        }
    ''')

    def test_real_extension_api(self):
        ext = self.dir / "ext.ts"
        ext.write_text(self.EXT, encoding="utf-8")
        s = self.session(no_extensions=False, extensions=[str(ext)], tools="read,bash,edit,write")
        info = s.ext.extensions[0]
        self.assertTrue(info["ok"], info.get("error"))
        self.assertIn("pi-coding-agent.notARealExport", info["missing"])
        self.assertIn("shout", s.active_tool_names)
        Faux.queue = [{"tool": "shout", "args": {"text": "hey"}}, {"text": "ok"}]
        s.prompt("go")
        results = [e["message"] for e in s.sessions.entries if e["type"] == "message" and e["message"]["role"] == "toolResult"]
        self.assertEqual(results[-1]["content"][0]["text"], "HEY!a2")
        self.assertIn("EXTRA-RULE", Faux.requests[-1]["systemPrompt"])
        Faux.queue = [{"tool": "write", "args": {"path": "z.txt", "content": "x"}}, {"text": "ok"}]
        s.prompt("write")
        self.assertFalse((self.dir / "z.txt").exists())
        Faux.queue = [{"tool": "read", "args": {"path": "a.txt"}}, {"text": "ok"}]
        s.prompt("read")
        results = [e["message"] for e in s.sessions.entries if e["type"] == "message" and e["message"]["role"] == "toolResult"]
        self.assertEqual(results[-1]["content"][0]["text"], "patched")
        n = len(s.sessions.entries)
        s.prompt("ping")
        self.assertEqual(len(s.sessions.entries), n)
        s.prompt("?why")
        users = [e["message"] for e in s.sessions.entries if e["type"] == "message" and e["message"]["role"] == "user"]
        self.assertEqual(users[-1]["content"], "Q: why")
        s.ext.run_command("hello", "world")
        time.sleep(0.3)
        self.assertEqual(s.sessions.session_name(), "named world")
        self.assertTrue(any(e["type"] == "custom" and e["data"] == {"ok": True} for e in s.sessions.entries))
        lines = s.ext.render({"kind": "toolCall", "name": "shout", "args": {"text": "a"}, "width": 40})
        self.assertIn("shout a", "".join(lines))
        s.shutdown()


if __name__ == "__main__":
    unittest.main()
