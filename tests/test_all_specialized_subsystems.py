"""
Comprehensive Unit Test Suite for All 12 Specialized Subsystems:
1. Chrome DevTools Protocol Relay (Oh My Pi browser-relay)
2. Real-time WebRTC/WebSocket Collab Server (Oh My Pi collab-web)
3. Rust Native Acceleration (Oh My Pi natives)
4. Harbor & SWE-bench Evaluator Harness (Oh My Pi metaharness)
5. Multi-Platform Chat Gateways (OpenClaw Telegram, Discord, Slack, WhatsApp)
6. Ephemeral Docker Sandbox Runner (OpenClaw sandbox)
7. Companion Node Pairing (OpenClaw /pair, /nodes)
8. Interactive Visual Canvas (OpenClaw /canvas)
9. Local Open-Weights Engine (Hermes 3 llama.cpp/vLLM)
10. Offline Voice Loop & Wake-Word (Hermes "Hey Hermes")
11. Paperclip Managed Employee Adapter (Hermes paperclip)
12. Language Server Protocol (LSP) Bridge (Pi Core lspmux)
13. Shadow Git Worktree Isolation (Pi Core worktree)
"""

import asyncio
import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src.features.browser_relay import ChromeDevToolsRelay
from src.features.collab_web import CollabServerManager
from src.features.native_accel import native_accel
from src.features.metaharness import MetaHarnessManager
from src.features.gateways.manager import ChatGatewayManager
from src.features.docker_sandbox import DockerSandboxRunner
from src.features.node_pairing import NodePairingManager
from src.features.visual_canvas import VisualCanvasManager
from src.features.local_engine import LocalEngineRunner
from src.features.voice_loop import VoiceLoopManager
from src.features.paperclip_adapter import PaperclipAdapter
from src.features.lsp_bridge import LspClient
from src.features.shadow_worktree import ShadowWorktreeManager
from src.kernel.commands import CommandRegistry
from src.kernel.commands_executor import CommandExecutor
from src.kernel.state import SessionState


