"""Infrastructure capabilities modelled on other open-source projects: tracing (Langfuse), archival memory (Letta),
durable runs (Temporal), sandboxed shell (Daytona) and indexed browser control (Browser Use). Offline."""
import base64
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
from src.pi import caps as capmod  # noqa: E402
from src.pi.ai import Faux  # noqa: E402
from src.pi.caps import archive, browser, durable, sandbox, tracing  # noqa: E402
from src.pi.commands import Commands  # noqa: E402
from src.pi.tools.base import ToolDef, ToolError, text_result  # noqa: E402


class TestTracing(CapCase):
    def test_trace_file_command_and_langfuse_export(self):
        got = []

        class H(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                n = int(self.headers.get("Content-Length") or 0)
                got.append((self.path, self.headers.get("Authorization"), json.loads(self.rfile.read(n))))
                self.send_response(207)
                self.end_headers()
                self.wfile.write(b"{}")
        srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        env = {k: os.environ.get(k) for k in ("LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY", "LANGFUSE_HOST")}
        os.environ.update(LANGFUSE_PUBLIC_KEY="pk-lf-1", LANGFUSE_SECRET_KEY="sk-lf-2", LANGFUSE_HOST=f"http://127.0.0.1:{srv.server_address[1]}")
        try:
            s = self.session(capabilities=["tracing"], no_session=False)
            Faux.queue = [{"tool": "read", "args": {"path": "a.txt"}}, {"text": "The file has three lines."}]
            s.prompt("how long is a.txt?")
            tr = tracing.tracer_of(s).last
            self.assertEqual([o["type"] for o in tr["observations"]], ["generation", "span", "generation"])
            self.assertEqual(tr["observations"][1]["name"], "tool:read")
            self.assertEqual(tr["observations"][0]["toolCalls"], ["read"])
            self.assertEqual((tr["totals"]["generations"], tr["totals"]["tools"], tr["outcome"]), (2, 1, "completed"))
            self.assertEqual((tr["input"], tr["output"]), ("how long is a.txt?", "The file has three lines."))
            self.assertGreater(tr["totals"]["input"], 0)
            line = json.loads((self.agent / "traces" / f"{s.sessions.session_id}.jsonl").read_text(encoding="utf-8").splitlines()[0])
            self.assertEqual(line["id"], tr["id"])
            port = Port()
            self.assertTrue(Commands(s, port).run("trace", ""))
            out = port.notes[-1][1]
            self.assertIn("2 model call(s) · 1 tool call(s)", out)
            self.assertIn("tool:read", out)
            self.assertIn("Langfuse export on", out)
            deadline = time.time() + 5
            while not got and time.time() < deadline:
                time.sleep(0.05)
            path, auth, body = got[0]
            self.assertEqual(path, "/api/public/ingestion")
            self.assertEqual(base64.b64decode(auth.split()[1]).decode(), "pk-lf-1:sk-lf-2")
            kinds = [e["type"] for e in body["batch"]]
            self.assertEqual(kinds, ["trace-create", "generation-create", "span-create", "generation-create"])
            gen = body["batch"][1]["body"]
            self.assertEqual((gen["traceId"], gen["model"], gen["usage"]["unit"]), (tr["id"], "faux-1", "TOKENS"))
            self.assertEqual(body["batch"][0]["body"]["sessionId"], s.sessions.session_id)
            self.assertRegex(body["batch"][0]["timestamp"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z$")
        finally:
            for k, v in env.items():
                os.environ.pop(k, None)
                if v is not None:
                    os.environ[k] = v
            srv.shutdown()
            srv.server_close()

    def test_content_can_be_left_out_and_export_failure_is_quiet(self):
        (self.profiles / "nc.json").write_text(json.dumps({"name": "nc", "extends": "lean", "capabilities": ["tracing"],
                                                             "tracing": {"content": False}}))
        s = self.session(profile="nc")
        Faux.queue = [{"text": "secret answer"}]
        s.prompt("secret question")
        tr = tracing.tracer_of(s).last
        self.assertEqual(tr["input"], {"chars": 15})
        self.assertNotIn("secret", json.dumps(tr))
        os.environ.update(LANGFUSE_PUBLIC_KEY="a", LANGFUSE_SECRET_KEY="b", LANGFUSE_HOST="http://127.0.0.1:9")
        try:
            self.assertFalse(tracing.tracer_of(s).export(tr))
            self.assertFalse(tracing.tracer_of(s).export(tr))
        finally:
            for k in ("LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY", "LANGFUSE_HOST"):
                os.environ.pop(k, None)
        self.assertEqual(sum("Langfuse export failed" in d for d in s.diagnostics), 1)


class TestArchivalMemory(CapCase):
    def test_insert_search_tags_and_prompt_line(self):
        s = self.session(capabilities=["memory-archive"])
        self.assertEqual(s.caps.prompt_section(), "")                 # empty archive costs nothing
        r = self.call(s, "archival_memory_insert", {"content": "We chose PostgreSQL over SQLite for the billing service because of row-level locking.",
                                                    "tags": ["Decision", "billing"]})
        self.assertIn("Archived as passage 1", self.text(r))
        self.call(s, "archival_memory_insert", {"content": "The staging database password rotates every Monday at 09:00 UTC.", "tags": ["ops"]})
        self.call(s, "archival_memory_insert", {"content": "Billing invoices are generated by the nightly cron job invoice-run.", "tags": ["billing"]})
        dup = self.call(s, "archival_memory_insert", {"content": "The staging database password rotates every Monday at 09:00 UTC."})
        self.assertIn("Already archived as passage 2", self.text(dup))
        hit = self.text(self.call(s, "archival_memory_search", {"query": "why PostgreSQL billing"}))
        self.assertTrue(hit.startswith("[1] "), hit)                  # best match first (both words)
        self.assertIn("#decision #billing", hit)
        self.assertIn("row-level locking", hit)
        only = self.text(self.call(s, "archival_memory_search", {"query": "billing", "tag": "ops"}))
        self.assertIn("Nothing in archival memory", only)
        tagged = self.text(self.call(s, "archival_memory_search", {"query": "billing database", "tag": "billing", "limit": 5}))
        self.assertNotIn("password", tagged)
        self.assertIn("Nothing in archival memory matches", self.text(self.call(s, "archival_memory_search", {"query": "kubernetes"})))
        self.assertTrue(self.call(s, "archival_memory_insert", {"content": "x" * 9000}).get("isError"))
        self.assertTrue(s.all_tools()["archival_memory_search"].read_only)
        s2 = self.session(capabilities=["memory-archive"])
        self.assertIn("Archival memory holds 3 passage(s)", s2.system_prompt())
        self.assertEqual(archive.count(s2), 3)


class TestDurableRuns(CapCase):
    def crashed_session(self):
        """A session file as a killed process would leave it: a run started, one of two tool calls answered."""
        s = self.session(no_session=False)
        now = int(time.time() * 1000)
        sm = s.sessions
        sm.append_message({"role": "user", "content": "update the config", "timestamp": now})
        sm.append_custom("durable-run", {"state": "started"})
        sm.append_message({"role": "assistant", "provider": "faux", "model": "faux-1", "stopReason": "toolUse", "timestamp": now,
                           "usage": {"input": 1, "output": 1, "cacheRead": 0, "cacheWrite": 0, "totalTokens": 2},
                           "content": [{"type": "toolCall", "id": "c1", "name": "read", "arguments": {"path": "a.txt"}},
                                       {"type": "toolCall", "id": "c2", "name": "write", "arguments": {"path": "cfg.txt", "content": "v2"}}]})
        sm.append_message({"role": "toolResult", "toolCallId": "c1", "toolName": "read", "content": [{"type": "text", "text": "alpha"}],
                           "isError": False, "timestamp": now})
        return str(sm.file)

    def test_interrupted_run_is_detected_and_resumed_without_repeating_work(self):
        path = self.crashed_session()
        s = self.session(session=path, capabilities=["durable"], no_session=False)
        self.assertEqual(durable.interrupted(s), "1 tool call(s) had no result: write")
        self.assertTrue(any("was interrupted" in d and "/resume-run" in d for d in s.diagnostics))
        Faux.queue = [{"text": "The write may not have happened; I checked and redid it."}]
        port = Port()
        self.assertTrue(Commands(s, port).run("resume-run", ""))
        self.assertIn("Resumed the interrupted run", port.notes[-1][1])
        msgs = [e["message"] for e in s.sessions.get_branch() if e["type"] == "message"]
        self.assertEqual([m["role"] for m in msgs], ["user", "assistant", "toolResult", "toolResult", "assistant"])
        self.assertEqual(msgs[3]["toolCallId"], "c2")
        self.assertIn("[interrupted]", msgs[3]["content"][0]["text"])
        self.assertTrue(msgs[3]["isError"])
        self.assertFalse((self.dir / "cfg.txt").exists())            # the unfinished write was NOT replayed
        self.assertEqual(len(Faux.requests), 1)                      # one model call: the continuation
        self.assertEqual([m["role"] for m in Faux.requests[0]["messages"]], ["user", "assistant", "toolResult", "toolResult"])
        self.assertIsNone(durable.interrupted(s))
        self.assertIn("Nothing to resume", durable.resume(s))

    def test_new_message_repairs_the_record_first(self):
        path = self.crashed_session()
        s = self.session(session=path, capabilities=["durable"], no_session=False)
        Faux.queue = [{"text": "ok"}]
        s.prompt("never mind, what time is it?")
        roles = [m["role"] for m in Faux.requests[0]["messages"]]
        self.assertEqual(roles, ["user", "assistant", "toolResult", "toolResult", "user"])   # valid for any provider
        self.assertEqual(durable.last_marker(s)["state"], "ended")

    def test_a_normal_run_is_never_reported(self):
        s = self.session(capabilities=["durable"], no_session=False)
        Faux.queue = [{"tool": "read", "args": {"path": "a.txt"}}, {"text": "done"}]
        s.prompt("read")
        self.assertIsNone(durable.interrupted(s))
        again = self.session(session=str(s.sessions.file), capabilities=["durable"], no_session=False)
        self.assertIsNone(durable.interrupted(again))
        self.assertFalse(any("interrupted" in d for d in again.diagnostics))

    def test_transient_failures_of_read_only_tools_are_retried(self):
        calls = {"flaky": 0, "danger": 0, "hard": 0}

        def tools(_s):
            def flaky(_id, p, signal=None, on_update=None, ctx=None):
                calls["flaky"] += 1
                if calls["flaky"] < 3:
                    raise ToolError("could not fetch: Connection reset by peer")
                return text_result("payload")

            def danger(_id, p, signal=None, on_update=None, ctx=None):
                calls["danger"] += 1
                raise ToolError("HTTP 503 from deploy API")

            def hard(_id, p, signal=None, on_update=None, ctx=None):
                calls["hard"] += 1
                raise ToolError("HTTP 404 not found")
            mk = lambda n, f, ro: ToolDef(name=n, label=n, description=n, parameters={"type": "object", "properties": {}}, execute=f, read_only=ro)  # noqa: E731
            return {"flaky": mk("flaky", flaky, True), "danger": mk("danger", danger, False), "hard": mk("hard", hard, True)}
        capmod.register(capmod.Capability("flaky-tools", "t", "t", tools=tools))
        (self.profiles / "du.json").write_text(json.dumps({"name": "du", "extends": "lean", "capabilities": ["durable", "flaky-tools"],
                                                             "durable": {"retries": 2, "backoff": 0.01}}))
        try:
            s = self.session(profile="du")
            Faux.queue = [{"tools": [{"tool": "flaky"}, {"tool": "danger"}, {"tool": "hard"}]}, {"text": "done"}]
            s.prompt("go")
            res = [e["message"] for e in s.sessions.entries if e["type"] == "message" and e["message"]["role"] == "toolResult"]
            self.assertEqual((res[0]["content"][0]["text"], res[0]["isError"], res[0]["details"]), ("payload", False, {"retries": 2}))
            self.assertEqual(calls, {"flaky": 3, "danger": 1, "hard": 1})   # side effects and permanent errors: one try
            self.assertTrue(res[1]["isError"] and res[2]["isError"])
        finally:
            capmod.unregister("flaky-tools")


FAKE_RUNTIME = r'''
import json, sys
a = sys.argv[1:]
open(sys.argv[0] + ".log", "a").write(json.dumps(a) + "\n")
if a[0] == "run":
    print("container123")
elif a[0] == "exec":
    cmd = a[-1]
    sys.stdout.write("ran: " + cmd + "\n")
    if "fail" in cmd:
        sys.stderr.write("boom\n"); sys.exit(3)
elif a[0] == "rm":
    pass
'''


class TestSandbox(CapCase):
    def setUp(self):
        super().setUp()
        self.fake = self.dir / "fake_runtime.py"
        self.fake.write_text(FAKE_RUNTIME, encoding="utf-8")
        self._old = os.environ.get("OMNI_SANDBOX_RUNTIME")
        os.environ["OMNI_SANDBOX_RUNTIME"] = f'"{sys.executable}" "{self.fake}"'

    def tearDown(self):
        os.environ.pop("OMNI_SANDBOX_RUNTIME", None)
        if self._old is not None:
            os.environ["OMNI_SANDBOX_RUNTIME"] = self._old
        super().tearDown()

    def log(self):
        f = Path(str(self.fake) + ".log")
        return [json.loads(l) for l in f.read_text().splitlines()] if f.exists() else []

    def test_bash_runs_in_a_per_session_container(self):
        (self.profiles / "sb.json").write_text(json.dumps({"name": "sb", "extends": "lean", "capabilities": ["sandbox"],
                                                             "sandbox": {"image": "node:22-slim", "memory": "1g"}}))
        s = self.session(profile="sb")
        tool = s.all_tools()["bash"]
        self.assertEqual(tool.source, "capability:sandbox")
        self.assertIn("isolated Linux container (image node:22-slim)", tool.description)
        self.assertEqual(self.log(), [])                                   # nothing starts until it is needed
        Faux.queue = [{"tool": "bash", "args": {"command": "npm test"}}, {"tool": "bash", "args": {"command": "make fail"}}, {"text": "done"}]
        s.prompt("test it")
        res = [e["message"] for e in s.sessions.entries if e["type"] == "message" and e["message"]["role"] == "toolResult"]
        self.assertEqual(res[0]["content"][0]["text"], "ran: npm test")
        self.assertIn("boom", res[1]["content"][0]["text"])
        self.assertTrue(res[1]["content"][0]["text"].endswith("Command exited with code 3"))
        self.assertTrue(res[1]["isError"])
        log = self.log()
        self.assertEqual([l[0] for l in log], ["run", "exec", "exec"])     # one container, reused
        run = log[0]
        name = run[run.index("--name") + 1]
        self.assertTrue(name.startswith("omni-"))
        self.assertEqual(run[run.index("-v") + 1], f"{s.cwd}:/workspace:rw")
        self.assertEqual(run[run.index("--network") + 1], "none")
        self.assertEqual((run[run.index("--memory") + 1], run[-3:]), ("1g", ["node:22-slim", "sleep", "infinity"]))
        self.assertEqual(log[1], ["exec", "-w", "/workspace", name, "sh", "-lc", "npm test"])
        port = Port()
        Commands(s, port).run("sandbox", "")
        self.assertIn("running", port.notes[-1][1])
        s.shutdown()
        self.assertEqual(self.log()[-1], ["rm", "-f", name])               # removed with the session

    def test_without_a_runtime_the_host_bash_stays(self):
        os.environ["OMNI_SANDBOX_RUNTIME"] = ""
        real = sandbox.shutil.which
        sandbox.shutil.which = lambda _n: None
        try:
            s = self.session(capabilities=["sandbox"])
        finally:
            sandbox.shutil.which = real
        self.assertIn("no container runtime", s.caps.problems["sandbox"])
        self.assertEqual(s.all_tools()["bash"].source, "builtin")


class FakeRelay:
    """A two-page site: a login form, then a dashboard."""

    def __init__(self):
        self.connected, self.url, self.log, self.fields = False, "about:blank", [], {}

    def is_connected(self):
        return self.connected

    def launch(self, url="about:blank", headless=False):
        self.connected = True
        self.log.append(("launch", headless))

    def navigate(self, url):
        self.url = url
        self.log.append(("navigate", url))

    def close(self):
        self.log.append(("close",))

    def evaluate_js(self, expr):
        if expr == "document.readyState":
            return "complete"
        if "data-omni-idx" in expr and "const MAX" in expr:
            if "dashboard" in self.url:
                return {"title": "Dashboard", "url": self.url, "items": [{"i": 1, "tag": "a", "text": "Log out", "attrs": {"href": "/logout"}}],
                        "total": 1, "scrollY": 0, "height": 500, "viewport": 800}
            return {"title": "Sign in", "url": self.url, "total": 140, "scrollY": 0, "height": 2400, "viewport": 800, "items": [
                {"i": 1, "tag": "input", "text": self.fields.get(1, ""), "attrs": {"type": "email", "placeholder": "Email"}},
                {"i": 2, "tag": "input", "text": "", "attrs": {"type": "password"}},
                {"i": 3, "tag": "button", "text": "Sign in", "attrs": {"type": "submit"}}]}
        if "e.click()" in expr:
            idx = int(expr.split('data-omni-idx=\\"')[1].split('\\"')[0]) if '\\"' in expr else int(expr.split('data-omni-idx="')[1].split('"')[0])
            if idx > 3:
                return "missing"
            self.log.append(("click", idx))
            if idx == 3:
                self.url = "https://app.example/dashboard"
            return "ok"
        if "HTMLInputElement" in expr:
            idx = int(expr.split('data-omni-idx="')[1].split('"')[0])
            text = json.loads(expr.split("const text = ")[1].split(", submit = ")[0])
            self.fields[idx] = text
            self.log.append(("type", idx, text, "submit = true" in expr))
            return "ok"
        if "innerText" in expr:
            return "Welcome back. " + "content " * 2000
        self.log.append(("js", expr[:40]))
        return None


class TestBrowser(CapCase):
    def setUp(self):
        super().setUp()
        self.relay = FakeRelay()
        self._make = browser.make_relay
        browser.make_relay = lambda _s: self.relay

    def tearDown(self):
        browser.make_relay = self._make
        super().tearDown()

    def b(self, s, **kw):
        return self.call(s, "browser", kw)

    def test_pages_are_numbered_elements_and_actions_use_the_numbers(self):
        s = self.session(capabilities=["browser"])
        page = self.text(self.b(s, action="open", url="app.example/login"))
        self.assertEqual(page.split("\n")[:4], ["Sign in — https://app.example/login",
                                                 "[1]<input type=email placeholder=Email>",
                                                 "[2]<input type=password>",
                                                 "[3]<button type=submit>Sign in</button>"])
        self.assertIn("… 137 more elements not listed", page)
        self.assertIn("scrolled 33% of the page", page)
        self.assertEqual(self.relay.log[:2], [("launch", False), ("navigate", "https://app.example/login")])
        typed = self.text(self.b(s, action="type", index=1, text='me@example.com "x"'))
        self.assertIn('[1]<input type=email placeholder=Email>me@example.com "x"</input>', typed)
        self.assertIn(("type", 1, 'me@example.com "x"', False), self.relay.log)
        after = self.text(self.b(s, action="click", index=3))
        self.assertTrue(after.startswith("Dashboard — https://app.example/dashboard\n[1]<a href=/logout>Log out</a>"))
        missing = self.b(s, action="click", index=9)
        self.assertTrue(missing.get("isError"))
        self.assertIn("no element [9]", self.text(missing))
        text = self.b(s, action="text")
        self.assertTrue(self.text(text).startswith("Welcome back."))
        self.assertIn("call again with offset=6000", self.text(text))
        self.assertTrue(self.b(s, action="dance").get("isError"))
        self.assertTrue(self.b(s, action="open").get("isError"))
        s.shutdown()
        self.assertIn(("close",), self.relay.log)

    def test_state_formatting(self):
        self.assertEqual(browser.format_state({"title": "", "url": "u", "items": []}), "(untitled) — u\n(no interactive elements visible)")


if __name__ == "__main__":
    unittest.main()
