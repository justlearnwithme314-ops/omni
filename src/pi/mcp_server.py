"""`omni mcp serve`: Omni as a Model Context Protocol server, so other agents and editors can use it as a tool.

Hermes has `mcp serve` and OpenClaw ships an MCP server; this is the same idea. Claude Desktop, Cursor, another
harness - anything that speaks MCP over stdio - starts Omni as a child process and gets:

    omni_agent(prompt, new_session?)   run a full agent turn (its own tools, profile and model) and return the answer.
                                       The conversation continues from call to call; `new_session: true` starts over.
    omni_status()                      model, profile, cwd, enabled capabilities and context usage, as JSON.

With `--expose agent,tools` the session's own active tools are offered as well, each under its name with an `omni_`
prefix (`omni_read`, `omni_bash`, `omni_edit`, `omni_write`, and whatever the profile's capabilities add) and with
its real JSON schema. They run through the same gates as inside a conversation: plan mode keeps the session
read-only and the permission gate still refuses what it would refuse. A refused or failed call comes back as a
tool result with `isError: true`, never as a protocol error.

    omni mcp serve [--expose agent,tools] [--profile NAME] [--model PATTERN] [--cwd DIR]
    omni mcp serve --print-config      the snippet to paste into the client's MCP configuration

Transport: newline-delimited JSON-RPC 2.0 on stdin/stdout, UTF-8, one message per line. Nothing else is ever
written to stdout; diagnostics and anything a tool or the session prints go to stderr. Tool calls run one at a time
on a worker (a second call waits for the first), while `ping`, `tools/list` and `omni_status` answer immediately.
`notifications/cancelled` aborts the running turn; a cancelled request gets no response, as the protocol asks.
There is no terminal, so dialogs resolve to "cancel": a tool that needs to ask the user gets no answer.
"""
from __future__ import annotations

import json
import os
import queue
import re
import sys
import threading
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from .mcp import PROTOCOL_VERSION
from .tools.base import ToolContext, ToolError

SUPPORTED_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")
SERVER_NAME = "omni"
EXPOSE = ("agent", "tools")
PREFIX = "omni_"
PARSE_ERROR, INVALID_REQUEST, METHOD_NOT_FOUND, INVALID_PARAMS, INTERNAL_ERROR = -32700, -32600, -32601, -32602, -32603

AGENT_TOOL = {
    "name": "omni_agent",
    "description": ("Give a task or question to the Omni coding agent. It works in its own project folder with its own "
                    "tools (reading and editing files, running commands, and more, depending on its profile), runs until "
                    "it is done and returns its final answer. Calls continue the same conversation; pass new_session: "
                    "true to start a fresh one."),
    "inputSchema": {"type": "object", "properties": {
        "prompt": {"type": "string", "description": "The task or question, with all the context the agent needs."},
        "new_session": {"type": "boolean", "description": "Start a fresh conversation before this prompt (default false)."}},
        "required": ["prompt"]},
}
STATUS_TOOL = {
    "name": "omni_status",
    "description": "Omni's current model, profile, working directory, enabled capabilities and context usage, as JSON.",
    "inputSchema": {"type": "object", "properties": {}},
    "annotations": {"readOnlyHint": True},
}


def _log(text: str) -> None:
    try:
        sys.stderr.write(f"[omni mcp] {text}\n")
        sys.stderr.flush()
    except Exception:
        pass


def _version() -> str:
    try:
        from . import PI_COMPAT_VERSION
        return str(PI_COMPAT_VERSION)
    except Exception:
        return "0"


def default_session_factory(opts: Any) -> Any:
    """A started headless session, as print and rpc modes create it."""
    from .agent import AgentSession
    from .modes import HeadlessUI
    s = AgentSession(opts, ui=HeadlessUI())
    s.start()
    for d in s.diagnostics:
        _log(f"warning: {d}")
    return s


