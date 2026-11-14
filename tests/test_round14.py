"""The task board, self-scheduled wake-ups, the MCP package audit and structured output. Offline."""
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

from test_capabilities import CapCase  # noqa: E402
from src.pi.ai import Faux  # noqa: E402
from src.pi.caps import guard  # noqa: E402


class Round14(CapCase):
    def profile(self, caps, **extra):
        (self.profiles / "r14.json").write_text(json.dumps({"name": "r14", "extends": "lean", "capabilities": caps, **extra}))
        return self.session(profile="r14")


class TestBoard(Round14):
    def test_cards_persist_across_sessions_and_projects(self):
        s = self.profile(["board"])
        self.assertIn("#1", self.text(self.call(s, "board", {"action": "add", "title": "Fix login", "body": "500 on empty password"})))
        self.call(s, "board", {"action": "add", "title": "Write docs"})
        self.assertTrue(self.call(s, "board", {"action": "move", "id": "1", "status": "blocked"}).get("isError"))
        self.call(s, "board", {"action": "move", "id": "#1", "status": "blocked", "reason": "needs API key"})
        self.call(s, "board", {"action": "comment", "id": "1", "text": "asked ops"})
        shown = self.text(self.call(s, "board", {"action": "show", "id": "1"}))
        self.assertIn("blocked: needs API key", shown)
        self.assertIn("asked ops", shown)
        self.assertIn("500 on empty password", shown)
        self.call(s, "board", {"action": "move", "id": "2", "status": "doing"})

        s2 = self.session(profile="r14")                       # a new session of the same project sees the cards
        listing = self.text(self.call(s2, "board", {"action": "list"}))
        self.assertLess(listing.index("Write docs"), listing.index("Fix login"))   # doing sorts before blocked
        self.assertIn("2 open card(s)", s2.caps.prompt_section())
        self.assertIn("#2 Write docs", s2.caps.prompt_section())
        self.call(s2, "board", {"action": "move", "id": "2", "status": "done"})
        self.assertNotIn("Write docs", self.text(self.call(s2, "board", {"action": "list"})))
        self.assertIn("[x] #2", self.text(self.call(s2, "board", {"action": "list", "status": "done"})))

        other = self.session(profile="r14", cwd=tempfile.mkdtemp(prefix="omni-board-other-"))
        self.assertEqual(self.text(self.call(other, "board", {"action": "list"})), "The board is empty.")
        self.assertIn("Fix login", self.text(self.call(other, "board", {"action": "list", "everywhere": True})))
        self.assertEqual(other.caps.commands()["board"][1](""), "The board is empty.")
        self.call(s2, "board", {"action": "remove", "id": "1"})
        self.assertTrue(self.call(s2, "board", {"action": "show", "id": "1"}).get("isError"))
        self.assertTrue(self.call(s2, "board", {"action": "show", "id": "abc"}).get("isError"))


class TestWake(Round14):
    def test_wake_prompts_the_session_later(self):
        s = self.profile(["wake"])
        r = self.call(s, "wake", {"in": "10m", "note": "check the deploy"})
        self.assertTrue(r.get("isError"))                          # headless: pointed at cronjob instead
        self.assertIn("cronjob", self.text(r))
        s.allow_wake = True
        self.assertTrue(self.call(s, "wake", {"in": "10m"}).get("isError"))
        self.assertTrue(self.call(s, "wake", {"in": "soon", "note": "x"}).get("isError"))
        self.assertTrue(self.call(s, "wake", {"in": "2d", "note": "x"}).get("isError"))
        r = self.call(s, "wake", {"in": "10m", "note": "check the deploy"})
        self.assertEqual(r["details"]["seconds"], 600)
        self.assertIn("check the deploy", self.text(self.call(s, "wake", {"action": "list"})))
        timer = s._wakes[0]["timer"]
        Faux.queue = [{"text": "deploy is green"}]
        timer.cancel()
        timer.function(*timer.args)                               # the timer firing
        users = [e["message"]["content"] for e in s.sessions.entries if e["type"] == "message" and e["message"]["role"] == "user"]
        self.assertEqual(users, ["[Wake-up you scheduled 10m ago] check the deploy"])
        self.assertIn("No wake-ups pending", self.text(self.call(s, "wake", {"action": "list"})))
        self.call(s, "wake", {"in": "5m", "note": "again"})
        self.assertEqual(self.call(s, "wake", {"action": "cancel"})["details"]["cancelled"], 1)


