"""Pi features beyond the core loop: MCP, codemode, tool_search, packages, images, parallel read-only tools, /share
export, virtual models, extension UI hooks (editor component, autocomplete, terminal input, markdown transformers),
extension-registered MCP servers and TSX extensions.

Offline: MCP uses a tiny stdio server written here; models are `faux`. Node tests are skipped without Node >= 22.13.
"""
import base64
import io
import json
import os
import shutil
import sys
import tempfile
import textwrap
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
_AGENT = tempfile.mkdtemp(prefix="omni-pi-agent-")
os.environ["OMNI_PI_AGENT_DIR"] = _AGENT
os.environ.setdefault("OMNI_AUTH_DB", os.path.join(_AGENT, "agent_auth.db"))

from src.pi.agent import AgentSession, SessionOptions  # noqa: E402
from src.pi.ai import Faux  # noqa: E402
from src.pi.extensions.bridge import MIN_NODE, node_executable, node_version  # noqa: E402
from src.pi.modes import HeadlessUI  # noqa: E402

MCP_SERVER = textwrap.dedent('''
    import json, sys
    def send(m): sys.stdout.write(json.dumps(m) + "\\n"); sys.stdout.flush()
    tools = [{"name": "echo", "description": "Echo back a message", "annotations": {"readOnlyHint": True},
              "inputSchema": {"type": "object", "properties": {"text": {"type": "string", "description": "text to echo"}}, "required": ["text"]}},
             {"name": "search-issues", "description": "Search GitHub issues by keyword", "inputSchema": {"type": "object", "properties": {"q": {"type": "string"}}}},
             {"name": "big", "description": "Large output", "inputSchema": {"type": "object", "properties": {}}}]
    for line in sys.stdin:
        m = json.loads(line); mid = m.get("id"); meth = m.get("method")
        if meth == "initialize":
            send({"jsonrpc": "2.0", "id": mid, "result": {"protocolVersion": "2025-06-18", "capabilities": {"tools": {}, "resources": {}},
                  "serverInfo": {"name": "t"}, "instructions": "Test server"}})
        elif meth == "tools/list": send({"jsonrpc": "2.0", "id": mid, "result": {"tools": tools}})
        elif meth == "tools/call":
            p = m["params"]
            text = "x" * 30000 if p["name"] == "big" else "echo:" + json.dumps(p["arguments"])
            send({"jsonrpc": "2.0", "id": mid, "result": {"content": [{"type": "text", "text": text}], "structuredContent": {"got": p["arguments"]}}})
        elif meth == "resources/list": send({"jsonrpc": "2.0", "id": mid, "result": {"resources": [{"uri": "mem://a", "name": "a"}]}})
        elif meth == "resources/read": send({"jsonrpc": "2.0", "id": mid, "result": {"contents": [{"uri": "mem://a", "text": "hello resource"}]}})
        elif mid is not None: send({"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": "nope"}})
''')


def _node_ok() -> bool:
    n = node_executable()
    v = node_version(n) if n else None
    return bool(v and v >= MIN_NODE)


class Base(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="omni-pi-ws-"))
        self.agent = Path(tempfile.mkdtemp(prefix="omni-pi-agent-"))
        (self.dir / "a.txt").write_text("alpha\n", encoding="utf-8")
        self.server = self.dir / "server.py"
        self.server.write_text(MCP_SERVER, encoding="utf-8")
        Faux.queue, Faux.requests = [], []
        self.sessions = []

    def tearDown(self):
        for s in self.sessions:
            try:
                s.shutdown()
            except Exception:
                pass
        shutil.rmtree(self.dir, ignore_errors=True)
        shutil.rmtree(self.agent, ignore_errors=True)

    def mcp_config(self, **server):
        cfg = {"command": sys.executable, "args": [str(self.server)], **server}
        (self.agent / "mcp.json").write_text(json.dumps({"mcpServers": {"demo": cfg}}), encoding="utf-8")

    def session(self, **kw) -> AgentSession:
        opts = dict(cwd=str(self.dir), model="faux/faux-1", no_extensions=True, mode="print", no_session=True, agent_dir=str(self.agent))
        opts.update(kw)
        s = AgentSession(SessionOptions(**opts), ui=HeadlessUI())
        s.start()
        self.sessions.append(s)
        if s.mcp:
            s.mcp.wait_all(15)
            time.sleep(0.1)
        return s

    def tool_texts(self, s):
        return [e["message"]["content"][0]["text"] for e in s.sessions.get_branch()
                if e.get("type") == "message" and e["message"]["role"] == "toolResult"]


