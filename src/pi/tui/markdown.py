"""Small Markdown -> ANSI renderer (headings, emphasis, code, lists, quotes, tables, rules) with light syntax colouring."""
from __future__ import annotations

import re
from typing import Dict, List

from .text import BOLD, DIM, ITALIC, NOBOLD, NOITALIC, RESET, UNDER, NOUNDER, bg, fg, pad, width, wrap

KEYWORDS = {
    "python": "def class return if elif else for while in not and or import from as with try except finally raise yield lambda "
              "pass break continue None True False async await global nonlocal is del assert",
    "js": "function const let var return if else for while do in of new class extends import from export default async await "
          "try catch finally throw typeof instanceof null undefined true false this super switch case break continue yield interface type enum",
    "bash": "if then else elif fi for in do done while case esac function return export local echo cd exit set unset source",
    "go": "func package import return if else for range var const type struct interface map chan go defer select switch case nil true false",
    "rust": "fn let mut pub use mod struct enum impl trait return if else match for while loop in as const static self Self true false None Some Ok Err",
}
ALIASES = {"ts": "js", "typescript": "js", "javascript": "js", "tsx": "js", "jsx": "js", "py": "python", "sh": "bash", "shell": "bash",
           "zsh": "bash", "powershell": "bash", "ps1": "bash", "rs": "rust", "golang": "go", "json": "js"}


