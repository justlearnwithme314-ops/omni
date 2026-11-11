"""`hashline`: line-addressed edits (Oh My Pi's hashline edit mode).

With this on, `read` shows each file as `[path#TAG]` plus numbered lines, and `edit` takes a patch that names lines
by number instead of repeating the text to replace:

    [src/app.py#3F2A]
    PUT 12.=14:
    +    return compute(x)
    PUT >20:
    +
    +def helper():
    +    pass

`TAG` is a 4-hex snapshot of the file. An edit whose tag no longer matches is refused, so a stale view can never
corrupt a file. The model writes only the new lines (no old text to reproduce), which makes edits cheaper and
immune to whitespace-matching failures.

Supported ops: `PUT N.=M:` replace, `PUT <N:` / `PUT >N:` insert before / after, `PUT >$:` append, `CUT N.=M [@reg]`
delete (and capture), `PUT <N @reg` / `PUT >N @reg` / `PUT N.=M @reg` paste, `REM` delete the file, `MV dest` rename.
Oh My Pi's syntax-block ops (`N*`) need a parser and are not implemented; the tool says so and asks for a range.
"""
from __future__ import annotations

import hashlib
import os
import re
from typing import Any, Dict, List, Optional, Tuple

from ..tools.base import ToolDef, ToolError, file_mutation_lock, resolve_to_cwd, text_result
from . import Capability

_HEADER = re.compile(r"^\[(?P<path>.+?)(?:#(?P<tag>[0-9A-Za-z]{3,8}))?\]\s*$")
_NOTE = re.compile(r"\n\n\[[^\n]*\]\Z")
_RANGE = r"(\d+)(?:\.=(\d+))?"
CONTEXT = 2  # unchanged lines shown around each change in the result


def normalize(raw: bytes) -> Tuple[str, bool, str]:
    """(LF text without BOM, had BOM, newline style) - the same view `read` shows."""
    text = raw.decode("utf-8", "replace")
    bom = text.startswith("﻿")
    if bom:
        text = text[1:]
    nl = "\r\n" if "\r\n" in text else "\n"
    return text.replace("\r\n", "\n"), bom, nl


def tag_of(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:4].upper()


def split_lines(text: str) -> Tuple[List[str], bool]:
    lines = text.split("\n")
    trailing = text.endswith("\n")
    if trailing:
        lines.pop()
    return ([] if lines == [""] and not trailing else lines), trailing


def numbered(lines: List[str], start: int) -> str:
    return "\n".join(f"{start + i}|{l}" for i, l in enumerate(lines))


# -------------------------------------------------------------------------------------------- read
def _wrap_read(session: Any) -> ToolDef:
    orig = session.builtin_tools["read"]

    def execute(_id, p, signal=None, on_update=None, ctx=None):
        r = orig.execute(_id, p, signal, on_update, ctx)
        content = r.get("content") or []
        if len(content) != 1 or content[0].get("type") != "text":
            return r  # images etc.
        text = content[0]["text"]
        path = str(p.get("path") or "")
        try:
            with open(resolve_to_cwd(path, session.cwd), "rb") as f:
                full, _, _ = normalize(f.read())
        except OSError:
            return r
        m = _NOTE.search(text)
        body, note = (text[:m.start()], text[m.start():]) if m else (text, "")
        if not m and text.startswith("[") and text.endswith("]") and "\n" not in text:
            return r  # a lone notice such as "[Line 5 is 80KB ...]"
        start = max(1, int(p.get("offset") or 1))
        lines = body.split("\n") if body else []
        out = f"[{path}#{tag_of(full)}]\n" + (numbered(lines, start) if lines else "(empty file)") + note
        return {**r, "content": [{"type": "text", "text": out}]}

    return ToolDef(name="read", label=orig.label,
                   description=orig.description + " Text files are shown as `[path#TAG]` followed by `N|line` rows: "
                   "the number and `|` are not part of the file. Copy `[path#TAG]` into `edit` patches.",
                   parameters=orig.parameters, execute=execute, prompt_snippet=orig.prompt_snippet,
                   prompt_guidelines=list(orig.prompt_guidelines), read_only=True)


