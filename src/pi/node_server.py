"""The Omni node server: lets an agent on another machine run commands and read/write files here.

Start it on the machine you want to reach, in the folder the agent may work in:

    omni node serve --root ~/project            # prints the URL and a freshly generated token
    omni node serve --root /srv/logs --read-only   # files can be read and listed; no writes, no commands

Then, on the machine where the agent runs, register it and turn on the `nodes` capability:

    omni node add buildbox http://127.0.0.1:8765 <token>
    omni node list
    omni node remove buildbox

Everything is confined to `--root`: a path that resolves outside it is refused. Every request must carry the token.

The traffic is PLAIN HTTP - the token and everything sent travel unencrypted. The server therefore listens on
127.0.0.1 unless told otherwise; reach it from another machine through an SSH tunnel (`ssh -L 8765:127.0.0.1:8765
host`), a VPN or a tailnet instead of opening `--host 0.0.0.0` to a network you do not control.

Protocol (all POST, JSON in and out, `Authorization: Bearer <token>`): /v1/info, /v1/exec, /v1/read, /v1/write,
/v1/list. Errors are `{"error": "..."}` with status 400 (bad request / path escape), 401 (token), 403 (read-only),
404 (no such file or endpoint), 413 (body over 5 MB).
"""
from __future__ import annotations

import hmac
import http.server
import json
import os
import platform
import secrets
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

NODES_FILE = "nodes.json"
MAX_BODY = 5 * 1024 * 1024
MAX_OUTPUT = 60_000
MAX_TIMEOUT = 600
DEFAULT_TIMEOUT = 120
DEFAULT_READ_LINES = 2000
MAX_ENTRIES = 2000
LOOPBACK = ("127.0.0.1", "localhost", "::1")


class NodeError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


# ------------------------------------------------------------------------------------------------ operations
def resolve_under(root: Path, rel: Any) -> Path:
    """`rel` resolved inside `root` (symlinks followed); anything that lands outside is refused."""
    if rel is None or rel == "":
        rel = "."
    if not isinstance(rel, str) or "\x00" in rel:
        raise NodeError(400, "path must be a string")
    try:
        target = (root / rel).resolve()
    except (OSError, ValueError) as e:
        raise NodeError(400, f"bad path: {e}") from None
    if target != root and root not in target.parents:
        raise NodeError(400, f"path escapes the node root: {rel}")
    return target


def _kill_tree(popen: subprocess.Popen) -> None:
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(popen.pid)], capture_output=True, timeout=10)
        else:
            import signal
            os.killpg(os.getpgid(popen.pid), signal.SIGKILL)
    except Exception:
        pass
    try:
        popen.kill()
    except Exception:
        pass


def run_command(command: str, cwd: Path, timeout: float) -> Dict[str, Any]:
    kwargs: Dict[str, Any] = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt" else {"start_new_session": True}
    try:
        popen = subprocess.Popen(command, shell=True, cwd=str(cwd), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                 stdin=subprocess.DEVNULL, **kwargs)
    except OSError as e:
        raise NodeError(400, f"could not start: {e}") from None
    # read on a thread, keeping only the tail, so a chatty command cannot exhaust memory or block on a full pipe
    chunks: List[bytes] = []
    state = {"size": 0, "dropped": False}
    keep = MAX_OUTPUT * 4  # bytes; enough for MAX_OUTPUT characters of any UTF-8 text

    def pump() -> None:
        assert popen.stdout is not None
        while True:
            data = popen.stdout.read1(65536)
            if not data:
                return
            chunks.append(data)
            state["size"] += len(data)
            while len(chunks) > 1 and state["size"] - len(chunks[0]) >= keep:
                state["size"] -= len(chunks.pop(0))
                state["dropped"] = True
    reader = threading.Thread(target=pump, daemon=True, name="omni-node-exec")
    reader.start()
    timed_out = False
    try:
        popen.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        _kill_tree(popen)
        try:
            popen.wait(timeout=10)
        except subprocess.TimeoutExpired:
            pass
    reader.join(timeout=5)  # a grandchild that survived may still hold the pipe; do not wait for it forever
    try:
        popen.stdout.close()  # type: ignore[union-attr]
    except Exception:
        pass
    text = b"".join(list(chunks)).decode("utf-8", errors="replace").replace("\r\n", "\n")
    truncated = state["dropped"] or len(text) > MAX_OUTPUT
    return {"exit_code": popen.returncode, "output": text[-MAX_OUTPUT:], "timed_out": timed_out, "truncated": truncated}


