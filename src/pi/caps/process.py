"""`processes`: long-running commands in the background (Hermes' process registry, Letta's Monitor).

`bash` blocks until a command exits, which is wrong for a dev server, a file watcher or a long build. The `process`
tool starts such a command and returns at once; the agent keeps working and comes back to it:

    start(command, name?)     run in the background, returns an id
    list                      what is running or has finished
    log(id, lines?)           the latest output (kept in a rolling buffer of the last 2,000 lines)
    wait(id, seconds?, text?) block until it exits, or until `text` appears in its output (e.g. "listening on")
    kill(id)                  stop it

Processes belong to the session: they are stopped when it ends. Lines matching `notify` (a regular expression
given to `start`) are also announced to you as they appear - a cheap way to watch for "error" or "ready".
Plan mode and the approvals capability treat `start` like a shell command.
"""
from __future__ import annotations

import collections
import os
import re
import subprocess
import threading
import time
from typing import Any, Deque, Dict, List, Optional

from ..tools.base import ToolDef, ToolError, text_result
from . import Capability

MAX_LINES, MAX_PROCESSES = 2000, 12


class Proc:
    def __init__(self, pid: str, name: str, command: str, popen: subprocess.Popen, notify: Optional[re.Pattern], session: Any):
        self.id, self.name, self.command, self.popen = pid, name, command, popen
        self.lines: Deque[str] = collections.deque(maxlen=MAX_LINES)
        self.total = 0
        self.started = time.time()
        self.ended: Optional[float] = None
        self.changed = threading.Condition()

        def pump() -> None:
            assert popen.stdout is not None
            for raw in popen.stdout:
                line = raw.rstrip("\r\n")
                with self.changed:
                    self.lines.append(line)
                    self.total += 1
                    self.changed.notify_all()
                if notify is not None and notify.search(line):
                    session._notify({"type": "notice", "level": "info", "message": f"[{self.name}] {line[:200]}"})
            popen.wait()
            with self.changed:
                self.ended = time.time()
                self.changed.notify_all()
        threading.Thread(target=pump, daemon=True, name=f"omni-proc-{pid}").start()

    @property
    def running(self) -> bool:
        return self.ended is None

    def status(self) -> str:
        if self.running:
            return f"running {time.time() - self.started:.0f}s"
        return f"exited {self.popen.returncode} after {self.ended - self.started:.0f}s"

    def tail(self, n: int) -> str:
        with self.changed:
            rows = list(self.lines)[-n:]
            dropped = self.total - len(self.lines)
        head = f"[{dropped} earlier lines dropped]\n" if dropped > 0 and n >= len(self.lines) else ""
        return head + "\n".join(rows)

    def kill(self) -> None:
        if not self.running:
            return
        try:
            if os.name == "nt":
                subprocess.run(["taskkill", "/F", "/T", "/PID", str(self.popen.pid)], capture_output=True, timeout=10)
            else:
                import signal
                os.killpg(os.getpgid(self.popen.pid), signal.SIGTERM)
        except Exception:
            try:
                self.popen.kill()
            except Exception:
                pass


def registry(session: Any) -> Dict[str, Proc]:
    reg = getattr(session, "_processes", None)
    if reg is None:
        reg = session._processes = {}
    return reg


