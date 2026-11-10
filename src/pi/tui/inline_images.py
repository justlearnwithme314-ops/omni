"""Inline images in the transcript (Pi's `terminal.showImages` / `terminal.images`).

Blocks reserve rows for an image and put a zero-width APC marker `\\x1b_omniimg:<id>\\x1b\\\\` on the first row.
After a frame is painted the app finds the markers and draws the images there with the kitty graphics protocol
(kitty, Ghostty, WezTerm) or iTerm2's inline images (iTerm2, WezTerm, VS Code). Other terminals show a text
placeholder.
"""
from __future__ import annotations

import base64
import hashlib
import io
import os
import re
from typing import Any, Dict, List, Optional, Tuple

MARKER_RE = re.compile(r"\x1b_omniimg:(\d+)\x1b\\")


def detect_protocol(settings_getter=None) -> Optional[str]:
    def get(k, d=None):
        try:
            return settings_getter(k, d) if settings_getter else d
        except Exception:
            return d
    if get("terminal.showImages", True) is False:
        return None
    forced = os.environ.get("OMNI_IMAGES") or get("terminal.images", "auto")
    if forced in ("kitty", "iterm2"):
        return forced
    if forced in ("none", "off", False):
        return None
    term = os.environ.get("TERM", "")
    prog = os.environ.get("TERM_PROGRAM", "")
    if os.environ.get("KITTY_WINDOW_ID") or "kitty" in term or prog.lower() in ("ghostty",) or os.environ.get("GHOSTTY_RESOURCES_DIR"):
        return "kitty"
    if prog in ("iTerm.app", "WezTerm", "vscode") or os.environ.get("LC_TERMINAL") == "iTerm2" or os.environ.get("WEZTERM_EXECUTABLE"):
        return "iterm2"
    return None


class ImageStore:
    def __init__(self) -> None:
        self.by_key: Dict[str, int] = {}
        self.items: Dict[int, Dict[str, Any]] = {}
        self.sent: set = set()                       # kitty: ids transmitted
        self.placed: Dict[int, Tuple[int, int]] = {}  # id -> (row, col) currently on screen

    def register(self, data: str, mime: str) -> int:
        key = hashlib.sha1((mime + data[:4096] + str(len(data))).encode()).hexdigest()
        if key in self.by_key:
            return self.by_key[key]
        iid = len(self.items) + 1
        self.by_key[key] = iid
        self.items[iid] = {"data": data, "mime": mime, "size": _size(data)}
        return iid

    def cells(self, iid: int, max_cols: int, max_rows: int = 18) -> Tuple[int, int]:
        w, h = self.items[iid]["size"] or (400, 300)
        cols = max(4, min(max_cols, w // 9 or 4))
        rows = max(1, min(max_rows, round(h / w * cols * 0.5)))  # cells are about twice as tall as wide
        return cols, rows


def _size(data: str) -> Optional[Tuple[int, int]]:
    try:
        from ..images import image_size
        return image_size(base64.b64decode(data[:65536] + "=" * (-len(data[:65536]) % 4)))
    except Exception:
        return None


def block_lines(store: ImageStore, protocol: Optional[str], data: str, mime: str, cols: int, label_style: str = "") -> List[str]:
    """Rows reserved for one image (or a one-line placeholder when images are not drawn)."""
    if not protocol:
        size = _size(data)
        dims = f" {size[0]}x{size[1]}" if size else ""
        return [label_style + f"[image {mime}{dims}]" + "\x1b[0m"]
    iid = store.register(data, mime)
    c, r = store.cells(iid, cols)
    store.items[iid]["cells"] = (c, r)
    return [f"\x1b_omniimg:{iid}\x1b\\"] + [""] * (r - 1)


def _png(data: str, mime: str) -> Optional[str]:
    if mime == "image/png":
        return data
    try:
        from PIL import Image  # type: ignore
        im = Image.open(io.BytesIO(base64.b64decode(data)))
        buf = io.BytesIO()
        im.save(buf, "PNG")
        return base64.b64encode(buf.getvalue()).decode("ascii")
    except Exception:
        return None


def _chunks(s: str, n: int = 4096):
    for i in range(0, len(s), n):
        yield s[i:i + n], i + n >= len(s)


def paint(store: ImageStore, protocol: Optional[str], placements: Dict[int, Tuple[int, int]], dirty_rows: set,
          height: int) -> str:
    """Escape sequences that draw `placements` ({id: (row0, col0)}, 0-based screen cells)."""
    if not protocol:
        return ""
    out: List[str] = []
    if protocol == "kitty":
        if placements != store.placed:
            out.append("\x1b_Ga=d,d=a,q=2\x1b\\")  # remove placements (keeps the transmitted images)
            for iid, (row, col) in placements.items():
                it = store.items[iid]
                cols, rows = it.get("cells", (20, 8))
                if row + rows > height:
                    continue
                if iid not in store.sent:
                    png = _png(it["data"], it["mime"])
                    if png is None:
                        continue
                    first = True
                    for chunk, last in _chunks(png):
                        head = f"a=t,f=100,i={iid},q=2," if first else ""
                        out.append(f"\x1b_G{head}m={0 if last else 1};{chunk}\x1b\\")
                        first = False
                    store.sent.add(iid)
                out.append(f"\x1b7\x1b[{row + 1};{col + 1}H\x1b_Ga=p,i={iid},c={cols},r={rows},C=1,q=2\x1b\\\x1b8")
    else:  # iterm2: redraw an image whenever it moved or one of its rows was repainted
        for iid, (row, col) in placements.items():
            it = store.items[iid]
            cols, rows = it.get("cells", (20, 8))
            if row + rows > height:
                continue
            if store.placed.get(iid) == (row, col) and not any(r in dirty_rows for r in range(row, row + rows)):
                continue
            size = len(base64.b64decode(it["data"] + "=" * (-len(it["data"]) % 4)))
            out.append(f"\x1b7\x1b[{row + 1};{col + 1}H\x1b]1337;File=inline=1;size={size};width={cols};height={rows};"
                       f"preserveAspectRatio=1:{it['data']}\x07\x1b8")
    store.placed = dict(placements)
    return "".join(out)


def extract(frame: List[str]) -> Tuple[List[str], Dict[int, Tuple[int, int]]]:
    """Strip markers from a frame; return the clean frame and {id: (row, col)}."""
    from .text import width
    placements: Dict[int, Tuple[int, int]] = {}
    out = []
    for r, line in enumerate(frame):
        m = MARKER_RE.search(line)
        if m:
            placements[int(m.group(1))] = (r, width(line[:m.start()]))
            line = MARKER_RE.sub("", line)
        out.append(line)
    return out, placements
