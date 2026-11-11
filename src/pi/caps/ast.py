"""`ast_grep` / `ast_edit`: structural code search and rewrite (Oh My Pi's AST tools), through the ast-grep CLI.

Oh My Pi links ast-grep natively; here the same tool names and parameters drive the `ast-grep` binary
(`npm i -g @ast-grep/cli`, `cargo install ast-grep`, or `brew install ast-grep`). Without it the capability reports
"not loaded" in `/capabilities` and costs nothing. `OMNI_AST_GREP` overrides the command.
"""
from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
from typing import Any, Dict, List, Optional

from ..tools.base import ToolDef, ToolError, check_abort, resolve_to_cwd, text_result
from . import Capability

MAX_MATCHES = 50
INSTALL = "ast-grep is not installed (npm i -g @ast-grep/cli); set OMNI_AST_GREP to use another command"


def binary() -> Optional[List[str]]:
    custom = os.environ.get("OMNI_AST_GREP")
    if custom:
        return [t.strip('"') for t in shlex.split(custom, posix=os.name != "nt")]  # non-posix split keeps the quotes
    for name in ("ast-grep", "sg"):
        found = shutil.which(name)
        if found and not (name == "sg" and os.name != "nt" and "ast-grep" not in os.path.realpath(found)):
            return [found]  # on Linux `sg` is usually shadow-utils' "switch group", not ast-grep
    return None


def run(args: List[str], cwd: str, timeout: float = 120.0) -> List[Dict[str, Any]]:
    cmd = binary()
    if cmd is None:
        raise ToolError(INSTALL)
    try:
        p = subprocess.run(cmd + args, cwd=cwd, capture_output=True, text=True, timeout=timeout, encoding="utf-8", errors="replace")
    except subprocess.TimeoutExpired:
        raise ToolError("ast-grep timed out; narrow `path`") from None
    except OSError as e:
        raise ToolError(f"could not run ast-grep: {e}") from None
    out: List[Dict[str, Any]] = []
    for line in p.stdout.splitlines():
        line = line.strip()
        if line.startswith("{"):
            try:
                out.append(json.loads(line))
            except ValueError:
                pass
    if p.returncode not in (0, 1) or (not out and p.stderr.strip() and "rror" in p.stderr):
        raise ToolError(f"ast-grep failed: {p.stderr.strip()[:600] or 'exit ' + str(p.returncode)}\n"
                        "A pattern that does not parse is a query error, not an absence of matches.")
    return out


def _common(p: Dict[str, Any], cwd: str) -> List[str]:
    pat = str(p.get("pat") or p.get("pattern") or "").strip()
    if not pat:
        raise ToolError("pat is required: one AST pattern, e.g. `console.log($$$ARGS)`")
    args = ["run", "-p", pat, "--json=stream"]
    if p.get("lang"):
        args += ["-l", str(p["lang"])]
    return args


def _loc(m: Dict[str, Any]) -> str:
    line = ((m.get("range") or {}).get("start") or {}).get("line", 0) + 1
    return f"{str(m.get('file', '?')).replace(os.sep, '/')}:{line}"


def _first(s: str, n: int = 160) -> str:
    s = (s or "").strip().split("\n")[0]
    return s if len(s) <= n else s[:n] + "…"


def _tools(session: Any) -> Dict[str, ToolDef]:
    def grep(_id, p, signal=None, on_update=None, ctx=None):
        check_abort(signal)
        target = resolve_to_cwd(str(p.get("path") or "."), session.cwd)
        matches = run(_common(p, session.cwd) + [target], session.cwd)
        if not matches:
            return text_result("No matches. (If the pattern should match, check that it parses as one AST node and "
                               "that `lang` is right.)", {"matches": 0})
        limit = max(1, min(int(p.get("limit") or MAX_MATCHES), 200))
        rows = [f"{_loc(m)}: {_first(m.get('lines') or m.get('text'))}" for m in matches[:limit]]
        more = f"\n[{len(matches) - limit} more matches; narrow `path` or raise `limit`]" if len(matches) > limit else ""
        return text_result("\n".join(rows) + more, {"matches": len(matches), "files": len({m.get("file") for m in matches})})

    def edit(_id, p, signal=None, on_update=None, ctx=None):
        check_abort(signal)
        if p.get("rewrite") is None:
            raise ToolError("rewrite is required: the replacement, using the pattern's metavariables")
        if not p.get("path"):
            raise ToolError("path is required for ast_edit: rewrite one file or folder at a time")
        target = resolve_to_cwd(str(p["path"]), session.cwd)
        base = _common(p, session.cwd) + ["-r", str(p["rewrite"])]
        preview = run(base + [target], session.cwd)
        if not preview:
            return text_result("No matches; nothing changed.", {"matches": 0})
        rows = [f"{_loc(m)}: {_first(m.get('text'), 70)}  ->  {_first(m.get('replacement'), 70)}" for m in preview[:20]]
        more = f"\n[{len(preview) - 20} more]" if len(preview) > 20 else ""
        files = sorted({str(m.get("file")) for m in preview})
        if p.get("dry_run"):
            return text_result(f"Would rewrite {len(preview)} match(es) in {len(files)} file(s):\n" + "\n".join(rows) + more,
                               {"matches": len(preview), "files": files, "applied": False})
        run(base + ["--update-all", target], session.cwd)
        return text_result(f"Rewrote {len(preview)} match(es) in {len(files)} file(s):\n" + "\n".join(rows) + more,
                           {"matches": len(preview), "files": files, "applied": True})

    shared = ("`$NAME` captures one node, `$$$NAME` zero or more, `$_` matches without binding; names are UPPERCASE "
              "and stand for whole nodes. The pattern must parse as a single AST node in `lang`.")
    return {
        "ast_grep": ToolDef(
            name="ast_grep", label="AST grep",
            description="Structural code search: use when syntax shape matters more than text (calls, declarations, "
                        f"language constructs). {shared} Narrow `path` first; avoid repo-root scans.",
            parameters={"type": "object", "properties": {
                "pat": {"type": "string"}, "lang": {"type": "string", "description": "e.g. python, typescript, rust"},
                "path": {"type": "string"}, "limit": {"type": "integer"}}, "required": ["pat"]},
            execute=grep, prompt_snippet="Search code by syntax structure", read_only=True),
        "ast_edit": ToolDef(
            name="ast_edit", label="AST edit",
            description=f"Structural rewrite of every match of `pat` under `path` with `rewrite`. {shared} Run with "
                        "dry_run=true first when the pattern is broad.",
            parameters={"type": "object", "properties": {
                "pat": {"type": "string"}, "rewrite": {"type": "string"}, "lang": {"type": "string"},
                "path": {"type": "string"}, "dry_run": {"type": "boolean"}}, "required": ["pat", "rewrite", "path"]},
            execute=edit, prompt_snippet="Rewrite code by syntax structure"),
    }


CAPABILITIES = [Capability("ast", "AST search and rewrite", "ast_grep / ast_edit: structural code search and "
                           "codemods through the ast-grep CLI.", "Oh My Pi ast_grep / ast_edit", tools=_tools,
                           available=lambda s: None if binary() else INSTALL)]
