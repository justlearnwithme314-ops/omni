"""grep / find - Pi semantics (ripgrep / fd when installed, a .gitignore-aware Python fallback otherwise)."""
from __future__ import annotations

import fnmatch
import json
import os
import re
import shutil
import subprocess
from typing import Dict, Iterator, List, Optional

from .base import ToolDef, ToolError, check_abort, resolve_to_cwd, text_result
from .truncate import DEFAULT_MAX_BYTES, GREP_MAX_LINE_LENGTH, format_size, truncate_head, truncate_line

SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", ".hg", ".svn", ".mypy_cache", ".pytest_cache"}


def glob_to_regex(pattern: str) -> "re.Pattern[str]":
    i, out = 0, ""
    while i < len(pattern):
        c = pattern[i]
        if pattern.startswith("**/", i):
            out += "(?:.*/)?"
            i += 3
            continue
        if pattern.startswith("**", i):
            out += ".*"
            i += 2
            continue
        if c == "*":
            out += "[^/]*"
        elif c == "?":
            out += "[^/]"
        elif c == "{":
            j = pattern.find("}", i)
            if j > i:
                out += "(?:" + "|".join(re.escape(x) for x in pattern[i + 1:j].split(",")) + ")"
                i = j + 1
                continue
            out += re.escape(c)
        elif c == "[":
            j = pattern.find("]", i)
            if j > i:
                out += "[" + pattern[i + 1:j].replace("!", "^", 1) + "]"
                i = j + 1
                continue
            out += re.escape(c)
        else:
            out += re.escape(c)
        i += 1
    return re.compile("^" + out + "$")


def _git_files(root: str) -> Optional[List[str]]:
    if not shutil.which("git"):
        return None
    try:
        r = subprocess.run(["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"], cwd=root,
                           capture_output=True, timeout=30)
        if r.returncode != 0:
            return None
        return [p for p in r.stdout.decode("utf-8", "replace").split("\0") if p]
    except Exception:
        return None


def walk_files(root: str, signal=None) -> Iterator[str]:
    """Relative paths ('/' separators) under root, respecting .gitignore when root is in a git repo."""
    files = _git_files(root)
    if files is not None:
        for f in files:
            if os.path.exists(os.path.join(root, f)):
                yield f
        return
    for dirpath, dirnames, filenames in os.walk(root):
        check_abort(signal)
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        rel = os.path.relpath(dirpath, root)
        for fn in filenames:
            yield fn if rel == "." else os.path.join(rel, fn).replace(os.sep, "/")


