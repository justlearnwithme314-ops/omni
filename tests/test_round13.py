"""Background processes, quiet shell output, secret redaction, the file inspector, magic keywords, the feed watcher,
/diff and the browser domain allowlist. Offline."""
import http.server
import json
import os
import sqlite3
import subprocess
import sys
import tarfile
import threading
import time
import unittest
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from test_capabilities import CapCase, Port  # noqa: E402
from test_capabilities_infra import FakeRelay  # noqa: E402
from src.pi.ai import Faux  # noqa: E402
from src.pi.caps import browser, extras, output  # noqa: E402
from src.pi.commands import Commands  # noqa: E402

PY = f'"{sys.executable}"'


class TestProcesses(CapCase):
    def test_start_wait_log_kill(self):
        s = self.session(capabilities=["processes"])
        notices = []
        s.subscribe(lambda ev: notices.append(ev.get("message")) if ev.get("type") == "notice" else None)
        (self.dir / "srv.py").write_text("import sys, time\nprint('booting', flush=True)\ntime.sleep(0.3)\nprint('listening on 8080', flush=True)\n"
                                         "for i in range(600):\n    time.sleep(0.1)\n", encoding="utf-8")
        p = lambda **kw: self.call(s, "process", kw)  # noqa: E731
        try:
            r = p(action="start", command=f"{PY} srv.py", name="server", notify="listening")
            pid = r["details"]["id"]
            self.assertTrue(r["details"]["running"])
            w = p(action="wait", id=pid, text="listening on", seconds=10)
            self.assertIn("'listening on' appeared", self.text(w))
            self.assertIn("booting\nlistening on 8080", self.text(w))
            self.assertTrue(any("[server] listening on 8080" in str(n) for n in notices))
            self.assertIn("running", self.text(p(action="list")))
            self.assertIn("listening on 8080", self.text(p(action="log", id="server", lines=5)))    # by name too
            self.assertIn("still running after 0.3s", self.text(p(action="wait", id=pid, seconds=0.3)))
            self.assertIn("stopped.", self.text(p(action="kill", id=pid)))
            self.assertIn("exited", self.text(p(action="list")))
            quick = p(action="start", command=f"{PY} -c \"print('done'); raise SystemExit(3)\"")
            done = p(action="wait", id=quick["details"]["id"], seconds=10)
            self.assertIn("exited 3", self.text(done))
            self.assertEqual(done["details"]["exitCode"], 3)
            self.assertIn("no process 'p99'", self.text(p(action="log", id="p99")))
            self.assertTrue(p(action="start", command=" ").get("isError"))
            port = Port()
            Commands(s, port).run("ps", "")
            self.assertIn("server", port.notes[-1][1])
        finally:
            s.shutdown()

    def test_session_end_stops_them_and_guards_apply(self):
        s = self.session(capabilities=["processes", "plan", "approvals"])
        r = self.call(s, "process", {"action": "start", "command": f"{PY} -c \"import time; time.sleep(60)\""})
        proc = s._processes[r["details"]["id"]]
        s.shutdown()
        self.assertFalse(proc.running)
        s2 = self.session(capabilities=["processes", "approvals"])
        self.assertIn("recursive delete needs the user's approval", s2.caps.gate("process", {"action": "start", "command": "rm -rf /tmp/x"}))
        self.assertIsNone(s2.caps.gate("process", {"action": "list"}))


