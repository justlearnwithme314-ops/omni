"""OpenClaw plugins (openclaw.plugin.json + definePluginEntry) through the real Node extension host."""
import json
import sys
import textwrap
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from test_capabilities import CapCase, Port  # noqa: E402
from test_pi_runtime import _node_ok  # noqa: E402
from src.pi.ai import Faux  # noqa: E402
from src.pi.caps import openclaw_plugins  # noqa: E402
from src.pi.commands import Commands  # noqa: E402

PLUGIN = textwrap.dedent('''
    import { Type } from "typebox";
    import { definePluginEntry, type AnyAgentTool } from "openclaw/plugin-sdk/plugin-entry";
    import { jsonResult, readStringParam, readPositiveIntegerParam } from "openclaw/plugin-sdk/provider-web-search";
    import { optionalStringEnum } from "openclaw/plugin-sdk/channel-actions";
    import { somethingDeep } from "openclaw/plugin-sdk/media-runtime";

    const repeat: AnyAgentTool = {
      name: "oc_repeat", label: "Repeat", description: "Repeat a word",
      parameters: Type.Object({ word: Type.String(), times: Type.Optional(Type.Integer()), mode: optionalStringEnum(["upper", "lower"] as const) }),
      async execute(_id, params) {
        const word = readStringParam(params, "word", { required: true });
        const times = readPositiveIntegerParam(params, "times") ?? 2;
        const out = Array(times).fill(params.mode === "upper" ? word.toUpperCase() : word).join(" ");
        return jsonResult({ out });
      },
    };

    export default definePluginEntry({
      id: "demo-claw", name: "Demo Claw", description: "test plugin",
      register(api) {
        api.registerTool(repeat);
        api.registerTool((ctx) => ({ name: "oc_where", description: "workspace dir", parameters: Type.Object({}),
          async execute() { return { content: [{ type: "text", text: "ws=" + Boolean(ctx.workspaceDir) }] }; } }), { optional: true });
        api.registerCommand({ name: "claw-ping", description: "Ping", handler: async ({ args }) => ({ text: "pong " + args }) });
        api.on("before_tool_call", async (event) => {
          if (event.toolName === "write") return { block: true, blockReason: "demo-claw forbids writes" };
          if (event.toolName === "oc_repeat") return { params: { times: 3 } };
        });
        api.on("before_prompt_build", async () => ({ appendSystemContext: "CLAW-RULE: be kind" }));
        api.on("gateway_start", async () => {});
        api.registerService({ id: "bg", start() {}, stop() {} });
        api.registerWebSearchProvider({ id: "x" });
        void somethingDeep;
      },
    });
''')


@unittest.skipUnless(_node_ok(), "Node >= 22.13 required for the extension host")
class TestOpenClawPlugins(CapCase):
    def make(self):
        d = self.dir / "claws" / "demo-claw"
        d.mkdir(parents=True)
        (d / "openclaw.plugin.json").write_text(json.dumps({"id": "demo-claw", "name": "Demo Claw", "contracts": {"tools": ["oc_repeat"]}}))
        (d / "package.json").write_text(json.dumps({"name": "demo-claw", "type": "module", "openclaw": {"extensions": ["./index.ts"]}}))
        (d / "index.ts").write_text(PLUGIN, encoding="utf-8")
        skip = self.dir / "claws" / "not-a-plugin"
        skip.mkdir()
        (skip / "index.ts").write_text("export default function () {}")
        (self.profiles / "oc.json").write_text(json.dumps({"name": "oc", "extends": "lean", "capabilities": ["openclaw-plugins"],
                                                             "omni_plugins_pi": False, "openclaw_plugin_dirs": [str(self.dir / "claws")],
                                                             "openclaw_default_dirs": False}))
        return d

    def test_tools_commands_and_hooks_of_an_openclaw_plugin(self):
        d = self.make()
        s = self.session(profile="oc", no_extensions=False)
        try:
            found = openclaw_plugins.discover(s)
            self.assertEqual([(p["id"], [e.name for e in p["entries"]]) for p in found], [("demo-claw", ["index.ts"])])
            ext = next(e for e in s.ext.report() if e["name"] == "demo-claw")
            self.assertTrue(ext["ok"], ext.get("error"))
            self.assertEqual(sorted(ext["tools"]), ["oc_repeat", "oc_where"])
            gaps = set(ext["missing"])
            self.assertTrue({"openclaw.registerService", "openclaw.registerWebSearchProvider", "openclaw.on:gateway_start"} <= gaps, gaps)
            self.assertIn("oc_repeat", s.active_tool_names)
            Faux.queue = [{"tool": "oc_repeat", "args": {"word": "hi", "mode": "upper"}},
                          {"tool": "write", "args": {"path": "z.txt", "content": "x"}},
                          {"tool": "oc_where", "args": {}}, {"text": "done"}]
            s.prompt("go")
            res = [e["message"] for e in s.sessions.entries if e["type"] == "message" and e["message"]["role"] == "toolResult"]
            self.assertEqual(json.loads(res[0]["content"][0]["text"]), {"out": "HI HI HI"})      # hook rewrote times to 3
            self.assertEqual(res[1]["content"][0]["text"], "demo-claw forbids writes")
            self.assertFalse((self.dir / "z.txt").exists())
            self.assertEqual(res[2]["content"][0]["text"], "ws=true")
            self.assertIn("CLAW-RULE: be kind", Faux.requests[0]["systemPrompt"])
            notes = []
            s.ui.ext_notify = lambda message="", type="info", **_: notes.append(message)
            s.prompt("/claw-ping now")
            self.assertEqual(notes, ["pong now"])
            port = Port()
            self.assertTrue(Commands(s, port).run("openclaw", ""))
            out = port.notes[-1][1]
            self.assertIn("demo-claw — loaded, tools: oc_repeat, oc_where", out)
            self.assertIn("registerService", out)
            self.assertIn(str(d), out)
        finally:
            s.shutdown()

    def test_needs_the_extension_host(self):
        self.make()
        s = self.session(profile="oc", no_extensions=True)
        self.assertIn("extension host", s.caps.problems["openclaw-plugins"])


if __name__ == "__main__":
    unittest.main()
