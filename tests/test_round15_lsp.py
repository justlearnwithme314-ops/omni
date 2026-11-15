"""The `lsp` capability against a small fake language server (JSON-RPC over stdio). Offline."""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from test_capabilities import CapCase  # noqa: E402
from src.pi import caps  # noqa: E402
from src.pi.caps import lsp  # noqa: E402

caps.load_builtin()                      # builtins first, so registering here works with or without later wiring
for _cap in lsp.CAPABILITIES:
    caps.register(_cap)

FAKE_SERVER = r'''
import json, pathlib, re, sys

# switches: --pull (pull diagnostics instead of publishing), --encoding=utf-32 (if the client offers it),
#           --die=METHOD (write 25 lines to stderr and exit 3 when METHOD arrives)
ARGS = sys.argv[1:]
PULL = "--pull" in ARGS
WANT = next((a.split("=", 1)[1] for a in ARGS if a.startswith("--encoding=")), None)
DIE = next((a.split("=", 1)[1] for a in ARGS if a.startswith("--die=")), None)
ENC = "utf-16"
OUTSIDE = pathlib.Path(__file__).resolve().with_name("outside.txt").as_uri()

inp, out = sys.stdin.buffer, sys.stdout.buffer
docs = {}


def send(msg):
    body = json.dumps(msg).encode("utf-8")
    out.write(b"Content-Length: %d\r\n\r\n" % len(body) + body)
    out.flush()


def read():
    length = 0
    while True:
        line = inp.readline()
        if not line:
            return None
        line = line.strip()
        if not line:
            break
        if line.lower().startswith(b"content-length:"):
            length = int(line.split(b":")[1])
    return json.loads(inp.read(length).decode("utf-8"))


def units(text, col):
    return col if ENC == "utf-32" else len(text[:col].encode("utf-16-le")) // 2


def chars(text, n):
    if ENC == "utf-32":
        return n
    seen = 0
    for i, ch in enumerate(text):
        if seen >= n:
            return i
        seen += 2 if ord(ch) > 0xFFFF else 1
    return len(text)


def rows(uri):
    return docs[uri]["text"].splitlines()


def rng(uri, line, col, n):
    text = rows(uri)[line] if line < len(rows(uri)) else ""
    return {"start": {"line": line, "character": units(text, col)}, "end": {"line": line, "character": units(text, col + n)}}


def word_at(uri, pos):
    line = rows(uri)[pos["line"]]
    col = chars(line, pos["character"])
    for m in re.finditer(r"\w+", line):
        if m.start() <= col <= m.end():
            return m.group(0)
    return ""


def occurrences(uri, word):
    found = []
    for i, line in enumerate(rows(uri)):
        for m in re.finditer(r"\b%s\b" % re.escape(word), line):
            found.append(rng(uri, i, m.start(), len(word)))
    return found


def declarations(uri):
    for i, line in enumerate(rows(uri)):
        m = re.match(r"(\s*)(def|class)\s+(\w+)", line)
        if m:
            yield i, m.start(3), m.group(2), m.group(3), bool(m.group(1))


def problems(uri):
    doc = docs[uri]
    return [{"range": rng(uri, i, line.index("BUG"), 3), "severity": 1, "source": "fake-pull" if PULL else "fake", "code": "B1",
             "message": "bug marker (v%d)" % doc["version"]}
            for i, line in enumerate(rows(uri)) if "BUG" in line]


def publish(uri):
    if not PULL:
        send({"jsonrpc": "2.0", "method": "textDocument/publishDiagnostics", "params": {"uri": uri, "diagnostics": problems(uri)}})


def insert(uri, line, col, text):
    return {"range": rng(uri, line, col, 0), "newText": text}


while True:
    msg = read()
    if msg is None:
        break
    method, params, rid = msg.get("method"), msg.get("params") or {}, msg.get("id")
    uri = (params.get("textDocument") or {}).get("uri")
    result = None
    if DIE and method == DIE:
        for n in range(1, 26):
            sys.stderr.write("fake: line %d\n" % n)
        sys.stderr.flush()
        sys.exit(3)
    if method == "initialize":
        client = params.get("capabilities") or {}
        caps = {"textDocumentSync": 1, "definitionProvider": True, "codeActionProvider": {"resolveProvider": True},
                "documentFormattingProvider": True}
        if PULL and "diagnostic" in (client.get("textDocument") or {}):
            caps["diagnosticProvider"] = {"interFileDependencies": False, "workspaceDiagnostics": False}
        if WANT and WANT in ((client.get("general") or {}).get("positionEncodings") or []):
            ENC = caps["positionEncoding"] = WANT
        result = {"capabilities": caps, "serverInfo": {"name": "fake"}}
    elif method == "textDocument/didOpen":
        docs[uri] = {"text": params["textDocument"]["text"], "version": params["textDocument"]["version"]}
        publish(uri)
    elif method == "textDocument/didChange":
        docs[uri] = {"text": params["contentChanges"][0]["text"], "version": params["textDocument"]["version"]}
        publish(uri)
    elif method == "textDocument/diagnostic":
        state = "r%d" % docs[uri]["version"]
        if params.get("previousResultId") == state:
            result = {"kind": "unchanged", "resultId": state}
        else:
            result = {"kind": "full", "resultId": state, "items": problems(uri)}
    elif method == "textDocument/definition":
        word = word_at(uri, params["position"])
        result = [{"uri": uri, "range": rng(uri, i, col, len(name))} for i, col, _k, name, _n in declarations(uri) if name == word]
    elif method == "textDocument/references":
        result = [{"uri": uri, "range": r} for r in occurrences(uri, word_at(uri, params["position"]))]
    elif method == "textDocument/hover":
        pos = params["position"]
        word = word_at(uri, pos)
        result = {"contents": {"kind": "plaintext", "value": "hover: %s @ %d:%d" % (word, pos["line"], pos["character"])}} if word else None
    elif method == "textDocument/documentSymbol":
        result = []
        for i, col, kind, name, nested in declarations(uri):
            sym = {"name": name, "kind": 5 if kind == "class" else 12, "range": rng(uri, i, 0, 1),
                   "selectionRange": rng(uri, i, col, len(name)), "children": []}
            (result[-1]["children"] if nested and result else result).append(sym)
    elif method == "workspace/symbol":
        result = [{"name": name, "kind": 5 if kind == "class" else 12, "location": {"uri": u, "range": rng(u, i, col, len(name))}}
                  for u in docs for i, col, kind, name, _n in declarations(u) if params["query"].lower() in name.lower()]
    elif method == "textDocument/rename":
        word = word_at(uri, params["position"])
        result = {"changes": {uri: [{"range": r, "newText": params["newName"]} for r in occurrences(uri, word)]}} if word else None
    elif method == "textDocument/codeAction":
        start, end = params["range"]["start"], params["range"]["end"]
        ln = start["line"]
        here = rows(uri)[ln]
        sibling = uri.rsplit("/", 1)[0] + "/other.py"
        result = [{"title": "Replace BUG marker", "kind": "quickfix", "isPreferred": True, "diagnostics": [d],
                   "edit": {"changes": {uri: [{"range": d["range"], "newText": "FIXED"}]}}}
                  for d in params["context"]["diagnostics"]]
        result += [
            {"title": "Add header", "kind": "refactor", "edit": {"documentChanges": [
                {"textDocument": {"uri": uri, "version": docs[uri]["version"]},
                 "edits": [insert(uri, 0, 0, "# one\n"), insert(uri, 0, 0, "# two\n"), insert(uri, ln, len(here), "  # seen")]}]}},
            {"title": "Run organizer", "command": "fake.organize", "arguments": [uri]},
            {"title": "Touch outside", "kind": "quickfix", "edit": {"changes": {OUTSIDE: [insert(uri, 0, 0, "x")]}}},
            {"title": "Lazy fix", "kind": "quickfix", "data": {"uri": uri, "line": ln}},
            {"title": "Two files", "kind": "refactor", "edit": {"changes": {uri: [insert(uri, 0, 0, "# a\n")],
                                                                            sibling: [insert(uri, 0, 0, "# b\n")]}}},
            {"title": "Inline it", "kind": "refactor.inline", "disabled": {"reason": "not here"}}]
        if start == end:
            result.append({"title": "Cursor at %d" % start["character"], "command": "fake.cursor"})
    elif method == "codeAction/resolve":
        data = params["data"]
        result = dict(params, edit={"changes": {data["uri"]: [insert(data["uri"], data["line"], 0, "# resolved\n")]}})
    elif method == "textDocument/formatting":
        result = [{"range": {"start": {"line": i, "character": units(line, len(line.rstrip()))},
                             "end": {"line": i, "character": units(line, len(line))}}, "newText": ""}
                  for i, line in enumerate(rows(uri)) if line != line.rstrip()]
    elif method == "exit":
        break
    if rid is not None and method is not None:
        send({"jsonrpc": "2.0", "id": rid, "result": result})
'''

