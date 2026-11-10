"""read / write / edit / ls - ports of the Pi tools with identical names, schemas, descriptions and messages."""
from __future__ import annotations

import base64
import difflib
import json
import os
import unicodedata
import re
from typing import Any, Dict, List, Optional, Tuple

from .base import ToolDef, ToolError, check_abort, file_mutation_lock, resolve_to_cwd, text_result
from .truncate import DEFAULT_MAX_BYTES, DEFAULT_MAX_LINES, format_size, truncate_head

IMAGE_MIME = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".gif": "image/gif",
              ".webp": "image/webp", ".bmp": "image/bmp"}


def _sniff_image(path: str) -> Optional[str]:
    try:
        with open(path, "rb") as f:
            head = f.read(16)
    except OSError:
        return None
    if head.startswith(b"\x89PNG"):
        return "image/png"
    if head[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if head[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "image/webp"
    if head[:2] == b"BM" and os.path.splitext(path)[1].lower() == ".bmp":
        return "image/bmp"
    return None


# ------------------------------------------------------------------------------------------------- read
def make_read(cwd: str) -> ToolDef:
    def execute(_id, params, signal=None, on_update=None, ctx=None):
        check_abort(signal)
        path = params["path"]
        offset = params.get("offset")
        limit = params.get("limit")
        abs_path = resolve_to_cwd(path, getattr(ctx, "cwd", None) or cwd)
        if not os.path.exists(abs_path):
            raise ToolError(f"ENOENT: no such file or directory, access '{abs_path}'")
        if os.path.isdir(abs_path):
            raise ToolError(f"EISDIR: illegal operation on a directory, read '{abs_path}'")
        mime = _sniff_image(abs_path)
        if mime:
            from ..images import process_image
            with open(abs_path, "rb") as f:
                raw_bytes = f.read()
            sess = getattr(ctx, "session", None)
            auto = True
            try:
                auto = sess.settings.get("images.autoResize", True) is not False if sess is not None else True
            except Exception:
                pass
            processed = process_image(raw_bytes, mime, auto)
            model = getattr(ctx, "model", None)
            non_vision = ("[Current model does not support images. The image will be omitted from this request.]"
                          if model and "image" not in (model.get("input") or []) else None)
            if not processed["ok"]:
                note = f"Read image file [{mime}]\n{processed['message']}" + (f"\n{non_vision}" if non_vision else "")
                return {"content": [{"type": "text", "text": note}], "details": None}
            note = f"Read image file [{processed['mimeType']}]"
            if processed["hints"]:
                note += "\n" + "\n".join(processed["hints"])
            if non_vision:
                note += f"\n{non_vision}"
            return {"content": [{"type": "text", "text": note}, {"type": "image", "data": processed["data"],
                                                                  "mimeType": processed["mimeType"]}], "details": None}
        with open(abs_path, "rb") as f:
            raw = f.read()
        text = raw.decode("utf-8", "replace")
        if text.startswith("﻿"):
            text = text[1:]
        text = text.replace("\r\n", "\n")  # CRLF files read like LF ones (edit preserves CRLF on write)
        all_lines = text.split("\n")
        if text.endswith("\n"):
            all_lines.pop()
        total = len(all_lines)
        start = max(0, int(offset) - 1) if offset else 0
        if start >= total and not (total == 0 and start == 0):
            raise ToolError(f"Offset {offset} is beyond end of file ({total} lines total)")
        end = total if limit is None else min(start + int(limit), total)
        selected = "\n".join(all_lines[start:end])
        tr = truncate_head(selected)
        start_disp = start + 1
        details = None
        if tr["firstLineExceedsLimit"]:
            size = format_size(len(all_lines[start].encode("utf-8")))
            out = (f"[Line {start_disp} is {size}, exceeds {format_size(DEFAULT_MAX_BYTES)} limit. "
                   f"Use bash: sed -n '{start_disp}p' {path} | head -c {DEFAULT_MAX_BYTES}]")
            details = {"truncation": tr}
        elif tr["truncated"]:
            end_disp = start_disp + tr["outputLines"] - 1
            nxt = end_disp + 1
            out = tr["content"]
            if tr["truncatedBy"] == "lines":
                out += f"\n\n[Showing lines {start_disp}-{end_disp} of {total}. Use offset={nxt} to continue.]"
            else:
                out += (f"\n\n[Showing lines {start_disp}-{end_disp} of {total} ({format_size(DEFAULT_MAX_BYTES)} limit). "
                        f"Use offset={nxt} to continue.]")
            details = {"truncation": tr}
        elif limit is not None and end < total:
            out = f"{tr['content']}\n\n[{total - end} more lines in file. Use offset={end + 1} to continue.]"
        else:
            out = tr["content"]
        return {"content": [{"type": "text", "text": out}], "details": details}

    return ToolDef(
        name="read", label="read",
        description=(f"Read the contents of a file. Supports text files and images (jpg, png, gif, webp, bmp). Images are sent as "
                     f"attachments. For text files, output is truncated to {DEFAULT_MAX_LINES} lines or {DEFAULT_MAX_BYTES // 1024}KB "
                     f"(whichever is hit first). Use offset/limit for large files. When you need the full file, continue with offset "
                     f"until complete."),
        parameters={"type": "object", "properties": {
            "path": {"type": "string", "description": "Path to the file to read (relative or absolute)"},
            "offset": {"type": "number", "description": "Line number to start reading from (1-indexed)"},
            "limit": {"type": "number", "description": "Maximum number of lines to read"}}, "required": ["path"]},
        execute=execute, prompt_snippet="Read file contents",
        prompt_guidelines=["Use read to examine files instead of cat or sed."], read_only=True)


# ------------------------------------------------------------------------------------------------ write
def make_write(cwd: str) -> ToolDef:
    def execute(_id, params, signal=None, on_update=None, ctx=None):
        check_abort(signal)
        path, content = params["path"], params.get("content", "")
        abs_path = resolve_to_cwd(path, getattr(ctx, "cwd", None) or cwd)
        with file_mutation_lock(abs_path):
            d = os.path.dirname(abs_path)
            if d:
                os.makedirs(d, exist_ok=True)
            check_abort(signal)
            with open(abs_path, "w", encoding="utf-8", newline="") as f:
                f.write(content)
        return text_result(f"Successfully wrote to {path}")

    return ToolDef(
        name="write", label="write",
        description=("Write content to a file. Creates the file if it doesn't exist, overwrites if it does. "
                     "Automatically creates parent directories."),
        parameters={"type": "object", "properties": {
            "path": {"type": "string", "description": "Path to the file to write (relative or absolute)"},
            "content": {"type": "string", "description": "Content to write to the file"}}, "required": ["path", "content"]},
        execute=execute, prompt_snippet="Create or overwrite files",
        prompt_guidelines=["Use write only for new files or complete rewrites."])


# ------------------------------------------------------------------------------------------------- edit
def normalize_for_fuzzy(text: str) -> str:
    text = unicodedata.normalize("NFKC", text)
    text = "\n".join(line.rstrip() for line in text.split("\n"))
    text = re.sub("[‘’‚‛]", "'", text)
    text = re.sub("[“”„‟]", '"', text)
    text = re.sub("[‐‑‒–—―−]", "-", text)
    text = re.sub("[  -   　]", " ", text)
    return text


def apply_edits(content: str, edits: List[Dict[str, str]], path: str) -> Tuple[str, str, int]:
    """Returns (base_content, new_content, first_changed_line). Raises ToolError with Pi's messages."""
    n = len(edits)
    for i, e in enumerate(edits):
        if not e.get("oldText"):
            raise ToolError(f"oldText must not be empty in {path}." if n == 1 else f"edits[{i}].oldText must not be empty in {path}.")
    use_fuzzy = any(content.find(e["oldText"].replace("\r\n", "\n")) == -1 for e in edits)
    base = normalize_for_fuzzy(content) if use_fuzzy else content
    spans: List[Tuple[int, int, str, int]] = []
    for i, e in enumerate(edits):
        old = e["oldText"].replace("\r\n", "\n")
        new = (e.get("newText") or "").replace("\r\n", "\n")
        needle = normalize_for_fuzzy(old) if use_fuzzy else old
        idx = base.find(needle)
        if idx == -1:
            raise ToolError(f"Could not find the exact text in {path}. The old text must match exactly including all whitespace and newlines."
                            if n == 1 else f"Could not find edits[{i}] in {path}. The oldText must match exactly including all whitespace and newlines.")
        count = normalize_for_fuzzy(content).count(normalize_for_fuzzy(old))
        if count > 1:
            raise ToolError(f"Found {count} occurrences of the text in {path}. The text must be unique. Please provide more context to make it unique."
                            if n == 1 else f"Found {count} occurrences of edits[{i}] in {path}. Each oldText must be unique. Please provide more context to make it unique.")
        spans.append((idx, idx + len(needle), new, i))
    spans.sort()
    for a, b in zip(spans, spans[1:]):
        if b[0] < a[1]:
            raise ToolError(f"edits[{a[3]}] and edits[{b[3]}] overlap in {path}. Merge them into one edit or target disjoint regions.")
    out = base
    for start, end, new, _ in reversed(spans):
        out = out[:start] + new + out[end:]
    if out == base:
        raise ToolError(f"No changes made to {path}. The replacement produced identical content. This might indicate an issue with special "
                        f"characters or the text not existing as expected." if n == 1 else
                        f"No changes made to {path}. The replacements produced identical content.")
    first_line = base[:spans[0][0]].count("\n") + 1
    return base, out, first_line


def unified_diff(path: str, old: str, new: str, context: int = 4) -> str:
    return "".join(difflib.unified_diff(old.splitlines(True), new.splitlines(True), f"a/{path}", f"b/{path}", n=context))


def prepare_edit_arguments(args: Any) -> Any:
    if isinstance(args, dict):
        if "edits" not in args and "oldText" in args:
            args = {"path": args.get("path"), "edits": [{"oldText": args["oldText"], "newText": args.get("newText", "")}]}
        elif isinstance(args.get("edits"), str):
            try:
                args = dict(args, edits=json.loads(args["edits"]))
            except json.JSONDecodeError:
                pass
        if isinstance(args.get("edits"), dict):
            args = dict(args, edits=[args["edits"]])
    return args


def make_edit(cwd: str) -> ToolDef:
    def execute(_id, params, signal=None, on_update=None, ctx=None):
        params = prepare_edit_arguments(params)
        path = params["path"]
        edits = params.get("edits") or []
        if not isinstance(edits, list) or not edits:
            raise ToolError("Edit tool input is invalid. edits must contain at least one replacement.")
        abs_path = resolve_to_cwd(path, getattr(ctx, "cwd", None) or cwd)
        with file_mutation_lock(abs_path):
            check_abort(signal)
            if not os.path.isfile(abs_path):
                raise ToolError(f"Could not edit file: {path}. Error code: ENOENT.")
            with open(abs_path, "rb") as f:
                raw = f.read().decode("utf-8")
            bom = "﻿" if raw.startswith("﻿") else ""
            text = raw[len(bom):]
            crlf = "\r\n" in text
            normalized = text.replace("\r\n", "\n")
            base, new, first_line = apply_edits(normalized, edits, path)
            diff = unified_diff(path, base, new)
            check_abort(signal)
            final = new.replace("\n", "\r\n") if crlf else new
            with open(abs_path, "w", encoding="utf-8", newline="") as f:
                f.write(bom + final)
        return {"content": [{"type": "text", "text": f"Successfully replaced {len(edits)} block(s) in {path}."}],
                "details": {"diff": diff, "firstChangedLine": first_line}}

    return ToolDef(
        name="edit", label="edit",
        description=("Edit a single file using exact text replacement. Every edits[].oldText must match a unique, non-overlapping region "
                     "of the original file. If two changes affect the same block or nearby lines, merge them into one edit instead of "
                     "emitting overlapping edits. Do not include large unchanged regions just to connect distant changes."),
        parameters={"type": "object", "properties": {
            "path": {"type": "string", "description": "Path to the file to edit (relative or absolute)"},
            "edits": {"type": "array", "description": ("One or more targeted replacements. Each edit is matched against the original file, "
                                                       "not incrementally. Do not include overlapping or nested edits. If two changes touch "
                                                       "the same block or nearby lines, merge them into one edit instead."),
                      "items": {"type": "object", "properties": {
                          "oldText": {"type": "string", "description": ("Exact text for one targeted replacement. It must be unique in the "
                                                                        "original file and must not overlap with any other edits[].oldText "
                                                                        "in the same call.")},
                          "newText": {"type": "string", "description": "Replacement text for this targeted edit."}},
                          "required": ["oldText", "newText"]}}},
            "required": ["path", "edits"]},
        execute=execute,
        prompt_snippet="Make precise file edits with exact text replacement, including multiple disjoint edits in one call",
        prompt_guidelines=["Use edit for targeted changes. Batch changes to one file in a single call.",
                           "Each edits[].oldText must exactly and uniquely match the original file. Keep matches short and "
                           "non-overlapping; merge nearby changes without including large unchanged regions."],
        prepare_arguments=prepare_edit_arguments)


# --------------------------------------------------------------------------------------------------- ls
def make_ls(cwd: str) -> ToolDef:
    default_limit = 500

    def execute(_id, params, signal=None, on_update=None, ctx=None):
        check_abort(signal)
        path = params.get("path") or "."
        limit = max(1, int(params.get("limit") or default_limit))
        abs_path = resolve_to_cwd(path, getattr(ctx, "cwd", None) or cwd)
        if not os.path.exists(abs_path):
            raise ToolError(f"Path not found: {abs_path}")
        if not os.path.isdir(abs_path):
            raise ToolError(f"Not a directory: {abs_path}")
        try:
            names = sorted(os.listdir(abs_path), key=lambda s: s.lower())
        except OSError as e:
            raise ToolError(f"Cannot read directory: {e}")
        if not names:
            return text_result("(empty directory)")
        entries = []
        for nm in names[:limit]:
            entries.append(nm + ("/" if os.path.isdir(os.path.join(abs_path, nm)) else ""))
        tr = truncate_head("\n".join(entries), max_lines=10 ** 9)
        out = tr["content"]
        notices, details = [], {}
        if len(names) > limit:
            notices.append(f"{limit} entries limit reached. Use limit={limit * 2} for more")
            details["entryLimitReached"] = limit
        if tr["truncated"]:
            notices.append(f"{format_size(DEFAULT_MAX_BYTES)} limit reached")
            details["truncation"] = tr
        if notices:
            out += f"\n\n[{'. '.join(notices)}]"
        return {"content": [{"type": "text", "text": out}], "details": details or None}

    return ToolDef(
        name="ls", label="ls",
        description=(f"List directory contents. Returns entries sorted alphabetically, with '/' suffix for directories. Includes dotfiles. "
                     f"Output is truncated to {default_limit} entries or {DEFAULT_MAX_BYTES // 1024}KB (whichever is hit first)."),
        parameters={"type": "object", "properties": {
            "path": {"type": "string", "description": "Directory to list (default: current directory)"},
            "limit": {"type": "number", "description": "Maximum number of entries to return (default: 500)"}}},
        execute=execute, prompt_snippet="List directory contents", read_only=True)