class TestQuietOutputAndSecrets(CapCase):
    def test_compact(self):
        noisy = ("\x1b[32mResolving\x1b[0m packages\n" + "".join(f"Downloading pkg {i}/200 ({i * 3} kB)\n" for i in range(1, 201)) +
                 "\n\n\n\nprogress 10%\rprogress 55%\rprogress 100%\n" + "PASS a\nPASS b\nFAIL c: expected 1 got 2\n")
        out = output.compact(noisy)
        self.assertLess(len(out), len(noisy) // 10)
        self.assertEqual(out.split("\n")[:4], ["Resolving packages", "Downloading pkg 1/200 (3 kB)", "  [... 198 similar lines ...]",
                                               "Downloading pkg 200/200 (600 kB)"])
        self.assertIn("\n\nprogress 100%\n", out)
        self.assertNotIn("progress 10%", out)
        self.assertTrue(out.endswith("PASS a\nPASS b\nFAIL c: expected 1 got 2"))         # distinct lines are untouched
        self.assertEqual(output.compact("a\nb\nc"), "a\nb\nc")

    def test_bash_results_are_compacted_and_small_ones_left_alone(self):
        s = self.session(capabilities=["quiet-output"], tools="read,bash,edit,write,grep,find,ls")
        script = "for i in range(300): print(f'step {i} of 300 ok')\nprint('RESULT 42')"
        (self.dir / "noisy.py").write_text(script, encoding="utf-8")
        Faux.queue = [{"tool": "bash", "args": {"command": f"{PY} noisy.py"}}, {"tool": "bash", "args": {"command": "echo short"}}, {"text": "ok"}]
        s.prompt("run")
        res = [e["message"] for e in s.sessions.entries if e["type"] == "message" and e["message"]["role"] == "toolResult"]
        big = res[0]["content"][0]["text"]
        self.assertIn("[... 298 similar lines ...]", big)
        self.assertIn("RESULT 42", big)
        self.assertLess(len(big), 400)
        self.assertEqual(res[1]["content"][0]["text"].strip(), "short")

    def test_secrets_are_masked_before_the_model_sees_them(self):
        os.environ["OMNI_TEST_DEPLOY_TOKEN"] = "tok-9f8e7d6c5b4a3210"
        (self.agent / "secrets.txt").write_text("# mine\nDB_URL=postgres://u:hunter2secret@db/prod\n", encoding="utf-8")
        try:
            s = self.session(capabilities=["secrets"], tools="read,bash,edit,write,grep,find,ls")
            (self.dir / ".env").write_text("DEPLOY=tok-9f8e7d6c5b4a3210\nGH=ghp_" + "a1B2" * 9 + "\nDB=postgres://u:hunter2secret@db/prod\n"
                                           "AWS=AKIAIOSFODNN7EXAMPLE\nNAME=plain-value\n", encoding="utf-8")
            Faux.queue = [{"tool": "read", "args": {"path": ".env"}}, {"text": "read it"}]
            s.prompt("show the env file")
            res = [e["message"] for e in s.sessions.entries if e["type"] == "message" and e["message"]["role"] == "toolResult"][0]
            text = res["content"][0]["text"]
            self.assertEqual(text, "DEPLOY=[REDACTED:OMNI_TEST_DEPLOY_TOKEN]\nGH=[REDACTED:GITHUB_TOKEN]\nDB=[REDACTED:DB_URL]\n"
                                   "AWS=[REDACTED:AWS_ACCESS_KEY]\nNAME=plain-value")
            sent = json.dumps(Faux.requests[-1]["messages"])
            for secret in ("tok-9f8e7d6c5b4a3210", "hunter2secret", "AKIAIOSFODNN7EXAMPLE"):
                self.assertNotIn(secret, sent)
            self.assertNotIn("hunter2secret", (s.sessions.file or Path("x")).read_text(encoding="utf-8") if s.sessions.file else "")
            self.assertEqual(output.redact("-----BEGIN RSA PRIVATE KEY-----\nabc\n-----END RSA PRIVATE KEY-----", [])[0], "[REDACTED:PRIVATE_KEY]")
            self.assertEqual(output.redact("nothing here", [("X", "longsecretvalue")]), ("nothing here", 0))
        finally:
            os.environ.pop("OMNI_TEST_DEPLOY_TOKEN", None)


class TestInspect(CapCase):
    def test_sqlite_archives_notebook_csv(self):
        s = self.session(capabilities=["file-inspect"])
        i = lambda **kw: self.call(s, "inspect", kw)  # noqa: E731
        db = self.dir / "app.db"
        con = sqlite3.connect(db)
        con.execute("CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT, bio TEXT)")
        con.executemany("INSERT INTO users (name, bio) VALUES (?, ?)", [("ann", None), ("bob", "x" * 200)])
        con.commit()
        con.close()
        self.assertEqual(self.text(i(path="app.db")), "table users (2 rows)\n  id INTEGER, name TEXT, bio TEXT")
        rows = self.text(i(path="app.db", table="users"))
        self.assertTrue(rows.startswith("id | name | bio\n1 | ann | NULL\n2 | bob | xxx"))
        self.assertIn("...", rows)
        self.assertEqual(self.text(i(path="app.db", query="SELECT count(*) AS n FROM users")), "n\n2")
        self.assertIn("only SELECT", self.text(i(path="app.db", query="DELETE FROM users")))
        self.assertIn("no table 'nope'", self.text(i(path="app.db", table="nope")))
        with zipfile.ZipFile(self.dir / "b.zip", "w") as z:
            z.writestr("docs/readme.txt", "hello from the zip")
            z.writestr("data.bin", "x" * 10)
        listing = self.text(i(path="b.zip"))
        self.assertTrue(listing.startswith("2 entries, 28 bytes unpacked"))
        self.assertIn("docs/readme.txt", listing)
        self.assertEqual(self.text(i(path="b.zip", member="docs/readme.txt")), "hello from the zip")
        self.assertIn("no member", self.text(i(path="b.zip", member="missing")))
        with tarfile.open(self.dir / "c.tar.gz", "w:gz") as t:
            t.add(self.dir / "a.txt", arcname="pkg/a.txt")
        self.assertIn("pkg/a.txt", self.text(i(path="c.tar.gz")))
        self.assertIn("alpha", self.text(i(path="c.tar.gz", member="pkg/a.txt")))
        (self.dir / "n.ipynb").write_text(json.dumps({"cells": [
            {"cell_type": "markdown", "source": ["# Title\n", "text"]},
            {"cell_type": "code", "source": "print(1+1)", "outputs": [{"output_type": "stream", "text": ["2\n"]},
                                                                      {"output_type": "display_data", "data": {"image/png": "AAAA"}}]}]}))
        nb = self.text(i(path="n.ipynb"))
        self.assertEqual(nb, "--- [1] markdown ---\n# Title\ntext\n--- [2] code ---\nprint(1+1)\n[output]\n2\n[image/png output not shown]")
        (self.dir / "d.csv").write_text("name;age\nann;31\nbob;45\n", encoding="utf-8")
        self.assertEqual(self.text(i(path="d.csv")), "2 data rows, 2 columns\nname | age\nann | 31\nbob | 45")
        self.assertIn("use read for text", self.text(i(path="a.txt")))
        self.assertTrue(s.all_tools()["inspect"].read_only)


class TestKeywordsFeedsDiff(CapCase):
    def test_keywords(self):
        (self.profiles / "kw.json").write_text(json.dumps({"name": "kw", "extends": "lean", "capabilities": ["keywords"],
                                                             "keywords": {"shipit": "Run the tests, then commit."}}))
        s = self.session(profile="kw")
        Faux.queue = [{"text": "a"}, {"text": "b"}, {"text": "c"}]
        s.prompt("please ULTRATHINK about this bug")
        s.prompt("just rename it, chillax and shipit")
        s.prompt("the ultrathinking word and chillaxed should not trigger")
        users = [e["message"]["content"] for e in s.sessions.entries if e["type"] == "message" and e["message"]["role"] == "user"]
        self.assertIn("[ultrathink] Take this slowly and carefully.", users[0])
        self.assertIn("[chillax] Keep this quick", users[1])
        self.assertIn("[shipit] Run the tests, then commit.", users[1])
        self.assertEqual(users[2], "the ultrathinking word and chillaxed should not trigger")
        self.assertEqual(Faux.requests[0]["options"]["reasoning"], "max")            # raised for that request ...
        self.assertNotEqual(Faux.requests[1]["options"]["reasoning"], "max") if s.thinking_level != "max" else None
        self.assertEqual(extras._on_prompt(s, "/review ultrathink"), None)            # commands are left alone

    def test_feeds_report_only_new_items(self):
        state = {"items": ["One", "Two"]}

        class H(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                if self.path == "/atom":
                    body = ('<feed xmlns="http://www.w3.org/2005/Atom"><entry><title>Release 2.0</title><id>tag:r2</id>'
                            '<link rel="alternate" href="https://ex.test/r2"/><updated>2026-11-13T10:00:00Z</updated></entry></feed>')
                else:
                    items = "".join(f"<item><title>{t}</title><link>https://ex.test/{t.lower()}</link><guid>{t}</guid>"
                                    f"<pubDate>Fri, 13 Nov 2026</pubDate></item>" for t in reversed(state["items"]))
                    body = f"<rss><channel><title>Blog</title>{items}</channel></rss>"
                self.send_response(200)
                self.send_header("Content-Type", "application/xml")
                self.end_headers()
                self.wfile.write(body.encode())
        srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{srv.server_address[1]}"
        try:
            s = self.session(capabilities=["feeds"])
            f = lambda **kw: self.call(s, "feeds", kw)  # noqa: E731
            self.assertIn("Following blog (2 existing items marked as seen)", self.text(f(action="add", url=base + "/rss", name="Blog")))
            self.assertEqual(self.text(f(action="check")), "Nothing new.")
            state["items"] += ["Three", "Four"]
            new = f(action="check", name="blog")
            self.assertEqual(new["details"]["new"], 2)
            self.assertTrue(self.text(new).startswith("blog — 2 new\n  Four (Fri, 13 Nov 2026)\n    https://ex.test/four\n  Three"))
            self.assertEqual(self.text(f(action="check")), "Nothing new.")            # remembered
            f(action="add", url=base + "/atom", name="rel")
            self.assertEqual(extras.parse_feed("<feed xmlns='http://www.w3.org/2005/Atom'><entry><title>T</title>"
                                               "<link href='https://x/1'/></entry></feed>"), [{"id": "https://x/1", "title": "T", "link": "https://x/1", "date": ""}])
            self.assertIn("rel  " + base + "/atom", self.text(f(action="list")))
            self.assertIn("Stopped following rel", self.text(f(action="remove", name="rel")))
            self.assertTrue(f(action="add", url="ftp://nope").get("isError"))
            self.assertTrue(f(action="check", name="ghost").get("isError"))
            self.assertEqual(sorted(json.loads((self.agent / "feeds.json").read_text())), ["blog"])
        finally:
            srv.shutdown()
            srv.server_close()

    def test_diff_command(self):
        s = self.session(capabilities=["workflows"])
        port = Port()
        cmds = Commands(s, port)
        cmds.run("diff", "")
        self.assertEqual(port.notes[-1][1], "Not a git repository.")
        git = lambda *a: subprocess.run(["git", *a], cwd=self.dir, capture_output=True, text=True)  # noqa: E731
        if git("init", "-q").returncode != 0:
            self.skipTest("git not available")
        git("config", "user.email", "t@example.com")
        git("config", "user.name", "t")
        git("add", "a.txt")
        git("commit", "-qm", "init")
        cmds.run("diff", "")
        self.assertIn("Untracked files:\n  + src/", port.notes[-1][1])
        (self.dir / "a.txt").write_text("alpha\nBETA\ngamma\n", encoding="utf-8")
        cmds.run("diff", "")
        out = port.notes[-1][1]
        self.assertIn("a.txt | 2 +-", out)
        self.assertIn("-beta\n+BETA", out)
        cmds.run("diff", "staged")
        self.assertEqual(port.notes[-1][1], "No changes staged.")


class TestBrowserAllowlist(CapCase):
    def test_only_allowed_sites(self):
        relay = FakeRelay()
        relay.connected = True
        make = browser.make_relay
        browser.make_relay = lambda _s: relay
        try:
            (self.profiles / "br.json").write_text(json.dumps({"name": "br", "extends": "lean", "capabilities": ["browser"],
                                                                 "browser": {"allowed_domains": ["*.example", "docs.python.org"]}}))
            s = self.session(profile="br")
            ok = self.call(s, "browser", {"action": "open", "url": "app.example/login"})
            self.assertFalse(ok.get("isError"), self.text(ok))
            bad = self.call(s, "browser", {"action": "open", "url": "https://evil.test/steal"})
            self.assertIn("evil.test is not in this profile's allowed browser domains", self.text(bad))
            self.assertNotIn(("navigate", "https://evil.test/steal"), relay.log)
            relay.url = "https://elsewhere.test/redirected"               # a click led off-site
            off = self.call(s, "browser", {"action": "state"})
            self.assertTrue(off.get("isError"))
            self.assertTrue(any(e[0] == "js" and "history.back" in e[1] for e in relay.log))
        finally:
            browser.make_relay = make


if __name__ == "__main__":
    unittest.main()
