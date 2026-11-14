"""`github`: read pull requests, issues and CI runs (Oh My Pi's `github` tool), through the `gh` CLI.

One read-only tool with a fixed set of actions, so the model does not have to remember `gh` flags and cannot
change anything by accident:

    pr_list · pr_view · pr_diff · pr_checks · issue_list · issue_view · run_list · run_view · repo_view · search_issues

Writing (commenting, merging, creating PRs) stays with `bash` and `gh`, where the `approvals` capability can see
it. Needs the GitHub CLI installed and signed in (`gh auth login`); without it the capability is off and free.
`OMNI_GH` overrides the command.
"""
from __future__ import annotations

import os
import shlex
import shutil
import subprocess
from typing import Any, Dict, List, Optional

from ..tools.base import ToolDef, ToolError, check_abort, text_result
from . import Capability

MAX_OUTPUT = 24_000
INSTALL = "the GitHub CLI is not installed (https://cli.github.com), or set OMNI_GH"


def gh() -> Optional[List[str]]:
    custom = os.environ.get("OMNI_GH")
    if custom:
        return [t.strip('"') for t in shlex.split(custom, posix=os.name != "nt")]
    found = shutil.which("gh")
    return [found] if found else None


def build(action: str, p: Dict[str, Any]) -> List[str]:
    n = str(p.get("number") or "").strip()
    limit = str(max(1, min(int(p.get("limit") or 20), 100)))
    need = lambda: n if n else (_ for _ in ()).throw(ToolError(f"number is required for {action}"))  # noqa: E731
    state = str(p.get("state") or "open")
    table = {
        "pr_list": lambda: ["pr", "list", "--limit", limit, "--state", state],
        "pr_view": lambda: ["pr", "view", need(), "--comments"],
        "pr_diff": lambda: ["pr", "diff", need()],
        "pr_checks": lambda: ["pr", "checks", need()],
        "issue_list": lambda: ["issue", "list", "--limit", limit, "--state", state],
        "issue_view": lambda: ["issue", "view", need(), "--comments"],
        "run_list": lambda: ["run", "list", "--limit", limit],
        "run_view": lambda: ["run", "view", need(), "--log-failed"] if p.get("failed_log") else ["run", "view", need()],
        "repo_view": lambda: ["repo", "view"],
        "search_issues": lambda: ["search", "issues", str(p.get("query") or "") or (_ for _ in ()).throw(ToolError("query is required")),
                                  "--limit", limit],
    }
    if action not in table:
        raise ToolError("action must be one of: " + ", ".join(table))
    args = table[action]()
    if p.get("repo") and action != "search_issues":
        args += ["--repo", str(p["repo"])]
    return args


def _tools(session: Any) -> Dict[str, ToolDef]:
    def execute(_id, p, signal=None, on_update=None, ctx=None):
        check_abort(signal)
        cmd = gh()
        if cmd is None:
            raise ToolError(INSTALL)
        args = build(str(p.get("action") or ""), p)
        try:
            r = subprocess.run(cmd + args, cwd=session.cwd, capture_output=True, text=True, timeout=60, encoding="utf-8", errors="replace",
                               env=dict(os.environ, GH_PAGER="", NO_COLOR="1", GH_PROMPT_DISABLED="1"))
        except subprocess.TimeoutExpired:
            raise ToolError("gh timed out after 60 seconds") from None
        out = (r.stdout or "").strip()
        if r.returncode != 0 and not (args[:2] == ["pr", "checks"] and out):  # `pr checks` exits non-zero when a check failed
            raise ToolError((r.stderr or out or f"gh exited with {r.returncode}").strip()[:1500])
        if len(out) > MAX_OUTPUT:
            out = out[:MAX_OUTPUT] + f"\n[{len(out) - MAX_OUTPUT} more characters; narrow the request]"
        return text_result(out or "(nothing to show)", {"command": "gh " + " ".join(args)})

    return {"github": ToolDef(
        name="github", label="GitHub",
        description="Read GitHub for the current repository: pr_list, pr_view(number), pr_diff(number), pr_checks(number), "
                    "issue_list, issue_view(number), run_list, run_view(number, failed_log?), repo_view, "
                    "search_issues(query). Read-only; use bash with `gh` to change anything.",
        parameters={"type": "object", "properties": {
            "action": {"type": "string", "enum": ["pr_list", "pr_view", "pr_diff", "pr_checks", "issue_list", "issue_view", "run_list",
                                                 "run_view", "repo_view", "search_issues"]},
            "number": {"type": "string"}, "state": {"type": "string", "enum": ["open", "closed", "merged", "all"]},
            "limit": {"type": "integer"}, "query": {"type": "string"}, "repo": {"type": "string", "description": "owner/name (optional)"},
            "failed_log": {"type": "boolean"}}, "required": ["action"]},
        execute=execute, prompt_snippet="Read GitHub pull requests, issues and CI runs", read_only=True)}


CAPABILITIES = [Capability("github", "GitHub", "Read pull requests, issues and CI runs through the gh CLI.",
                           "Oh My Pi github tool", tools=_tools, available=lambda s: None if gh() else INSTALL)]
