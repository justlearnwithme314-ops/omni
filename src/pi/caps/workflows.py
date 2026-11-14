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


CAPABILITIES = [Capability("workflows", "Workflow commands", "/review, /commit, /explain, /test, /fix, /catchup as ready-made "
                           "prompts.", "Oh My Pi, Claude Code commands", template_roots=_roots)]
