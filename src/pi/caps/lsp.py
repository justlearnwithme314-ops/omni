"""`lsp`: ask a real language server instead of guessing with grep (Oh My Pi's lsp tool).

Text search finds strings; a language server knows the program. The `lsp` tool starts the server for a file's
language (once per session, on first use) and asks it:

    definition(path, line, column? | symbol?)     where the thing under the cursor is defined
    references(path, line, column? | symbol?)     every use of it
    hover(path, line, column? | symbol?)          its type and documentation
    symbols(path)                                 the outline of one file
    workspace_symbols(query, path?)               symbols by name across the project
    diagnostics(path)                             errors and warnings for a file, re-read from disk
    rename(path, line, symbol, new_name)          the edits a rename would make - a preview, nothing is written
    status                                        which servers run, which are installed

Lines and columns are 1-based, as editors show them. Instead of counting columns, give `symbol`: the name as it
appears on that line. Answers come back as `relative/path:line:col  source line`, at most 60 rows.

Servers are found on PATH by file extension: Python `pyright-langserver` (then `pylsp`), TypeScript/JavaScript
`typescript-language-server`, Go `gopls`, Rust `rust-analyzer`, C/C++ `clangd`. A profile can name its own:

    "lsp": {"py": ["pylsp"], "lua": ["lua-language-server"]}

Servers belong to the session and are shut down when it ends. `/lsp` shows what is running and what is installed.
"""
from __future__ import annotations

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
        self.capabilities: Dict[str, Any] = {}
        self.changed = threading.Condition()
        self._pending: Dict[int, Dict[str, Any]] = {}
        self._next = 0
        self._write = threading.Lock()
        self.dead: Optional[str] = None
        kwargs: Dict[str, Any] = {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)} if os.name == "nt" else {}
        try:
            self.popen = subprocess.Popen(command, cwd=cwd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                          stderr=subprocess.DEVNULL, **kwargs)
        except OSError as e:
            raise ToolError(f"could not start {command[0]}: {e}") from None
        threading.Thread(target=self._read, daemon=True, name=f"omni-lsp-{lang}").start()
        try:
            root = path_to_uri(cwd)
            result = self.request("initialize", {
                "processId": os.getpid(), "rootUri": root, "rootPath": cwd,
                "workspaceFolders": [{"uri": root, "name": os.path.basename(cwd.rstrip("\\/")) or cwd}],
                "clientInfo": {"name": "omni"},
                "capabilities": {
                    "textDocument": {"synchronization": {"didSave": False}, "publishDiagnostics": {},
                                     "hover": {"contentFormat": ["plaintext", "markdown"]},
                                     "definition": {"linkSupport": True}, "references": {},
                                     "documentSymbol": {"hierarchicalDocumentSymbolSupport": True},
                                     "rename": {"prepareSupport": False}},
                    "workspace": {"symbol": {}, "workspaceFolders": True, "configuration": True,
                                  "workspaceEdit": {"documentChanges": True}}}}, timeout=INIT_TIMEOUT)
            self.capabilities = (result or {}).get("capabilities") or {}
            self.notify("initialized", {})
        except ToolError:
            self.stop()
            raise

    # -- wire
    def _send(self, msg: Dict[str, Any]) -> None:
        body = json.dumps(msg, ensure_ascii=False).encode("utf-8")
        with self._write:
            try:
                assert self.popen.stdin is not None
                self.popen.stdin.write(b"Content-Length: %d\r\n\r\n" % len(body) + body)
                self.popen.stdin.flush()
            except (OSError, ValueError):
                raise ToolError(f"the {self.lang} language server ({self.name}) has stopped") from None

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
            raise ToolError(f"the {self.lang} language server ({self.name}) has {self.dead}")
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
            raise ToolError(f"the {self.lang} language server ({self.name}) {self.dead or 'stopped'} while answering {method}")
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
        for stream in (self.popen.stdin, self.popen.stdout):
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
    return srv, language_id


def describe(session: Any) -> str:
    reg = getattr(session, "_lsp", None) or {}
    rows = ["Running:"]
    live = [s for s in reg.values() if s.running]
    rows += [f"  {s.lang:<4} {s.name} (pid {s.popen.pid}, {len(s.docs)} file(s) open, up {time.time() - s.started:.0f}s)" for s in live]
    if not live:
        rows.append("  none (a server starts the first time the lsp tool is used on a file of its language)")
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

    def rel(self, path: str) -> str:
        try:
            rel = os.path.relpath(os.path.realpath(path), os.path.realpath(self.cwd))
        except ValueError:
            return path.replace("\\", "/")
        return (path if rel.startswith("..") else rel).replace("\\", "/")

    def line(self, path: str, index: int) -> str:
        if path not in self.cache:
            try:
                self.cache[path] = Path(path).read_text(encoding="utf-8", errors="replace").splitlines()
            except OSError:
                self.cache[path] = []
        rows = self.cache[path]
        return rows[index].strip()[:200] if 0 <= index < len(rows) else ""

    def row(self, uri: str, rng: Dict[str, Any], text: Optional[str] = None) -> str:
        path = uri_to_path(uri)
        start = (rng or {}).get("start") or {}
        ln, col = int(start.get("line") or 0), int(start.get("character") or 0)
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