def mcp_content(blocks: Any) -> List[Dict[str, Any]]:
    """Omni tool-result content -> MCP content. Text passes through; images need base64 data and a MIME type."""
    out: List[Dict[str, Any]] = []
    if isinstance(blocks, str):
        blocks = [{"type": "text", "text": blocks}]
    for b in blocks or []:
        if not isinstance(b, dict):
            out.append({"type": "text", "text": str(b)})
            continue
        t = b.get("type")
        if t == "text":
            out.append({"type": "text", "text": str(b.get("text", ""))})
        elif t == "image":
            src = b.get("source") if isinstance(b.get("source"), dict) else {}
            data = b.get("data") or src.get("data")
            mime = b.get("mimeType") or b.get("mime_type") or src.get("media_type") or src.get("mimeType")
            if isinstance(data, str) and data and isinstance(mime, str) and mime:
                out.append({"type": "image", "data": data, "mimeType": mime})
            else:
                out.append({"type": "text", "text": "[image omitted: no inline data to pass on]"})
        else:
            out.append({"type": "text", "text": f"[{t or 'unknown'} content omitted]"})
    return out or [{"type": "text", "text": ""}]


def _result(text: str, is_error: bool = False) -> Dict[str, Any]:
    r: Dict[str, Any] = {"content": [{"type": "text", "text": text}]}
    if is_error:
        r["isError"] = True
    return r


class _Call:
    def __init__(self, rid: Any, name: str, args: Dict[str, Any]):
        self.id, self.name, self.args = rid, name, args
        self.cancelled = threading.Event()
        self.signal: Any = None  # AbortSignal of a direct tool call


