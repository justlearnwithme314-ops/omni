"""`notify`: tell you when a long run has finished (Oh My Pi and Hermes ring or pop a notification on completion).

When a run that took at least `min_seconds` (default 20) ends in the terminal UI, Omni rings the terminal bell and
sends the terminal's "notification" escape sequence, which Windows Terminal, iTerm2, kitty, WezTerm and others
turn into a desktop notification or a tab badge. Nothing happens for short runs or in headless modes.

    "notify": {"min_seconds": 20, "command": "powershell -c \\"[console]::beep(880,200)\\""}

`command` (optional) runs as well, with OMNI_NOTIFY_TEXT set, for a custom sound or a real toast.
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
from typing import Any, Dict

from . import Capability


def emit(text: str) -> None:
    """Bell + OSC 9 (desktop notification) straight to the terminal."""
    out = sys.__stdout__
    try:
        out.write("\a\x1b]9;" + text.replace("\x07", " ").replace("\x1b", " ")[:120] + "\x07")
        out.flush()
    except Exception:
        pass


def _setup(session: Any) -> str:
    _unload(session)
    opt = session.profile.get("notify") if isinstance(session.profile.get("notify"), dict) else {}
    min_seconds = float(opt.get("min_seconds", 20))
    st: Dict[str, Any] = {"started": None}

    def on_event(ev: Dict[str, Any]) -> None:
        t = ev.get("type")
        if t == "agent_start" and st["started"] is None:
            st["started"] = time.time()
        elif t == "agent_settled" or (t == "agent_end" and not getattr(session, "is_streaming", False)):
            started, st["started"] = st["started"], None
            if started is None or time.time() - started < min_seconds or not getattr(session.ui, "has_ui", False):
                return
            text = f"Omni finished ({(time.time() - started) / 60:.1f} min) in {os.path.basename(session.cwd)}"
            sys.modules[__name__].emit(text)
            if opt.get("command"):
                try:
                    subprocess.Popen(str(opt["command"]), shell=True, env=dict(os.environ, OMNI_NOTIFY_TEXT=text),
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                except OSError:
                    pass
    session._notify_unsub = session.subscribe(on_event)
    return ""


def _unload(session: Any) -> None:
    unsub = getattr(session, "_notify_unsub", None)
    if unsub:
        unsub()
    session._notify_unsub = None


CAPABILITIES = [Capability("notify", "Finish notifications", "Ring / notify when a long run finishes.",
                           "Oh My Pi, Hermes completion notifications", prompt=_setup, unload=_unload)]
