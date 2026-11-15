"""Agent Client Protocol (ACP) server: editors that speak ACP (Zed and others) use Omni as their coding agent.

    omni acp [--profile NAME] [--model PATTERN]     serve ACP on stdin/stdout (the editor starts this)
    omni acp --print-config                         the `agent_servers` snippet for Zed's settings.json

Transport: JSON-RPC 2.0, one JSON object per line, UTF-8. stdout carries nothing but protocol lines; logs go to
stderr (`OMNI_ACP_DEBUG=1` for a trace). Standard library only - the wire format is written by hand.

Client -> agent
    initialize          -> {protocolVersion: 1, agentCapabilities, agentInfo, authMethods: []}
    authenticate        -> {}                                  (nothing to do: Omni reads its own credentials)
    session/new         {cwd, mcpServers} -> {sessionId}
    session/load        {sessionId, cwd, mcpServers} -> {}     history is replayed as session/update first
    session/prompt      {sessionId, prompt: [content blocks]} -> {stopReason}
    session/cancel      {sessionId}                            notification: aborts the running turn
Agent -> client
    session/update              notification {sessionId, update: {sessionUpdate: ..., ...}}
        agent_message_chunk / agent_thought_chunk / user_message_chunk   {content: {type: "text", text}}
        tool_call               {toolCallId, title, kind, status, rawInput, locations, content}
        tool_call_update        {toolCallId, status, content, ...}
        plan                    {entries: [{content, priority, status}]}
    session/request_permission  request {sessionId, toolCall, options} -> {outcome: {outcome, optionId}}

Where the shapes come from
--------------------------
Read from Hermes' `acp_adapter` (it builds these through the `acp` Python package, with snake_case names):
  - the method set, and that `session/load` must stream the transcript *before* it answers;
  - `sessionUpdate` tags `agent_message_chunk`, `agent_thought_chunk`, `user_message_chunk`, `tool_call`,
    `tool_call_update`, `plan`, and that chunk content is a `{type: "text", text}` block;
  - tool calls: id, title, kind, status, content, locations (path, line), raw input / raw output; kinds
    read / edit / search / execute / fetch / think / other; a diff content item with path, old text, new text;
  - plan entries `{content, priority: "medium", status: pending | in_progress | completed}`;
  - permission options `{optionId, name, kind}` with kinds allow_once / allow_always / reject_once /
    reject_always, a tool-call-update payload attached to the request, and the answer's `selected` outcome
    carrying the option id;
  - stop reasons `end_turn`, `cancelled`, `refusal`; `initialize` answering protocol version, agent info
    (name, version), agent capabilities (load session, prompt capabilities), auth methods.
From memory of the ACP specification (the package hides the JSON names; not checked against a live client):
  - every camelCase spelling: `protocolVersion`, `agentCapabilities`, `loadSession`, `promptCapabilities`
    {image, audio, embeddedContext}, `mcpCapabilities` {http, sse}, `agentInfo`, `authMethods`, `sessionId`,
    `sessionUpdate`, `toolCallId`, `rawInput`, `rawOutput`, `optionId`, `stopReason`, `oldText`, `newText`,
    `mimeType`;
  - protocol version being the integer 1;
  - tool content items being `{type: "content", content: <block>}` and `{type: "diff", path, oldText, newText}`;
  - the permission answer `{outcome: {outcome: "selected", optionId}}` / `{outcome: {outcome: "cancelled"}}`;
  - prompt blocks `{type: "resource", resource: {uri, text | blob, mimeType}}` and
    `{type: "resource_link", uri, name, title, mimeType}`;
  - the stop reason `max_tokens`, and -32002 as the "resource not found" error code;
  - the shape of Zed's `agent_servers` setting (`type: "custom"`, `command`, `args`, `env`).
Left out on purpose: `messageId` on chunks (Hermes sets it; it is newer than the stable schema), session modes,
model selection, slash-command advertisement, `session/list`, client-side `fs/*` and `terminal/*` (Omni's own
tools touch the disk directly), and MCP servers handed over in `session/new` (accepted and ignored).
"""
from __future__ import annotations

import argparse
import base64
import inspect
import io
import itertools
import json
import os
import sys
import threading
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import unquote, urlparse