def highlight(line: str, lang: str, pal: Dict[str, str]) -> str:
    lang = ALIASES.get(lang.lower(), lang.lower())
    kws = set(KEYWORDS.get(lang, "").split())
    comment = "#" if lang in ("python", "bash") else "//"
    out, i = [], 0
    pat = re.compile(r'("(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'|`(?:\\.|[^`\\])*`)|(\b\d+(?:\.\d+)?\b)|([A-Za-z_][A-Za-z0-9_]*)(\s*\()?|(\S)')
    ci = line.find(comment) if kws else -1
    code, cmt = (line[:ci], line[ci:]) if ci >= 0 and line[:ci].count('"') % 2 == 0 and line[:ci].count("'") % 2 == 0 else (line, "")
    for m in pat.finditer(code):
        out.append(code[i:m.start()])
        s, num, ident, call, punct = m.groups()
        if s:
            out.append(fg(pal["syntaxString"]) + s)
        elif num:
            out.append(fg(pal["syntaxNumber"]) + num)
        elif ident:
            if ident in kws:
                out.append(fg(pal["syntaxKeyword"]) + ident)
            elif call:
                out.append(fg(pal["syntaxFunction"]) + ident + fg(pal["syntaxPunctuation"]) + call)
            elif ident[:1].isupper():
                out.append(fg(pal["syntaxType"]) + ident)
            else:
                out.append(fg(pal["syntaxVariable"]) + ident)
        elif punct:
            out.append(fg(pal["syntaxOperator" if punct in "=+-*/<>!&|%^~?:" else "syntaxPunctuation"]) + punct)
        i = m.end()
    out.append(code[i:])
    if cmt:
        out.append(fg(pal["syntaxComment"]) + ITALIC + cmt + NOITALIC)
    return "".join(out) + RESET


def inline(text: str, pal: Dict[str, str], base: str = "") -> str:
    """Inline markdown; `base` is the colour to return to after a styled span."""
    restore = RESET + base
    text = re.sub(r"`([^`]+)`", lambda m: fg(pal["mdCode"]) + m.group(1) + restore, text)
    text = re.sub(r"\*\*([^*]+)\*\*|__([^_]+)__", lambda m: BOLD + (m.group(1) or m.group(2)) + NOBOLD, text)
    text = re.sub(r"(?<![*\w])\*([^*\s][^*]*)\*(?!\*)|(?<![_\w])_([^_\s][^_]*)_(?![_\w])",
                  lambda m: ITALIC + (m.group(1) or m.group(2)) + NOITALIC, text)
    text = re.sub(r"~~([^~]+)~~", lambda m: "\x1b[9m" + m.group(1) + "\x1b[29m", text)
    text = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", lambda m: fg(pal["mdLink"]) + UNDER + m.group(1) + NOUNDER + fg(pal["mdLinkUrl"])
                  + f" ({m.group(2)})" + restore, text)
    return base + text + RESET


def render_markdown(md: str, cols: int, pal: Dict[str, str]) -> List[str]:
    lines = md.replace("\r\n", "\n").split("\n")
    out: List[str] = []
    text_c = fg(pal["text"])
    i = 0
    while i < len(lines):
        line = lines[i]
        fence = re.match(r"^(\s*)(```+|~~~+)\s*([\w+#.-]*)", line)
        if fence:
            marker, lang = fence.group(2), fence.group(3)
            body = []
            i += 1
            while i < len(lines) and not lines[i].strip().startswith(marker[:3]):
                body.append(lines[i])
                i += 1
            i += 1
            border = fg(pal["mdCodeBlockBorder"])
            label = f" {lang} " if lang else ""
            out.append(border + "╭─" + fg(pal["muted"]) + label + border + "─" * max(0, cols - 3 - len(label)) + RESET)
            for b in body:
                for seg in wrap(highlight(b.expandtabs(4), lang or "", pal) if lang else fg(pal["mdCodeBlock"]) + b.expandtabs(4), cols - 2):
                    out.append(border + "│ " + RESET + seg)
            out.append(border + "╰" + "─" * max(0, cols - 1) + RESET)
            continue
        if re.match(r"^\s*\|.*\|\s*$", line) and i + 1 < len(lines) and re.match(r"^\s*\|?\s*:?-{2,}", lines[i + 1]):
            rows = []
            while i < len(lines) and re.match(r"^\s*\|.*\|\s*$", lines[i]):
                if not re.match(r"^\s*\|?[\s:|-]+\|?\s*$", lines[i]):
                    rows.append([c.strip() for c in lines[i].strip().strip("|").split("|")])
                i += 1
            out.extend(render_table(rows, cols, pal))
            continue
        h = re.match(r"^(#{1,6})\s+(.*)$", line)
        if h:
            level, title = len(h.group(1)), h.group(2)
            if out and out[-1] != "":
                out.append("")
            style = BOLD + fg(pal["mdHeading"]) + (UNDER if level == 1 else "")
            prefix = {1: "", 2: "", 3: "▸ "}.get(level, "· ")
            for seg in wrap(inline(prefix + title, pal, style), cols):
                out.append(seg)
            i += 1
            continue
        if re.match(r"^\s*([-*_])\s*(\1\s*){2,}$", line):
            out.append(fg(pal["mdHr"]) + "─" * cols + RESET)
            i += 1
            continue
        q = re.match(r"^\s*>\s?(.*)$", line)
        if q:
            for seg in wrap(inline(q.group(1), pal, fg(pal["mdQuote"]) + ITALIC), cols - 2):
                out.append(fg(pal["mdQuoteBorder"]) + "▌ " + RESET + seg)
            i += 1
            continue
        li = re.match(r"^(\s*)([-*+]|\d+[.)])\s+(\[[ xX]\]\s+)?(.*)$", line)
        if li:
            indent = len(li.group(1).expandtabs(4)) // 2 * 2
            bullet = li.group(2)
            task = li.group(3)
            if bullet in "-*+":
                bullet = "•" if indent == 0 else "◦"
            mark = ""
            if task:
                mark = (fg(pal["success"]) + "✔ " if task.strip()[1] in "xX" else fg(pal["muted"]) + "☐ ") + RESET
            lead = " " * indent + fg(pal["mdListBullet"]) + bullet + RESET + " " + mark
            body = wrap(inline(li.group(4), pal, text_c), max(10, cols - indent - width(bullet) - 1 - (2 if task else 0)))
            out.append(lead + body[0])
            hang = " " * (indent + width(bullet) + 1 + (2 if task else 0))
            out.extend(hang + b for b in body[1:])
            i += 1
            continue
        if not line.strip():
            if out and out[-1] != "":
                out.append("")
            i += 1
            continue
        para = [line.strip()]
        i += 1
        while i < len(lines) and lines[i].strip() and not re.match(r"^\s*(#{1,6}\s|```|~~~|>|[-*+]\s|\d+[.)]\s|\|)", lines[i]):
            para.append(lines[i].strip())
            i += 1
        out.extend(wrap(inline(" ".join(para), pal, text_c), cols))
    while out and out[-1] == "":
        out.pop()
    return out


def render_table(rows: List[List[str]], cols: int, pal: Dict[str, str]) -> List[str]:
    if not rows:
        return []
    n = max(len(r) for r in rows)
    rows = [r + [""] * (n - len(r)) for r in rows]
    widths = [max(width(inline(r[c], pal)) for r in rows) for c in range(n)]
    budget = cols - (3 * n + 1)
    while sum(widths) > budget and max(widths) > 4:
        k = widths.index(max(widths))
        widths[k] -= 1
    b = fg(pal["border"])
    top = b + "┌" + "┬".join("─" * (w + 2) for w in widths) + "┐" + RESET
    mid = b + "├" + "┼".join("─" * (w + 2) for w in widths) + "┤" + RESET
    bot = b + "└" + "┴".join("─" * (w + 2) for w in widths) + "┘" + RESET
    out = [top]
    for ri, r in enumerate(rows):
        style = BOLD + fg(pal["mdHeading"]) if ri == 0 else fg(pal["text"])
        cells = [pad(inline(r[c], pal, style), widths[c]) for c in range(n)]
        out.append(b + "│ " + RESET + (b + " │ " + RESET).join(cells) + b + " │" + RESET)
        if ri == 0:
            out.append(mid)
    out.append(bot)
    return out