class McpServer:
    """One stdio connection. `run()` reads requests until EOF; tool calls go through a single worker thread."""

    def __init__(self, opts: Any, stdin: Any, stdout: Any, expose: Sequence[str] = ("agent",),
                 session_factory: Optional[Callable[[Any], Any]] = None):
        self.opts = opts
        self.expose = tuple(expose) or ("agent",)
        self.factory = session_factory or default_session_factory
        self._in = getattr(stdin, "buffer", stdin)
        self._out = getattr(stdout, "buffer", stdout)
        self._write_lock = threading.Lock()
        self._session_lock = threading.Lock()
        self._state_lock = threading.Lock()
        self.session: Any = None
        self._queue: "queue.Queue[Optional[_Call]]" = queue.Queue()
        self._calls: Dict[Any, _Call] = {}      # queued or running, by JSON-RPC id
        self._running: Optional[_Call] = None
        self._worker: Optional[threading.Thread] = None
        self._dead = False                       # stdout is gone: stop working for nobody
        self.initialized = False

    # ---------------------------------------------------------------------------------- transport
    def _send(self, msg: Dict[str, Any]) -> None:
        line = json.dumps(msg, ensure_ascii=True, default=str) + "\n"  # ASCII: safe whatever the pipe's encoding
        with self._write_lock:
            if self._dead:
                return
            try:
                try:
                    self._out.write(line.encode("ascii"))
                except TypeError:  # a text stream
                    self._out.write(line)
                self._out.flush()
            except Exception as e:
                self._dead = True
                _log(f"cannot write to the client ({type(e).__name__}: {e}); stopping")
        if self._dead:
            self._abort_running()

    def _reply(self, rid: Any, result: Any) -> None:
        self._send({"jsonrpc": "2.0", "id": rid, "result": result})

    def _error(self, rid: Any, code: int, message: str) -> None:
        self._send({"jsonrpc": "2.0", "id": rid, "error": {"code": code, "message": message}})

    def run(self) -> int:
        try:
            while not self._dead:
                try:
                    raw = self._in.readline()
                except (OSError, ValueError):
                    break
                if not raw:
                    break
                if isinstance(raw, bytes):
                    try:
                        line = raw.decode("utf-8")
                    except UnicodeDecodeError:
                        self._error(None, PARSE_ERROR, "Parse error: the line is not valid UTF-8")
                        continue
                else:
                    line = raw
                line = line.lstrip("﻿").strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                except ValueError as e:
                    self._error(None, PARSE_ERROR, f"Parse error: {e}")
                    continue
                if isinstance(msg, list):  # JSON-RPC batch (allowed by protocol 2025-03-26)
                    if not msg:
                        self._error(None, INVALID_REQUEST, "Invalid request: empty batch")
                    for m in msg:
                        self._dispatch(m)
                else:
                    self._dispatch(msg)
        finally:
            self._close()
        return 0

    def _close(self) -> None:
        """EOF: calls already accepted finish (unless the client is gone), then the session shuts down."""
        if self._dead:
            self._abort_running()
        self._queue.put(None)
        w = self._worker
        if w is not None:
            w.join()
        s, self.session = self.session, None
        if s is not None:
            try:
                s.shutdown("quit")
            except Exception as e:
                _log(f"shutdown: {type(e).__name__}: {e}")

    # ---------------------------------------------------------------------------------- dispatch
    def _dispatch(self, msg: Any) -> None:
        if not isinstance(msg, dict):
            self._error(None, INVALID_REQUEST, "Invalid request: expected a JSON object")
            return
        method = msg.get("method")
        has_id = "id" in msg and msg["id"] is not None
        rid = msg.get("id")
        if method is None:
            if has_id and ("result" in msg or "error" in msg):
                return  # a response to something we never asked: ignore
            self._error(rid, INVALID_REQUEST, "Invalid request: no method")
            return
        if not isinstance(method, str):
            self._error(rid, INVALID_REQUEST, "Invalid request: method must be a string")
            return
        params = msg.get("params")
        if params is None:
            params = {}
        if not isinstance(params, dict):
            if has_id:
                self._error(rid, INVALID_PARAMS, "Invalid params: expected an object")
            return
        if not has_id:  # notification: never answered
            if method == "notifications/initialized":
                self.initialized = True
            elif method == "notifications/cancelled":
                self._cancel(params.get("requestId"))
            return
        try:
            if method == "initialize":
                self._reply(rid, self._initialize(params))
            elif method == "ping":
                self._reply(rid, {})
            elif method == "tools/list":
                self._reply(rid, {"tools": self.list_tools()})
            elif method == "tools/call":
                self._tools_call(rid, params)
            else:
                self._error(rid, METHOD_NOT_FOUND, f"Method not found: {method}")
        except Exception as e:
            _log(f"{method}: {type(e).__name__}: {e}")
            self._error(rid, INTERNAL_ERROR, f"{type(e).__name__}: {e}")

    def _initialize(self, params: Dict[str, Any]) -> Dict[str, Any]:
        asked = params.get("protocolVersion")
        version = asked if asked in SUPPORTED_VERSIONS else PROTOCOL_VERSION
        extra = " Its own tools are also available directly, prefixed omni_." if "tools" in self.expose else ""
        return {"protocolVersion": version, "capabilities": {"tools": {}},
                "serverInfo": {"name": SERVER_NAME, "title": "Omni", "version": _version()},
                "instructions": "Omni is a coding agent. omni_agent runs a task in its project folder and returns the "
                                "answer; the conversation continues between calls." + extra}

    # ---------------------------------------------------------------------------------- session
    def get_session(self) -> Any:
        with self._session_lock:
            if self.session is None:
                self.session = self.factory(self.opts)
            return self.session

    def _session_tools(self) -> Dict[str, Any]:
        """exposed name -> ToolDef, for the session's active tools."""
        s = self.get_session()
        out: Dict[str, Any] = {}
        for t in s.active_tools():
            name = PREFIX + re.sub(r"[^A-Za-z0-9_-]", "_", t.name)
            if name not in (AGENT_TOOL["name"], STATUS_TOOL["name"]) and name not in out:
                out[name] = t
        return out

    def list_tools(self) -> List[Dict[str, Any]]:
        tools: List[Dict[str, Any]] = []
        if "agent" in self.expose:
            tools += [AGENT_TOOL, STATUS_TOOL]
        if "tools" in self.expose:
            for name, t in self._session_tools().items():
                schema = dict(t.parameters) if isinstance(t.parameters, dict) else {}
                schema.setdefault("type", "object")
                schema.setdefault("properties", {})
                d: Dict[str, Any] = {"name": name, "description": t.description or t.label or t.name, "inputSchema": schema}
                if t.read_only:
                    d["annotations"] = {"readOnlyHint": True}
                tools.append(d)
        return tools

    def status(self) -> Dict[str, Any]:
        s = self.get_session()
        m = s.model or {}
        try:
            usage = s.context_usage()
        except Exception:
            usage = None
        return {"model": f"{m.get('provider')}/{m.get('id')}" if m else None, "profile": (s.profile or {}).get("name"),
                "cwd": s.cwd, "capabilities": list(s.caps.enabled), "tools": list(s.active_tool_names),
                "contextUsage": usage, "busy": bool(s.is_streaming), "sessionId": s.sessions.session_id,
                "expose": list(self.expose)}

    # ---------------------------------------------------------------------------------- tools/call
    def _tools_call(self, rid: Any, params: Dict[str, Any]) -> None:
        name = params.get("name")
        args = params.get("arguments")
        if args is None:
            args = {}
        if not isinstance(name, str) or not name:
            self._error(rid, INVALID_PARAMS, "Invalid params: `name` (string) is required")
            return
        if not isinstance(args, dict):
            self._error(rid, INVALID_PARAMS, "Invalid params: `arguments` must be an object")
            return
        fixed = name in (AGENT_TOOL["name"], STATUS_TOOL["name"]) and "agent" in self.expose
        if not fixed and not ("tools" in self.expose and name.startswith(PREFIX)):
            self._error(rid, INVALID_PARAMS, f"Unknown tool: {name}")
            return
        if name == STATUS_TOOL["name"] and fixed and self.session is not None:
            self._reply(rid, _result(json.dumps(self.status(), indent=1, default=str)))  # answers during a running turn
            return
        call = _Call(rid, name, args)
        with self._state_lock:
            self._calls[rid] = call
            if self._worker is None:
                self._worker = threading.Thread(target=self._work, name="omni-mcp-worker", daemon=True)
                self._worker.start()
        self._queue.put(call)

    def _work(self) -> None:
        while True:
            call = self._queue.get()
            if call is None:
                return
            with self._state_lock:
                self._running = call
            answer: Tuple[str, Any] = ("result", _result("", True))
            try:
                if not call.cancelled.is_set() and not self._dead:
                    answer = self._execute(call)
            except Exception as e:  # never let one call take the worker down
                _log(f"{call.name}: {type(e).__name__}: {e}")
                answer = ("result", _result(f"{type(e).__name__}: {e}", True))
            finally:
                with self._state_lock:
                    self._running = None
                    self._calls.pop(call.id, None)
            if call.cancelled.is_set():
                continue  # a cancelled request is not answered
            if answer[0] == "error":
                self._error(call.id, answer[1][0], answer[1][1])
            else:
                self._reply(call.id, answer[1])

    def _execute(self, call: _Call) -> Tuple[str, Any]:
        try:
            s = self.get_session()
        except Exception as e:
            return "result", _result(f"Omni could not start a session: {type(e).__name__}: {e}", True)
        if call.name == STATUS_TOOL["name"] and "agent" in self.expose:
            return "result", _result(json.dumps(self.status(), indent=1, default=str))
        if call.name == AGENT_TOOL["name"] and "agent" in self.expose:
            return "result", self._run_agent(s, call)
        tool = self._session_tools().get(call.name)
        if tool is None:
            return "error", (INVALID_PARAMS, f"Unknown tool: {call.name}")
        return "result", self._run_tool(s, tool, call)

    def _run_agent(self, s: Any, call: _Call) -> Dict[str, Any]:
        prompt = call.args.get("prompt")
        if not isinstance(prompt, str) or not prompt.strip():
            return _result("omni_agent: `prompt` (a non-empty string) is required", True)
        if call.args.get("new_session"):
            if s.sessions.entries and not s.new_session():
                return _result("omni_agent: starting a new session was cancelled by an extension", True)
        before = {e.get("id") for e in s.sessions.get_branch()}
        errors: List[str] = []
        outcome: List[str] = []

        def on_event(ev: Dict[str, Any]) -> None:
            if ev.get("type") == "error":
                errors.append(str(ev.get("message")))
            elif ev.get("type") == "agent_end":
                outcome.append(str(ev.get("outcome")))
            elif ev.get("type") == "notice" and ev.get("level") in ("warning", "error"):
                errors.append(str(ev.get("message")))
            if call.cancelled.is_set():  # cancelled before the turn had a signal to abort
                s.abort()

        unsubscribe = s.subscribe(on_event)
        try:
            s.prompt(prompt, source="mcp")
        finally:
            unsubscribe()
        last = None
        text = ""
        for e in reversed(s.sessions.get_branch()):
            if e.get("id") in before:
                break
            if e.get("type") == "message" and e["message"].get("role") == "assistant":
                m = e["message"]
                last = last or m
                t = "".join(b.get("text", "") for b in m.get("content") or [] if b.get("type") == "text")
                if t.strip():
                    text = t
                    break
        if last is not None and last.get("stopReason") == "error":
            return _result(f"Omni's model request failed: {last.get('errorMessage')}", True)
        if (last is not None and last.get("stopReason") == "aborted") or outcome[-1:] == ["aborted"]:
            return _result((text + "\n\n" if text else "") + "[The turn was aborted before it finished.]", True)
        if text:
            return _result(text)
        if errors:
            return _result("\n".join(errors), True)
        return _result("(Omni finished without a text answer.)")

    def _run_tool(self, s: Any, tool: Any, call: _Call) -> Dict[str, Any]:
        """A direct tool call: the same checks a conversation applies (arguments, capability gate, permission gate)."""
        from .ai.stream import AbortSignal
        args: Any = call.args
        try:
            if tool.prepare_arguments:
                args = tool.prepare_arguments(args)
            missing = [k for k in (tool.parameters.get("required") or []) if k not in (args or {})]
            if missing:
                return _result("Validation failed for tool \"%s\":\n%s" % (tool.name, "\n".join(f"- {k}: missing" for k in missing)), True)
            why = s.caps.gate(tool.name, args)
            if not why and getattr(s, "gate", None) is not None:
                ok, reason = s.gate.check(tool.name, args)
                why = None if ok else (reason or f"{tool.name} was refused by the permission gate")
            if why:
                return _result(str(why), True)
            call.signal = AbortSignal()
            if call.cancelled.is_set():
                call.signal.abort()
            ctx = ToolContext(cwd=s.cwd, signal=call.signal, model=s.model, session=s)
            result = tool.execute(f"mcp_{call.id}", args, call.signal, None, ctx)
            if not isinstance(result, dict):
                result = {"content": [{"type": "text", "text": "" if result is None else str(result)}]}
            try:
                result = s.caps.after_tool(tool.name, args, result) or result
            except Exception as e:
                _log(f"after_tool {tool.name}: {type(e).__name__}: {e}")
        except ToolError as e:
            return _result(str(e), True)
        except Exception as e:
            return _result(f"{type(e).__name__}: {e}", True)
        out: Dict[str, Any] = {"content": mcp_content(result.get("content"))}
        if result.get("isError"):
            out["isError"] = True
        return out

    # ---------------------------------------------------------------------------------- cancellation
    def _cancel(self, rid: Any) -> None:
        with self._state_lock:
            call = self._calls.get(rid)
            running = call is not None and call is self._running
        if call is None:
            return
        call.cancelled.set()
        if running:
            self._abort(call)

    def _abort_running(self) -> None:
        with self._state_lock:
            call = self._running
            for c in self._calls.values():
                c.cancelled.set()
        if call is not None:
            self._abort(call)

    def _abort(self, call: _Call) -> None:
        try:
            if call.signal is not None:
                call.signal.abort()
            elif self.session is not None:
                self.session.abort()
        except Exception as e:
            _log(f"abort: {type(e).__name__}: {e}")