def _tools(session: Any) -> Dict[str, ToolDef]:
    def target(p: Dict[str, Any]) -> str:
        raw = str(p.get("path") or "").strip()
        if not raw:
            raise ToolError("path is required")
        path = resolve_to_cwd(raw, session.cwd)
        if not os.path.isfile(path):
            raise ToolError(f"no such file: {raw}")
        return path

    def position(p: Dict[str, Any], text: str) -> Dict[str, int]:
        try:
            line = int(p.get("line") or 0)
        except (TypeError, ValueError):
            line = 0
        rows = text.splitlines()
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
        return {"line": line - 1, "character": col}

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
                for sym in srv.request("workspace/symbol", {"query": query}, signal=signal) or []:
                    loc = sym.get("location") or {}
                    if loc.get("uri"):
                        owner = f"  (in {sym['containerName']})" if sym.get("containerName") else ""
                        rows.append(lines.row(loc["uri"], loc.get("range") or {}, f"{_kind(sym.get('kind'))} {sym.get('name')}{owner}"))
            if not rows:
                return text_result(f"No symbols match '{query}'.", {"count": 0})
            return text_result(f"{len(rows)} symbol(s) matching '{query}':\n{_capped(rows)}", {"count": len(rows)})

        if action not in ("definition", "references", "hover", "symbols", "diagnostics", "rename"):
            raise ToolError("action must be definition, references, hover, symbols, workspace_symbols, diagnostics, rename or status")
        path = target(p)
        srv, language_id = server_for(session, path)
        before = srv.published.get(path_to_uri(path), 0)
        uri, text, sent = srv.open(path, language_id)
        doc = {"uri": uri}
        rel = lines.rel(path)

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
            deadline = time.time() + limit
            with srv.changed:
                while srv.published.get(uri, 0) <= (before if sent else 0) and srv.running:
                    remaining = deadline - time.time()
                    if remaining <= 0 or (signal is not None and getattr(signal, "aborted", False)):
                        break
                    srv.changed.wait(min(remaining, 0.25))
                fresh = srv.published.get(uri, 0) > (before if sent else 0)
                found = list(srv.diagnostics.get(uri) or [])
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

        pos = position(p, text)
        at = f"{rel}:{pos['line'] + 1}:{pos['character'] + 1}"
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
        edits: List[Tuple[str, Dict[str, Any]]] = []
        others: List[str] = []
        for u, changes in (result.get("changes") or {}).items():
            edits += [(u, e) for e in changes or []]
        for change in result.get("documentChanges") or []:
            if change.get("kind"):                           # file create / rename / delete
                others.append(f"{change['kind']} {lines.rel(uri_to_path(change.get('uri') or change.get('newUri') or ''))}")
                continue
            u = (change.get("textDocument") or {}).get("uri")
            edits += [(u, e) for e in change.get("edits") or [] if u]
        if not edits and not others:
            return text_result(f"The server proposes no edits for renaming at {at}. Is that a renameable symbol?", {"count": 0})
        edits.sort(key=lambda ue: (ue[0], (ue[1].get("range") or {}).get("start", {}).get("line", 0),
                                   (ue[1].get("range") or {}).get("start", {}).get("character", 0)))
        rows = [lines.row(u, e.get("range") or {}) + ("" if e.get("newText") == new_name else f"  => {str(e.get('newText'))[:80]!r}")
                for u, e in edits] + others
        files = sorted({lines.rel(uri_to_path(u)) for u, _e in edits})
        old = str(p.get("symbol") or "the symbol")
        return text_result(f"Renaming {old} to {new_name} would change {len(edits)} place(s) in {len(files)} file(s). "
                           f"Preview only: nothing was written. Make the edits yourself.\n{_capped(rows)}",
                           {"count": len(edits), "files": files, "applied": False,
                            "edits": [{"path": lines.rel(uri_to_path(u)), "range": e.get("range"), "newText": e.get("newText")}
                                      for u, e in edits[:MAX_ROWS]]})

    return {"lsp": ToolDef(
        name="lsp", label="LSP",
        description="Ask the project's language server (precise where grep guesses). Actions: definition, references, "
                    "hover (path, line, and column or symbol: the name as written on that line), symbols(path) for a file "
                    "outline, workspace_symbols(query, path?), diagnostics(path) for errors after an edit, "
                    "rename(path, line, symbol, new_name) which previews the edits without writing them, status. "
                    "Lines and columns are 1-based. Results are `path:line:col  source line`.",
        parameters={"type": "object", "properties": {
            "action": {"type": "string", "enum": ["definition", "references", "hover", "symbols", "workspace_symbols",
                                                  "diagnostics", "rename", "status"]},
            "path": {"type": "string"}, "line": {"type": "integer", "description": "1-based"},
            "column": {"type": "integer", "description": "1-based; or give symbol instead"},
            "symbol": {"type": "string", "description": "the name as it appears on that line"},
            "query": {"type": "string"}, "new_name": {"type": "string"},
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


CAPABILITIES = [Capability("lsp", "Language servers", "Definitions, references, hover, symbols, diagnostics and rename previews "
                           "from a real language server.", "Oh My Pi lsp tool", tools=_tools, unload=_unload,
                           commands=lambda s: {"lsp": ("Show running language servers and which are installed",
                                                       lambda _a: describe(s))})]
