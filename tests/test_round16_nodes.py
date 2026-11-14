"""Remote nodes: the node server, the `node` tool that talks to it, and the `omni node` commands. Offline."""
import contextlib
import http.client
import io
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from test_capabilities import CapCase  # noqa: E402
from src.pi import caps, node_server  # noqa: E402
from src.pi.caps import nodes  # noqa: E402
from src.pi.tools.base import ToolError  # noqa: E402

# works whether or not `nodes` is in the built-in module list yet
caps.load_builtin()
[caps.register(c) for c in nodes.CAPABILITIES]

PY = f'"{sys.executable}"'


def alive(pid: int) -> bool:
    if os.name == "nt":
        out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"], capture_output=True, text=True).stdout
        return str(pid) in out
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Round16(CapCase):
    def setUp(self):
        super().setUp()
        self.root = Path(tempfile.mkdtemp(prefix="omni-node-root-")).resolve()
        self.servers = []

    def tearDown(self):
        for srv in self.servers:
            srv.shutdown()
        deadline = time.time() + 5
        while time.time() < deadline and any(t.name.startswith("omni-node-") and t.is_alive() for t in threading.enumerate()):
            time.sleep(0.05)
        self.assertEqual([t.name for t in threading.enumerate() if t.name.startswith("omni-node-") and t.is_alive()], [])
        super().tearDown()

    def serve(self, **kw):
        srv = node_server.serve(str(kw.pop("root", self.root)), port=0, **kw)
        self.servers.append(srv)
        return srv

    def profile(self, capabilities=("nodes",), **extra):
        (self.profiles / "r16.json").write_text(json.dumps({"name": "r16", "extends": "lean", "capabilities": list(capabilities), **extra}))
        return self.session(profile="r16")

    def add(self, name, srv, token=None):
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(node_server.cli(["add", name, srv.url, token or srv.token], self.agent), 0)

    def node(self, s, **args):
        return self.call(s, "node", args)