def serve(opts: Any, stdin: Any = None, stdout: Any = None, expose: Sequence[str] = ("agent",),
          session_factory: Optional[Callable[[Any], Any]] = None) -> int:
    """Serve MCP on stdin/stdout until EOF. While it runs, `sys.stdout` points at stderr, so a stray `print` from
    the session, a tool or a plugin can never corrupt the protocol stream."""
    out = stdout if stdout is not None else sys.stdout
    inp = stdin if stdin is not None else sys.stdin
    saved = sys.stdout
    sys.stdout = sys.stderr
    try:
        return McpServer(opts, inp, out, expose, session_factory).run()
    finally:
        sys.stdout = saved


def config_snippet() -> Dict[str, Any]:
    main = Path(__file__).resolve().parents[2] / "omni_main.py"
    return {"mcpServers": {"omni": {"command": sys.executable, "args": [str(main), "mcp", "serve"]}}}


def print_config(out: Any = None) -> None:
    """The JSON a client needs (Claude Desktop's claude_desktop_config.json, Cursor's mcp.json, ...)."""
    (out or sys.stdout).write(json.dumps(config_snippet(), indent=2) + "\n")


USAGE = ("usage: omni mcp serve [--expose agent,tools] [--profile NAME] [--model PATTERN] [--cwd DIR]\n"
         "       omni mcp serve --print-config\n"
         "  Runs Omni as an MCP server on stdin/stdout. --expose agent (default) offers omni_agent and omni_status;\n"
         "  adding tools also offers the session's own tools as omni_<name>.")


