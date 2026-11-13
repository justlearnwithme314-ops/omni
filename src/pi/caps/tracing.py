"""`tracing`: observability for every run (Langfuse's model: a trace made of generations and spans).

Each user message opens a **trace**. Every model call becomes a **generation** (model, tokens in/out/cached, cost,
latency) and every tool call a **span** (arguments, result size, error flag, latency). Traces go to

    <agent-dir>/traces/<session-id>.jsonl      always (one JSON object per run)
    a Langfuse server                          when LANGFUSE_PUBLIC_KEY and LANGFUSE_SECRET_KEY are set
                                               (LANGFUSE_HOST, default https://cloud.langfuse.com), through its
                                               public ingestion API, in the background

`/trace` shows the last run step by step: where the time and the tokens went.

Prompt and output text is stored truncated (2,000 characters per field). Set `"tracing": {"content": false}` in the
profile to record only sizes, timings, tokens and cost.
"""
from __future__ import annotations

import base64
import json
import os
import threading
import time
import urllib.request
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import Capability

MAX_FIELD = 2000
DEFAULT_HOST = "https://cloud.langfuse.com"


def _iso(ts: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(ts)) + f".{int(ts * 1000) % 1000:03d}Z"


def _text(content: Any) -> str:
    if isinstance(content, str):
        return content
    return "".join(b.get("text", "") for b in content or [] if isinstance(b, dict) and b.get("type") == "text")