class TestNodeTool(Round16):
    def test_every_action_end_to_end(self):
        srv = self.serve()
        self.assertTrue(srv.url.startswith("http://127.0.0.1:"))
        self.assertGreaterEqual(len(srv.token), 24)
        s = self.profile()
        self.assertIn("node", s.all_tools())
        self.assertIn("None", self.text(self.node(s, action="list")) + "None")
        self.assertEqual(self.node(s, action="list")["details"]["count"], 0)
        self.add("box", srv)

        listing = self.node(s, action="list")
        self.assertIn("box", self.text(listing))
        self.assertIn(srv.url, self.text(listing))
        self.assertNotIn(srv.token, self.text(listing))

        info = self.node(s, action="info", node="box")
        self.assertFalse(info.get("isError"), self.text(info))
        self.assertEqual(info["details"]["hostname"], socket.gethostname())
        self.assertEqual(Path(info["details"]["root"]), self.root)
        self.assertFalse(info["details"]["read_only"])
        self.assertTrue(info["details"]["platform"])

        w = self.node(s, action="write", node="box", path="sub/dir/notes.txt", content="one\ntwo\nthree\nfour\n")
        self.assertFalse(w.get("isError"), self.text(w))
        self.assertEqual((self.root / "sub" / "dir" / "notes.txt").read_bytes(), b"one\ntwo\nthree\nfour\n")
        self.assertEqual(w["details"]["bytes"], 19)

        r = self.node(s, action="read", node="box", path="sub/dir/notes.txt")
        self.assertEqual(self.text(r), "one\ntwo\nthree\nfour")
        self.assertEqual(r["details"]["totalLines"], 4)
        part = self.text(self.node(s, action="read", node="box", path="sub/dir/notes.txt", offset=2, limit=2))
        self.assertTrue(part.startswith("two\nthree\n[lines 2-3 of 4"), part)
        self.assertTrue(self.node(s, action="read", node="box", path="missing.txt").get("isError"))
        self.assertTrue(self.node(s, action="read", node="box").get("isError"))

        (self.root / "top.txt").write_text("hello")
        ls = self.node(s, action="ls", node="box")
        self.assertEqual(self.text(ls).splitlines(), ["sub/", "top.txt  (5 bytes)"])
        self.assertEqual(self.text(self.node(s, action="ls", node="box", path="sub/dir")), "notes.txt  (19 bytes)")
        self.assertTrue(self.node(s, action="ls", node="box", path="nope").get("isError"))

        x = self.node(s, action="exec", node="box", command=f"{PY} -c \"import os,sys; print(os.getcwd()); sys.stderr.write('warn\\n')\"")
        self.assertFalse(x.get("isError"), self.text(x))
        self.assertIn(self.root, [Path(line) for line in self.text(x).splitlines()])   # runs in the node's root
        self.assertIn("warn", self.text(x))                                  # stderr is merged in
        self.assertEqual(x["details"], {"exitCode": 0, "timedOut": False, "truncated": False})
        bad = self.node(s, action="exec", node="box", command=f"{PY} -c \"print('boom'); raise SystemExit(3)\"")
        self.assertEqual(self.text(bad), "boom\n[exit code 3]")
        self.assertEqual(bad["details"]["exitCode"], 3)
        self.assertTrue(self.node(s, action="exec", node="box", command=" ").get("isError"))
        self.assertTrue(self.node(s, action="frobnicate", node="box").get("isError"))

        status = s.caps.commands()["nodes"][1]("")
        self.assertIn("box", status)
        self.assertIn("ok: " + socket.gethostname(), status)

    def test_wrong_token_unknown_and_unreachable(self):
        srv = self.serve(token="right-token")
        self.assertEqual(srv.token, "right-token")
        self.add("good", srv)
        self.add("badtoken", srv, token="wrong-token")
        with contextlib.redirect_stdout(io.StringIO()):
            node_server.cli(["add", "gone", f"http://127.0.0.1:{free_port()}", "t"], self.agent)
        s = self.profile()
        self.assertFalse(self.node(s, action="info", node="good").get("isError"))
        r = self.node(s, action="info", node="badtoken")
        self.assertTrue(r.get("isError"))
        self.assertIn("rejected the token (401)", self.text(r))
        r = self.node(s, action="exec", node="badtoken", command="echo hi")
        self.assertIn("401", self.text(r))
        r = self.node(s, action="info", node="nope")
        self.assertTrue(r.get("isError"))
        self.assertIn("no node 'nope'", self.text(r))
        self.assertIn("badtoken, gone, good", self.text(r))
        r = self.node(s, action="info", node="gone")
        self.assertTrue(r.get("isError"))
        self.assertIn("node 'gone' is unreachable at http://127.0.0.1:", self.text(r))
        status = s.caps.commands()["nodes"][1]("")
        self.assertEqual(status.count("NOT ANSWERING"), 2)
        self.assertEqual(status.count("ok: "), 1)

    def test_raw_protocol_auth_and_limits(self):
        srv = self.serve()
        host, port = srv.server_address[0], srv.server_address[1]

        def post(path, body=b"{}", headers=None, send_body=True):
            c = http.client.HTTPConnection(host, port, timeout=10)
            try:
                c.putrequest("POST", path)
                for k, v in ({"Content-Length": str(len(body)), **(headers or {})}).items():
                    c.putheader(k, v)
                c.endheaders(body if send_body else None)
                resp = c.getresponse()
                return resp.status, json.loads(resp.read())
            finally:
                c.close()
        auth = {"Authorization": f"Bearer {srv.token}"}
        self.assertEqual(post("/v1/info")[0], 401)                                         # no header
        self.assertEqual(post("/v1/info", headers={"Authorization": "Bearer nope"})[0], 401)
        self.assertEqual(post("/v1/info", headers={"Authorization": srv.token})[0], 401)   # not a Bearer header
        self.assertEqual(post("/v1/info", headers={"Authorization": "Bearer té"})[0], 401)
        self.assertEqual(post("/v1/exec", b'{"command": "echo x"}', {"Authorization": "Bearer nope"})[0], 401)
        status, info = post("/v1/info", headers=auth)
        self.assertEqual((status, info["read_only"], Path(info["root"])), (200, False, self.root))
        self.assertEqual(post("/v1/nothing", headers=auth)[0], 404)
        self.assertEqual(post("/v1/info", b"[1]", auth)[0], 400)
        self.assertEqual(post("/v1/info", b"not json", auth)[0], 400)
        status, err = post("/v1/write", headers={**auth, "Content-Length": str(node_server.MAX_BODY + 1)}, send_body=False)
        self.assertEqual(status, 413)
        self.assertIn("5 MB", err["error"])
        c = http.client.HTTPConnection(host, port, timeout=10)
        c.request("GET", "/v1/info", headers=auth)
        self.assertEqual(c.getresponse().status, 405)
        c.close()
        # the tool refuses an oversized write before sending it
        self.add("box", srv)
        r = self.node(self.profile(), action="write", node="box", path="big.bin", content="x" * (node_server.MAX_BODY + 1))
        self.assertTrue(r.get("isError"))
        self.assertIn("5 MB", self.text(r))
        self.assertFalse((self.root / "big.bin").exists())

    def test_path_escape(self):
        outside = Path(tempfile.mkdtemp(prefix="omni-node-outside-")).resolve()
        (outside / "secret.txt").write_text("secret")
        srv = self.serve()
        self.add("box", srv)
        s = self.profile()
        for path in ("../" + outside.name + "/secret.txt", str(outside / "secret.txt"), "a/../../" + outside.name + "/secret.txt"):
            r = self.node(s, action="read", node="box", path=path)
            self.assertTrue(r.get("isError"), path)
            self.assertIn("path escapes the node root", self.text(r))
            self.assertIn("(400)", self.text(r))
            self.assertNotIn("secret\n", self.text(r) + "\n")
        r = self.node(s, action="write", node="box", path="../escaped.txt", content="x")
        self.assertIn("path escapes the node root", self.text(r))
        self.assertFalse((self.root.parent / "escaped.txt").exists())
        self.assertIn("path escapes the node root", self.text(self.node(s, action="ls", node="box", path="..")))
        with self.assertRaises(ToolError) as cm:   # exec's cwd is confined too
            nodes.request("box", {"url": srv.url, "token": srv.token}, "/v1/exec", {"command": "echo hi", "cwd": ".."})
        self.assertIn("path escapes the node root", str(cm.exception))
        (self.root / "inner").mkdir()
        ok = nodes.request("box", {"url": srv.url, "token": srv.token}, "/v1/exec",
                           {"command": f"{PY} -c \"import os; print(os.path.basename(os.getcwd()))\"", "cwd": "inner"})
        self.assertEqual(ok["output"].strip(), "inner")

    def test_read_only_mode(self):
        (self.root / "a.txt").write_text("alpha\n")
        srv = self.serve(allow_write=False)
        self.add("ro", srv)
        s = self.profile()
        info = self.node(s, action="info", node="ro")
        self.assertTrue(info["details"]["read_only"])
        self.assertIn("read-only", self.text(info))
        self.assertEqual(self.text(self.node(s, action="read", node="ro", path="a.txt")), "alpha")
        self.assertIn("a.txt", self.text(self.node(s, action="ls", node="ro")))
        w = self.node(s, action="write", node="ro", path="b.txt", content="x")
        self.assertTrue(w.get("isError"))
        self.assertIn("read-only", self.text(w))
        self.assertIn("(403)", self.text(w))
        self.assertFalse((self.root / "b.txt").exists())
        marker = self.root / "ran.txt"
        x = self.node(s, action="exec", node="ro", command=f"{PY} -c \"open('ran.txt','w').write('x')\"")
        self.assertTrue(x.get("isError"))
        self.assertIn("(403)", self.text(x))
        self.assertFalse(marker.exists())

    def test_output_cap_keeps_the_tail(self):
        srv = self.serve()
        self.add("box", srv)
        s = self.profile()
        raw = nodes.request("box", {"url": srv.url, "token": srv.token}, "/v1/exec",
                            {"command": f"{PY} -c \"print('HEAD' + 'x' * 200000 + 'TAIL')\""})
        self.assertEqual(len(raw["output"]), node_server.MAX_OUTPUT)
        self.assertTrue(raw["output"].rstrip("\n").endswith("TAIL"))
        self.assertNotIn("HEAD", raw["output"])
        self.assertTrue(raw["truncated"])
        r = self.node(s, action="exec", node="box", command=f"{PY} -c \"print('HEAD' + 'x' * 200000 + 'TAIL')\"")
        self.assertTrue(self.text(r).startswith("[earlier output dropped"))
        self.assertTrue(self.text(r).endswith("TAIL"))
        self.assertTrue(r["details"]["truncated"])
        small = self.node(s, action="exec", node="box", command=f"{PY} -c \"print('x' * 1000)\"")
        self.assertFalse(small["details"]["truncated"])

    def test_timeout_kills_the_command(self):
        srv = self.serve()
        self.add("box", srv)
        s = self.profile()
        started = time.time()
        r = self.node(s, action="exec", node="box", timeout=1.5, command=(
            f"{PY} -u -c \"import os,time; open('pid.txt','w').write(str(os.getpid())); print('started'); time.sleep(60)\""))
        self.assertLess(time.time() - started, 30)
        self.assertFalse(r.get("isError"), self.text(r))
        self.assertTrue(r["details"]["timedOut"])
        self.assertIn("started", self.text(r))
        self.assertIn("timed out after 1.5s", self.text(r))
        pid = int((self.root / "pid.txt").read_text())
        deadline = time.time() + 10
        while alive(pid) and time.time() < deadline:
            time.sleep(0.2)
        self.assertFalse(alive(pid), "the timed-out command was left running")
        # the timeout is clamped to 600 seconds on both sides
        with mock.patch.object(nodes, "request", return_value={"exit_code": 0, "output": "", "timed_out": False}) as req:
            self.node(s, action="exec", node="box", command="echo hi", timeout=99999)
        self.assertEqual(req.call_args[0][3]["timeout"], 600)
        with mock.patch.object(node_server, "run_command", return_value={}) as run:
            node_server.Node(str(self.root)).exec({"command": "echo hi", "timeout": 99999})
        self.assertEqual(run.call_args[0][2], 600)

    def test_profile_nodes_and_env_token(self):
        srv = self.serve()
        self.add("filenode", srv, token="stale-token")
        os.environ.pop("OMNI_R16_NODE_TOKEN", None)
        s = self.profile(nodes={"envnode": {"url": srv.url + "/", "token": "$OMNI_R16_NODE_TOKEN"},
                                "filenode": {"url": srv.url, "token": srv.token}})      # the profile wins over nodes.json
        self.assertEqual(self.node(s, action="list")["details"]["nodes"], ["envnode", "filenode"])
        r = self.node(s, action="info", node="envnode")
        self.assertTrue(r.get("isError"))
        self.assertIn("OMNI_R16_NODE_TOKEN", self.text(r))
        self.assertIn("not set", self.text(r))
        os.environ["OMNI_R16_NODE_TOKEN"] = srv.token
        try:
            self.assertFalse(self.node(s, action="info", node="envnode").get("isError"))
            os.environ["OMNI_R16_NODE_TOKEN"] = "wrong"
            self.assertIn("401", self.text(self.node(s, action="info", node="envnode")))
        finally:
            os.environ.pop("OMNI_R16_NODE_TOKEN", None)
        self.assertFalse(self.node(s, action="info", node="filenode").get("isError"))

    def test_exec_goes_through_the_bash_guards(self):
        s = self.profile(["nodes", "approvals"])
        why = s.caps.gate("node", {"action": "exec", "node": "box", "command": "rm -rf /tmp/x"})
        self.assertEqual(why, s.caps.gate("bash", {"command": "rm -rf /tmp/x"}))
        self.assertIn("recursive delete needs the user's approval", why)
        self.assertIsNone(s.caps.gate("node", {"action": "exec", "node": "box", "command": "ls"}))
        self.assertIsNone(s.caps.gate("node", {"action": "read", "node": "box", "path": "rm -rf x"}))
        self.assertIsNone(s.caps.gate("node", {"action": "list"}))
        seen = []
        with mock.patch.object(s.caps, "gate", side_effect=lambda t, a: seen.append((t, a)) or "blocked"):
            self.assertEqual(nodes._gate(s, "node", {"action": "exec", "command": "make deploy"}), "blocked")
            self.assertIsNone(nodes._gate(s, "node", {"action": "write", "content": "x"}))
            self.assertIsNone(nodes._gate(s, "bash", {"command": "x"}))
        self.assertEqual(seen, [("bash", {"command": "make deploy"})])


