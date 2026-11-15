"""`persona`, `commit` and `auto-title`: three conveniences.

`persona` (Hermes personalities, OpenClaw SOUL.md): a named voice and working style for the agent. `/persona` lists
them, `/persona teacher` switches, `/persona off` returns to the default. Built in: `concise`, `teacher`,
`reviewer`, `pair`. Your own are Markdown files in `<agent-dir>/personas/<name>.md` (a file named like a built-in
replaces it); `"persona": "name"` in a profile sets the default. The text is added to the system prompt, so a
persona costs its own length on every request: keep them short.

`commit` (Oh My Pi's commit): `/commit` reads the staged changes (or, when nothing is staged, all changes to
tracked files), asks the model for a commit message in the repository's own style, shows it and commits when you
confirm. `/commit <hint>` passes a hint ("this fixes #12"). Without a window to confirm in, it only prints the
message, unless you add `--yes`. One model call, with at most about 24,000 characters of diff.

`auto-title` (Hermes, OpenClaw): after the first exchange of an unnamed session, a tiny model call (at most 24
output tokens) names it, so `/resume` shows something better than the first words you typed.
"""
from __future__ import annotations

import re
import subprocess
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from . import Capability

BUILTIN_PERSONAS = {
    "concise": "Answer in as few words as the question allows. No preamble, no recap, no offers of further help. Code over prose.",
    "teacher": "Explain as you go: say why before how, define a term the first time you use it, and check understanding with one "
               "short question when a step is subtle. Prefer a small example to an abstract rule.",
    "reviewer": "Act as a strict but fair code reviewer: look for bugs, unclear names, missing tests and needless complexity "
                "before anything else, cite file and line, and separate must-fix from nice-to-have.",
    "pair": "Work as a pair programmer: think aloud briefly, propose the next small step and wait for a go-ahead before large "
            "changes, and say so plainly when you are unsure.",
}
MAX_PERSONA = 4000


def personas(session: Any) -> Dict[str, str]:
    table = dict(BUILTIN_PERSONAS)
    folder = Path(session.agent_dir) / "personas"
    if folder.is_dir():
        for f in sorted(folder.glob("*.md")):
            try:
                text = f.read_text(encoding="utf-8").strip()
            except OSError:
                continue
            if text:
                table[f.stem.lower()] = text[:MAX_PERSONA]
    return table


def active_persona(session: Any) -> Optional[str]:
    chosen = getattr(session, "_persona", None)
    if chosen is None:
        chosen = session.profile.get("persona")
    name = str(chosen or "").lower()
    return name if name and name != "off" and name in personas(session) else None


def _persona_prompt(session: Any) -> str:
    name = active_persona(session)
    return f"Persona ({name}): {personas(session)[name]}" if name else ""


def _persona_command(session: Any, args: str) -> str:
    table = personas(session)
    arg = args.strip().lower()
    if not arg or arg == "list":
        current = active_persona(session)
        return "\n".join(f"{'*' if n == current else ' '} {n:<12} {t.splitlines()[0][:80]}" for n, t in table.items()) + \
            "\n/persona <name> to switch, /persona off for none. Your own: <agent-dir>/personas/<name>.md"
    if arg == "off":
        session._persona = "off"
        return "Persona off."
    if arg not in table:
        return f"No persona '{arg}'. Available: {', '.join(table)}"
    session._persona = arg
    return f"Persona: {arg}."


# ------------------------------------------------------------------------------------------- commit
MAX_DIFF = 24_000
COMMIT_SYSTEM = """Write a git commit message for the diff. First line: imperative mood, at most 72 characters, no trailing period; follow the style of the recent commit subjects shown (for example a `type: subject` prefix if they use one). If the change needs explaining, add a blank line and a short body saying what changed and why. Do not mention files that are obvious from the diff, and do not invent issue numbers. Reply with the commit message only."""


def _git(session: Any, *args: str, input_text: Optional[str] = None) -> Tuple[int, str]:
    try:
        r = subprocess.run(["git", *args], cwd=session.cwd, capture_output=True, text=True, encoding="utf-8", errors="replace",
                           timeout=60, input=input_text, stdin=None if input_text is not None else subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired) as e:
        return 1, str(e)
    return r.returncode, (r.stdout or "") + (r.stderr or "")