def cli(argv: List[str], make_opts: Callable[[Optional[str], Optional[str], str], Any],
        session_factory: Optional[Callable[[Any], Any]] = None, stdin: Any = None, stdout: Any = None) -> int:
    """`omni mcp serve ...`; `argv` is what follows `mcp` (a leading `serve` is optional).
    `make_opts(profile, model, cwd)` builds the SessionOptions; profile and model are None when not given."""
    args = list(argv[1:] if argv[:1] == ["serve"] else argv)
    expose, profile, model, cwd, show = "agent", None, None, None, False
    i = 0
    while i < len(args):
        a = args[i]
        key, eq, val = a.partition("=")
        if a in ("-h", "--help"):
            sys.stderr.write(USAGE + "\n")
            return 0
        if a == "--print-config":
            show = True
            i += 1
            continue
        if key in ("--expose", "--profile", "--model", "--cwd"):
            if not eq:
                if i + 1 >= len(args):
                    sys.stderr.write(f"omni mcp serve: {key} needs a value\n{USAGE}\n")
                    return 2
                val = args[i + 1]
                i += 1
            i += 1
            if key == "--expose":
                expose = val
            elif key == "--profile":
                profile = val
            elif key == "--model":
                model = val
            else:
                cwd = val
            continue
        sys.stderr.write(f"omni mcp serve: unknown argument {a}\n{USAGE}\n")
        return 2
    if show:
        print_config(stdout)
        return 0
    modes = tuple(dict.fromkeys(m.strip().lower() for m in expose.split(",") if m.strip()))
    bad = [m for m in modes if m not in EXPOSE]
    if bad or not modes:
        sys.stderr.write(f"omni mcp serve: --expose takes agent and/or tools, not '{expose}'\n")
        return 2
    cwd = os.path.abspath(os.path.expanduser(cwd)) if cwd else os.getcwd()
    if not os.path.isdir(cwd):
        sys.stderr.write(f"omni mcp serve: --cwd {cwd} is not a folder\n")
        return 2
    opts = make_opts(profile, model, cwd)
    return serve(opts, stdin=stdin, stdout=stdout, expose=modes, session_factory=session_factory)
