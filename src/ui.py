"""Terminal UI helpers: everything is computed from the *current* terminal width so resizing never breaks layout."""
import shutil
from typing import List, Tuple

Seg = Tuple[str, str]  # (style, text)


def term_width() -> int:
    return max(30, shutil.get_terminal_size((100, 30)).columns)


def _len(segs: List[Seg]) -> int:
    return sum(len(t) for _, t in segs)


def fit_bar(left: List[Seg], right: List[Seg], width: int = 0, fill_style: str = ""):
    """Full-width status bar: left segments, right-aligned segments, padded to `width`.

    Too narrow? Drop the right side first, then ellipsize the left, so the bar is never wider than the terminal
    (a bar wider than the window is what makes prompt_toolkit redraw badly on resize).
    """
    from prompt_toolkit.formatted_text import FormattedText

    width = (width or term_width()) - 1
    left, right = list(left), list(right)
    if _len(left) + _len(right) + 1 > width:
        right = []
    while _len(left) > width and left:
        style, text = left[-1]
        over = _len(left) - width
        if len(text) > over + 1:
            left[-1] = (style, text[: len(text) - over - 1] + "…")
        else:
            left.pop()
    pad = max(0, width - _len(left) - _len(right))
    return FormattedText(left + [(fill_style, " " * pad)] + right)


def header_rule(console, title: str, style: str) -> None:
    """A rule that spans the terminal as it is *now* (Rich reads the width at print time)."""
    console.rule(title, style=style, characters="─")


def short_path(p: str, width: int) -> str:
    return p if len(p) <= width else "…" + p[-(width - 1):]
