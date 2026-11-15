"""The Omni node server: lets an agent on another machine run commands and read/write files here.

Start it on the machine you want to reach, in the folder the agent may work in:

    omni node serve --root ~/project            # prints the URL and a freshly generated token
    omni node serve --root /srv/logs --read-only   # files can be read and listed; no writes, no commands

Then, on the machine where the agent runs, register it and turn on the `nodes` capability:

    omni node add buildbox http://127.0.0.1:8765 <token>
    omni node list
    omni node remove buildbox

Everything is confined to `--root`: a path that resolves outside it is refused. Every request must carry a token.

Encryption (TLS). Without a certificate the traffic is plain HTTP: the token and everything sent travel
unencrypted, which is only acceptable on 127.0.0.1 or inside an SSH tunnel (`ssh -L 8765:127.0.0.1:8765 host`), a
VPN or a tailnet. To serve HTTPS, give the server a certificate and its key:

    omni node serve --root ~/project --host 0.0.0.0 --cert cert.pem --key key.pem

Omni cannot create a certificate itself. `omni node serve --self-signed` (with `--cert`/`--key`, or the default
~/.omni-node/cert.pem and key.pem) uses the pair when the files exist and otherwise prints the `openssl` command
that creates a self-signed one, then stops. Keep the key file outside `--root`. At start the server prints the
certificate's SHA-256 fingerprint. Nobody vouches for a self-signed certificate, so the client pins it instead:

    omni node add buildbox https://buildbox:8765 <token> --fingerprint sha256:AB:CD:...
    omni node add buildbox https://buildbox:8765 <token> --ca cert.pem

With `--fingerprint` the client accepts exactly the certificate with that fingerprint and refuses any other; with
`--ca` it verifies the certificate (and the host name in it) against that file. An https URL with neither is
verified the normal way, against the system's certificate authorities - right for a certificate bought or issued
by Let's Encrypt, a failure for a self-signed one.

Pairing, instead of copying the long token:

    omni node serve --root ~/project --pair         # also prints a short code, e.g. K7QF3MXA
    omni node pair buildbox https://buildbox:8765 K7QF3MXA      (on the machine where the agent runs)

The code is good for 10 minutes and for one pairing; after 5 wrong codes pairing is switched off until the server
is restarted. Pairing hands that client a token of its own, which the server accepts next to the main one and
remembers in `<root>/.omni-node/clients.json` (only a hash of it is stored), so it still works after a restart.
`omni node serve --root ~/project --revoke CLIENT` takes it away again, also from a running server; CLIENT is the
pairing machine's host name unless `omni node pair ... --as CLIENT` chose another. Over https `omni node pair`
pins the certificate it was shown and prints its fingerprint: compare that with the one the server printed (or
pass `--fingerprint`/`--ca` to have it checked for you). Over plain http the code and the new token are as
exposed as everything else, so pair through the tunnel.

Protocol (all POST, JSON in and out, `Authorization: Bearer <token>`): /v1/info, /v1/exec, /v1/read, /v1/write,
/v1/list, and /v1/pair `{code, name}` -> `{token, name, fingerprint?}`, the only one that needs no token. Errors
are `{"error": "..."}` with status 400 (bad request / path escape), 401 (token, or wrong pairing code), 403
(read-only, or pairing not available), 404 (no such file or endpoint), 413 (body over 5 MB).
"""
from __future__ import annotations

import hashlib
import hmac
import http.client
import http.server
import json
import os
import platform
import re
import secrets
import socket
import ssl
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

NODES_FILE = "nodes.json"
STATE_DIR = ".omni-node"           # under the node's root
CLIENTS_FILE = "clients.json"
PAIR_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"   # no 0/O, 1/I/L
PAIR_CODE_LENGTH = 8
PAIR_TTL = 600                     # seconds
PAIR_MAX_FAILURES = 5
HANDSHAKE_TIMEOUT = 10             # seconds a client gets to finish the TLS handshake
PAIR_HTTP_TIMEOUT = 20
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


# ------------------------------------------------------------------------------------------------ TLS
class PinError(Exception):
    """The node presented a certificate other than the pinned one."""


def normalize_fingerprint(value: Any) -> str:
    """`sha256:AB:CD:...` (prefix, colons, spaces and case optional) as 64 lowercase hex digits; ValueError otherwise."""
    text = str(value or "").strip().lower()
    if text.startswith("sha256:"):
        text = text[7:]
    text = re.sub(r"[:\s]", "", text)
    if not re.fullmatch(r"[0-9a-f]{64}", text):
        raise ValueError("a fingerprint is sha256: followed by 32 colon-separated hex pairs, as `omni node serve` prints it")
    return text


