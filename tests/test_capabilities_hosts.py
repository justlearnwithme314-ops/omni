"""Hosts and hooks: Hermes plugins with their hooks, shell hooks (Claude Code format), web / session search, named
subagents, deferred startup and /doctor. Offline."""
import http.server
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
from src.pi.ai import Faux  # noqa: E402
from src.pi.caps import hermes_plugins, search  # noqa: E402
from src.pi.commands import Commands  # noqa: E402

PLUGIN = textwrap.dedent('''
    import json
    CALLS = []

    def register(ctx):
        async def shout(args, **kw):
            return json.dumps({"result": args["text"].upper(), "task": bool(kw.get("task_id"))})
        ctx.register_tool("shout", "demo", {"name": "shout", "description": "Uppercase text",
                          "parameters": {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]}},
                          shout, is_async=True)
        ctx.register_tool("legacy", {"name": "legacy", "description": "3-arg form", "parameters": {"type": "object", "properties": {}}},
                          lambda args: "legacy ok")
        ctx.register_tool("needs_key", "demo", {"description": "x", "parameters": {"type": "object"}}, lambda a: "x",
                          requires_env=["OMNI_TEST_MISSING_KEY"])
        ctx.register_tool("broken", "demo", {"description": "x", "parameters": {"type": "object"}},
                          lambda a: json.dumps({"error": "nope"}))

        def pre_tool(tool_name, args, **kw):
            CALLS.append(("pre", tool_name))
            if tool_name == "write":
                return {"action": "block", "message": "writes are frozen by demo-plugin"}

        ctx.register_hook("pre_tool_call", pre_tool)
        ctx.register_hook("post_tool_call", lambda tool_name, result, **kw: CALLS.append(("post", tool_name, result[:20])))
        ctx.register_hook("transform_tool_result", lambda tool_name, result, **kw: result.replace("alpha", "ALPHA") if tool_name == "read" else None)
        ctx.register_hook("transform_terminal_output", lambda command, output, **kw: output + "[seen]")
        ctx.register_hook("pre_llm_call", lambda user_message, is_first_turn, **kw: {"context": f"ctx-for:{user_message}:{is_first_turn}"})
        ctx.register_hook("post_llm_call", lambda assistant_response, **kw: CALLS.append(("post_llm", assistant_response)))
        ctx.register_hook("on_session_start", lambda **kw: CALLS.append(("start", kw.get("platform"))))
        ctx.register_hook("on_session_end", lambda **kw: CALLS.append(("end",)))
        ctx.register_hook("on_stream_delta", lambda **kw: None)          # not mapped: recorded, not fatal
        ctx.register_command("demo-status", lambda raw: f"status:{raw}", description="Demo status")
        ctx.register_system_prompt_section("rules", lambda info: "DEMO-PLUGIN-RULE cwd=" + str(bool(info["cwd"])))
        ctx.register_platform("telegram", object())                      # a kind without a slot here
        ctx.set_config("seen", ctx.get_config("seen", 0) + 1)
''')


