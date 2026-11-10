"""Multi-line prompt editor with history, word motions and soft wrapping."""
from __future__ import annotations

from typing import List, Optional, Tuple

from .text import INV, RESET, char_width, width


class Editor:
    def __init__(self) -> None:
        self.lines: List[str] = [""]
        self.row = 0
        self.col = 0
        self.history: List[str] = []
        self.hist_idx: Optional[int] = None
        self.stash = ""

    # ------------------------------------------------------------------------------------------- content
    @property
    def text(self) -> str:
        return "\n".join(self.lines)

    def set_text(self, text: str) -> None:
        self.lines = text.split("\n") or [""]
        self.row = len(self.lines) - 1
        self.col = len(self.lines[-1])

    def set_cursor(self, row: int, col: int) -> None:
        self.row = max(0, min(row, len(self.lines) - 1))
        self.col = max(0, min(col, len(self.lines[self.row])))

    def clear(self) -> None:
        self.lines, self.row, self.col = [""], 0, 0
        self.hist_idx = None

    def is_empty(self) -> bool:
        return self.text == ""

    def insert(self, s: str) -> None:
        s = s.replace("\r\n", "\n").replace("\r", "\n").replace("\t", "    ")
        cur = self.lines[self.row]
        before, after = cur[: self.col], cur[self.col:]
        parts = s.split("\n")
        if len(parts) == 1:
            self.lines[self.row] = before + s + after
            self.col += len(s)
            return
        self.lines[self.row] = before + parts[0]
        new = parts[1:-1] + [parts[-1] + after]
        self.lines[self.row + 1:self.row + 1] = new
        self.row += len(parts) - 1
        self.col = len(parts[-1])

    def newline(self) -> None:
        self.insert("\n")

    def backspace(self) -> None:
        if self.col > 0:
            l = self.lines[self.row]
            self.lines[self.row] = l[: self.col - 1] + l[self.col:]
            self.col -= 1
        elif self.row > 0:
            prev = self.lines[self.row - 1]
            self.lines[self.row - 1] = prev + self.lines[self.row]
            del self.lines[self.row]
            self.row -= 1
            self.col = len(prev)

    def delete(self) -> None:
        l = self.lines[self.row]
        if self.col < len(l):
            self.lines[self.row] = l[: self.col] + l[self.col + 1:]
        elif self.row < len(self.lines) - 1:
            self.lines[self.row] = l + self.lines[self.row + 1]
            del self.lines[self.row + 1]

    def delete_word_back(self) -> None:
        l = self.lines[self.row]
        i = self.col
        while i > 0 and l[i - 1] == " ":
            i -= 1
        while i > 0 and l[i - 1] not in " /\\.,;:()[]{}\"'":
            i -= 1
        if i == self.col and self.col == 0:
            self.backspace()
            return
        self.lines[self.row] = l[:i] + l[self.col:]
        self.col = i

    def kill_to_start(self) -> None:
        l = self.lines[self.row]
        self.lines[self.row] = l[self.col:]
        self.col = 0

    def kill_to_end(self) -> None:
        l = self.lines[self.row]
        if self.col == len(l) and self.row < len(self.lines) - 1:
            self.delete()
        else:
            self.lines[self.row] = l[: self.col]

    # -------------------------------------------------------------------------------------------- motion
    def left(self) -> None:
        if self.col > 0:
            self.col -= 1
        elif self.row > 0:
            self.row -= 1
            self.col = len(self.lines[self.row])

    def right(self) -> None:
        if self.col < len(self.lines[self.row]):
            self.col += 1
        elif self.row < len(self.lines) - 1:
            self.row += 1
            self.col = 0

    def word_left(self) -> None:
        l = self.lines[self.row]
        i = self.col
        while i > 0 and not l[i - 1].isalnum():
            i -= 1
        while i > 0 and l[i - 1].isalnum():
            i -= 1
        self.col = i

    def word_right(self) -> None:
        l = self.lines[self.row]
        i = self.col
        while i < len(l) and not l[i].isalnum():
            i += 1
        while i < len(l) and l[i].isalnum():
            i += 1
        self.col = i

    def home(self) -> None:
        self.col = 0

    def end(self) -> None:
        self.col = len(self.lines[self.row])

    def up(self) -> bool:
        """Move up; returns False when already on the first line (caller may browse history)."""
        if self.row == 0:
            return False
        self.row -= 1
        self.col = min(self.col, len(self.lines[self.row]))
        return True

    def down(self) -> bool:
        if self.row >= len(self.lines) - 1:
            return False
        self.row += 1
        self.col = min(self.col, len(self.lines[self.row]))
        return True

    # ------------------------------------------------------------------------------------------- history
    def remember(self, text: str) -> None:
        if text.strip() and (not self.history or self.history[-1] != text):
            self.history.append(text)
        self.hist_idx = None

    def history_prev(self) -> None:
        if not self.history:
            return
        if self.hist_idx is None:
            self.stash = self.text
            self.hist_idx = len(self.history)
        if self.hist_idx > 0:
            self.hist_idx -= 1
            self.set_text(self.history[self.hist_idx])

    def history_next(self) -> None:
        if self.hist_idx is None:
            return
        self.hist_idx += 1
        if self.hist_idx >= len(self.history):
            self.hist_idx = None
            self.set_text(self.stash)
        else:
            self.set_text(self.history[self.hist_idx])

    # ---------------------------------------------------------------------------------------- completion
    def token_before_cursor(self) -> Tuple[str, int]:
        l = self.lines[self.row][: self.col]
        i = len(l)
        while i > 0 and not l[i - 1].isspace():
            i -= 1
        return l[i:], i

    def replace_token(self, start: int, new: str) -> None:
        l = self.lines[self.row]
        self.lines[self.row] = l[:start] + new + l[self.col:]
        self.col = start + len(new)

    # ------------------------------------------------------------------------------------------- render
    def render(self, cols: int, style: str = "", cursor: bool = True) -> List[str]:
        """Soft-wrapped lines with an inverse-video cursor cell."""
        out: List[str] = []
        cols = max(4, cols)
        for r, line in enumerate(self.lines):
            segs: List[Tuple[int, str]] = []  # (start index, text)
            start, w = 0, 0
            for i, ch in enumerate(line):
                cw = char_width(ch)
                if w + cw > cols - 1:
                    segs.append((start, line[start:i]))
                    start, w = i, 0
                w += cw
            segs.append((start, line[start:]))
            for si, (s0, seg) in enumerate(segs):
                last = si == len(segs) - 1
                if cursor and r == self.row and (s0 <= self.col < s0 + len(seg) or (last and self.col == s0 + len(seg))):
                    k = self.col - s0
                    under = seg[k] if k < len(seg) else " "
                    out.append(style + seg[:k] + INV + under + "\x1b[27m" + style + seg[k + 1:] + RESET)
                else:
                    out.append(style + seg + RESET)
        return out