def format_fingerprint(der: bytes) -> str:
    digest = hashlib.sha256(der).hexdigest().upper()
    return "sha256:" + ":".join(digest[i:i + 2] for i in range(0, len(digest), 2))


def cert_fingerprint(certfile: Any) -> str:
    """The SHA-256 fingerprint of the (first) certificate in a PEM file."""
    text = Path(certfile).read_text(encoding="ascii", errors="replace")
    m = re.search(r"-----BEGIN CERTIFICATE-----.*?-----END CERTIFICATE-----", text, re.S)
    if not m:
        raise ValueError(f"no certificate found in {certfile}")
    return format_fingerprint(ssl.PEM_cert_to_DER_cert(m.group(0)))


def _no_password(*_a: Any) -> str:
    raise ValueError("the key file is password-protected; the node server needs an unencrypted key")


def _tls_context(certfile: str, keyfile: str) -> Tuple[ssl.SSLContext, str]:
    try:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(certfile, keyfile, password=_no_password)
        return ctx, cert_fingerprint(certfile)
    except (OSError, ValueError) as e:  # ssl.SSLError is an OSError
        raise ValueError(f"could not load the certificate {certfile} with the key {keyfile}: {e}") from None


def openssl_command(certfile: str, keyfile: str, host: str) -> str:
    """The command that creates a self-signed certificate valid for `host` (this module cannot create one)."""
    names = ["DNS:localhost", "IP:127.0.0.1"]
    cn = "localhost"
    if host in ("0.0.0.0", "", "::"):
        cn = socket.gethostname() or "localhost"
        names.insert(0, f"DNS:{cn}")
    elif host not in LOOPBACK:
        cn = host
        names.insert(0, ("IP:" if re.fullmatch(r"[0-9.]+|[0-9A-Fa-f:]*:[0-9A-Fa-f:.]*", host) else "DNS:") + host)
    return (f'openssl req -x509 -newkey rsa:2048 -sha256 -days 825 -nodes -keyout "{keyfile}" -out "{certfile}" '
            f'-subj "/CN={cn}" -addext "subjectAltName={",".join(names)}"')


def post_json(url: str, payload: Dict[str, Any], token: Optional[str] = None, timeout: float = 20,
              fingerprint: Optional[str] = None, ca: Optional[str] = None, verify: bool = True,
              seen: Optional[Dict[str, Any]] = None) -> Any:
    """POST JSON to a node and return the decoded answer. For an https URL: `ca` verifies against that file,
    `fingerprint` (or `verify=False`) turns verification off, and a `fingerprint` must then match the certificate
    the node presents (PinError otherwise). `seen["fingerprint"]` receives the presented certificate's fingerprint.
    urllib's errors, and ValueError for an answer that is not JSON, pass through."""
    headers = {"Content-Type": "application/json"}
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"), method="POST", headers=headers)
    if not url.lower().startswith("https://"):
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    want = normalize_fingerprint(fingerprint) if fingerprint else None
    if ca:
        ctx = ssl.create_default_context(cafile=ca)
    else:
        ctx = ssl.create_default_context()
        if want or not verify:
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE

    class Connection(http.client.HTTPSConnection):
        def connect(self) -> None:
            super().connect()
            der = self.sock.getpeercert(binary_form=True) or b""
            got = format_fingerprint(der)
            if seen is not None:
                seen["fingerprint"] = got
            if want and not hmac.compare_digest(normalize_fingerprint(got), want):
                self.close()
                raise PinError(f"the certificate does not match the pinned fingerprint: expected "
                               f"{_pretty(want)}, the server "
                               f"presented {got}. Refusing to talk to it. If the node's certificate was replaced on "
                               "purpose, register the node again with the new fingerprint.")

    class Handler(urllib.request.HTTPSHandler):
        def https_open(self, r: Any) -> Any:
            return self.do_open(Connection, r, context=ctx)

    with urllib.request.build_opener(Handler(context=ctx)).open(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _pretty(hexdigest: str) -> str:
    h = hexdigest.upper()
    return "sha256:" + ":".join(h[i:i + 2] for i in range(0, len(h), 2))


# ------------------------------------------------------------------------------------------------ paired clients
def _clock() -> float:
    return time.monotonic()


def new_pair_code() -> str:
    return "".join(secrets.choice(PAIR_ALPHABET) for _ in range(PAIR_CODE_LENGTH))


def clients_path(root: Any) -> Path:
    return Path(root) / STATE_DIR / CLIENTS_FILE


def load_clients(root: Any) -> Dict[str, Dict[str, Any]]:
    """The paired clients of the node rooted at `root`: name -> {"sha256": hash of its token, "paired_at": ...}."""
    try:
        data = json.loads(clients_path(root).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    clients = data.get("clients") if isinstance(data, dict) else None
    return {str(k): dict(v) for k, v in clients.items() if isinstance(v, dict)} if isinstance(clients, dict) else {}


def save_clients(root: Any, clients: Dict[str, Dict[str, Any]]) -> None:
    p = clients_path(root)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".tmp")
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)  # never readable by others, even briefly
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(json.dumps({"clients": clients}, indent=2, sort_keys=True))
    os.replace(tmp, p)
    try:
        os.chmod(p, 0o600)
    except OSError:
        pass


