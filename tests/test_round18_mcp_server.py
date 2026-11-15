"""MCP server mode (`omni mcp serve`): Omni as a tool for other agents, over in-memory pipes. Offline."""
import io
import json
import os
import sys
import threading
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from test_capabilities import CapCase  # noqa: E402
from src.pi import mcp_server  # noqa: E402
from src.pi.ai import Faux  # noqa: E402
from src.pi.caps import plan  # noqa: E402
from src.pi.mcp import PROTOCOL_VERSION  # noqa: E402
from src.pi.tools.base import ToolDef, text_result  # noqa: E402

WAIT = 30


class Client:
    """The other end of the pipes: writes requests, collects every line the server writes."""

    def __init__(self, factory, expose=("agent",), opts="OPTS"):
        c2s_r, self._c2s_w = os.pipe()
        self._s2c_r, s2c_w = os.pipe()
        self._server_in = os.fdopen(c2s_r, "rb")
        self._server_out = os.fdopen(s2c_w, "wb")
        self._to_server = os.fdopen(self._c2s_w, "wb")
        self._from_server = os.fdopen(self._s2c_r, "rb")
        self.raw, self.messages, self.rc = [], [], None
        self._cond = threading.Condition()
        self._ids = 0
        self._reader = threading.Thread(target=self._read, name="test-mcp-client", daemon=True)
        self._reader.start()

        def run():
            self.rc = mcp_server.serve(opts, stdin=self._server_in, stdout=self._server_out, expose=expose,
                                       session_factory=factory)
        self._server = threading.Thread(target=run, name="test-mcp-serve", daemon=True)
        self._server.start()
        self.closed = False

    def _read(self):
        for line in self._from_server:
            with self._cond:
                self.raw.append(line)
                try:
                    self.messages.append(json.loads(line.decode("utf-8")))
                except ValueError:
                    self.messages.append(None)
                self._cond.notify_all()

    def send_raw(self, data: bytes):
        self._to_server.write(data)
        self._to_server.flush()

    def send(self, method, params=None, **extra):
        self._ids += 1
        msg = {"jsonrpc": "2.0", "id": self._ids, "method": method, **extra}
        if params is not None:
            msg["params"] = params
        self.send_raw((json.dumps(msg, ensure_ascii=False) + "\n").encode("utf-8"))
        return self._ids

    def notify(self, method, params=None):
        msg = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            msg["params"] = params
        self.send_raw((json.dumps(msg) + "\n").encode("utf-8"))

    def answered(self, rid):
        with self._cond:
            return any(isinstance(m, dict) and m.get("id") == rid for m in self.messages)

    def wait(self, rid, timeout=WAIT):
        end = time.time() + timeout
        with self._cond:
            while True:
                for m in self.messages:
                    if isinstance(m, dict) and m.get("id") == rid:
                        return m
                left = end - time.time()
                if left <= 0:
                    raise AssertionError(f"no response to request {rid}; got {self.messages}")
                self._cond.wait(left)

    def wait_count(self, n, timeout=WAIT):
        end = time.time() + timeout
        with self._cond:
            while len(self.messages) < n:
                left = end - time.time()
                if left <= 0:
                    raise AssertionError(f"expected {n} lines, got {self.messages}")
                self._cond.wait(left)
            return list(self.messages)

    def request(self, method, params=None):
        return self.wait(self.send(method, params))

    def call(self, name, args=None):
        """tools/call -> the MCP result (asserting it is not a protocol error)."""
        m = self.request("tools/call", {"name": name, "arguments": args or {}})
        assert "result" in m, m
        return m["result"]

    def close(self):
        """EOF on the server's stdin; returns serve()'s exit code."""
        if self.closed:
            return self.rc
        self.closed = True
        self._to_server.close()
        self._server.join(WAIT)
        alive = self._server.is_alive()
        self._server_out.close()          # the reader sees EOF
        self._reader.join(WAIT)
        self._server_in.close()
        self._from_server.close()
        assert not alive, "serve() did not return after EOF"
        return self.rc


def text(result):
    return "\n".join(b["text"] for b in result["content"] if b["type"] == "text")


