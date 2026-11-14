"""`quiet-output` and `secrets`: clean up what tools hand back before the model reads it.

`quiet-output` (OpenClaw's tokenjuice) shrinks noisy shell output without changing its meaning:
  - terminal colour codes and carriage-return progress redraws are removed (only the final state of a progress bar
    stays);
  - runs of identical or near-identical lines (the same line differing only in numbers, such as download progress
    or `test 12/800 passed`) collapse to the first, a count, and the last;
  - long runs of blank lines collapse to one.
The exit code line and everything else stay as they were. Typical install and test logs lose 60-90% of their size.

`secrets` (Oh My Pi's secret redaction) replaces secret values in tool results with `[REDACTED:NAME]` before they
reach the model or the session file: the values of environment variables whose names look like credentials
(`*_KEY`, `*_TOKEN`, `*_SECRET`, `*PASSWORD*`, ...), anything listed in `<agent-dir>/secrets.txt` (one
`NAME=value` per line), and well-known token shapes (GitHub, OpenAI / Anthropic, AWS, Slack, Google, private key
blocks). It protects against accidents such as `cat .env`; it does not restore values when the model needs them,
so commands should keep reading secrets from the environment.
"""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from . import Capability

_ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b[@-Z\\-_]")
_NUM = re.compile(r"\d+(?:[.,:]\d+)*%?")
MIN_RUN = 4          # collapse runs of at least this many similar lines
MIN_SAVING = 200     # leave short outputs alone


def compact(text: str) -> str:
    text = _ANSI.sub("", text)
    lines: List[str] = []
    for raw in text.split("\n"):
        if "\r" in raw:  # a progress bar redrawn in place: keep what was last on screen
            parts = [p for p in raw.split("\r") if p.strip()]
            raw = parts[-1] if parts else ""
        lines.append(raw.rstrip())
    out: List[str] = []
    i, n = 0, len(lines)
    while i < n:
        line = lines[i]
        if not line.strip():
            j = i
            while j < n and not lines[j].strip():
                j += 1
            out.append("")
            i = j
            continue
        shape = _NUM.sub("#", line)
        j = i + 1
        while j < n and lines[j].strip() and _NUM.sub("#", lines[j]) == shape:
            j += 1
        run = j - i
        if run >= MIN_RUN:
            out.append(line)
            out.append(f"  [... {run - 2} similar lines ...]")
            out.append(lines[j - 1])
        else:
            out.extend(lines[i:j])
        i = j
    return "\n".join(out).strip("\n")


def _quiet_after(session: Any, tool_name: str, args: Dict[str, Any], result: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    if tool_name not in ("bash", "process"):
        return None
    content = list(result.get("content") or [])
    changed, saved = False, 0
    for k, b in enumerate(content):
        if b.get("type") != "text" or len(b.get("text", "")) < 400:
            continue
        new = compact(b["text"])
        if len(b["text"]) - len(new) >= MIN_SAVING:
            saved += len(b["text"]) - len(new)
            content[k] = dict(b, text=new)
            changed = True
    if not changed:
        return None
    details = result.get("details")
    details = {**details, "compacted": saved} if isinstance(details, dict) else details
    return {**result, "content": content, "details": details}


# ------------------------------------------------------------------------------------------- secrets
_NAME = re.compile(r"(?:^|_)(?:KEY|TOKEN|SECRET|PASSWORD|PASSWD|PWD|CREDENTIALS?|API_?KEY|AUTH)(?:_|$)|PASSWORD|SECRET", re.I)
_SHAPES: List[Tuple[str, re.Pattern]] = [
    ("GITHUB_TOKEN", re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{40,})\b")),
    ("ANTHROPIC_KEY", re.compile(r"\bsk-ant-[A-Za-z0-9_-]{20,}")),
    ("OPENAI_KEY", re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9_-]{32,}")),
    ("AWS_ACCESS_KEY", re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b")),
    ("SLACK_TOKEN", re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}")),
    ("GOOGLE_KEY", re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b")),
    ("GOOGLE_OAUTH", re.compile(r"\bya29\.[0-9A-Za-z_-]{20,}")),
    ("BEARER", re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/-]{24,}=*")),
    ("PRIVATE_KEY", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.S)),
]
MIN_SECRET_LEN = 8


def known_secrets(session: Any) -> List[Tuple[str, str]]:
    """(name, value) pairs to mask, longest value first so a short one never splits a long one."""
    found: Dict[str, str] = {}
    for name, value in os.environ.items():
        if _NAME.search(name) and len(value) >= MIN_SECRET_LEN and not value.lower() in ("true", "false"):
            found[value] = name
    try:
        for line in (Path(session.agent_dir) / "secrets.txt").read_text(encoding="utf-8").splitlines():
            name, sep, value = line.strip().partition("=")
            if sep and len(value.strip()) >= MIN_SECRET_LEN and not line.lstrip().startswith("#"):
                found[value.strip()] = name.strip() or "SECRET"
    except OSError:
        pass
    return sorted(((n, v) for v, n in found.items()), key=lambda nv: -len(nv[1]))


def redact(text: str, secrets: List[Tuple[str, str]]) -> Tuple[str, int]:
    count = 0
    for name, value in secrets:
        if value in text:
            count += text.count(value)
            text = text.replace(value, f"[REDACTED:{name}]")
    for name, rx in _SHAPES:
        text, n = rx.subn(f"[REDACTED:{name}]", text)
        count += n
    return text, count


def _secrets_after(session: Any, tool_name: str, args: Dict[str, Any], result: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    cache = getattr(session, "_secret_values", None)
    if cache is None:
        cache = session._secret_values = known_secrets(session)
    content = list(result.get("content") or [])
    total = 0
    for k, b in enumerate(content):
        if b.get("type") == "text" and b.get("text"):
            new, n = redact(b["text"], cache)
            if n:
                total += n
                content[k] = dict(b, text=new)
    if not total:
        return None
    details = result.get("details")
    details = {**details, "redacted": total} if isinstance(details, dict) else details
    return {**result, "content": content, "details": details}


def _secrets_prompt(session: Any) -> str:
    session._secret_values = None  # re-read the environment and secrets.txt with the capability
    return ""


CAPABILITIES = [
    Capability("quiet-output", "Quiet shell output", "Strip colour codes and progress redraws and collapse repeated lines in "
               "shell output before the model reads it.", "OpenClaw tokenjuice", after_tool=_quiet_after),
    Capability("secrets", "Secret redaction", "Mask credentials (environment values, secrets.txt, known token shapes) in tool "
               "results before they reach the model or the session file.", "Oh My Pi secret redaction",
               prompt=_secrets_prompt, after_tool=_secrets_after),
]
