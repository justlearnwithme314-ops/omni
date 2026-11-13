"""`hooks`: shell-command hooks, in Claude Code's `hooks` format (Hermes calls the same idea shell hooks).

Any program in any language can observe or veto what the agent does. Configure them in

    <agent-dir>/hooks.json          yours, every project
    <project>/.omni/hooks.json      project hooks (trusted projects only)
    ~/.claude/settings.json, <project>/.claude/settings.json, .claude/settings.local.json
                                    only when the profile sets  "hooks": {"claude": true}

    {"hooks": {"PreToolUse": [{"matcher": "bash|write", "hooks": [{"type": "command", "command": "python guard.py"}]}],
               "UserPromptSubmit": [{"hooks": [{"type": "command", "command": "git status --short"}]}]}}

Events: `PreToolUse` (may block), `PostToolUse` (may add feedback), `UserPromptSubmit` (stdout becomes context; may
block), `SessionStart` (stdout becomes a prompt section), `Stop` (observer). The hook gets a JSON object on stdin
(`session_id`, `cwd`, `hook_event_name`, `tool_name`, `tool_input`, `tool_response`, `prompt`). Exit code 2 blocks,
with stderr as the reason shown to the model; stdout may instead be JSON `{"decision": "block", "reason": "..."}`.
`matcher` is a regular expression tested against the tool name in both spellings (`bash` and `Bash`).
"""
from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from . import Capability

EVENTS = ("PreToolUse", "PostToolUse", "UserPromptSubmit", "SessionStart", "Stop")
CLAUDE_NAMES = {"bash": "Bash", "read": "Read", "edit": "Edit", "write": "Write", "grep": "Grep", "find": "Glob", "ls": "LS",
                "fetch": "WebFetch", "web_search": "WebSearch", "task": "Task", "todo": "TodoWrite"}
DEFAULT_TIMEOUT = 60


def _read(path: Path) -> Dict[str, List[Dict[str, Any]]]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    hooks = data.get("hooks") if isinstance(data, dict) else None
    return {k: v for k, v in hooks.items() if k in EVENTS and isinstance(v, list)} if isinstance(hooks, dict) else {}


def load_config(session: Any) -> Dict[str, List[Dict[str, Any]]]:
    files = [Path(session.agent_dir) / "hooks.json"]
    if session.trusted:
        files.append(Path(session.cwd) / ".omni" / "hooks.json")
    opt = session.profile.get("hooks") if isinstance(session.profile.get("hooks"), dict) else {}
    if opt.get("claude"):
        files.append(Path.home() / ".claude" / "settings.json")
        if session.trusted:
            files += [Path(session.cwd) / ".claude" / "settings.json", Path(session.cwd) / ".claude" / "settings.local.json"]
    merged: Dict[str, List[Dict[str, Any]]] = {}
    for f in files:
        for event, groups in _read(f).items():
            merged.setdefault(event, []).extend(g for g in groups if isinstance(g, dict))
    for f, plugin_root in session.caps.collect("hook_files"):  # hooks shipped inside plugins
        for event, groups in _read(Path(f)).items():
            for g in groups:
                if isinstance(g, dict):
                    g = dict(g, hooks=[dict(h, command=str(h.get("command", "")).replace("${CLAUDE_PLUGIN_ROOT}", str(plugin_root or "")))
                                       for h in g.get("hooks") or [] if isinstance(h, dict)])
                    merged.setdefault(event, []).append(g)
    return merged


def _commands(cfg: Dict[str, List[Dict[str, Any]]], event: str, tool: Optional[str] = None) -> List[Tuple[str, float]]:
    out = []
    for group in cfg.get(event, []):
        matcher = group.get("matcher")
        if tool is not None and matcher not in (None, "", "*"):
            try:
                rx = re.compile(str(matcher), re.I)
            except re.error:
                continue
            if not (rx.fullmatch(tool) or rx.fullmatch(CLAUDE_NAMES.get(tool, tool))):
                continue
        for h in group.get("hooks") or []:
            if isinstance(h, dict) and h.get("type", "command") == "command" and h.get("command"):
                out.append((str(h["command"]), float(h.get("timeout") or DEFAULT_TIMEOUT)))
    return out


def run_hook(session: Any, command: str, timeout: float, payload: Dict[str, Any]) -> Tuple[int, str, str]:
    env = dict(os.environ, OMNI_PROJECT_DIR=session.cwd, CLAUDE_PROJECT_DIR=session.cwd)
    try:
        p = subprocess.run(command, shell=True, cwd=session.cwd, input=json.dumps(payload, default=str), capture_output=True,
                           text=True, timeout=timeout, env=env, encoding="utf-8", errors="replace")
        return p.returncode, (p.stdout or "").strip(), (p.stderr or "").strip()
    except subprocess.TimeoutExpired:
        return 1, "", f"hook timed out after {timeout:.0f}s: {command}"
    except OSError as e:
        return 1, "", f"hook failed to start: {e}"


