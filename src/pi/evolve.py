"""Self-evolution: learn from past sessions and propose improvements (Hermes' self-evolution, in a reviewable form).

`omni evolve run` (or `/evolve run` in a session) reads your recent sessions and asks the model one question: what
would have made this work go better next time? The answer is a short list of **proposals**, each one of

    new_skill      a reusable procedure that was worked out by hand and will be needed again
    patch_skill    a correction to an existing skill that was wrong or out of date
    memory         a durable fact for core memory (`MEMORY.md` / `USER.md`)

Nothing is changed automatically. Proposals are saved to `<agent-dir>/evolution/` and you decide:

    omni evolve list                 /evolve
    omni evolve show <id>            /evolve show <id>
    omni evolve apply <id>           /evolve apply <id>       writes the skill or memory entry
    omni evolve discard <id>         /evolve discard <id>

One model call per run, over at most ~24,000 characters of transcript from the last 8 sessions.
"""
from __future__ import annotations

import json
import re
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

MAX_SESSIONS, MAX_CHARS, PER_SESSION = 8, 24_000, 5_000
_NAME_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
INSTRUCTIONS = """You improve a coding assistant by studying how its recent sessions went.

Look for:
- a procedure that was worked out step by step and will clearly be needed again -> propose a new skill
- an existing skill (listed below) whose instructions were wrong, incomplete or out of date -> propose a patch
- a durable fact about the user or the project that the assistant had to rediscover -> propose a memory entry

Be strict. Propose only what the transcripts clearly support, at most 4 proposals, and none if nothing qualifies.
A skill must be specific and actionable (exact commands, paths, checks), not general advice.

Reply with JSON only:
{"proposals": [
  {"kind": "new_skill", "name": "lowercase-with-hyphens", "description": "when to use it, one sentence", "content": "the instructions in markdown", "reason": "what in the sessions shows this is needed"},
  {"kind": "patch_skill", "name": "existing-skill-name", "content": "the full corrected instructions", "reason": "..."},
  {"kind": "memory", "target": "memory" | "user", "content": "one short sentence", "reason": "..."}
]}"""


def evo_dir(agent_dir: Path) -> Path:
    return Path(agent_dir) / "evolution"