PROTOCOL_VERSION = 1
PARSE_ERROR, INVALID_REQUEST, METHOD_NOT_FOUND, INVALID_PARAMS, INTERNAL_ERROR = -32700, -32600, -32601, -32602, -32603
NOT_FOUND = -32002
MAX_TOOL_TEXT = 20_000
MAX_DIFF_BYTES = 256 * 1024
MAX_RESOURCE_CHARS = 512 * 1024
DEFAULT_PERMISSION_TIMEOUT = 300.0

KINDS = {"read": "read", "ls": "read", "edit": "edit", "write": "edit", "bash": "execute", "grep": "search",
         "find": "search", "tool_search": "search", "fetch": "fetch", "web_search": "search", "task": "other",
         "todo": "other", "codemode": "execute"}
EDIT_TOOLS = ("write", "edit")


def _log(*parts: Any) -> None:
    try:
        print("[omni acp]", *parts, file=sys.stderr, flush=True)
    except Exception:
        pass


def _debug(*parts: Any) -> None:
    if os.environ.get("OMNI_ACP_DEBUG"):
        _log(*parts)


def _version() -> str:
    try:
        from .. import __version__  # type: ignore
        return str(__version__)
    except Exception:
        return "0"


# ------------------------------------------------------------------------------------------ pure helpers
def tool_kind(name: str) -> str:
    """ACP tool kind for an Omni tool name: read / edit / execute / search / fetch / other."""
    n = (name or "").lower()
    if n in KINDS:
        return KINDS[n]
    for kind, words in (("fetch", ("fetch", "http", "browse", "download", "web")),
                        ("search", ("search", "grep", "find", "glob")),
                        ("execute", ("bash", "shell", "exec", "run", "terminal")),
                        ("edit", ("edit", "write", "patch")),
                        ("read", ("read", "view", "list"))):
        if any(w in n for w in words):
            return kind
    return "other"


def tool_title(name: str, args: Any) -> str:
    a = args if isinstance(args, dict) else {}
    for key in ("path", "command", "pattern", "url", "query", "question", "description"):
        v = a.get(key)
        if isinstance(v, str) and v.strip():
            first = v.strip().splitlines()[0]
            return f"{name}: {first[:120]}" + ("..." if len(first) > 120 or "\n" in v.strip() else "")
    return name or "tool"


def text_block(text: str) -> Dict[str, Any]:
    return {"type": "text", "text": text}


def tool_text(text: str) -> Dict[str, Any]:
    if len(text) > MAX_TOOL_TEXT:
        text = text[:MAX_TOOL_TEXT] + f"\n[... {len(text) - MAX_TOOL_TEXT} more characters]"
    return {"type": "content", "content": text_block(text)}


def diff_block(path: str, old: Optional[str], new: str) -> Dict[str, Any]:
    return {"type": "diff", "path": path, "oldText": old, "newText": new}


def _content_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    out = []
    for b in content or []:
        if not isinstance(b, dict):
            continue
        if b.get("type") == "text":
            out.append(str(b.get("text") or ""))
        elif b.get("type") == "image":
            out.append("[image]")
    return "\n".join(out)


def path_from_uri(uri: str) -> Optional[str]:
    """A local path for a `file://` URI or a bare path; None for other schemes."""
    raw = (uri or "").strip()
    if not raw:
        return None
    if len(raw) >= 3 and raw[0].isalpha() and raw[1] == ":" and raw[2] in "/\\":
        return raw  # a bare Windows path, which urlparse would read as a scheme
    p = urlparse(raw)
    if not p.scheme:
        return unquote(raw)
    if p.scheme != "file" or p.netloc not in ("", "localhost"):
        return None
    path = unquote(p.path or "")
    if len(path) >= 3 and path[0] == "/" and path[2] == ":" and path[1].isalpha():
        path = path[1:]  # file:///C:/x -> C:/x
    return path or None


def _mention(uri: str, cwd: str) -> str:
    path = path_from_uri(uri)
    if path is None:
        return uri
    try:
        rel = os.path.relpath(path, cwd)
        if not rel.startswith(".."):
            path = rel
    except ValueError:  # another drive
        pass
    return "@" + path.replace("\\", "/")


