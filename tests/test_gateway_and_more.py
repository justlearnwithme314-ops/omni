"""Gateway (Telegram + HTTP channels, pairing, per-chat sessions), edit diagnostics, memory reflection, trace ratings,
browser screenshots and module dependencies. Offline: a local server plays the Telegram Bot API."""
import http.server
import json
import os
import sys
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from test_capabilities import CapCase, Port  # noqa: E402
from test_capabilities_infra import FakeRelay  # noqa: E402
from src.pi import gateway as gw  # noqa: E402
from src.pi.ai import Faux  # noqa: E402
from src.pi.caps import browser, diagnostics, reflect, tracing  # noqa: E402
from src.pi.commands import Commands  # noqa: E402


class FakeTelegram:
    def __init__(self):
        self.updates, self.sent, self.calls = [], [], []
        outer = self

        class H(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                n = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(n)) if n else {}
                method = self.path.rsplit("/", 1)[-1]
                outer.calls.append((self.path, method))
                result = True
                if method == "getUpdates":
                    result = [u for u in outer.updates if u["update_id"] >= body.get("offset", 0)]
                    if not result:
                        time.sleep(0.05)
                elif method == "sendMessage":
                    outer.sent.append((str(body["chat_id"]), body["text"]))
                data = json.dumps({"ok": True, "result": result}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(data)
        self.srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.srv.server_address[1]}"

    def push(self, uid, chat, sender, text):
        self.updates.append({"update_id": uid, "message": {"chat": {"id": chat}, "from": {"id": sender, "username": f"u{sender}"}, "text": text}})

    def wait_sent(self, n, timeout=15):
        end = time.time() + timeout
        while len(self.sent) < n and time.time() < end:
            time.sleep(0.05)
        return self.sent

    def close(self):
        self.srv.shutdown()
        self.srv.server_close()