class TestHermesHost(CapCase):
    def make(self, **kw):
        plug = self.dir / "hplugins" / "demo-plugin"
        plug.mkdir(parents=True)
        (plug / "plugin.yaml").write_text("name: demo-plugin\nversion: 1.2.0\ndescription: Demo\n", encoding="utf-8")
        (plug / "__init__.py").write_text(PLUGIN, encoding="utf-8")
        bad = self.dir / "hplugins" / "bad-plugin"
        bad.mkdir()
        (bad / "plugin.yaml").write_text("name: bad-plugin\n", encoding="utf-8")
        (bad / "__init__.py").write_text("raise RuntimeError('boom at import')\n", encoding="utf-8")
        prof = {"name": "hp", "extends": "lean", "capabilities": ["hermes-plugins"],
                "hermes_plugin_dirs": [str(self.dir / "hplugins")], "hermes_default_dirs": False}
        (self.profiles / "hp.json").write_text(json.dumps(prof))
        return self.session(profile="hp", **kw)

    def calls(self, s):
        mod = next(m for n, m in sys.modules.items() if n.startswith("omni_hermes_plugin_demo_plugin") and hasattr(m, "CALLS"))
        return mod.CALLS

    def test_tools_hooks_commands_and_prompt_section(self):
        s = self.make()
        host = hermes_plugins.host_of(s)
        demo = next(p for p in host.plugins if p["name"] == "demo-plugin")
        self.assertTrue(demo["ok"], demo)
        self.assertEqual(sorted(demo["tools"]), ["broken", "legacy", "shout"])
        self.assertIn("tool needs_key (needs OMNI_TEST_MISSING_KEY)", demo["unsupported"])
        self.assertIn("hook on_stream_delta", demo["unsupported"])
        self.assertIn("platform", demo["unsupported"])
        bad = next(p for p in host.plugins if p["name"] == "bad-plugin")
        self.assertIn("boom at import", bad["error"])
        self.assertTrue(any("hermes plugin bad-plugin" in d for d in s.diagnostics))
        self.assertIn("shout", s.active_tool_names)
        self.assertIn("DEMO-PLUGIN-RULE cwd=True", s.system_prompt())
        self.assertEqual(json.loads(self.text(self.call(s, "shout", {"text": "hi"}))), {"result": "HI", "task": True})
        self.assertEqual(self.text(self.call(s, "legacy", {})), "legacy ok")
        self.assertTrue(self.call(s, "broken", {}).get("isError"))

        Faux.queue = [{"tool": "write", "args": {"path": "z.txt", "content": "x"}},
                      {"tool": "read", "args": {"path": "a.txt"}},
                      {"tool": "bash", "args": {"command": "echo hello"}},
                      {"text": "all done"}]
        s.prompt("go")
        msgs = [e["message"] for e in s.sessions.entries if e["type"] == "message"]
        self.assertTrue(msgs[0]["content"].endswith("ctx-for:go:True"))                 # pre_llm_call context
        results = [m["content"][0]["text"] for m in msgs if m["role"] == "toolResult"]
        self.assertEqual(results[0], "writes are frozen by demo-plugin")                 # pre_tool_call veto
        self.assertFalse((self.dir / "z.txt").exists())
        self.assertTrue(results[1].startswith("ALPHA"))                                  # transform_tool_result
        self.assertIn("hello", results[2])
        self.assertTrue(results[2].rstrip().endswith("[seen]"))                          # transform_terminal_output
        calls = self.calls(s)
        self.assertIn(("start", "cli"), calls)
        self.assertIn(("pre", "read"), calls)
        self.assertTrue(any(c[0] == "post" and c[1] == "bash" for c in calls))
        self.assertIn(("post_llm", "all done"), calls)

        port = Port()
        self.assertTrue(Commands(s, port).run("demo-status", "now"))
        self.assertIn(("info", "status:now"), port.notes)
        self.assertEqual(json.loads((self.agent / "hermes-plugins.json").read_text())["demo-plugin"]["seen"], 1)
        s.shutdown()
        self.assertIn(("end",), calls)

    def test_bundled_plugin_loads_and_doctor_reports(self):
        prof = {"name": "hb", "extends": "lean", "capabilities": ["hermes-plugins", "hooks", "todo"]}
        (self.profiles / "hb.json").write_text(json.dumps(prof))
        s = self.session(profile="hb")
        if (ROOT / "plugins" / "hermes" / "crypto_helper").is_dir():
            out = self.text(self.call(s, "sha256_hash", {"text": "abc"}))
            self.assertEqual(out, "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad")
        lines = "\n".join(Commands(s, Port()).doctor_lines())
        self.assertIn("Profile hb", lines)
        self.assertIn("Hermes plugins:", lines)
        self.assertIn("Shell hooks: none configured", lines)
        self.assertIn("Modules (", lines)
        self.assertIn("Extensions: off", lines)


