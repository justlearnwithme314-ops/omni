"""Port of coding-agent/src/core/tools/truncate.ts. Two independent limits; whichever is hit first wins."""
from __future__ import annotations

from typing import Any, Dict, Optional

DEFAULT_MAX_LINES = 2000
DEFAULT_MAX_BYTES = 50 * 1024
GREP_MAX_LINE_LENGTH = 500


def format_size(n: int) -> str:
    if n < 1024:
        return f"{n}B"
    if n < 1024 * 1024:
        return f"{n / 1024:.1f}KB"
    return f"{n / (1024 * 1024):.1f}MB"


def _lines(content: str):
    if not content:
        return []
    lines = content.split("\n")
    if content.endswith("\n"):
        lines.pop()
    return lines


def _nbytes(s: str) -> int:
    return len(s.encode("utf-8"))


def truncate_head(content: str, max_lines: Optional[int] = None, max_bytes: Optional[int] = None) -> Dict[str, Any]:
    max_lines = DEFAULT_MAX_LINES if max_lines is None else max_lines
    max_bytes = DEFAULT_MAX_BYTES if max_bytes is None else max_bytes
    lines = _lines(content)
    total_bytes = _nbytes(content)
    res = {"content": content, "truncated": False, "truncatedBy": None, "totalLines": len(lines),
           "totalBytes": total_bytes, "outputLines": len(lines), "outputBytes": total_bytes,
           "lastLinePartial": False, "firstLineExceedsLimit": False, "maxLines": max_lines, "maxBytes": max_bytes}
    if len(lines) <= max_lines and total_bytes <= max_bytes:
        return res
    if lines and _nbytes(lines[0]) > max_bytes:
        res.update(content="", truncated=True, truncatedBy="bytes", outputLines=0, outputBytes=0, firstLineExceedsLimit=True)
        return res
    out, used, by = [], 0, "lines"
    for i, line in enumerate(lines):
        if i >= max_lines:
            by = "lines"
            break
        b = _nbytes(line) + (1 if i > 0 else 0)
        if used + b > max_bytes:
            by = "bytes"
            break
        out.append(line)
        used += b
    text = "\n".join(out)
    res.update(content=text, truncated=True, truncatedBy=by, outputLines=len(out), outputBytes=_nbytes(text))
    return res


def truncate_tail(content: str, max_lines: Optional[int] = None, max_bytes: Optional[int] = None) -> Dict[str, Any]:
    max_lines = DEFAULT_MAX_LINES if max_lines is None else max_lines
    max_bytes = DEFAULT_MAX_BYTES if max_bytes is None else max_bytes
    lines = _lines(content)
    total_bytes = _nbytes(content)
    res = {"content": content, "truncated": False, "truncatedBy": None, "totalLines": len(lines),
           "totalBytes": total_bytes, "outputLines": len(lines), "outputBytes": total_bytes,
           "lastLinePartial": False, "firstLineExceedsLimit": False, "maxLines": max_lines, "maxBytes": max_bytes}
    if len(lines) <= max_lines and total_bytes <= max_bytes:
        return res
    out, used, by = [], 0, "lines"
    for i, line in enumerate(reversed(lines)):
        if i >= max_lines:
            by = "lines"
            break
        b = _nbytes(line) + (1 if out else 0)
        if used + b > max_bytes:
            by = "bytes"
            if not out:  # a single enormous last line: keep its tail
                raw = line.encode("utf-8")[-max_bytes:].decode("utf-8", "ignore")
                out.append(raw)
                res["lastLinePartial"] = True
            break
        out.append(line)
        used += b
    out.reverse()
    text = "\n".join(out)
    res.update(content=text, truncated=True, truncatedBy=by, outputLines=len(out), outputBytes=_nbytes(text))
    return res


def truncate_line(line: str, max_chars: int = GREP_MAX_LINE_LENGTH):
    if len(line) <= max_chars:
        return line, False
    return line[:max_chars] + "... [truncated]", True