class McpCase(CapCase):
    def setUp(self):
        super().setUp()
        self._threads = set(threading.enumerate())
        self.clients, self.sessions, self.shutdowns = [], [], []
        self.slow_started, self.slow_release, self.slow_aborted = threading.Event(), threading.Event(), threading.Event()

    def tearDown(self):
        self.slow_release.set()
        for c in self.clients:
            c.close()
            for line, m in zip(c.raw, c.messages):   # nothing but JSON-RPC lines ever reaches stdout
                self.assertTrue(line.endswith(b"\n") and b"\n" not in line[:-1] and b"\r" not in line, line)
                self.assertIsInstance(m, dict, line)
                self.assertEqual(m.get("jsonrpc"), "2.0", line)
                self.assertTrue(("result" in m) != ("error" in m) and "id" in m and "method" not in m, line)
        end = time.time() + 10
        left = []
        while time.time() < end:
            left = [t for t in threading.enumerate() if t not in self._threads and t.is_alive()]
            if not left:
                break
            time.sleep(0.05)
        self.assertEqual(left, [], "threads left behind")
        super().tearDown()

    def factory(self, profile="tcoder", after=None):
        def make(opts):
            s = self.session(profile=profile)

            def slow(_id, p, signal=None, on_update=None, ctx=None):
                self.slow_started.set()
                end = time.time() + WAIT
                while time.time() < end and not self.slow_release.is_set():
                    if signal is not None and signal.aborted:
                        self.slow_aborted.set()
                        return text_result("slow: aborted")
                    time.sleep(0.01)
                return text_result("slow: released")

            def noisy(_id, p, signal=None, on_update=None, ctx=None):
                print("NOISE from a tool")
                sys.stdout.write("more noise\n")
                return {"content": [{"type": "text", "text": "quiet result"},
                                    {"type": "image", "data": "aGk=", "mimeType": "image/png"},
                                    {"type": "image", "path": "/nowhere.png"}], "details": None}

            for name, fn in (("slow", slow), ("noisy", noisy)):
                s.builtin_tools[name] = ToolDef(name, name, f"{name} test tool", {"type": "object", "properties": {}}, fn)
                s.active_tool_names.append(name)
            real_shutdown = s.shutdown
            s.shutdown = lambda reason="quit": (self.shutdowns.append(reason), real_shutdown(reason))[1]
            print("NOISE while the session starts")
            if after:
                after(s)
            self.sessions.append((opts, s))
            return s
        return make

    def client(self, expose=("agent",), **kw):
        c = Client(self.factory(**kw), expose)
        self.clients.append(c)
        return c