def commit_message(session: Any, diff: str, hint: str = "") -> str:
    from ..ai.stream import complete
    _code, log = _git(session, "log", "-8", "--pretty=%s")
    if len(diff) > MAX_DIFF:
        diff = diff[:MAX_DIFF] + f"\n[... diff cut at {MAX_DIFF:,} characters ...]"
    user = f"RECENT COMMIT SUBJECTS:\n{log.strip() or '(none yet)'}\n\n" + (f"NOTE FROM THE AUTHOR: {hint}\n\n" if hint else "") + f"DIFF:\n{diff}"
    msg = complete(session.model, {"systemPrompt": COMMIT_SYSTEM, "tools": [],
                                   "messages": [{"role": "user", "content": user, "timestamp": int(time.time() * 1000)}]},
                   {"apiKey": session.auth.get_api_key(session.model["provider"]), "maxTokens": 400})
    if msg.get("stopReason") == "error":
        raise RuntimeError(msg.get("errorMessage") or "model error")
    text = "".join(b.get("text", "") for b in msg.get("content") or [] if b.get("type") == "text").strip()
    text = re.sub(r"^```[a-z]*\n|\n```$", "", text).strip()
    return text


def _commit_command(session: Any, args: str) -> str:
    yes = "--yes" in args.split()
    hint = " ".join(a for a in args.split() if a != "--yes")
    if session.model is None:
        return "No model selected."
    code, _out = _git(session, "rev-parse", "--is-inside-work-tree")
    if code != 0:
        return "This folder is not a git repository."
    _c, diff = _git(session, "diff", "--cached")
    staged = bool(diff.strip())
    if not staged:
        _c, diff = _git(session, "diff")
        if not diff.strip():
            return "Nothing to commit: no staged changes and no changes to tracked files. (New files must be staged with git add.)"
    try:
        message = commit_message(session, diff, hint)
    except Exception as e:
        return f"Could not write a commit message: {e}"
    if not message:
        return "The model returned an empty commit message."
    scope = "the staged changes" if staged else "all changes to tracked files"
    ui = session.ui
    if getattr(ui, "has_ui", False) and not yes:
        if not ui.confirm("Commit " + scope + "?", message):
            return "Not committed. The proposed message was:\n" + message
    elif not yes:
        return f"Proposed message for {scope} (run /commit --yes to commit with it):\n{message}"
    code, out = _git(session, "commit", *([] if staged else ["-a"]), "-F", "-", input_text=message)
    if code != 0:
        return f"git commit failed:\n{out.strip()[:800]}"
    return f"Committed {scope}:\n{message}"


# ------------------------------------------------------------------------------------------- auto-title
TITLE_SYSTEM = "Give this conversation a title of 3 to 6 words that says what it is about. Reply with the title only: no quotes, no period."


def make_title(session: Any) -> str:
    from ..ai.stream import complete
    from .reflect import transcript
    convo = transcript(session, 3000)
    if not convo.strip() or session.model is None:
        return ""
    msg = complete(session.model, {"systemPrompt": TITLE_SYSTEM, "tools": [],
                                   "messages": [{"role": "user", "content": convo, "timestamp": int(time.time() * 1000)}]},
                   {"apiKey": session.auth.get_api_key(session.model["provider"]), "maxTokens": 24})
    if msg.get("stopReason") == "error":
        return ""
    text = "".join(b.get("text", "") for b in msg.get("content") or [] if b.get("type") == "text")
    text = re.sub(r"\s+", " ", text).strip().strip("\"'`.").strip()
    return " ".join(text.split()[:8])[:60]


def _title_setup(session: Any) -> str:
    _title_unload(session)

    def on_event(ev: Dict[str, Any]) -> None:
        if ev.get("type") != "agent_end" or session.sessions.session_name():
            return
        users = sum(1 for e in session.sessions.get_branch() if e.get("type") == "message" and e["message"].get("role") == "user")
        if users != 1:          # only once, after the first exchange
            return
        try:
            title = make_title(session)
            if title:
                session.set_session_name(title)
        except Exception:
            pass
    session._title_unsub = session.subscribe(on_event)
    return ""


def _title_unload(session: Any) -> None:
    unsub = getattr(session, "_title_unsub", None)
    if unsub:
        unsub()
    session._title_unsub = None


CAPABILITIES: List[Capability] = [
    Capability("persona", "Personas", "Named voices and working styles (concise, teacher, reviewer, pair, or your own).",
               "Hermes personalities, OpenClaw SOUL.md", live_prompt=_persona_prompt,
               commands=lambda s: {"persona": ("Switch persona: /persona [name|off|list]", lambda a: _persona_command(s, a))}),
    Capability("commit", "Commit messages", "/commit writes a commit message from the diff and commits when you confirm.",
               "Oh My Pi commit",
               commands=lambda s: {"commit": ("Write a commit message from the changes and commit: /commit [hint] [--yes]",
                                              lambda a: _commit_command(s, a))}),
    Capability("auto-title", "Automatic session titles", "Name an unnamed session after its first exchange (one tiny model call).",
               "Hermes, OpenClaw session titles", prompt=_title_setup, unload=_title_unload),
]
