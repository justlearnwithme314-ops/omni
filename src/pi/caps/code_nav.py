"""`code-nav`: find where things are defined and used (the navigation part of Oh My Pi's `lsp` tool).

One tool, `code_nav`, three actions:

    outline(path)          the classes, functions and methods of a file, with line numbers
    definition(name)       where a symbol is defined, across the project
    references(name)       every line that uses it (whole-word matches), grouped by file

Python files are parsed with Python's own `ast`, so their outlines and definitions are exact. Other languages
(JavaScript / TypeScript, Go, Rust, Java, C / C++, C#, Ruby, PHP, Kotlin, Swift) use declaration patterns: good for
finding the place, but it is text matching, not a language server - no type resolution, no rename, and a method
with a common name will list every class that has one. It skips `.git`, `node_modules`, virtualenvs and build
output, and caps results so a broad name cannot flood the context.
"""
from __future__ import annotations

import ast
import os
import re
from typing import Any, Dict, Iterator, List, Optional, Tuple

from ..tools.base import ToolDef, ToolError, check_abort, resolve_to_cwd, text_result
from . import Capability

SKIP_DIRS = {".git", "node_modules", ".venv", "venv", "env", "__pycache__", "dist", "build", "target", ".next", ".tox", ".mypy_cache",
             ".pytest_cache", "vendor", ".idea", ".vscode", "coverage", ".gradle", "out"}
CODE_EXT = {".py", ".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx", ".go", ".rs", ".java", ".c", ".h", ".cc", ".cpp", ".hpp", ".cs", ".rb",
            ".php", ".kt", ".swift", ".scala", ".lua", ".sh"}
MAX_FILES, MAX_FILE_BYTES, MAX_RESULTS = 6000, 1_500_000, 60

_ID = r"[A-Za-z_$][\w$]*"
DECLS: List[Tuple[str, re.Pattern]] = [
    ("class", re.compile(rf"^\s*(?:export\s+)?(?:default\s+)?(?:abstract\s+|final\s+|public\s+|private\s+|sealed\s+|data\s+|open\s+)*"
                         rf"(?:class|interface|struct|enum|trait|protocol|object|record)\s+({_ID})")),
    ("function", re.compile(rf"^\s*(?:export\s+)?(?:default\s+)?(?:async\s+)?function\s*\*?\s*({_ID})")),
    ("function", re.compile(rf"^\s*(?:pub(?:\([a-z]+\))?\s+)?(?:async\s+)?(?:unsafe\s+)?fn\s+({_ID})")),
    ("function", re.compile(rf"^\s*func\s+(?:\([^)]*\)\s*)?({_ID})")),
    ("function", re.compile(rf"^\s*(?:def|fun|sub)\s+(?:self\.)?({_ID})")),
    ("const", re.compile(rf"^\s*(?:export\s+)?(?:const|let|var)\s+({_ID})\s*(?::[^=]+)?=\s*(?:async\s*)?(?:\([^)]*\)|{_ID})\s*=>")),
    ("type", re.compile(rf"^\s*(?:export\s+)?(?:type|typedef)\s+({_ID})\b")),
    ("method", re.compile(rf"^\s+(?:public|private|protected|static|async|override|final|virtual|\s)*\s*({_ID})\s*\([^;{{]*\)\s*(?::\s*[^{{;]+)?\{{")),
]
_NOT_METHODS = {"if", "for", "while", "switch", "catch", "return", "function", "else", "do", "try", "with", "new", "typeof", "await"}


def iter_files(root: str) -> Iterator[str]:
    n = 0
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not d.startswith(".")]
        for fn in filenames:
            if os.path.splitext(fn)[1].lower() in CODE_EXT:
                n += 1
                if n > MAX_FILES:
                    return
                yield os.path.join(dirpath, fn)


def read(path: str) -> Optional[str]:
    try:
        if os.path.getsize(path) > MAX_FILE_BYTES:
            return None
        with open(path, "rb") as f:
            return f.read().decode("utf-8", "replace")
    except OSError:
        return None


def outline(path: str, text: str) -> List[Tuple[int, str, str]]:
    """[(line, kind, signature)] for one file."""
    out: List[Tuple[int, str, str]] = []
    if path.endswith(".py"):
        try:
            tree = ast.parse(text)
        except SyntaxError:
            tree = None
        if tree is not None:
            def walk(node: ast.AST, prefix: str) -> None:
                for child in ast.iter_child_nodes(node):
                    if isinstance(child, ast.ClassDef):
                        bases = ", ".join(ast.unparse(b) for b in child.bases)
                        out.append((child.lineno, "class", f"{prefix}{child.name}" + (f"({bases})" if bases else "")))
                        walk(child, f"{prefix}{child.name}.")
                    elif isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        args = ast.unparse(child.args)
                        kind = "method" if prefix else "function"
                        out.append((child.lineno, kind, f"{prefix}{child.name}({args if len(args) < 70 else args[:67] + '...'})"))
            walk(tree, "")
            return out
    for i, line in enumerate(text.split("\n"), 1):
        if len(line) > 300:
            continue
        for kind, rx in DECLS:
            m = rx.match(line)
            if m and not (kind == "method" and m.group(1) in _NOT_METHODS):
                out.append((i, kind, line.strip().rstrip("{").strip()[:110]))
                break
    return out