def prompt_from_blocks(blocks: Any, cwd: str = ".") -> Tuple[str, List[Dict[str, Any]]]:
    """ACP prompt content blocks -> (prompt text, images for `AgentSession.prompt`).

    Text is kept as is; an embedded `resource` contributes its text under an "[Attached file: ...]" header (Hermes'
    format); a `resource_link` becomes an `@path` mention for a local file, otherwise the URI."""
    parts: List[str] = []
    images: List[Dict[str, Any]] = []
    for b in blocks if isinstance(blocks, list) else []:
        if isinstance(b, str):
            parts.append(b)
            continue
        if not isinstance(b, dict):
            continue
        t = b.get("type")
        if t == "text":
            if b.get("text"):
                parts.append(str(b["text"]))
        elif t == "resource_link":
            if b.get("uri"):
                parts.append(_mention(str(b["uri"]), cwd))
        elif t == "resource":
            r = b.get("resource") if isinstance(b.get("resource"), dict) else {}
            uri = str(r.get("uri") or "")
            name = os.path.basename(path_from_uri(uri) or uri) or uri or "resource"
            body = r.get("text")
            if body is None and r.get("blob"):
                mime = str(r.get("mimeType") or "")
                if mime.startswith("image/"):
                    images.append({"type": "image", "data": r["blob"], "mimeType": mime})
                    parts.append(f"[Attached image: {name}]")
                    continue
                try:
                    data = base64.b64decode(r["blob"])
                    body = data.decode("utf-8") if b"\x00" not in data else None
                except Exception:
                    body = None
                if body is None:
                    body = f"[Binary file omitted, mime={mime or 'unknown'}]"
            if body is None:
                if uri:
                    parts.append(_mention(uri, cwd))
                continue
            body = str(body)
            if len(body) > MAX_RESOURCE_CHARS:
                body = body[:MAX_RESOURCE_CHARS] + "\n[truncated]"
            parts.append(f"[Attached file: {name}]\nURI: {uri}\n\n{body}")
        elif t == "image":
            if b.get("data"):
                images.append({"type": "image", "data": b["data"], "mimeType": b.get("mimeType") or "image/png"})
            elif b.get("uri"):
                parts.append(str(b["uri"]))
    return "\n\n".join(parts), images


def plan_update(todos: Any) -> Optional[Dict[str, Any]]:
    """The todo tool's list as ACP's native plan (editors show it as a task panel)."""
    if not isinstance(todos, list):
        return None
    entries = []
    for t in todos:
        if isinstance(t, dict) and str(t.get("content") or "").strip():
            status = t.get("status") if t.get("status") in ("pending", "in_progress", "completed") else "pending"
            entries.append({"content": str(t["content"]), "priority": "medium", "status": status})
    return {"sessionUpdate": "plan", "entries": entries}


def permission_options(options: List[str]) -> List[Dict[str, str]]:
    """Picker choices -> ACP permission options. The option id is `opt<N>`, so the answer maps back by position."""
    out = []
    for i, label in enumerate(options):
        low = str(label).lower()
        if low.startswith("always"):
            kind = "allow_always"
        elif low.startswith(("deny", "reject", "no", "cancel", "keep ")):
            kind = "reject_once"
        else:
            kind = "allow_once"
        out.append({"optionId": f"opt{i}", "name": str(label), "kind": kind})
    return out


# ------------------------------------------------------------------------------------------ the session UI
class AcpUI:
    """`session.ui` for an ACP session: questions the agent asks (the permission gate, the `approvals`, `ask` and
    `plan` capabilities all go through `ext_select`) become `session/request_permission` requests to the editor.

    Create it unbound (`AcpUI()`) and pass it to `AgentSession(opts, ui=...)`; `serve` binds it to the connection.
    Unbound, or when the editor does not answer in time, every question resolves to its cancel value."""
    has_ui = True
    width, height = 100, 30

    def __init__(self) -> None:
        self.server: Optional["AcpServer"] = None
        self.session_id: Optional[str] = None

    def bind(self, server: "AcpServer", session_id: str) -> None:
        self.server, self.session_id = server, session_id

    def ext_select(self, title: str = "", options: Optional[List[str]] = None, **_: Any) -> Optional[str]:
        options = [str(o) for o in options or []]
        if self.server is None or not self.session_id or not options:
            return None
        return self.server.request_permission(self.session_id, title, options)

    def ext_confirm(self, title: str = "", message: str = "", **_: Any) -> bool:
        if self.server is None or not self.session_id:
            return False
        q = f"{title}\n{message}".strip() if message else title
        return self.server.request_permission(self.session_id, q, ["Allow", "Reject"]) == "Allow"

    def __getattr__(self, name: str) -> Any:
        if name.startswith("ext_"):  # input, notify, setStatus, ...: nothing in ACP to show them with
            return lambda **kw: None
        raise AttributeError(name)