class TestMcp(Base):
    def test_codemode_exposure_prompt_and_resources(self):
        self.mcp_config(description="Demo tools")
        s = self.session()
        self.assertIn("codemode", s.active_tool_names)                      # auto-enabled for codemode exposure
        self.assertNotIn("mcp__demo__echo", s.active_tool_names)
        self.assertIn("mcp__demo (via codemode): Demo tools", s.system_prompt())
        desc = {d["name"]: d for d in s.tool_declarations()}["codemode"]["description"]
        self.assertNotIn("mcp__demo__echo", desc)                            # deferred: never listed, found with searchTools()
        r = s.all_tools()["read_mcp_resource"].execute("x", {"server": "demo", "uri": "mem://a"})
        self.assertEqual(r["content"][0]["text"], "hello resource")
        r = s.all_tools()["mcp__demo__search_issues"].execute("x", {"q": "a"})
        self.assertIn("echo:", r["content"][0]["text"])
        big = s.all_tools()["mcp__demo__big"].execute("x", {})["content"][0]["text"]
        self.assertIn("chars truncated", big)
        self.assertIn("[Full output:", big)

    def test_direct_exposure_and_tool_exposure_patterns(self):
        self.mcp_config(exposure="direct", toolExposure={"big": "hidden", "search-*": "deferred"})
        s = self.session()
        self.assertIn("mcp__demo__echo", s.active_tool_names)
        self.assertNotIn("mcp__demo__big", s.all_tools())
        self.assertIn("tool_search", s.active_tool_names)
        self.assertTrue(s.all_tools()["mcp__demo__echo"].read_only)

    def test_config_validation(self):
        from src.pi.mcp import validate, sanitize
        self.assertIn("SSE", validate("x", {"type": "sse", "url": "http://h/sse"}))
        self.assertIn("command", validate("x", {}))
        self.assertIsNone(validate("dev-radius", {"url": "https://h/mcp"}))
        self.assertEqual(sanitize("dev-radius"), "dev_radius")

    def test_cli_add_list_remove(self):
        from src.pi.subcommands import mcp_cli
        self.assertEqual(mcp_cli(["add", "demo", "--exposure", "direct", "--", sys.executable, str(self.server)], self.agent, str(self.dir)), 0)
        self.assertEqual(json.loads((self.agent / "mcp.json").read_text())["mcpServers"]["demo"]["exposure"], "direct")
        self.assertEqual(mcp_cli(["list"], self.agent, str(self.dir)), 0)
        self.assertEqual(mcp_cli(["remove", "demo"], self.agent, str(self.dir)), 0)

    def test_no_mcp_flag(self):
        self.mcp_config()
        s = self.session(no_mcp=True)
        self.assertIsNone(s.mcp)


class TestToolSearch(Base):
    def test_bm25_ranking(self):
        from src.pi.toolsearch import rank
        docs = [{"name": "mcp__gh__search_issues", "text": "Search GitHub issues", "namespaceText": "mcp__gh github"},
                {"name": "mcp__fs__read_file", "text": "Read a file from disk", "namespaceText": ""},
                {"name": "window", "text": "Manage windows", "namespaceText": ""}]
        self.assertEqual(rank("issues", docs)[0]["name"], "mcp__gh__search_issues")
        self.assertEqual(rank("read_file", docs)[0]["name"], "mcp__fs__read_file")  # exact local name first
        self.assertEqual(rank("the of", docs), [])

    def test_tool_search_loads_matches_for_next_call(self):
        self.mcp_config(exposure="deferred")
        s = self.session()
        self.assertIn("tool_search", s.active_tool_names)
        Faux.queue = [{"tool": "tool_search", "args": {"query": "search issues"}}, {"text": "ok"}]
        s.prompt("find")
        s.wait_for_idle(30)
        self.assertIn("mcp__demo__search_issues", s.active_tool_names)
        self.assertIn("Loaded 1 tool", self.tool_texts(s)[-1])
        self.assertIn("mcp__demo__search_issues", Faux.requests[-1]["tools"])


