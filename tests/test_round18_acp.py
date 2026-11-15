"""The ACP server (src/pi/acp_server.py): JSON-RPC over in-memory pipes, sessions on the offline `faux` model."""
import contextlib
import io
import json
import os
import queue
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from test_capabilities import CapCase  # noqa: E402
from src.pi import acp_server as acp  # noqa: E402
from src.pi.ai import Faux  # noqa: E402

WAIT = 20


class Pipe:
    """A blocking line pipe: `write` on one side, `readline` on the other; `close` is EOF."""

    def __init__(self):
        self.q = queue.Queue()
        self.lines = []          # every line ever written, for the purity check

    def write(self, text):
        assert isinstance(text, str), "the server must write text to a text stream"
        self.lines.append(text)
        self.q.put(text)
        return len(text)

    def flush(self):
        pass

    def close(self):
        self.q.put("")

    def readline(self, timeout=None):
        try:
            return self.q.get(timeout=timeout)
        except queue.Empty:
            raise AssertionError("timed out waiting for a line")


class Client:
    def __init__(self, make_session, **kw):
        self.inp, self.out = Pipe(), Pipe()
        self.code = None
        self.next_id = 0
        self.notes = []                       # every notification seen
        self.permission = lambda params: {"outcome": {"outcome": "cancelled"}}
        self.asked = []
        self.thread = threading.Thread(target=self._run, args=(make_session, kw), name="acp-test-server")
        self.thread.start()

    def _run(self, make_session, kw):
        self.code = acp.serve(make_session, self.inp, self.out, **kw)

    def raw(self, line):
        self.inp.write(line + "\n")

    def send(self, method, params=None, notify=False):
        msg = {"jsonrpc": "2.0", "method": method, "params": params or {}}
        if not notify:
            self.next_id += 1
            msg["id"] = self.next_id
        self.raw(json.dumps(msg))
        return msg.get("id")

    def read(self):
        line = self.out.readline(WAIT)
        assert line.endswith("\n") and line.count("\n") == 1, repr(line)
        msg = json.loads(line)
        assert msg.get("jsonrpc") == "2.0", msg
        return msg

    def wait(self, rid):
        """Read until the response to `rid`; notifications are collected, permission requests answered."""
        notes = []
        while True:
            msg = self.read()
            if "method" not in msg:
                if msg.get("id") == rid:
                    return msg, notes
                continue
            if msg["method"] == "session/request_permission":
                self.asked.append(msg["params"])
                answer = self.permission(msg["params"])
                if answer is not None:
                    self.raw(json.dumps({"jsonrpc": "2.0", "id": msg["id"], "result": answer}))
            else:
                notes.append(msg)
                self.notes.append(msg)

    def call(self, method, params=None):
        return self.wait(self.send(method, params))

    def ok(self, method, params=None):
        msg, notes = self.call(method, params)
        assert "result" in msg, msg
        return msg["result"], [n["params"]["update"] for n in notes if n["method"] == "session/update"]

    def stop(self):
        self.inp.close()
        self.thread.join(WAIT)
        assert not self.thread.is_alive(), "the server did not exit on EOF"
        return self.code


def text_of(updates, kind="agent_message_chunk"):
    return "".join(u["content"]["text"] for u in updates if u["sessionUpdate"] == kind)


class AcpCase(CapCase):
    def setUp(self):
        super().setUp()
        self.before = set(threading.enumerate())
        self.made = []
        self.clients = []
        (self.profiles / "tapprove.json").write_text(json.dumps(
            {"name": "tapprove", "extends": "lean", "capabilities": ["approvals"]}))
        self.profile_name = "tcoder"
        self.hook = None

    def tearDown(self):
        for c in self.clients:
            if c.thread.is_alive():
                c.stop()
        deadline = time.time() + 10
        while time.time() < deadline:
            left = [t for t in threading.enumerate() if t not in self.before and t.is_alive()]
            if not left:
                break
            time.sleep(0.05)
        self.assertEqual([t.name for t in left], [], "threads left behind")
        super().tearDown()

    def make(self, cwd, session_path=None):
        s = self.session(profile=self.profile_name, cwd=cwd, session=session_path)
        self.made.append(s)
        if self.hook:
            self.hook(s)
        return s

    def client(self, **kw):
        c = Client(self.make, **kw)
        self.clients.append(c)
        return c

    def new(self, c):
        r, _ = c.ok("session/new", {"cwd": str(self.dir), "mcpServers": []})
        return r["sessionId"]

    def prompt(self, c, sid, text):
        return c.ok("session/prompt", {"sessionId": sid, "prompt": [{"type": "text", "text": text}]})