class TestShellHooks(CapCase):
    def hook(self, name, body):
        f = self.dir / f"{name}.py"
        f.write_text(textwrap.dedent(body), encoding="utf-8")
        return f'"{sys.executable}" "{f}"'

    def test_pre_post_prompt_and_session_hooks(self):
        guard = self.hook("guard", '''
            import json, sys
            d = json.load(sys.stdin)
            open("hook.log", "a").write(d["hook_event_name"] + ":" + d.get("tool_name", "") + "\\n")
            if "secret" in json.dumps(d["tool_input"]):
                sys.stderr.write("never touch secrets"); sys.exit(2)
        ''')
        lint = self.hook("lint", '''
            import json, sys
            d = json.load(sys.stdin)
            assert d["tool_input"]["file_path"] == d["tool_input"]["path"]
            print(json.dumps({"decision": "block", "reason": "lint: line too long in " + d["tool_input"]["path"]}))
        ''')
        ctx = self.hook("ctx", '''
            import json, sys
            d = json.load(sys.stdin)
            if "forbidden" in d["prompt"]:
                sys.stderr.write("that topic is off limits"); sys.exit(2)
            print("branch: main")
        ''')
        start = self.hook("start", 'print("TEAM-RULES: be brief")')
        stop = self.hook("stop", 'open("stopped.flag", "w").write("1")')
        (self.agent / "hooks.json").write_text(json.dumps({"hooks": {
            "PreToolUse": [{"matcher": "Bash|write", "hooks": [{"type": "command", "command": guard}]}],
            "PostToolUse": [{"matcher": "Write", "hooks": [{"type": "command", "command": lint}]}],
            "UserPromptSubmit": [{"hooks": [{"type": "command", "command": ctx}]}],
            "SessionStart": [{"hooks": [{"type": "command", "command": start}]}],
            "Stop": [{"hooks": [{"type": "command", "command": stop}]}]}}))
        s = self.session(capabilities=["hooks"], tools="read,bash,edit,write,grep,find,ls")
        self.assertIn("TEAM-RULES: be brief", s.system_prompt())
        Faux.queue = [{"tool": "bash", "args": {"command": "cat secret.txt"}},
                      {"tool": "read", "args": {"path": "a.txt"}},
                      {"tool": "write", "args": {"path": "ok.txt", "content": "fine"}},
                      {"text": "finished"}]
        s.prompt("do things")
        msgs = [e["message"] for e in s.sessions.entries if e["type"] == "message"]
        self.assertTrue(msgs[0]["content"].endswith("branch: main"))
        results = [m["content"] for m in msgs if m["role"] == "toolResult"]
        self.assertEqual(results[0][0]["text"], "Blocked by a PreToolUse hook: never touch secrets")
        self.assertIn("alpha", results[1][0]["text"])                                   # read is not matched
        self.assertIn("lint: line too long in ok.txt", results[2][-1]["text"])          # feedback appended
        self.assertTrue((self.dir / "ok.txt").exists())
        self.assertEqual((self.dir / "hook.log").read_text().split(), ["PreToolUse:Bash", "PreToolUse:Write"])
        self.assertTrue((self.dir / "stopped.flag").exists())
        n = len(s.sessions.entries)
        notes = []
        s.subscribe(lambda ev: notes.append(ev) if ev.get("type") == "notice" else None)
        s.prompt("something forbidden")
        self.assertEqual(len(s.sessions.entries), n)                                    # blocked before the model saw it
        self.assertIn("that topic is off limits", notes[0]["message"])

    def test_claude_settings_only_when_opted_in(self):
        (self.dir / ".claude").mkdir()
        (self.dir / ".claude" / "settings.json").write_text(json.dumps({"hooks": {"PreToolUse": [
            {"matcher": "*", "hooks": [{"type": "command", "command": self.hook("deny", "import sys; sys.exit(2)")}]}]}}))
        s = self.session(capabilities=["hooks"], approve=True)
        self.assertFalse(self.call_gate(s))
        (self.profiles / "ch.json").write_text(json.dumps({"name": "ch", "extends": "lean", "capabilities": ["hooks"],
                                                             "hooks": {"claude": True}}))
        s2 = self.session(profile="ch", approve=True)
        self.assertTrue(self.call_gate(s2))

    @staticmethod
    def call_gate(s):
        return bool(s.caps.gate("read", {"path": "a.txt"}))