@unittest.skipUnless(_node_ok(), "Node >= 22.13 required for codemode")
class TestCodemode(Base):
    def test_script_calls_tools_in_parallel_and_stores_values(self):
        self.mcp_config()
        s = self.session()
        code = textwrap.dedent('''
            const [a, b] = await Promise.all([tools.mcp__demo__echo({ text: "x" }), tools.read({ path: "a.txt" })]);
            text(a.structuredContent); text(b);
            console.log(JSON.stringify(await searchTools("github issues")));
            store("k", 42);
            const r = await tools.bash({ command: "echo hello" });
            return r.exit_code + ":" + r.output.trim();''')
        Faux.queue = [{"tool": "codemode", "args": {"code": code}}, {"text": "done"}]
        s.prompt("go")
        s.wait_for_idle(60)
        out = self.tool_texts(s)[-1]
        self.assertTrue(out.startswith("Script completed\nWall time"))
        self.assertIn("==> text 3/3 <==\n0:hello", out)
        self.assertIn('"got"', out)
        self.assertIn("<console_output>\n[{\"name\":\"mcp__demo__search_issues\"", out)
        self.assertEqual(s.codemode_store(), {"k": 42})

    def test_failures_and_options(self):
        s = self.session(tools="read,bash,codemode")
        Faux.queue = [{"tool": "codemode", "args": {"code": "await new Promise(() => {})"}}, {"text": "a"},
                      {"tool": "codemode", "args": {"code": "text('partial'); throw new Error('boom')"}}, {"text": "b"},
                      {"tool": "codemode", "args": {"code": "1;\n// @options: {\"timeout_ms\": 5}"}}, {"text": "c"}]
        for p in ("one", "two", "three"):
            s.prompt(p)
            s.wait_for_idle(30)
        a, b, c = self.tool_texts(s)[-3:]
        self.assertIn("never settle", a)
        self.assertTrue(b.startswith("Script failed") and "partial" in b and "Script error:\nError: boom" in b)
        self.assertIn("must be the first line", c)


class TestParallelAndImages(Base):
    def test_read_only_batch_keeps_order(self):
        s = self.session(tools="read,bash,ls,grep")
        Faux.queue = [{"tools": [{"tool": "read", "args": {"path": "a.txt"}}, {"tool": "ls", "args": {}},
                                 {"tool": "bash", "args": {"command": "echo z"}}, {"tool": "grep", "args": {"pattern": "alpha"}}]},
                      {"text": "fin"}]
        batches = s._parallel_batches([{"id": "1", "name": "read"}, {"id": "2", "name": "ls"}, {"id": "3", "name": "bash"},
                                       {"id": "4", "name": "grep"}])
        self.assertEqual([[c["id"] for c in b] for b in batches], [["1", "2"], ["3"], ["4"]])
        s.prompt("par")
        s.wait_for_idle(30)
        names = [e["message"].get("toolName") for e in s.sessions.get_branch() if e.get("type") == "message"
                 and e["message"]["role"] == "toolResult"]
        self.assertEqual(names, ["read", "ls", "bash", "grep"])

    def test_resize_note_and_block_images(self):
        from src.pi.images import process_image, _resize_png_pure, image_size
        import struct
        import zlib
        w, h = 2600, 1300  # a plain PNG built by hand (no Pillow needed)
        raw = b"".join(b"\x00" + b"\x10\x20\x30" * w for _ in range(h))
        chunk = lambda t, b: struct.pack(">I", len(b)) + t + b + struct.pack(">I", zlib.crc32(t + b) & 0xFFFFFFFF)  # noqa: E731
        png = b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)) + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b"")
        self.assertEqual(image_size(png), (w, h))
        r = process_image(png, "image/png")
        self.assertTrue(r["ok"])
        self.assertEqual(r["hints"], ["[Image: original 2600x1300, displayed at 2000x1000. Multiply coordinates by 1.30 to map to original image.]"])
        self.assertEqual(_resize_png_pure(png, 2000, 2000, 4 * 1024 * 1024)[3], (2000, 1000))
        (self.dir / "big.png").write_bytes(png)
        s = self.session()
        out = s.all_tools()["read"].execute("1", {"path": "big.png"})
        self.assertIn("displayed at 2000x1000", out["content"][0]["text"])
        from src.pi.agent import block_images
        msgs = block_images([{"role": "user", "content": [{"type": "image", "data": "x", "mimeType": "image/png"}]}])
        self.assertEqual(msgs[0]["content"][0]["type"], "text")