def revoke_client(root: Any, name: str) -> bool:
    """Forget a paired client. A running server notices the changed file on its next request."""
    clients = load_clients(root)
    if name not in clients:
        return False
    del clients[name]
    save_clients(root, clients)
    return True


def _file_sig(p: Path) -> Optional[Tuple[int, int]]:
    try:
        st = os.stat(p)
        return (st.st_mtime_ns, st.st_size)
    except OSError:
        return None


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8", "replace")).hexdigest()


def default_client_name() -> str:
    return re.sub(r"[^A-Za-z0-9._-]", "-", socket.gethostname() or "")[:64] or "client"


# ------------------------------------------------------------------------------------------------ HTTP
class _Handler(http.server.BaseHTTPRequestHandler):
    server_version = "OmniNode/1"
    protocol_version = "HTTP/1.1"

    def setup(self) -> None:
        # the TLS handshake happens here, on this connection's own thread, so a slow or silent client cannot
        # hold up the accept loop
        if isinstance(self.request, ssl.SSLSocket):
            self.request.settimeout(HANDSHAKE_TIMEOUT)
            self.request.do_handshake()
            self.request.settimeout(None)
        super().setup()

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
        path = self.path.split("?", 1)[0].rstrip("/")
        # pairing is the one endpoint without a token: the one-time code is its credential
        authorized = path == "/v1/pair" or srv.authorized(self.headers.get("Authorization") or "")
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
        ops = {"/v1/info": srv.info, "/v1/exec": srv.node.exec, "/v1/read": srv.node.read,
               "/v1/write": srv.node.write, "/v1/list": srv.node.list, "/v1/pair": srv.pair}
        op = ops.get(path)
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

    def __init__(self, addr: Tuple[str, int], node: Node, token: str, certfile: Optional[str] = None,
                 keyfile: Optional[str] = None, pair: bool = False):
        self._ssl: Optional[ssl.SSLContext] = None
        self.fingerprint: Optional[str] = None
        if certfile or keyfile:
            if not (certfile and keyfile):
                raise ValueError("TLS needs both a certificate file and a key file")
            self._ssl, self.fingerprint = _tls_context(str(certfile), str(keyfile))  # before the port is taken
        super().__init__(addr, _Handler)
        self.node = node
        self.token = token
        self.root = str(node.root)
        self.read_only = not node.allow_write
        self.tls = self._ssl is not None
        host, port = self.server_address[0], self.server_address[1]
        shown = socket.gethostname() if host in ("0.0.0.0", "") else host
        self.url = f"{'https' if self.tls else 'http'}://{shown}:{port}"
        self._thread: Optional[threading.Thread] = None
        self._stopped = False
        self._lock = threading.Lock()
        self._clients_file = clients_path(node.root)
        self._clients: Dict[str, Dict[str, Any]] = {}
        self._clients_sig: Any = ()
        self._refresh_clients()
        # pairing: "off" (not asked for), "open", "used" (the code did its one pairing), "locked" (too many wrong codes)
        self.pair_code: Optional[str] = new_pair_code() if pair else None
        self._pair_state = "open" if pair else "off"
        self._pair_deadline = _clock() + PAIR_TTL
        self._pair_failures = 0

    # -- TLS: accept plain, wrap without handshaking; the handler thread does the handshake
    def get_request(self) -> Tuple[Any, Any]:
        sock, addr = super().get_request()
        if self._ssl is not None:
            try:
                sock = self._ssl.wrap_socket(sock, server_side=True, do_handshake_on_connect=False)
            except OSError:
                sock.close()
                raise
        return sock, addr

    def handle_error(self, request: Any, client_address: Any) -> None:
        if isinstance(sys.exc_info()[1], OSError):  # a failed handshake or a client that went away: not our problem
            return
        super().handle_error(request, client_address)

    # -- tokens
    def _refresh_clients(self) -> None:
        sig = _file_sig(self._clients_file)
        if sig != self._clients_sig:  # first load, or changed behind our back (`omni node serve --revoke`)
            self._clients = load_clients(self.node.root)
            self._clients_sig = sig

    @property
    def paired_clients(self) -> List[str]:
        with self._lock:
            self._refresh_clients()
            return sorted(self._clients)

    def authorized(self, header: str) -> bool:
        if hmac.compare_digest(header.encode("utf-8", "replace"), f"Bearer {self.token}".encode("utf-8")):
            return True
        if not header.startswith("Bearer "):
            return False
        digest = _token_hash(header[7:]).encode("ascii")
        with self._lock:
            self._refresh_clients()
            known = [str(c.get("sha256") or "").encode("utf-8", "replace") for c in self._clients.values()]
        ok = False
        for h in known:
            ok = hmac.compare_digest(digest, h) or ok
        return ok

    def info(self, p: Dict[str, Any]) -> Dict[str, Any]:
        return {**self.node.info(p), "tls": self.tls, "paired_clients": len(self.paired_clients)}

    def pair(self, p: Dict[str, Any]) -> Dict[str, Any]:
        """/v1/pair: trade the one-time code for a token of the client's own."""
        again = "restart `omni node serve --pair` for a new code"
        with self._lock:
            if self._pair_state == "off":
                raise NodeError(403, "pairing is not enabled on this node (start it with `omni node serve --pair`)")
            if self._pair_state == "locked":
                raise NodeError(403, f"pairing is disabled after {PAIR_MAX_FAILURES} wrong codes; {again}")
            if self._pair_state == "used":
                raise NodeError(403, f"the pairing code was already used; {again}")
            if _clock() >= self._pair_deadline:
                raise NodeError(403, f"the pairing code has expired (it is valid {PAIR_TTL // 60} minutes); {again}")
            code, name = p.get("code"), p.get("name")
            if not isinstance(code, str) or not isinstance(name, str):
                raise NodeError(400, "code and name are required")
            if not re.fullmatch(r"[A-Za-z0-9._-]{1,64}", name):
                raise NodeError(400, "name must be 1-64 letters, digits, dots, dashes or underscores")
            given = re.sub(r"[\s-]", "", code).upper()
            if not hmac.compare_digest(given.encode("utf-8"), (self.pair_code or "").encode("ascii")):
                self._pair_failures += 1
                left = PAIR_MAX_FAILURES - self._pair_failures
                if left <= 0:
                    self._pair_state = "locked"
                    raise NodeError(401, f"wrong pairing code; that was wrong attempt {PAIR_MAX_FAILURES}, so pairing is now disabled; {again}")
                raise NodeError(401, f"wrong pairing code ({left} attempt{'s' if left != 1 else ''} left)")
            token = secrets.token_urlsafe(24)
            self._refresh_clients()
            clients = dict(self._clients)
            clients[name] = {"sha256": _token_hash(token), "paired_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
            try:
                save_clients(self.node.root, clients)
            except OSError as e:
                raise NodeError(500, f"could not save the paired client: {e}") from None
            self._clients, self._clients_sig = clients, _file_sig(self._clients_file)
            self._pair_state = "used"
            result: Dict[str, Any] = {"token": token, "name": name}
            if self.fingerprint:
                result["fingerprint"] = self.fingerprint
            return result

    def revoke(self, name: str) -> bool:
        """Forget a paired client; its token stops working at once."""
        with self._lock:
            done = revoke_client(self.node.root, name)
            self._clients_sig = ()
            self._refresh_clients()
            return done

    def shutdown(self) -> None:
        """Stop serving, close the socket and wait for the serving thread. Safe to call twice."""
        if self._stopped:
            return
        self._stopped = True
        if self._thread is not None:
            super().shutdown()
            self._thread.join(timeout=5)
        self.server_close()


def serve(root: str, host: str = "127.0.0.1", port: int = 8765, token: Optional[str] = None, allow_write: bool = True,
          certfile: Optional[str] = None, keyfile: Optional[str] = None, pair: bool = False) -> NodeServer:
    """Start a node server on a background thread and return it (`.url`, `.token`, `.shutdown()`).
    `port=0` picks a free port. Without a token a random one is generated. `certfile` + `keyfile` serve HTTPS
    (`.tls`, `.fingerprint`); `pair=True` opens pairing with a one-time code (`.pair_code`)."""
    server = NodeServer((host, int(port)), Node(root, allow_write), token or secrets.token_urlsafe(24),
                        certfile=certfile, keyfile=keyfile, pair=pair)
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
                       [--cert FILE --key FILE | --self-signed] [--pair]
       omni node serve --root DIR --revoke CLIENT
       omni node list
       omni node add NAME URL TOKEN [--fingerprint sha256:..] [--ca FILE]   (TOKEN may be $ENV_VAR)
       omni node pair NAME URL CODE [--fingerprint sha256:..] [--ca FILE]
       omni node remove NAME"""


def _options(args: List[str], valued: Tuple[str, ...], flags: Tuple[str, ...]) -> Tuple[Dict[str, Any], List[str]]:
    """-> ({option: value or True}, positional). Raises ValueError with a message for the user."""
    opts: Dict[str, Any] = {}
    rest: List[str] = []
    i = 0
    while i < len(args):
        a = args[i]
        key, eq, value = a[2:].partition("=") if a.startswith("--") else ("", "", "")
        if a.startswith("--") and key in flags and not eq:
            opts[key] = True
        elif a.startswith("--") and key in valued:
            if not eq:
                i += 1
                if i >= len(args):
                    raise ValueError(f"{a} needs a value")
                value = args[i]
            opts[key] = value
        elif a.startswith("--"):
            raise ValueError(f"unknown option: {a}")
        else:
            rest.append(a)
        i += 1
    return opts, rest


def _serve_cli(args: List[str]) -> int:
    try:
        opts, rest = _options(args, ("root", "host", "port", "token", "cert", "key", "revoke"), ("read-only", "self-signed", "pair"))
        if rest:
            raise ValueError(f"unexpected argument: {rest[0]}")
        port = int(opts.get("port", 8765))
    except ValueError as e:
        print(f"{e}\n{USAGE}")
        return 2
    root, host = str(opts.get("root", ".")), str(opts.get("host", "127.0.0.1"))
    if opts.get("revoke"):
        if revoke_client(root, str(opts["revoke"])):
            print(f"Revoked client '{opts['revoke']}'; its token stops working at once, also on a running node.")
            return 0
        print(f"no paired client '{opts['revoke']}' under {root}")
        return 1
    cert, key = opts.get("cert"), opts.get("key")
    if opts.get("self-signed"):
        cert = cert or str(Path(root) / ".omni-node" / "cert.pem")
        key = key or str(Path(root) / ".omni-node" / "key.pem")
        if not (Path(cert).is_file() and Path(key).is_file()):
            Path(cert).parent.mkdir(parents=True, exist_ok=True)
            print("Omni cannot create a certificate itself. Create a self-signed pair with:\n\n  "
                  + openssl_command(cert, key, host) + "\n\nthen run this command again.")
            return 2
    if bool(cert) != bool(key):
        print(f"--cert and --key go together\n{USAGE}")
        return 2
    try:
        server = serve(root, host, port, opts.get("token"), allow_write=not opts.get("read-only"),
                       certfile=cert, keyfile=key, pair=bool(opts.get("pair")))
    except (OSError, ValueError, OverflowError, ssl.SSLError) as e:
        print(f"could not start the node server: {e}")
        return 1
    print(f"Omni node serving {server.root}" + (" (read-only)" if opts.get("read-only") else ""))
    print(f"  url          {server.url}")
    print(f"  token        {server.token}")
    if server.fingerprint:
        print(f"  fingerprint  {server.fingerprint}")
    pin = f" --fingerprint {server.fingerprint}" if server.fingerprint else ""
    print(f"Register it where the agent runs:  omni node add NAME {server.url} {server.token}{pin}")
    if server.pair_code:
        print(f"Or pair without copying the token (code valid {PAIR_TTL // 60} minutes, once):  "
              f"omni node pair NAME {server.url} {server.pair_code}{pin}")
    if host not in LOOPBACK and not server.tls:
        print(f"WARNING: listening on {host} over plain HTTP: the token and all data travel unencrypted. Add "
              "--cert/--key (or --self-signed), or reach this node through an SSH tunnel or a VPN.")
    print("Ctrl+C to stop.", flush=True)
    try:
        while server._thread is not None and server._thread.is_alive():
            time.sleep(0.5)
    except KeyboardInterrupt:
        pass
    finally:
        server.shutdown()
    return 0


def _pair_cli(agent_dir: Any, name: str, url: str, code: str, fingerprint: Optional[str], ca: Optional[str]) -> int:
    seen: Dict[str, Any] = {}
    try:
        answer = post_json(url.rstrip("/") + "/v1/pair", {"code": code, "name": default_client_name()}, timeout=20,
                           fingerprint=fingerprint, ca=ca, verify=not url.lower().startswith("https://") or bool(ca), seen=seen)
    except PinError as e:
        print(str(e))
        return 1
    except urllib.error.HTTPError as e:
        try:
            why = json.loads(e.read().decode("utf-8")).get("error") or e.reason
        except Exception:
            why = e.reason
        print(f"pairing refused: {why}")
        return 1
    except (urllib.error.URLError, OSError, ValueError) as e:
        print(f"could not reach {url}: {getattr(e, 'reason', e)}")
        return 1
    token = answer.get("token") if isinstance(answer, dict) else None
    if not token:
        print("the node did not answer like an Omni node")
        return 1
    entry: Dict[str, Any] = {"url": url.rstrip("/"), "token": token}
    got = seen.get("fingerprint")
    if ca:
        entry["ca"] = ca
    elif url.lower().startswith("https://") and got:
        entry["fingerprint"] = got          # pin what we were shown (checked against --fingerprint when given)
    nodes = load_nodes(agent_dir)
    nodes[name] = entry
    save_nodes(agent_dir, nodes)
    print(f"Paired with {url} as '{name}'. The token was saved in {nodes_path(agent_dir)}.")
    if entry.get("fingerprint") and not fingerprint:
        print(f"The node's certificate is pinned: {got}\nCompare it with the fingerprint `omni node serve` printed; if they "
              f"differ, run `omni node remove {name}` at once.")
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
    if sub not in ("list", "add", "remove", "pair"):
        print(f"unknown node command: {sub}\n{USAGE}")
        return 2
    try:
        opts, rest = _options(args[1:], ("fingerprint", "ca"), ())
        if opts.get("fingerprint"):
            opts["fingerprint"] = normalize_fingerprint(opts["fingerprint"])
    except ValueError as e:
        print(f"{e}\n{USAGE}")
        return 2
    if opts and sub not in ("add", "pair"):
        print(USAGE)
        return 2
    if opts.get("ca") and not Path(str(opts["ca"])).is_file():
        print(f"--ca: no such file: {opts['ca']}")
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
            n = nodes[name]
            token = str(n.get("token") or "")
            trust = " pinned" if n.get("fingerprint") else " ca" if n.get("ca") else ""
            print(f"{name:<16} {n.get('url', '')}  token: {token if token.startswith('$') else ('set' if token else 'MISSING')}{trust}")
        return 0
    if sub in ("add", "pair"):
        if len(rest) != 3:
            print(USAGE)
            return 2
        name, url = rest[0], rest[1].rstrip("/")
        if not url.startswith(("http://", "https://")):
            print(f"URL must start with http:// or https://, not {url!r}")
            return 2
        if (opts.get("fingerprint") or opts.get("ca")) and not url.startswith("https://"):
            print("--fingerprint and --ca only make sense for an https:// URL")
            return 2
        if sub == "pair":
            return _pair_cli(agent_dir, name, url, rest[2], opts.get("fingerprint"), opts.get("ca"))
        replaced = name in nodes
        nodes[name] = {"url": url, "token": rest[2], **{k: str(v) for k, v in opts.items()}}
        save_nodes(agent_dir, nodes)
        print(f"{'Updated' if replaced else 'Added'} node '{name}' ({url}) in {nodes_path(agent_dir)}")
        return 0
    if len(rest) != 1:
        print(USAGE)
        return 2
    if rest[0] not in nodes:
        print(f"no node '{rest[0]}'. " + (f"Configured: {', '.join(sorted(nodes))}" if nodes else "None are configured."))
        return 1
    del nodes[rest[0]]
    save_nodes(agent_dir, nodes)
    print(f"Removed node '{rest[0]}'")
    return 0


if __name__ == "__main__":
    sys.exit(cli(sys.argv[1:]))
