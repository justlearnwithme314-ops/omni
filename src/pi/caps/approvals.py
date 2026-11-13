"""`approvals`: ask before a dangerous command runs (Hermes' command approval, OpenClaw's exec approvals).

Shell commands are matched against a short list of destructive patterns (recursive deletes, force pushes, history
rewrites, `sudo`, piping a download into a shell, dropping databases, formatting disks, ...), and file tools are
checked for paths outside the project. A match needs a decision:

    in the terminal UI     a picker: allow once / always allow this kind / deny
    through the gateway    the question is sent to the chat; reply `yes`, `always` or `no`
    nobody to ask          denied, and the model is told why (print mode, scheduled jobs, subagents)

"Always allow" is remembered per kind in `<agent-dir>/approvals.json`. Profile options:
`"approvals": {"mode": "ask" | "deny" | "off", "outside_project": true}`.

This is a guard against mistakes, not a security boundary: a command can be written to slip past pattern
matching. For isolation use the `sandbox` capability.
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from . import Capability

PATTERNS: List[Tuple[str, str, str]] = [
    ("recursive-delete", r"\brm\s+(?:-[a-zA-Z]*\s+)*-[a-zA-Z]*[rR][a-zA-Z]*\b|\brm\s+(?:-[a-zA-Z]+\s+)*--recursive\b|"
                         r"\bRemove-Item\b[^|;&]*-Recurse|\b(?:rd|rmdir)\s+/s\b|\bdel\s+/[sq]\b", "recursive delete"),
    ("force-push", r"\bgit\s+push\b[^|;&]*(?:--force\b|--force-with-lease\b|\s-f\b)", "git force push"),
    ("history-rewrite", r"\bgit\s+(?:reset\s+--hard|clean\s+-[a-zA-Z]*f|filter-branch|checkout\s+--\s+\.|restore\s+\.)", "discarding or rewriting git work"),
    ("privilege", r"(?:^|[\s;&|])(?:sudo|doas|runas)\s", "running with elevated privileges"),
    ("pipe-to-shell", r"\b(?:curl|wget|iwr|Invoke-WebRequest)\b[^|;&]*\|\s*(?:sudo\s+)?(?:ba|z|k)?sh\b|\biex\s*\(", "piping a download into a shell"),
    ("database-drop", r"\b(?:DROP\s+(?:TABLE|DATABASE|SCHEMA)|TRUNCATE\s+TABLE)\b", "dropping database objects"),
    ("disk", r"\bmkfs\b|\bdd\b[^|;&]*\bof=/dev/|\bformat\s+[a-zA-Z]:|>\s*/dev/sd[a-z]\b|\bdiskpart\b", "writing to a disk device"),
    ("permissions", r"\bchmod\s+(?:-[a-zA-Z]*\s+)*-?[a-zA-Z]*R[a-zA-Z]*\s+0?777\b|\bchown\s+-R\b", "recursive permission change"),
    ("power", r"(?:^|[\s;&|])(?:shutdown|reboot|halt|poweroff)\b|\bStop-Computer\b|\bRestart-Computer\b", "shutting the machine down"),
    ("kill-all", r"\bkill\s+-9\s+-1\b|\bkillall\b|\btaskkill\b[^|;&]*/f\b[^|;&]*/im\b", "killing processes broadly"),
    ("publish", r"\b(?:npm|pnpm|yarn)\s+publish\b|\btwine\s+upload\b|\bcargo\s+publish\b|\bgh\s+release\s+create\b", "publishing a package or release"),
    ("container-prune", r"\bdocker\s+(?:system|volume|image)\s+prune\b|\bkubectl\s+delete\b", "deleting containers or cluster resources"),
]
_COMPILED = [(k, re.compile(rx, re.I), d) for k, rx, d in PATTERNS]
FILE_TOOLS = {"write", "edit"}


def classify_command(command: str) -> Optional[Tuple[str, str]]:
    for key, rx, desc in _COMPILED:
        if rx.search(command or ""):
            return key, desc
    return None


def _store(session: Any) -> Path:
    return Path(session.agent_dir) / "approvals.json"


def always_allowed(session: Any) -> List[str]:
    try:
        d = json.loads(_store(session).read_text(encoding="utf-8"))
        return [str(x) for x in d.get("always") or []] if isinstance(d, dict) else []
    except (OSError, ValueError):
        return []


def remember(session: Any, key: str) -> None:
    keys = sorted(set(always_allowed(session)) | {key})
    _store(session).write_text(json.dumps({"always": keys}, indent=2), encoding="utf-8")


def _outside_project(session: Any, path: str) -> bool:
    try:
        from ..tools.base import resolve_to_cwd
        target = Path(resolve_to_cwd(path, session.cwd)).resolve()
        root = Path(session.cwd).resolve()
        return root != target and root not in target.parents
    except (OSError, ValueError):
        return False


def needs_approval(session: Any, tool_name: str, args: Dict[str, Any]) -> Optional[Tuple[str, str, str]]:
    """(kind, what it is, the exact thing to show) when this call needs a decision."""
    opt = session.profile.get("approvals") if isinstance(session.profile.get("approvals"), dict) else {}
    if tool_name == "bash":
        tool = session.all_tools().get("bash")
        if tool is not None and str(tool.source).endswith(":sandbox"):
            return None  # already isolated in a container
        hit = classify_command(str(args.get("command") or ""))
        if hit:
            return hit[0], hit[1], str(args.get("command"))[:300]
    elif tool_name in FILE_TOOLS and opt.get("outside_project", True) and isinstance(args.get("path"), str):
        if _outside_project(session, args["path"]):
            return "outside-project", "changing a file outside the project", args["path"]
    return None


def decide(session: Any, kind: str, desc: str, detail: str) -> str:
    """'once' | 'always' | 'deny' from whoever can answer."""
    question = f"Approval needed — {desc}:\n  {detail}"
    approver = getattr(session, "approver", None)
    if callable(approver):  # the gateway asks in the chat
        try:
            return str(approver(question) or "deny")
        except Exception:
            return "deny"
    ui = session.ui
    if ui is not None and getattr(ui, "has_ui", False):
        options = ["Allow once", f"Always allow: {desc}", "Deny"]
        pick = ui.ext_select(title=question, options=options)
        return {options[0]: "once", options[1]: "always"}.get(pick or "", "deny")
    return "unattended"


def _gate(session: Any, tool_name: str, args: Dict[str, Any]) -> Optional[str]:
    opt = session.profile.get("approvals") if isinstance(session.profile.get("approvals"), dict) else {}
    mode = str(opt.get("mode") or "ask")
    if mode == "off":
        return None
    hit = needs_approval(session, tool_name, args)
    if hit is None:
        return None
    kind, desc, detail = hit
    granted = getattr(session, "_approved_kinds", None)
    if granted is None:
        granted = session._approved_kinds = set()
    if kind in granted or kind in always_allowed(session):
        return None
    if mode == "deny":
        return f"Blocked by policy ({desc}). This profile never allows it; find another way or ask the user to do it."
    answer = decide(session, kind, desc, detail)
    if answer == "once":
        return None
    if answer == "always":
        remember(session, kind)
        granted.add(kind)
        return None
    if answer == "unattended":
        return (f"Blocked: {desc} needs the user's approval, and nobody is available to give it in this run. "
                "Do not retry it; finish what you can and report that this step is waiting for approval.")
    return f"The user denied this ({desc}). Do not retry it; ask what they would prefer instead."


def _commands(session: Any) -> Dict[str, Any]:
    def run(args: str) -> str:
        word = args.strip().lower()
        if word == "reset":
            try:
                _store(session).unlink()
            except OSError:
                pass
            session._approved_kinds = set()
            return "Forgot every \"always allow\" decision."
        kept = always_allowed(session)
        return ("Always allowed: " + (", ".join(kept) or "nothing") + "\nKinds that need approval: " +
                ", ".join(k for k, _r, _d in PATTERNS) + ", outside-project\n/approvals reset forgets the always-allow list.")
    return {"approvals": ("Show or reset what is always allowed", run)}


CAPABILITIES = [Capability("approvals", "Command approvals", "Ask before destructive shell commands and edits outside the "
                           "project; answer in the UI or from chat.", "Hermes command approval, OpenClaw exec approvals",
                           gate=_gate, commands=_commands)]