class TestSpecializedSubsystems(unittest.TestCase):
    def setUp(self):
        self.cmd_reg = CommandRegistry(active_profile="full")
        self.executor = CommandExecutor(command_registry=self.cmd_reg)
        self.state = SessionState()

    def test_01_chrome_cdp_browser_relay(self):
        relay = ChromeDevToolsRelay()
        tabs = relay.list_tabs()
        self.assertTrue(len(tabs) >= 1)
        dom = relay.snapshot_dom()
        self.assertIn("title", dom)
        val = relay.evaluate_js("document.title")
        self.assertIsNotNone(val)

    def test_02_webrtc_collab_server(self):
        collab = CollabServerManager(port=18793)
        room = collab.create_room("Test Suite Room")
        self.assertIsNotNone(room.id)
        url = collab.get_share_url(room.id)
        self.assertIn(room.id, url)
        ok = collab.broadcast_event(room.id, "diff", {"file": "core.py", "lines": "+10"})
        self.assertTrue(ok)
        collab.stop()

    def test_03_native_acceleration(self):
        old_txt = "x = 1\ny = 2\n"
        new_txt = "x = 1\ny = 3\nz = 4\n"
        diff = native_accel.fast_diff(old_txt, new_txt, "test.py")
        self.assertTrue(diff.additions >= 2)
        self.assertEqual(diff.deletions, 1)
        matches = native_accel.scan_tokens_regex("hello world 123", r"\d+")
        self.assertEqual(len(matches), 1)
        self.assertTrue(native_accel.verify_digest(b"omni", "cfb12585da56e4c0e3e189f24a8418f08108e5a40a84d7aafc504c704e21512c"))

    def test_04_metaharness_evaluator(self):
        meta = MetaHarnessManager()
        exp_id = meta.create_experiment("SWE-Bench-Trial", "swe_bench")
        self.assertTrue(exp_id.startswith("exp_"))
        run = meta.execute_mock_trial(exp_id, "astropy__astropy-1234")
        self.assertEqual(run.status, "resolved")
        self.assertEqual(run.score, 1.0)
        summary = meta.get_experiment_summary(exp_id)
        self.assertEqual(summary["total_runs"], 1)

    def test_05_multi_platform_chat_gateways(self):
        gw = ChatGatewayManager()
        gw.start_all()
        status = gw.get_status()
        self.assertTrue(status.get("telegram"))
        self.assertTrue(status.get("discord"))
        self.assertTrue(status.get("slack"))
        self.assertTrue(status.get("whatsapp"))
        reply = gw.gateways["telegram"].handle_raw_update({
            "message": {"message_id": 1, "chat": {"id": 10}, "from": {"id": 20, "username": "alice"}, "text": "ping"}
        })
        self.assertIn("ping", reply)
        gw.stop_all()

    def test_06_docker_sandbox(self):
        sandbox = DockerSandboxRunner()
        res = sandbox.run("python -c \"print('sandbox_test')\"")
        self.assertEqual(res.exit_code, 0)
        self.assertIn("sandbox_test", res.stdout)

    def test_07_node_pairing(self):
        npm = NodePairingManager()
        req = npm.create_pairing_request()
        url = npm.get_pairing_url(req)
        self.assertIn(req.token, url)
        node = npm.approve_pairing(req.request_id, "Test Companion", "android")
        self.assertIsNotNone(node)
        self.assertEqual(node.name, "Test Companion")
        task_res = npm.dispatch_remote_task(node.id, "system.run", {"command": "dir"})
        self.assertTrue(task_res["success"])

    def test_08_visual_canvas(self):
        vcm = VisualCanvasManager(port=18794)
        doc = vcm.create_document("Architecture Diagram", "graph TD; A-->B;", "mermaid")
        self.assertIsNotNone(doc.id)
        self.assertEqual(doc.kind, "mermaid")
        vcm.stop()

    def test_09_local_open_weights_engine(self):
        engine = LocalEngineRunner()
        models = engine.list_available_models()
        self.assertTrue(len(models) >= 1)
        ans = engine.query([{"role": "user", "content": "hello"}])
        self.assertIn("assistant", ans.get("role", ""))

    def test_10_voice_loop_and_wake_word(self):
        voice = VoiceLoopManager()
        res = voice.simulate_wake_and_prompt("What is my current task?")
        self.assertIn("What is my current task?", res)

    def test_11_paperclip_adapter(self):
        clip = PaperclipAdapter()
        hb = clip.heartbeat()
        self.assertEqual(hb["status"], "online")
        tickets = clip.list_tickets()
        self.assertTrue(len(tickets) >= 1)
        sub = clip.submit_deliverable(tickets[0].id, "Completed test task")
        self.assertTrue(sub["success"])

    def test_12_lsp_bridge(self):
        lsp = LspClient()
        diags = lsp.get_diagnostics(__file__)
        self.assertEqual(len(diags), 0)
        symbols = lsp.get_symbols(__file__)
        self.assertTrue(len(symbols) >= 1)
        names = [s.name for s in symbols]
        self.assertIn("TestSpecializedSubsystems", names)

    def test_13_shadow_worktree_isolation(self):
        wt_mgr = ShadowWorktreeManager()
        ok, msg, wt = wt_mgr.create_worktree("unit_test")
        self.assertTrue(ok)
        self.assertIsNotNone(wt)
        code, out, err = wt_mgr.execute_in_worktree(wt.id, "python -c \"print('wt_ok')\"")
        self.assertEqual(code, 0)
        self.assertIn("wt_ok", out)
        ok_d, msg_d = wt_mgr.discard_worktree(wt.id)
        self.assertTrue(ok_d)

    def test_14_slash_commands_integration(self):
        loop = asyncio.new_event_loop()
        try:
            cmds = [
                ("/browser tabs", "Chrome DevTools Relay"),
                ("/collab", "Collaboration Live"),
                ("/eval", "Metaharness Trial"),
                ("/gateway", "Omni Multi-Platform Chat Gateways"),
                ("/sandbox echo hello", "Sandbox Run"),
                ("/pair", "Paired Companion Nodes"),
                ("/canvas", "Visual Canvas"),
                ("/local", "Local Open-Weights Engine"),
                ("/voice", "Voice Loop Status"),
                ("/paperclip", "Paperclip Managed Employee"),
                ("/lsp", "LSP Operation"),
                ("/worktree list", "Active Shadow Worktrees"),
            ]
            for cmd_str, expected in cmds:
                handled, output = loop.run_until_complete(self.executor.execute(cmd_str, self.state))
                self.assertTrue(handled, f"Command '{cmd_str}' must be handled")
                self.assertIn(expected.lower(), output.lower(), f"Output of '{cmd_str}' must contain '{expected}'")
        finally:
            loop.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
