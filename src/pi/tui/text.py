"""ANSI-aware width, truncation, wrapping and styling helpers."""
from __future__ import annotations

import re
import unicodedata
from functools import lru_cache
from typing import List, Optional, Tuple

ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b_[^\x1b]*\x1b\\")
RESET = "\x1b[0m"


def hex_rgb(h: str) -> Tuple[int, int, int]:
    h = h.lstrip("#")
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


def fg(h: Optional[str]) -> str:
    if not h:
        return ""
    r, g, b = hex_rgb(h)
    return f"\x1b[38;2;{r};{g};{b}m"


def bg(h: Optional[str]) -> str:
    if not h:
        return ""
    r, g, b = hex_rgb(h)
    return f"\x1b[48;2;{r};{g};{b}m"


def blend(a: str, b: str, t: float) -> str:
    ra, ga, ba = hex_rgb(a)
    rb, gb, bb = hex_rgb(b)
    return "#%02x%02x%02x" % (int(ra + (rb - ra) * t), int(ga + (gb - ga) * t), int(ba + (bb - ba) * t))


BOLD, DIM, ITALIC, UNDER, INV = "\x1b[1m", "\x1b[2m", "\x1b[3m", "\x1b[4m", "\x1b[7m"
NOBOLD, NOITALIC, NOUNDER = "\x1b[22m", "\x1b[23m", "\x1b[24m"


def strip(s: str) -> str:
    return ANSI_RE.sub("", s)


@lru_cache(maxsize=4096)
def char_width(ch: str) -> int:
    cp = ord(ch)
    if cp < 32 or 0x7F <= cp < 0xA0:
        return 0
    if unicodedata.combining(ch) or cp in (0x200D,) or 0xFE00 <= cp <= 0xFE0F:
        return 0
    ea = unicodedata.east_asian_width(ch)
    if ea in ("W", "F"):
        return 2
    if 0x1F300 <= cp <= 0x1FAFF:
        return 2
    return 1


def width(s: str) -> int:
    return sum(char_width(c) for c in strip(s))


def _tokens(s: str):
    pos = 0
    for m in ANSI_RE.finditer(s):
        if m.start() > pos:
            for ch in s[pos:m.start()]:
                yield ch, False
        yield m.group(0), True
        pos = m.end()
    for ch in s[pos:]:
        yield ch, False


def truncate(s: str, cols: int, ellipsis: str = "…") -> str:
    if width(s) <= cols:
        return s
    target = max(0, cols - width(ellipsis))
    out, w = [], 0
    for tok, is_ansi in _tokens(s):
        if is_ansi:
            out.append(tok)
            continue
        cw = char_width(tok)
        if w + cw > target:
            break
        out.append(tok)
        w += cw
    return "".join(out) + RESET + ellipsis


def pad(s: str, cols: int, align: str = "left") -> str:
    s = truncate(s, cols)
    gap = max(0, cols - width(s))
    if align == "right":
        return " " * gap + s
    if align == "center":
        return " " * (gap // 2) + s + " " * (gap - gap // 2)
    return s + " " * gap


def wrap(s: str, cols: int) -> List[str]:
    """Word-wrap one logical line (may contain ANSI) to `cols`, carrying active SGR state across breaks."""
    cols = max(1, cols)
    if width(s) <= cols:
        return [s]
    lines: List[str] = []
    cur: List[str] = []
    cur_w = 0
    active = ""
    last_space = -1
    for tok, is_ansi in _tokens(s):
        if is_ansi:
            cur.append(tok)
            active = "" if tok in (RESET, "\x1b[m") else active + tok
            continue
        cw = char_width(tok)
        if cur_w + cw > cols:
            if tok == " ":
                lines.append("".join(cur) + RESET)
                cur, cur_w, last_space = [active], 0, -1
                continue
            if last_space > 0:
                head, tail = cur[:last_space], cur[last_space + 1:]
                lines.append("".join(head) + RESET)
                cur = [active] + tail
                cur_w = sum(char_width(t) for t in tail if not t.startswith("\x1b"))
            else:
                lines.append("".join(cur) + RESET)
                cur, cur_w = [active], 0
            last_space = -1
        if tok == " ":
            last_space = len(cur)
        cur.append(tok)
        cur_w += cw
    lines.append("".join(cur))
    return lines


def wrap_text(text: str, cols: int) -> List[str]:
    out: List[str] = []
    for line in text.split("\n"):
        out.extend(wrap(line.expandtabs(4), cols))
    return out


def gradient(text: str, colors: List[str], bold: bool = True) -> str:
    """Horizontal colour gradient across visible characters."""
    chars = [c for c in text]
    n = max(1, len(chars) - 1)
    out = []
    for i, ch in enumerate(chars):
        t = i / n
        seg = t * (len(colors) - 1)
        k = min(int(seg), len(colors) - 2)
        col = blend(colors[k], colors[k + 1], seg - k)
        out.append(fg(col) + ch)
    return (BOLD if bold else "") + "".join(out) + RESET
