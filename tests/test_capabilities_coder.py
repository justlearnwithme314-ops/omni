"""Coding capabilities ported from Oh My Pi: checkpoint/rewind, hashline edits, ast_grep/ast_edit, plan mode."""
import json
import os
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from test_capabilities import CapCase, Port  # noqa: E402
from src.pi.ai import Faux  # noqa: E402
from src.pi.caps import hashline as hl  # noqa: E402
from src.pi.caps import plan as planmod  # noqa: E402
from src.pi.commands import Commands  # noqa: E402
from src.pi.modes import HeadlessUI  # noqa: E402
from src.pi.tools.base import ToolError  # noqa: E402

FAKE_AST_GREP = r'''
import json, re, sys
a = sys.argv[1:]
pat = a[a.index("-p") + 1]
rew = a[a.index("-r") + 1] if "-r" in a else None
path = a[-1]
if pat == "((":
    sys.stderr.write("Error: Cannot parse query as a valid pattern.\n"); sys.exit(2)
import os
files = [os.path.join(d, f) for d, _, fs in os.walk(path) for f in fs] if os.path.isdir(path) else [path]
rx = re.compile(re.escape(pat).replace(r"\$\$\$ARGS", "(.*)"))
for f in sorted(files):
    src = open(f, encoding="utf-8").read()
    out = src
    for i, line in enumerate(src.split("\n")):
        m = rx.search(line)
        if not m:
            continue
        rec = {"file": f, "text": m.group(0), "lines": line, "range": {"start": {"line": i, "column": m.start()}}}
        if rew is not None:
            rec["replacement"] = rew.replace("$$$ARGS", m.group(1))
            out = out.replace(m.group(0), rec["replacement"])
        print(json.dumps(rec))
    if rew is not None and "--update-all" in a and out != src:
        open(f, "w", encoding="utf-8").write(out)
'''