# -------------------------------------------------------------------------------------------- patch
def parse_patch(patch: str) -> List[Dict[str, Any]]:
    """Patch text -> [{path, tag, ops: [{kind, a, b, body, reg, line}]}]."""
    sections: List[Dict[str, Any]] = []
    cur: Optional[Dict[str, Any]] = None
    op: Optional[Dict[str, Any]] = None
    for n, raw in enumerate(patch.replace("\r\n", "\n").split("\n"), 1):
        line = raw.rstrip("\r")
        if op is not None and op.get("wants_body"):
            if line.startswith("+"):
                op["body"].append(line[1:])
                continue
            if line.strip() == "" and n == len(patch.split("\n")):
                continue
        s = line.strip()
        if not s or s in ("*** Begin Patch", "*** End Patch"):
            if op is not None and op.get("wants_body") and s == "" and op["body"]:
                pass
            continue
        h = _HEADER.match(s)
        if h and not s.upper().startswith(("PUT ", "CUT ", "MV ")):
            cur = {"path": h.group("path").strip(), "tag": (h.group("tag") or "").upper(), "ops": []}
            sections.append(cur)
            op = None
            continue
        if cur is None:
            raise ToolError(f"line {n}: expected a `[path#TAG]` header before `{s[:40]}`")
        if "*" in s.split("@")[0] and re.match(r"^(PUT|CUT)\s+[<>]?\d+\*", s):
            raise ToolError(f"line {n}: block operations (`N*`) are not supported; give an explicit range `N.=M`")
        m = re.match(rf"^PUT\s+{_RANGE}\s*(?::|@(\w*))\s*$", s)
        if m:
            a, b = int(m.group(1)), int(m.group(2) or m.group(1))
            op = {"kind": "replace", "a": a, "b": b, "body": [], "reg": m.group(3), "wants_body": m.group(3) is None, "line": n}
            cur["ops"].append(op)
            continue
        m = re.match(r"^PUT\s+([<>])\s*(\d+|\$)\s*(?::|@(\w*))\s*$", s)
        if m:
            op = {"kind": "before" if m.group(1) == "<" else "after", "a": m.group(2), "body": [], "reg": m.group(3),
                  "wants_body": m.group(3) is None, "line": n}
            cur["ops"].append(op)
            continue
        m = re.match(rf"^CUT\s+{_RANGE}\s*(?:@(\w+))?\s*$", s)
        if m:
            op = {"kind": "cut", "a": int(m.group(1)), "b": int(m.group(2) or m.group(1)), "reg": m.group(3) or "",
                  "body": [], "wants_body": False, "line": n}
            cur["ops"].append(op)
            continue
        if s == "REM":
            op = {"kind": "rem", "wants_body": False, "line": n, "body": []}
            cur["ops"].append(op)
            continue
        m = re.match(r"^MV\s+(.+)$", s)
        if m:
            op = {"kind": "mv", "dest": m.group(1).strip(), "wants_body": False, "line": n, "body": []}
            cur["ops"].append(op)
            continue
        if line.startswith(("-", " ")) or op is not None:
            raise ToolError(f"line {n}: `{s[:50]}` is not an operation or a `+` body row. Send only new lines, each "
                            "starting with `+`; never removed (`-`) or unchanged context lines.")
        raise ToolError(f"line {n}: unknown operation `{s[:50]}`")
    if not sections:
        raise ToolError("the patch has no `[path#TAG]` header")
    return sections


def apply_ops(lines: List[str], ops: List[Dict[str, Any]], registers: Dict[str, List[str]]) -> Tuple[List[str], List[Tuple[int, int]]]:
    """Apply ops (all numbered against the original `lines`). Returns (new lines, changed [start, end) in new numbering)."""
    total = len(lines)
    edits: List[Tuple[int, int, List[str], int]] = []  # (start index, end index exclusive, replacement, order)
    for order, op in enumerate(ops):
        kind = op["kind"]
        if kind in ("rem", "mv"):
            continue
        where = f"patch line {op['line']}"
        if kind in ("replace", "cut"):
            a, b = op["a"], op["b"]
            if a < 1 or b < a or b > total:
                raise ToolError(f"{where}: range {a}.={b} is outside the file (1-{total})")
            if kind == "cut":
                registers[op["reg"]] = lines[a - 1:b]
                registers[""] = lines[a - 1:b]
                edits.append((a - 1, b, [], order))
                continue
            start, end = a - 1, b
        else:
            if op["a"] == "$":
                if kind != "after":
                    raise ToolError(f"{where}: use `PUT >$:` to append")
                start = end = total
            else:
                n = int(op["a"])
                if n < 1 or n > max(total, 1) or (total == 0 and n != 1):
                    raise ToolError(f"{where}: line {n} is outside the file (1-{total})")
                start = end = (n - 1) if kind == "before" else min(n, total)
        if op.get("reg") is not None:
            if op["reg"] not in registers:
                raise ToolError(f"{where}: register @{op['reg'] or '(anonymous)'} is empty; CUT into it first")
            body = list(registers[op["reg"]])
        else:
            body = list(op["body"])
            if not body and kind != "replace":
                raise ToolError(f"{where}: an insert needs at least one `+` row")
        edits.append((start, end, body, order))
    spans = sorted((e for e in edits if e[1] > e[0]), key=lambda e: e[0])
    for x, y in zip(spans, spans[1:]):
        if y[0] < x[1]:
            raise ToolError(f"ranges {x[0] + 1}.={x[1]} and {y[0] + 1}.={y[1]} overlap; merge them into one operation")
    out = list(lines)
    changed: List[Tuple[int, int]] = []
    for start, end, body, _ in sorted(edits, key=lambda e: (e[0], e[1], e[3]), reverse=True):
        out[start:end] = body
        delta = len(body) - (end - start)
        changed = [(s + delta, e + delta) for s, e in changed]
        changed.append((start, start + len(body)))
    return out, sorted(changed)