class TestProtocol(AcpCase):
    def test_initialize_authenticate_new_and_errors(self):
        c = self.client()
        r, _ = c.ok("initialize", {"protocolVersion": 1, "clientCapabilities": {}})
        self.assertEqual(r["protocolVersion"], 1)
        self.assertEqual(r["agentInfo"]["name"], "omni")
        caps = r["agentCapabilities"]
        self.assertIs(caps["loadSession"], True)
        self.assertIs(caps["promptCapabilities"]["embeddedContext"], True)
        self.assertEqual(c.ok("authenticate", {"methodId": "none"})[0], {})
        sid = self.new(c)
        self.assertEqual(sid, self.made[0].sessions.session_id)
        self.assertIsInstance(self.made[0].ui, acp.AcpUI)

        msg, _ = c.call("session/nope", {})
        self.assertEqual(msg["error"]["code"], -32601)
        c.send("something/unknown", notify=True)                      # an unknown notification gets no answer
        c.raw("this is not json")
        self.assertEqual((c.read()["error"]["code"]), -32700)
        c.raw("[1, 2]")
        self.assertEqual(c.read()["error"]["code"], -32600)
        c.raw("")                                                      # blank lines are skipped
        msg, _ = c.call("session/prompt", {"sessionId": "missing", "prompt": [{"type": "text", "text": "x"}]})
        self.assertEqual(msg["error"]["code"], -32002)
        msg, _ = c.call("session/new", {"cwd": str(self.dir / "no-such-dir")})
        self.assertEqual(msg["error"]["code"], -32602)
        msg, _ = c.call("session/prompt", {"sessionId": sid, "prompt": []})
        self.assertEqual(msg["error"]["code"], -32602)
        self.assertEqual(c.ok("initialize", {})[0]["protocolVersion"], 1)   # still alive after all of that
        self.assertEqual(c.stop(), 0)

    def test_prompt_streams_chunks_and_ends_turn(self):
        c = self.client()
        sid = self.new(c)
        answer = "The quick brown fox jumps over the lazy dog, twice."
        Faux.queue = [{"thinking": "let me think", "text": answer}]
        r, ups = self.prompt(c, sid, "hello")
        self.assertEqual(r, {"stopReason": "end_turn"})
        chunks = [u for u in ups if u["sessionUpdate"] == "agent_message_chunk"]
        self.assertGreater(len(chunks), 1)                              # streamed, not one blob
        self.assertEqual(text_of(ups), answer)
        self.assertEqual(text_of(ups, "agent_thought_chunk"), "let me think")
        self.assertTrue(all(n["params"]["sessionId"] == sid for n in c.notes))
        self.assertEqual(Faux.requests[-1]["messages"][-1]["content"], "hello")
        self.assertEqual(c.stop(), 0)

    def test_tool_calls_with_diffs_and_plan(self):
        c = self.client()
        sid = self.new(c)
        Faux.queue = [{"tool": "write", "args": {"path": "new.txt", "content": "hello\n"}, "id": "w1"},
                      {"tool": "edit", "args": {"path": "a.txt", "edits": [{"oldText": "beta", "newText": "BETA"}]}, "id": "e1"},
                      {"tool": "read", "args": {"path": "missing.txt"}, "id": "r1"},
                      {"tool": "bash", "args": {"command": "echo hi"}, "id": "b1"},
                      {"tool": "todo", "args": {"todos": [{"id": "1", "content": "first", "status": "in_progress"},
                                                          {"id": "2", "content": "second"}]}, "id": "t1"},
                      {"text": "all done"}]
        r, ups = self.prompt(c, sid, "do things")
        self.assertEqual(r["stopReason"], "end_turn")
        self.assertEqual((self.dir / "new.txt").read_text(), "hello\n")

        def events(cid):
            return [u for u in ups if u.get("toolCallId") == cid]

        w = events("w1")
        self.assertEqual([(u["sessionUpdate"], u["status"]) for u in w],
                         [("tool_call", "pending"), ("tool_call_update", "in_progress"), ("tool_call_update", "completed")])
        self.assertEqual((w[0]["kind"], w[0]["rawInput"]["path"]), ("edit", "new.txt"))
        self.assertIn("new.txt", w[0]["title"])
        self.assertEqual(w[0]["locations"], [{"path": os.path.join(str(self.dir), "new.txt")}])
        diff = [b for b in w[-1]["content"] if b["type"] == "diff"][0]
        self.assertEqual((os.path.basename(diff["path"]), diff["oldText"], diff["newText"]), ("new.txt", None, "hello\n"))
        self.assertTrue(os.path.isabs(diff["path"]))
        self.assertTrue(any(b["type"] == "content" and b["content"]["type"] == "text" for b in w[-1]["content"]))

        e = events("e1")
        diff = [b for b in e[-1]["content"] if b["type"] == "diff"][0]
        self.assertEqual((diff["oldText"], diff["newText"]), ("alpha\nbeta\ngamma\n", "alpha\nBETA\ngamma\n"))

        r1 = events("r1")
        self.assertEqual((r1[0]["kind"], r1[-1]["status"]), ("read", "failed"))
        self.assertFalse([b for b in r1[-1]["content"] if b["type"] == "diff"])
        b1 = events("b1")
        self.assertEqual((b1[0]["kind"], b1[-1]["status"]), ("execute", "completed"))
        self.assertIn("hi", b1[-1]["content"][0]["content"]["text"])

        plan = [u for u in ups if u["sessionUpdate"] == "plan"]
        self.assertEqual(plan[-1]["entries"], [{"content": "first", "priority": "medium", "status": "in_progress"},
                                               {"content": "second", "priority": "medium", "status": "pending"}])
        order = [u.get("toolCallId") for u in ups if u["sessionUpdate"] == "tool_call"]
        self.assertEqual(order, ["w1", "e1", "r1", "b1", "t1"])
        self.assertEqual(text_of(ups), "all done")
        self.assertEqual(c.stop(), 0)

    def test_prompt_content_blocks(self):
        c = self.client()
        sid = self.new(c)
        link = (self.dir / "src" / "m.py").as_uri()
        blocks = [{"type": "text", "text": "Explain this"},
                  {"type": "resource", "resource": {"uri": "file:///proj/notes.md", "mimeType": "text/markdown",
                                                    "text": "SECRET-NOTES-BODY"}},
                  {"type": "resource_link", "uri": link, "name": "m.py"},
                  {"type": "resource_link", "uri": "https://example.com/doc", "name": "doc"}]
        r, ups = c.ok("session/prompt", {"sessionId": sid, "prompt": blocks})
        self.assertEqual(r["stopReason"], "end_turn")
        sent = Faux.requests[-1]["messages"][-1]["content"]
        self.assertTrue(sent.startswith("Explain this"))
        self.assertIn("[Attached file: notes.md]", sent)
        self.assertIn("SECRET-NOTES-BODY", sent)
        self.assertIn("@src/m.py", sent)
        self.assertIn("https://example.com/doc", sent)
        self.assertIn("SECRET-NOTES-BODY", text_of(ups))                 # faux echoes the prompt back
        self.assertEqual(c.stop(), 0)

    def test_cancel(self):
        started, release = threading.Event(), threading.Event()

        def hook(s):
            def gate(ev):
                if ev.get("type") == "message_update" and not started.is_set():
                    started.set()
                    release.wait(WAIT)
            s.subscribe(gate)

        self.hook = hook
        c = self.client()
        sid = self.new(c)
        Faux.queue = [{"text": "x" * 400}]
        rid = c.send("session/prompt", {"sessionId": sid, "prompt": [{"type": "text", "text": "long one"}]})
        self.assertTrue(started.wait(WAIT))
        c.send("session/cancel", {"sessionId": sid}, notify=True)
        agent = self.made[0]
        deadline = time.time() + WAIT
        while not (agent.signal is not None and agent.signal.aborted) and time.time() < deadline:
            time.sleep(0.01)
        release.set()
        msg, notes = c.wait(rid)
        self.assertEqual(msg["result"], {"stopReason": "cancelled"})
        self.assertLess(len(text_of([n["params"]["update"] for n in notes])), 400)
        r, ups = self.prompt(c, sid, "again")                            # the session is usable afterwards
        self.assertEqual((r["stopReason"], text_of(ups)), ("end_turn", "faux: again"))
        self.assertEqual(c.stop(), 0)

    def test_two_sessions_are_kept_apart(self):
        c = self.client()
        a, b = self.new(c), self.new(c)
        self.assertNotEqual(a, b)
        ra = c.send("session/prompt", {"sessionId": a, "prompt": [{"type": "text", "text": "I am session A"}]})
        rb = c.send("session/prompt", {"sessionId": b, "prompt": [{"type": "text", "text": "I am session B"}]})
        done = {}
        while len(done) < 2:                                             # both run at once; answers in any order
            msg = c.read()
            if "method" in msg:
                c.notes.append(msg)
            else:
                done[msg["id"]] = msg["result"]
        self.assertEqual(done, {ra: {"stopReason": "end_turn"}, rb: {"stopReason": "end_turn"}})

        def said(sid):
            return text_of([n["params"]["update"] for n in c.notes if n["params"]["sessionId"] == sid])

        self.assertEqual((said(a), said(b)), ("faux: I am session A", "faux: I am session B"))
        sa, sb = self.made
        self.assertEqual(len([e for e in sa.sessions.entries if e["type"] == "message"]), 2)
        self.assertEqual(len([e for e in sb.sessions.entries if e["type"] == "message"]), 2)
        self.assertEqual(c.stop(), 0)

    def test_load_replays_history(self):
        c = self.client()
        sid = self.new(c)
        Faux.queue = [{"tool": "read", "args": {"path": "a.txt"}, "id": "r1"}, {"thinking": "pondering", "text": "it has three lines"}]
        self.prompt(c, sid, "read a.txt")
        r, ups = c.ok("session/load", {"sessionId": sid, "cwd": str(self.dir), "mcpServers": []})   # a live session
        self.assertEqual(r, {})
        self.assertEqual(text_of(ups, "user_message_chunk"), "read a.txt")
        self.assertEqual(c.stop(), 0)

        c2 = self.client()                                               # a new server process: from disk
        r, ups = c2.ok("session/load", {"sessionId": sid, "cwd": str(self.dir), "mcpServers": []})
        self.assertEqual(r, {})
        kinds = [u["sessionUpdate"] for u in ups]
        self.assertEqual(kinds, ["user_message_chunk", "tool_call", "tool_call_update", "agent_thought_chunk", "agent_message_chunk"])
        self.assertEqual(text_of(ups, "user_message_chunk"), "read a.txt")
        self.assertEqual(text_of(ups), "it has three lines")
        self.assertEqual(text_of(ups, "agent_thought_chunk"), "pondering")
        call, done = ups[1], ups[2]
        self.assertEqual((call["toolCallId"], call["kind"], done["toolCallId"], done["status"]), ("r1", "read", "r1", "completed"))
        self.assertIn("alpha", done["content"][0]["content"]["text"])
        r, ups = self.prompt(c2, sid, "and now?")                        # the loaded session continues
        self.assertEqual(r["stopReason"], "end_turn")
        self.assertEqual(len([m for m in Faux.requests[-1]["messages"] if m["role"] == "user"]), 2)
        msg, _ = c2.call("session/load", {"sessionId": "00000000-dead-beef-0000-000000000000", "cwd": str(self.dir)})
        self.assertEqual(msg["error"]["code"], -32002)
        self.assertEqual(c2.stop(), 0)

    def test_permission_round_trip(self):
        self.profile_name = "tapprove"
        outside = Path(tempfile.mkdtemp(prefix="omni-acp-outside-"))
        c = self.client()
        sid = self.new(c)

        def choose(kind):
            def answer(params):
                opt = [o for o in params["options"] if o["kind"] == kind][0]
                return {"outcome": {"outcome": "selected", "optionId": opt["optionId"]}}
            return answer

        c.permission = choose("reject_once")
        Faux.queue = [{"tool": "write", "args": {"path": str(outside / "no.txt"), "content": "x"}, "id": "w1"}, {"text": "ok"}]
        r, ups = self.prompt(c, sid, "write outside")
        self.assertEqual(r["stopReason"], "end_turn")
        self.assertFalse((outside / "no.txt").exists())
        ask = c.asked[0]
        self.assertEqual(ask["sessionId"], sid)
        self.assertEqual(ask["toolCall"]["toolCallId"], "w1")           # attached to the tool call that asked
        self.assertIn("outside the project", ask["toolCall"]["content"][0]["content"]["text"])
        self.assertEqual([o["kind"] for o in ask["options"]], ["allow_once", "allow_always", "reject_once"])
        self.assertEqual([u["status"] for u in ups if u.get("toolCallId") == "w1"][-1], "failed")

        c.permission = choose("allow_once")
        Faux.queue = [{"tool": "write", "args": {"path": str(outside / "yes.txt"), "content": "x"}, "id": "w2"}, {"text": "ok"}]
        r, ups = self.prompt(c, sid, "write outside again")
        self.assertEqual((outside / "yes.txt").read_text(), "x")
        self.assertEqual([u["status"] for u in ups if u.get("toolCallId") == "w2"][-1], "completed")

        c.permission = lambda params: {"outcome": {"outcome": "cancelled"}}
        Faux.queue = [{"tool": "write", "args": {"path": str(outside / "no2.txt"), "content": "x"}, "id": "w3"}, {"text": "ok"}]
        self.prompt(c, sid, "once more")
        self.assertFalse((outside / "no2.txt").exists())
        self.assertEqual(len(c.asked), 3)
        self.assertEqual(c.stop(), 0)


