"""`lsp`: ask a real language server instead of guessing with grep (Oh My Pi's lsp tool).

Text search finds strings; a language server knows the program. The `lsp` tool starts the server for a file's
language (once per session, on first use) and asks it:

    definition(path, line, column? | symbol?)     where the thing under the cursor is defined
    references(path, line, column? | symbol?)     every use of it
    hover(path, line, column? | symbol?)          its type and documentation
    symbols(path)                                 the outline of one file
    workspace_symbols(query, path?)               symbols by name across the project
    diagnostics(path)                             errors and warnings for a file, re-read from disk
    rename(path, line, symbol, new_name, apply?)  the edits a rename would make - a preview unless `apply` is true
    code_actions(path, line, column? | symbol?)   the quick fixes and refactors the server offers there, numbered
    apply_action(path, line, index)               apply one of them (same line/column/symbol as when it was listed)
    format(path)                                  let the server format the file
    status                                        which servers run, which are installed, what a dead one last said

Lines and columns are 1-based, as editors show them; a column counts characters. Instead of counting columns, give
`symbol`: the name as it appears on that line. Answers come back as `relative/path:line:col  source line`, at most
60 rows.

Columns on the wire: the client offers `general.positionEncodings: ["utf-32", "utf-16"]` and uses the server's
`positionEncoding` answer (UTF-16 when it gives none, as the protocol says). Unless that is utf-32, every column is
converted against the actual line text, both ways, so a line with emoji or other astral characters still points at
the right place.

Diagnostics: a server that advertises `diagnosticProvider` is asked (`textDocument/diagnostic`; an "unchanged"
report reuses what is cached); any other server is waited on for `publishDiagnostics`, as before.

Writing: `apply_action`, `rename` with `apply: true` and `format` change files on disk. Only text edits
(`changes` / `documentChanges`) are applied, per file from the bottom up. Nothing is written when the edit touches
a file outside the session's directory, creates/renames/deletes files, overlaps itself, or targets a file that
changed since the server read it; an action that only carries a `command` is refused (commands are never run).
Every file goes through the session's `edit` gate first, so plan mode and approvals can refuse; a refusal for one
file means no file is written.

Servers are found on PATH by file extension: Python `pyright-langserver` (then `pylsp`), TypeScript/JavaScript
`typescript-language-server`, Go `gopls`, Rust `rust-analyzer`, C/C++ `clangd`. A profile can name its own:

    "lsp": {"py": ["pylsp"], "lua": ["lua-language-server"]}

Servers belong to the session and are shut down when it ends. `/lsp` shows what is running and what is installed.
The last 20 lines of each server's stderr are kept: they are shown by `status` / `/lsp` for a server that died,
and the last few are added to the error of the call it died under.
"""
from __future__ import annotations

import collections
import json
import os
import re
import shutil
import subprocess
import threading
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from ..tools.base import ToolDef, ToolError, resolve_to_cwd, text_result
from . import Capability

MAX_ROWS, REQUEST_TIMEOUT, INIT_TIMEOUT, DIAGNOSTICS_WAIT = 60, 20.0, 30.0, 4.0
STDERR_LINES = 20                        # how much of a server's stderr is remembered
ENCODINGS = ["utf-32", "utf-16"]         # offered to the server, preferred first; utf-16 is the protocol's default
_EOL = re.compile(r"\r\n|\r|\n")         # what LSP counts as a line break (str.splitlines knows many more)

# language key -> candidate commands, first one found on PATH wins
SERVERS: Dict[str, List[List[str]]] = {
    "py": [["pyright-langserver", "--stdio"], ["pylsp"]],
    "ts": [["typescript-language-server", "--stdio"]],
    "go": [["gopls"]],
    "rs": [["rust-analyzer"]],
    "c": [["clangd"]],
}
# extension -> (language key, LSP languageId)
EXTENSIONS: Dict[str, Tuple[str, str]] = {
    "py": ("py", "python"), "pyi": ("py", "python"),
    "ts": ("ts", "typescript"), "tsx": ("ts", "typescriptreact"), "mts": ("ts", "typescript"), "cts": ("ts", "typescript"),
    "js": ("ts", "javascript"), "jsx": ("ts", "javascriptreact"), "mjs": ("ts", "javascript"), "cjs": ("ts", "javascript"),
    "go": ("go", "go"), "rs": ("rs", "rust"),
    "c": ("c", "c"), "h": ("c", "c"), "cpp": ("c", "cpp"), "cc": ("c", "cpp"), "cxx": ("c", "cpp"), "hpp": ("c", "cpp"),
}
INSTALL = {
    "py": "pip install pyright (or: pip install python-lsp-server)",
    "ts": "npm install -g typescript-language-server typescript",
    "go": "go install golang.org/x/tools/gopls@latest",
    "rs": "rustup component add rust-analyzer",
    "c": "install clangd (part of LLVM)",
}
SYMBOL_KINDS = ["", "file", "module", "namespace", "package", "class", "method", "property", "field", "constructor", "enum",
                "interface", "function", "variable", "constant", "string", "number", "boolean", "array", "object", "key",
                "null", "enum member", "struct", "event", "operator", "type parameter"]
SEVERITY = {1: "error", 2: "warning", 3: "info", 4: "hint"}
_start_lock = threading.Lock()


def path_to_uri(path: str) -> str:
    return Path(path).resolve().as_uri()


def uri_to_path(uri: str) -> str:
    parsed = urllib.parse.urlparse(uri)
    if parsed.scheme != "file":
        return uri
    raw = urllib.request.url2pathname(urllib.parse.unquote(parsed.path))
    if parsed.netloc and parsed.netloc != "localhost":
        raw = f"\\\\{parsed.netloc}{raw}" if os.name == "nt" else raw
    return os.path.normpath(raw)


def _same_file(a: str, b: str) -> bool:
    return os.path.normcase(os.path.realpath(a)) == os.path.normcase(os.path.realpath(b))


def _rows(text: str) -> List[str]:
    """The lines of a text as a language server counts them."""
    rows = _EOL.split(text)
    if rows and rows[-1] == "":
        rows.pop()
    return rows