def _excerpt(lines: List[str], changed: List[Tuple[int, int]]) -> str:
    if not lines:
        return "(empty file)"
    windows: List[List[int]] = []
    for s, e in changed:
        lo, hi = max(0, s - CONTEXT), min(len(lines), max(e, s + 1) + CONTEXT)
        if windows and lo <= windows[-1][1]:
            windows[-1][1] = max(windows[-1][1], hi)
        else:
            windows.append([lo, hi])
    parts = [numbered(lines[lo:hi], lo + 1) for lo, hi in windows[:6]]
    return "\n…\n".join(parts) + (f"\n… ({len(windows) - 6} more changed regions)" if len(windows) > 6 else "")


def _edit_tool(session: Any) -> ToolDef:
    def execute(_id, p, signal=None, on_update=None, ctx=None):
        patch = p.get("input") or p.get("patch")
        if not isinstance(patch, str) or not patch.strip():
            raise ToolError("input is required: a hashline patch starting with a `[path#TAG]` header")
        sections = parse_patch(patch)
        registers: Dict[str, List[str]] = {}
        report: List[str] = []
        files: List[str] = []
        for sec in sections:
            abs_path = resolve_to_cwd(sec["path"], session.cwd)
            with file_mutation_lock(abs_path):
                exists = os.path.isfile(abs_path)
                kinds = [o["kind"] for o in sec["ops"]]
                if not exists:
                    if sec["tag"] not in ("", "NEW", "0000") or any(k not in ("after", "mv") for k in kinds) or \
                            any(o["kind"] == "after" and o["a"] != "$" for o in sec["ops"]):
                        raise ToolError(f"{sec['path']} does not exist. To create it use `[{sec['path']}#NEW]` with `PUT >$:`")
                    text, bom, nl, trailing, lines = "", False, "\n", True, []
                else:
                    with open(abs_path, "rb") as f:
                        text, bom, nl = normalize(f.read())
                    current = tag_of(text)
                    if sec["tag"] != current:
                        raise ToolError(f"{sec['path']}: tag {sec['tag'] or '(missing)'} does not match the file (now "
                                        f"#{current}). It changed since you read it: read it again and rebuild the patch.")
                    lines, trailing = split_lines(text)
                if "rem" in kinds:
                    if len(kinds) > 1:
                        raise ToolError(f"{sec['path']}: REM cannot be combined with other operations")
                    os.remove(abs_path)
                    report.append(f"Deleted {sec['path']}")
                    files.append(abs_path)
                    continue
                new_lines, changed = apply_ops(lines, sec["ops"], registers)
                new_text = "\n".join(new_lines) + ("\n" if (trailing or not exists) and new_lines else "")
                dest = next((o["dest"] for o in sec["ops"] if o["kind"] == "mv"), None)
                target = resolve_to_cwd(dest, session.cwd) if dest else abs_path
                if dest and os.path.exists(target):
                    raise ToolError(f"cannot move to {dest}: it already exists")
                os.makedirs(os.path.dirname(target) or ".", exist_ok=True)
                with open(target, "wb") as f:
                    f.write((("﻿" if bom else "") + new_text.replace("\n", nl)).encode("utf-8"))
                if dest and exists:
                    os.remove(abs_path)
                shown = dest or sec["path"]
                added = sum(e - s for s, e in changed)
                verb = "Created" if not exists else ("Moved to" if dest and not changed else "Updated")
                report.append(f"{verb} [{shown}#{tag_of(new_text)}] ({len(lines)} -> {len(new_lines)} lines, {added} written)"
                              + (f"\n{_excerpt(new_lines, changed)}" if changed else ""))
                files.append(target)
        return text_result("\n\n".join(report), {"files": files})

    return ToolDef(
        name="edit", label="edit",
        description=(
            "Edit files with a hashline patch. Use only lines you saw in the latest `read` of the file.\n"
            "[PATH#TAG]            header per file; copy it from read (TAG = 4-hex snapshot)\n"
            "PUT N.=M:             replace lines N-M (inclusive) with the `+` rows that follow; `PUT N:` for one line\n"
            "PUT <N: / PUT >N:     insert the `+` rows before / after line N; `PUT >$:` appends\n"
            "CUT N.=M [@name]      delete lines (kept in a register); `PUT >N @name` pastes them, no body\n"
            "REM                   delete the file      MV DEST   rename it\n"
            "New file: `[path#NEW]` then `PUT >$:`.\n"
            "Rules: body rows start with `+` (a lone `+` is a blank line); the rest of the row is verbatim, indent "
            "included. Never send removed or unchanged lines. All numbers refer to the file as read, even with "
            "several operations in one patch; ranges must not overlap. The result shows the new tag and the changed "
            "lines renumbered, so you can chain another edit without re-reading."),
        parameters={"type": "object", "properties": {"input": {"type": "string", "description": "the patch"}},
                    "required": ["input"]},
        execute=execute, prompt_snippet="Edit files with line-addressed patches")


def _tools(session: Any) -> Dict[str, ToolDef]:
    return {"read": _wrap_read(session), "edit": _edit_tool(session)}


CAPABILITIES = [Capability("hashline", "Hashline edits", "read shows numbered lines with a snapshot tag; edit takes "
                           "line-addressed patches instead of old/new text.", "Oh My Pi hashline edit mode", tools=_tools)]