class StubSessions:
    session_id = "stub-1"

    def get_branch(self):
        return []


class StubAgent:
    """Just enough of AgentSession to drive the server's turn handling."""

    def __init__(self, cwd, script):
        self.cwd, self.script, self.ui = cwd, script, None
        self.sessions, self.listeners, self.diagnostics = StubSessions(), [], []
        self.answers, self.down = [], False

    def subscribe(self, fn):
        self.listeners.append(fn)
        return lambda: self.listeners.remove(fn)

    def emit(self, ev):
        for fn in list(self.listeners):
            fn(ev)

    def prompt(self, text, images=None, source=""):
        self.script(self, text)

    def wait_for_idle(self, timeout=None):
        return True

    def abort(self):
        pass

    def shutdown(self, reason="quit"):
        self.down = True


class TestTurnOutcomes(unittest.TestCase):
    def setUp(self):
        self.before = set(threading.enumerate())
        self.cwd = tempfile.mkdtemp(prefix="omni-acp-stub-")

    def tearDown(self):
        time.sleep(0.05)
        self.assertEqual([t.name for t in threading.enumerate() if t not in self.before and t.is_alive()], [])

    def run_one(self, script, silent=False, **kw):
        agent = StubAgent(self.cwd, script)
        c = Client(lambda cwd, session_path=None: agent, **kw)
        if silent:
            c.permission = lambda params: None                           # the editor never answers
        sid, _ = c.ok("session/new", {"cwd": self.cwd})
        msg, notes = c.call("session/prompt", {"sessionId": sid["sessionId"], "prompt": [{"type": "text", "text": "go"}]})
        self.assertEqual(c.stop(), 0)
        self.assertTrue(agent.down)                                      # EOF shuts sessions down
        return agent, c, msg, [n["params"]["update"] for n in notes]

    def test_max_tokens_error_and_refusal(self):
        end = lambda reason, **kw: (lambda a, t: a.emit({"type": "message_end", "message": {"role": "assistant", "stopReason": reason, **kw}}))
        self.assertEqual(self.run_one(end("length"))[2]["result"], {"stopReason": "max_tokens"})
        self.assertEqual(self.run_one(end("stop"))[2]["result"], {"stopReason": "end_turn"})
        self.assertEqual(self.run_one(end("aborted"))[2]["result"], {"stopReason": "cancelled"})
        err = self.run_one(end("error", errorMessage="No API key found for x"))[2]["error"]
        self.assertEqual((err["code"], err["message"]), (-32603, "No API key found for x"))
        blocked = lambda a, t: a.emit({"type": "notice", "level": "warning", "message": "Message blocked by a hook: nope"})
        _, _, msg, ups = self.run_one(blocked)
        self.assertEqual(msg["result"], {"stopReason": "refusal"})
        self.assertIn("nope", text_of(ups))

        def crash(a, t):
            raise RuntimeError("boom")
        self.assertIn("boom", self.run_one(crash)[2]["error"]["message"])

    def test_permission_forwarding_timeout_and_dangling_tool_call(self):
        def script(a, t):
            a.emit({"type": "message_update", "assistantMessageEvent": {"type": "toolcall_end", "toolCall": {
                "id": "c9", "name": "fetch", "arguments": {"url": "https://example.com"}}}})
            a.answers.append(a.ui.ext_select(title="Allow fetch?  -  network", options=["Allow once", "Always allow (this session)", "Deny"]))
            a.answers.append(a.ui.ext_confirm(title="Sure?"))
            a.answers.append(a.ui.ext_input(title="free text"))
            a.ui.ext_notify(message="hi")

        agent, c, msg, ups = self.run_one(script)                        # the editor answers "cancelled"
        self.assertIsInstance(agent.ui, acp.AcpUI)
        self.assertEqual(agent.answers, [None, False, None])
        self.assertEqual(msg["result"], {"stopReason": "end_turn"})

        t0 = time.time()
        agent, c, msg, ups = self.run_one(script, silent=True, permission_timeout=0.3)
        self.assertEqual(agent.answers, [None, False, None])             # nobody answered: rejected after the timeout
        self.assertGreaterEqual(time.time() - t0, 0.6)
        self.assertEqual(len(c.asked), 2)
        self.assertTrue(c.asked[0]["toolCall"]["toolCallId"].startswith("perm-"))   # not inside a running tool
        self.assertEqual(c.asked[0]["toolCall"]["status"], "pending")
        perm = [u for u in ups if u.get("toolCallId") == c.asked[0]["toolCall"]["toolCallId"]]
        self.assertEqual(perm[-1]["status"], "failed")
        dangling = [u for u in ups if u.get("toolCallId") == "c9"]       # announced, never run: closed at turn end
        self.assertEqual((dangling[0]["kind"], dangling[0]["status"], dangling[-1]["status"]), ("fetch", "pending", "failed"))

        def allow(a, t):
            a.answers.append(a.ui.ext_select(title="Plan ready?", options=["Approve and start", "Keep planning"]))
            a.answers.append(a.ui.ext_confirm(title="Sure?"))

        agent = StubAgent(self.cwd, allow)
        c = Client(lambda cwd, session_path=None: agent)
        c.permission = lambda p: {"outcome": {"outcome": "selected", "optionId": p["options"][0]["optionId"]}}
        sid = c.ok("session/new", {"cwd": self.cwd})[0]["sessionId"]
        _, ups = c.ok("session/prompt", {"sessionId": sid, "prompt": [{"type": "text", "text": "go"}]})
        self.assertEqual(agent.answers, ["Approve and start", True])
        self.assertEqual([o["kind"] for o in c.asked[0]["options"]], ["allow_once", "reject_once"])
        self.assertEqual([u["status"] for u in ups if u["sessionUpdate"] == "tool_call_update"], ["completed", "completed"])
        self.assertEqual(c.stop(), 0)

    def test_eof_releases_a_waiting_permission_request(self):
        asked = threading.Event()

        def script(a, t):
            asked.set()
            a.answers.append(a.ui.ext_select(title="Allow?", options=["Allow once", "Deny"]))

        agent = StubAgent(self.cwd, script)
        c = Client(lambda cwd, session_path=None: agent, permission_timeout=60)
        sid = c.ok("session/new", {"cwd": self.cwd})[0]["sessionId"]
        c.send("session/prompt", {"sessionId": sid, "prompt": [{"type": "text", "text": "go"}]})
        self.assertTrue(asked.wait(WAIT))
        while c.read().get("method") != "session/request_permission":
            pass
        t0 = time.time()
        self.assertEqual(c.stop(), 0)                                    # EOF while the question is open
        self.assertLess(time.time() - t0, 10)
        self.assertEqual(agent.answers, [None])


