"""`memory-reflect`: turn a finished conversation into lasting memory (Letta's sleep-time memory agent, Oh My Pi's
`reflect`, Hermes' memory nudges).

`/reflect` makes one extra model call over the recent conversation and asks for the few facts worth keeping:
durable preferences and conventions go to core memory (`memory`), longer reference material to the archive
(`memory-archive`, if on). The model is told what is already stored, so it adds only what is new, and the stores'
size limits still apply. With `"memory": {"reflect_on_exit": true}` in the profile it also runs when a session
with at least four exchanges ends.

It costs one model call per reflection (input: up to about 12,000 characters of transcript).
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any, Dict, List

from . import Capability

MAX_TRANSCRIPT = 12_000
INSTRUCTIONS = """You maintain an assistant's long-term memory. Read the conversation and decide what is worth remembering in FUTURE sessions.

Keep only durable facts: the user's preferences and working style, project conventions, decisions with their reasons, fixes for problems that will recur, facts about the environment. Skip anything that only mattered for this task, and anything already stored.

Reply with JSON only, no prose:
{"core": [{"target": "memory" | "user", "content": "one short sentence"}], "archive": [{"content": "a self-contained passage", "tags": ["tag"]}]}

Use "user" for facts about the person, "memory" for everything else. At most 4 core entries and 3 archive passages. If nothing is worth keeping, reply {"core": [], "archive": []}."""


def transcript(session: Any, limit: int = MAX_TRANSCRIPT) -> str:
    rows = []
    for e in session.sessions.get_branch():
        if e.get("type") != "message":
            continue
        m = e["message"]
        role = m.get("role")
        if role not in ("user", "assistant"):
            continue
        c = m.get("content")
        text = c if isinstance(c, str) else "".join(b.get("text", "") for b in c or [] if isinstance(b, dict) and b.get("type") == "text")
        if text.strip():
            rows.append(f"{role.upper()}: {text.strip()}")
    out = "\n\n".join(rows)
    return out if len(out) <= limit else "[earlier conversation omitted]\n\n" + out[-limit:]


def parse_reply(text: str) -> Dict[str, List[Dict[str, Any]]]:
    m = re.search(r"\{.*\}", text or "", re.S)
    try:
        data = json.loads(m.group(0)) if m else {}
    except ValueError:
        data = {}
    core = [x for x in data.get("core") or [] if isinstance(x, dict) and str(x.get("content") or "").strip()]
    arch = [x for x in data.get("archive") or [] if isinstance(x, dict) and str(x.get("content") or "").strip()]
    return {"core": core[:4], "archive": arch[:3]}


def reflect(session: Any) -> str:
    from ..ai.stream import complete
    from . import memory as mem
    if session.model is None:
        return "No model selected."
    convo = transcript(session)
    if len(convo) < 200:
        return "Not enough conversation to reflect on yet."
    d = mem.memory_dir(session)
    blocks = mem.blocks(session)
    stored = "\n".join(f"[{label}] " + " | ".join(mem.read_entries(d, label, spec["file"])) for label, spec in blocks.items()
                       if mem.read_entries(d, label, spec["file"]))
    prompt = f"ALREADY STORED:\n{stored or '(nothing)'}\n\nCONVERSATION:\n{convo}"
    msg = complete(session.model, {"systemPrompt": INSTRUCTIONS, "tools": [],
                                   "messages": [{"role": "user", "content": prompt, "timestamp": int(time.time() * 1000)}]},
                   {"apiKey": session.auth.get_api_key(session.model["provider"]), "maxTokens": 1200})
    if msg.get("stopReason") == "error":
        return f"Reflection failed: {msg.get('errorMessage') or 'model error'}"
    found = parse_reply("".join(b.get("text", "") for b in msg.get("content") or [] if b.get("type") == "text"))
    saved, skipped = [], []
    for item in found["core"]:
        target = item.get("target") if item.get("target") in blocks else "memory"
        spec, content = blocks[target], str(item["content"]).strip()
        entries = mem.read_entries(d, target, spec["file"])
        if content in entries:
            continue
        if mem.used(entries + [content]) > spec["limit"]:
            skipped.append(f"{target} is full")
            continue
        mem.write_entries(d, target, entries + [content], spec["file"])
        saved.append(f"[{target}] {content}")
    if found["archive"] and session.caps.is_enabled("memory-archive"):
        from . import archive
        for item in found["archive"]:
            tags = [str(t).lower() for t in item.get("tags") or []][:5]
            if archive.insert(session, str(item["content"]).strip()[:archive.MAX_PASSAGE], tags, source=session.cwd) > 0:
                saved.append(f"[archive] {str(item['content']).strip()[:90]}")
    session.sessions.append_custom("reflect", {"saved": saved, "skipped": skipped})
    if not saved:
        return "Reflected: nothing new worth keeping." + (f" ({'; '.join(sorted(set(skipped)))})" if skipped else "")
    return f"Reflected: saved {len(saved)} item(s) for future sessions.\n" + "\n".join(f"  {s}" for s in saved) + \
        (f"\n  skipped: {'; '.join(sorted(set(skipped)))}" if skipped else "")


def _setup(session: Any) -> str:
    _unload(session)
    opt = session.profile.get("memory") if isinstance(session.profile.get("memory"), dict) else {}
    if not opt.get("reflect_on_exit"):
        return ""

    def on_event(ev: Dict[str, Any]) -> None:
        if ev.get("type") != "session_shutdown" or ev.get("reason") == "reload":
            return
        users = sum(1 for e in session.sessions.get_branch() if e.get("type") == "message" and e["message"].get("role") == "user")
        already = any(e.get("type") == "custom" and e.get("customType") == "reflect" for e in session.sessions.get_branch()[-3:])
        if users >= 4 and not already:
            try:
                reflect(session)
            except Exception:
                pass
    session._reflect_unsub = session.subscribe(on_event)
    return ""


def _unload(session: Any) -> None:
    unsub = getattr(session, "_reflect_unsub", None)
    if unsub:
        unsub()
    session._reflect_unsub = None


CAPABILITIES = [Capability("memory-reflect", "Memory reflection", "/reflect distils the conversation into lasting memory "
                           "with one extra model call.", "Letta sleep-time agent, Oh My Pi reflect, Hermes memory nudges",
                           prompt=_setup, commands=lambda s: {"reflect": ("Save what is worth remembering from this conversation",
                                                                           lambda _a: reflect(s))},
                           requires=["memory"])]
