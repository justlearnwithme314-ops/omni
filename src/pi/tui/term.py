"""Terminal I/O: raw mode, alternate screen, mouse + bracketed paste, and a key parser. Windows (VT mode) and POSIX."""
from __future__ import annotations

import os
import queue
import re
import shutil
import sys
import threading
from dataclasses import dataclass
from typing import List, Optional, Tuple

IS_WINDOWS = os.name == "nt"


@dataclass
class Key:
    name: str            # "enter", "up", "ctrl+c", "a", "paste", "mouse", ...
    text: str = ""       # printable text (for chars and paste)
    raw: str = ""        # original sequence (forwarded to extension custom UIs)
    x: int = 0
    y: int = 0
    button: int = 0

    def __repr__(self) -> str:
        return f"Key({self.name!r})"


CSI_NAMES = {"A": "up", "B": "down", "C": "right", "D": "left", "H": "home", "F": "end", "Z": "shift+tab", "P": "f1", "Q": "f2",
             "R": "f3", "S": "f4"}
TILDE_NAMES = {"1": "home", "2": "insert", "3": "delete", "4": "end", "5": "pageup", "6": "pagedown", "7": "home", "8": "end",
               "15": "f5", "17": "f6", "18": "f7", "19": "f8", "20": "f9", "21": "f10", "23": "f11", "24": "f12"}
MOD_PREFIX = {2: "shift+", 3: "alt+", 4: "alt+shift+", 5: "ctrl+", 6: "ctrl+shift+", 7: "ctrl+alt+", 8: "ctrl+alt+shift+"}
_CSI_RE = re.compile(r"\x1b\[([0-9;<?]*)([A-Za-z~])")
_SS3_RE = re.compile(r"\x1bO([A-Za-z])")


def parse_keys(data: str) -> List[Key]:
    keys: List[Key] = []
    i = 0
    n = len(data)
    while i < n:
        c = data[i]
        if data.startswith("\x1b[200~", i):
            end = data.find("\x1b[201~", i + 6)
            if end == -1:
                end = n
            keys.append(Key("paste", data[i + 6:end].replace("\r\n", "\n").replace("\r", "\n"), raw=data[i:end + 6]))
            i = end + 6
            continue
        if c == "\x1b":
            m = _CSI_RE.match(data, i)
            if m:
                params, final = m.group(1), m.group(2)
                raw = m.group(0)
                i = m.end()
                if params.startswith("<"):  # SGR mouse
                    try:
                        b, x, y = (int(v) for v in params[1:].split(";"))
                    except ValueError:
                        continue
                    if final == "M":
                        name = "wheelup" if b == 64 else "wheeldown" if b == 65 else "mousedown"
                        keys.append(Key(name, raw=raw, x=x, y=y, button=b))
                    continue
                parts = params.split(";") if params else []
                mod = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 1
                prefix = MOD_PREFIX.get(mod, "")
                if final == "u" and parts:  # kitty keyboard protocol
                    code = int(parts[0]) if parts[0].isdigit() else 0
                    base = {13: "enter", 27: "escape", 9: "tab", 127: "backspace", 32: "space"}.get(code, chr(code) if code else "")
                    keys.append(Key(prefix + base, raw=raw))
                elif final == "~":
                    keys.append(Key(prefix + TILDE_NAMES.get(parts[0] if parts else "", "unknown"), raw=raw))
                elif final in CSI_NAMES:
                    keys.append(Key(prefix + CSI_NAMES[final] if final != "Z" else "shift+tab", raw=raw))
                else:
                    keys.append(Key("unknown", raw=raw))
                continue
            m = _SS3_RE.match(data, i)
            if m:
                keys.append(Key(CSI_NAMES.get(m.group(1), "unknown"), raw=m.group(0)))
                i = m.end()
                continue
            if i + 1 < n:
                nxt = data[i + 1]
                if nxt in "\r\n":
                    keys.append(Key("alt+enter", raw=data[i:i + 2]))
                elif nxt == "\x7f":
                    keys.append(Key("alt+backspace", raw=data[i:i + 2]))
                elif nxt == "\x1b":
                    keys.append(Key("escape", raw="\x1b"))
                    i += 1
                    continue
                else:
                    keys.append(Key("alt+" + nxt.lower(), raw=data[i:i + 2]))
                i += 2
                continue
            keys.append(Key("escape", raw="\x1b"))
            i += 1
            continue
        if c in "\r":
            keys.append(Key("enter", raw=c))
        elif c == "\n":
            keys.append(Key("ctrl+j", raw=c))
        elif c == "\t":
            keys.append(Key("tab", raw=c))
        elif c in "\x7f\x08":
            keys.append(Key("backspace", raw=c))
        elif c == "\x00":
            keys.append(Key("ctrl+space", raw=c))
        elif ord(c) < 32:
            keys.append(Key("ctrl+" + chr(ord(c) + 96), raw=c))
        else:
            j = i
            while j < n and data[j] >= " " and data[j] != "\x7f" and data[j] != "\x1b":
                j += 1
            chunk = data[i:j]
            if len(chunk) > 1 and keys == [] and j == n and "\n" not in chunk and len(chunk) > 8:
                keys.append(Key("paste", chunk, raw=chunk))  # terminals without bracketed paste deliver bursts
            else:
                for ch in chunk:
                    keys.append(Key(ch, ch, raw=ch))
            i = j
            continue
        i += 1
    return keys