class Node:
    """The operations behind the endpoints; usable without HTTP."""

    def __init__(self, root: str, allow_write: bool = True):
        self.root = Path(root).expanduser().resolve()
        if not self.root.is_dir():
            raise ValueError(f"node root is not a directory: {self.root}")
        self.allow_write = allow_write

    def info(self, _p: Dict[str, Any]) -> Dict[str, Any]:
        return {"hostname": socket.gethostname(), "platform": f"{platform.system()} {platform.release()}".strip(),
                "root": str(self.root), "read_only": not self.allow_write}

    def exec(self, p: Dict[str, Any]) -> Dict[str, Any]:
        if not self.allow_write:
            raise NodeError(403, "this node is read-only: commands are not allowed")
        command = p.get("command")
        if not isinstance(command, str) or not command.strip():
            raise NodeError(400, "command is required")
        try:
            timeout = float(p.get("timeout") or DEFAULT_TIMEOUT)
        except (TypeError, ValueError):
            raise NodeError(400, "timeout must be a number of seconds") from None
        timeout = max(0.1, min(timeout, MAX_TIMEOUT))
        cwd = resolve_under(self.root, p.get("cwd"))
        if not cwd.is_dir():
            raise NodeError(400, f"cwd is not a directory: {p.get('cwd')}")
        return run_command(command, cwd, timeout)

    def read(self, p: Dict[str, Any]) -> Dict[str, Any]:
        target = resolve_under(self.root, p.get("path"))
        if not target.is_file():
            raise NodeError(404, f"no such file: {p.get('path')}")
        try:
            offset = max(1, int(p.get("offset") or 1))            # 1-based first line
            limit = max(1, int(p.get("limit") or DEFAULT_READ_LINES))
        except (TypeError, ValueError):
            raise NodeError(400, "offset and limit must be integers") from None
        try:
            lines = target.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError as e:
            raise NodeError(400, f"could not read: {e}") from None
        return {"content": "\n".join(lines[offset - 1:offset - 1 + limit]), "total_lines": len(lines)}

    def write(self, p: Dict[str, Any]) -> Dict[str, Any]:
        if not self.allow_write:
            raise NodeError(403, "this node is read-only: writes are not allowed")
        target = resolve_under(self.root, p.get("path"))
        content = p.get("content")
        if not isinstance(content, str):
            raise NodeError(400, "content must be a string")
        if target == self.root or target.is_dir():
            raise NodeError(400, f"path is a directory: {p.get('path')}")
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            data = content.encode("utf-8")
            target.write_bytes(data)
        except OSError as e:
            raise NodeError(400, f"could not write: {e}") from None
        return {"ok": True, "bytes": len(data)}

    def list(self, p: Dict[str, Any]) -> Dict[str, Any]:
        target = resolve_under(self.root, p.get("path"))
        if not target.is_dir():
            raise NodeError(404, f"no such directory: {p.get('path') or '.'}")
        entries = []
        try:
            for child in sorted(target.iterdir(), key=lambda c: c.name.lower()):
                try:
                    is_dir = child.is_dir()
                    size = 0 if is_dir else child.stat().st_size
                except OSError:
                    is_dir, size = False, 0
                entries.append({"name": child.name, "type": "dir" if is_dir else "file", "size": size})
        except OSError as e:
            raise NodeError(400, f"could not list: {e}") from None
        return {"entries": entries[:MAX_ENTRIES], "truncated": len(entries) > MAX_ENTRIES}


