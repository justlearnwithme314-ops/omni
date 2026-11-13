"""`sandbox`: the agent's shell runs in an isolated container, not on your machine (Daytona's core idea).

With this on, `bash` executes inside a per-session container: the project is mounted at `/workspace`, the network
is off unless you allow it, and the container is removed when the session ends. File tools (`read`, `edit`,
`write`) still act on the project folder directly, so edits are visible on both sides.

    "sandbox": {"image": "python:3.12-slim", "network": false, "mount": "rw", "memory": "2g", "cpus": "2"}

Runtime: Docker or Podman (`OMNI_SANDBOX_RUNTIME` overrides the command). Without one the capability reports
"not loaded" in `/capabilities` and the normal `bash` stays in place - it never silently falls back to the host
once it is active. `/sandbox` shows the state; `/sandbox reset` throws the container away.
"""
from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import time
from typing import Any, Dict, List, Optional

from ..tools.base import ToolDef, ToolError, check_abort, text_result
from . import Capability

DEFAULT_IMAGE = "python:3.12-slim"
MAX_OUTPUT = 30_000
INSTALL = "no container runtime found (install Docker or Podman, or set OMNI_SANDBOX_RUNTIME)"


def runtime() -> Optional[List[str]]:
    custom = os.environ.get("OMNI_SANDBOX_RUNTIME")
    if custom:
        return [t.strip('"') for t in shlex.split(custom, posix=os.name != "nt")]
    for name in ("docker", "podman"):
        found = shutil.which(name)
        if found:
            return [found]
    return None


class Sandbox:
    def __init__(self, session: Any):
        self.session = session
        opt = session.profile.get("sandbox") if isinstance(session.profile.get("sandbox"), dict) else {}
        self.image = str(opt.get("image") or DEFAULT_IMAGE)
        self.network = bool(opt.get("network", False))
        self.mount = "ro" if opt.get("mount") == "ro" else "rw"
        self.memory, self.cpus = opt.get("memory"), opt.get("cpus")
        self.name = "omni-" + "".join(ch for ch in str(session.sessions.session_id) if ch.isalnum())[:16].lower()
        self.started = False

    def _run(self, args: List[str], timeout: float, stdin: Optional[str] = None) -> subprocess.CompletedProcess:
        rt = runtime()
        if rt is None:
            raise ToolError(INSTALL)
        return subprocess.run(rt + args, capture_output=True, text=True, timeout=timeout, input=stdin,
                              encoding="utf-8", errors="replace")

    def ensure(self) -> None:
        if self.started:
            return
        args = ["run", "-d", "--rm", "--name", self.name, "-v", f"{self.session.cwd}:/workspace:{self.mount}", "-w", "/workspace"]
        if not self.network:
            args += ["--network", "none"]
        if self.memory:
            args += ["--memory", str(self.memory)]
        if self.cpus:
            args += ["--cpus", str(self.cpus)]
        p = self._run(args + [self.image, "sleep", "infinity"], timeout=300)
        if p.returncode != 0:
            raise ToolError(f"could not start the sandbox container ({self.image}): {(p.stderr or p.stdout).strip()[:500]}")
        self.started = True

    def exec(self, command: str, timeout: float) -> Dict[str, Any]:
        self.ensure()
        try:
            p = self._run(["exec", "-w", "/workspace", self.name, "sh", "-lc", command], timeout=timeout)
        except subprocess.TimeoutExpired:
            return {"code": None, "output": f"Command timed out after {timeout:.0f} seconds"}
        out = (p.stdout or "") + (p.stderr or "")
        if len(out) > MAX_OUTPUT:
            out = f"[{len(out) - MAX_OUTPUT} earlier characters omitted]\n" + out[-MAX_OUTPUT:]
        return {"code": p.returncode, "output": out}

    def destroy(self) -> None:
        if not self.started:
            return
        self.started = False
        try:
            self._run(["rm", "-f", self.name], timeout=30)
        except Exception:
            pass

    def describe(self) -> str:
        state = "running" if self.started else "not started yet (starts with the first command)"
        return (f"Sandbox {self.name}: {state}\n  image {self.image} · network {'on' if self.network else 'off'} · "
                f"project mounted {self.mount} at /workspace · runtime {' '.join(runtime() or ['(none)'])}")


def sandbox_of(session: Any) -> Optional[Sandbox]:
    return getattr(session, "_sandbox", None)


def _tools(session: Any) -> Dict[str, ToolDef]:
    _unload(session)
    box = session._sandbox = Sandbox(session)
    orig = session.builtin_tools.get("bash")

    def execute(_id, p, signal=None, on_update=None, ctx=None):
        check_abort(signal)
        command = str(p.get("command") or "").strip()
        if not command:
            raise ToolError("command is required")
        r = box.exec(command, float(p.get("timeout") or 120))
        out = r["output"].rstrip() or "(no output)"
        if r["code"] is None:
            return text_result(out, {"sandbox": box.name, "timedOut": True}, True)
        if r["code"] != 0:
            return text_result(f"{out}\n\nCommand exited with code {r['code']}", {"sandbox": box.name, "exitCode": r["code"]}, True)
        return text_result(out, {"sandbox": box.name, "exitCode": 0})

    params = orig.parameters if orig is not None else {"type": "object", "properties": {
        "command": {"type": "string"}, "timeout": {"type": "number"}}, "required": ["command"]}
    return {"bash": ToolDef(
        name="bash", label="bash",
        description=f"Run a shell command inside an isolated Linux container (image {box.image}). The project is mounted "
                    f"at /workspace ({'read-write' if box.mount == 'rw' else 'read-only'}) and is the working directory; "
                    f"the network is {'available' if box.network else 'disabled'}. Nothing outside /workspace persists.",
        parameters=params, execute=execute, prompt_snippet="Run shell commands in an isolated sandbox")}


def _unload(session: Any) -> None:
    box = sandbox_of(session)
    if box is not None:
        box.destroy()
        session._sandbox = None


def _commands(session: Any) -> Dict[str, Any]:
    def run(args: str) -> str:
        box = sandbox_of(session)
        if box is None:
            return "The sandbox is not active."
        if args.strip() == "reset":
            box.destroy()
            return "Sandbox removed; a fresh one starts with the next command."
        word, _, name = args.strip().partition(" ")
        if word == "snapshot":  # Daytona snapshots: keep the container's installed state as an image
            name = name.strip() or f"omni-snapshot-{int(time.time())}"
            if not box.started:
                return "Nothing to snapshot yet: the sandbox starts with the first command."
            try:
                p = box._run(["commit", box.name, name], timeout=300)
            except Exception as e:
                return f"Snapshot failed: {type(e).__name__}: {e}"
            if p.returncode != 0:
                return f"Snapshot failed: {(p.stderr or p.stdout).strip()[:300]}"
            return (f"Saved the sandbox as image `{name}`. To start from it, set \"sandbox\": {{\"image\": \"{name}\"}} "
                    "in a profile.")
        return box.describe() + "\n/sandbox snapshot [name] saves its state as an image · /sandbox reset discards it"
    return {"sandbox": ("Show the sandbox container, `snapshot [name]` it, or `reset` it", run)}


CAPABILITIES = [Capability("sandbox", "Sandboxed shell", "bash runs in a per-session container with the project mounted "
                           "and the network off.", "Daytona sandboxes", tools=_tools, commands=_commands, unload=_unload,
                           available=lambda s: None if runtime() else INSTALL, group="host")]