def _width(ch: str, enc: str) -> int:
    return len(ch.encode("utf-8", "surrogatepass")) if enc == "utf-8" else 2 if ord(ch) > 0xFFFF else 1


def to_units(line: str, col: int, enc: str) -> int:
    """A 0-based character column on `line` -> the offset the server counts in (`enc` code units)."""
    if enc == "utf-32" or col <= 0 or line.isascii():
        return max(col, 0)
    return sum(_width(ch, enc) for ch in line[:col]) + max(0, col - len(line))


def from_units(line: str, units: int, enc: str) -> int:
    """The server's offset on `line` -> a 0-based character column. Past the end of the line it keeps counting."""
    if enc == "utf-32" or units <= 0 or line.isascii():
        return max(units, 0)
    seen = 0
    for i, ch in enumerate(line):
        if seen >= units:
            return i
        seen += _width(ch, enc)
    return len(line) + max(0, units - seen)


def apply_text_edits(text: str, edits: List[Dict[str, Any]], enc: str = "utf-16") -> str:
    """`text` with LSP TextEdits applied, last one first so earlier offsets stay valid."""
    starts = [0] + [m.end() for m in _EOL.finditer(text)]
    crlf = "\r\n" in text

    def offset(pos: Dict[str, Any]) -> int:
        line, ch = int(pos.get("line") or 0), int(pos.get("character") or 0)
        if line >= len(starts):
            return len(text)
        begin = starts[max(line, 0)]
        end = starts[line + 1] if line + 1 < len(starts) else len(text)
        body = text[begin:end].rstrip("\r\n")
        return begin + min(len(body), from_units(body, ch, enc))

    spans = []
    for i, e in enumerate(edits):
        rng = e.get("range") or {}
        a, b = offset(rng.get("start") or {}), offset(rng.get("end") or {})
        new = str(e.get("newText") or "")
        spans.append((min(a, b), max(a, b), i, _EOL.sub("\r\n", new) if crlf else new))
    spans.sort()                                             # equal positions keep the order the server gave
    for prev, cur in zip(spans, spans[1:]):
        if cur[0] < prev[1]:
            raise ToolError("the server's edits overlap each other; nothing was written")
    for a, b, _i, new in reversed(spans):
        text = text[:a] + new + text[b:]
    return text


def resolve_command(cmd: List[str]) -> Optional[List[str]]:
    """The command as it can be started here, or None when its program is not installed."""
    if not cmd:
        return None
    exe = shutil.which(cmd[0]) or (cmd[0] if os.path.isfile(cmd[0]) else None)
    if exe is None:
        return None
    if os.name == "nt" and exe.lower().endswith((".cmd", ".bat")):  # npm shims are batch files
        return [os.environ.get("COMSPEC") or "cmd.exe", "/c", exe, *cmd[1:]]
    return [exe, *cmd[1:]]


def _overrides(session: Any) -> Dict[str, List[str]]:
    raw = session.profile.get("lsp") if isinstance(getattr(session, "profile", None), dict) else None
    out: Dict[str, List[str]] = {}
    for k, v in (raw or {}).items() if isinstance(raw, dict) else []:
        if isinstance(v, str):
            v = v.split()
        if isinstance(v, list) and v:
            out[str(k).lower().lstrip(".")] = [str(x) for x in v]
    return out


def language_of(session: Any, path: str) -> Tuple[str, str]:
    """(language key, languageId) for a file, or a ToolError saying which languages are covered."""
    ext = os.path.splitext(path)[1].lower().lstrip(".")
    if ext in EXTENSIONS:
        return EXTENSIONS[ext]
    if ext and ext in _overrides(session):
        return ext, ext
    raise ToolError(f"no language server is configured for '.{ext}' files. Built in: {', '.join(sorted(EXTENSIONS))}. "
                    f"Add one to the profile: \"lsp\": {{\"{ext or 'ext'}\": [\"server-command\", \"--stdio\"]}}")


def command_for(session: Any, lang: str, ext: str = "") -> List[str]:
    over = _overrides(session)
    custom = over.get(ext) or over.get(lang)
    if custom:
        found = resolve_command(custom)
        if found is None:
            raise ToolError(f"the language server '{custom[0]}' (the profile's lsp setting for '{ext if ext in over else lang}') "
                            "was not found on PATH. Install it or correct the profile.")
        return found
    candidates = SERVERS.get(lang) or []
    for cmd in candidates:
        found = resolve_command(cmd)
        if found is not None:
            return found
    names = " or ".join(c[0] for c in candidates) or "a language server"
    raise ToolError(f"no language server for '{lang}' files was found on PATH. Install {names}"
                    + (f": {INSTALL[lang]}" if lang in INSTALL else "") + ". Until then use grep/read.")