# ------------------------------------------------------------------------------------------------ HTTP
class _Handler(http.server.BaseHTTPRequestHandler):
    server_version = "OmniNode/1"
    protocol_version = "HTTP/1.1"

    def log_message(self, *a: Any) -> None:  # quiet: the caller decides what to print
        pass

    def _send(self, status: int, payload: Dict[str, Any], close: bool = False) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        if close:
            self.send_header("Connection", "close")
            self.close_connection = True
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        self._send(405, {"error": "use POST with a JSON body"}, close=True)

    def do_POST(self) -> None:
        srv: "NodeServer" = self.server  # type: ignore[assignment]
        given = self.headers.get("Authorization") or ""
        expected = f"Bearer {srv.token}"
        authorized = hmac.compare_digest(given.encode("utf-8", "replace"), expected.encode("utf-8"))
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return self._send(400, {"error": "bad Content-Length"}, close=True)
        if length < 0:
            return self._send(400, {"error": "bad Content-Length"}, close=True)
        if length > MAX_BODY:
            return self._send(413, {"error": f"request body over {MAX_BODY // (1024 * 1024)} MB"}, close=True)
        # the (capped) body is drained before a 401 goes out, so the client reads the answer instead of a reset
        raw = self.rfile.read(length) if length else b"{}"
        if not authorized:
            return self._send(401, {"error": "missing or wrong token"}, close=True)
        try:
            params = json.loads(raw.decode("utf-8") or "{}")
            if not isinstance(params, dict):
                raise ValueError("not an object")
        except (ValueError, UnicodeDecodeError):
            return self._send(400, {"error": "the body must be a JSON object"})
        ops = {"/v1/info": srv.node.info, "/v1/exec": srv.node.exec, "/v1/read": srv.node.read,
               "/v1/write": srv.node.write, "/v1/list": srv.node.list}
        op = ops.get(self.path.split("?", 1)[0].rstrip("/"))
        if op is None:
            return self._send(404, {"error": f"no such endpoint: {self.path}"})
        try:
            result = op(params)
        except NodeError as e:
            return self._send(e.status, {"error": str(e)})
        except Exception as e:  # never take the server down for one request
            return self._send(500, {"error": f"{type(e).__name__}: {e}"})
        try:
            self._send(200, result)
        except OSError:
            pass  # the client went away (e.g. its own timeout)


class NodeServer(http.server.ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False

    def __init__(self, addr: Tuple[str, int], node: Node, token: str):
        super().__init__(addr, _Handler)
        self.node = node
        self.token = token
        self.root = str(node.root)
        self.read_only = not node.allow_write
        host, port = self.server_address[0], self.server_address[1]
        shown = socket.gethostname() if host in ("0.0.0.0", "") else host
        self.url = f"http://{shown}:{port}"
        self._thread: Optional[threading.Thread] = None
        self._stopped = False

    def shutdown(self) -> None:
        """Stop serving, close the socket and wait for the serving thread. Safe to call twice."""
        if self._stopped:
            return
        self._stopped = True
        if self._thread is not None:
            super().shutdown()
            self._thread.join(timeout=5)
        self.server_close()


def serve(root: str, host: str = "127.0.0.1", port: int = 8765, token: Optional[str] = None, allow_write: bool = True) -> NodeServer:
    """Start a node server on a background thread and return it (`.url`, `.token`, `.shutdown()`).
    `port=0` picks a free port. Without a token a random one is generated."""
    server = NodeServer((host, int(port)), Node(root, allow_write), token or secrets.token_urlsafe(24))
    server._thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.1}, daemon=True, name="omni-node-server")
    server._thread.start()
    return server


# ------------------------------------------------------------------------------------------------ nodes.json
def nodes_path(agent_dir: Any) -> Path:
    return Path(agent_dir) / NODES_FILE


def load_nodes(agent_dir: Any) -> Dict[str, Dict[str, Any]]:
    """The `nodes` map of `<agent-dir>/nodes.json`; empty when the file is missing or malformed."""
    try:
        data = json.loads(nodes_path(agent_dir).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    nodes = data.get("nodes") if isinstance(data, dict) else None
    return {str(k): dict(v) for k, v in nodes.items() if isinstance(v, dict)} if isinstance(nodes, dict) else {}


def save_nodes(agent_dir: Any, nodes: Dict[str, Dict[str, Any]]) -> None:
    p = nodes_path(agent_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"nodes": nodes}, indent=2, sort_keys=True), encoding="utf-8")
    try:
        os.chmod(p, 0o600)  # it holds tokens
    except OSError:
        pass