def find_definitions(root: str, name: str) -> List[Tuple[str, int, str]]:
    hits = []
    short = name.split(".")[-1]
    for path in iter_files(root):
        text = read(path)
        if text is None or short not in text:
            continue
        for line, kind, sig in outline(path, text):
            if path.endswith(".py"):
                sym = sig.split("(")[0]                       # qualified: Class.method
                ok = sym == name or (sym.split(".")[-1] == short and (name == short or sym.endswith("." + name)))
            else:
                sym = next((m.group(1) for _k, rx in DECLS for m in [rx.match(" " + sig + " {")] if m), "")
                ok = sym == short
            if ok:
                hits.append((path, line, f"{kind} {sig}"))
    return hits


def find_references(root: str, name: str) -> List[Tuple[str, int, str]]:
    short = name.split(".")[-1]
    rx = re.compile(rf"(?<![\w$]){re.escape(short)}(?![\w$])")
    hits = []
    for path in iter_files(root):
        text = read(path)
        if text is None or short not in text:
            continue
        for i, line in enumerate(text.split("\n"), 1):
            if rx.search(line):
                hits.append((path, i, line.strip()[:140]))
    return hits


def _tools(session: Any) -> Dict[str, ToolDef]:
    def rel(p: str) -> str:
        return os.path.relpath(p, session.cwd).replace(os.sep, "/")

    def execute(_id, p, signal=None, on_update=None, ctx=None):
        check_abort(signal)
        action = str(p.get("action") or "").lower()
        if action == "outline":
            if not p.get("path"):
                raise ToolError("path is required for outline")
            path = resolve_to_cwd(str(p["path"]), session.cwd)
            text = read(path)
            if text is None:
                raise ToolError(f"cannot read {p['path']}")
            items = outline(path, text)
            if not items:
                return text_result("No declarations found in this file.", {"symbols": 0})
            return text_result("\n".join(f"{line:>5}  {kind:<8} {sig}" for line, kind, sig in items[:400]), {"symbols": len(items)})
        name = str(p.get("name") or "").strip()
        if not name or not re.match(rf"^{_ID}(?:\.{_ID})*$", name):
            raise ToolError("name is required: an identifier such as `parse_config` or `Session.start`")
        root = resolve_to_cwd(str(p.get("path") or "."), session.cwd)
        if action == "definition":
            hits = find_definitions(root, name)
            if not hits:
                return text_result(f"No definition of `{name}` found under {rel(root)}. It may come from a dependency, or be "
                                   "declared in a form this tool does not recognise; try references.", {"results": 0})
            return text_result("\n".join(f"{rel(f)}:{line}: {sig}" for f, line, sig in hits[:MAX_RESULTS]), {"results": len(hits)})
        if action == "references":
            hits = find_references(root, name)
            if not hits:
                return text_result(f"No use of `{name}` found under {rel(root)}.", {"results": 0})
            files: Dict[str, List[Tuple[int, str]]] = {}
            for f, line, text in hits:
                files.setdefault(f, []).append((line, text))
            rows, shown = [], 0
            for f, lines in sorted(files.items(), key=lambda kv: -len(kv[1])):
                if shown >= MAX_RESULTS:
                    break
                rows.append(f"{rel(f)} ({len(lines)})")
                for line, text in lines[:8]:
                    rows.append(f"  {line}: {text}")
                    shown += 1
                if len(lines) > 8:
                    rows.append(f"  … {len(lines) - 8} more in this file")
            more = f"\n[{len(hits)} uses in {len(files)} files; narrow `path` to see the rest]" if len(hits) > shown else ""
            return text_result("\n".join(rows) + more, {"results": len(hits), "files": len(files)})
        raise ToolError("action must be outline, definition or references")

    return {"code_nav": ToolDef(
        name="code_nav", label="Code nav",
        description="Navigate code. outline(path): the classes and functions of a file with line numbers - read this "
                    "before reading a large file. definition(name, path?): where a symbol is defined. "
                    "references(name, path?): the lines that use it. Exact for Python; pattern-based for other languages.",
        parameters={"type": "object", "properties": {
            "action": {"type": "string", "enum": ["outline", "definition", "references"]},
            "name": {"type": "string"}, "path": {"type": "string"}}, "required": ["action"]},
        execute=execute, prompt_snippet="Find definitions, references and file outlines", read_only=True)}


CAPABILITIES = [Capability("code-nav", "Code navigation", "code_nav: file outlines, where a symbol is defined, and where it "
                           "is used.", "Oh My Pi lsp (navigation)", tools=_tools)]