# ------------------------------------------------------------------------------------------ per-session state
class _Session:
    def __init__(self, server: "AcpServer", session_id: str, agent: Any):
        self.server, self.id, self.agent = server, session_id, agent
        self.lock = threading.Lock()
        self.running = False
        self.cancelled = False
        self.announced: Dict[str, str] = {}        # toolCallId -> tool name (a tool_call was sent)
        self.active: List[str] = []                # toolCallIds executing now
        self.snapshots: Dict[str, Tuple[str, Optional[str]]] = {}  # toolCallId -> (abs path, text before)
        self.last_stop: Optional[str] = None
        self.error: Optional[str] = None
        self.refused = False
        self.unsubscribe = agent.subscribe(self.on_event)

    # -- outgoing
    def update(self, update: Dict[str, Any]) -> None:
        self.server.notify("session/update", {"sessionId": self.id, "update": update})

    def _abs(self, path: str) -> str:
        return path if os.path.isabs(path) else os.path.join(self.agent.cwd, path)

    def _read(self, path: str) -> Optional[str]:
        try:
            if os.path.getsize(path) > MAX_DIFF_BYTES:
                return None
            with open(path, encoding="utf-8") as f:
                return f.read()
        except (OSError, UnicodeDecodeError):
            return None

    def _announce(self, call_id: str, name: str, args: Any, status: str) -> None:
        a = args if isinstance(args, dict) else {}
        upd: Dict[str, Any] = {"sessionUpdate": "tool_call", "toolCallId": call_id, "title": tool_title(name, a),
                               "kind": tool_kind(name), "status": status, "rawInput": a}
        if isinstance(a.get("path"), str) and a["path"]:
            loc: Dict[str, Any] = {"path": self._abs(a["path"])}
            if isinstance(a.get("offset"), int):
                loc["line"] = a["offset"]
            upd["locations"] = [loc]
        self.announced[call_id] = name
        self.update(upd)

    # -- incoming agent events (worker and tool-pool threads)
    def on_event(self, ev: Dict[str, Any]) -> None:
        try:
            self._on_event(ev)
        except Exception as e:  # a rendering problem must never break the turn
            _log(f"event {ev.get('type')}: {type(e).__name__}: {e}")

    def _on_event(self, ev: Dict[str, Any]) -> None:
        t = ev.get("type")
        if t == "message_update":
            d = ev.get("assistantMessageEvent") or {}
            if d.get("type") == "text_delta" and d.get("delta"):
                self.update({"sessionUpdate": "agent_message_chunk", "content": text_block(d["delta"])})
            elif d.get("type") == "thinking_delta" and d.get("delta"):
                self.update({"sessionUpdate": "agent_thought_chunk", "content": text_block(d["delta"])})
            elif d.get("type") == "toolcall_end" and isinstance(d.get("toolCall"), dict):
                c = d["toolCall"]
                self._announce(str(c.get("id")), str(c.get("name")), c.get("arguments"), "pending")
        elif t == "tool_execution_start":
            cid, name, args = str(ev.get("toolCallId")), str(ev.get("toolName")), ev.get("args") or {}
            if name in EDIT_TOOLS and isinstance(args.get("path"), str):
                p = self._abs(args["path"])
                self.snapshots[cid] = (p, self._read(p) if os.path.exists(p) else "")
            self.active.append(cid)
            if cid in self.announced:
                self.update({"sessionUpdate": "tool_call_update", "toolCallId": cid, "status": "in_progress"})
            else:
                self._announce(cid, name, args, "in_progress")
        elif t == "tool_execution_end":
            self._tool_end(ev)
        elif t == "message_end":
            m = ev.get("message") or {}
            if m.get("role") == "assistant":
                self.last_stop = m.get("stopReason")
                if m.get("stopReason") == "error":
                    self.error = str(m.get("errorMessage") or "the model request failed")
        elif t == "error":
            self.error = str(ev.get("message") or "error")
        elif t == "notice" and "blocked by a hook" in str(ev.get("message") or ""):
            self.refused = True
            self.update({"sessionUpdate": "agent_message_chunk", "content": text_block(str(ev["message"]))})

    def _tool_end(self, ev: Dict[str, Any]) -> None:
        cid, name = str(ev.get("toolCallId")), str(ev.get("toolName"))
        result = ev.get("result") if isinstance(ev.get("result"), dict) else {}
        failed = bool(ev.get("isError"))
        if cid in self.active:
            self.active.remove(cid)
        content: List[Dict[str, Any]] = []
        snap = self.snapshots.pop(cid, None)
        if snap and not failed:
            path, old = snap
            new = self._read(path)
            if old is not None and new is not None and old != new:
                content.append(diff_block(path, old or None, new))
        text = _content_text(result.get("content"))
        if text:
            content.append(tool_text(text))
        self.announced.pop(cid, None)
        self.update({"sessionUpdate": "tool_call_update", "toolCallId": cid, "status": "failed" if failed else "completed",
                     "content": content})
        if name == "todo" and not failed:
            plan = plan_update((result.get("details") or {}).get("todos"))
            if plan:
                self.update(plan)

    def close_open_tool_calls(self) -> None:
        """A call the model emitted but the loop never ran (aborted, skipped) must not spin forever."""
        for cid in list(self.announced):
            self.announced.pop(cid, None)
            self.update({"sessionUpdate": "tool_call_update", "toolCallId": cid, "status": "failed",
                         "content": [tool_text("This tool call ended without a result.")]})
        self.active.clear()
        self.snapshots.clear()

    # -- history
    def replay(self) -> None:
        for e in self.agent.sessions.get_branch():
            if e.get("type") != "message":
                continue
            m = e.get("message") or {}
            role = m.get("role")
            if role == "user":
                text = _content_text(m.get("content"))
                if text:
                    self.update({"sessionUpdate": "user_message_chunk", "content": text_block(text)})
            elif role == "assistant":
                for b in m.get("content") or []:
                    if not isinstance(b, dict):
                        continue
                    if b.get("type") == "thinking" and b.get("thinking"):
                        self.update({"sessionUpdate": "agent_thought_chunk", "content": text_block(b["thinking"])})
                    elif b.get("type") == "text" and b.get("text"):
                        self.update({"sessionUpdate": "agent_message_chunk", "content": text_block(b["text"])})
                    elif b.get("type") == "toolCall":
                        self._announce(str(b.get("id")), str(b.get("name")), b.get("arguments"), "pending")
            elif role == "toolResult":
                cid = str(m.get("toolCallId"))
                text = _content_text(m.get("content"))
                self.announced.pop(cid, None)
                self.update({"sessionUpdate": "tool_call_update", "toolCallId": cid,
                             "status": "failed" if m.get("isError") else "completed",
                             "content": [tool_text(text)] if text else []})
        self.close_open_tool_calls()


