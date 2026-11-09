"""
Behavioural tests for the specialised subsystems.

These assert what the code actually DOES: security rejections, refusal to run without
isolation, real HTTP round-trips, a real headless Chromium over CDP, real git worktrees,
and that slash commands share state through ``services``. Everything runs against the
scratch locations created by ``_isolation``.
"""

import os as _os, sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))
import _isolation  # noqa: F401  (must come before any src import)

import asyncio
import http.server
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.parse
import urllib.request
from unittest import mock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src.features.browser_relay import (
    BrowserNotConnected, CdpError, ChromeDevToolsRelay, find_chromium,
)
from src.features.collab_web import CollabServerManager
from src.features.docker_sandbox import DockerSandboxRunner
from src.features.gateways.base import GatewayNotConfigured, InboundChatMessage
from src.features.gateways.manager import ChatGatewayManager
from src.features.local_engine import LocalEngineError, LocalEngineRunner, LocalModelConfig
from src.features.lsp_bridge import LspClient
from src.features.metaharness import EvaluationRun, MetaHarnessManager
from src.features.native_accel import native_accel
from src.features.node_pairing import NodePairingManager
from src.features.paperclip_adapter import PaperclipAdapter
from src.features.shadow_worktree import ShadowWorktreeManager, WorktreeError
from src.features.visual_canvas import VisualCanvasManager
from src.features.voice_loop import VoiceLoopManager, VoiceUnavailable
from src.kernel.commands import CommandRegistry
from src.kernel.commands_executor import CommandExecutor
from src.kernel.services import services
from src.kernel.state import SessionState


def fetch(url, data=None, headers=None):
    body = json.dumps(data).encode() if data is not None else None
    req = urllib.request.Request(url, data=body, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, r.read().decode(), dict(r.headers)
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode(), dict(e.headers)


def free_port():
    import socket
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# ----------------------------------------------------------------------------- browser relay
class TestBrowserRelay(unittest.TestCase):
    def test_not_connected_gives_actionable_error(self):
        relay = ChromeDevToolsRelay(port=free_port())
        with self.assertRaises(BrowserNotConnected) as cm:
            relay.list_tabs()
        self.assertIn("/browser launch", str(cm.exception))

    @unittest.skipUnless(find_chromium(), "no Chromium-family browser installed")
    def test_real_chromium_cdp_roundtrip(self):
        port = free_port()
        relay = ChromeDevToolsRelay(port=port)
        page = "data:text/html," + urllib.parse.quote(
            "<title>T</title><h1>Hello</h1><button id=b onclick=\"document.title='clicked'\">go</button>"
            "<input id=i><script>console.error('page-error')</script>"
        )
        try:
            relay.launch(page, headless=True)
            self.assertEqual(relay.evaluate_js("6*7"), 42)
            snap = relay.snapshot_dom()
            self.assertEqual(snap["title"], "T")
            self.assertIn("Hello", snap["text"])
            self.assertTrue(relay.click_element("#b"))
            self.assertEqual(relay.evaluate_js("document.title"), "clicked")
            self.assertTrue(relay.fill_input("#i", "typed"))
            self.assertEqual(relay.evaluate_js("document.getElementById('i').value"), "typed")
            self.assertFalse(relay.click_element("#does-not-exist"))
            self.assertTrue(any("page-error" in e for e in relay.read_console_errors()))
            shot = relay.capture_screenshot()
            self.assertGreater(os.path.getsize(shot), 100)
            with self.assertRaises(CdpError):
                relay.evaluate_js("throw new Error('boom')")
        finally:
            relay.close()


# ----------------------------------------------------------------------------- collab + canvas security
class TestCollabServer(unittest.TestCase):
    def setUp(self):
        self.port = free_port()
        self.c = CollabServerManager(port=self.port)
        self.pair = self.c.create_room("pair", mode="pair")
        self.view = self.c.create_room("view", mode="view")
        self.base = f"http://127.0.0.1:{self.port}"

    def tearDown(self):
        self.c.stop()

    def test_binds_loopback_only(self):
        self.assertEqual(self.c._server.server_address[0], "127.0.0.1")

    def test_key_is_required_and_checked(self):
        r = self.pair
        self.assertEqual(fetch(f"{self.base}/api/room/{r.id}")[0], 403)
        self.assertEqual(fetch(f"{self.base}/api/room/{r.id}?key=wrong")[0], 403)
        status, body, _ = fetch(f"{self.base}/api/room/{r.id}?key={r.key}")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["id"], r.id)

    def test_guest_prompt_flow_and_view_only(self):
        s, _, _ = fetch(f"{self.base}/api/room/{self.pair.id}/prompt", {"prompt": "hello", "key": self.pair.key})
        self.assertEqual(s, 200)
        self.assertEqual(self.c.pop_guest_prompts(self.pair.id), ["hello"])
        self.assertEqual(self.c.pop_guest_prompts(self.pair.id), [])  # drained
        s, _, _ = fetch(f"{self.base}/api/room/{self.pair.id}/prompt", {"prompt": "x"})
        self.assertEqual(s, 403)  # no key
        s, _, _ = fetch(f"{self.base}/api/room/{self.view.id}/prompt", {"prompt": "x", "key": self.view.key})
        self.assertEqual(s, 403)  # view-only room

    def test_events_are_bounded(self):
        for i in range(700):
            self.c.broadcast_event(self.pair.id, "diff", {"i": i})
        self.assertLessEqual(len(self.pair.events), 500)