SOURCE = '''def greet(name):
    return "hi " + name

class Box:
    def size(self):
        return 1

x = greet("a")  # BUG
y = greet("b")
'''


class TestLsp(CapCase):
    def setUp(self):
        super().setUp()
        self.fake = Path(tempfile.mkdtemp(prefix="omni-lsp-fake-")) / "fake_lsp.py"
        self.fake.write_text(FAKE_SERVER, encoding="utf-8")
        self.work = Path(tempfile.mkdtemp(prefix="omni-lsp-work-"))
        (self.work / "pkg").mkdir()
        self.src = self.work / "pkg" / "main.py"
        self.src.write_text(SOURCE, encoding="utf-8")

    def start(self, **extra):
        cfg = {"py": [sys.executable, str(self.fake)], "go": ["omni-no-such-language-server"]}
        (self.profiles / "r15.json").write_text(json.dumps(
            {"name": "r15", "extends": "lean", "capabilities": ["lsp"], "lsp": cfg, **extra}))
        s = self.session(profile="r15", cwd=str(self.work))
        self.addCleanup(lsp._unload, s)
        return s

    def lsp(self, s, action, **args):
        return self.call(s, "lsp", {"action": action, **args})

    def test_navigation(self):
        s = self.start()
        self.assertEqual(s.caps.problems, {})
        self.assertIn("lsp", s.all_tools())
        self.assertFalse(getattr(s, "_lsp", None))                                # nothing starts until it is asked for

        r = self.lsp(s, "definition", path="pkg/main.py", line=8, symbol="greet")
        self.assertFalse(r.get("isError"), self.text(r))
        self.assertEqual(self.text(r).splitlines()[1], "pkg/main.py:1:5  def greet(name):")
        self.assertEqual(r["details"]["count"], 1)
        self.assertEqual(list(s._lsp), ["py"])

        refs = self.text(self.lsp(s, "references", path=str(self.src), line=9, column=5))   # absolute path, explicit column
        self.assertTrue(refs.startswith("3 reference(s) for pkg/main.py:9:5"), refs)
        self.assertEqual(refs.splitlines()[1:], ["pkg/main.py:1:5  def greet(name):", 'pkg/main.py:8:5  x = greet("a")  # BUG',
                                                 'pkg/main.py:9:5  y = greet("b")'])

        # symbol -> column: `name` is at column 20 (0-based 19) of line 2, and the word boundary skips nothing else
        self.assertEqual(self.text(self.lsp(s, "hover", path="pkg/main.py", line=2, symbol="name")), "pkg/main.py:2:20\nhover: name @ 1:19")
        # no column and no symbol: the first word on the line
        self.assertIn("hover: x @ 7:0", self.text(self.lsp(s, "hover", path="pkg/main.py", line=8)))
        self.assertIn("does not appear on line 1", self.text(self.lsp(s, "hover", path="pkg/main.py", line=1, symbol="nothere")))
        self.assertTrue(self.lsp(s, "hover", path="pkg/main.py", line=99, symbol="x").get("isError"))
        self.assertTrue(self.lsp(s, "definition", path="pkg/missing.py", line=1).get("isError"))
        self.assertTrue(self.lsp(s, "definition", line=1).get("isError"))
        self.assertTrue(self.lsp(s, "explode").get("isError"))
        self.assertEqual(len(s._lsp), 1)                                          # one server served all of it

    def test_symbols_and_workspace_symbols(self):
        s = self.start()
        r = self.lsp(s, "workspace_symbols", query="box")
        self.assertTrue(r.get("isError"))                                         # nothing running yet: asks for a path
        self.assertIn("path", self.text(r))
        out = self.text(self.lsp(s, "symbols", path="pkg/main.py"))
        self.assertEqual(out.splitlines(), ["3 symbol(s) in pkg/main.py:", "pkg/main.py:1:5  function greet",
                                            "pkg/main.py:4:7  class Box", "pkg/main.py:5:9    function size"])
        found = self.text(self.lsp(s, "workspace_symbols", query="box"))
        self.assertEqual(found.splitlines()[1:], ["pkg/main.py:4:7  class Box"])
        self.assertIn("No symbols match", self.text(self.lsp(s, "workspace_symbols", query="zzz")))
        self.assertTrue(self.lsp(s, "workspace_symbols").get("isError"))

    def test_workspace_symbols_starts_a_server_from_path(self):
        s = self.start()
        self.assertIn("function size", self.text(self.lsp(s, "workspace_symbols", query="siz", path="pkg/main.py")))

    def test_diagnostics_follow_the_file_on_disk(self):
        s = self.start()
        r = self.lsp(s, "diagnostics", path="pkg/main.py")
        self.assertEqual(self.text(r).splitlines(), ["1 problem(s) in pkg/main.py (1 error(s)):",
                                                     "pkg/main.py:8:19  error: bug marker (v1)  [fake B1]"])
        self.assertEqual((r["details"]["count"], r["details"]["fresh"]), (1, True))
        again = self.lsp(s, "diagnostics", path="pkg/main.py")                    # unchanged: answered from what was published
        self.assertIn("bug marker (v1)", self.text(again))
        self.src.write_text(SOURCE + "z = 1  # BUG\n", encoding="utf-8")          # edited on disk -> didChange, version 2
        changed = self.text(self.lsp(s, "diagnostics", path="pkg/main.py"))
        self.assertTrue(changed.startswith("2 problem(s)"), changed)
        self.assertIn("pkg/main.py:10:10  error: bug marker (v2)", changed)
        self.assertNotIn("(v1)", changed)
        self.src.write_text("ok = 1\n", encoding="utf-8")
        self.assertEqual(self.text(self.lsp(s, "diagnostics", path="pkg/main.py")), "No problems in pkg/main.py (fake_lsp).")
        self.assertEqual(self.text(self.lsp(s, "symbols", path="pkg/main.py")), "No symbols in pkg/main.py.")

    def test_rename_is_a_preview(self):
        s = self.start()
        r = self.lsp(s, "rename", path="pkg/main.py", line=1, symbol="greet", new_name="welcome")
        text = self.text(r)
        self.assertIn("would change 3 place(s) in 1 file(s)", text)
        self.assertIn("nothing was written", text)
        self.assertEqual(text.splitlines()[1:], ["pkg/main.py:1:5  def greet(name):", 'pkg/main.py:8:5  x = greet("a")  # BUG',
                                                 'pkg/main.py:9:5  y = greet("b")'])
        self.assertEqual((r["details"]["count"], r["details"]["files"], r["details"]["applied"]), (3, ["pkg/main.py"], False))
        self.assertEqual(r["details"]["edits"][0]["newText"], "welcome")
        self.assertEqual(self.src.read_text(encoding="utf-8"), SOURCE)            # the file is untouched
        self.assertTrue(self.lsp(s, "rename", path="pkg/main.py", line=1, symbol="greet").get("isError"))

    def test_results_are_capped(self):
        s = self.start()
        self.src.write_text("def f():\n    pass\n" + "f()\n" * 100, encoding="utf-8")
        out = self.text(self.lsp(s, "references", path="pkg/main.py", line=1, symbol="f"))
        self.assertTrue(out.startswith("101 reference(s)"), out[:80])
        self.assertEqual(len(out.splitlines()), 1 + lsp.MAX_ROWS + 1)
        self.assertTrue(out.endswith("... and 41 more"))

    def test_missing_server(self):
        s = self.start()
        (self.work / "main.go").write_text("package main\n", encoding="utf-8")
        (self.work / "lib.rs").write_text("fn main() {}\n", encoding="utf-8")
        (self.work / "notes.xyz").write_text("x\n", encoding="utf-8")
        r = self.lsp(s, "symbols", path="main.go")                                # the profile names a server that is not installed
        self.assertTrue(r.get("isError"))
        self.assertIn("omni-no-such-language-server", self.text(r))
        self.assertIn("not found on PATH", self.text(r))
        with mock.patch.object(lsp.shutil, "which", return_value=None):           # built-in table, nothing installed
            r = self.lsp(s, "symbols", path="lib.rs")
            self.assertTrue(r.get("isError"))
            self.assertIn("Install rust-analyzer", self.text(r))
            self.assertIn("rustup component add rust-analyzer", self.text(r))
            status = self.text(self.lsp(s, "status"))
            self.assertIn("not installed (pyright-langserver or pylsp)", lsp.describe(self.session(profile="lean", cwd=str(self.work))))
            self.assertIn("omni-no-such-language-server (profile) - NOT on PATH", status)
        r = self.lsp(s, "symbols", path="notes.xyz")
        self.assertTrue(r.get("isError"))
        self.assertIn("no language server is configured for '.xyz'", self.text(r))
        self.assertFalse(s._lsp)

    def test_windows_shims_run_through_cmd(self):
        with mock.patch.object(lsp.shutil, "which", return_value=r"C:\npm\typescript-language-server.cmd"), \
                mock.patch.object(lsp.os, "name", "nt"):
            cmd = lsp.resolve_command(["typescript-language-server", "--stdio"])
        self.assertEqual(cmd[1:], ["/c", r"C:\npm\typescript-language-server.cmd", "--stdio"])
        self.assertIsNone(lsp.resolve_command(["omni-no-such-language-server"]))

    def test_status_command_and_unload(self):
        s = self.start()
        command = s.caps.commands()["lsp"][1]
        self.assertIn("none (a server starts", command(""))
        self.assertIn("(profile) - found", command(""))
        self.lsp(s, "symbols", path="pkg/main.py")
        srv = s._lsp["py"]
        self.assertIsNone(srv.popen.poll())
        status = self.text(self.lsp(s, "status"))
        self.assertIn(f"fake_lsp (pid {srv.popen.pid}, 1 file(s) open", status)
        self.assertIn(f"pid {srv.popen.pid}", command(""))
        s.caps.unload()                                                           # what a reload or shutdown does
        self.assertIsNotNone(srv.popen.poll())
        self.assertEqual(s._lsp, {})
        self.assertIn("none (a server starts", command(""))
        again = self.text(self.lsp(s, "symbols", path="pkg/main.py"))             # and it comes back on demand
        self.assertIn("class Box", again)
        self.assertNotEqual(s._lsp["py"].popen.pid, srv.popen.pid)

    def test_a_crashed_server_is_restarted(self):
        s = self.start()
        self.lsp(s, "symbols", path="pkg/main.py")
        first = s._lsp["py"]
        first.popen.kill()
        first.popen.wait(timeout=5)
        r = self.lsp(s, "symbols", path="pkg/main.py")
        self.assertFalse(r.get("isError"), self.text(r))
        self.assertIsNot(s._lsp["py"], first)


    # ------------------------------------------------------------------------------- writing, encodings, diagnostics
    def start_with(self, *flags, **extra):
        cfg = {"py": [sys.executable, str(self.fake), *flags]}
        (self.profiles / "r15.json").write_text(json.dumps({"name": "r15", "extends": "lean", "capabilities": ["lsp"], "lsp": cfg, **extra}))
        s = self.session(profile="r15", cwd=str(self.work))
        self.addCleanup(lsp._unload, s)
        return s

    def test_pull_diagnostics(self):
        s = self.start_with("--pull")
        r = self.lsp(s, "diagnostics", path="pkg/main.py")
        self.assertIn("fake-pull", self.text(r))
        self.assertIn("bug marker (v1)", self.text(r))
        self.assertTrue(s._lsp["py"].capabilities.get("diagnosticProvider"))
        again = self.text(self.lsp(s, "diagnostics", path="pkg/main.py"))          # "unchanged": the cached report holds
        self.assertIn("bug marker (v1)", again)
        self.src.write_text(SOURCE.replace("# BUG", "# BUG BUG"), encoding="utf-8")
        self.assertIn("bug marker (v2)", self.text(self.lsp(s, "diagnostics", path="pkg/main.py")))

    def test_columns_on_a_line_with_emoji(self):
        line = 's = "\U0001F600\U0001F600"; target = 1\n'
        (self.work / "pkg" / "emoji.py").write_text(line, encoding="utf-8")
        col = line.index("target")                                                 # 0-based characters
        s = self.start()                                                           # the server counts UTF-16 units
        hover = self.text(self.lsp(s, "hover", path="pkg/emoji.py", line=1, symbol="target"))
        self.assertIn(f"hover: target @ 0:{col + 2}", hover)                       # two emoji = two extra units
        self.assertTrue(hover.startswith(f"pkg/emoji.py:1:{col + 1}"), hover)       # shown back in characters
        refs = self.text(self.lsp(s, "references", path="pkg/emoji.py", line=1, symbol="target"))
        self.assertIn(f"pkg/emoji.py:1:{col + 1}", refs)
        s32 = self.start_with("--encoding=utf-32")
        self.assertIn(f"hover: target @ 0:{col}", self.text(self.lsp(s32, "hover", path="pkg/emoji.py", line=1, symbol="target")))
        self.assertEqual(s32._lsp["py"].encoding, "utf-32")

    def test_code_actions_list_and_apply(self):
        s = self.start()
        listing = self.text(self.lsp(s, "code_actions", path="pkg/main.py", line=8))
        for n, title in ((1, "Replace BUG marker"), (2, "Add header"), (3, "Run organizer"), (4, "Touch outside"),
                         (5, "Lazy fix"), (6, "Two files"), (7, "Inline it")):
            self.assertIn(f"{n}. {title}", listing)
        self.assertIn("command only: cannot be applied", listing)
        self.assertIn("disabled: not here", listing)

        for index, why in ((3, "command only"), (7, "disabled"), (4, "outside the project")):   # refusals write nothing
            r = self.lsp(s, "apply_action", path="pkg/main.py", line=8, index=index)
            self.assertTrue(r.get("isError"), index)
            self.assertIn(why, self.text(r))
        self.assertEqual(self.src.read_text(encoding="utf-8"), SOURCE)
        self.assertFalse((self.fake.with_name("outside.txt")).exists())
        self.assertIn("between 1 and", self.text(self.lsp(s, "apply_action", path="pkg/main.py", line=8, index=99)))

        r = self.lsp(s, "apply_action", path="pkg/main.py", line=8, index=1)
        self.assertFalse(r.get("isError"), self.text(r))
        self.assertIn('x = greet("a")  # FIXED', self.src.read_text(encoding="utf-8"))
        # line 1 has no diagnostics, so its list starts at "Add header": 1 Add header ... 4 Lazy fix
        self.assertIn("4. Lazy fix", self.text(self.lsp(s, "code_actions", path="pkg/main.py", line=1)))
        self.lsp(s, "apply_action", path="pkg/main.py", line=1, index=4)          # resolved by the server on request
        self.assertTrue(self.src.read_text(encoding="utf-8").startswith("# resolved\n"))
        self.lsp(s, "apply_action", path="pkg/main.py", line=1, index=1)          # documentChanges, two inserts at one spot
        after = self.src.read_text(encoding="utf-8")
        self.assertTrue(after.startswith("# one\n# two\n# resolved"), after[:40])
        self.assertEqual(after.count("# seen"), 1)

    def test_rename_apply_format_and_the_gate(self):
        s = self.start()
        r = self.lsp(s, "rename", path="pkg/main.py", line=1, symbol="greet", new_name="welcome", apply=True)
        self.assertFalse(r.get("isError"), self.text(r))
        text = self.src.read_text(encoding="utf-8")
        self.assertEqual((text.count("welcome"), text.count("greet")), (3, 0))

        messy = self.work / "pkg" / "messy.py"
        messy.write_text("a = 1   \nb = 2\t\nc = 3\n", encoding="utf-8")
        r = self.lsp(s, "format", path="pkg/messy.py")
        self.assertIn("Formatted pkg/messy.py", self.text(r))
        self.assertEqual(messy.read_text(encoding="utf-8"), "a = 1\nb = 2\nc = 3\n")
        self.assertIn("already formatted", self.text(self.lsp(s, "format", path="pkg/messy.py")))

        messy.write_text("a = 1   \n", encoding="utf-8")
        with mock.patch.object(s.caps, "gate", return_value="plan mode is on: no edits until the plan is approved"):
            r = self.lsp(s, "format", path="pkg/messy.py")
        self.assertTrue(r.get("isError"))
        self.assertIn("plan mode is on", self.text(r))
        self.assertEqual(messy.read_text(encoding="utf-8"), "a = 1   \n")          # nothing written

    def test_a_dying_server_shows_its_last_output(self):
        s = self.start_with("--die=textDocument/hover")
        r = self.lsp(s, "hover", path="pkg/main.py", line=1, symbol="greet")
        self.assertTrue(r.get("isError"))
        status = s.caps.commands()["lsp"][1]("")
        self.assertIn("Died (restarted on next use)", status)
        self.assertIn("fake: line 25", status)
        self.assertNotIn("fake: line 5\n", status)                                 # only the last lines are kept


if __name__ == "__main__":
    unittest.main()