# ------------------------------------------------------------------------------------------------- grep
def make_grep(cwd: str) -> ToolDef:
    default_limit = 100

    def execute(_id, params, signal=None, on_update=None, ctx=None):
        check_abort(signal)
        pattern = params["pattern"]
        base = getattr(ctx, "cwd", None) or cwd
        search_path = resolve_to_cwd(params.get("path") or ".", base)
        if not os.path.exists(search_path):
            raise ToolError(f"Path not found: {search_path}")
        limit = max(1, int(params.get("limit") or default_limit))
        context = max(0, int(params.get("context") or 0))
        is_dir = os.path.isdir(search_path)
        rows: List[str] = []
        matches = 0
        lines_truncated = False
        limit_reached = False

        def push(rel: str, ln: int, text: str, is_match: bool) -> None:
            nonlocal lines_truncated
            t, w = truncate_line(text.rstrip("\r\n"))
            lines_truncated = lines_truncated or w
            rows.append(f"{rel}:{ln}: {t}" if is_match else f"{rel}-{ln}- {t}")

        rg = shutil.which("rg")
        if rg:
            args = [rg, "--no-config", "--json", "--line-number", "--color=never", "--hidden"]
            if params.get("ignoreCase"):
                args.append("--ignore-case")
            if params.get("literal"):
                args.append("--fixed-strings")
            if params.get("glob"):
                args += ["--glob", re.sub(r"^(!?)(?:\./)+", r"\1", params["glob"])]
            if context:
                args += ["--context", str(context)]
            args += ["--", pattern, "." if is_dir else search_path]
            proc = subprocess.Popen(args, cwd=search_path if is_dir else base, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            remove = signal.add_listener(proc.kill) if signal is not None and hasattr(signal, "add_listener") else (lambda: None)
            try:
                for raw in proc.stdout:  # type: ignore[union-attr]
                    ev = json.loads(raw)
                    t = ev.get("type")
                    if t not in ("match", "context"):
                        continue
                    d = ev["data"]
                    p = d["path"].get("text", "")
                    # like Pi: path relative to the search dir with forward slashes (rg prints .\src\x on Windows)
                    rel = p.replace("\\", "/")
                    rel = rel[2:] if rel.startswith("./") else (
                        os.path.relpath(p, base).replace(os.sep, "/") if os.path.isabs(p) else rel)
                    if not is_dir:
                        rel = os.path.basename(search_path)
                    text = d["lines"].get("text", "")
                    if t == "match":
                        if matches >= limit:
                            limit_reached = True
                            proc.kill()
                            break
                        matches += 1
                    push(rel, d["line_number"], text, t == "match")
                proc.wait(timeout=30)
            finally:
                remove()
                for stream in (proc.stdout, proc.stderr):
                    try:
                        if stream and not stream.closed and stream is proc.stdout:
                            stream.close()
                    except Exception:
                        pass
            check_abort(signal)
            if proc.returncode not in (0, 1, None, -9) and not rows and not limit_reached:
                err = (proc.stderr.read().decode("utf-8", "replace") if proc.stderr else "").strip()  # type: ignore[union-attr]
                if err:
                    raise ToolError(err)
            if proc.stderr:
                proc.stderr.close()
        else:
            flags = re.IGNORECASE if params.get("ignoreCase") else 0
            try:
                rx = re.compile(re.escape(pattern) if params.get("literal") else pattern, flags)
            except re.error as e:
                raise ToolError(f"regex parse error: {e}")
            glob_rx = glob_to_regex(params["glob"].lstrip("./")) if params.get("glob") else None
            files = [os.path.basename(search_path)] if not is_dir else walk_files(search_path, signal)
            root = os.path.dirname(search_path) if not is_dir else search_path
            for rel in files:
                check_abort(signal)
                if glob_rx and not (glob_rx.match(rel) or glob_rx.match(os.path.basename(rel))):
                    continue
                try:
                    with open(os.path.join(root, rel), "r", encoding="utf-8", errors="strict") as f:
                        lines = f.read().split("\n")
                except (UnicodeDecodeError, OSError):
                    continue
                last_emitted = -1
                for i, line in enumerate(lines):
                    if rx.search(line):
                        if matches >= limit:
                            limit_reached = True
                            break
                        matches += 1
                        for j in range(max(last_emitted + 1, i - context), i):
                            push(rel, j + 1, lines[j], False)
                        push(rel, i + 1, line, True)
                        last_emitted = i
                        for j in range(i + 1, min(len(lines), i + 1 + context)):
                            if not rx.search(lines[j]):
                                push(rel, j + 1, lines[j], False)
                                last_emitted = j
                if limit_reached:
                    break
        if not rows:
            return text_result("No matches found")
        tr = truncate_head("\n".join(rows), max_lines=10 ** 9)
        out = tr["content"]
        notices, details = [], {}
        if limit_reached:
            notices.append(f"{limit} matches limit reached. Use limit={limit * 2} for more, or refine pattern")
            details["matchLimitReached"] = limit
        if tr["truncated"]:
            notices.append(f"{format_size(DEFAULT_MAX_BYTES)} limit reached")
            details["truncation"] = tr
        if lines_truncated:
            notices.append(f"Some lines truncated to {GREP_MAX_LINE_LENGTH} chars. Use read tool to see full lines")
            details["linesTruncated"] = True
        if notices:
            out += f"\n\n[{'. '.join(notices)}]"
        return {"content": [{"type": "text", "text": out}], "details": details or None}

    return ToolDef(
        name="grep", label="grep",
        description=(f"Search file contents for a pattern. Returns matching lines with file paths and line numbers. Respects .gitignore. "
                     f"Output is truncated to {default_limit} matches or {DEFAULT_MAX_BYTES // 1024}KB (whichever is hit first). "
                     f"Long lines are truncated to {GREP_MAX_LINE_LENGTH} chars."),
        parameters={"type": "object", "properties": {
            "pattern": {"type": "string", "description": "Search pattern (regex or literal string)"},
            "path": {"type": "string", "description": "Directory or file to search (default: current directory)"},
            "glob": {"type": "string", "description": "Filter files by glob pattern, e.g. '*.ts' or '**/*.spec.ts'"},
            "ignoreCase": {"type": "boolean", "description": "Case-insensitive search (default: false)"},
            "literal": {"type": "boolean", "description": "Treat pattern as literal string instead of regex (default: false)"},
            "context": {"type": "number", "description": "Number of lines to show before and after each match (default: 0)"},
            "limit": {"type": "number", "description": "Maximum number of matches to return (default: 100)"}},
            "required": ["pattern"]},
        execute=execute, prompt_snippet="Search file contents for patterns (respects .gitignore)", read_only=True)


# ------------------------------------------------------------------------------------------------- find
def make_find(cwd: str) -> ToolDef:
    default_limit = 1000

    def execute(_id, params, signal=None, on_update=None, ctx=None):
        check_abort(signal)
        pattern = params["pattern"]
        base = getattr(ctx, "cwd", None) or cwd
        search_path = resolve_to_cwd(params.get("path") or ".", base)
        if not os.path.exists(search_path):
            raise ToolError(f"Path not found: {search_path}")
        limit = max(1, int(params.get("limit") or default_limit))
        pat = re.sub(r"^(?:\./)+", "", pattern.replace("\\", "/"))
        full_path = "/" in pat
        rx = glob_to_regex(pat)
        results: List[str] = []
        fd = shutil.which("fd") or shutil.which("fdfind")
        if fd:
            args = [fd, "--glob", "--color=never", "--hidden", "--max-results", str(limit + 1)]
            if full_path:
                args.append("--full-path")
                pat = "**/" + pat if not pat.startswith("**") else pat
            if not os.path.isdir(os.path.join(search_path, ".git")) and _git_files(search_path) is None:
                args.append("--no-require-git")
            args += ["--", pat, "."]
            try:
                r = subprocess.run(args, cwd=search_path, capture_output=True, timeout=60)
                for line in r.stdout.decode("utf-8", "replace").splitlines():
                    line = line[2:] if line.startswith("./") else line
                    if line:
                        results.append(line.replace("\\", "/"))
            except Exception as e:
                raise ToolError(f"Failed to run fd: {e}")
        else:
            seen_dirs = set()
            for rel in walk_files(search_path, signal):
                target = rel if full_path else os.path.basename(rel)
                if rx.match(target):
                    results.append(rel)
                    if len(results) > limit:
                        break
                parts = rel.split("/")[:-1]
                for i in range(len(parts)):
                    d = "/".join(parts[: i + 1])
                    if d in seen_dirs:
                        continue
                    seen_dirs.add(d)
                    if rx.match(d if full_path else parts[i]):
                        results.append(d + "/")
        check_abort(signal)
        if not results:
            return text_result("No files found matching pattern")
        limit_hit = len(results) > limit
        results = sorted(results[:limit])
        tr = truncate_head("\n".join(results), max_lines=10 ** 9)
        out = tr["content"]
        notices, details = [], {}
        if limit_hit:
            notices.append(f"{limit} results limit reached")
            details["resultLimitReached"] = limit
        if tr["truncated"]:
            notices.append(f"{format_size(DEFAULT_MAX_BYTES)} limit reached")
            details["truncation"] = tr
        if notices:
            out += f"\n\n[{'. '.join(notices)}]"
        return {"content": [{"type": "text", "text": out}], "details": details or None}

    return ToolDef(
        name="find", label="find",
        description=(f"Search for files by glob pattern. Returns matching file paths relative to the search directory. Respects .gitignore. "
                     f"Output is truncated to {default_limit} results or {DEFAULT_MAX_BYTES // 1024}KB (whichever is hit first)."),
        parameters={"type": "object", "properties": {
            "pattern": {"type": "string", "description": "Glob pattern to match files, e.g. '*.ts', '**/*.json', or 'src/**/*.spec.ts'"},
            "path": {"type": "string", "description": "Directory to search in (default: current directory)"},
            "limit": {"type": "number", "description": "Maximum number of results (default: 1000)"}},
            "required": ["pattern"]},
        execute=execute, prompt_snippet="Find files by glob pattern (respects .gitignore)", read_only=True)
