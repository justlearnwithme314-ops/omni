"""`workflows`: ready-made slash commands for everyday coding work (Oh My Pi and Claude Code ship similar ones).

    /review [focus]     review the uncommitted changes for real bugs, without touching files
    /commit             propose a commit message and file list, commit after you confirm
    /explain <thing>    how a file, function or topic works, with file:line references
    /test <thing>       write or fix tests in the project's own style
    /fix <failure>      reproduce, find the root cause, make the smallest fix
    /catchup [since]    what changed recently in the repo

They are plain prompt templates in `src/pi/workflows/`. A prompt of your own with the same name (in
`<agent-dir>/prompts` or the project) replaces the bundled one. They cost nothing in context until you use one.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, List

from . import Capability

PACK_DIR = Path(__file__).resolve().parents[1] / "workflows"


def _roots(session: Any) -> List[Path]:
    return [PACK_DIR] if PACK_DIR.is_dir() else []


def working_diff(session: Any, args: str = "") -> str:
    """`/diff [staged|all]`: what has changed in the working tree (Hermes' working diff), bounded for reading."""
    import subprocess
    mode = args.strip().lower()

    def git(*a: str) -> str:
        try:
            r = subprocess.run(["git", *a], cwd=session.cwd, capture_output=True, text=True, timeout=30, encoding="utf-8", errors="replace")
        except (OSError, subprocess.TimeoutExpired) as e:
            return f"(git failed: {e})"
        return r.stdout if r.returncode == 0 else f"(git: {(r.stderr or '').strip()[:200]})"
    if git("rev-parse", "--is-inside-work-tree").strip() != "true":
        return "Not a git repository."
    scope = ["--staged"] if mode == "staged" else ["HEAD"] if mode == "all" else []
    stat = git("diff", "--stat", *scope).rstrip()
    body = git("diff", *scope)
    untracked = [l[3:] for l in git("status", "--porcelain").splitlines() if l.startswith("??")]
    if not stat and (mode == "staged" or not untracked):
        return "No changes" + (" staged." if mode == "staged" else ".")
    out = (stat + "\n\n" if stat else "") + (body if len(body) <= 12_000 else body[:12_000] + f"\n[diff truncated: {len(body) - 12_000} more characters]")
    if untracked and mode != "staged":
        out += "\nUntracked files:\n" + "\n".join(f"  + {u}" for u in untracked[:40])
    return out.rstrip()


CAPABILITIES = [Capability("workflows", "Workflow commands", "/review, /commit, /explain, /test, /fix, /catchup as ready-made "
                           "prompts, plus /diff.", "Oh My Pi, Claude Code commands, Hermes working diff", template_roots=_roots,
                           commands=lambda s: {"diff": ("Show the working-tree changes (`staged` or `all` to narrow / widen)",
                                                         lambda a: working_diff(s, a))})]
