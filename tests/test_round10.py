"""Code navigation, the usage report, shared memory for subagents, browser `wait`, sandbox snapshots. Offline."""
import contextlib
import io
import json
import os
import sys
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from test_capabilities import CapCase, Port  # noqa: E402
from test_capabilities_infra import FAKE_RUNTIME, FakeRelay  # noqa: E402
from src.pi import usage  # noqa: E402
from src.pi.ai import Faux  # noqa: E402
from src.pi.caps import browser  # noqa: E402
from src.pi.commands import Commands  # noqa: E402

PY = '''import os

class Store(Base):
    """Keeps things."""
    def save(self, key, value=None):
        return write_item(key, value)

    async def load(self, key):
        return None

def write_item(key, value):
    return os.path.join(key, str(value))

def helper():
    store = Store()
    return store.save("a") or write_item("a", 1)
'''
TS = '''export interface Shape { area(): number }
export class Circle implements Shape {
  constructor(private r: number) {}
  area(): number {
    return Math.PI * this.r ** 2;
  }
}
export function makeCircle(r: number): Circle {
  return new Circle(r);
}
export const double = (x: number) => x * 2;
if (makeCircle(1).area() > 3) { console.log(double(2)); }
'''


class TestCodeNav(CapCase):
    def setUp(self):
        super().setUp()
        (self.dir / "pkg").mkdir()
        (self.dir / "pkg" / "store.py").write_text(PY, encoding="utf-8")
        (self.dir / "pkg" / "shapes.ts").write_text(TS, encoding="utf-8")
        (self.dir / "node_modules" / "dep").mkdir(parents=True)
        (self.dir / "node_modules" / "dep" / "index.js").write_text("function write_item() {}\n", encoding="utf-8")

    def nav(self, s, **kw):
        return self.text(self.call(s, "code_nav", kw))

    def test_outline_definition_references(self):
        s = self.session(capabilities=["code-nav"])
        out = self.nav(s, action="outline", path="pkg/store.py").split("\n")
        self.assertEqual([l.split(None, 2)[1:] for l in out], [
            ["class", "Store(Base)"], ["method", "Store.save(self, key, value=None)"], ["method", "Store.load(self, key)"],
            ["function", "write_item(key, value)"], ["function", "helper()"]])
        self.assertTrue(out[0].strip().startswith("3 "))
        ts = self.nav(s, action="outline", path="pkg/shapes.ts")
        for want in ("interface Shape", "class Circle implements Shape", "area(): number", "function makeCircle", "const double"):
            self.assertIn(want, ts)
        self.assertNotIn("if (makeCircle", ts)
        self.assertEqual(self.nav(s, action="definition", name="write_item"), "pkg/store.py:11: function write_item(key, value)")   # node_modules skipped
        self.assertEqual(self.nav(s, action="definition", name="Store.save"), "pkg/store.py:5: method Store.save(self, key, value=None)")
        self.assertIn("pkg/store.py:5: method Store.save", self.nav(s, action="definition", name="save"))
        self.assertIn("pkg/shapes.ts:8: function export function makeCircle(r: number): Circle", self.nav(s, action="definition", name="makeCircle"))
        self.assertIn("pkg/shapes.ts:2: class export class Circle implements Shape", self.nav(s, action="definition", name="Circle"))
        self.assertIn("No definition of `nowhere_fn`", self.nav(s, action="definition", name="nowhere_fn"))
        refs = self.nav(s, action="references", name="write_item")
        self.assertTrue(refs.startswith("pkg/store.py (3)"))
        self.assertIn("  6: return write_item(key, value)", refs)
        self.assertNotIn("node_modules", refs)
        self.assertIn("No use of `zzz`", self.nav(s, action="references", name="zzz"))
        self.assertIn("pkg/shapes.ts", self.nav(s, action="references", name="Circle", path="pkg"))
        for bad in ({"action": "outline"}, {"action": "definition", "name": "a b"}, {"action": "jump", "name": "x"}):
            self.assertTrue(self.call(s, "code_nav", bad).get("isError"), bad)
        self.assertTrue(s.all_tools()["code_nav"].read_only)