class TestSearch(CapCase):
    DDG = ('<div class="result"><a rel="nofollow" class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Fdocs&amp;rut=x">'
           'Example <b>Docs</b></a><a class="result__snippet" href="x">The <b>official</b> docs &amp; guide</a></div>'
           '<div class="result"><a class="result__a" href="https://second.example/">Second</a>'
           '<a class="result__snippet" href="y">Another result</a></div>')

    def test_web_search_backends(self):
        self.assertEqual(search.parse_duckduckgo(self.DDG, 5), [
            {"title": "Example Docs", "url": "https://example.com/docs", "snippet": "The official docs & guide"},
            {"title": "Second", "url": "https://second.example/", "snippet": "Another result"}])
        seen = []

        class H(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _go(self):
                n = int(self.headers.get("Content-Length") or 0)
                seen.append((self.path, self.headers.get("X-Subscription-Token"), json.loads(self.rfile.read(n)) if n else None))
                if self.path.startswith("/brave"):
                    body = {"web": {"results": [{"title": "B", "url": "https://b.example", "description": "<strong>bold</strong> text"}]}}
                elif self.path.startswith("/tavily"):
                    body = {"results": [{"title": "T", "url": "https://t.example", "content": "tavily text"}]}
                else:
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html")
                    self.end_headers()
                    self.wfile.write(TestSearch.DDG.encode())
                    return
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps(body).encode())
            do_GET = do_POST = _go
        srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{srv.server_address[1]}"
        saved, env = dict(search.ENDPOINTS), {k: os.environ.pop(k, None) for k in ("TAVILY_API_KEY", "BRAVE_API_KEY", "EXA_API_KEY", "OMNI_SEARCH_BACKEND")}
        search.ENDPOINTS.update(tavily=base + "/tavily", brave=base + "/brave", duckduckgo=base + "/ddg")
        try:
            s = self.session(capabilities=["web-search"])
            r = self.call(s, "web_search", {"query": "python docs"})                    # no key: DuckDuckGo
            self.assertEqual(r["details"], {"backend": "duckduckgo", "results": 2})
            self.assertTrue(self.text(r).startswith("1. Example Docs\n   https://example.com/docs\n   The official docs & guide"))
            os.environ["BRAVE_API_KEY"] = "bk"
            r = self.call(s, "web_search", {"query": "x", "max_results": 3})
            self.assertEqual((r["details"]["backend"], seen[-1][1]), ("brave", "bk"))
            self.assertIn("bold text", self.text(r))
            os.environ["TAVILY_API_KEY"] = "tk"                                          # first configured wins
            r = self.call(s, "web_search", {"query": "y"})
            self.assertEqual(r["details"]["backend"], "tavily")
            self.assertEqual(seen[-1][2], {"api_key": "tk", "query": "y", "max_results": 6})
            os.environ["OMNI_SEARCH_BACKEND"] = "exa"
            self.assertIn("EXA_API_KEY", self.text(self.call(s, "web_search", {"query": "z"})))
            self.assertTrue(s.all_tools()["web_search"].read_only)
        finally:
            search.ENDPOINTS.clear()
            search.ENDPOINTS.update(saved)
            for k, v in env.items():
                os.environ.pop(k, None)
                if v is not None:
                    os.environ[k] = v
            srv.shutdown()
            srv.server_close()

    def test_session_search_finds_earlier_conversations(self):
        old = self.session(no_session=False)
        Faux.queue = [{"text": "Use the frobnicator flag --turbo to fix the cache bug."}]
        old.prompt("how did we fix the cache bug?")
        Faux.queue = [{"text": "unrelated"}]
        other = self.session(no_session=False)
        other.prompt("what is the weather")
        s = self.session(capabilities=["session-search"], no_session=False)
        r = self.call(s, "session_search", {"query": "frobnicator turbo"})
        t = self.text(r)
        self.assertEqual(r["details"]["sessions"], 1)
        self.assertIn("assistant: Use the frobnicator flag --turbo", t)
        self.assertIn(f"resume: omni --session {old.sessions.session_id}", t)
        self.assertIn("No earlier session mentions", self.text(self.call(s, "session_search", {"query": "zebra unicorn"})))
        port = Port()
        self.assertTrue(Commands(s, port).run("search", "cache bug"))
        self.assertTrue(any("cache bug" in t for _l, t in port.notes))


