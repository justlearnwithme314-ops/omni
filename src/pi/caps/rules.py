"""`rules`: project rules, including ones that fire only when the model breaks them (Oh My Pi's rules and TTSR).

A rule is a markdown file with frontmatter:

    ---
    name: no-any
    description: TypeScript code must not use `any`
    condition: ":\\s*any\\b|as any\\b"        # a regular expression (or a list of them)
    ---
    Do not use `any`. Use a precise type, `unknown` with narrowing, or a generic.

- A rule **with `condition`** costs nothing until it is needed: its text is not in the prompt. When the model's
  output (its reply, or the arguments of a tool call such as the content of an edit) matches the pattern, the rule
  is sent to the model as a correction and it continues, fixing what it just wrote. Each rule fires once per
  session (`repeat: true` lets it fire again).
- A rule with `alwaysApply: true` (and no condition) is part of the prompt from the start.
- Any other rule is listed by name and description, and the model reads the file when the topic comes up.

Rules are read from `<agent-dir>/rules/`, and in trusted projects from `.omni/rules/`, `.pi/rules/` and `.omp/rules/`.
`/rules` lists them and what has fired.

Oh My Pi interrupts the stream mid-token and rewinds; here the check runs when the model's message is complete and
the correction arrives before its next step. The effect is the same one turn later, without discarding output.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import Capability

MAX_ALWAYS_CHARS = 6000


def discover(session: Any) -> List[Dict[str, Any]]:
    from .. import resources as res
    roots = [Path(session.agent_dir) / "rules"]
    if session.trusted:
        roots += [Path(session.cwd) / d / "rules" for d in (".omni", ".pi", ".omp")]
    rules: List[Dict[str, Any]] = []
    seen = set()
    for root in roots:
        if not root.is_dir():
            continue
        for f in sorted(root.glob("*.md")):
            try:
                fm, body = res.parse_frontmatter(f.read_text(encoding="utf-8"))
            except OSError:
                continue
            name = str(fm.get("name") or f.stem).strip()
            if not body.strip() or name in seen:
                continue
            seen.add(name)
            raw = fm.get("condition") or fm.get("ttsrTrigger") or fm.get("ttsr_trigger")
            patterns, bad = [], []
            for c in (raw if isinstance(raw, list) else [raw] if raw else []):
                try:
                    patterns.append(re.compile(str(c), re.M))
                except re.error as e:
                    bad.append(f"{c!r}: {e}")
            rules.append({"name": name, "description": str(fm.get("description") or "").strip(), "body": body.strip(), "path": str(f),
                          "patterns": patterns, "bad": bad, "always": bool(fm.get("alwaysApply") or fm.get("always_apply")) and not patterns,
                          "repeat": bool(fm.get("repeat"))})
    return rules


def _state(session: Any) -> Dict[str, Any]:
    st = getattr(session, "_rules", None)
    if st is None:
        st = session._rules = {"rules": discover(session), "unsub": None}
    return st


def fired(session: Any) -> List[str]:
    return [(e.get("data") or {}).get("name") for e in session.sessions.get_branch()
            if e.get("type") == "custom" and e.get("customType") == "rule-fired"]


def _message_text(message: Dict[str, Any]) -> str:
    parts = []
    for b in message.get("content") or []:
        if b.get("type") == "text":
            parts.append(b.get("text", ""))
        elif b.get("type") == "toolCall":
            args = b.get("arguments") or {}
            parts += [v for v in args.values() if isinstance(v, str)]
            parts.append(json.dumps({k: v for k, v in args.items() if not isinstance(v, str)}, ensure_ascii=False))
    return "\n".join(parts)


def check(session: Any, message: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Conditional rules that this assistant message has just broken (and that may still fire)."""
    text = _message_text(message)
    done = set(fired(session))
    hits = []
    for r in _state(session)["rules"]:
        if not r["patterns"] or (r["name"] in done and not r["repeat"]):
            continue
        m = next((m for m in (p.search(text) for p in r["patterns"]) if m), None)
        if m:
            hits.append(dict(r, matched=m.group(0)[:80]))
    return hits


def _setup(session: Any) -> str:
    _unload(session)
    st = _state(session)
    for r in st["rules"]:
        for bad in r["bad"]:
            session.diagnostics.append(f"rule {r['name']}: invalid condition {bad}")

    def on_event(ev: Dict[str, Any]) -> None:
        m = ev.get("message") or {}
        if ev.get("type") != "message_end" or m.get("role") != "assistant" or m.get("stopReason") in ("error", "aborted"):
            return
        try:
            for r in check(session, m):
                session.sessions.append_custom("rule-fired", {"name": r["name"], "matched": r["matched"]})
                session.steer(f"[Rule `{r['name']}` applies to what you just wrote (matched: {r['matched']!r}). "
                              f"Correct it now, then continue with the task.]\n\n{r['body']}")
                session._notify({"type": "notice", "level": "info", "message": f"Rule {r['name']} fired; the model is correcting course."})
        except Exception:
            pass
    st["unsub"] = session.subscribe(on_event)
    always = [f"## Rule: {r['name']}\n{r['body']}" for r in st["rules"] if r["always"]]
    listed = [f"- {r['name']}: {r['description'] or '(no description)'} ({r['path']})" for r in st["rules"]
              if not r["always"] and not r["patterns"]]
    out = "\n\n".join(always)[:MAX_ALWAYS_CHARS]
    if listed:
        out += ("\n\n" if out else "") + "Project rules (read the file when the topic applies):\n" + "\n".join(listed)
    return out


def _unload(session: Any) -> None:
    st = getattr(session, "_rules", None)
    if st and st.get("unsub"):
        st["unsub"]()
    session._rules = None


def describe(session: Any) -> str:
    rules = _state(session)["rules"]
    if not rules:
        return f"No rules. Add markdown files to {Path(session.agent_dir) / 'rules'} or the project's .omni/rules."
    done = fired(session)
    rows = []
    for r in rules:
        kind = "always in the prompt" if r["always"] else f"fires on /{r['patterns'][0].pattern}/" if r["patterns"] else "listed, read on demand"
        rows.append(f"{r['name']} — {kind}" + (f" · fired {done.count(r['name'])}×" if r["name"] in done else "") +
                    (f" · INVALID condition {r['bad'][0]}" if r["bad"] else "") + f"\n   {r['description'] or r['path']}")
    return "\n".join(rows)


CAPABILITIES = [Capability("rules", "Rules", "Project rules: always-on, listed, or fired as a correction when the model's "
                           "output matches a pattern.", "Oh My Pi rules and time-travelling stream rules", prompt=_setup,
                           commands=lambda s: {"rules": ("List rules and which have fired", lambda _a: describe(s))},
                           unload=_unload)]