class TestPackagesAndShare(Base):
    def test_parse_sources(self):
        from src.pi.packages import parse_source
        n = parse_source("npm:@scope/pkg@1.2.3")
        self.assertEqual((n.kind, n.name, n.pinned), ("npm", "@scope/pkg", True))
        self.assertFalse(parse_source("npm:pkg").pinned)
        g = parse_source("git:github.com/example/pi-tools@v1")
        self.assertEqual((g.kind, g.host, g.repo_path, g.ref, g.url), ("git", "github.com", "example/pi-tools", "v1",
                                                                       "https://github.com/example/pi-tools.git"))
        self.assertEqual(parse_source("https://github.com/a/b").identity, parse_source("git:github.com/a/b@main").identity)

    def test_local_package_install_and_resources(self):
        from src.pi.packages import cli, package_resources
        pkg = self.dir / "mypkg"
        for d in ("extensions", "skills/demo", "prompts", "themes"):
            (pkg / d).mkdir(parents=True)
        (pkg / "package.json").write_text('{"name": "mypkg"}')
        (pkg / "extensions" / "a.ts").write_text("export default function(pi){}")
        (pkg / "extensions" / "legacy.ts").write_text("export default function(pi){}")
        (pkg / "skills" / "demo" / "SKILL.md").write_text("---\nname: demo\ndescription: Demo skill\n---\nbody\n")
        (pkg / "prompts" / "review.md").write_text("---\ndescription: review\n---\nReview $@\n")
        old = os.getcwd()
        os.chdir(self.dir)
        try:
            self.assertEqual(cli(["install", "./mypkg"], self.agent, str(self.dir)), 0)
        finally:
            os.chdir(old)
        self.assertEqual(len(package_resources(pkg, {"extensions": ["!extensions/legacy.ts"]})["extensions"]), 1)
        self.assertEqual(package_resources(pkg, {"skills": []})["skills"], [])
        s = self.session()
        self.assertIn("demo", [k.name for k in s.skills])
        self.assertIn("review", [t.name for t in s.templates])

    def test_share_export_has_trailing_pi_share_entry(self):
        from src.pi.share import export_for_share
        s = self.session()
        s.prompt("hello")
        s.wait_for_idle(10)
        out = self.dir / "share.jsonl"
        export_for_share(s, str(out))
        lines = [json.loads(l) for l in out.read_text().splitlines()]
        self.assertEqual(lines[0]["type"], "session")
        self.assertEqual(lines[-1]["customType"], "pi.share")
        self.assertIn("read", [t["name"] for t in lines[-1]["data"]["tools"]])