def _session_text(path: str, limit: int) -> str:
    rows = []
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                if '"message"' not in line:
                    continue
                try:
                    e = json.loads(line)
                except ValueError:
                    continue
                m = e.get("message") if e.get("type") == "message" else None
                if not m:
                    continue
                role, c = m.get("role"), m.get("content")
                if role in ("user", "assistant"):
                    text = c if isinstance(c, str) else "".join(b.get("text", "") for b in c or [] if isinstance(b, dict) and b.get("type") == "text")
                    calls = [b.get("name") for b in (c if isinstance(c, list) else []) if isinstance(b, dict) and b.get("type") == "toolCall"]
                    if text.strip() or calls:
                        rows.append(f"{role.upper()}: {text.strip()[:1200]}" + (f" [tools: {', '.join(calls)}]" if calls else ""))
                elif role == "toolResult" and m.get("isError"):
                    text = "".join(b.get("text", "") for b in c or [] if isinstance(b, dict) and b.get("type") == "text")
                    rows.append(f"TOOL ERROR ({m.get('toolName')}): {text.strip()[:300]}")
    except OSError:
        return ""
    out = "\n".join(rows)
    return out if len(out) <= limit else out[:limit // 2] + "\n[...]\n" + out[-limit // 2:]


def gather(agent_dir: Path, skip: Optional[str] = None) -> str:
    from .session import SessionManager
    parts, total = [], 0
    for info in SessionManager.list_all_sessions(Path(agent_dir)):
        path = str(info.get("path") or "")
        if not path or path == skip:
            continue
        text = _session_text(path, PER_SESSION)
        if len(text) < 300:
            continue
        block = f"=== session in {info.get('cwd') or '?'} ===\n{text}"
        if total + len(block) > MAX_CHARS:
            break
        parts.append(block)
        total += len(block)
        if len(parts) >= MAX_SESSIONS:
            break
    return "\n\n".join(parts)


def parse(text: str) -> List[Dict[str, Any]]:
    m = re.search(r"\{.*\}", text or "", re.S)
    try:
        data = json.loads(m.group(0)) if m else {}
    except ValueError:
        return []
    out = []
    for p in data.get("proposals") or []:
        if not isinstance(p, dict) or not str(p.get("content") or "").strip():
            continue
        kind = p.get("kind")
        if kind in ("new_skill", "patch_skill"):
            name = str(p.get("name") or "").strip().lower()
            if not _NAME_RE.match(name) or len(name) > 64:
                continue
            p = dict(p, name=name)
        elif kind == "memory":
            p = dict(p, target=p.get("target") if p.get("target") in ("memory", "user") else "memory")
        else:
            continue
        out.append(p)
    return out[:4]


def load(agent_dir: Path) -> List[Dict[str, Any]]:
    out = []
    d = evo_dir(agent_dir)
    for f in sorted(d.glob("*.json")) if d.is_dir() else []:
        try:
            p = json.loads(f.read_text(encoding="utf-8"))
            if isinstance(p, dict) and p.get("id"):
                out.append(p)
        except (OSError, ValueError):
            continue
    return out


def _save(agent_dir: Path, p: Dict[str, Any]) -> None:
    d = evo_dir(agent_dir)
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{p['id']}.json").write_text(json.dumps(p, indent=2, ensure_ascii=False), encoding="utf-8")


def run(session: Any) -> str:
    """One evolution pass using `session`'s model. Returns a human-readable summary."""
    from .ai.stream import complete
    if session.model is None:
        return "No model selected."
    material = gather(session.agent_dir, skip=str(session.sessions.file or ""))
    if not material:
        return "No past sessions with enough content to learn from yet."
    skills = "\n".join(f"- {k.name}: {k.description[:120]}" for k in session.skills) or "(none)"
    msg = complete(session.model, {"systemPrompt": INSTRUCTIONS, "tools": [], "messages": [
        {"role": "user", "content": f"EXISTING SKILLS:\n{skills}\n\nRECENT SESSIONS:\n{material}", "timestamp": int(time.time() * 1000)}]},
        {"apiKey": session.auth.get_api_key(session.model["provider"]), "maxTokens": 4000})
    if msg.get("stopReason") == "error":
        return f"Evolution run failed: {msg.get('errorMessage') or 'model error'}"
    found = parse("".join(b.get("text", "") for b in msg.get("content") or [] if b.get("type") == "text"))
    known = {k.name for k in session.skills}
    pending = [p for p in load(session.agent_dir) if p.get("status") == "pending"]
    saved = []
    for p in found:
        if p["kind"] == "patch_skill" and p["name"] not in known:
            p["kind"] = "new_skill"
            p.setdefault("description", f"Procedure for {p['name'].replace('-', ' ')}")
        if p["kind"] == "new_skill" and p["name"] in known:
            p["kind"] = "patch_skill"
        if any(q["kind"] == p["kind"] and q.get("name") == p.get("name") and q.get("content") == p.get("content") for q in pending):
            continue
        p.update(id=uuid.uuid4().hex[:6], status="pending", created=time.time())
        _save(session.agent_dir, p)
        saved.append(p)
    if not saved:
        return "Evolution run finished: nothing worth proposing."
    return f"{len(saved)} proposal(s) to review:\n" + format_list(saved) + "\nApply one with /evolve apply <id>, or /evolve show <id> to read it."


def format_list(items: List[Dict[str, Any]]) -> str:
    if not items:
        return "No proposals. Run `evolve run` to look for some."
    rows = []
    for p in items:
        what = f"{p['kind'].replace('_', ' ')} {p.get('name') or '(' + p.get('target', 'memory') + ')'}"
        rows.append(f"  {p['id']}  {p.get('status', 'pending'):<9} {what} — {str(p.get('reason') or p.get('description') or '')[:110]}")
    return "\n".join(rows)


def show(agent_dir: Path, pid: str) -> str:
    p = next((x for x in load(agent_dir) if x["id"] == pid), None)
    if p is None:
        return f"No proposal {pid}."
    head = f"{p['id']} · {p['kind']} · {p.get('name') or p.get('target')} · {p.get('status')}\nWhy: {p.get('reason') or '—'}"
    if p.get("description"):
        head += f"\nWhen to use: {p['description']}"
    return head + "\n\n" + str(p["content"])


def apply(agent_dir: Path, pid: str) -> str:
    p = next((x for x in load(agent_dir) if x["id"] == pid), None)
    if p is None:
        return f"No proposal {pid}."
    if p.get("status") != "pending":
        return f"Proposal {pid} was already {p.get('status')}."
    if p["kind"] == "memory":
        from .caps import memory as mem
        d = Path(agent_dir) / "memories"
        target = p.get("target", "memory")
        entries = mem.read_entries(d, target)
        content = str(p["content"]).strip()
        if content not in entries:
            if mem.used(entries + [content]) > mem.LIMITS[target]:
                return f"The {target} store is full; remove something first, then apply again."
            mem.write_entries(d, target, entries + [content])
        where = str(d / mem.FILES[target])
    else:
        d = Path(agent_dir) / "skills" / p["name"]
        f = d / "SKILL.md"
        body = str(p["content"]).strip()
        if p["kind"] == "patch_skill" and f.is_file():
            old = f.read_text(encoding="utf-8")
            m = re.match(r"^(---\r?\n.*?\r?\n---\r?\n)", old, re.S)
            f.with_suffix(".md.bak").write_text(old, encoding="utf-8")  # keep the previous version next to it
            # a proposal usually carries only the instructions: keep the skill's own frontmatter (name, description)
            new = body + "\n" if body.startswith("---") else (m.group(1) if m else "") + "\n" + body + "\n"
            f.write_text(new, encoding="utf-8")
        elif p["kind"] == "patch_skill":
            return f"Skill '{p['name']}' is not one of your own skills in {Path(agent_dir) / 'skills'}; copy it there first."
        else:
            if f.exists():
                return f"A skill named '{p['name']}' already exists; discard this proposal or rename the existing one."
            d.mkdir(parents=True, exist_ok=True)
            desc = str(p.get("description") or f"Procedure for {p['name']}").strip()[:1024]
            f.write_text(f"---\nname: {p['name']}\ndescription: {json.dumps(desc, ensure_ascii=False)}\n---\n\n{body}\n", encoding="utf-8")
        where = str(f)
    p.update(status="applied", applied=time.time())
    _save(agent_dir, p)
    return f"Applied {pid}: {where}"


def discard(agent_dir: Path, pid: str) -> str:
    p = next((x for x in load(agent_dir) if x["id"] == pid), None)
    if p is None:
        return f"No proposal {pid}."
    p["status"] = "discarded"
    _save(agent_dir, p)
    return f"Discarded {pid}."


def command(session: Any, args: str) -> str:
    word, _, rest = args.strip().partition(" ")
    agent = Path(session.agent_dir)
    if word == "run":
        return run(session)
    if word == "show" and rest:
        return show(agent, rest.strip())
    if word == "apply" and rest:
        out = apply(agent, rest.strip())
        session.load_resources()
        return out
    if word == "discard" and rest:
        return discard(agent, rest.strip())
    if word in ("", "list"):
        return format_list([p for p in load(agent) if p.get("status") == "pending"])
    return "Usage: /evolve [run | list | show <id> | apply <id> | discard <id>]"


def cli(argv: List[str], agent_dir: Path) -> int:
    sub = argv[0] if argv else "list"
    if sub == "run":
        import os
        from .agent import AgentSession, SessionOptions
        from .modes import HeadlessUI
        s = AgentSession(SessionOptions(cwd=os.getcwd(), mode="print", no_session=True, no_extensions=True, no_mcp=True,
                                        agent_dir=str(agent_dir)), ui=HeadlessUI())
        s.start()
        print(run(s))
        return 0
    if sub == "list":
        print(format_list([p for p in load(agent_dir) if p.get("status") == "pending"]))
    elif sub == "show" and len(argv) > 1:
        print(show(agent_dir, argv[1]))
    elif sub == "apply" and len(argv) > 1:
        print(apply(agent_dir, argv[1]))
    elif sub == "discard" and len(argv) > 1:
        print(discard(agent_dir, argv[1]))
    else:
        print("usage: omni evolve run | list | show <id> | apply <id> | discard <id>")
        return 2
    return 0