class TestNamedAgentsAndStartup(CapCase):
    def test_named_agent_prompt_tools_and_errors(self):
        agents = self.agent / "agents"
        agents.mkdir()
        (agents / "reviewer.md").write_text("---\nname: reviewer\ndescription: Reviews diffs for bugs\ntools: Read, Grep, Glob, Bash\n"
                                            "model: inherit\n---\nYou are a strict code reviewer. Report only real bugs.\n", encoding="utf-8")
        s = self.session(capabilities=["task"], tools="read,bash,edit,write,grep,find,ls")
        self.assertIn("reviewer: Reviews diffs for bugs", s.all_tools()["task"].description)
        Faux.queue = [{"tool": "task", "args": {"agent": "reviewer", "prompt": "Review a.txt"}},
                      {"text": "No bugs found."}, {"text": "ok"}]
        s.prompt("review please")
        sub = Faux.requests[1]
        self.assertEqual(sub["tools"], ["read", "grep", "find", "bash"])       # Claude Code names mapped
        self.assertIn("You are a strict code reviewer", sub["systemPrompt"])
        self.assertIn("You are a subagent", sub["systemPrompt"])
        bad = self.call(s, "task", {"agent": "ghost", "prompt": "x"})
        self.assertIn("unknown agent 'ghost'. Available: reviewer", self.text(bad))

    def test_deferred_start_is_usable_at_once_and_prompt_waits(self):
        from src.pi.agent import AgentSession, SessionOptions
        from src.pi.modes import HeadlessUI
        s = AgentSession(SessionOptions(cwd=str(self.dir), model="faux/faux-1", no_extensions=True, mode="print",
                                        agent_dir=str(self.agent), profile="tcoder"), ui=HeadlessUI())
        events = []
        s.subscribe(lambda ev: events.append(ev["type"]))
        s.start(defer_hosts=True)
        self.assertTrue(s.loading)
        self.assertFalse(s.wait_ready(0))
        self.assertEqual(s.model["id"], "faux-1")                    # session, model and tools are there immediately
        self.assertIn("todo", s.active_tool_names)
        self.assertNotIn("session_start", events)
        done = threading.Event()
        threading.Thread(target=lambda: (s.prompt("hello"), done.set()), daemon=True).start()
        time.sleep(0.3)
        self.assertFalse(done.is_set())                              # the message waits for the hosts
        self.assertEqual(Faux.requests, [])
        s.finish_start()
        self.assertTrue(done.wait(10))
        self.assertFalse(s.loading)
        self.assertEqual(events.index("startup_complete") < events.index("agent_start"), True)
        self.assertIn("session_start", events)

    def test_startup_failure_is_reported_not_fatal(self):
        from src.pi.agent import AgentSession, SessionOptions
        from src.pi.modes import HeadlessUI
        s = AgentSession(SessionOptions(cwd=str(self.dir), model="faux/faux-1", no_extensions=True, mode="print",
                                        agent_dir=str(self.agent)), ui=HeadlessUI())
        s.start(defer_hosts=True)
        s._start_mcp = lambda: (_ for _ in ()).throw(RuntimeError("mcp exploded"))
        s.finish_start()
        self.assertTrue(s.wait_ready(0))
        self.assertTrue(any("mcp exploded" in d for d in s.diagnostics))


if __name__ == "__main__":
    unittest.main()