class TestProtocol(McpCase):
    def test_handshake_ping_and_errors(self):
        c = self.client()
        r = c.request("initialize", {"protocolVersion": "2024-11-05", "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}})
        self.assertEqual(r["result"]["protocolVersion"], "2024-11-05")       # a version we support is echoed
        self.assertEqual(r["result"]["capabilities"], {"tools": {}})
        self.assertEqual(r["result"]["serverInfo"]["name"], "omni")
        self.assertEqual(c.request("initialize", {"protocolVersion": "1999-01-01"})["result"]["protocolVersion"], PROTOCOL_VERSION)
        self.assertEqual(c.request("initialize", {"protocolVersion": PROTOCOL_VERSION})["result"]["protocolVersion"], PROTOCOL_VERSION)
        c.notify("notifications/initialized")
        c.notify("notifications/whatever", {"x": 1})                          # unknown notifications are ignored
        c.notify("notifications/cancelled", {"requestId": 999})               # nothing to cancel
        self.assertEqual(c.request("ping")["result"], {})
        self.assertEqual(c.request("resources/list")["error"]["code"], -32601)
        self.assertEqual(c.request("tools/call", {"arguments": {}})["error"]["code"], -32602)
        self.assertEqual(c.request("tools/call", {"name": "omni_agent", "arguments": "x"})["error"]["code"], -32602)
        self.assertEqual(c.request("tools/list", ["not", "an", "object"])["error"]["code"], -32602)
        unknown = c.request("tools/call", {"name": "nope", "arguments": {}})["error"]
        self.assertEqual(unknown["code"], -32602)
        self.assertIn("Unknown tool: nope", unknown["message"])
        self.assertEqual(c.request("tools/call", {"name": "omni_read", "arguments": {"path": "a.txt"}})["error"]["code"], -32602)
        n = len(c.messages)
        c.send_raw(b'{"jsonrpc": "2.0", "id": 1, "method": \n')               # malformed line
        c.send_raw(b"\n")                                                     # blank lines are skipped
        c.send_raw(b"\xff\xfe{}\n")                                           # not UTF-8
        c.send_raw(b"42\n")                                                   # JSON, but not a request
        got = c.wait_count(n + 3)[n:]
        self.assertEqual([(m["id"], m["error"]["code"]) for m in got], [(None, -32700), (None, -32700), (None, -32600)])
        self.assertEqual(c.request("ping")["result"], {})                     # and the server is still there
        self.assertEqual(self.sessions, [])                                   # none of this needed a session
        self.assertEqual(c.close(), 0)
        self.assertEqual(self.shutdowns, [])

    def test_tools_list_in_both_modes(self):
        c = self.client()
        tools = c.request("tools/list")["result"]["tools"]
        self.assertEqual([t["name"] for t in tools], ["omni_agent", "omni_status"])
        agent = tools[0]["inputSchema"]
        self.assertEqual((agent["type"], agent["required"], agent["properties"]["prompt"]["type"],
                          agent["properties"]["new_session"]["type"]), ("object", ["prompt"], "string", "boolean"))
        self.assertEqual(self.sessions, [])                                   # the session is created lazily

        c2 = self.client(expose=("agent", "tools"))
        tools = {t["name"]: t for t in c2.request("tools/list")["result"]["tools"]}
        s = self.sessions[0][1]
        self.assertEqual(self.sessions[0][0], "OPTS")                         # the factory got serve()'s opts
        self.assertEqual(list(tools), ["omni_agent", "omni_status"] + ["omni_" + n for n in s.active_tool_names])
        self.assertTrue({"omni_read", "omni_bash", "omni_edit", "omni_write", "omni_todo", "omni_fetch"} <= set(tools))
        for name in ("read", "bash", "edit", "write", "todo"):
            self.assertEqual(tools["omni_" + name]["inputSchema"], s.all_tools()[name].parameters)
            self.assertTrue(tools["omni_" + name]["description"])
        self.assertEqual(tools["omni_edit"]["inputSchema"]["required"], ["path", "edits"])

        c3 = self.client(expose=("tools",))                                   # tools only: no agent tool
        names = [t["name"] for t in c3.request("tools/list")["result"]["tools"]]
        self.assertNotIn("omni_agent", names)
        self.assertIn("omni_read", names)
        self.assertEqual(c3.request("tools/call", {"name": "omni_agent", "arguments": {"prompt": "x"}})["error"]["code"], -32602)


class TestAgentTool(McpCase):
    def test_agent_call_continuity_new_session_and_status(self):
        c = self.client()
        Faux.queue = [{"text": "hello from omni"}]
        r = c.call("omni_agent", {"prompt": "first question"})
        self.assertEqual((text(r), r.get("isError")), ("hello from omni", None))
        self.assertEqual(len(self.sessions), 1)

        Faux.queue = [{"tool": "read", "args": {"path": "a.txt"}}, {"text": "it has three lines"}]
        r = c.call("omni_agent", {"prompt": "second question"})               # a full turn, with a tool call inside
        self.assertEqual(text(r), "it has three lines")
        seen = json.dumps(Faux.requests[-1]["messages"])
        self.assertIn("first question", seen)                                 # the conversation continued
        self.assertIn("hello from omni", seen)
        self.assertIn("alpha", seen)
        self.assertEqual(len(self.sessions), 1)

        st = json.loads(text(c.call("omni_status")))
        s = self.sessions[0][1]
        self.assertEqual((st["model"], st["profile"], st["cwd"], st["busy"]), ("faux/faux-1", "tcoder", s.cwd, False))
        self.assertTrue({"todo", "task", "ask", "fetch"} <= set(st["capabilities"]))
        self.assertGreater(st["contextUsage"]["tokens"], 0)

        r = c.call("omni_agent", {"prompt": "third question", "new_session": True})
        self.assertEqual(text(r), "faux: third question")
        seen = json.dumps(Faux.requests[-1]["messages"])
        self.assertNotIn("first question", seen)                              # a fresh conversation
        self.assertNotIn("second question", seen)
        self.assertEqual(len(self.sessions), 1)

        self.assertTrue(c.call("omni_agent", {}).get("isError"))              # bad arguments: a tool error, not a protocol error
        Faux.queue = [{"error": "model exploded", "status": 400}]         # not retried
        r = c.call("omni_agent", {"prompt": "fail please"})
        self.assertTrue(r.get("isError"))
        self.assertIn("model exploded", text(r))
        self.assertEqual(c.close(), 0)
        self.assertEqual(self.shutdowns, ["quit"])                            # EOF shuts the session down

    def test_status_before_any_call_creates_the_session(self):
        c = self.client()
        st = json.loads(text(c.call("omni_status")))
        self.assertEqual((st["profile"], st["expose"]), ("tcoder", ["agent"]))
        self.assertEqual(len(self.sessions), 1)

    def test_ping_and_list_answer_while_a_call_runs_and_calls_queue(self):
        c = self.client(expose=("agent", "tools"))
        Faux.queue = [{"tool": "slow", "args": {}}, {"text": "slow turn finished"}, {"text": "second turn"}]
        first = c.send("tools/call", {"name": "omni_agent", "arguments": {"prompt": "take your time"}})
        self.assertTrue(self.slow_started.wait(WAIT))
        self.assertEqual(c.request("ping")["result"], {})
        self.assertIn("omni_slow", [t["name"] for t in c.request("tools/list")["result"]["tools"]])
        self.assertTrue(json.loads(text(c.call("omni_status")))["busy"])
        second = c.send("tools/call", {"name": "omni_agent", "arguments": {"prompt": "next"}})
        self.assertEqual(c.request("ping")["result"], {})
        self.assertFalse(c.answered(first) or c.answered(second))             # the second call waits for the first
        self.slow_release.set()
        self.assertEqual(text(c.wait(first)["result"]), "slow turn finished")
        self.assertEqual(text(c.wait(second)["result"]), "second turn")
        ids = [m["id"] for m in c.messages]
        self.assertLess(ids.index(first), ids.index(second))

    def test_cancellation_aborts_the_turn_and_sends_no_response(self):
        c = self.client()
        Faux.queue = [{"tool": "slow", "args": {}}, {"text": "never said"}]
        running = c.send("tools/call", {"name": "omni_agent", "arguments": {"prompt": "long job"}})
        self.assertTrue(self.slow_started.wait(WAIT))
        queued = c.send("tools/call", {"name": "omni_agent", "arguments": {"prompt": "queued job"}})
        c.notify("notifications/cancelled", {"requestId": queued, "reason": "changed my mind"})
        c.notify("notifications/cancelled", {"requestId": running, "reason": "too slow"})
        self.assertTrue(self.slow_aborted.wait(WAIT))                         # session.abort() reached the tool
        Faux.queue = []
        self.assertEqual(text(c.call("omni_agent", {"prompt": "still there?"})), "faux: still there?")
        self.assertEqual(c.close(), 0)
        self.assertFalse(c.answered(running) or c.answered(queued))           # cancelled requests are not answered
        users = [e["message"]["content"] for e in self.sessions[0][1].sessions.entries
                 if e["type"] == "message" and e["message"]["role"] == "user"]
        self.assertEqual(users, ["long job", "still there?"])                 # the queued one never ran

    def test_eof_lets_an_accepted_call_finish_then_exits(self):
        c = self.client()
        Faux.queue = [{"tool": "slow", "args": {}}, {"text": "finished after EOF"}]
        rid = c.send("tools/call", {"name": "omni_agent", "arguments": {"prompt": "go"}})
        self.assertTrue(self.slow_started.wait(WAIT))
        threading.Timer(0.2, self.slow_release.set).start()
        self.assertEqual(c.close(), 0)
        self.assertEqual(text(c.wait(rid, 1)["result"]), "finished after EOF")
        self.assertEqual(self.shutdowns, ["quit"])
        self.assertFalse(any(t.name == "omni-mcp-worker" for t in threading.enumerate()))


class TestDirectTools(McpCase):
    def test_direct_read_write_and_tool_errors(self):
        c = self.client(expose=("agent", "tools"))
        (self.dir / "note.txt").write_text("first line\nsecond line\n", encoding="utf-8")
        r = c.call("omni_read", {"path": str(self.dir / "note.txt")})
        self.assertIsNone(r.get("isError"))
        self.assertIn("first line\nsecond line", text(r))
        self.assertIn("alpha", text(c.call("omni_read", {"path": "a.txt"})))  # relative to the session's cwd
        r = c.call("omni_write", {"path": "made.txt", "content": "héllo ✓"})
        self.assertIsNone(r.get("isError"))
        self.assertEqual((self.dir / "made.txt").read_text(encoding="utf-8"), "héllo ✓")
        self.assertIn("héllo ✓", text(c.call("omni_read", {"path": "made.txt"})))   # non-ASCII survives the pipe
        missing = c.call("omni_read", {"path": "does-not-exist.txt"})        # a failed call: isError, not a protocol error
        self.assertTrue(missing["isError"])
        self.assertTrue(text(missing))
        invalid = c.call("omni_read", {})
        self.assertTrue(invalid["isError"])
        self.assertIn("path: missing", text(invalid))
        gone = c.request("tools/call", {"name": "omni_nope", "arguments": {}})
        self.assertEqual(gone["error"]["code"], -32602)
        self.assertEqual(Faux.requests, [])                                   # no model was involved

    def test_gate_refuses_with_is_error(self):
        (self.profiles / "planner.json").write_text(json.dumps(
            {"name": "planner", "extends": "lean", "capabilities": ["plan", "todo"]}))
        c = self.client(expose=("tools",), profile="planner", after=lambda s: plan.set_mode(s, True))
        r = c.call("omni_write", {"path": "blocked.txt", "content": "x"})
        self.assertTrue(r["isError"])
        self.assertIn("Plan mode", text(r))
        self.assertFalse((self.dir / "blocked.txt").exists())
        r = c.call("omni_bash", {"command": "echo hi > blocked.txt"})
        self.assertTrue(r["isError"])
        self.assertFalse((self.dir / "blocked.txt").exists())
        self.assertIn("alpha", text(c.call("omni_read", {"path": "a.txt"})))  # read-only tools still work
        plan.set_mode(self.sessions[0][1], False)
        self.assertIsNone(c.call("omni_write", {"path": "blocked.txt", "content": "x"}).get("isError"))
        self.assertTrue((self.dir / "blocked.txt").exists())

    def test_prints_go_to_stderr_and_images_convert(self):
        err, real = io.StringIO(), sys.stderr
        sys.stderr = err
        try:
            c = self.client(expose=("agent", "tools"))
            r = c.call("omni_noisy")
            Faux.queue = [{"tool": "noisy", "args": {}}, {"text": "done"}]
            self.assertEqual(text(c.call("omni_agent", {"prompt": "make noise"})), "done")
            self.assertEqual(c.close(), 0)
        finally:
            sys.stderr = real
        self.assertEqual(r["content"][0], {"type": "text", "text": "quiet result"})
        self.assertEqual(r["content"][1], {"type": "image", "data": "aGk=", "mimeType": "image/png"})
        self.assertEqual(r["content"][2]["type"], "text")                     # an image without data becomes a note
        self.assertIn("image omitted", r["content"][2]["text"])
        self.assertIn("NOISE while the session starts", err.getvalue())
        self.assertEqual(err.getvalue().count("NOISE from a tool"), 2)
        self.assertTrue(all(b"NOISE" not in line and b"noise" not in line for line in c.raw))

    def test_content_conversion(self):
        conv = mcp_server.mcp_content
        self.assertEqual(conv([{"type": "text", "text": "a"}]), [{"type": "text", "text": "a"}])
        self.assertEqual(conv([{"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": "QQ=="}}]),
                         [{"type": "image", "data": "QQ==", "mimeType": "image/jpeg"}])
        self.assertEqual(conv([{"type": "image", "data": "QQ=="}])[0]["type"], "text")
        self.assertEqual(conv([{"type": "audio", "data": "QQ=="}]), [{"type": "text", "text": "[audio content omitted]"}])
        self.assertEqual(conv(None), [{"type": "text", "text": ""}])
        self.assertEqual(conv("plain"), [{"type": "text", "text": "plain"}])


class TestCli(McpCase):
    def test_print_config(self):
        out = io.StringIO()
        self.assertEqual(mcp_server.cli(["serve", "--print-config"], lambda *a: self.fail("no session needed"), stdout=out), 0)
        cfg = json.loads(out.getvalue())
        self.assertEqual(list(cfg), ["mcpServers"])
        omni = cfg["mcpServers"]["omni"]
        self.assertEqual(omni["command"], sys.executable)
        self.assertEqual(omni["args"][1:], ["mcp", "serve"])
        main = Path(omni["args"][0])
        self.assertTrue(main.is_absolute() and main.is_file() and main.name == "omni_main.py")
        out2 = io.StringIO()
        real, sys.stdout = sys.stdout, out2
        try:
            mcp_server.print_config()
        finally:
            sys.stdout = real
        self.assertEqual(out2.getvalue(), out.getvalue())

    def test_cli_builds_options_and_serves_text_streams(self):
        made = []

        def make_opts(profile, model, cwd):
            made.append((profile, model, cwd))
            return {"marker": 1}

        lines = [{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": PROTOCOL_VERSION}},
                 {"jsonrpc": "2.0", "method": "notifications/initialized"},
                 [{"jsonrpc": "2.0", "id": 2, "method": "tools/list"}, {"jsonrpc": "2.0", "id": 3, "method": "ping"}],
                 {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "omni_agent", "arguments": {"prompt": "hi"}}},
                 {"jsonrpc": "2.0", "id": 5, "method": "tools/call", "params": {"name": "omni_read", "arguments": {"path": "a.txt"}}}]
        stdin = io.StringIO("".join(json.dumps(l) + "\n" for l in lines))
        stdout = io.StringIO()
        before = sys.stdout
        rc = mcp_server.cli(["serve", "--expose", "agent,tools", "--profile", "tcoder", "--model=faux/faux-1", "--cwd", str(self.dir)],
                            make_opts, session_factory=self.factory(), stdin=stdin, stdout=stdout)
        self.assertEqual(rc, 0)
        self.assertIs(sys.stdout, before)                                     # restored after serving
        self.assertEqual(made, [("tcoder", "faux/faux-1", os.path.abspath(str(self.dir)))])
        self.assertEqual(self.sessions[0][0], {"marker": 1})
        out = [json.loads(l) for l in stdout.getvalue().splitlines()]         # every line is JSON-RPC, EOF drained the calls
        self.assertEqual([m["id"] for m in out], [1, 2, 3, 4, 5])
        self.assertTrue(all(m["jsonrpc"] == "2.0" for m in out))
        self.assertIn("omni_bash", [t["name"] for t in out[1]["result"]["tools"]])
        self.assertEqual(text(out[3]["result"]), "faux: hi")
        self.assertIn("alpha", text(out[4]["result"]))
        self.assertEqual(self.shutdowns, ["quit"])

        made.clear()
        self.assertEqual(mcp_server.cli(["serve"], make_opts, session_factory=self.factory(), stdin=io.StringIO(""), stdout=io.StringIO()), 0)
        self.assertEqual(made, [(None, None, os.getcwd())])                   # defaults
        err, real = io.StringIO(), sys.stderr
        sys.stderr = err
        try:
            self.assertEqual(mcp_server.cli(["serve", "--expose", "everything"], make_opts), 2)
            self.assertEqual(mcp_server.cli(["serve", "--bogus"], make_opts), 2)
            self.assertEqual(mcp_server.cli(["serve", "--profile"], make_opts), 2)
            self.assertEqual(mcp_server.cli(["serve", "--cwd", str(self.dir / "missing")], make_opts), 2)
            self.assertEqual(mcp_server.cli(["serve", "--help"], make_opts), 0)
        finally:
            sys.stderr = real
        self.assertIn("usage: omni mcp serve", err.getvalue())
        self.assertEqual(len(made), 1)


if __name__ == "__main__":
    unittest.main()
