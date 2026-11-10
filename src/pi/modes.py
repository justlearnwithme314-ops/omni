"""Non-interactive modes: print (-p), json (--mode json), rpc (--mode rpc). Same session machinery as the TUI."""
from __future__ import annotations

import json
import sys
import threading
from typing import Any, Dict, List, Optional

from .agent import AgentSession, SessionOptions
from .ai import THINKING_LEVELS, model_key
from .commands import export_html, last_assistant_text


class HeadlessUI:
    """ctx.ui for modes without a terminal UI (hasUI = false): dialogs resolve to their 'cancel' value."""
    has_ui = False
    width, height = 100, 30

    def __init__(self, emit=None):
        self.emit = emit

    def __getattr__(self, name: str):
        if name.startswith("ext_"):
            def fn(**kw: Any) -> Any:
                if self.emit and name in ("ext_notify", "ext_setStatus", "ext_setWidget", "ext_setTitle"):
                    self.emit({"type": "extension_ui_request", "method": name[4:], **kw})
                return False if name == "ext_confirm" else None
            return fn
        raise AttributeError(name)


def _write(obj: Dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(obj, ensure_ascii=False, default=str) + "\n")
    sys.stdout.flush()


def run_print(opts: SessionOptions, messages: List[str], json_mode: bool = False) -> int:
    s = AgentSession(opts, ui=HeadlessUI(_write if json_mode else None))
    s.start()
    if json_mode:
        s.subscribe(lambda ev: _write(ev) if ev.get("type") != "message_update" else _write(
            {"type": "message_update", "assistantMessageEvent": ev.get("assistantMessageEvent")}))
    for d in s.diagnostics:
        print(f"warning: {d}", file=sys.stderr)
    code = 0
    try:
        for m in messages:
            s.prompt(m, source="print")
        if not json_mode:
            last = None
            for e in reversed(s.sessions.get_branch()):
                if e["type"] == "message" and e["message"].get("role") == "assistant":
                    last = e["message"]
                    break
            if last and last.get("stopReason") == "error":
                print(f"Error: {last.get('errorMessage')}", file=sys.stderr)
                code = 1
            else:
                print(last_assistant_text(s))
    finally:
        s.shutdown("quit")
    return code