class TestNodeCli(Round16):
    def run_cli(self, *argv):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = node_server.cli(list(argv), self.agent)
        return code, out.getvalue()

    def test_list_add_remove(self):
        code, out = self.run_cli("list")
        self.assertEqual(code, 0)
        self.assertIn("No nodes configured", out)
        self.assertEqual(self.run_cli("add", "box", "http://127.0.0.1:8765/", "tok-123")[0], 0)
        self.assertEqual(self.run_cli("add", "pi", "http://10.0.0.5:9000", "$PI_TOKEN")[0], 0)
        self.assertEqual(json.loads((self.agent / "nodes.json").read_text()), {"nodes": {
            "box": {"url": "http://127.0.0.1:8765", "token": "tok-123"}, "pi": {"url": "http://10.0.0.5:9000", "token": "$PI_TOKEN"}}})
        code, out = self.run_cli("node", "list")              # a leading "node" is tolerated
        self.assertEqual(code, 0)
        self.assertIn("http://127.0.0.1:8765", out)
        self.assertIn("$PI_TOKEN", out)
        self.assertNotIn("tok-123", out)                      # a literal token is not echoed
        code, out = self.run_cli("add", "box", "http://127.0.0.1:1", "new")
        self.assertIn("Updated node 'box'", out)
        self.assertEqual(node_server.load_nodes(self.agent)["box"], {"url": "http://127.0.0.1:1", "token": "new"})
        self.assertEqual(self.run_cli("add", "x", "ftp://nope", "t")[0], 2)
        self.assertEqual(self.run_cli("add", "x")[0], 2)
        self.assertEqual(self.run_cli("remove", "nope")[0], 1)
        self.assertEqual(self.run_cli("remove")[0], 2)
        self.assertEqual(self.run_cli("remove", "box")[0], 0)
        self.assertEqual(list(node_server.load_nodes(self.agent)), ["pi"])
        self.assertEqual(self.run_cli("frob")[0], 2)
        self.assertEqual(self.run_cli("--help")[0], 0)
        (self.agent / "nodes.json").write_text("{broken")
        self.assertEqual(node_server.load_nodes(self.agent), {})

    def test_serve_prints_url_and_token_and_stops(self):
        started = []
        real = node_server.serve

        def spy(*a, **kw):
            started.append(real(*a, **kw))
            return started[-1]
        with mock.patch.object(node_server, "serve", side_effect=spy), \
                mock.patch.object(node_server.time, "sleep", side_effect=KeyboardInterrupt):
            code, out = self.run_cli("serve", "--root", str(self.root), "--port=0", "--read-only")
        self.assertEqual(code, 0)
        srv = started[0]
        self.assertIn(srv.url, out)
        self.assertIn(srv.token, out)
        self.assertGreaterEqual(len(srv.token), 32)           # secrets.token_urlsafe(24)
        self.assertTrue(srv.read_only)
        self.assertIn("(read-only)", out)
        self.assertNotIn("WARNING", out)
        self.assertFalse(srv._thread.is_alive())              # Ctrl+C shut it down
        with self.assertRaises(OSError):
            socket.create_connection(srv.server_address[:2], timeout=2).close()

        fake = mock.Mock(url="http://box:8765", token="given", root=str(self.root), _thread=None)
        with mock.patch.object(node_server, "serve", return_value=fake) as serve:
            code, out = self.run_cli("serve", "--root", str(self.root), "--host", "0.0.0.0", "--port", "9001", "--token", "given")
        self.assertEqual(code, 0)
        self.assertEqual(serve.call_args[0], (str(self.root), "0.0.0.0", 9001, "given"))
        self.assertEqual(serve.call_args[1], {"allow_write": True})
        self.assertIn("WARNING", out)
        self.assertIn("plain HTTP", out)
        self.assertIn("tunnel", out)
        fake.shutdown.assert_called_once()
        self.assertEqual(self.run_cli("serve", "--port", "abc")[0], 2)
        self.assertEqual(self.run_cli("serve", "--bogus")[0], 2)
        self.assertEqual(self.run_cli("serve", "--root")[0], 2)
        self.assertEqual(self.run_cli("serve", "--root", str(self.root / "missing"), "--port", "0")[0], 1)


if __name__ == "__main__":
    unittest.main()