class TestMcpAudit(Round14):
    def test_package_of(self):
        self.assertEqual(guard.package_of({"command": "npx", "args": ["-y", "@scope/server-x@1.2.3", "--port", "1"]}), ("npm", "@scope/server-x", "1.2.3"))
        self.assertEqual(guard.package_of({"command": "C:/n/npx.cmd", "args": ["some-server"]}), ("npm", "some-server", ""))
        self.assertEqual(guard.package_of({"command": "uvx", "args": ["--from", "mcp-thing==0.4", "run-it"]}), ("PyPI", "mcp-thing", "0.4"))
        self.assertEqual(guard.package_of({"command": "uv", "args": ["tool", "run", "mcp-git"]}), ("PyPI", "mcp-git", ""))
        self.assertEqual(guard.package_of({"command": "pnpm", "args": ["dlx", "pkg@2"]}), ("npm", "pkg", "2"))
        self.assertIsNone(guard.package_of({"command": "python", "args": ["server.py"]}))
        self.assertIsNone(guard.package_of({"url": "https://x.test/mcp"}))

    def test_audit_reports_malware(self):
        seen = []

        class H(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                q = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                seen.append(q)
                vulns = {"evil-mcp": [{"id": "MAL-2026-1", "summary": "steals tokens"}],
                         "old-mcp": [{"id": "GHSA-aaaa"}]}.get(q["package"]["name"], [])
                body = json.dumps({"vulns": vulns} if vulns else {}).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
        srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        os.environ["OMNI_OSV_URL"] = f"http://127.0.0.1:{srv.server_address[1]}/v1/query"
        try:
            (self.agent / "mcp.json").write_text(json.dumps({"mcpServers": {
                "bad": {"command": "npx", "args": ["-y", "evil-mcp"], "enabled": False},
                "old": {"command": "uvx", "args": ["old-mcp==1.0"], "enabled": False},
                "fine": {"command": "npx", "args": ["good-mcp"], "enabled": False},
                "local": {"command": "python", "args": ["s.py"], "enabled": False}}}))
            s = self.profile(["mcp-audit"])
            text = self.text(self.call(s, "mcp_audit", {}))
            self.assertTrue(text.startswith("1 server(s) run a package reported as malware"))
            self.assertIn("! bad: evil-mcp (npm) is reported as MALWARE: MAL-2026-1 - steals tokens", text)
            self.assertIn("~ old: old-mcp@1.0 (PyPI) has 1 known vulnerability: GHSA-aaaa", text)
            self.assertIn("fine: good-mcp (npm) - no malware reports (version not pinned)", text)
            self.assertIn("local: not started through a package runner", text)
            self.assertIn({"package": {"ecosystem": "PyPI", "name": "old-mcp"}, "version": "1.0"}, seen)
            self.assertEqual(s.caps.commands()["mcp-audit"][1](""), text)
            os.environ["OMNI_OSV_URL"] = "http://127.0.0.1:1/none"
            self.assertIn("could not reach OSV", guard.audit(s))
        finally:
            os.environ.pop("OMNI_OSV_URL", None)
            srv.shutdown()
            srv.server_close()


class TestStructuredOutput(Round14):
    SCHEMA = {"type": "object", "properties": {"title": {"type": "string"}, "score": {"type": "integer"},
                                               "tags": {"type": "array", "items": {"type": "string"}},
                                               "level": {"enum": ["low", "high"]}},
              "required": ["title", "score"], "additionalProperties": False}

    def test_check(self):
        self.assertEqual(guard.check({"title": "a", "score": 1, "tags": ["x"], "level": "low"}, self.SCHEMA), [])
        errs = guard.check({"title": 1, "tags": ["x", 2], "level": "mid", "extra": 1}, self.SCHEMA)
        self.assertEqual(sorted(e.split(":")[0] for e in errs), ["$.extra", "$.level", "$.score", "$.tags[1]", "$.title"])
        self.assertTrue(guard.check(True, {"type": "integer"}))
        self.assertEqual(guard.check(None, {"type": ["string", "null"]}), [])

    def test_respond_validates_and_stores(self):
        s = self.profile(["structured-output"])
        self.assertNotIn("respond", s.all_tools())                 # no schema: no tool, no prompt text
        self.assertEqual(s.caps.prompt_section(), "")
        s = self.profile(["structured-output"], output_schema=self.SCHEMA)
        self.assertIn("respond", s.caps.prompt_section())
        out = Path(tempfile.mkdtemp(prefix="omni-out-")) / "result.json"
        os.environ["OMNI_OUTPUT_FILE"] = str(out)
        try:
            r = self.call(s, "respond", {"title": "Report"})
            self.assertTrue(r.get("isError"))
            self.assertIn("$.score: missing", self.text(r))
            self.assertIsNone(s.structured_output)
            self.call(s, "respond", {"title": "Report", "score": 7})
            self.assertEqual(s.structured_output, {"title": "Report", "score": 7})
            self.assertEqual(json.loads(out.read_text(encoding="utf-8")), {"title": "Report", "score": 7})
        finally:
            os.environ.pop("OMNI_OUTPUT_FILE", None)

    def test_non_object_schema_from_project_file(self):
        s0 = self.profile(["structured-output"])
        (Path(s0.cwd) / ".omni").mkdir(exist_ok=True)
        (Path(s0.cwd) / ".omni" / "output-schema.json").write_text(json.dumps({"type": "array", "items": {"type": "string"}}))
        s = self.session(profile="r14", cwd=s0.cwd)
        self.assertTrue(self.call(s, "respond", {"value": ["a", 1]}).get("isError"))
        self.call(s, "respond", {"value": ["a", "b"]})
        self.assertEqual(s.structured_output, ["a", "b"])


if __name__ == "__main__":
    unittest.main()