def _tools(session: Any) -> Dict[str, ToolDef]:
    counter = {"n": 0}

    def get(pid: str) -> Proc:
        reg = registry(session)
        p = reg.get(pid) or next((x for x in reg.values() if x.name == pid), None)
        if p is None:
            raise ToolError(f"no process '{pid}'. " + (", ".join(f"{x.id} ({x.name})" for x in reg.values()) or "None have been started."))
        return p

    def execute(_id, p, signal=None, on_update=None, ctx=None):
        action = str(p.get("action") or "list").lower()
        reg = registry(session)
        if action == "start":
            command = str(p.get("command") or "").strip()
            if not command:
                raise ToolError("command is required")
            if sum(1 for x in reg.values() if x.running) >= MAX_PROCESSES:
                raise ToolError(f"{MAX_PROCESSES} processes are already running; kill one first")
            pattern = None
            if p.get("notify"):
                try:
                    pattern = re.compile(str(p["notify"]), re.I)
                except re.error as e:
                    raise ToolError(f"notify is not a valid regular expression: {e}") from None
            kwargs: Dict[str, Any] = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt" else {"start_new_session": True}
            try:
                popen = subprocess.Popen(command, shell=True, cwd=session.cwd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                         stdin=subprocess.DEVNULL, text=True, encoding="utf-8", errors="replace", bufsize=1, **kwargs)
            except OSError as e:
                raise ToolError(f"could not start: {e}") from None
            counter["n"] += 1
            pid = f"p{counter['n']}"
            proc = reg[pid] = Proc(pid, str(p.get("name") or command.split()[0])[:30], command, popen, pattern, session)
            time.sleep(0.4)  # catch an immediate failure (typo, missing program)
            first = proc.tail(15)
            return text_result(f"Started {pid} ({proc.name}): {proc.status()}" + (f"\n{first}" if first else ""), {"id": pid, "running": proc.running})
        if action == "list":
            if not reg:
                return text_result("No background processes.", {"count": 0})
            return text_result("\n".join(f"{x.id}  {x.name:<16} {x.status():<24} {x.command[:70]}" for x in reg.values()), {"count": len(reg)})
        proc = get(str(p.get("id") or ""))
        if action == "log":
            n = max(1, min(int(p.get("lines") or 60), 500))
            return text_result(f"{proc.id} ({proc.name}): {proc.status()}\n{proc.tail(n) or '(no output yet)'}", {"running": proc.running})
        if action == "wait":
            limit = max(0.1, min(float(p.get("seconds") or 60), 600))
            want = str(p.get("text") or "")
            deadline = time.time() + limit
            with proc.changed:
                while True:
                    if want and any(want.lower() in line.lower() for line in proc.lines):
                        outcome = f"'{want}' appeared"
                        break
                    if not proc.running:
                        outcome = proc.status()
                        break
                    remaining = deadline - time.time()
                    if remaining <= 0 or (signal is not None and getattr(signal, "aborted", False)):
                        outcome = f"still running after {limit:g}s" + (f"; '{want}' has not appeared" if want else "")
                        break
                    proc.changed.wait(min(remaining, 0.5))
            return text_result(f"{proc.id} ({proc.name}): {outcome}\n{proc.tail(30)}", {"running": proc.running, "exitCode": proc.popen.returncode})
        if action == "kill":
            was = proc.running
            proc.kill()
            with proc.changed:
                proc.changed.wait_for(lambda: not proc.running, timeout=5)
            return text_result(f"{proc.id} ({proc.name}): " + ("stopped." if was else "had already exited."), {"running": proc.running})
        raise ToolError("action must be start, list, log, wait or kill")

    return {"process": ToolDef(
        name="process", label="Process",
        description="Run long-lived commands in the background (dev servers, watchers, long builds) instead of blocking "
                    "in bash. Actions: start(command, name?, notify?: regex of lines to announce), list, log(id, lines?), "
                    "wait(id, seconds?, text?: stop waiting when this appears), kill(id). Start it, do other work, then "
                    "check log or wait.",
        parameters={"type": "object", "properties": {
            "action": {"type": "string", "enum": ["start", "list", "log", "wait", "kill"]},
            "command": {"type": "string"}, "name": {"type": "string"}, "id": {"type": "string"}, "lines": {"type": "integer"},
            "seconds": {"type": "number"}, "text": {"type": "string"}, "notify": {"type": "string"}}, "required": ["action"]},
        execute=execute, prompt_snippet="Run and watch background processes")}


def _unload(session: Any) -> None:
    for proc in list(getattr(session, "_processes", {}).values() if getattr(session, "_processes", None) else []):
        proc.kill()
    session._processes = None


def _gate(session: Any, tool_name: str, args: Dict[str, Any]) -> Optional[str]:
    # a background command is still a shell command: let the same guards see it
    if tool_name == "process" and str(args.get("action")) == "start":
        return session.caps.gate("bash", {"command": str(args.get("command") or "")})
    return None


CAPABILITIES = [Capability("processes", "Background processes", "Start long-running commands in the background, then tail, "
                           "wait on or kill them.", "Hermes process registry, Letta Monitor", tools=_tools, gate=_gate,
                           commands=lambda s: {"ps": ("List background processes", lambda _a: "\n".join(
                               f"{x.id}  {x.name:<16} {x.status():<24} {x.command[:70]}" for x in registry(s).values()) or "No background processes.")},
                           unload=_unload)]
