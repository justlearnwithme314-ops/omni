"""bash tool + the shared shell executor used by `!` / `!!` user commands (Pi's bash.ts + bash-executor.ts)."""
from __future__ import annotations

import os
import shutil
import signal as _signal
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from typing import Any, Callable, Dict, List, Optional, Tuple

from .base import ToolDef, ToolError, check_abort
from .truncate import DEFAULT_MAX_BYTES, DEFAULT_MAX_LINES, format_size, truncate_tail

IS_WINDOWS = os.name == "nt"


def find_shell(shell_path: Optional[str] = None) -> Tuple[List[str], str]:
    """Returns (argv prefix, display name). Mirrors Pi: settings.shellPath, Git Bash on Windows, /bin/bash, sh."""
    if shell_path:
        sp = os.path.expanduser(shell_path)
        name = os.path.basename(sp).lower()
        if "powershell" in name or "pwsh" in name:
            return [sp, "-NoProfile", "-NonInteractive", "-Command"], "PowerShell"
        return [sp, "-c"], "bash" if "bash" in name else os.path.basename(sp)
    if IS_WINDOWS:
        candidates = [os.path.expandvars(r"%ProgramFiles%\Git\bin\bash.exe"),
                      os.path.expandvars(r"%ProgramFiles(x86)%\Git\bin\bash.exe"),
                      os.path.expandvars(r"%LOCALAPPDATA%\Programs\Git\bin\bash.exe")]
        which = shutil.which("bash")
        if which and "system32" not in which.lower():  # skip the WSL launcher
            candidates.insert(0, which)
        for c in candidates:
            if c and os.path.isfile(c):
                return [c, "-c"], "bash"
        pw = shutil.which("pwsh") or shutil.which("powershell")
        if pw:
            return [pw, "-NoProfile", "-NonInteractive", "-Command"], "PowerShell"
        return [os.environ.get("COMSPEC", "cmd.exe"), "/d", "/s", "/c"], "cmd"
    for c in ("/bin/bash", "/usr/bin/bash", shutil.which("bash") or ""):
        if c and os.path.isfile(c):
            return [c, "-c"], "bash"
    return ["/bin/sh", "-c"], "sh"


def _kill_tree(proc: subprocess.Popen) -> None:
    try:
        if IS_WINDOWS:
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)], capture_output=True, timeout=10)
        else:
            os.killpg(os.getpgid(proc.pid), _signal.SIGKILL)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


