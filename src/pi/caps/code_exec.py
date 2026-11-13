"""`code-exec`: run a snippet of code and get its output (Hermes `execute_code`, Oh My Pi `eval`).

For calculations, data wrangling and quick checks, a short script is cheaper and more reliable than a chain of
shell commands or reasoning in prose. The snippet runs as a separate process in the project folder (Python with the
interpreter Omni runs on, JavaScript with Node), with a timeout and capped output. It is not sandboxed: it can do
whatever the shell can. Plan mode blocks it like any other tool that can change things.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from typing import Any, Dict

from ..tools.base import ToolDef, ToolError, check_abort, text_result
from . import Capability

MAX_OUTPUT = 20_000
LANGS = {"python": ".py", "javascript": ".mjs"}


def _tools(session: Any) -> Dict[str, ToolDef]:
    def execute(_id, p, signal=None, on_update=None, ctx=None):
        check_abort(signal)
        code = str(p.get("code") or "")
        if not code.strip():
            raise ToolError("code is required")
        lang = str(p.get("language") or "python").lower()
        lang = {"py": "python", "js": "javascript", "node": "javascript"}.get(lang, lang)
        if lang not in LANGS:
            raise ToolError("language must be python or javascript")
        if lang == "python":
            cmd = [sys.executable, "-X", "utf8"]
        else:
            node = shutil.which("node")
            if not node:
                raise ToolError("Node.js is not installed; use language=python")
            cmd = [node]
        timeout = max(1.0, min(float(p.get("timeout") or 60), 600))
        fd, path = tempfile.mkstemp(suffix=LANGS[lang], prefix="omni-exec-")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(code)
            env = dict(os.environ, PYTHONIOENCODING="utf-8")
            try:
                r = subprocess.run(cmd + [path], cwd=session.cwd, capture_output=True, text=True, timeout=timeout, env=env,
                                   encoding="utf-8", errors="replace")
            except subprocess.TimeoutExpired:
                return text_result(f"Timed out after {timeout:.0f} seconds.", {"timedOut": True}, True)
        finally:
            try:
                os.remove(path)
            except OSError:
                pass
        out = (r.stdout or "")
        err = (r.stderr or "").replace(path, f"<snippet{LANGS[lang]}>")
        text = out + (("\n[stderr]\n" + err) if err.strip() else "")
        if len(text) > MAX_OUTPUT:
            text = text[:MAX_OUTPUT // 2] + f"\n[... {len(text) - MAX_OUTPUT} characters omitted ...]\n" + text[-MAX_OUTPUT // 2:]
        text = text.rstrip() or "(no output; print the values you need)"
        if r.returncode != 0:
            return text_result(f"{text}\n\nExited with code {r.returncode}", {"exitCode": r.returncode, "language": lang}, True)
        return text_result(text, {"exitCode": 0, "language": lang})

    return {"execute_code": ToolDef(
        name="execute_code", label="Run code",
        description="Run a Python (default) or JavaScript snippet in the project folder and return what it prints. "
                    "Use it for calculations, parsing and transforming data, and quick experiments; print the results "
                    "you need. Not for long-running servers.",
        parameters={"type": "object", "properties": {
            "code": {"type": "string"}, "language": {"type": "string", "enum": ["python", "javascript"]},
            "timeout": {"type": "number", "description": "seconds, default 60"}}, "required": ["code"]},
        execute=execute, prompt_snippet="Run Python or JavaScript snippets")}


CAPABILITIES = [Capability("code-exec", "Code execution", "execute_code: run a Python or JavaScript snippet and read its "
                           "output.", "Hermes execute_code, Oh My Pi eval", tools=_tools)]