# ------------------------------------------------------------------------------------------------ cli
USAGE = """usage: omni node serve [--root DIR] [--host H] [--port N] [--token T] [--read-only]
       omni node list
       omni node add NAME URL TOKEN      (TOKEN may be $ENV_VAR to read it from the environment)
       omni node remove NAME"""


def _serve_cli(args: List[str]) -> int:
    opts: Dict[str, Any] = {"root": ".", "host": "127.0.0.1", "port": "8765", "token": None}
    read_only = False
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--read-only":
            read_only = True
        elif a.startswith("--") and a[2:].split("=", 1)[0] in opts:
            key, _, value = a[2:].partition("=")
            if not _:
                i += 1
                if i >= len(args):
                    print(f"{a} needs a value\n{USAGE}")
                    return 2
                value = args[i]
            opts[key] = value
        else:
            print(f"unknown option: {a}\n{USAGE}")
            return 2
        i += 1
    try:
        port = int(opts["port"])
    except ValueError:
        print(f"--port must be a number, not {opts['port']!r}")
        return 2
    try:
        server = serve(opts["root"], opts["host"], port, opts["token"], allow_write=not read_only)
    except (OSError, ValueError, OverflowError) as e:
        print(f"could not start the node server: {e}")
        return 1
    print(f"Omni node serving {server.root}" + (" (read-only)" if read_only else ""))
    print(f"  url    {server.url}")
    print(f"  token  {server.token}")
    print(f"Register it where the agent runs:  omni node add NAME {server.url} {server.token}")
    if opts["host"] not in LOOPBACK:
        print(f"WARNING: listening on {opts['host']}, not loopback. Traffic is plain HTTP: the token and all data are "
              "unencrypted. Reach this node through an SSH tunnel or a VPN, not an open network.")
    print("Ctrl+C to stop.", flush=True)
    try:
        while server._thread is not None and server._thread.is_alive():
            time.sleep(0.5)
    except KeyboardInterrupt:
        pass
    finally:
        server.shutdown()
    return 0


def cli(argv: List[str], agent_dir: Any = None) -> int:
    """`omni node ...`. `argv` starts at the subcommand (a leading "node" is tolerated)."""
    args = list(argv or [])
    if args[:1] == ["node"]:
        args = args[1:]
    sub = args[0] if args else "list"
    if sub in ("-h", "--help", "help"):
        print(USAGE)
        return 0
    if sub == "serve":
        return _serve_cli(args[1:])
    if sub not in ("list", "add", "remove"):
        print(f"unknown node command: {sub}\n{USAGE}")
        return 2
    if agent_dir is None:
        from . import resources as res
        agent_dir = res.agent_dir()
    nodes = load_nodes(agent_dir)
    if sub == "list":
        if not nodes:
            print("No nodes configured. Add one: omni node add NAME URL TOKEN")
            return 0
        for name in sorted(nodes):
            token = str(nodes[name].get("token") or "")
            print(f"{name:<16} {nodes[name].get('url', '')}  token: {token if token.startswith('$') else ('set' if token else 'MISSING')}")
        return 0
    if sub == "add":
        if len(args) != 4:
            print(USAGE)
            return 2
        name, url, token = args[1], args[2].rstrip("/"), args[3]
        if not url.startswith(("http://", "https://")):
            print(f"URL must start with http:// or https://, not {url!r}")
            return 2
        replaced = name in nodes
        nodes[name] = {"url": url, "token": token}
        save_nodes(agent_dir, nodes)
        print(f"{'Updated' if replaced else 'Added'} node '{name}' ({url}) in {nodes_path(agent_dir)}")
        return 0
    if len(args) != 2:
        print(USAGE)
        return 2
    if args[1] not in nodes:
        print(f"no node '{args[1]}'. " + (f"Configured: {', '.join(sorted(nodes))}" if nodes else "None are configured."))
        return 1
    del nodes[args[1]]
    save_nodes(agent_dir, nodes)
    print(f"Removed node '{args[1]}'")
    return 0


if __name__ == "__main__":
    sys.exit(cli(sys.argv[1:]))