class TestHelpersAndCli(unittest.TestCase):
    def test_kinds_titles_and_blocks(self):
        kinds = {n: acp.tool_kind(n) for n in ("read", "ls", "edit", "write", "bash", "grep", "find", "fetch", "todo", "mcp__x__web_fetch", "zzz")}
        self.assertEqual(kinds, {"read": "read", "ls": "read", "edit": "edit", "write": "edit", "bash": "execute", "grep": "search",
                                 "find": "search", "fetch": "fetch", "todo": "other", "mcp__x__web_fetch": "fetch", "zzz": "other"})
        self.assertEqual(acp.tool_title("bash", {"command": "echo a\necho b"}), "bash: echo a...")
        self.assertEqual(acp.tool_title("todo", {}), "todo")
        self.assertEqual(acp.path_from_uri("file:///C:/a%20b/c.txt"), "C:/a b/c.txt")
        self.assertEqual(acp.path_from_uri("file:///home/u/c.txt"), "/home/u/c.txt")
        self.assertEqual(acp.path_from_uri("C:\\x\\y.txt"), "C:\\x\\y.txt")
        self.assertIsNone(acp.path_from_uri("https://example.com/x"))
        text, images = acp.prompt_from_blocks([
            {"type": "text", "text": "hi"}, {"type": "image", "data": "QUJD", "mimeType": "image/png"},
            {"type": "resource", "resource": {"uri": "file:///p/b.bin", "blob": "AAEC", "mimeType": "application/octet-stream"}},
            {"type": "resource", "resource": {"uri": "file:///p/t.txt", "blob": "aGVsbG8="}}, {"type": "audio", "data": "x"}, 7])
        self.assertEqual(images, [{"type": "image", "data": "QUJD", "mimeType": "image/png"}])
        self.assertIn("[Binary file omitted", text)
        self.assertIn("[Attached file: t.txt]\nURI: file:///p/t.txt\n\nhello", text)
        self.assertEqual(acp.prompt_from_blocks(None), ("", []))
        self.assertIsNone(acp.AcpUI().ext_select(title="q", options=["a"]))      # unbound: the cancel value

    def test_print_config(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = acp.cli(["--print-config", "--profile", "coder"], lambda *a, **k: self.fail("no session is made"))
        self.assertEqual(code, 0)
        server = json.loads(out.getvalue())["agent_servers"]["Omni"]
        self.assertEqual(server["command"], sys.executable)
        main = Path(server["args"][0])
        self.assertTrue(main.is_absolute() and main.is_file() and main.name == "omni_main.py")
        self.assertEqual(server["args"][1:], ["acp", "--profile", "coder"])
        self.assertEqual(json.loads(acp.zed_config())["agent_servers"]["Omni"]["args"][1:], ["acp"])

    def test_cli_serves_on_the_process_streams_and_keeps_stdout_pure(self):
        before = set(threading.enumerate())
        cwd = tempfile.mkdtemp(prefix="omni-acp-cli-")
        seen = {}

        def factory(cwd, session_path=None, profile=None, model=None, ui=None):
            seen.update(profile=profile, model=model, ui=ui, session_path=session_path)
            print("a stray print from session start")                   # must not reach the protocol stream
            agent = StubAgent(cwd, lambda a, t: (print("stray print in a turn"), a.emit(
                {"type": "message_update", "assistantMessageEvent": {"type": "text_delta", "delta": "héllo ✓"}})))
            agent.ui = ui
            return agent

        inp, out, err = Pipe(), Pipe(), io.StringIO()
        saved = sys.stdin, sys.stdout, sys.stderr
        sys.stdin, sys.stdout, sys.stderr = inp, out, err
        try:
            result = {}
            t = threading.Thread(target=lambda: result.update(code=acp.cli(["--profile", "tcoder", "--model", "faux/faux-1"], factory)))
            t.start()
            inp.write(json.dumps({"jsonrpc": "2.0", "id": 1, "method": "session/new", "params": {"cwd": cwd}}) + "\n")
            sid = json.loads(out.readline(WAIT))["result"]["sessionId"]
            inp.write(json.dumps({"jsonrpc": "2.0", "id": 2, "method": "session/prompt",
                                  "params": {"sessionId": sid, "prompt": [{"type": "text", "text": "go"}]}}) + "\n")
            chunk = json.loads(out.readline(WAIT))
            self.assertEqual(chunk["params"]["update"]["content"]["text"], "héllo ✓")
            self.assertEqual(json.loads(out.readline(WAIT))["result"], {"stopReason": "end_turn"})
            inp.close()
            t.join(WAIT)
            self.assertFalse(t.is_alive())
        finally:
            sys.stdin, sys.stdout, sys.stderr = saved
        self.assertEqual(result, {"code": 0})
        self.assertEqual((seen["profile"], seen["model"], seen["session_path"]), ("tcoder", "faux/faux-1", None))
        self.assertIsInstance(seen["ui"], acp.AcpUI)
        self.assertEqual(seen["ui"].session_id, sid)                     # the launcher's UI object was bound, not replaced
        self.assertEqual(len(out.lines), 3)
        for line in out.lines:
            self.assertEqual(json.loads(line)["jsonrpc"], "2.0")
        self.assertIn("a stray print from session start", err.getvalue())
        self.assertIn("stray print in a turn", err.getvalue())
        self.assertIs(sys.stdout, saved[1])
        self.assertEqual([t.name for t in threading.enumerate() if t not in before and t.is_alive()], [])


if __name__ == "__main__":
    unittest.main()