class TestCanvasServer(unittest.TestCase):
    def setUp(self):
        self.port = free_port()
        self.v = VisualCanvasManager(port=self.port)
        self.base = f"http://127.0.0.1:{self.port}"

    def tearDown(self):
        self.v.stop()

    def test_loopback_and_html_escaping(self):
        self.assertEqual(self.v._server.server_address[0], "127.0.0.1") if self.v.is_running else None
        d = self.v.create_document("<script>alert(1)</script>", "graph TD; A-->B", "mermaid")
        self.assertEqual(self.v._server.server_address[0], "127.0.0.1")
        _, page, _ = fetch(f"{self.base}/canvas/{d.id}")
        self.assertNotIn("<script>alert(1)</script>", page)
        self.assertIn("&lt;script&gt;alert(1)", page)

    def test_untrusted_html_is_csp_sandboxed(self):
        d = self.v.create_document("x", "<script>parent.steal()</script>", "html_bundle")
        _, _, headers = fetch(f"{self.base}/canvas/{d.id}")
        self.assertIn("sandbox", headers.get("Content-Security-Policy", ""))

    def test_post_requires_token_and_valid_kind(self):
        self.v.start()
        payload = {"title": "a", "content": "graph TD; A-->B"}
        self.assertEqual(fetch(f"{self.base}/canvas", payload)[0], 403)
        auth = {"Authorization": "Bearer " + self.v.token}
        self.assertEqual(fetch(f"{self.base}/canvas", payload, auth)[0], 200)
        self.assertEqual(fetch(f"{self.base}/canvas", dict(payload, kind="exe"), auth)[0], 400)