EXT = textwrap.dedent('''
    import { CustomEditor, type ExtensionAPI } from "@earendil-works/pi-coding-agent";
    class Bang extends CustomEditor {
      handleInput(data: string): void { if (data === "!") { this.insertTextAtCursor("BANG"); return; } super.handleInput(data); }
    }
    export default function (pi: ExtensionAPI) {
      pi.registerMarkdownTransformer((md) => md.replaceAll("-->", "→"));
      pi.registerVirtualModel({ provider: "router", id: "auto", name: "Auto", thinkingLevels: ["low", "high"],
        route(request, ctx) {
          const st = (request.state as any) ?? { n: 0 };
          return { model: ctx.modelRegistry.find("faux", "faux-1")!, thinkingLevel: "off", state: { n: st.n + 1, reason: request.reason } };
        } });
      pi.registerMcpServer("extdemo", { command: process.env.PY!, args: [process.env.MCP_SERVER!], exposure: "direct" });
      pi.on("session_start", (_e, ctx) => {
        ctx.ui.setEditorComponent((tui, theme, kb) => new Bang(tui, theme, kb));
        ctx.ui.onTerminalInput((data) => (data === "\\x07" ? { consume: true } : undefined));
        ctx.ui.addAutocompleteProvider((current) => ({
          triggerCharacters: ["#"],
          async getSuggestions(lines, line, col, options) {
            const m = (lines[line] ?? "").slice(0, col).match(/(?:^|[ \\t])#([^\\s#]*)$/);
            if (!m) return current.getSuggestions(lines, line, col, options);
            return { prefix: `#${m[1] ?? ""}`, items: [{ value: "#2983", label: "#2983", description: "issue" }] };
          },
          applyCompletion(lines, line, col, item, prefix) { return current.applyCompletion(lines, line, col, item, prefix); },
        }));
      });
    }
''')
TSX = textwrap.dedent('''
    import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
    const h = (tag: any, props: any, ...children: any[]) => ({ tag, props, children });
    /** @jsx h */
    export default function (pi: ExtensionAPI) {
      const el = <box title="hi">{"x"}<b /></box>;
      pi.registerCommand("tsxcheck", { description: JSON.stringify(el), handler: async () => {} });
    }
''')


@unittest.skipUnless(_node_ok(), "Node >= 22.13 required for the extension host")
class TestExtensionFeatures(Base):
    def test_ui_hooks_virtual_models_mcp_and_tsx(self):
        (self.dir / "features.ts").write_text(EXT, encoding="utf-8")
        (self.dir / "hello.tsx").write_text(TSX, encoding="utf-8")
        os.environ["PY"], os.environ["MCP_SERVER"] = sys.executable, str(self.server)
        s = self.session(no_extensions=False, extensions=[str(self.dir / "features.ts"), str(self.dir / "hello.tsx")],
                         model="router/auto", approve=True)
        ext = s.ext
        self.assertTrue(all(e["ok"] for e in ext.extensions), ext.extensions)
        self.assertIn('"tag":"box"', ext.commands["tsxcheck"]["description"])
        self.assertTrue(s.model.get("virtual"))
        # virtual model routing + router state on the branch
        s.prompt("hi")
        s.wait_for_idle(20)
        msgs = [e for e in s.sessions.get_branch()]
        asst = [e["message"] for e in msgs if e.get("type") == "message" and e["message"]["role"] == "assistant"]
        self.assertEqual((asst[-1]["provider"], asst[-1]["model"]), ("faux", "faux-1"))
        states = [e["data"] for e in msgs if e.get("type") == "custom" and e.get("customType") == "virtual-model-state"]
        self.assertEqual(states[-1]["state"], {"n": 1, "reason": "user"})
        # extension-registered MCP server with direct exposure
        s.mcp.wait_all(15)
        time.sleep(0.2)
        self.assertIn("mcp__extdemo__echo", s.all_tools())
        # UI hooks through the host
        self.assertEqual(ext.features["terminalInput"], 1)
        self.assertIsNone(ext.terminal_input("\x07"))
        self.assertEqual(ext.terminal_input("a"), "a")
        r = ext.editor_input("h")
        r = ext.editor_input("!")
        self.assertEqual(r["text"], "hBANG")
        ac = ext.autocomplete(["see #2"], 0, 6)
        self.assertEqual(ac["items"][0]["value"], "#2983")
        applied = ext.autocomplete_apply(["see #2"], 0, 6, ac["items"][0], ac["prefix"])
        self.assertEqual(applied["lines"], ["see #2983"])
        self.assertEqual(ext.markdown_transform("a --> b", "assistant"), "a → b")
        self.assertEqual(ext.markdown_transform("a --> b", "changelog"), "a --> b")


if __name__ == "__main__":
    unittest.main()