class Tracer:
    def __init__(self, session: Any):
        self.session = session
        opt = session.profile.get("tracing") if isinstance(session.profile.get("tracing"), dict) else {}
        self.keep_content = opt.get("content", True) is not False
        self.current: Optional[Dict[str, Any]] = None
        self.last: Optional[Dict[str, Any]] = None
        self._gen_start = 0.0
        self._tools: Dict[str, Dict[str, Any]] = {}
        self._export_error_reported = False
        self.unsubscribe = session.subscribe(self.on_event)

    def clip(self, value: Any) -> Any:
        if not self.keep_content:
            return {"chars": len(value if isinstance(value, str) else json.dumps(value, default=str))}
        s = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
        return s if len(s) <= MAX_FIELD else s[:MAX_FIELD] + f"… [{len(s) - MAX_FIELD} more chars]"

    # --- event handling
    def on_event(self, ev: Dict[str, Any]) -> None:
        t, now = ev.get("type"), time.time()
        try:
            if t == "agent_start" and self.current is None:
                s = self.session
                users = [e["message"] for e in s.sessions.get_branch() if e.get("type") == "message" and e["message"].get("role") == "user"]
                self.current = {"id": uuid.uuid4().hex, "sessionId": s.sessions.session_id, "start": now, "end": now,
                                "name": "run", "input": self.clip(_text(users[-1]["content"])) if users else "",
                                "output": "", "profile": s.profile.get("name"), "cwd": s.cwd, "observations": []}
                self._gen_start = now
            elif self.current is None:
                return
            elif t == "message_start":
                self._gen_start = now
            elif t == "message_end" and (ev.get("message") or {}).get("role") == "assistant":
                m = ev["message"]
                u = m.get("usage") or {}
                calls = [b.get("name") for b in m.get("content") or [] if b.get("type") == "toolCall"]
                self.current["observations"].append({
                    "id": uuid.uuid4().hex, "type": "generation", "name": f"{m.get('provider')}/{m.get('model')}",
                    "model": m.get("model"), "provider": m.get("provider"), "start": self._gen_start or now, "end": now,
                    "usage": {"input": u.get("input", 0), "output": u.get("output", 0), "cacheRead": u.get("cacheRead", 0),
                              "cacheWrite": u.get("cacheWrite", 0), "total": u.get("totalTokens", 0)},
                    "cost": float((u.get("cost") or {}).get("total") or 0.0), "stopReason": m.get("stopReason"),
                    "output": self.clip(_text(m.get("content"))), "toolCalls": calls,
                    "error": m.get("errorMessage")})
                self.current["output"] = self.clip(_text(m.get("content"))) or self.current["output"]
                self._gen_start = now
            elif t == "tool_execution_start":
                self._tools[str(ev.get("toolCallId"))] = {"start": now, "name": ev.get("toolName"), "args": ev.get("args")}
            elif t == "tool_execution_end":
                st = self._tools.pop(str(ev.get("toolCallId")), {"start": now, "name": ev.get("toolName"), "args": ev.get("args")})
                result = ev.get("result") or {}
                self.current["observations"].append({
                    "id": uuid.uuid4().hex, "type": "span", "name": f"tool:{st['name']}", "start": st["start"], "end": now,
                    "input": self.clip(st.get("args") or {}), "output": self.clip(_text(result.get("content"))),
                    "error": bool(ev.get("isError"))})
                self._gen_start = now
            elif t == "agent_end":
                self.finish(ev.get("outcome") or "completed")
        except Exception:
            pass  # tracing must never disturb a run

    def finish(self, outcome: str) -> None:
        tr, self.current = self.current, None
        if tr is None:
            return
        tr["end"], tr["outcome"] = time.time(), outcome
        gens = [o for o in tr["observations"] if o["type"] == "generation"]
        tr["totals"] = {"generations": len(gens), "tools": len(tr["observations"]) - len(gens),
                        "input": sum(g["usage"]["input"] + g["usage"]["cacheRead"] for g in gens),
                        "output": sum(g["usage"]["output"] for g in gens),
                        "cost": round(sum(g["cost"] for g in gens), 6), "seconds": round(tr["end"] - tr["start"], 3)}
        self.last = tr
        try:
            d = Path(self.session.agent_dir) / "traces"
            d.mkdir(parents=True, exist_ok=True)
            with open(d / f"{tr['sessionId']}.jsonl", "a", encoding="utf-8") as f:
                f.write(json.dumps(tr, ensure_ascii=False, default=str) + "\n")
        except OSError:
            pass
        if os.environ.get("LANGFUSE_PUBLIC_KEY") and os.environ.get("LANGFUSE_SECRET_KEY"):
            threading.Thread(target=self.export, args=(tr,), daemon=True, name="omni-langfuse").start()

    def score(self, value: float, comment: str = "") -> Optional[Dict[str, Any]]:
        """Attach a user rating to the last run (Langfuse scores): saved locally and exported when configured."""
        tr = self.last
        if tr is None:
            return None
        score = {"id": uuid.uuid4().hex, "type": "score", "traceId": tr["id"], "name": "user-feedback", "value": value,
                 "comment": comment, "at": time.time()}
        tr.setdefault("scores", []).append(score)
        try:
            with open(Path(self.session.agent_dir) / "traces" / f"{tr['sessionId']}.jsonl", "a", encoding="utf-8") as f:
                f.write(json.dumps(score, ensure_ascii=False) + "\n")
        except OSError:
            pass
        if os.environ.get("LANGFUSE_PUBLIC_KEY") and os.environ.get("LANGFUSE_SECRET_KEY"):
            batch = [{"id": uuid.uuid4().hex, "type": "score-create", "timestamp": _iso(score["at"]),
                      "body": {"id": score["id"], "traceId": tr["id"], "name": "user-feedback", "value": value,
                               "comment": comment or None, "dataType": "NUMERIC"}}]
            threading.Thread(target=self.post, args=(batch,), daemon=True, name="omni-langfuse").start()
        return score

    # --- Langfuse
    def langfuse_batch(self, tr: Dict[str, Any]) -> List[Dict[str, Any]]:
        def ev(kind: str, ts: float, body: Dict[str, Any]) -> Dict[str, Any]:
            return {"id": uuid.uuid4().hex, "type": kind, "timestamp": _iso(ts), "body": body}
        batch = [ev("trace-create", tr["start"], {
            "id": tr["id"], "name": tr["name"], "sessionId": tr["sessionId"], "timestamp": _iso(tr["start"]),
            "input": tr["input"], "output": tr["output"], "tags": ["omni", f"profile:{tr.get('profile')}"],
            "metadata": {"cwd": tr.get("cwd"), "outcome": tr.get("outcome"), **tr.get("totals", {})}})]
        for o in tr["observations"]:
            body = {"id": o["id"], "traceId": tr["id"], "name": o["name"], "startTime": _iso(o["start"]), "endTime": _iso(o["end"]),
                    "output": o.get("output"), "level": "ERROR" if o.get("error") else "DEFAULT"}
            if o["type"] == "generation":
                u = o["usage"]
                body.update({"model": o.get("model"), "usage": {"input": u["input"] + u["cacheRead"], "output": u["output"],
                                                                "total": u["total"], "unit": "TOKENS", "totalCost": o["cost"]},
                             "metadata": {"provider": o.get("provider"), "stopReason": o.get("stopReason"),
                                          "toolCalls": o.get("toolCalls"), "cacheRead": u["cacheRead"]},
                             "statusMessage": o.get("error") or None})
                batch.append(ev("generation-create", o["start"], body))
            else:
                body["input"] = o.get("input")
                batch.append(ev("span-create", o["start"], body))
        return batch

    def export(self, tr: Dict[str, Any]) -> bool:
        return self.post(self.langfuse_batch(tr))

    def post(self, batch: List[Dict[str, Any]]) -> bool:
        host = (os.environ.get("LANGFUSE_HOST") or os.environ.get("LANGFUSE_BASE_URL") or DEFAULT_HOST).rstrip("/")
        auth = base64.b64encode(f"{os.environ['LANGFUSE_PUBLIC_KEY']}:{os.environ['LANGFUSE_SECRET_KEY']}".encode()).decode()
        req = urllib.request.Request(host + "/api/public/ingestion", data=json.dumps({"batch": batch}).encode(),
                                     headers={"Content-Type": "application/json", "Authorization": f"Basic {auth}"}, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                r.read()
            return True
        except Exception as e:
            if not self._export_error_reported:  # say it once, then stay quiet
                self._export_error_reported = True
                self.session.diagnostics.append(f"tracing: Langfuse export failed ({type(e).__name__}: {e}); local traces are still written")
            return False


def tracer_of(session: Any) -> Optional[Tracer]:
    return getattr(session, "_tracer", None)


def _setup(session: Any) -> str:
    _unload(session)
    session._tracer = Tracer(session)
    return ""


def _unload(session: Any) -> None:
    tr = tracer_of(session)
    if tr is not None:
        tr.unsubscribe()
        session._tracer = None


def format_trace(tr: Optional[Dict[str, Any]]) -> str:
    if not tr:
        return "No run traced yet in this session."
    t = tr.get("totals", {})
    rows = [f"Last run: {t.get('seconds', 0)} s · {t.get('generations', 0)} model call(s) · {t.get('tools', 0)} tool call(s) · "
            f"{t.get('input', 0)} tokens in / {t.get('output', 0)} out · ${t.get('cost', 0):.4f} · {tr.get('outcome')}"]
    for o in tr["observations"]:
        dur = o["end"] - o["start"]
        if o["type"] == "generation":
            u = o["usage"]
            rows.append(f"  {dur:6.2f}s  model {o['name']}  in {u['input'] + u['cacheRead']} (cached {u['cacheRead']}) · out {u['output']}"
                        f" · ${o['cost']:.4f}" + (f" → {', '.join(o['toolCalls'])}" if o.get("toolCalls") else "")
                        + (f"  ERROR {o['error']}" if o.get("error") else ""))
        else:
            rows.append(f"  {dur:6.2f}s  {o['name']}" + ("  (error)" if o.get("error") else ""))
    return "\n".join(rows)


def _commands(session: Any) -> Dict[str, Any]:
    def run(_args: str) -> str:
        tr = tracer_of(session)
        where = Path(session.agent_dir) / "traces" / f"{session.sessions.session_id}.jsonl"
        export = "Langfuse export on" if os.environ.get("LANGFUSE_PUBLIC_KEY") and os.environ.get("LANGFUSE_SECRET_KEY") else \
            "Langfuse export off (set LANGFUSE_PUBLIC_KEY / LANGFUSE_SECRET_KEY)"
        return format_trace(tr.last if tr else None) + f"\n{where} · {export}"
    def rate(args: str) -> str:
        tr = tracer_of(session)
        word, _, comment = args.strip().partition(" ")
        values = {"up": 1.0, "good": 1.0, "+": 1.0, "down": 0.0, "bad": 0.0, "-": 0.0}
        try:
            value = values[word.lower()] if word.lower() in values else float(word)
        except ValueError:
            return "Usage: /rate up|down|<number> [comment]"
        if tr is None or tr.score(value, comment.strip()) is None:
            return "No run to rate yet."
        return f"Rated the last run {value:g}" + (f" ({comment.strip()})" if comment.strip() else "") + "."
    return {"trace": ("Show where the last run's time, tokens and cost went", run),
            "rate": ("Rate the last run (up / down / a number), with an optional comment", rate)}


CAPABILITIES = [Capability("tracing", "Tracing", "Record every run as a trace (model calls, tool calls, tokens, cost, "
                           "latency); local JSONL plus optional Langfuse export.", "Langfuse", prompt=_setup,
                           commands=_commands, unload=_unload, group="host")]