class TestCheckpoint(CapCase):
    def test_rewind_removes_exploration_from_context_but_not_from_the_session(self):
        s = self.session(capabilities=["checkpoint"], tools="read,bash,edit,write,grep,find,ls")
        (self.dir / "big.txt").write_text("needle-in-haystack\n" + "filler line\n" * 300, encoding="utf-8")
        Faux.queue = [
            {"tool": "checkpoint", "args": {"goal": "find where the needle is"}},
            {"tool": "read", "args": {"path": "big.txt"}},
            {"tool": "grep", "args": {"pattern": "needle"}},
            {"tool": "rewind", "args": {"report": "The needle is on line 1 of big.txt."}},
            {"text": "Found it."},
        ]
        s.prompt("where is the needle?")
        before = Faux.requests[3]["messages"]                 # the request that produced the rewind call
        after = Faux.requests[4]["messages"]                  # the next one
        self.assertIn("filler line", json.dumps(before))
        self.assertNotIn("filler line", json.dumps(after))    # the read result left the context
        self.assertIn("The needle is on line 1 of big.txt.", json.dumps(after))
        self.assertEqual([m["role"] for m in after], ["user", "assistant", "toolResult", "assistant", "toolResult"])
        self.assertLess(len(json.dumps(after)), len(json.dumps(before)) // 2)
        result = after[-1]
        self.assertIn("4 messages", result["content"][0]["text"])
        entries = s.sessions.entries
        self.assertTrue(any("filler line" in json.dumps(e) for e in entries))   # still in the file / transcript
        self.assertEqual(sum(1 for e in entries if e["type"] == "context_edit"), 4)
        s.prompt("thanks")                                    # later turns keep the saving
        self.assertNotIn("filler line", json.dumps(Faux.requests[-1]["messages"]))

    def test_rules(self):
        s = self.session(capabilities=["checkpoint"])
        self.assertIn("no active checkpoint", self.text(self.call(s, "rewind", {"report": "x"})))
        self.call(s, "checkpoint", {"goal": "a"})
        self.assertIn("already active", self.text(self.call(s, "checkpoint", {"goal": "b"})))
        self.assertTrue(self.call(s, "rewind", {"report": " "}).get("isError"))
        self.assertIn("Report kept", self.text(self.call(s, "rewind", {"report": "done"})))
        self.assertFalse(self.call(s, "checkpoint", {"goal": "again"}).get("isError"))  # reusable after a rewind


class TestHashline(CapCase):
    SRC = "def a():\n    return 1\n\ndef b():\n    return 2\n\nprint(a() + b())\n"

    def setUp(self):
        super().setUp()
        (self.dir / "m.py").write_text(self.SRC, encoding="utf-8")
        self.tag = hl.tag_of(self.SRC)

    def edit(self, s, patch):
        return self.call(s, "edit", {"input": patch})

    def test_read_shows_tag_and_numbers_and_edit_applies_by_line(self):
        s = self.session(capabilities=["hashline"])
        self.assertEqual(s.all_tools()["edit"].source, "capability:hashline")
        self.assertEqual(list(s.all_tools()["edit"].parameters["properties"]), ["input"])
        r = self.text(self.call(s, "read", {"path": "m.py"}))
        self.assertEqual(r, f"[m.py#{self.tag}]\n1|def a():\n2|    return 1\n3|\n4|def b():\n5|    return 2\n6|\n7|print(a() + b())")
        part = self.text(self.call(s, "read", {"path": "m.py", "offset": 4, "limit": 2}))
        self.assertTrue(part.startswith(f"[m.py#{self.tag}]\n4|def b():\n5|    return 2\n\n[2 more lines"), part)
        out = self.edit(s, f"*** Begin Patch\n[m.py#{self.tag}]\nPUT 2:\n+    return 10\nPUT >5:\n+\n+def c():\n+    return 3\nPUT 7.=7:\n+print(a() + b() + c())\n*** End Patch")
        new = "def a():\n    return 10\n\ndef b():\n    return 2\n\ndef c():\n    return 3\n\nprint(a() + b() + c())\n"
        self.assertEqual((self.dir / "m.py").read_text(encoding="utf-8"), new)
        t = self.text(out)
        self.assertTrue(t.startswith(f"Updated [m.py#{hl.tag_of(new)}] (7 -> 10 lines, 5 written)"), t)
        self.assertIn("2|    return 10", t)        # changed lines come back renumbered: no re-read needed
        self.assertIn("10|print(a() + b() + c())", t)
        again = self.edit(s, f"[m.py#{self.tag}]\nPUT 1:\n+x")  # the old tag is now stale
        self.assertTrue(again.get("isError"))
        self.assertIn("does not match the file", self.text(again))
        self.assertEqual((self.dir / "m.py").read_text(encoding="utf-8"), new)

    def test_cut_paste_create_delete_move_and_crlf(self):
        s = self.session(capabilities=["hashline"])
        self.edit(s, f"[m.py#{self.tag}]\nCUT 4.=6 @fn\nPUT <1 @fn")
        self.assertEqual((self.dir / "m.py").read_text(encoding="utf-8"),
                         "def b():\n    return 2\n\ndef a():\n    return 1\n\nprint(a() + b())\n")
        self.assertIn("Created [new/n.txt#", self.text(self.edit(s, "[new/n.txt#NEW]\nPUT >$:\n+one\n+\n+three")))
        self.assertEqual((self.dir / "new" / "n.txt").read_text(encoding="utf-8"), "one\n\nthree\n")
        (self.dir / "w.txt").write_bytes(b"a\r\nb\r\nc\r\n")
        tag = hl.tag_of("a\nb\nc\n")
        self.edit(s, f"[w.txt#{tag}]\nPUT 2:\n+B\nMV w2.txt")
        self.assertFalse((self.dir / "w.txt").exists())
        self.assertEqual((self.dir / "w2.txt").read_bytes(), b"a\r\nB\r\nc\r\n")   # CRLF preserved
        self.edit(s, f"[w2.txt#{hl.tag_of(chr(10).join(['a', 'B', 'c']) + chr(10))}]\nREM")
        self.assertFalse((self.dir / "w2.txt").exists())

    def test_patch_errors_are_specific(self):
        s = self.session(capabilities=["hashline"])
        cases = {
            f"[m.py#{self.tag}]\nPUT 2:\n-    return 1\n+    return 9": "never removed",
            f"[m.py#{self.tag}]\nPUT 4*:\n+x": "block operations",
            f"[m.py#{self.tag}]\nPUT 9.=12:\n+x": "outside the file (1-7)",
            f"[m.py#{self.tag}]\nPUT 1.=3:\n+x\nPUT 2.=4:\n+y": "overlap",
            f"[m.py#{self.tag}]\nPUT >2 @nope": "register @nope is empty",
            "PUT 1:\n+x": "expected a `[path#TAG]` header",
            "[ghost.py#ABCD]\nPUT 1:\n+x": "does not exist",
            f"[m.py]\nPUT 1:\n+x": "(missing)",
            f"[m.py#{self.tag}]\nFROB 1": "unknown operation",
        }
        for patch, expect in cases.items():
            r = self.edit(s, patch)
            self.assertTrue(r.get("isError"), patch)
            self.assertIn(expect, self.text(r), patch)
        self.assertEqual((self.dir / "m.py").read_text(encoding="utf-8"), self.SRC)   # nothing was written

    def test_core_read_and_edit_unchanged(self):
        core = self.session(profile="core")
        self.assertIn("edits", core.all_tools()["edit"].parameters["properties"])  # Pi's edit when the capability is off
        self.assertFalse(self.text(self.call(core, "read", {"path": "m.py"})).startswith("["))


class TestAst(CapCase):
    def setUp(self):
        super().setUp()
        fake = self.dir / "fake_ast_grep.py"
        fake.write_text(FAKE_AST_GREP, encoding="utf-8")
        self._old = os.environ.get("OMNI_AST_GREP")
        os.environ["OMNI_AST_GREP"] = f'"{sys.executable}" "{fake}"' if os.name == "nt" else f"{sys.executable} {fake}"
        (self.dir / "pkg").mkdir()
        (self.dir / "pkg" / "x.js").write_text("console.log(1, 2)\nfoo()\nconsole.log('a')\n", encoding="utf-8")

    def tearDown(self):
        if self._old is None:
            os.environ.pop("OMNI_AST_GREP", None)
        else:
            os.environ["OMNI_AST_GREP"] = self._old
        super().tearDown()

    def test_search_preview_and_rewrite(self):
        s = self.session(capabilities=["ast"])
        self.assertEqual(sorted(n for n, o in s.caps.owner.items() if o == "ast"), ["ast_edit", "ast_grep"])
        r = self.call(s, "ast_grep", {"pat": "console.log($$$ARGS)", "lang": "javascript", "path": "pkg"})
        rows = self.text(r).split("\n")
        self.assertEqual(len(rows), 2)
        self.assertTrue(rows[0].endswith("pkg/x.js:1: console.log(1, 2)"), rows[0])
        self.assertEqual(r["details"], {"matches": 2, "files": 1})
        self.assertIn("No matches", self.text(self.call(s, "ast_grep", {"pat": "nothing()", "path": "pkg"})))
        bad = self.call(s, "ast_grep", {"pat": "((", "path": "pkg"})
        self.assertTrue(bad.get("isError"))
        self.assertIn("query error", self.text(bad))
        dry = self.call(s, "ast_edit", {"pat": "console.log($$$ARGS)", "rewrite": "log.info($$$ARGS)", "path": "pkg", "dry_run": True})
        self.assertIn("Would rewrite 2 match(es) in 1 file(s)", self.text(dry))
        self.assertIn("console.log(1, 2)  ->  log.info(1, 2)", self.text(dry))
        self.assertIn("console.log", (self.dir / "pkg" / "x.js").read_text(encoding="utf-8"))
        done = self.call(s, "ast_edit", {"pat": "console.log($$$ARGS)", "rewrite": "log.info($$$ARGS)", "path": "pkg"})
        self.assertIn("Rewrote 2 match(es)", self.text(done))
        self.assertEqual((self.dir / "pkg" / "x.js").read_text(encoding="utf-8"), "log.info(1, 2)\nfoo()\nlog.info('a')\n")
        self.assertTrue(self.call(s, "ast_edit", {"pat": "a", "rewrite": "b"}).get("isError"))  # path is mandatory

    def test_missing_binary_is_quiet_and_free(self):
        os.environ["OMNI_AST_GREP"] = ""
        from src.pi.caps import ast as astmod
        real = astmod.shutil.which
        astmod.shutil.which = lambda _n: None
        try:
            s = self.session(capabilities=["ast", "todo"])
        finally:
            astmod.shutil.which = real
        self.assertEqual(s.caps.enabled, ["todo"])
        self.assertIn("not installed", s.caps.problems["ast"])
        self.assertNotIn("ast_grep", s.active_tool_names)
        self.assertFalse(any("ast" in d for d in s.diagnostics))    # no startup warning for an optional program


class TestPlanMode(CapCase):
    def test_gate_blocks_changes_until_the_plan_is_approved(self):
        s = self.session(capabilities=["plan", "todo"], tools="read,bash,edit,write,grep,find,ls")
        port = Port()
        cmds = Commands(s, port)
        self.assertNotIn("PLAN MODE", s.system_prompt())
        cmds.cmd_plan("")
        self.assertTrue(planmod.is_on(s))
        self.assertIn("PLAN MODE IS ACTIVE", s.system_prompt())
        Faux.queue = [
            {"tool": "write", "args": {"path": "z.txt", "content": "x"}},
            {"tool": "bash", "args": {"command": "echo hi > z.txt"}},
            {"tool": "bash", "args": {"command": "git status && ls"}},
            {"tool": "read", "args": {"path": "a.txt"}},
            {"tool": "exit_plan_mode", "args": {"plan": "1. write z.txt"}},
            {"tool": "write", "args": {"path": "z.txt", "content": "x"}},
            {"text": "done"},
        ]
        s.prompt("make z.txt")
        results = [e["message"] for e in s.sessions.entries if e["type"] == "message" and e["message"]["role"] == "toolResult"]
        texts = [m["content"][0]["text"] for m in results]
        self.assertIn("`write` is blocked until the plan is approved", texts[0])
        self.assertIn("could change something", texts[1])
        self.assertNotIn("Plan mode", texts[2])              # read-only shell is fine
        self.assertIn("alpha", texts[3])
        self.assertIn("plan mode is off", texts[4])          # headless: recorded, auto-approved
        self.assertTrue((self.dir / "z.txt").exists())       # the write after approval went through
        self.assertFalse(planmod.is_on(s))
        self.assertNotIn("PLAN MODE", s.system_prompt())
        self.assertTrue(any(e.get("customType") == "plan" for e in s.sessions.entries))

    def test_interactive_approval_and_feedback(self):
        s = self.session(capabilities=["plan"])

        class UI(HeadlessUI):
            has_ui = True
            pick, typed = "Keep planning", "add tests"

            def ext_select(self, title="", options=None, **_):
                return self.pick

            def ext_input(self, title="", placeholder="", **_):
                return self.typed
        s.ui = UI()
        planmod.set_mode(s, True)
        r = self.call(s, "exit_plan_mode", {"plan": "do it"})
        self.assertIn("Feedback: add tests", self.text(r))
        self.assertTrue(planmod.is_on(s))
        s.ui.pick = "Approve and start"
        self.assertIn("approved the plan", self.text(self.call(s, "exit_plan_mode", {"plan": "do it + tests"})))
        self.assertFalse(planmod.is_on(s))

    def test_shell_classifier_and_command_without_capability(self):
        ok = ["ls -la", "git status", "git log --oneline | head -5", "rg TODO src && wc -l a.txt", "cat a.txt"]
        bad = ["rm -rf x", "echo a > b", "git commit -m x", "npm install", "python build.py", "ls; touch x", "sed -i s/a/b/ f"]
        self.assertEqual([c for c in ok if not planmod.bash_is_read_only(c)], [])
        self.assertEqual([c for c in bad if planmod.bash_is_read_only(c)], [])
        s = self.session(profile="core")
        port = Port()
        Commands(s, port).cmd_plan("on")
        self.assertTrue(any(l == "warning" and "/capabilities plan on" in t for l, t in port.notes))
        self.assertFalse(planmod.is_on(s))


if __name__ == "__main__":
    unittest.main()