def run_rpc(opts: SessionOptions) -> int:
    """JSONL commands on stdin, responses + events on stdout (docs/rpc-commands.md subset)."""
    s = AgentSession(opts, ui=HeadlessUI(_write))
    s.start()
    s.subscribe(_write)

    def respond(cmd: Dict[str, Any], success: bool = True, data: Any = None, error: Optional[str] = None) -> None:
        r: Dict[str, Any] = {"type": "response", "command": cmd.get("type"), "success": success}
        if cmd.get("id") is not None:
            r["id"] = cmd["id"]
        if data is not None:
            r["data"] = data
        if error:
            r["error"] = error
        _write(r)

    def state() -> Dict[str, Any]:
        return {"model": s.model, "thinkingLevel": s.thinking_level, "isStreaming": s.is_streaming,
                "sessionFile": str(s.sessions.file) if s.sessions.file else None, "sessionId": s.sessions.session_id,
                "sessionName": s.sessions.session_name(), "messageCount": len(s.sessions.entries),
                "pendingMessageCount": len(s.steer_queue) + len(s.follow_queue),
                "steeringMode": s.settings.get("steeringMode"), "followUpMode": s.settings.get("followUpMode")}

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            cmd = json.loads(line)
        except json.JSONDecodeError as e:
            _write({"type": "response", "success": False, "error": f"invalid JSON: {e}"})
            continue
        t = cmd.get("type")
        try:
            if t == "prompt":
                if s.is_streaming:
                    (s.follow_up if cmd.get("streamingBehavior") == "followUp" else s.steer)(cmd.get("message", ""))
                    respond(cmd, data={"disposition": "queued"})
                else:
                    threading.Thread(target=s.prompt, args=(cmd.get("message", ""), cmd.get("images"), "rpc"), daemon=True).start()
                    respond(cmd, data={"disposition": "started"})
            elif t == "steer":
                s.steer(cmd.get("message", ""))
                respond(cmd, data={"disposition": "queued"})
            elif t == "follow_up":
                s.follow_up(cmd.get("message", ""))
                respond(cmd, data={"disposition": "queued"})
            elif t == "abort":
                s.abort()
                respond(cmd)
            elif t == "clear_queue":
                respond(cmd, data={"messages": s.dequeue_all()})
            elif t == "new_session":
                respond(cmd, data={"cancelled": not s.new_session()})
            elif t == "get_state":
                respond(cmd, data=state())
            elif t == "get_messages":
                respond(cmd, data={"messages": s.sessions.build_context()["messages"]})
            elif t == "set_model":
                m = s.registry.resolve(f"{cmd.get('provider')}/{cmd.get('modelId')}")
                if m:
                    s.set_model(m)
                    respond(cmd, data=m)
                else:
                    respond(cmd, False, error="model not found")
            elif t == "cycle_model":
                m = s.cycle_model(1)
                respond(cmd, data={"model": m, "thinkingLevel": s.thinking_level} if m else None)
            elif t == "get_available_models":
                respond(cmd, data={"models": [m for m in s.registry.all() if s.auth.has_auth(m["provider"])]})
            elif t == "set_thinking_level":
                s.set_thinking_level(cmd.get("level", "medium"))
                respond(cmd)
            elif t == "cycle_thinking_level":
                respond(cmd, data={"level": s.cycle_thinking()})
            elif t == "get_available_thinking_levels":
                respond(cmd, data={"levels": THINKING_LEVELS if s.model and s.model.get("reasoning") else ["off"]})
            elif t in ("set_steering_mode", "set_follow_up_mode"):
                key = "steeringMode" if t == "set_steering_mode" else "followUpMode"
                s.settings.data[key] = cmd.get("mode", "one-at-a-time")
                respond(cmd)
            elif t == "compact":
                e = s.compact(cmd.get("customInstructions"))
                respond(cmd, data=e)
            elif t == "set_auto_compaction":
                s.settings.data.setdefault("compaction", {})["enabled"] = bool(cmd.get("enabled"))
                respond(cmd)
            elif t == "set_auto_retry":
                s.settings.data.setdefault("retry", {})["enabled"] = bool(cmd.get("enabled"))
                respond(cmd)
            elif t == "bash":
                respond(cmd, data=s.user_bash(cmd.get("command", ""), bool(cmd.get("excludeFromContext"))))
            elif t == "get_session_stats":
                respond(cmd, data={**s.totals(), "contextUsage": s.context_usage()})
            elif t == "export_html":
                path = cmd.get("outputPath") or f"pi-session-{s.sessions.session_id[:8]}.html"
                export_html(s, path)
                respond(cmd, data={"path": path})
            elif t == "switch_session":
                respond(cmd, data={"cancelled": not s.resume(cmd["sessionPath"])})
            elif t == "fork":
                respond(cmd, data={"cancelled": not s.fork(cmd["entryId"])})
            elif t == "get_commands":
                from .command_sources import CommandSources
                respond(cmd, data={"commands": [
                    {"name": e.name, "description": e.description, "source": e.kind, "qualified": e.qualified,
                     "origin": e.origin, "path": e.path}
                    for e in CommandSources(s, include_builtins=False).entries()]})
            elif t == "set_session_name":
                s.set_session_name(cmd.get("name", ""))
                respond(cmd)
            elif t == "get_last_assistant_text":
                respond(cmd, data={"text": last_assistant_text(s)})
            else:
                respond(cmd, False, error=f"unknown command: {t}")
        except Exception as e:
            respond(cmd, False, error=f"{type(e).__name__}: {e}")
    s.wait_for_idle(300)
    s.shutdown("quit")
    return 0
