"""The `codemode` tool (Pi docs/codemode.md): the model writes JavaScript that calls other tools.

Scripts run in a Node `vm` context (codemode_runner.mjs) with only the codemode globals. Nested tool calls go
through the session's tool pipeline (validation, permission gate, tool_call / tool_result hooks), run in parallel
when the script awaits them together, and only the script's output reaches the model.
"""
from __future__ import annotations

import base64
import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .tools.base import ToolDef, ToolError
from . import toolsearch

CODEMODE_TOOL_NAME = "codemode"
STORE_ENTRY_TYPE = "codemode-store"
DEFAULT_INLINE_BUDGET = 3000
DEFAULT_MAX_OUTPUT_TOKENS = 10000
CHARS_PER_TOKEN = 4
RUNNER = Path(__file__).with_name("codemode_runner.mjs")
DOCS_PATH = Path(__file__).resolve().parents[2] / "docs" / "codemode.md"

DESCRIPTION_INTRO = (
    "Run JavaScript that calls other tools. The input is raw JavaScript (not JSON, no code fence), run as an async function body "
    "in a sandbox: top-level `await` and `return` work. No Node, file system, network, or timers.\n"
    "- `await tools.<name>({ ...args })` resolves to a string, or an object if the tool's declaration says so, and rejects with an "
    "Error on failure. A tool whose declaration returns an object resolves on failure too when it returned that object; check its "
    "fields. Calls still running when the script ends are cancelled.\n"
    "- Optional first line: `// @options: {\"max_output_tokens\": 10000, \"timeout_ms\": 60000}`")

SNIPPET = "Run JavaScript that calls other tools"
GUIDELINES = ["Use codemode to batch independent tool calls (Promise.allSettled), chain them, or filter large output, instead of many separate calls."]

BASH_OUTPUT_SCHEMA = {"type": "object", "properties": {
    "output": {"type": "string"}, "truncated": {"type": "boolean"}, "full_output_path": {"type": "string"},
    "exit_code": {"type": ["number", "null"]}, "wall_time_seconds": {"type": "number"}},
    "required": ["output", "truncated", "exit_code", "wall_time_seconds"]}
MCP_OUTPUT_SCHEMA = {"type": "object", "properties": {"content": {"type": "array"}, "structuredContent": {"type": "object"},
                                                      "isError": {"type": "boolean"}}, "required": ["content"]}


def describe_globals(models: bool) -> str:
    lines = [
        "Globals:",
        "- `text(value)`, `image(dataUrlOrImageBlock)`, `console.log(...)`, and top-level `return` add output; `exit()` ends the script. "
        "With several text items, each starts with a `==> text N/M <==` line, and `console` lines follow the other output in one "
        "`<console_output>` block. `image()` also saves the image to a temp file and the result names its path.",
        "- `store(key, value)` and `load(key)` keep JSON values across codemode calls.",
        "- `ALL_TOOLS`, `await searchTools(query, { limit?, namespace? })`, `await describeTool(name)`, `await describeNamespace(name)`: "
        "find unlisted tools, such as MCP tools.",
    ]
    if models:
        lines.append(f"- `models`: classifiers and image generation. Read {DOCS_PATH} first.")
    return "\n".join(lines)


def ident(name: str) -> str:
    s = re.sub(r"[^A-Za-z0-9_$]", "_", name)
    return "_" + s if s and s[0].isdigit() else s


# ---------------------------------------------------------------------------------------------- TS rendering
def ts_type(schema: Any, root: Any = None, depth: int = 0, indent: str = "") -> str:
    root = root if root is not None else schema
    if not isinstance(schema, dict) or depth > 6:
        return "unknown"
    if "$ref" in schema and isinstance(schema["$ref"], str) and schema["$ref"].startswith("#/"):
        tgt: Any = root
        for part in schema["$ref"][2:].split("/"):
            tgt = tgt.get(part) if isinstance(tgt, dict) else None
        return ts_type(tgt, root, depth + 1, indent) if tgt is not None else "unknown"
    if "const" in schema:
        return json.dumps(schema["const"])
    if "enum" in schema and isinstance(schema["enum"], list):
        return " | ".join(json.dumps(v) for v in schema["enum"]) or "never"
    for k in ("anyOf", "oneOf"):
        if isinstance(schema.get(k), list):
            return " | ".join(ts_type(v, root, depth + 1, indent) for v in schema[k]) or "unknown"
    t = schema.get("type")
    if isinstance(t, list):
        return " | ".join(ts_type({**schema, "type": x}, root, depth + 1, indent) for x in t)
    if t == "string":
        return "string"
    if t in ("number", "integer"):
        return "number"
    if t == "boolean":
        return "boolean"
    if t == "null":
        return "null"
    if t == "array":
        inner = ts_type(schema.get("items"), root, depth + 1, indent)
        return f"({inner})[]" if "|" in inner else f"{inner}[]"
    if t == "object" or "properties" in schema:
        props = schema.get("properties") or {}
        if not props:
            return "Record<string, unknown>"
        req = set(schema.get("required") or [])
        inner_indent = indent + "  "
        lines = ["{"]
        for name, p in props.items():
            desc = p.get("description") if isinstance(p, dict) else None
            if desc:
                lines.append(f"{inner_indent}/** {' '.join(str(desc).split())} */")
            key = name if re.fullmatch(r"[A-Za-z_$][A-Za-z0-9_$]*", name) else json.dumps(name)
            lines.append(f"{inner_indent}{key}{'' if name in req else '?'}: {ts_type(p, root, depth + 1, inner_indent)};")
        lines.append(indent + "}")
        return "\n".join(lines)
    return "unknown"