def run_shell(command: str, cwd: str, timeout: Optional[float] = None, signal=None,
              on_output: Optional[Callable[[str], None]] = None, shell_path: Optional[str] = None,
              prefix: Optional[str] = None, env: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    """Run a command, streaming combined output. Returns {output, exitCode, cancelled, timedOut, truncated, fullOutputPath}."""
    argv, _ = find_shell(shell_path)
    if prefix:
        command = f"{prefix}\n{command}"
    full_env = dict(os.environ)
    full_env.update(env or {})
    kwargs: Dict[str, Any] = {}
    if IS_WINDOWS:
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    else:
        kwargs["start_new_session"] = True
    proc = subprocess.Popen(argv + [command], cwd=cwd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, env=full_env, **kwargs)
    chunks: List[str] = []
    size = 0
    spill: Optional[Any] = None
    spill_path: Optional[str] = None
    state = {"cancelled": False, "timedOut": False}

    def abort() -> None:
        state["cancelled"] = True
        _kill_tree(proc)

    remove = signal.add_listener(abort) if signal is not None and hasattr(signal, "add_listener") else (lambda: None)
    timer = None
    if timeout:
        def on_timeout() -> None:
            state["timedOut"] = True
            _kill_tree(proc)
        timer = threading.Timer(float(timeout), on_timeout)
        timer.daemon = True
        timer.start()
    try:
        assert proc.stdout is not None
        while True:
            data = proc.stdout.read1(65536) if hasattr(proc.stdout, "read1") else proc.stdout.read(4096)
            if not data:
                break
            text = data.decode("utf-8", "replace").replace("\r\n", "\n")
            chunks.append(text)
            size += len(data)
            if size > DEFAULT_MAX_BYTES * 4 and spill is None:  # keep memory bounded; full output goes to a temp file
                spill_path = os.path.join(tempfile.gettempdir(), f"pi-bash-{uuid.uuid4().hex[:8]}.log")
                spill = open(spill_path, "w", encoding="utf-8")
                spill.write("".join(chunks))
            elif spill is not None:
                spill.write(text)
            if spill is not None and len(chunks) > 64:
                chunks = ["".join(chunks)[-DEFAULT_MAX_BYTES * 2:]]
            if on_output:
                on_output(text)
        proc.wait()
    finally:
        remove()
        if timer:
            timer.cancel()
        if proc.stdout:
            proc.stdout.close()
        if spill is not None:
            spill.close()
    output = "".join(chunks)
    tr = truncate_tail(output)
    if tr["truncated"] and spill_path is None:
        spill_path = os.path.join(tempfile.gettempdir(), f"pi-bash-{uuid.uuid4().hex[:8]}.log")
        with open(spill_path, "w", encoding="utf-8") as f:
            f.write(output)
    return {"output": output, "truncation": tr, "exitCode": proc.returncode, "cancelled": state["cancelled"],
            "timedOut": state["timedOut"], "truncated": tr["truncated"] or (spill_path is not None),
            "fullOutputPath": spill_path}


def format_bash_output(res: Dict[str, Any]) -> str:
    tr = res["truncation"]
    text = tr["content"] if tr["truncated"] else res["output"].rstrip("\n")
    if not text:
        text = "(no output)"
    if tr["truncated"]:
        start = tr["totalLines"] - tr["outputLines"] + 1
        end = tr["totalLines"]
        if tr["lastLinePartial"]:
            text += (f"\n\n[Showing last {format_size(tr['outputBytes'])} of line {end}. Full output: {res['fullOutputPath']}]")
        elif tr["truncatedBy"] == "lines":
            text += f"\n\n[Showing lines {start}-{end} of {tr['totalLines']}. Full output: {res['fullOutputPath']}]"
        else:
            text += (f"\n\n[Showing lines {start}-{end} of {tr['totalLines']} ({format_size(DEFAULT_MAX_BYTES)} limit). "
                     f"Full output: {res['fullOutputPath']}]")
    return text


def make_bash(cwd: str, settings_getter: Optional[Callable[[], Dict[str, Any]]] = None,
              env_getter: Optional[Callable[[], Dict[str, str]]] = None) -> ToolDef:
    settings = settings_getter() if settings_getter else {}
    _, shell_name = find_shell(settings.get("shellPath"))

    def execute(_id, params, signal=None, on_update=None, ctx=None):
        check_abort(signal)
        command = params["command"]
        timeout = params.get("timeout")
        st = settings_getter() if settings_getter else {}
        started = time.time()
        acc: List[str] = []

        def on_output(t: str) -> None:
            acc.append(t)
            if on_update:
                tail = truncate_tail("".join(acc))
                on_update({"content": [{"type": "text", "text": tail["content"]}], "details": None})

        res = run_shell(command, getattr(ctx, "cwd", None) or cwd, timeout=timeout, signal=signal, on_output=on_output,
                        shell_path=st.get("shellPath"), prefix=st.get("shellCommandPrefix"),
                        env=env_getter() if env_getter else None)
        text = format_bash_output(res)
        if text == "(no output)":
            text = ""

        def status(t: str, s: str) -> str:
            return f"{t}\n\n{s}" if t else s

        details = {"truncation": res["truncation"] if res["truncated"] else None, "fullOutputPath": res["fullOutputPath"],
                   "exitCode": res["exitCode"], "wallTimeSeconds": round(time.time() - started, 1)}
        if res["cancelled"]:
            raise ToolError(status(text, "Command aborted"))
        if res["timedOut"]:
            raise ToolError(status(text, f"Command timed out after {timeout} seconds"))
        if res["exitCode"] not in (0, None):
            return {"content": [{"type": "text", "text": status(text, f"Command exited with code {res['exitCode']}")}],
                    "details": details, "isError": True}
        return {"content": [{"type": "text", "text": text or "(no output)"}], "details": details}

    return ToolDef(
        name="bash", label="bash",
        description=(f"Execute a {shell_name} command in the current working directory. Returns stdout and stderr. Output is truncated to "
                     f"last {DEFAULT_MAX_LINES} lines or {DEFAULT_MAX_BYTES // 1024}KB (whichever is hit first). If truncated, full "
                     f"output is saved to a temp file. Optionally provide a timeout in seconds."),
        parameters={"type": "object", "properties": {
            "command": {"type": "string", "description": "Shell command to execute"},
            "timeout": {"type": "number", "description": "Timeout in seconds (optional, no default timeout)"}},
            "required": ["command"]},
        execute=execute, prompt_snippet="Execute bash commands (ls, grep, find, etc.)",
        prompt_guidelines=["You can inspect PI_* environment variables for current model and session details."])
