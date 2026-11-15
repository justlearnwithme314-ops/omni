"""The web interface (omni web), omni batch and omni update. Offline."""
import contextlib
import io
import json
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

from test_capabilities import CapCase  # noqa: E402
from src.pi import batch, web  # noqa: E402
from src.pi.ai import Faux  # noqa: E402


class WebCase(CapCase):
    def start(self):
        s = self.session(profile="tcoder")
        s.ui = web.WebUI(web.Hub())
        self.server = web.serve(s, port=0, token="KEY")
        self.base = self.server.url.split("/?")[0]
        self.addCleanup(lambda: (self.server.shutdown(), self.server.server_close()))
        return s

    def req(self, path, body=None, key="KEY", host=None):
        r = urllib.request.Request(self.base + path, data=json.dumps(body).encode() if body is not None else None,
                                   headers={"X-Omni-Key": key, "Content-Type": "application/json", **({"Host": host} if host else {})})
        try:
            with urllib.request.urlopen(r, timeout=10) as resp:
                raw = resp.read().decode("utf-8")
                return resp.status, (json.loads(raw) if resp.headers.get("Content-Type", "").startswith("application/json") else raw)
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode("utf-8", "replace")

    def events(self):
        q = self.server.app.hub.join()
        self.addCleanup(lambda: self.server.app.hub.leave(q))
        return q

    @staticmethod
    def drain(q, until, timeout=10):
        out, end = [], time.time() + timeout
        while time.time() < end:
            try:
                ev = q.get(timeout=0.2)
            except Exception:
                continue
            out.append(ev)
            if ev.get("type") == until:
                break
        return out


class TestWeb(WebCase):
    def test_access_key_and_page(self):
        self.start()
        self.assertEqual(self.req("/health")[0], 200)
        self.assertEqual(self.req("/", key="wrong")[0], 403)
        self.assertEqual(self.req("/api/state", key="")[0], 403)
        self.assertEqual(self.req("/api/prompt", {"text": "hi"}, key="nope")[0], 403)
        self.assertEqual(self.req("/api/state", host="evil.example")[0], 403)          # a rebound DNS name is refused
        status, page = self.req("/")
        self.assertEqual(status, 200)
        self.assertIn("<title>Omni</title>", page)
        self.assertNotIn("http://", page.replace("http://www.w3.org", ""))             # no external resources
        self.assertNotIn("https://", page)
        with urllib.request.urlopen(self.base + "/?k=KEY", timeout=10) as r:           # the printed address works as is
            self.assertEqual(r.status, 200)
        self.assertEqual(self.req("/api/nope")[0], 404)

    def test_prompt_streams_events_and_history(self):
        s = self.start()
        (Path(s.cwd) / "note.txt").write_text("hello from the file", encoding="utf-8")
        q = self.events()
        Faux.queue = [{"tool": "read", "args": {"path": "note.txt"}}, {"text": "The file says hello."}]
        self.assertEqual(self.req("/api/prompt", {"text": "what is in note.txt?"})[1]["kind"], "prompt")
        evs = self.drain(q, "done")
        kinds = [e["type"] for e in evs]
        self.assertEqual(kinds[0], "user")
        self.assertIn("tool_start", kinds)
        end = next(e for e in evs if e["type"] == "tool_end")
        self.assertEqual((end["name"], end["ok"]), ("read", True))
        self.assertIn("hello from the file", end["text"])
        self.assertEqual("".join(e["text"] for e in evs if e["type"] == "delta"), "The file says hello.")
        _status, st = self.req("/api/state")
        self.assertEqual([h["type"] for h in st["history"]], ["user", "tool_start", "tool_end", "assistant"])
        self.assertEqual((st["model"], st["busy"]), ("faux/faux-1", False))
        self.assertTrue(any(c["name"] == "model" for c in st["commands"]))
        self.assertEqual(self.req("/api/prompt", {"text": "  "})[0], 400)

    def test_commands_and_dialogs(self):
        s = self.start()
        q = self.events()
        self.req("/api/prompt", {"text": "/session"})
        panel = next(e for e in self.drain(q, "state") if e["type"] == "panel")
        self.assertEqual(panel["title"], "session")
        self.assertIn("faux/faux-1", panel["text"])
        answer = {}
        t = threading.Thread(target=lambda: answer.update(v=s.ui.ext_select("Pick one", ["red", "green"])))
        t.start()
        ask = next(e for e in self.drain(q, "ask") if e["type"] == "ask")
        self.assertEqual((ask["kind"], ask["options"]), ("select", ["red", "green"]))
        self.assertEqual(self.req("/api/answer", {"id": ask["id"], "value": 1})[1], {"ok": True})
        t.join(5)
        self.assertEqual(answer["v"], "green")
        self.assertEqual(self.req("/api/answer", {"id": "gone", "value": 1})[1], {"ok": False})
        self.server.app.hub.leave(q)
        self.assertIs(s.ui.confirm("Sure?", "really"), False)                          # nobody is looking: the safe answer

    def test_translate(self):
        self.assertEqual(web.translate({"type": "message_update", "assistantMessageEvent": {"type": "thinking_delta", "delta": "hm"}}),
                         {"type": "thinking", "text": "hm"})
        self.assertIsNone(web.translate({"type": "turn_start"}))
        self.assertEqual(web.translate({"type": "message_end", "message": {"role": "assistant", "stopReason": "error", "errorMessage": "x"}}),
                         {"type": "message_end", "error": "x"})
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(web.cli(["--port"], None), 2)
            self.assertEqual(web.cli(["--bogus"], None), 2)
            self.assertEqual(web.cli(["--help"], None), 0)


