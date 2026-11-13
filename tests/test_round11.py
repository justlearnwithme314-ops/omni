"""Rules (always-on, listed, and fired when the output matches) and the webhook delivery channel. Offline."""
import http.server
import json
import sys
import threading
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from test_capabilities import CapCase, Port  # noqa: E402
from src.pi import gateway as gw  # noqa: E402
from src.pi.ai import Faux  # noqa: E402
from src.pi.caps import rules, scheduler  # noqa: E402
from src.pi.commands import Commands  # noqa: E402


class TestRules(CapCase):
    def setUp(self):
        super().setUp()
        d = self.agent / "rules"
        d.mkdir()
        (d / "no-any.md").write_text("---\nname: no-any\ndescription: no any in TypeScript\ncondition: \":\\\\s*any\\\\b|as any\\\\b\"\n---\n"
                                     "Do not use `any`. Use a precise type or `unknown`.\n", encoding="utf-8")
        (d / "tone.md").write_text("---\nname: tone\nalwaysApply: true\n---\nAnswer in plain English.\n", encoding="utf-8")
        (d / "db.md").write_text("---\nname: db-migrations\ndescription: how to write migrations here\n---\nUse alembic.\n", encoding="utf-8")
        (d / "broken.md").write_text("---\nname: broken\ncondition: \"(unclosed\"\n---\nbody\n", encoding="utf-8")

    def test_prompt_holds_only_what_it_must(self):
        s = self.session(capabilities=["rules"])
        sp = s.system_prompt()
        self.assertIn("## Rule: tone\nAnswer in plain English.", sp)
        self.assertIn("- db-migrations: how to write migrations here", sp)
        self.assertNotIn("Use alembic.", sp)                       # listed rules are read on demand
        self.assertNotIn("Do not use `any`", sp)                   # conditional rules cost nothing until they fire
        self.assertTrue(any("rule broken: invalid condition" in d for d in s.diagnostics))
        port = Port()
        Commands(s, port).run("rules", "")
        out = port.notes[-1][1]
        self.assertIn("no-any — fires on /", out)
        self.assertIn("tone — always in the prompt", out)
        self.assertIn("db-migrations — listed, read on demand", out)
        self.assertIn("INVALID condition", out)

    def test_rule_fires_as_a_correction_once(self):
        s = self.session(capabilities=["rules"], tools="read,bash,edit,write,grep,find,ls")
        notices = []
        s.subscribe(lambda ev: notices.append(ev.get("message")) if ev.get("type") == "notice" else None)
        Faux.queue = [{"tool": "write", "args": {"path": "x.ts", "content": "export function f(a: any) { return a }\n"}},
                      {"tool": "write", "args": {"path": "x.ts", "content": "export function f(a: unknown) { return a }\n"}},
                      {"text": "Fixed: no more any."},
                      {"text": "Here is another: const y = z as any"}, {"text": "ok"}]
        s.prompt("write x.ts")
        msgs = [e["message"] for e in s.sessions.entries if e["type"] == "message"]
        roles = [m["role"] for m in msgs]
        self.assertEqual(roles, ["user", "assistant", "toolResult", "user", "assistant", "toolResult", "assistant"])
        correction = msgs[3]["content"]
        self.assertIn("[Rule `no-any` applies to what you just wrote (matched: ': any')", correction)
        self.assertIn("Do not use `any`. Use a precise type or `unknown`.", correction)
        self.assertEqual((self.dir / "x.ts").read_text(encoding="utf-8"), "export function f(a: unknown) { return a }\n")
        self.assertEqual(rules.fired(s), ["no-any"])
        self.assertTrue(any("Rule no-any fired" in str(n) for n in notices))
        s.prompt("show me another")                                 # breaks it again: fires once per session only
        self.assertEqual(rules.fired(s), ["no-any"])
        self.assertEqual([e["message"]["role"] for e in s.sessions.entries if e["type"] == "message"][-2:], ["user", "assistant"])
        port = Port()
        Commands(s, port).run("rules", "")
        self.assertIn("no-any — fires on", port.notes[-1][1])
        self.assertIn("fired 1×", port.notes[-1][1])

    def test_project_rules_need_trust_and_core_is_untouched(self):
        d = self.dir / ".omni" / "rules"
        d.mkdir(parents=True)
        (d / "proj.md").write_text("---\nname: proj\nalwaysApply: true\n---\nPROJECT-RULE\n", encoding="utf-8")
        self.assertNotIn("PROJECT-RULE", self.session(capabilities=["rules"], approve=False).system_prompt())
        self.assertIn("PROJECT-RULE", self.session(capabilities=["rules"], approve=True).system_prompt())
        self.assertNotIn("Answer in plain English", self.session(profile="core").system_prompt())


class TestWebhookChannel(CapCase):
    def test_job_results_go_to_a_webhook(self):
        got = []

        class H(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                n = int(self.headers.get("Content-Length") or 0)
                got.append((self.path, json.loads(self.rfile.read(n))))
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"ok")
        srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{srv.server_address[1]}"
        try:
            gw.save_config(self.agent, {"channels": {"alerts": {"type": "webhook", "url": base + "/slack"},
                                                     "disc": {"type": "webhook", "url": base + "/discord/hook", "format": "discord"},
                                                     "bad": {"type": "webhook", "url": "not-a-url"}}})
            self.assertIsNone(gw.deliver(self.agent, "alerts:ops", "hello"))
            self.assertIsNone(gw.deliver(self.agent, "disc:x", "hi there"))
            self.assertEqual(got, [("/slack", {"text": "hello"}), ("/discord/hook", {"content": "hi there"})])
            self.assertIn("not an http(s) URL", gw.deliver(self.agent, "bad:x", "x"))
            (self.profiles / "sc.json").write_text(json.dumps({"name": "sc", "extends": "core", "capabilities": ["scheduler"]}))
            scheduler.create_job(self.agent, "nightly", "report", "every 1h", str(self.dir), "sc", "faux/faux-1", deliver="alerts:ops")
            Faux.queue = [{"text": "All green."}]
            res = scheduler.run_due(self.agent, now=time.time() + 4000)
            self.assertTrue(res[0]["delivered"])
            self.assertEqual(got[-1], ("/slack", {"text": "⏰ nightly (ok)\n\nAll green."}))
            g = gw.Gateway(self.agent)                               # in the running gateway it is a send-only channel
            try:
                self.assertEqual(sorted(g.channels), ["alerts", "disc"])
                self.assertEqual(len(g.errors), 1)
            finally:
                g.stop()
        finally:
            srv.shutdown()
            srv.server_close()


if __name__ == "__main__":
    unittest.main()
