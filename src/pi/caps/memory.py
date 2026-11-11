"""`memory`: small persistent notes that survive across sessions (Hermes' built-in memory, same file format).

Two bounded stores under `<agent-dir>/memories/`, entries separated by a `§` line exactly as Hermes writes them, so
the files can be copied to or from `~/.hermes/memories`:

    MEMORY.md   the agent's notes: environment facts, conventions, lessons learned      (2200 characters)
    USER.md     what it knows about the user: preferences, role, communication style      (1375 characters)

The limits are the point: the whole store is injected into the system prompt, so it has a fixed, small context
cost. The prompt copy is a snapshot taken when the session starts (edits show up next session), which keeps the
provider's prompt cache valid for the whole conversation.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Dict, List

from ..tools.base import ToolDef, ToolError, file_mutation_lock, text_result
from . import Capability

SEP = "\n§\n"
LIMITS = {"memory": 2200, "user": 1375}
FILES = {"memory": "MEMORY.md", "user": "USER.md"}
TITLES = {"memory": "MEMORY (your personal notes)", "user": "USER PROFILE (who the user is)"}


def memory_dir(session: Any) -> Path:
    custom = (session.profile.get("memory") or {}).get("dir") if isinstance(session.profile.get("memory"), dict) else None
    return Path(os.path.expanduser(custom)) if custom else Path(session.agent_dir) / "memories"


def read_entries(d: Path, target: str) -> List[str]:
    try:
        raw = (d / FILES[target]).read_text(encoding="utf-8")
    except OSError:
        return []
    return [e.strip() for e in raw.split("§") if e.strip()]


def write_entries(d: Path, target: str, entries: List[str]) -> None:
    d.mkdir(parents=True, exist_ok=True)
    p = d / FILES[target]
    tmp = p.with_suffix(".tmp")
    tmp.write_text(SEP.join(entries), encoding="utf-8")
    os.replace(tmp, p)


def used(entries: List[str]) -> int:
    return len(SEP.join(entries))


def _block(target: str, entries: List[str]) -> str:
    n, limit = used(entries), LIMITS[target]
    bar = "═" * 46
    return f"{bar}\n{TITLES[target]} [{n * 100 // limit}% — {n}/{limit} chars]\n{bar}\n" + SEP.join(entries)


def _prompt(session: Any) -> str:
    d = memory_dir(session)
    blocks = [_block(t, e) for t in ("memory", "user") for e in [read_entries(d, t)] if e]
    return "\n\n".join(blocks)


def _tools(session: Any) -> Dict[str, ToolDef]:
    def execute(_id, p, signal=None, on_update=None, ctx=None):
        action, target = p.get("action"), p.get("target") or "memory"
        if target not in LIMITS:
            raise ToolError("target must be 'memory' or 'user'")
        d = memory_dir(session)
        content = str(p.get("content") or "").strip()
        old = str(p.get("old_text") or "").strip()
        with file_mutation_lock(str(d / FILES[target])):
            entries = read_entries(d, target)
            if action == "add":
                if not content:
                    raise ToolError("content is required for add")
                if content in entries:
                    return text_result("Already stored; nothing added.", {"entries": len(entries)})
                new = entries + [content]
            elif action in ("replace", "remove"):
                if not old:
                    raise ToolError(f"old_text is required for {action}: a short unique part of the entry")
                hits = [i for i, e in enumerate(entries) if old in e]
                if not hits:
                    raise ToolError(f"no entry contains '{old}'. Entries:\n" + "\n".join(f"- {e[:80]}" for e in entries))
                if len(hits) > 1:
                    raise ToolError(f"'{old}' matches {len(hits)} entries; use a more specific old_text")
                new = list(entries)
                if action == "remove":
                    del new[hits[0]]
                else:
                    if not content:
                        raise ToolError("content is required for replace")
                    new[hits[0]] = content
            else:
                raise ToolError("action must be add, replace or remove")
            if used(new) > LIMITS[target]:
                raise ToolError(f"{target} store would be {used(new)}/{LIMITS[target]} chars. Remove or shorten an entry "
                                "first:\n" + "\n".join(f"- {e[:80]}" for e in entries))
            write_entries(d, target, new)
        return text_result(f"{action} ok. {target}: {used(new)}/{LIMITS[target]} chars, {len(new)} entries. "
                           "(The prompt copy refreshes next session.)", {"target": target, "entries": len(new), "chars": used(new)})

    return {"memory": ToolDef(
        name="memory", label="Memory",
        description="Save durable facts across sessions. target 'memory' = your notes (environment, conventions, "
                    "lessons learned); 'user' = who the user is and how they like to work. Stores are small: keep "
                    "entries compact, merge related facts, and replace or remove stale ones. Never store secrets.",
        parameters={"type": "object", "properties": {
            "action": {"type": "string", "enum": ["add", "replace", "remove"]},
            "target": {"type": "string", "enum": ["memory", "user"]},
            "content": {"type": "string"}, "old_text": {"type": "string", "description": "unique part of the entry to replace/remove"}},
            "required": ["action", "target"]},
        execute=execute, prompt_snippet="Remember durable facts across sessions",
        prompt_guidelines=["Save to memory when you learn something that will matter in future sessions (a user "
                           "preference, a project convention, a fix for a recurring problem); skip one-off details."])}


CAPABILITIES = [Capability("memory", "Persistent memory", "Bounded cross-session notes (agent notes + user profile) "
                           "injected into the prompt.", "Hermes memory (MEMORY.md / USER.md)", tools=_tools, prompt=_prompt)]