class TestBatch(CapCase):
    def factory(self):
        return lambda profile, model, cwd: self.session(profile="tcoder", no_session=True)

    def test_runs_every_prompt_and_resumes(self):
        src = self.dir / "prompts.jsonl"
        src.write_text('{"id": "a", "prompt": "first"}\n# a comment\nplain text prompt\n\n{"prompt": "third", "id": 7}\n', encoding="utf-8")
        items = batch.read_items(src)
        self.assertEqual([(i["id"], i["prompt"]) for i in items], [("a", "first"), ("2", "plain text prompt"), ("7", "third")])
        Faux.queue = [{"text": "one"}, {"error": "boom", "status": 400}, {"text": "three"}]
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = batch.cli([str(src), "--parallel", "1"], self.factory())
        self.assertEqual(code, 1)
        rows = [json.loads(l) for l in (self.dir / "prompts.results.jsonl").read_text(encoding="utf-8").splitlines()]
        self.assertEqual([(r["id"], r["answer"], bool(r["error"])) for r in rows], [("a", "one", False), ("2", "", True), ("7", "three", False)])
        self.assertIn("2 answered, 1 failed", out.getvalue())
        Faux.queue = [{"text": "two, finally"}]
        with contextlib.redirect_stdout(out):
            self.assertEqual(batch.cli([str(src), "--resume", "--parallel", "3"], self.factory()), 0)
        rows = [json.loads(l) for l in (self.dir / "prompts.results.jsonl").read_text(encoding="utf-8").splitlines()]
        self.assertEqual((len(rows), rows[-1]["id"], rows[-1]["answer"]), (4, "2", "two, finally"))
        self.assertIn("2 already done", out.getvalue())

    def test_usage_errors_and_a_failing_session(self):
        out = io.StringIO()
        bad = self.dir / "bad.jsonl"
        bad.write_text('{"prompt": ""}\n', encoding="utf-8")
        dup = self.dir / "dup.jsonl"
        dup.write_text('{"id": 1, "prompt": "a"}\n{"id": 1, "prompt": "b"}\n', encoding="utf-8")
        with contextlib.redirect_stdout(out):
            self.assertEqual(batch.cli([], None), 2)
            self.assertEqual(batch.cli([str(self.dir / "missing.jsonl")], None), 2)
            self.assertEqual(batch.cli([str(bad)], None), 2)
            self.assertEqual(batch.cli([str(dup)], None), 2)
            self.assertEqual(batch.cli(["--parallel"], None), 2)

        def broken(cwd=None):
            raise SystemExit("no such model")
        row = batch.run_one({"id": "x", "prompt": "p"}, broken)
        self.assertEqual((row["answer"], row["error"]), ("", "SystemExit: no such model"))


class FakeGit:
    def __init__(self, **answers):
        self.answers, self.calls = answers, []

    def __call__(self, cmd, **kw):
        self.calls.append(cmd[1:])
        key = cmd[1]
        code, text = self.answers.get(key, (0, ""))
        if key == "rev-parse" and "--short" in cmd:
            heads = self.answers.get("heads", ["abc1234", "abc1234"])
            text = heads.pop(0) if len(heads) > 1 else heads[0]
            code = 0
        return type("R", (), {"returncode": code, "stdout": text, "stderr": text})()


class TestUpdate(unittest.TestCase):
    def run_update(self, git):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = batch.update(Path("."), run=git)
        return code, out.getvalue()

    def test_update_paths(self):
        self.assertEqual(self.run_update(FakeGit(**{"rev-parse": (128, "")}))[0], 1)
        code, text = self.run_update(FakeGit(status=(0, " M src/pi/agent.py\n M x.pyc\n")))
        self.assertEqual(code, 1)
        self.assertIn("local changes", text)
        self.assertNotIn("x.pyc", text)
        self.assertIn("up to date (abc1234)", self.run_update(FakeGit())[1])
        code, text = self.run_update(FakeGit(heads=["aaa1111", "bbb2222"], log=(0, "bbb2222 Add things\n")))
        self.assertEqual(code, 0)
        self.assertIn("Updated aaa1111 -> bbb2222 (1 commit(s))", text)
        code, text = self.run_update(FakeGit(merge=(1, "fatal: Not possible to fast-forward")))
        self.assertEqual(code, 1)
        self.assertIn("Could not fast-forward", text)
        self.assertIn("Could not reach the remote", self.run_update(FakeGit(fetch=(1, "no network")))[1])


if __name__ == "__main__":
    unittest.main()