def output_schema(tool: ToolDef) -> Optional[Dict[str, Any]]:
    if tool.name == "bash":
        return BASH_OUTPUT_SCHEMA
    if tool.__dict__.get("mcp"):
        return MCP_OUTPUT_SCHEMA
    return tool.__dict__.get("output_schema")


def describe_output(tool: ToolDef) -> str:
    s = output_schema(tool)
    if not s:
        return "a string"
    props = s.get("properties") or {}
    req = set(s.get("required") or [])
    return "`{ " + ", ".join(f"{k}{'' if k in req else '?'}" for k in props) + " }`"


def declaration(tool: ToolDef, guidelines: Optional[List[str]] = None) -> str:
    desc = (tool.description or "").strip()
    bullets = [f"- {g.strip()}" for g in (guidelines if guidelines is not None else tool.prompt_guidelines) if g.strip()]
    if bullets:
        desc += "\n\n" + "\n".join(bullets)
    out = output_schema(tool)
    ret = ts_type(out) if out else "string"
    doc = "\n".join(" * " + l if l else " *" for l in desc.splitlines())
    return f"/**\n{doc}\n */\ndeclare function {ident(tool.name)}(args: {ts_type(tool.parameters)}): Promise<{ret}>;"


def section(tool: ToolDef) -> str:
    i = ident(tool.name)
    head = f"### `{i}`" if i == tool.name else f"### `{i}` (`{tool.name}`)"
    return f"{head}\n```ts\n{declaration(tool)}\n```"


def describe_script_call(tool: ToolDef) -> str:
    return f"{(tool.description or '').strip()}\n\nCodemode: `tools.{ident(tool.name)}(args)` resolves to {describe_output(tool)}."