class TestUsageReport(CapCase):
    def test_report_from_traces(self):
        s = self.session(capabilities=["tracing"], no_session=False)
        port = Port()
        cmds = Commands(s, port)
        cmds.run("usage", "")
        self.assertIn("No traced runs", port.notes[-1][1])
        Faux.queue = [{"tool": "read", "args": {"path": "a.txt"}}, {"text": "three lines"}, {"text": "hello"}]
        s.prompt("how long is a.txt?")
        s.prompt("hi")
        cmds.run("rate", "up")
        summary = usage.summarize(self.agent, 7)
        self.assertEqual((summary["total"]["runs"], summary["ratings"]), (2, {"count": 1, "average": 1.0}))
        self.assertEqual(summary["by_model"]["faux/faux-1"]["calls"], 3)
        self.assertEqual(summary["by_tool"]["read"]["calls"], 1)
        self.assertEqual(sum(d["runs"] for d in summary["by_day"].values()), 2)
        cmds.run("usage", "")
        text = port.notes[-1][1]
        self.assertIn("Last 7 day(s): 2 runs", text)
        self.assertIn("faux/faux-1", text)
        self.assertIn("By tool (slowest first)", text)
        self.assertIn("Your ratings: 1 · average 1.0", text)
        out_file = self.dir / "report.html"
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            self.assertEqual(usage.cli(["--days", "30", "--html", str(out_file)], self.agent), 0)
            self.assertEqual(usage.cli(["--bogus"], self.agent), 2)
        page = out_file.read_text(encoding="utf-8")
        self.assertIn("<h1>Omni usage — last 30 day(s)</h1>", page)
        self.assertIn("faux/faux-1", page)
        self.assertEqual(usage.summarize(self.agent, 7, now=time.time() + 30 * 86400)["total"]["runs"], 0)   # outside the window


class TestSharedMemoryWaitSnapshot(CapCase):
    def test_subagents_read_the_parents_memory(self):
        (self.agent / "memories").mkdir()
        (self.agent / "memories" / "USER.md").write_text("Prefers terse answers", encoding="utf-8")
        s = self.session(capabilities=["task", "memory"], tools="read,bash,edit,write,grep,find,ls")
        Faux.queue = [{"tool": "task", "args": {"prompt": "look around", "read_only": True}}, {"text": "report"}, {"text": "ok"}]
        s.prompt("delegate")
        sub = Faux.requests[1]
        self.assertIn("Shared memory (read-only", sub["systemPrompt"])
        self.assertIn("Prefers terse answers", sub["systemPrompt"])
        self.assertNotIn("memory", sub["tools"])                                           # it can read, not write
        plain = self.session(capabilities=["task"], tools="read,bash,edit,write,grep,find,ls")
        Faux.queue = [{"tool": "task", "args": {"prompt": "x"}}, {"text": "r"}, {"text": "ok"}]
        plain.prompt("go")
        self.assertNotIn("Shared memory", Faux.requests[-2]["systemPrompt"])

    def test_browser_wait(self):
        relay = FakeRelay()
        relay.connected = True
        make = browser.make_relay
        browser.make_relay = lambda _s: relay
        try:
            s = self.session(capabilities=["browser"])
            ok = self.call(s, "browser", {"action": "wait", "text": "welcome back", "seconds": 2})
            self.assertEqual(ok["details"], {"action": "wait", "found": True})
            miss = self.call(s, "browser", {"action": "wait", "text": "never shown", "seconds": 0.5})
            self.assertIn("did not appear within 0.5 seconds", self.text(miss))
            self.assertEqual(self.call(s, "browser", {"action": "wait", "seconds": 0.2})["details"]["waited"], 0.2)
        finally:
            browser.make_relay = make

    def test_sandbox_snapshot(self):
        fake = self.dir / "fake_runtime.py"
        fake.write_text(FAKE_RUNTIME + "\nif a[0] == 'commit':\n    print('sha256:abc')\n", encoding="utf-8")
        old = os.environ.get("OMNI_SANDBOX_RUNTIME")
        os.environ["OMNI_SANDBOX_RUNTIME"] = f'"{sys.executable}" "{fake}"'
        try:
            s = self.session(capabilities=["sandbox"])
            port = Port()
            cmds = Commands(s, port)
            cmds.run("sandbox", "snapshot my-env")
            self.assertIn("Nothing to snapshot yet", port.notes[-1][1])
            self.call(s, "bash", {"command": "pip install requests"})
            cmds.run("sandbox", "snapshot my-env")
            self.assertIn("Saved the sandbox as image `my-env`", port.notes[-1][1])
            log = [json.loads(l) for l in Path(str(fake) + ".log").read_text().splitlines()]
            self.assertEqual(log[-1][0], "commit")
            self.assertEqual(log[-1][2], "my-env")
            cmds.run("sandbox", "")
            self.assertIn("/sandbox snapshot [name]", port.notes[-1][1])
            s.shutdown()
        finally:
            os.environ.pop("OMNI_SANDBOX_RUNTIME", None)
            if old is not None:
                os.environ["OMNI_SANDBOX_RUNTIME"] = old


if __name__ == "__main__":
    unittest.main()