# ------------------------------------------------------------------------------------------ the server
class AcpServer:
    def __init__(self, make_session: Callable[..., Any], stdin: Any, stdout: Any,
                 permission_timeout: Optional[float] = None):
        self.make_session = make_session
        self.stdin, self.stdout = stdin, stdout
        self.binary = isinstance(stdout, (io.RawIOBase, io.BufferedIOBase))  # sys.stdout.buffer; else a text stream
        if permission_timeout is None:
            try:
                permission_timeout = float(os.environ.get("OMNI_ACP_PERMISSION_TIMEOUT") or DEFAULT_PERMISSION_TIMEOUT)
            except ValueError:
                permission_timeout = DEFAULT_PERMISSION_TIMEOUT
        self.permission_timeout = permission_timeout
        self.sessions: Dict[str, _Session] = {}
        self.write_lock = threading.Lock()
        self.state_lock = threading.Lock()
        self.pending: Dict[str, Dict[str, Any]] = {}   # our request id -> {event, result, session}
        self.ids = itertools.count(1)
        self.workers: List[threading.Thread] = []
        self.closed = False
        self.methods: Dict[str, Tuple[Callable[[Dict[str, Any]], Any], bool]] = {
            "initialize": (self.m_initialize, False), "authenticate": (self.m_authenticate, False),
            "session/new": (self.m_new, True), "session/load": (self.m_load, True),
            "session/prompt": (self.m_prompt, True), "session/cancel": (self.m_cancel, False)}

    # -- wire
    def send(self, obj: Dict[str, Any]) -> None:
        line = json.dumps(obj, ensure_ascii=False, default=str) + "\n"
        with self.write_lock:
            if self.closed and "method" in obj:
                return
            try:
                self.stdout.write(line.encode("utf-8") if self.binary else line)
                self.stdout.flush()
            except Exception as e:
                _debug(f"write failed: {e}")
        _debug("->", line.rstrip()[:400])

    def notify(self, method: str, params: Dict[str, Any]) -> None:
        self.send({"jsonrpc": "2.0", "method": method, "params": params})

    def reply(self, rid: Any, result: Any) -> None:
        self.send({"jsonrpc": "2.0", "id": rid, "result": result})

    def fail(self, rid: Any, code: int, message: str) -> None:
        self.send({"jsonrpc": "2.0", "id": rid, "error": {"code": code, "message": message}})

    # -- main loop
    def run(self) -> int:
        try:
            while True:
                line = self.stdin.readline()
                if not line:
                    break
                if isinstance(line, bytes):
                    line = line.decode("utf-8", errors="replace")
                line = line.strip().lstrip("\ufeff")
                if line:
                    self.handle_line(line)
        except KeyboardInterrupt:
            pass
        finally:
            self.close()
        return 0

    def handle_line(self, line: str) -> None:
        _debug("<-", line[:400])
        try:
            msg = json.loads(line)
        except ValueError as e:
            self.fail(None, PARSE_ERROR, f"Parse error: {e}")
            return
        if not isinstance(msg, dict):
            self.fail(None, INVALID_REQUEST, "Invalid request: expected a JSON object")
            return
        rid, method = msg.get("id"), msg.get("method")
        if method is None:  # an answer to one of our requests
            if "result" in msg or "error" in msg:
                self.resolve(str(rid), msg.get("result") if "error" not in msg else None)
            else:
                self.fail(rid, INVALID_REQUEST, "Invalid request: no method")
            return
        params = msg.get("params") if isinstance(msg.get("params"), dict) else {}
        entry = self.methods.get(method) if isinstance(method, str) else None
        if entry is None:
            if rid is not None:  # unknown notifications are ignored, as JSON-RPC asks
                self.fail(rid, METHOD_NOT_FOUND, f"Method not found: {method}")
            return
        fn, threaded = entry
        if threaded:
            t = threading.Thread(target=self.call, args=(fn, rid, params), name=f"acp-{method}", daemon=False)
            with self.state_lock:
                self.workers = [w for w in self.workers if w.is_alive()] + [t]
            t.start()
        else:
            self.call(fn, rid, params)

    def call(self, fn: Callable[[Dict[str, Any]], Any], rid: Any, params: Dict[str, Any]) -> None:
        try:
            result = fn(params)
        except RpcError as e:
            if rid is not None:
                self.fail(rid, e.code, str(e))
            return
        except BaseException as e:  # SystemExit from session start included
            _log(f"{getattr(fn, '__name__', fn)}: {type(e).__name__}: {e}")
            if rid is not None:
                self.fail(rid, INTERNAL_ERROR, f"{type(e).__name__}: {e}")
            return
        if rid is not None:
            self.reply(rid, result)

    def close(self) -> None:
        """EOF: stop every turn, wait for the workers, shut the sessions down."""
        with self.write_lock:
            self.closed = True
        with self.state_lock:
            sessions = list(self.sessions.values())
            waiting = list(self.pending.values())
            workers = list(self.workers)
        for p in waiting:
            p["event"].set()
        for s in sessions:
            s.cancelled = True
            try:
                s.agent.abort()
            except Exception:
                pass
        for w in workers:
            w.join(timeout=30)
        for s in sessions:
            try:
                s.unsubscribe()
                s.agent.shutdown("quit")
            except Exception as e:
                _log(f"shutdown: {e}")
        with self.state_lock:
            self.sessions.clear()

    # -- agent -> client requests
    def resolve(self, rid: str, result: Any) -> None:
        with self.state_lock:
            p = self.pending.get(rid)
        if p is not None:
            p["result"] = result
            p["event"].set()

    def request(self, method: str, params: Dict[str, Any], timeout: float, session_id: Optional[str] = None) -> Any:
        """Send a request to the editor and block for its answer; None on timeout, error, cancel or EOF."""
        rid = f"omni-{next(self.ids)}"
        p = {"event": threading.Event(), "result": None, "session": session_id}
        with self.state_lock:
            if self.closed:
                return None
            self.pending[rid] = p
        try:
            self.send({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
            if not p["event"].wait(timeout):
                _log(f"{method} timed out after {timeout:g}s")
            return p["result"]
        finally:
            with self.state_lock:
                self.pending.pop(rid, None)

    def request_permission(self, session_id: str, question: str, options: List[str]) -> Optional[str]:
        """Forward a question to the editor; returns the chosen option text, or None (rejected / cancelled / late)."""
        s = self.sessions.get(session_id)
        opts = permission_options(options)
        own_bubble = True
        call: Dict[str, Any] = {"toolCallId": f"perm-{next(self.ids)}", "title": question.strip().splitlines()[0][:200] if question.strip() else "Permission",
                                "kind": "other", "status": "pending", "content": [tool_text(question)]}
        if s is not None and len(s.active) == 1:  # asked from inside a tool: attach to that tool call
            cid = s.active[0]
            call = {"toolCallId": cid, "title": tool_title(s.announced.get(cid, ""), {}) + " - " + call["title"],
                    "content": [tool_text(question)]}
            own_bubble = False
        result = self.request("session/request_permission", {"sessionId": session_id, "toolCall": call, "options": opts},
                              self.permission_timeout, session_id)
        outcome = result.get("outcome") if isinstance(result, dict) else None
        picked = None
        if isinstance(outcome, dict) and outcome.get("outcome") == "selected":
            for o, label in zip(opts, options):
                if o["optionId"] == outcome.get("optionId"):
                    picked = label
        if own_bubble and s is not None:
            allowed = picked is not None and not permission_options([picked])[0]["kind"].startswith("reject")
            s.update({"sessionUpdate": "tool_call_update", "toolCallId": call["toolCallId"],
                      "status": "completed" if allowed else "failed"})
        return picked

    # -- methods
    def m_initialize(self, p: Dict[str, Any]) -> Dict[str, Any]:
        return {"protocolVersion": PROTOCOL_VERSION,
                "agentCapabilities": {"loadSession": True,
                                      "promptCapabilities": {"image": False, "audio": False, "embeddedContext": True},
                                      "mcpCapabilities": {"http": False, "sse": False}},
                "agentInfo": {"name": "omni", "title": "Omni", "version": _version()},
                "authMethods": []}

    def m_authenticate(self, p: Dict[str, Any]) -> Dict[str, Any]:
        return {}

    def _adopt(self, agent: Any) -> _Session:
        sid = str(agent.sessions.session_id)
        if isinstance(getattr(agent, "ui", None), AcpUI):
            ui = agent.ui
        else:  # a session built with another headless UI: give it one that can ask the editor
            ui = agent.ui = AcpUI()
            gate = getattr(agent, "gate", None)
            if gate is not None and getattr(gate, "ask", None) is None:
                gate.ask = agent._ask_permission
        ui.bind(self, sid)
        s = _Session(self, sid, agent)
        with self.state_lock:
            self.sessions[sid] = s
        return s

    def _cwd(self, p: Dict[str, Any]) -> str:
        cwd = p.get("cwd")
        if not isinstance(cwd, str) or not cwd:
            raise RpcError(INVALID_PARAMS, "cwd is required")
        if not os.path.isdir(cwd):
            raise RpcError(INVALID_PARAMS, f"cwd is not a directory: {cwd}")
        return cwd

    def m_new(self, p: Dict[str, Any]) -> Dict[str, Any]:
        s = self._adopt(self.make_session(self._cwd(p)))
        for d in getattr(s.agent, "diagnostics", None) or []:
            _log(f"warning: {d}")
        return {"sessionId": s.id}

    def m_load(self, p: Dict[str, Any]) -> Dict[str, Any]:
        sid = p.get("sessionId")
        if not isinstance(sid, str) or not sid:
            raise RpcError(INVALID_PARAMS, "sessionId is required")
        s = self.sessions.get(sid)
        if s is None:
            cwd = self._cwd(p)
            try:
                agent = self.make_session(cwd, sid)
            except (SystemExit, FileNotFoundError, ValueError) as e:
                raise RpcError(NOT_FOUND, f"Session not found: {sid} ({e})")
            if str(agent.sessions.session_id) != sid:  # a prefix matched some other session
                try:
                    agent.shutdown("quit")
                except Exception:
                    pass
                raise RpcError(NOT_FOUND, f"Session not found: {sid}")
            s = self._adopt(agent)
        elif s.running:
            raise RpcError(INVALID_REQUEST, "session is busy")
        s.replay()
        return {}

    def _session(self, p: Dict[str, Any]) -> _Session:
        s = self.sessions.get(str(p.get("sessionId")))
        if s is None:
            raise RpcError(NOT_FOUND, f"Unknown session: {p.get('sessionId')}")
        return s

    def m_prompt(self, p: Dict[str, Any]) -> Dict[str, Any]:
        s = self._session(p)
        text, images = prompt_from_blocks(p.get("prompt"), s.agent.cwd)
        if not text.strip() and not images:
            raise RpcError(INVALID_PARAMS, "prompt is empty")
        with s.lock:
            if s.running:
                raise RpcError(INVALID_REQUEST, "a prompt is already running in this session")
            s.running, s.cancelled, s.last_stop, s.error, s.refused = True, False, None, None, False
        try:
            s.agent.prompt(text, images or None, "acp")
            s.agent.wait_for_idle(5)
        finally:
            s.close_open_tool_calls()
            s.running = False
        if s.cancelled or s.last_stop == "aborted":
            return {"stopReason": "cancelled"}
        if s.error:
            raise RpcError(INTERNAL_ERROR, s.error)
        if s.refused or s.last_stop == "refusal":
            return {"stopReason": "refusal"}
        return {"stopReason": "max_tokens" if s.last_stop == "length" else "end_turn"}

    def m_cancel(self, p: Dict[str, Any]) -> None:
        s = self.sessions.get(str(p.get("sessionId")))
        if s is None:
            return None
        if s.running:
            s.cancelled = True
        s.agent.abort()
        with self.state_lock:
            waiting = [w for w in self.pending.values() if w["session"] == s.id]
        for w in waiting:  # a question nobody will answer any more
            w["event"].set()
        return None


class RpcError(Exception):
    def __init__(self, code: int, message: str):
        super().__init__(message)
        self.code = code


def serve(make_session: Callable[..., Any], stdin: Any = None, stdout: Any = None,
          permission_timeout: Optional[float] = None) -> int:
    """Serve ACP until stdin closes. `make_session(cwd, session_path=None)` returns a started headless
    `AgentSession`; `session_path` is a stored session's id (or file) for `session/load`.

    With the process' own streams (`stdout=None`), `sys.stdout` is pointed at stderr for the duration, so a stray
    `print` from a tool or plugin cannot corrupt the protocol stream."""
    own = stdout is None
    saved = sys.stdout
    if stdin is None:
        stdin = getattr(sys.stdin, "buffer", sys.stdin)
    if own:
        stdout = getattr(sys.stdout, "buffer", sys.stdout)
        sys.stdout = sys.stderr
    try:
        return AcpServer(make_session, stdin, stdout, permission_timeout).run()
    finally:
        if own:
            sys.stdout = saved


# ------------------------------------------------------------------------------------------ command line
def zed_config(profile: Optional[str] = None, model: Optional[str] = None) -> str:
    """The snippet for Zed's settings.json: Omni as a custom agent server."""
    main = Path(__file__).resolve().parents[2] / "omni_main.py"
    args = [str(main), "acp"]
    if profile:
        args += ["--profile", profile]
    if model:
        args += ["--model", model]
    return json.dumps({"agent_servers": {"Omni": {"type": "custom", "command": sys.executable, "args": args, "env": {}}}},
                      indent=2)


def _call_factory(make_session: Callable[..., Any], cwd: str, session_path: Optional[str], extra: Dict[str, Any]) -> Any:
    """Call the launcher's factory with the keywords it accepts (profile, model, ui)."""
    try:
        params = inspect.signature(make_session).parameters
        if not any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values()):
            extra = {k: v for k, v in extra.items() if k in params}
    except (TypeError, ValueError):
        extra = {}
    return make_session(cwd, session_path, **extra)


def cli(argv: List[str], make_session: Callable[..., Any]) -> int:
    """`omni acp [--profile NAME] [--model PATTERN] [--print-config]`.

    `make_session(cwd, session_path=None, profile=None, model=None, ui=None)` builds and starts the session."""
    ap = argparse.ArgumentParser(prog="omni acp", description="Serve the Agent Client Protocol on stdin/stdout.")
    ap.add_argument("--profile", help="profile for every session of this server")
    ap.add_argument("--model", help="model pattern (provider/id)")
    ap.add_argument("--print-config", action="store_true", help="print the agent_servers snippet for Zed's settings.json")
    try:
        a = ap.parse_args(argv)
    except SystemExit as e:
        return int(e.code or 0)
    if a.print_config:
        print(zed_config(a.profile, a.model))
        return 0

    def factory(cwd: str, session_path: Optional[str] = None) -> Any:
        return _call_factory(make_session, cwd, session_path, {"profile": a.profile, "model": a.model, "ui": AcpUI()})

    return serve(factory)