def build_description(listed: List[Tuple[ToolDef, str, Optional[Dict[str, Any]]]], models: bool, budget: Optional[int]) -> str:
    """listed: (tool, exposure, namespace). Deferred tools are callable but never listed."""
    sections = [DESCRIPTION_INTRO, describe_globals(models)]
    # deferred tools - which includes MCP tools with the default codemode exposure - are never listed, so the
    # description stays the same while servers connect; scripts find them with searchTools() / ALL_TOOLS
    decls = [(t, ns) for t, exp, ns in listed if exp != "deferred" and not (t.__dict__.get("mcp") and exp == "codemode")]
    if not decls:
        return "\n\n".join(sections)
    groups: Dict[str, Dict[str, Any]] = {"": {"ns": None, "entries": []}}
    for t, ns in decls:
        key = f"ns:{ns['name']}" if ns else ""
        g = groups.setdefault(key, {"ns": ns, "entries": []})
        sec = section(t)
        g["entries"].append({"name": t.name, "section": sec, "cost": -(-len(sec) // CHARS_PER_TOKEN)})
    ordered = sorted(groups.values(), key=lambda g: (g["ns"] is not None, (g["ns"] or {}).get("name", "")))
    shown: set = set()
    if budget is None:
        shown = {e["name"] for g in ordered for e in g["entries"]}
    else:
        queues = [sorted(g["entries"], key=lambda e: e["cost"]) for g in ordered]
        remaining = budget
        active = [q for q in queues if q]
        while active:
            nxt = []
            for q in active:
                e = q[0]
                if e["cost"] > remaining:
                    continue
                remaining -= e["cost"]
                shown.add(e["name"])
                q.pop(0)
                if q:
                    nxt.append(q)
            active = nxt
    parts = ["Nested tools:"]
    for g in ordered:
        visible = [e for e in g["entries"] if e["name"] in shown]
        if g["ns"]:
            ns = g["ns"]
            listing = "" if len(visible) == len(g["entries"]) else (" (tools not listed)" if not visible else " (some tools not listed)")
            d = (ns.get("description") or "").strip()
            parts.append(f"## {ns['name']}{listing}" + (f"\n{d}" if d else ""))
        elif not g["entries"]:
            continue
        parts.extend(e["section"] for e in visible)
    sections.append("\n\n".join(parts))
    return "\n\n".join(sections)


# --------------------------------------------------------------------------------------------- result format
def _limit_output(text: str, max_tokens: int) -> Tuple[str, Optional[str]]:
    limit = max_tokens * CHARS_PER_TOKEN
    if len(text) <= limit:
        return text, None
    fd, path = tempfile.mkstemp(prefix="pi-codemode-", suffix=".txt")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text)
    half = limit // 2
    return f"{text[:half]}\n\n... {len(text) - limit} characters omitted; full output: {path} ...\n\n{text[-half:]}", path


def format_result(done: Dict[str, Any], wall: float, max_tokens: int) -> Tuple[List[Dict[str, Any]], Optional[str]]:
    ok = bool(done.get("ok"))
    items = done.get("items") or []
    texts = [i for i in items if i.get("type") == "text"]
    parts: List[Dict[str, Any]] = []
    buf: List[str] = [f"{'Script completed' if ok else 'Script failed'}\nWall time {wall:.1f} seconds\nOutput:\n"]
    n = 0
    for it in items:
        if it.get("type") == "text":
            n += 1
            buf.append((f"==> text {n}/{len(texts)} <==\n" if len(texts) > 1 else "") + it["text"] + "\n")
        elif it.get("type") == "image":
            ext = {"image/png": ".png", "image/jpeg": ".jpg", "image/gif": ".gif", "image/webp": ".webp"}.get(it["mimeType"], ".img")
            fd, path = tempfile.mkstemp(prefix="pi-codemode-image-", suffix=ext)
            with os.fdopen(fd, "wb") as f:
                f.write(base64.b64decode(it["data"]))
            buf.append(f"Image saved to {path}\n")
            parts.append({"type": "__text", "text": "".join(buf)})
            buf = []
            parts.append({"type": "image", "data": it["data"], "mimeType": it["mimeType"]})
    if done.get("console"):
        buf.append("<console_output>\n" + "\n".join(done["console"]) + "\n</console_output>\n")
    if not ok and done.get("error"):
        buf.append(f"Script error:\n{done['error']}\n")
    if buf:
        parts.append({"type": "__text", "text": "".join(buf)})
    full_text = "".join(p["text"] for p in parts if p["type"] == "__text")
    _, full_path = _limit_output(full_text, max_tokens)
    out: List[Dict[str, Any]] = []
    budget = max_tokens * CHARS_PER_TOKEN
    total = len(full_text)
    for p in parts:
        if p["type"] == "__text":
            t = p["text"].rstrip("\n")
            if total > budget:
                t, _ = _limit_output(t, max(1, int(max_tokens * len(t) / max(total, 1))))
            out.append({"type": "text", "text": t})
        else:
            out.append(p)
    return out, full_path


# ------------------------------------------------------------------------------------------------- the tool
OPTIONS_RE = re.compile(r"^\s*//\s*@options:\s*(\{.*\})\s*$")


def parse_options(code: str) -> Tuple[Dict[str, Any], str]:
    lines = code.split("\n")
    opts: Dict[str, Any] = {}
    if lines and OPTIONS_RE.match(lines[0]):
        try:
            opts = json.loads(OPTIONS_RE.match(lines[0]).group(1))  # type: ignore[union-attr]
        except json.JSONDecodeError as e:
            raise ToolError(f"Invalid @options line: {e}")
        for k in opts:
            if k not in ("max_output_tokens", "timeout_ms"):
                raise ToolError(f"Unknown option '{k}' in @options line")
    for l in lines[1:]:
        if re.match(r"^\s*//\s*@options:", l):
            raise ToolError("The // @options: line must be the first line of the script")
    return opts, code


def strip_fence(code: str) -> str:
    m = re.match(r"^\s*```(?:js|javascript|ts|typescript)?\s*\n(.*)\n```\s*$", code, re.S)
    return m.group(1) if m else code


class Codemode:
    def __init__(self, session: Any):
        self.s = session
        self.tool = ToolDef(name=CODEMODE_TOOL_NAME, label=CODEMODE_TOOL_NAME,
                            description=build_description([], False, DEFAULT_INLINE_BUDGET),
                            parameters={"type": "object", "properties": {"code": {"type": "string", "description": "Raw JavaScript source."}},
                                        "required": ["code"]},
                            execute=self.execute, prompt_snippet=SNIPPET, prompt_guidelines=list(GUIDELINES),
                            prepare_arguments=self._prepare)
        self.tool.__dict__["exposure"] = "model-only"
        self.tool.__dict__["codemode"] = self

    @staticmethod
    def _prepare(args: Any) -> Any:
        if isinstance(args, str):
            return {"code": args}
        return args

    def settings(self) -> Dict[str, Any]:
        return self.s.settings.get("codemode") or {}

    def mode(self) -> str:
        return "only" if self.settings().get("mode") == "only" else "on"

    def budget(self) -> int:
        b = self.settings().get("inlineBudget")
        return int(b) if isinstance(b, (int, float)) and b >= 0 else DEFAULT_INLINE_BUDGET

    def callable(self) -> Dict[str, Tuple[ToolDef, str, Optional[Dict[str, Any]]]]:
        return self.s.codemode_callable()

    def refresh_description(self) -> None:
        call = self.callable()
        mode = self.mode()
        listed = [v for v in call.values() if mode == "only" or v[1] != "direct"]
        self.tool.description = build_description(listed, False, self.budget())

    # ------------------------------------------------------------------------------------------ execute
    def execute(self, tool_call_id, params, signal=None, on_update=None, ctx=None):
        code = strip_fence(str((params or {}).get("code") or ""))
        if not code.strip():
            raise ToolError("code must not be empty")
        opts, code = parse_options(code)
        max_tokens = int(opts.get("max_output_tokens") or DEFAULT_MAX_OUTPUT_TOKENS)
        timeout_ms = opts.get("timeout_ms")
        node = shutil.which("node")
        if not node:
            raise ToolError("codemode needs Node.js (node was not found on PATH)")
        mcp = self.s.mcp
        if mcp:
            if re.search(r"\b(searchTools|describeNamespace|describeTool|ALL_TOOLS)\b", code):
                mcp.wait_all(30)
            else:
                for name, srv in mcp.servers.items():
                    if f"mcp__{re.sub(r'[^A-Za-z0-9_]', '_', name)}" in code:
                        srv.connected.wait(30)
        call = self.callable()
        by_id = {ident(n): n for n in call}
        store = self.s.codemode_store()
        init = {"type": "init", "code": code, "store": store,
                "tools": [{"id": i, "name": n} for i, n in by_id.items()],
                "allTools": [{"name": n, "description": (t.description or "").strip()} for n, (t, _e, _ns) in call.items()]}
        started = time.time()
        deadline = started + timeout_ms / 1000 if timeout_ms else None
        proc = subprocess.Popen([node, "--no-warnings", str(RUNNER)], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True, encoding="utf-8", bufsize=1)
        wlock = threading.Lock()
        calls: List[Dict[str, Any]] = []
        done: Dict[str, Any] = {}

        def reply(mid: int, ok: bool, value: Any = None, error: Optional[str] = None) -> None:
            try:
                with wlock:
                    proc.stdin.write(json.dumps({"id": mid, "ok": ok, "value": value, "error": error}) + "\n")  # type: ignore[union-attr]
                    proc.stdin.flush()  # type: ignore[union-attr]
            except (BrokenPipeError, OSError, ValueError):
                pass

        def handle(m: Dict[str, Any]) -> None:
            t = m.get("type")
            try:
                if t == "call":
                    n = len(calls) + 1
                    rec = {"id": f"{tool_call_id}/{n}", "name": m["name"], "args": json.dumps(m.get("args"))[:200], "status": "running"}
                    calls.append(rec)
                    t0 = time.time()
                    if on_update:
                        on_update({"content": [{"type": "text", "text": ""}], "details": {"calls": calls}})
                    ok, value, err = self.s.execute_nested(rec["id"], m["name"], m.get("args") or {}, signal, script_value=True)
                    rec.update(status="ok" if ok else "error", durationMs=int((time.time() - t0) * 1000))
                    if err:
                        rec["error"] = err[:300]
                    reply(m["id"], ok, value, err)
                elif t == "search":
                    docs = []
                    for n, (td, _e, ns) in call.items():
                        if m.get("namespace") and (ns or {}).get("name") != m["namespace"]:
                            continue
                        docs.append(toolsearch.document(td, ns))
                    found = toolsearch.rank(m["query"], docs, int(m.get("limit") or 8))
                    reply(m["id"], True, [{"name": f["name"], "description": (call[f["name"]][0].description or "").strip()} for f in found])
                elif t == "describe":
                    hit = call.get(m["name"]) or call.get(by_id.get(m["name"], ""))
                    reply(m["id"], True, {"name": hit[0].name, "description": (hit[0].description or "").strip(),
                                          "declaration": declaration(hit[0])} if hit else None)
                elif t == "namespace":
                    tools = [n for n, (_t, _e, ns) in call.items() if ns and ns["name"] == m["name"]]
                    ns = next((ns for _t, _e, ns in call.values() if ns and ns["name"] == m["name"]), None)
                    reply(m["id"], True, {"name": ns["name"], "description": ns.get("description") or None,
                                          "instructions": ns.get("instructions") or None, "tools": tools} if ns else None)
                elif t == "models":
                    reply(m["id"], True, self._models(m.get("op"), m.get("args") or []))
            except Exception as e:  # a failing call rejects in the script
                reply(m["id"], False, None, str(e))

        threads: List[threading.Thread] = []
        try:
            with wlock:
                proc.stdin.write(json.dumps(init) + "\n")  # type: ignore[union-attr]
                proc.stdin.flush()  # type: ignore[union-attr]
            reader_q: "list" = []
            ev = threading.Event()

            def reader():
                for line in proc.stdout:  # type: ignore[union-attr]
                    try:
                        reader_q.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue
                    ev.set()
                reader_q.append({"type": "__eof"})
                ev.set()

            threading.Thread(target=reader, daemon=True).start()
            while True:
                ev.wait(0.1)
                ev.clear()
                if signal is not None and getattr(signal, "aborted", False):
                    done = {"ok": False, "items": [], "console": [], "error": "Script aborted"}
                    break
                if deadline and time.time() > deadline:
                    done = {"ok": False, "items": [], "console": [], "error": f"Script timed out after {timeout_ms} ms"}
                    break
                stop = False
                while reader_q:
                    m = reader_q.pop(0)
                    if m.get("type") == "done":
                        done = m
                        stop = True
                        break
                    if m.get("type") == "__eof":
                        err = (proc.stderr.read() or "").strip()[-2000:] if proc.stderr else ""  # type: ignore[union-attr]
                        done = {"ok": False, "items": [], "console": [], "error": err or "Script runner exited"}
                        stop = True
                        break
                    th = threading.Thread(target=handle, args=(m,), daemon=True)
                    th.start()
                    threads.append(th)
                if stop:
                    break
        finally:
            for c in calls:
                if c["status"] == "running":
                    c["status"] = "cancelled"
            try:
                proc.stdin.close()  # type: ignore[union-attr]
            except Exception:
                pass
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                proc.kill()
            for f in (proc.stdout, proc.stderr):
                try:
                    f.close()  # type: ignore[union-attr]
                except Exception:
                    pass
        if done.get("ok"):
            st = done.get("store") or {}
            if st.get("set") or st.get("delete"):
                self.s.sessions.append_custom(STORE_ENTRY_TYPE, {"set": st.get("set") or {}, "delete": st.get("delete") or []})
        content, full = format_result(done, time.time() - started, max_tokens)
        details: Dict[str, Any] = {"calls": calls}
        if full:
            details["fullOutputPath"] = full
        r = {"content": content, "details": details}
        if not done.get("ok"):
            r["isError"] = True
        return r

    def _models(self, op: str, args: List[Any]) -> Any:
        reg = self.s.registry
        typ = args[0] if args else "chat"
        if op in ("getModelsOfType", "getAvailableOfType"):
            out = [m for m in reg.all() if (m.get("type") or "chat") == typ and (len(args) < 2 or not args[1] or m["provider"] == args[1])]
            if op == "getAvailableOfType":
                out = [m for m in out if self.s.auth.has_auth(m["provider"])]
            return [{k: v for k, v in m.items() if not k.startswith("_")} for m in out]
        if op == "getModelOfType":
            m = reg.find(args[1], args[2])
            return {k: v for k, v in m.items() if not k.startswith("_")} if m and (m.get("type") or "chat") == typ else None
        if op in ("classify", "generateImages"):
            model = args[0] or {}
            return {"provider": model.get("provider"), "model": model.get("id"), "stopReason": "error",
                    "errorMessage": f"{op} is not supported for {model.get('provider')}/{model.get('id')} in this build",
                    "answers": {} if op == "classify" else None, "output": [] if op == "generateImages" else None}
        raise ToolError(f"unknown models operation {op}")