class Server:
    """One language server process speaking JSON-RPC over stdio with Content-Length framing."""

    def __init__(self, lang: str, command: List[str], cwd: str):
        self.lang, self.command, self.cwd = lang, command, cwd
        self.started = time.time()
        self.docs: Dict[str, Dict[str, Any]] = {}          # uri -> {"version", "text"}
        self.diagnostics: Dict[str, List[Dict[str, Any]]] = {}
        self.published: Dict[str, int] = {}                 # uri -> how many publishDiagnostics arrived
        self.result_ids: Dict[str, str] = {}                # uri -> resultId of the last pulled diagnostic report
        self.capabilities: Dict[str, Any] = {}
        self.encoding = "utf-16"                            # what the server counts columns in
        self.stderr: "collections.deque[str]" = collections.deque(maxlen=STDERR_LINES)
        self.changed = threading.Condition()
        self._pending: Dict[int, Dict[str, Any]] = {}
        self._next = 0
        self._write = threading.Lock()
        self.dead: Optional[str] = None
        kwargs: Dict[str, Any] = {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)} if os.name == "nt" else {}
        try:
            self.popen = subprocess.Popen(command, cwd=cwd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                          stderr=subprocess.PIPE, **kwargs)
        except OSError as e:
            raise ToolError(f"could not start {command[0]}: {e}") from None
        threading.Thread(target=self._read, daemon=True, name=f"omni-lsp-{lang}").start()
        self._errors = threading.Thread(target=self._drain, daemon=True, name=f"omni-lsp-{lang}-stderr")
        self._errors.start()

    def initialize(self) -> None:
        """The LSP handshake. On failure the server is stopped, and stays inspectable (`dead`, `stderr`)."""
        try:
            root = path_to_uri(self.cwd)
            kinds = ["", "quickfix", "refactor", "refactor.extract", "refactor.inline", "refactor.rewrite", "source",
                     "source.organizeImports", "source.fixAll"]
            result = self.request("initialize", {
                "processId": os.getpid(), "rootUri": root, "rootPath": self.cwd,
                "workspaceFolders": [{"uri": root, "name": os.path.basename(self.cwd.rstrip("\\/")) or self.cwd}],
                "clientInfo": {"name": "omni"},
                "capabilities": {
                    "general": {"positionEncodings": list(ENCODINGS)},
                    "textDocument": {"synchronization": {"didSave": False}, "publishDiagnostics": {},
                                     "diagnostic": {"dynamicRegistration": False, "relatedDocumentSupport": False},
                                     "hover": {"contentFormat": ["plaintext", "markdown"]},
                                     "definition": {"linkSupport": True}, "references": {},
                                     "documentSymbol": {"hierarchicalDocumentSymbolSupport": True},
                                     "rename": {"prepareSupport": False},
                                     "codeAction": {"codeActionLiteralSupport": {"codeActionKind": {"valueSet": kinds}},
                                                    "isPreferredSupport": True, "disabledSupport": True, "dataSupport": True,
                                                    "resolveSupport": {"properties": ["edit"]}},
                                     "formatting": {}},
                    "workspace": {"symbol": {}, "workspaceFolders": True, "configuration": True,
                                  "workspaceEdit": {"documentChanges": True}}}}, timeout=INIT_TIMEOUT)
            self.capabilities = (result or {}).get("capabilities") or {}
            chosen = self.capabilities.get("positionEncoding")
            self.encoding = chosen if chosen in ("utf-32", "utf-16", "utf-8") else "utf-16"
            self.notify("initialized", {})
        except ToolError:
            self.dead = self.dead or "failed to start"
            self.stop()
            raise

    # -- wire
    def _drain(self) -> None:
        """Keep the tail of the server's stderr (and keep the pipe from filling up)."""
        err = self.popen.stderr
        try:
            for raw in iter(err.readline, b""):  # type: ignore[union-attr]
                line = raw.decode("utf-8", "replace").rstrip()
                if line:
                    self.stderr.append(line[:300])
        except (OSError, ValueError):
            pass

    def stderr_tail(self, count: int = STDERR_LINES) -> List[str]:
        """The last lines the server wrote to stderr. For a server that is going down, waits a moment for the rest."""
        if self.dead or self.popen.poll() is not None:
            try:
                self.popen.wait(timeout=0.5)
            except (subprocess.TimeoutExpired, OSError):
                pass
            self._errors.join(0.5)
        return list(self.stderr)[-count:]

    def _said(self) -> str:
        tail = self.stderr_tail(5)
        return ("\nIts last output:\n" + "\n".join("  " + t for t in tail)) if tail else ""

    def _send(self, msg: Dict[str, Any]) -> None:
        body = json.dumps(msg, ensure_ascii=False).encode("utf-8")
        with self._write:
            try:
                assert self.popen.stdin is not None
                self.popen.stdin.write(b"Content-Length: %d\r\n\r\n" % len(body) + body)
                self.popen.stdin.flush()
            except (OSError, ValueError):
                raise ToolError(f"the {self.lang} language server ({self.name}) has stopped{self._said()}") from None

    def _read(self) -> None:
        out = self.popen.stdout
        assert out is not None
        try:
            while True:
                length = 0
                while True:
                    line = out.readline()
                    if not line:
                        raise EOFError
                    line = line.strip()
                    if not line:
                        break
                    key, _, value = line.partition(b":")
                    if key.strip().lower() == b"content-length":
                        length = int(value.strip())
                body = out.read(length) if length else b""
                if len(body) < length:
                    raise EOFError
                try:
                    msg = json.loads(body.decode("utf-8", "replace"))
                except ValueError:
                    continue
                if isinstance(msg, dict):
                    self._dispatch(msg)
        except Exception as e:
            reason = "exited" if isinstance(e, (EOFError, OSError, ValueError)) else f"failed: {type(e).__name__}: {e}"
            with self.changed:
                self.dead = self.dead or reason
                for slot in self._pending.values():
                    slot["event"].set()
                self.changed.notify_all()

    def _dispatch(self, msg: Dict[str, Any]) -> None:
        method = msg.get("method")
        if method is None:                                   # a response to one of our requests
            with self.changed:
                slot = self._pending.get(msg.get("id"))  # type: ignore[arg-type]
            if slot is not None:
                slot["msg"] = msg
                slot["event"].set()
            return
        if "id" in msg:                                      # the server asks us something: answer so it never waits
            params = msg.get("params") or {}
            result: Any = [None] * len(params.get("items") or []) if method == "workspace/configuration" else None
            try:
                self._send({"jsonrpc": "2.0", "id": msg["id"], "result": result})
            except ToolError:
                pass
            return
        if method == "textDocument/publishDiagnostics":
            params = msg.get("params") or {}
            uri = self._known_uri(str(params.get("uri") or ""))
            with self.changed:
                self.diagnostics[uri] = list(params.get("diagnostics") or [])
                self.published[uri] = self.published.get(uri, 0) + 1
                self.changed.notify_all()

    def _known_uri(self, uri: str) -> str:
        """Servers may spell a URI differently (drive letter case, escaping): map it back to the one we opened."""
        if uri in self.docs:
            return uri
        path = uri_to_path(uri)
        for known in list(self.docs):
            if os.path.normcase(uri_to_path(known)) == os.path.normcase(path):
                return known
        return uri

    def notify(self, method: str, params: Any) -> None:
        self._send({"jsonrpc": "2.0", "method": method, "params": params})

    def request(self, method: str, params: Any, timeout: float = REQUEST_TIMEOUT, signal: Any = None) -> Any:
        if self.dead:
            raise ToolError(f"the {self.lang} language server ({self.name}) has {self.dead}{self._said()}")
        slot: Dict[str, Any] = {"event": threading.Event(), "msg": None}
        with self.changed:
            self._next += 1
            rid = self._next
            self._pending[rid] = slot
        try:
            self._send({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
            deadline = time.time() + timeout
            while not slot["event"].wait(0.25):
                if signal is not None and getattr(signal, "aborted", False):
                    self._cancel(rid)
                    raise ToolError("Operation aborted")
                if time.time() >= deadline:
                    self._cancel(rid)
                    raise ToolError(f"the {self.lang} language server ({self.name}) did not answer {method} within {timeout:g}s")
        finally:
            with self.changed:
                self._pending.pop(rid, None)
        msg = slot["msg"]
        if msg is None:
            raise ToolError(f"the {self.lang} language server ({self.name}) {self.dead or 'stopped'} while answering {method}"
                            f"{self._said()}")
        if msg.get("error"):
            err = msg["error"]
            raise ToolError(f"{self.name}: {method} failed: {err.get('message') if isinstance(err, dict) else err}")
        return msg.get("result")

    def _cancel(self, rid: int) -> None:
        try:
            self.notify("$/cancelRequest", {"id": rid})
        except ToolError:
            pass

    # -- documents
    @property
    def name(self) -> str:
        rest = self.command[2:] if len(self.command) > 2 and self.command[1] == "/c" else self.command
        shown = os.path.splitext(os.path.basename(rest[0]))[0]
        if re.match(r"(python[\d.]*|pythonw|py|node|deno|bun)$", shown, re.I):   # an interpreter: name the script it runs
            script = next((a for a in rest[1:] if not a.startswith("-")), None)
            if script:
                shown = os.path.splitext(os.path.basename(script))[0]
        return shown

    @property
    def running(self) -> bool:
        return self.dead is None and self.popen.poll() is None

    def open(self, path: str, language_id: str) -> Tuple[str, str, bool]:
        """Make the server's copy of the file match the disk. Returns (uri, text, whether anything was sent)."""
        try:
            text = Path(path).read_text(encoding="utf-8", errors="replace")
        except OSError as e:
            raise ToolError(f"cannot read {path}: {e.strerror or e}") from None
        uri = path_to_uri(path)
        doc = self.docs.get(uri)
        if doc is None:
            self.docs[uri] = {"version": 1, "text": text}
            self.notify("textDocument/didOpen", {"textDocument": {"uri": uri, "languageId": language_id, "version": 1, "text": text}})
            return uri, text, True
        if doc["text"] != text:
            doc["version"] += 1
            doc["text"] = text
            self.notify("textDocument/didChange", {"textDocument": {"uri": uri, "version": doc["version"]},
                                                   "contentChanges": [{"text": text}]})
            return uri, text, True
        return uri, text, False

    def stop(self) -> None:
        if self.popen.poll() is None and self.dead is None:
            try:
                self.request("shutdown", None, timeout=2.0)
                self.notify("exit", None)
            except ToolError:
                pass
        self.dead = self.dead or "been stopped"
        try:
            self.popen.wait(timeout=2.0)
        except subprocess.TimeoutExpired:
            try:
                if os.name == "nt":  # a .cmd shim runs the real server as a child: take the tree down
                    subprocess.run(["taskkill", "/F", "/T", "/PID", str(self.popen.pid)], capture_output=True, timeout=10)
                self.popen.kill()
            except Exception:
                pass
            try:
                self.popen.wait(timeout=3.0)
            except Exception:
                pass
        self._errors.join(1.0)
        streams = [self.popen.stdin, self.popen.stdout] + ([] if self._errors.is_alive() else [self.popen.stderr])
        for stream in streams:
            try:
                if stream is not None:
                    stream.close()
            except Exception:
                pass


def registry(session: Any) -> Dict[str, Server]:
    reg = getattr(session, "_lsp", None)
    if reg is None:
        reg = session._lsp = {}
    return reg


def server_for(session: Any, path: str) -> Tuple[Server, str]:
    """The (lazily started) server for this file's language, and the file's languageId."""
    lang, language_id = language_of(session, path)
    with _start_lock:
        reg = registry(session)
        srv = reg.get(lang)
        if srv is not None and not srv.running:
            srv.stop()
            srv = None
        if srv is None:
            command = command_for(session, lang, os.path.splitext(path)[1].lower().lstrip("."))
            srv = reg[lang] = Server(lang, command, session.cwd)
            srv.initialize()                                 # a failure leaves it registered, dead, for `status` to show
    return srv, language_id


def describe(session: Any) -> str:
    reg = getattr(session, "_lsp", None) or {}
    rows = ["Running:"]
    live = [s for s in reg.values() if s.running]
    rows += [f"  {s.lang:<4} {s.name} (pid {s.popen.pid}, {len(s.docs)} file(s) open, up {time.time() - s.started:.0f}s)" for s in live]
    if not live:
        rows.append("  none (a server starts the first time the lsp tool is used on a file of its language)")
    died = [s for s in reg.values() if not s.running]
    if died:
        rows.append("Died (restarted on next use):")
        for s in died:
            tail = s.stderr_tail()
            code = s.popen.poll()
            rows.append(f"  {s.lang:<4} {s.name} has {s.dead or 'exited'}" + (f" (exit code {code})" if code is not None else "")
                        + (f"; its last {len(tail)} line(s) of output:" if tail else "; it wrote nothing to stderr"))
            rows += [f"       | {t}" for t in tail]
    rows.append("Available:")
    over = _overrides(session)
    for lang in list(SERVERS) + [k for k in over if k not in SERVERS and k not in EXTENSIONS]:
        exts = ", ".join("." + e for e, (l, _i) in EXTENSIONS.items() if l == lang) or "." + lang
        custom = next((over[k] for k in [lang] + [e for e, (l, _i) in EXTENSIONS.items() if l == lang] if k in over), None)
        if custom:
            state = f"{custom[0]} (profile) - " + ("found" if resolve_command(custom) else "NOT on PATH")
        else:
            found = next((c[0] for c in SERVERS[lang] if resolve_command(c)), None)
            state = f"{found} - found" if found else f"not installed ({' or '.join(c[0] for c in SERVERS[lang])}): {INSTALL.get(lang, '')}"
        rows.append(f"  {lang:<4} {exts}: {state}")
    return "\n".join(rows)


# ------------------------------------------------------------------------------------------- rendering
class _Lines:
    """Source lines by path, read once per tool call."""

    def __init__(self, session: Any):
        self.cwd, self.cache = session.cwd, {}
        self.enc = "utf-16"                                  # the encoding of the server whose answers are being shown

    def rel(self, path: str) -> str:
        try:
            rel = os.path.relpath(os.path.realpath(path), os.path.realpath(self.cwd))
        except ValueError:
            return path.replace("\\", "/")
        return (path if rel.startswith("..") else rel).replace("\\", "/")

    def raw(self, path: str, index: int) -> str:
        if path not in self.cache:
            try:
                self.cache[path] = _rows(Path(path).read_text(encoding="utf-8", errors="replace"))
            except OSError:
                self.cache[path] = []
        rows = self.cache[path]
        return rows[index] if 0 <= index < len(rows) else ""

    def line(self, path: str, index: int) -> str:
        return self.raw(path, index).strip()[:200]

    def row(self, uri: str, rng: Dict[str, Any], text: Optional[str] = None) -> str:
        path = uri_to_path(uri)
        start = (rng or {}).get("start") or {}
        ln = int(start.get("line") or 0)
        col = from_units(self.raw(path, ln), int(start.get("character") or 0), self.enc)
        return f"{self.rel(path)}:{ln + 1}:{col + 1}  {self.line(path, ln) if text is None else text}".rstrip()


def _capped(rows: List[str]) -> str:
    return "\n".join(rows[:MAX_ROWS]) + (f"\n... and {len(rows) - MAX_ROWS} more" if len(rows) > MAX_ROWS else "")


def _locations(result: Any) -> List[Tuple[str, Dict[str, Any]]]:
    items = result if isinstance(result, list) else [result] if result else []
    out, seen = [], set()
    for loc in items:
        if not isinstance(loc, dict):
            continue
        uri = loc.get("uri") or loc.get("targetUri")
        rng = loc.get("range") or loc.get("targetSelectionRange") or loc.get("targetRange") or {}
        key = (uri, json.dumps(rng.get("start"), sort_keys=True))
        if uri and key not in seen:
            seen.add(key)
            out.append((uri, rng))
    return out


def _hover_text(contents: Any) -> str:
    if isinstance(contents, str):
        return contents
    if isinstance(contents, dict):
        return str(contents.get("value") or "")
    if isinstance(contents, list):
        return "\n\n".join(t for t in (_hover_text(c) for c in contents) if t)
    return ""


def _kind(n: Any) -> str:
    return SYMBOL_KINDS[n] if isinstance(n, int) and 0 < n < len(SYMBOL_KINDS) else "symbol"


def _aborted(signal: Any) -> bool:
    return signal is not None and bool(getattr(signal, "aborted", False))


def current_diagnostics(srv: Server, uri: str, before: int, sent: bool, limit: float, signal: Any = None) -> Tuple[List[Dict[str, Any]], bool]:
    """(diagnostics for an open file, whether they describe its current text).

    A server with `diagnosticProvider` is asked; any other is waited on (up to `limit` seconds) to publish.
    `before` is how many times it had published for this file before the file was last synced, `sent` whether
    that sync sent anything.
    """
    if srv.capabilities.get("diagnosticProvider"):
        params: Dict[str, Any] = {"textDocument": {"uri": uri}}
        if srv.result_ids.get(uri):
            params["previousResultId"] = srv.result_ids[uri]
        try:
            report = srv.request("textDocument/diagnostic", params, signal=signal) or {}
        except ToolError:
            if not srv.running or _aborted(signal):
                raise
            report = None                                    # it advertised pulling but would not answer: wait for a push
        if isinstance(report, dict):
            with srv.changed:
                if report.get("kind") != "unchanged":        # "unchanged": what is cached still holds
                    srv.diagnostics[uri] = [d for d in report.get("items") or [] if isinstance(d, dict)]
                if report.get("resultId"):
                    srv.result_ids[uri] = str(report["resultId"])
                else:
                    srv.result_ids.pop(uri, None)
                return list(srv.diagnostics.get(uri) or []), True
    floor = before if sent else 0
    deadline = time.time() + limit
    with srv.changed:
        while srv.published.get(uri, 0) <= floor and srv.running:
            remaining = deadline - time.time()
            if remaining <= 0 or _aborted(signal):
                break
            srv.changed.wait(min(remaining, 0.25))
        return list(srv.diagnostics.get(uri) or []), srv.published.get(uri, 0) > floor


def split_workspace_edit(edit: Any, lines: _Lines) -> Tuple[List[Tuple[str, Dict[str, Any]]], List[str]]:
    """A WorkspaceEdit as ([(uri, TextEdit)], [descriptions of file creations / renames / deletions])."""
    edits: List[Tuple[str, Dict[str, Any]]] = []
    others: List[str] = []
    edit = edit if isinstance(edit, dict) else {}
    if edit.get("documentChanges"):                          # when both are present, documentChanges wins
        for change in edit["documentChanges"]:
            if change.get("kind"):                           # file create / rename / delete
                others.append(f"{change['kind']} {lines.rel(uri_to_path(change.get('uri') or change.get('newUri') or ''))}")
                continue
            u = (change.get("textDocument") or {}).get("uri")
            edits += [(u, e) for e in change.get("edits") or [] if u and isinstance(e, dict)]
    else:
        for u, changes in (edit.get("changes") or {}).items():
            edits += [(u, e) for e in changes or [] if isinstance(e, dict)]
    return edits, others


def apply_edits(session: Any, srv: Server, edits: List[Tuple[str, Dict[str, Any]]], lines: _Lines) -> List[str]:
    """Write the server's text edits to disk. Returns the changed files (relative), or raises with nothing written."""
    root = os.path.normcase(os.path.realpath(session.cwd))
    grouped: Dict[str, Dict[str, Any]] = {}
    for u, e in edits:
        if urllib.parse.urlparse(str(u)).scheme != "file":
            raise ToolError(f"refused: the edit targets {u}, which is not a file. Nothing was written.")
        path = os.path.realpath(uri_to_path(u))
        grouped.setdefault(os.path.normcase(path), {"path": path, "uri": u, "edits": []})["edits"].append(e)
    plan: List[Tuple[str, bytes, str]] = []
    for key, g in grouped.items():
        path, rel = g["path"], lines.rel(g["path"])
        try:
            inside = os.path.commonpath([root, key]) == root
        except ValueError:                                   # another drive
            inside = False
        if not inside:
            raise ToolError(f"refused: the edit would change {path}, which is outside the project ({session.cwd}). Nothing was written.")
        try:
            text = Path(path).read_bytes().decode("utf-8")
        except OSError as e:
            raise ToolError(f"cannot read {rel}: {e.strerror or e}. Nothing was written.") from None
        except UnicodeDecodeError:
            raise ToolError(f"refused: {rel} is not UTF-8 text. Nothing was written.") from None
        doc = srv.docs.get(srv._known_uri(g["uri"]))
        if doc is not None and doc["text"] != _EOL.sub("\n", text):
            raise ToolError(f"refused: {rel} changed on disk after the server read it, so its edits may be misplaced. "
                            "Nothing was written; run the action again.")
        new = apply_text_edits(text, g["edits"], srv.encoding)
        if new != text:
            plan.append((path, new.encode("utf-8"), rel))
    for path, _data, rel in plan:                            # every file is cleared before the first one is written
        why = session.caps.gate("edit", {"path": path})
        if why:
            raise ToolError(f"the edit of {rel} was refused: {why}\nNothing was written.")
    done: List[str] = []
    for path, data, rel in plan:
        try:
            Path(path).write_bytes(data)
        except OSError as e:
            raise ToolError(f"cannot write {rel}: {e.strerror or e}." + (f" Already written: {', '.join(done)}." if done else "")) from None
        done.append(rel)
    for key, g in grouped.items():                           # tell the server about the files it has open
        known = srv._known_uri(g["uri"])
        if known in srv.docs:
            try:
                srv.open(uri_to_path(known), "")
            except ToolError:
                pass
    return sorted(done)


def _tools(session: Any) -> Dict[str, ToolDef]:
    def target(p: Dict[str, Any]) -> str:
        raw = str(p.get("path") or "").strip()
        if not raw:
            raise ToolError("path is required")
        path = resolve_to_cwd(raw, session.cwd)
        if not os.path.isfile(path):
            raise ToolError(f"no such file: {raw}")
        return path

    def position(p: Dict[str, Any], text: str) -> Tuple[int, int, str]:
        """(0-based line, 0-based character column, the line's text) the call points at."""
        try:
            line = int(p.get("line") or 0)
        except (TypeError, ValueError):
            line = 0
        rows = _rows(text)
        if line < 1 or line > len(rows):
            raise ToolError(f"line must be between 1 and {len(rows)} (lines are 1-based)")
        src = rows[line - 1]
        symbol = str(p.get("symbol") or "")
        if symbol:
            m = re.search(r"(?<![A-Za-z0-9_])" + re.escape(symbol) + r"(?![A-Za-z0-9_])", src)
            col = m.start() if m else src.find(symbol)
            if col < 0:
                raise ToolError(f"'{symbol}' does not appear on line {line}: {src.strip()[:160]}")
            last = re.split(r"[^A-Za-z0-9_]+", symbol)[-1]   # `obj.method` points at `method`
            if last and last != symbol:
                col += symbol.rfind(last)
        elif p.get("column") is not None:
            try:
                col = int(p["column"]) - 1
            except (TypeError, ValueError):
                raise ToolError("column must be a number (1-based)") from None
            if col < 0 or col > len(src):
                raise ToolError(f"column must be between 1 and {len(src) + 1} on line {line}")
        else:
            col = len(src) - len(src.lstrip())
        return line - 1, col, src

    def flag(value: Any) -> bool:
        return value.strip().lower() in ("1", "true", "yes", "on") if isinstance(value, str) else bool(value)

    def execute(_id, p, signal=None, on_update=None, ctx=None):
        action = str(p.get("action") or "status").lower().replace("-", "_")
        lines = _Lines(session)
        if action == "status":
            reg = registry(session)
            return text_result(describe(session), {"running": sorted(k for k, s in reg.items() if s.running)})

        if action == "workspace_symbols":
            query = str(p.get("query") or "").strip()
            if not query:
                raise ToolError("query is required")
            if p.get("path"):
                started, language_id = server_for(session, target(p))
                started.open(target(p), language_id)         # most servers index only once a file is open
            servers = [s for s in registry(session).values() if s.running]
            if not servers:
                raise ToolError("no language server is running yet. Pass `path` (any file of the language to search) to start one.")
            rows: List[str] = []
            for srv in servers:
                lines.enc = srv.encoding
                for sym in srv.request("workspace/symbol", {"query": query}, signal=signal) or []:
                    loc = sym.get("location") or {}
                    if loc.get("uri"):
                        owner = f"  (in {sym['containerName']})" if sym.get("containerName") else ""
                        rows.append(lines.row(loc["uri"], loc.get("range") or {}, f"{_kind(sym.get('kind'))} {sym.get('name')}{owner}"))
            if not rows:
                return text_result(f"No symbols match '{query}'.", {"count": 0})
            return text_result(f"{len(rows)} symbol(s) matching '{query}':\n{_capped(rows)}", {"count": len(rows)})

        if action not in ("definition", "references", "hover", "symbols", "diagnostics", "rename", "code_actions",
                          "apply_action", "format"):
            raise ToolError("action must be definition, references, hover, symbols, workspace_symbols, diagnostics, rename, "
                            "code_actions, apply_action, format or status")
        path = target(p)
        srv, language_id = server_for(session, path)
        before = srv.published.get(path_to_uri(path), 0)
        uri, text, sent = srv.open(path, language_id)
        doc = {"uri": uri}
        rel = lines.rel(path)
        lines.enc = srv.encoding

        def write(edits: List[Tuple[str, Dict[str, Any]]], others: List[str], what: str) -> List[str]:
            if others:
                raise ToolError(f"refused: {what} also creates, renames or deletes files ({'; '.join(others[:5])}), which this "
                                "tool does not do. Nothing was written.")
            return apply_edits(session, srv, edits, lines)

        if action == "format":
            insert_spaces = not re.search(r"^\t", text, re.M)
            result = srv.request("textDocument/formatting", {"textDocument": doc, "options": {
                "tabSize": 4, "insertSpaces": insert_spaces}}, signal=signal)
            edits = [(uri, e) for e in result or [] if isinstance(e, dict)]
            files = write(edits, [], "the formatting") if edits else []
            if not files:
                return text_result(f"{rel} is already formatted ({srv.name}): nothing was written.", {"count": 0, "files": [], "applied": False})
            return text_result(f"Formatted {rel} ({srv.name}): {len(edits)} edit(s) written.",
                               {"count": len(edits), "files": files, "applied": True})

        if action in ("code_actions", "apply_action"):
            line0, col, src = position(p, text)
            if p.get("symbol") or p.get("column") is not None:
                start = end = {"line": line0, "character": to_units(src, col, srv.encoding)}
                at = f"{rel}:{line0 + 1}:{col + 1}"
            else:                                            # no column: everything offered anywhere on the line
                start, end = {"line": line0, "character": 0}, {"line": line0, "character": to_units(src, len(src), srv.encoding)}
                at = f"{rel}:{line0 + 1}"
            found, _fresh = current_diagnostics(srv, uri, before, sent, 2.0 if sent else 0.0, signal)

            def on_line(d: Dict[str, Any]) -> bool:
                rng = d.get("range") or {}
                first = int((rng.get("start") or {}).get("line") or 0)
                return first <= line0 <= max(first, int((rng.get("end") or {}).get("line") or 0))
            result = srv.request("textDocument/codeAction", {"textDocument": doc, "range": {"start": start, "end": end},
                                                             "context": {"diagnostics": [d for d in found if on_line(d)],
                                                                         "triggerKind": 1}}, signal=signal)
            actions = [a for a in result or [] if isinstance(a, dict) and a.get("title")]
            provider = srv.capabilities.get("codeActionProvider")
            resolves = isinstance(provider, dict) and bool(provider.get("resolveProvider"))

            def state(a: Dict[str, Any]) -> str:
                """'' when the action can be applied, else why not."""
                if isinstance(a.get("disabled"), dict):
                    return "disabled: " + str(a["disabled"].get("reason") or "no reason given")[:120]
                if isinstance(a.get("edit"), dict) or (resolves and not isinstance(a.get("command"), str)):
                    return ""
                return "command only: cannot be applied" if a.get("command") else "no edit: cannot be applied"

            if action == "code_actions":
                if not actions:
                    return text_result(f"No code actions at {at} ({srv.name}).", {"count": 0, "actions": []})
                rows, listed = [], []
                for i, a in enumerate(actions, 1):
                    notes = [str(x) for x in (a.get("kind"), "preferred" if a.get("isPreferred") else "", state(a)) if x]
                    rows.append(f"{i}. {' '.join(str(a['title']).split())[:200]}" + (f"  [{', '.join(notes)}]" if notes else ""))
                    listed.append({"index": i, "title": a["title"], "kind": a.get("kind"), "applicable": not state(a)})
                return text_result(f"{len(actions)} code action(s) at {at}. Apply one with apply_action and its index:\n{_capped(rows)}",
                                   {"count": len(actions), "actions": listed[:MAX_ROWS]})

            try:
                index = int(p.get("index"))  # type: ignore[arg-type]
            except (TypeError, ValueError):
                raise ToolError("index is required: the number of the action as listed by code_actions") from None
            if index < 1 or index > len(actions):
                raise ToolError(f"index must be between 1 and {len(actions)}: the server offers {len(actions)} code action(s) at {at}"
                                if actions else f"the server offers no code actions at {at}")
            chosen = actions[index - 1]
            title = " ".join(str(chosen["title"]).split())[:200]
            if state(chosen):
                raise ToolError(f"refused: '{title}' cannot be applied ({state(chosen)}). Nothing was written.")
            if not isinstance(chosen.get("edit"), dict):     # the server fills the edit in on request
                chosen = srv.request("codeAction/resolve", chosen, signal=signal) or {}
            edits, others = split_workspace_edit(chosen.get("edit"), lines)
            if not edits and not others:
                raise ToolError(f"refused: '{title}' carries no edit" + (", only a command, and commands are not run"
                                                                        if chosen.get("command") else "") + ". Nothing was written.")
            files = write(edits, others, f"'{title}'")
            tail = " The command attached to it was not run." if chosen.get("command") else ""
            if not files:
                return text_result(f"'{title}' changes nothing: nothing was written.{tail}",
                                   {"count": 0, "files": [], "applied": False, "title": chosen.get("title")})
            return text_result(f"Applied '{title}': {len(edits)} edit(s) in {len(files)} file(s): {', '.join(files)}.{tail}",
                               {"count": len(edits), "files": files, "applied": True, "title": chosen.get("title")})

        if action == "symbols":
            result = srv.request("textDocument/documentSymbol", {"textDocument": doc}, signal=signal) or []
            rows = []

            def walk(items: List[Dict[str, Any]], depth: int) -> None:
                for sym in items:
                    loc = sym.get("location") or {}
                    rng = sym.get("selectionRange") or sym.get("range") or loc.get("range") or {}
                    owner = f"  (in {sym['containerName']})" if sym.get("containerName") else ""
                    rows.append(lines.row(loc.get("uri") or uri, rng, f"{'  ' * depth}{_kind(sym.get('kind'))} {sym.get('name')}{owner}"))
                    walk(sym.get("children") or [], depth + 1)
            walk(result, 0)
            if not rows:
                return text_result(f"No symbols in {rel}.", {"count": 0})
            return text_result(f"{len(rows)} symbol(s) in {rel}:\n{_capped(rows)}", {"count": len(rows)})

        if action == "diagnostics":
            try:
                limit = max(0.0, min(float(p.get("seconds") or DIAGNOSTICS_WAIT), 60.0))
            except (TypeError, ValueError):
                limit = DIAGNOSTICS_WAIT
            found, fresh = current_diagnostics(srv, uri, before, sent, limit, signal)
            if not fresh and not found:
                return text_result(f"{srv.name} published no diagnostics for {rel} within {limit:g}s. It may still be analysing: "
                                   "ask again, or pass a larger `seconds`.", {"count": 0, "fresh": False})
            if not found:
                return text_result(f"No problems in {rel} ({srv.name}).", {"count": 0, "fresh": fresh})
            found.sort(key=lambda d: (d.get("severity") or 1, ((d.get("range") or {}).get("start") or {}).get("line") or 0))
            rows = []
            for d in found:
                tag = " ".join(str(x) for x in (d.get("source"), d.get("code")) if x not in (None, ""))
                rows.append(lines.row(uri, d.get("range") or {}, f"{SEVERITY.get(d.get('severity') or 1, 'error')}: "
                                      f"{' '.join(str(d.get('message') or '').split())[:300]}" + (f"  [{tag}]" if tag else "")))
            errors = sum(1 for d in found if (d.get("severity") or 1) == 1)
            head = f"{len(found)} problem(s) in {rel} ({errors} error(s))" + ("" if fresh else ", as last reported by the server")
            return text_result(f"{head}:\n{_capped(rows)}", {"count": len(found), "errors": errors, "fresh": fresh})

        line0, col, src = position(p, text)
        pos = {"line": line0, "character": to_units(src, col, srv.encoding)}
        at = f"{rel}:{line0 + 1}:{col + 1}"
        params: Dict[str, Any] = {"textDocument": doc, "position": pos}

        if action == "hover":
            result = srv.request("textDocument/hover", params, signal=signal)
            body = _hover_text((result or {}).get("contents")).strip()
            if not body:
                return text_result(f"Nothing to show at {at}.", {"found": False})
            return text_result(f"{at}\n{body[:4000]}", {"found": True})

        if action in ("definition", "references"):
            if action == "references":
                params["context"] = {"includeDeclaration": True}
            locs = _locations(srv.request(f"textDocument/{action}", params, signal=signal))
            if not locs:
                return text_result(f"No {action} found for {at}. Check the line and symbol.", {"count": 0})
            rows = [lines.row(u, r) for u, r in locs]
            return text_result(f"{len(rows)} {'definition(s)' if action == 'definition' else 'reference(s)'} for {at}:\n{_capped(rows)}",
                               {"count": len(rows)})

        new_name = str(p.get("new_name") or "").strip()
        if not new_name:
            raise ToolError("new_name is required")
        result = srv.request("textDocument/rename", {**params, "newName": new_name}, signal=signal) or {}
        edits, others = split_workspace_edit(result, lines)
        if not edits and not others:
            return text_result(f"The server proposes no edits for renaming at {at}. Is that a renameable symbol?", {"count": 0})
        edits.sort(key=lambda ue: (ue[0], (ue[1].get("range") or {}).get("start", {}).get("line", 0),
                                   (ue[1].get("range") or {}).get("start", {}).get("character", 0)))
        rows = [lines.row(u, e.get("range") or {}) + ("" if e.get("newText") == new_name else f"  => {str(e.get('newText'))[:80]!r}")
                for u, e in edits] + others
        files = sorted({lines.rel(uri_to_path(u)) for u, _e in edits})
        old = str(p.get("symbol") or "the symbol")
        shown = [{"path": lines.rel(uri_to_path(u)), "range": e.get("range"), "newText": e.get("newText")} for u, e in edits[:MAX_ROWS]]
        if flag(p.get("apply")):
            written = write(edits, others, "the rename")
            return text_result(f"Renamed {old} to {new_name}: {len(edits)} place(s) in {len(written)} file(s) written: "
                               f"{', '.join(written) or 'none'}.",
                               {"count": len(edits), "files": written, "applied": bool(written), "edits": shown})
        return text_result(f"Renaming {old} to {new_name} would change {len(edits)} place(s) in {len(files)} file(s). "
                           f"Preview only: nothing was written. Pass apply: true to write them.\n{_capped(rows)}",
                           {"count": len(edits), "files": files, "applied": False, "edits": shown})

    return {"lsp": ToolDef(
        name="lsp", label="LSP",
        description="Ask the project's language server (precise where grep guesses). Actions: definition, references, "
                    "hover (path, line, and column or symbol: the name as written on that line), symbols(path) for a file "
                    "outline, workspace_symbols(query, path?), diagnostics(path) for errors after an edit, "
                    "rename(path, line, symbol, new_name) which previews the edits (apply: true writes them), "
                    "code_actions(path, line, column or symbol optional) listing quick fixes/refactors, "
                    "apply_action(path, line, index) writing one, format(path), status. "
                    "Lines and columns are 1-based. Results are `path:line:col  source line`.",
        parameters={"type": "object", "properties": {
            "action": {"type": "string", "enum": ["definition", "references", "hover", "symbols", "workspace_symbols",
                                                  "diagnostics", "rename", "code_actions", "apply_action", "format", "status"]},
            "path": {"type": "string"}, "line": {"type": "integer", "description": "1-based"},
            "column": {"type": "integer", "description": "1-based; or give symbol instead"},
            "symbol": {"type": "string", "description": "the name as it appears on that line"},
            "query": {"type": "string"}, "new_name": {"type": "string"},
            "apply": {"type": "boolean", "description": "rename: write the edits (default false: preview)"},
            "index": {"type": "integer", "description": "apply_action: number from code_actions, same line/column/symbol"},
            "seconds": {"type": "number", "description": "diagnostics: how long to wait for the server (default 4)"}},
            "required": ["action"]},
        execute=execute, prompt_snippet="Go to definition, find references, diagnostics via a language server")}


def _unload(session: Any) -> None:
    for srv in list((getattr(session, "_lsp", None) or {}).values()):
        try:
            srv.stop()
        except Exception:
            pass
    session._lsp = {}


CAPABILITIES = [Capability("lsp", "Language servers", "Definitions, references, hover, symbols, diagnostics, renames, code "
                           "actions and formatting from a real language server.", "Oh My Pi lsp tool", tools=_tools, unload=_unload,
                           commands=lambda s: {"lsp": ("Show running language servers and which are installed",
                                                       lambda _a: describe(s))})]