class Terminal:
    def __init__(self) -> None:
        self.out = sys.stdout
        self.keys: "queue.Queue[Key]" = queue.Queue()
        self._stop = threading.Event()
        self._saved = None
        self._win_in_mode = None
        self._win_out_mode = None
        self.size = self.get_size()

    @staticmethod
    def is_tty() -> bool:
        return sys.stdin.isatty() and sys.stdout.isatty()

    def get_size(self) -> Tuple[int, int]:
        s = shutil.get_terminal_size((100, 30))
        return max(40, s.columns), max(12, s.lines)

    # ------------------------------------------------------------------------------------------------ mode
    def enter(self, alt_screen: bool = True, mouse: bool = True) -> None:
        if IS_WINDOWS:
            self._win_enter()
        else:
            import termios
            import tty
            fd = sys.stdin.fileno()
            self._saved = termios.tcgetattr(fd)
            tty.setraw(fd)
            attrs = termios.tcgetattr(fd)
            attrs[1] |= termios.OPOST | termios.ONLCR  # keep output post-processing
            termios.tcsetattr(fd, termios.TCSADRAIN, attrs)
        seq = ""
        self._alt = alt_screen
        if alt_screen:
            seq += "\x1b[?1049h"
        seq += "\x1b[?25l\x1b[?2004h"           # hide cursor, bracketed paste
        if mouse:
            seq += "\x1b[?1000h\x1b[?1006h"      # wheel/clicks, SGR encoding
        seq += "\x1b[>1u"                        # kitty keyboard protocol (ignored where unsupported)
        self.write(seq)
        self.flush()
        threading.Thread(target=self._reader, name="tty-reader", daemon=True).start()

    def leave(self) -> None:
        self._stop.set()
        self.write("\x1b[<u\x1b[?1000l\x1b[?1006l\x1b[?2004l\x1b[0m\x1b[?25h" + ("\x1b[?1049l" if getattr(self, "_alt", True) else "\r\n"))
        self.flush()
        if IS_WINDOWS:
            self._win_leave()
        elif self._saved is not None:
            import termios
            termios.tcsetattr(sys.stdin.fileno(), termios.TCSADRAIN, self._saved)

    def _win_enter(self) -> None:
        import ctypes
        from ctypes import wintypes
        k32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        hin, hout = k32.GetStdHandle(-10), k32.GetStdHandle(-11)
        mode = wintypes.DWORD()
        k32.GetConsoleMode(hout, ctypes.byref(mode))
        self._win_out_mode = mode.value
        k32.SetConsoleMode(hout, mode.value | 0x0004 | 0x0001)  # VT processing + processed output
        k32.GetConsoleMode(hin, ctypes.byref(mode))
        self._win_in_mode = mode.value
        new = (mode.value & ~(0x0001 | 0x0002 | 0x0004 | 0x0040)) | 0x0200 | 0x0080  # raw, VT input, no quick-edit
        k32.SetConsoleMode(hin, new)
        try:
            k32.SetConsoleOutputCP(65001)
        except Exception:
            pass

    def _win_leave(self) -> None:
        import ctypes
        k32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        if self._win_in_mode is not None:
            k32.SetConsoleMode(k32.GetStdHandle(-10), self._win_in_mode)
        if self._win_out_mode is not None:
            k32.SetConsoleMode(k32.GetStdHandle(-11), self._win_out_mode)

    # ----------------------------------------------------------------------------------------------- input
    def _reader(self) -> None:
        if IS_WINDOWS:
            self._win_reader()
            return
        import select
        fd = sys.stdin.fileno()
        buf = b""
        while not self._stop.is_set():
            r, _, _ = select.select([fd], [], [], 0.05)
            if not r:
                if buf:
                    self._emit(buf.decode("utf-8", "replace"))
                    buf = b""
                continue
            chunk = os.read(fd, 65536)
            if not chunk:
                break
            buf += chunk
            # wait briefly for the rest of an escape sequence / paste burst
            r2, _, _ = select.select([fd], [], [], 0.01)
            if r2 and len(buf) < 1 << 20:
                continue
            try:
                text = buf.decode("utf-8")
            except UnicodeDecodeError:
                continue  # incomplete multi-byte char; read more
            self._emit(text)
            buf = b""

    def _win_reader(self) -> None:
        import ctypes
        from ctypes import wintypes
        k32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        h = k32.GetStdHandle(-10)
        buf = ctypes.create_unicode_buffer(4096)
        n = wintypes.DWORD()
        pending = ""
        while not self._stop.is_set():
            if not k32.ReadConsoleW(h, buf, 4096, ctypes.byref(n), None):
                break
            text = pending + buf.value[: n.value]
            pending = ""
            if text.endswith("\x1b[200~") or ("\x1b[200~" in text and "\x1b[201~" not in text):
                pending = text
                continue
            self._emit(text)

    def _emit(self, text: str) -> None:
        for k in parse_keys(text):
            self.keys.put(k)

    # ---------------------------------------------------------------------------------------------- output
    def write(self, s: str) -> None:
        try:
            self.out.write(s)
        except UnicodeEncodeError:
            self.out.write(s.encode("utf-8", "replace").decode(self.out.encoding or "utf-8", "replace"))

    def flush(self) -> None:
        self.out.flush()

    def set_title(self, title: str) -> None:
        self.write(f"\x1b]0;{title}\x07")