# ----------------------------------------------------------------------------- sandbox
class TestDockerSandbox(unittest.TestCase):
    def test_fails_closed_without_docker(self):
        marker = os.path.join(_isolation.TMP, "should_not_exist.txt")
        runner = DockerSandboxRunner()
        with mock.patch.object(DockerSandboxRunner, "_probe_docker", return_value=False):
            res = runner.run([sys.executable, "-c", f"open(r'{marker}','w').write('x')"])
        self.assertTrue(res.refused)
        self.assertEqual(res.exit_code, DockerSandboxRunner.REFUSED_CODE)
        self.assertFalse(os.path.exists(marker), "command must NOT run on the host when Docker is missing")

    def test_host_execution_is_explicit_opt_in(self):
        runner = DockerSandboxRunner()
        with mock.patch.object(DockerSandboxRunner, "_probe_docker", return_value=False):
            res = runner.run([sys.executable, "-c", "print(40+2)"], allow_host=True)
        self.assertEqual(res.stdout.strip(), "42")
        self.assertFalse(res.isolated_in_docker)

    def test_docker_command_is_locked_down(self):
        cmd = DockerSandboxRunner().docker_command(["echo", "hi"], cwd=_isolation.TMP)
        joined = " ".join(cmd)
        for flag in ("--network=none", "--read-only", "--cap-drop=ALL", "--memory=512m", "--cpus=1.0",
                     "no-new-privileges", "--pids-limit=256"):
            self.assertIn(flag, joined)
        self.assertIn(":/workspace:ro", joined)  # workspace mounted read-only
        self.assertNotIn("shell", joined)

    def test_uses_docker_when_available(self):
        runner = DockerSandboxRunner()
        fake = subprocess.CompletedProcess([], 0, stdout="from-container\n", stderr="")
        with mock.patch.object(DockerSandboxRunner, "_probe_docker", return_value=True), \
                mock.patch("src.features.docker_sandbox.subprocess.run", return_value=fake) as run:
            res = runner.run("echo hi")
        self.assertTrue(res.isolated_in_docker)
        self.assertEqual(run.call_args[0][0][:2], ["docker", "run"])