def _decision(code: int, out: str, err: str) -> Tuple[bool, str]:
    """(blocked, text). Exit 2 blocks with stderr; JSON stdout {"decision": "block"} blocks with its reason."""
    if code == 2:
        return True, err or out or "blocked by a hook"
    if out.startswith("{"):
        try:
            d = json.loads(out)
            if isinstance(d, dict):
                if d.get("decision") == "block" or d.get("continue") is False:
                    return True, str(d.get("reason") or d.get("stopReason") or "blocked by a hook")
                extra = (d.get("hookSpecificOutput") or {}).get("additionalContext") if isinstance(d.get("hookSpecificOutput"), dict) else None
                return False, str(extra or d.get("systemMessage") or "")
        except ValueError:
            pass
    return False, out


def _payload(session: Any, event: str, **extra: Any) -> Dict[str, Any]:
    return {"session_id": session.sessions.session_id, "cwd": session.cwd, "hook_event_name": event,
            "transcript_path": str(session.sessions.file or ""), **extra}


def _tool_input(args: Dict[str, Any]) -> Dict[str, Any]:
    d = dict(args or {})
    if "path" in d and "file_path" not in d:
        d["file_path"] = d["path"]  # the key Claude Code hooks look for
    return d


def _state(session: Any) -> Dict[str, Any]:
    st = getattr(session, "_shell_hooks", None)
    if st is None:
        st = {"cfg": load_config(session), "start": "", "unsub": None}
        session._shell_hooks = st
    return st


def _gate(session: Any, tool_name: str, args: Dict[str, Any]) -> Optional[str]:
    cfg = _state(session)["cfg"]
    payload = _payload(session, "PreToolUse", tool_name=CLAUDE_NAMES.get(tool_name, tool_name), tool_input=_tool_input(args))
    for cmd, timeout in _commands(cfg, "PreToolUse", tool_name):
        blocked, text = _decision(*run_hook(session, cmd, timeout, payload))
        if blocked:
            return f"Blocked by a PreToolUse hook: {text}"
    return None


def _after_tool(session: Any, tool_name: str, args: Dict[str, Any], result: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    cfg = _state(session)["cfg"]
    cmds = _commands(cfg, "PostToolUse", tool_name)
    if not cmds:
        return None
    text = "".join(b.get("text", "") for b in result.get("content") or [] if b.get("type") == "text")
    payload = _payload(session, "PostToolUse", tool_name=CLAUDE_NAMES.get(tool_name, tool_name), tool_input=_tool_input(args),
                       tool_response={"output": text, "is_error": bool(result.get("isError"))})
    notes = []
    for cmd, timeout in cmds:
        blocked, msg = _decision(*run_hook(session, cmd, timeout, payload))
        if blocked and msg:
            notes.append(msg)
    if not notes:
        return None
    feedback = "\n\n[PostToolUse hook feedback]\n" + "\n".join(notes)
    return {**result, "content": list(result.get("content") or []) + [{"type": "text", "text": feedback}]}


def _on_prompt(session: Any, text: str) -> Any:
    cfg = _state(session)["cfg"]
    payload = _payload(session, "UserPromptSubmit", prompt=text)
    context = []
    for cmd, timeout in _commands(cfg, "UserPromptSubmit"):
        blocked, msg = _decision(*run_hook(session, cmd, timeout, payload))
        if blocked:
            return {"block": msg}
        if msg:
            context.append(msg)
    return "\n".join(context) if context else None


def _prompt(session: Any) -> str:
    session._shell_hooks = None  # (re)load the configuration with the capability
    st = _state(session)
    out = []
    for cmd, timeout in _commands(st["cfg"], "SessionStart"):
        code, stdout, _err = run_hook(session, cmd, timeout, _payload(session, "SessionStart", source="startup"))
        if code == 0 and stdout:
            out.append(_decision(code, stdout, "")[1] or stdout)

    def on_event(ev: Dict[str, Any]) -> None:
        if ev.get("type") == "agent_end":
            for cmd, timeout in _commands(st["cfg"], "Stop"):
                run_hook(session, cmd, timeout, _payload(session, "Stop", stop_hook_active=False))
    if st["cfg"].get("Stop"):
        st["unsub"] = session.subscribe(on_event)
    return "\n\n".join(out)[:4000]


def _unload(session: Any) -> None:
    st = getattr(session, "_shell_hooks", None)
    if st and st.get("unsub"):
        st["unsub"]()
    session._shell_hooks = None


def summary(session: Any) -> Dict[str, int]:
    return {event: len(_commands(_state(session)["cfg"], event)) for event in EVENTS}


CAPABILITIES = [Capability("hooks", "Shell hooks", "Run your own commands before/after tools and on each prompt; they "
                           "can block or add context (Claude Code hooks format).", "Claude Code hooks, Hermes shell hooks",
                           prompt=_prompt, gate=_gate, after_tool=_after_tool, on_prompt=_on_prompt, unload=_unload,
                           group="host")]