class TestGateway(CapCase):
    def gateway(self, **channels):
        cfg = {"profile": "tcoder", "model": "faux/faux-1", "cwd": str(self.dir), "channels": channels}
        gw.save_config(self.agent, cfg)
        return gw.Gateway(self.agent)

    def test_conversations_are_separate_persistent_sessions(self):
        g = self.gateway(telegram={"token": "T", "api_base": "http://127.0.0.1:9", "allow": [1, 2]})
        try:
            say = lambda chat, text, sender="1": g.handle(gw.Inbound("telegram", chat, sender, text))  # noqa: E731
            Faux.queue = [{"text": "hello Ann"}, {"text": "hello Bob"}, {"text": "you are Ann"}]
            self.assertEqual(say("100", "I am Ann"), "hello Ann")
            self.assertEqual(say("200", "I am Bob", "2"), "hello Bob")
            self.assertEqual(say("100", "who am I?"), "you are Ann")
            third = Faux.requests[2]["messages"]
            self.assertEqual([m["role"] for m in third], ["user", "assistant", "user"])       # chat 100's own history only
            self.assertNotIn("Bob", json.dumps(third))
            self.assertIn("through a chat app", Faux.requests[0]["systemPrompt"])
            self.assertIn("todo", Faux.requests[0]["tools"])                                  # the configured profile
            self.assertIn("model faux/faux-1 · profile tcoder", say("100", "/status"))
            self.assertEqual(say("100", "/stop"), "Nothing is running.")
            files = json.loads((self.agent / "gateway" / "conversations.json").read_text())
            self.assertEqual(sorted(files), ["telegram:100", "telegram:200"])
            first_file = files["telegram:100"]
        finally:
            g.stop()
        g2 = gw.Gateway(self.agent)                                                           # a restart keeps the context
        try:
            Faux.queue = [{"text": "still Ann"}]
            self.assertEqual(g2.handle(gw.Inbound("telegram", "100", "1", "and now?")), "still Ann")
            self.assertEqual(len(Faux.requests[-1]["messages"]), 5)
            self.assertEqual(g2.handle(gw.Inbound("telegram", "100", "1", "/new")), "Started a new conversation.")
            self.assertNotEqual(json.loads((self.agent / "gateway" / "conversations.json").read_text())["telegram:100"], first_file)
            Faux.queue = [{"text": "fresh"}]
            g2.handle(gw.Inbound("telegram", "100", "1", "hi"))
            self.assertEqual(len(Faux.requests[-1]["messages"]), 1)
        finally:
            g2.stop()

    def test_strangers_get_a_pairing_code_and_approval_lets_them_in(self):
        g = self.gateway(telegram={"token": "T", "api_base": "http://127.0.0.1:9", "allow": [1]})
        try:
            reply = g.handle(gw.Inbound("telegram", "900", "77", "hi", name="mallory"))
            self.assertIn("This assistant is private", reply)
            code = reply.split("approve ")[1].split()[0]
            self.assertEqual(g.handle(gw.Inbound("telegram", "900", "77", "again")).split("approve ")[1].split()[0], code)
            self.assertEqual(Faux.requests, [])                                               # the model never saw it
            self.assertIn("No pending pairing request", gw.approve(self.agent, "ZZZZZZ"))
            self.assertIn("Approved mallory on telegram", gw.approve(self.agent, code.lower()))
            self.assertEqual(gw.load_config(self.agent)["channels"]["telegram"]["allow"], ["1", "77"])
        finally:
            g.stop()
        g2 = gw.Gateway(self.agent)
        try:
            Faux.queue = [{"text": "welcome"}]
            self.assertEqual(g2.handle(gw.Inbound("telegram", "900", "77", "hi")), "welcome")
        finally:
            g2.stop()

    def test_telegram_and_http_channels_end_to_end(self):
        tg = FakeTelegram()
        g = self.gateway(telegram={"token": "SECRET", "api_base": tg.url, "allow": [5]}, http={"port": 0, "token": "tok"})
        try:
            self.assertEqual(g.errors, [])
            Faux.queue = [{"text": "pong " + "x" * 5000}]
            g.start()
            tg.push(10, 555, 5, "ping")
            sent = tg.wait_sent(2)
            self.assertEqual([c for c, _t in sent], ["555", "555"])                           # long replies are split
            self.assertTrue(sent[0][1].startswith("pong "))
            self.assertEqual(len(sent[0][1]) + len(sent[1][1]), 5005)
            self.assertTrue(any(m == "sendChatAction" for _p, m in tg.calls))
            self.assertTrue(all("/botSECRET/" in p for p, _m in tg.calls))
            port = g.channels["http"].port

            def post(body, token="tok"):
                req = urllib.request.Request(f"http://127.0.0.1:{port}/message", data=json.dumps(body).encode(),
                                             headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
                try:
                    with urllib.request.urlopen(req, timeout=20) as r:
                        return r.status, json.loads(r.read())
                except urllib.error.HTTPError as e:
                    return e.code, json.loads(e.read())
            Faux.queue = [{"text": "from http"}]
            self.assertEqual(post({"chat": "ci", "text": "status?"}), (200, {"reply": "from http"}))
            self.assertEqual(post({"chat": "ci", "text": "x"}, token="wrong")[0], 401)
            self.assertEqual(post({"nope": 1})[0], 400)
        finally:
            g.stop()
            tg.close()

    def test_config_errors_and_cli(self):
        import contextlib
        import io
        os.environ.pop("OMNI_TEST_NO_SUCH_TOKEN", None)
        g = self.gateway(telegram={"token": "$OMNI_TEST_NO_SUCH_TOKEN"}, mystery={"x": 1}, http={"token": ""})
        self.assertEqual(len(g.errors), 3)
        self.assertEqual(g.channels, {})
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(gw.cli(["status"], self.agent), 0)
            self.assertEqual(gw.cli(["run"], self.agent), 1)
            self.assertEqual(gw.cli(["bogus"], self.agent), 2)
        self.assertIn("telegram: on", out.getvalue())
        self.assertIn("No channel could be started", out.getvalue())
        gw.register_channel("mystery", lambda cfg: gw.Channel())
        try:
            self.assertIn("mystery", gw.Gateway(self.agent).channels)
        finally:
            gw.CHANNEL_TYPES.pop("mystery", None)


class TestDiagnostics(CapCase):
    def test_broken_edits_are_reported_in_the_same_result(self):
        s = self.session(capabilities=["diagnostics"], tools="read,bash,edit,write,grep,find,ls")
        Faux.queue = [{"tool": "write", "args": {"path": "bad.py", "content": "def f(:\n    return 1\n"}},
                      {"tool": "write", "args": {"path": "ok.py", "content": "def f():\n    return 1\n"}},
                      {"tool": "write", "args": {"path": "cfg.json", "content": "{\"a\": 1,}"}},
                      {"tool": "write", "args": {"path": "notes.txt", "content": "def f(:"}},
                      {"text": "done"}]
        s.prompt("write files")
        res = [e["message"] for e in s.sessions.entries if e["type"] == "message" and e["message"]["role"] == "toolResult"]
        bad = res[0]["content"][-1]["text"]
        self.assertIn("[diagnostics] The file was saved, but it no longer parses:", bad)
        self.assertIn("bad.py: line 1:", bad)
        self.assertEqual(res[0]["details"]["diagnostics"][0].split(":")[0], "bad.py")
        self.assertNotIn("diagnostics", json.dumps(res[1]["content"]))
        self.assertIn("cfg.json: line 1, column", res[2]["content"][-1]["text"])
        self.assertNotIn("diagnostics", json.dumps(res[3]["content"]))                        # unknown file type: untouched
        self.assertTrue((self.dir / "bad.py").exists())
        (self.dir / "t.toml").write_text("a = [1,\n")
        self.assertIsNotNone(diagnostics.check_file(str(self.dir / "t.toml")))
        self.assertIsNone(diagnostics.check_file(str(self.dir / "a.txt")))


class TestReflectRateScreenshot(CapCase):
    def test_reflect_saves_new_facts_and_respects_limits(self):
        (self.profiles / "rf.json").write_text(json.dumps({"name": "rf", "extends": "lean",
                                                             "capabilities": ["memory", "memory-archive", "memory-reflect"]}))
        s = self.session(profile="rf", no_session=False)
        port = Port()
        cmds = Commands(s, port)
        self.assertTrue(cmds.run("reflect", ""))
        self.assertIn("Not enough conversation", port.notes[-1][1])
        Faux.queue = [{"text": "Understood: tabs, not spaces, and deploys go through `make ship` because the CI token lives there. " * 3}]
        s.prompt("In this repo we always indent with tabs and deploy with make ship. Remember I prefer terse answers. " * 2)
        Faux.queue = [{"text": 'Here you go:\n{"core": [{"target": "user", "content": "Prefers terse answers"}, '
                               '{"target": "memory", "content": "This repo indents with tabs"}, {"target": "bogus", "content": "Deploy with make ship"}], '
                               '"archive": [{"content": "Deploys go through make ship because the CI token lives in the Makefile environment.", "tags": ["Deploy"]}]}'}]
        cmds.run("reflect", "")
        out = port.notes[-1][1]
        self.assertIn("saved 4 item(s)", out)
        self.assertEqual((self.agent / "memories" / "USER.md").read_text(encoding="utf-8"), "Prefers terse answers")
        self.assertEqual((self.agent / "memories" / "MEMORY.md").read_text(encoding="utf-8"),
                         "This repo indents with tabs\n§\nDeploy with make ship")           # unknown target falls back to memory
        self.assertIn("[archive] Deploys go through make ship", out)
        req = Faux.requests[-1]
        self.assertIn("ALREADY STORED:\n(nothing)", req["messages"][0]["content"])
        self.assertIn("USER: In this repo we always indent with tabs", req["messages"][0]["content"])
        self.assertEqual(req["tools"], [])
        Faux.queue = [{"text": '{"core": [{"target": "user", "content": "Prefers terse answers"}], "archive": []}'}]
        cmds.run("reflect", "")
        self.assertIn("nothing new worth keeping", port.notes[-1][1])
        self.assertIn("[user] Prefers terse answers", Faux.requests[-1]["messages"][0]["content"])   # told what is stored
        self.assertEqual(reflect.parse_reply("no json here"), {"core": [], "archive": []})

    def test_reflect_needs_memory_and_wizard_handles_dependencies(self):
        s = self.session(capabilities=["memory-reflect"])
        self.assertIn("needs memory", s.caps.problems["memory-reflect"])
        self.assertTrue(any("memory-reflect" in d for d in s.diagnostics))
        self.assertFalse(Commands(s, Port()).run("reflect", ""))
        from src.pi.caps import load_builtin
        from src.pi.profiles import BUILTIN, PROJECT_ROOT, list_profiles
        caps = load_builtin()
        order = sorted(caps.values(), key=lambda c: (c.is_on is not None, c.group != "feature", c.id))
        ix = lambda cid: next(i for i, c in enumerate(order) if c.id == cid)  # noqa: E731
        bases = [p["name"] for p in list_profiles() if p["name"] in BUILTIN or (PROJECT_ROOT / "profiles" / f"{p['name']}.json").is_file()]
        port = Port(answers=[bases.index("core"), ix("memory-reflect"), len(order), "deps-a"])
        Commands(s, port).cmd_profile("new")
        self.assertEqual(json.loads((self.profiles / "deps-a.json").read_text())["capabilities"], ["memory-reflect", "memory"])
        self.assertTrue(any("Also turned on memory (needed by memory-reflect)" in t for _l, t in port.notes))
        port = Port(answers=[bases.index("core"), ix("memory-reflect"), ix("memory"), len(order), "deps-b"])
        Commands(s, port).cmd_profile("new")
        self.assertEqual(json.loads((self.profiles / "deps-b.json").read_text())["capabilities"], [])
        self.assertTrue(any("Also removed memory-reflect (needs memory)" in t for _l, t in port.notes))

    def test_rate_the_last_run(self):
        s = self.session(capabilities=["tracing"], no_session=False)
        port = Port()
        cmds = Commands(s, port)
        cmds.run("rate", "up")
        self.assertIn("No run to rate yet", port.notes[-1][1])
        Faux.queue = [{"text": "answer"}]
        s.prompt("question")
        cmds.run("rate", "down too slow")
        self.assertEqual(port.notes[-1][1], "Rated the last run 0 (too slow).")
        cmds.run("rate", "0.5")
        cmds.run("rate", "maybe")
        self.assertIn("Usage: /rate", port.notes[-1][1])
        tr = tracing.tracer_of(s).last
        self.assertEqual([(x["value"], x["comment"]) for x in tr["scores"]], [(0.0, "too slow"), (0.5, "")])
        lines = [json.loads(l) for l in (self.agent / "traces" / f"{s.sessions.session_id}.jsonl").read_text(encoding="utf-8").splitlines()]
        self.assertEqual([l.get("type") for l in lines[1:]], ["score", "score"])
        self.assertEqual(lines[1]["traceId"], tr["id"])

    def test_browser_screenshot_returns_an_image(self):
        relay = FakeRelay()
        relay.connected = True

        def shot(path=None, tab_id=None):
            Path(path).write_bytes(b"\x89PNG fake")
            return path
        relay.capture_screenshot = shot
        make = browser.make_relay
        browser.make_relay = lambda _s: relay
        try:
            s = self.session(capabilities=["browser"])
            r = self.call(s, "browser", {"action": "screenshot"})
            self.assertEqual([b["type"] for b in r["content"]], ["text", "image"])
            self.assertEqual(r["content"][1]["mimeType"], "image/png")
            import base64
            self.assertEqual(base64.b64decode(r["content"][1]["data"]), b"\x89PNG fake")
        finally:
            browser.make_relay = make


if __name__ == "__main__":
    unittest.main()