# ----------------------------------------------------------------------------- gateways
class TestGateways(unittest.TestCase):
    def test_unconfigured_gateways_do_not_pretend_to_connect(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            for k in ("OMNI_TELEGRAM_TOKEN", "OMNI_DISCORD_TOKEN", "OMNI_SLACK_TOKEN", "OMNI_WHATSAPP_TOKEN"):
                os.environ.pop(k, None)
            mgr = ChatGatewayManager()
            lines = mgr.start_all()
            self.assertTrue(all("not started" in l for l in lines), lines)
            self.assertFalse(any(s["connected"] for s in mgr.get_status().values()))

    def test_only_telegram_claims_inbound(self):
        status = ChatGatewayManager().get_status()
        self.assertTrue(status["telegram"]["inbound"])
        for p in ("discord", "slack", "whatsapp"):
            self.assertFalse(status[p]["inbound"])
            self.assertTrue(status[p]["note"])

    def test_inbound_message_is_routed_to_agent_and_reply_is_sent(self):
        mgr = ChatGatewayManager(message_router=lambda m: f"agent says: {m.content.upper()}")
        tg = mgr.gateways["telegram"]
        with mock.patch.object(tg, "send_message", return_value=True) as send:
            reply = tg.handle_raw_update({"message": {
                "message_id": 1, "chat": {"id": 77}, "from": {"id": 5, "username": "alice"}, "text": "ping"}})
        self.assertEqual(reply, "agent says: PING")
        send.assert_called_once_with("77", "agent says: PING")

    def test_configured_token_allows_start(self):
        with mock.patch.dict(os.environ, {"OMNI_TELEGRAM_TOKEN": "123:abc"}):
            gw = ChatGatewayManager().gateways["telegram"]
            with mock.patch.object(gw, "_polling_loop"):
                gw.start()
            self.assertTrue(gw.is_connected)
            gw.stop()


# ----------------------------------------------------------------------------- node pairing
class TestNodePairing(unittest.TestCase):
    def setUp(self):
        self.path = os.path.join(tempfile.mkdtemp(dir=_isolation.TMP), "nodes.json")
        self.n = NodePairingManager(storage_path=self.path)

    def test_pair_flow_persists(self):
        req = self.n.create_pairing_request()
        self.assertIn(req.token, self.n.get_pairing_url(req))
        node = self.n.approve_pairing(req.request_id, "Phone", "android")
        self.assertIsNotNone(node)
        self.assertIn(node.id, NodePairingManager(storage_path=self.path).nodes)  # survives reload
        self.assertIsNone(self.n.approve_pairing(req.request_id, "Again", "x"))  # single use

    def test_expired_request_rejected(self):
        req = self.n.create_pairing_request(ttl_sec=-1)
        self.assertIsNone(self.n.approve_pairing(req.request_id, "Late", "x"))

    def test_remote_dispatch_is_honestly_unimplemented(self):
        node = next(iter(self.n.nodes.values()))
        res = self.n.dispatch_remote_task(node.id, "system.run", {"command": "dir"})
        self.assertFalse(res["success"])
        self.assertIn("not_implemented", res["error"])


# ----------------------------------------------------------------------------- local engine
class _FakeLlama(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        self.send_response(200 if self.path == "/health" else 404)
        self.end_headers()
        self.wfile.write(b"{}")

    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length", 0)))  # drain the request before replying
        content = ('Let me compute.\n<tool_call>{"name":"calculator","arguments":{"expression":"6*7"}}</tool_call>')
        body = json.dumps({"choices": [{"message": {"role": "assistant", "content": content}}]}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body)


class TestLocalEngine(unittest.TestCase):
    def test_start_reports_missing_binary(self):
        engine = LocalEngineRunner()
        with mock.patch("src.features.local_engine.shutil.which", return_value=None):
            with self.assertRaises(LocalEngineError) as cm:
                engine.start()
        self.assertIn("not found", str(cm.exception))

    def test_query_without_server_errors_instead_of_faking(self):
        engine = LocalEngineRunner(LocalModelConfig(port=free_port()))
        with self.assertRaises(LocalEngineError):
            engine.query([{"role": "user", "content": "hi"}])

    def test_hermes_tool_calls_are_parsed_from_real_http_response(self):
        port = free_port()
        server = http.server.ThreadingHTTPServer(("127.0.0.1", port), _FakeLlama)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            engine = LocalEngineRunner(LocalModelConfig(port=port))
            self.assertTrue(engine.is_running())
            msg = engine.query([{"role": "user", "content": "6*7?"}])
            self.assertEqual(msg["tool_calls"][0]["name"], "calculator")
            self.assertEqual(msg["tool_calls"][0]["arguments"], {"expression": "6*7"})
            self.assertNotIn("<tool_call>", msg["content"])
        finally:
            server.shutdown()
            server.server_close()


# ----------------------------------------------------------------------------- voice
class TestVoice(unittest.TestCase):
    def test_listening_is_not_faked(self):
        with self.assertRaises(VoiceUnavailable):
            VoiceLoopManager().start_listening()

    def test_speech_text_never_enters_the_script(self):
        v = VoiceLoopManager()
        evil = "hello'; Remove-Item -Recurse C:\\ ; '"
        with mock.patch.object(VoiceLoopManager, "tts_available", return_value=True), \
                mock.patch("src.features.voice_loop.subprocess.Popen") as popen:
            self.assertTrue(v.speak(evil))
        argv = popen.call_args[0][0]
        self.assertNotIn("Remove-Item", " ".join(argv))  # text is NOT in the command line / script
        self.assertIn("Remove-Item", popen.call_args[1]["env"]["OMNI_TTS_TEXT"])  # passed as data via env

    def test_clean_for_speech_strips_markdown_and_code(self):
        self.assertEqual(VoiceLoopManager.clean_for_speech("**bold** `x`"), "bold x")
        self.assertIn("code block", VoiceLoopManager.clean_for_speech("a ```py\nx=1\n``` b"))


# ----------------------------------------------------------------------------- misc stores / tools
class TestStoresAndTools(unittest.TestCase):
    def test_metaharness_records_and_summarises(self):
        m = MetaHarnessManager(db_path=os.path.join(_isolation.TMP, "bench.sqlite"))
        exp = m.create_experiment("e1", "swe_bench")
        m.record_run(EvaluationRun(id="r1", experiment_id=exp, task_id="t1", model="m", status="resolved",
                                   score=1.0, tokens_in=100, tokens_out=50, cost_usd=0.01))
        m.record_run(EvaluationRun(id="r2", experiment_id=exp, task_id="t2", model="m", status="failed",
                                   score=0.0, tokens_in=10, tokens_out=5, cost_usd=0.02))
        s = m.get_experiment_summary(exp)
        self.assertEqual((s["total_runs"], s["resolve_rate"], s["total_tokens"]), (2, 50.0, 165))
        self.assertEqual(m.list_experiments()[0]["runs"], 2)

    def test_native_diff_counts_and_regex(self):
        d = native_accel.fast_diff("a\nb\n", "a\nc\nd\n", "f.txt")
        self.assertEqual((d.additions, d.deletions), (2, 1))
        self.assertEqual([m[2] for m in native_accel.scan_tokens_regex("x1 y22", r"\d+")], ["1", "22"])
        self.assertTrue(native_accel.verify_digest(b"omni", __import__("hashlib").sha256(b"omni").hexdigest()))
        self.assertFalse(native_accel.verify_digest(b"omni", "0" * 64))

    def test_paperclip_ticket_store_is_local(self):
        p = PaperclipAdapter(config_file=os.path.join(_isolation.TMP, "tickets.json"))
        tid = p.list_tickets()[0].id
        self.assertTrue(p.submit_deliverable(tid, "done")["success"])
        self.assertFalse(p.submit_deliverable("NOPE", "x")["success"])
        self.assertEqual(PaperclipAdapter(config_file=p.config_file).tickets[tid].status, "submitted")

    def test_lsp_finds_syntax_errors_and_symbols(self):
        f = os.path.join(_isolation.TMP, "bad.py")
        with open(f, "w", encoding="utf-8") as fh:
            fh.write("class A:\n    def m(self):\n        return (\n")
        diags = LspClient().get_diagnostics(f)
        self.assertEqual(len(diags), 1)
        self.assertEqual(diags[0].severity, "error")
        good = os.path.join(_isolation.TMP, "good.py")
        with open(good, "w", encoding="utf-8") as fh:
            fh.write("class A:\n    def m(self): pass\n\ndef f(): pass\n")
        names = {s.name for s in LspClient().get_symbols(good)}
        self.assertEqual(names, {"A", "m", "f"})
        self.assertEqual(LspClient().find_definition(good, "f")["line"], 4)


# ----------------------------------------------------------------------------- worktrees (real git)
class TestShadowWorktree(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(dir=_isolation.TMP)
        self.repo = os.path.join(self.tmp, "repo")
        os.makedirs(self.repo)
        self.git("init", "-q", "-b", "main")
        self.write("a.py", "x = 1\n")
        self.git("add", ".")
        self.git("commit", "-qm", "init")
        self.m = ShadowWorktreeManager(repo_dir=self.repo, base_dir=os.path.join(self.tmp, "wts"))

    def git(self, *a):
        return subprocess.run(["git", *a], cwd=self.repo, capture_output=True, text=True).stdout.strip()

    def write(self, name, content, root=None):
        with open(os.path.join(root or self.repo, name), "w", encoding="utf-8") as f:
            f.write(content)

    def read(self, name, root=None):
        with open(os.path.join(root or self.repo, name), encoding="utf-8") as f:
            return f.read()

    def test_uncommitted_changes_are_carried_over(self):
        self.write("a.py", "x = 2\n")
        _, _, wt = self.m.create_worktree("carry")
        self.assertEqual(self.read("a.py", wt.path), "x = 2\n")

    def test_failing_tests_discard_and_leave_main_untouched(self):
        _, _, wt = self.m.create_worktree("fail")
        self.write("a.py", "x = 999\n", root=wt.path)
        out = self.m.verify_and_merge(wt.id, [sys.executable, "-c", "import sys; sys.exit(1)"])
        self.assertFalse(out.passed)
        self.assertFalse(out.merged)
        self.assertEqual(self.read("a.py"), "x = 1\n")
        self.assertNotIn("omni/", self.git("branch"))
        self.assertFalse(os.path.exists(wt.path))

    def test_passing_tests_merge_into_main(self):
        _, _, wt = self.m.create_worktree("pass")
        self.write("b.py", "y = 1\n", root=wt.path)
        out = self.m.verify_and_merge(wt.id, [sys.executable, "-c", "print('ok')"])
        self.assertTrue(out.passed and out.merged, out.message)
        self.assertTrue(os.path.exists(os.path.join(self.repo, "b.py")))

    def test_not_a_git_repo_raises_instead_of_simulating(self):
        with self.assertRaises(WorktreeError):
            ShadowWorktreeManager(repo_dir=self.tmp, base_dir=os.path.join(self.tmp, "w2")).create_worktree()


# ----------------------------------------------------------------------------- slash commands share state
class TestSlashCommandsUseSharedServices(unittest.TestCase):
    def setUp(self):
        services.shutdown()
        self.exec = CommandExecutor(command_registry=CommandRegistry(active_profile="full"))
        self.state = SessionState()
        self.loop = asyncio.new_event_loop()

    def tearDown(self):
        services.shutdown()
        self.loop.close()

    def run_cmd(self, line):
        handled, out = self.loop.run_until_complete(self.exec.execute(line, self.state))
        self.assertTrue(handled, f"{line} must be handled")
        return out

    def test_collab_state_persists_between_commands(self):
        first = self.run_cmd("/collab view")
        self.assertIn("view", first)
        status = self.run_cmd("/collab status")
        self.assertIn("view", status)
        self.assertEqual(len(services.collab.rooms), 1)
        self.assertIn("stopped", self.run_cmd("/collab stop"))
        self.assertEqual(len(services.collab.rooms), 0)

    def test_gateway_start_and_status_agree(self):
        with mock.patch.dict(os.environ, {"OMNI_TELEGRAM_TOKEN": "1:x"}):
            for k in ("OMNI_DISCORD_TOKEN", "OMNI_SLACK_TOKEN", "OMNI_WHATSAPP_TOKEN"):
                os.environ.pop(k, None)
            with mock.patch("src.features.gateways.telegram.TelegramChatGateway._polling_loop"):
                self.assertIn("started", self.run_cmd("/gateway start telegram"))
                status = self.run_cmd("/gateway")
        tg_line = next(l for l in status.splitlines() if l.strip().startswith("telegram"))
        self.assertIn("connected", tg_line)

    def test_sandbox_refuses_without_docker(self):
        with mock.patch.object(DockerSandboxRunner, "_probe_docker", return_value=False):
            out = self.run_cmd("/sandbox echo hi")
        self.assertIn("refused", out)
        self.assertIn("NOT run", out)

    def test_browser_without_browser_is_a_clean_message(self):
        services.browser.port = free_port()
        services.browser.base_url = f"http://127.0.0.1:{services.browser.port}"
        out = self.run_cmd("/browser tabs")
        self.assertTrue(out.startswith("[!] /browser failed:"))
        self.assertNotIn("Traceback", out)
        self.assertIn("launch", out)

    def test_usage_errors_are_friendly(self):
        self.assertIn("Usage:", self.run_cmd("/browser eval"))
        self.assertIn("Usage:", self.run_cmd("/canvas create onlytitle"))
        self.assertIn("not implemented", self.run_cmd("/eval run"))
        self.assertIn("not implemented", self.run_cmd("/voice start").lower())

    def test_worktree_commands_work_in_scratch_workspace(self):
        out = self.run_cmd("/worktree create")
        self.assertIn("Created worktree", out)
        listing = self.run_cmd("/worktree list")
        wid = next(l.split("]")[0].strip(" [") for l in listing.splitlines() if l.strip().startswith("[wt_"))
        self.assertIn("PASS", self.run_cmd(f"/worktree verify {wid} python -c pass"))
        self.assertIn("(0)", self.run_cmd("/worktree list"))

    def test_pair_and_nodes_headers(self):
        self.assertIn("OpenClaw Paired Nodes", self.run_cmd("/nodes"))
        self.assertIn("Companion Device Pairing", self.run_cmd("/pair"))
        self.assertIn("Pairing URL", self.run_cmd("/pair qr"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
