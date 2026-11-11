"""Transcript blocks: each renders itself to ANSI lines for a given width (cached until it changes)."""
from __future__ import annotations

import json
import os
import time
from typing import Any, Callable, Dict, List, Optional

from .markdown import highlight, render_markdown
from .text import BOLD, DIM, ITALIC, NOBOLD, NOITALIC, RESET, bg, fg, gradient, pad, strip, truncate, width, wrap, wrap_text
from .theme import BRAND_GRADIENT

SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
COLLAPSED_LINES = 8


def spinner_frame() -> str:
    return SPINNER[int(time.time() * 10) % len(SPINNER)]


def text_of(content: Any) -> str:
    if isinstance(content, str):
        return content
    return "".join(b.get("text", "") for b in content or [] if isinstance(b, dict) and b.get("type") == "text")


def panel(lines: List[str], cols: int, pal: Dict[str, str], border: str, title: str = "", right: str = "",
          bgc: Optional[str] = None, rounded: bool = True) -> List[str]:
    """A bordered card. `title`/`right` are already-styled strings placed on the top border."""
    tl, tr, bl, br = ("╭", "╮", "╰", "╯") if rounded else ("┌", "┐", "└", "┘")
    b = fg(border)
    fill = bg(bgc) if bgc else ""
    inner = cols - 4
    top_mid = cols - 2
    t = f" {title} " if title and width(title) else ""
    r = f" {right} " if right and width(right) else ""
    gap = max(0, top_mid - width(t) - width(r) - 1)
    out = [b + tl + "─" + RESET + t + b + "─" * gap + RESET + r + b + tr + RESET]
    for l in lines:
        out.append(b + "│" + RESET + fill + " " + pad(l + (fill if fill else ""), inner) + fill + " " + RESET + b + "│" + RESET)
    out.append(b + bl + "─" * (cols - 2) + br + RESET)
    return out


class Block:
    kind = "block"

    def __init__(self) -> None:
        self.version = 0
        self._cache_key = None
        self._cache: List[str] = []
        self.entry_id: Optional[str] = None

    def touch(self) -> None:
        self.version += 1

    def lines(self, cols: int, ctx: "RenderCtx") -> List[str]:
        key = (cols, self.version, ctx.expanded, ctx.show_thinking, ctx.pal_name, self.animated() and int(time.time() * 10))
        if key != self._cache_key:
            self._cache = self.render(cols, ctx)
            self._cache_key = key
        return self._cache

    def animated(self) -> bool:
        return False

    def render(self, cols: int, ctx: "RenderCtx") -> List[str]:
        return []


class RenderCtx:
    def __init__(self, pal: Dict[str, str], pal_name: str, expanded: bool, show_thinking: bool,
                 ext_render: Optional[Callable[[Dict[str, Any]], Optional[List[str]]]] = None, cwd: str = "",
                 md_transform: Optional[Callable[..., str]] = None, images: Any = None, image_protocol: Optional[str] = None):
        self.pal, self.pal_name, self.expanded, self.show_thinking, self.ext_render, self.cwd = pal, pal_name, expanded, show_thinking, ext_render, cwd
        self.md_transform = md_transform     # extension markdown transformers (display only)
        self.images = images                 # inline_images.ImageStore
        self.image_protocol = image_protocol

    def md(self, text: str, message_type: str, streaming: bool = False, cols: int = 80, entry_id: Optional[str] = None) -> str:
        if not self.md_transform:
            return text
        try:
            return self.md_transform(text, message_type, streaming, cols, entry_id)
        except Exception:
            return text

    def image_lines(self, data: str, mime: str, cols: int) -> List[str]:
        from .inline_images import block_lines
        if self.images is None:
            return [fg(self.pal["muted"]) + f"[image {mime}]" + RESET]
        return block_lines(self.images, self.image_protocol, data, mime, cols, fg(self.pal["muted"]))


# ---------------------------------------------------------------------------------------------- welcome
LOGO = [
    "  ██████╗ ███╗   ███╗███╗   ██╗██╗",
    " ██╔═══██╗████╗ ████║████╗  ██║██║",
    " ██║   ██║██╔████╔██║██╔██╗ ██║██║",
    " ██║   ██║██║╚██╔╝██║██║╚██╗██║██║",
    " ╚██████╔╝██║ ╚═╝ ██║██║ ╚████║██║",
    "  ╚═════╝ ╚═╝     ╚═╝╚═╝  ╚═══╝╚═╝",
]


class WelcomeBlock(Block):
    kind = "welcome"

    def __init__(self, info: Dict[str, Any]):
        super().__init__()
        self.info = info

    def render(self, cols, ctx):
        p = ctx.pal
        out = [""]
        if cols >= 44:
            for l in LOGO:
                out.append("  " + gradient(l, BRAND_GRADIENT))
        else:
            out.append("  " + gradient("◆ omni", BRAND_GRADIENT))
        out.append("  " + fg(p["accent"]) + BOLD + f"{self.info.get('profile', 'pi')} profile" + RESET + fg(p["muted"]) + ITALIC
                   + f" · {self.info.get('subtitle') or 'a minimal coding agent harness'}" + RESET)
        out.append("")
        rows = []
        lab = lambda s: fg(p["dim"]) + s.ljust(13) + RESET  # noqa: E731
        val = lambda s: fg(p["text"]) + s + RESET  # noqa: E731
        rows.append(lab("model") + val(self.info.get("model", "(none)")))
        rows.append(lab("profile") + fg(p["accent"]) + self.info.get("profile", "pi") + RESET + fg(p["dim"]) + "   /profile to switch" + RESET)
        rows.append(lab("cwd") + val(self.info.get("cwd", "")))
        for key in ("context", "skills", "prompts", "extensions", "tools", "omni tools"):
            items = self.info.get(key) or []
            if items:
                rows.append(lab(key) + fg(p["accent"]) + str(len(items)) + RESET + fg(p["muted"]) + "  " + ", ".join(items)[: max(10, cols - 30)] + RESET)
        for d in self.info.get("diagnostics", [])[:6]:
            rows.append(fg(p["warning"]) + "⚠ " + RESET + fg(p["muted"]) + d + RESET)
        card_w = min(cols - 2, 96)
        out += ["  " + l for l in panel(rows, card_w - 2, p, p["border"], title=fg(p["accent"]) + BOLD + "session" + RESET)]
        hints = [("enter", "send"), ("shift+enter", "newline"), ("esc", "interrupt"), ("/", "commands"), ("@", "files"), ("/profile", "switch"),
                 ("!", "shell"), ("ctrl+l", "model"), ("shift+tab", "thinking"), ("ctrl+o", "expand")]
        hl = "  " + "  ".join(fg(p["accent"]) + k + RESET + fg(p["dim"]) + " " + v + RESET for k, v in hints)
        out.extend("  " + l for l in wrap(hl.strip(), cols - 4))
        out.append("")
        return out


# ------------------------------------------------------------------------------------------------- user
class UserBlock(Block):
    kind = "user"

    def __init__(self, text: str, images: Any = 0):
        super().__init__()
        self.text = text
        self.image_blocks: List[Dict[str, Any]] = images if isinstance(images, list) else []
        self.images = len(self.image_blocks) if isinstance(images, list) else int(images or 0)

    def render(self, cols, ctx):
        p = ctx.pal
        b = bg(p["userMessageBg"])
        bar = fg(p["accent"]) + b + "▌" + RESET
        body = wrap_text(ctx.md(self.text.strip(), "user", False, cols - 4, self.entry_id) or "(empty)", cols - 4)
        out = [""]
        for i, l in enumerate(body):
            out.append(bar + b + " " + fg(p["userMessageText"]) + pad(l, cols - 3) + b + " " + RESET)
        if self.image_blocks and ctx.image_protocol:
            for im in self.image_blocks:
                out.extend("  " + l for l in ctx.image_lines(im.get("data", ""), im.get("mimeType", "image/png"), cols - 4))
        elif self.images:
            out.append(bar + b + " " + fg(p["muted"]) + pad(f"[{self.images} image(s) attached]", cols - 3) + " " + RESET)
        return out


# -------------------------------------------------------------------------------------------- assistant
class AssistantBlock(Block):
    kind = "assistant"

    def __init__(self, message: Dict[str, Any]):
        super().__init__()
        self.message = message
        self.streaming = False

    def animated(self):
        return self.streaming

    def render(self, cols, ctx):
        p = ctx.pal
        m = self.message
        out: List[str] = [""]
        for b in m.get("content") or []:
            t = b.get("type")
            if t == "thinking" and b.get("thinking", "").strip():
                head = fg(p["thinkingText"]) + ITALIC + ("✻ thinking" if ctx.show_thinking else "✻ thinking (ctrl+t to show)") + RESET
                out.append(head)
                if ctx.show_thinking:
                    for l in wrap_text(ctx.md(b["thinking"].strip(), "assistant-thinking", self.streaming, cols - 2, self.entry_id), cols - 2):
                        out.append(fg(p["borderMuted"]) + "│ " + RESET + fg(p["thinkingText"]) + ITALIC + l + RESET)
            elif t == "text" and b.get("text"):
                out.extend(render_markdown(ctx.md(b["text"], "assistant", self.streaming, cols, self.entry_id), cols, p))
        if self.streaming and len(out) == 1:
            out.append(fg(p["accent"]) + spinner_frame() + RESET + fg(p["muted"]) + " thinking…" + RESET)
        sr = m.get("stopReason")
        if sr == "error":
            msg = m.get("errorMessage") or "Unknown error"
            out += panel(wrap_text(msg, cols - 6), cols, p, p["error"], title=fg(p["error"]) + BOLD + "✗ error" + RESET)
        elif sr == "aborted":
            out.append(fg(p["warning"]) + "⏹ interrupted" + RESET)
        elif sr == "length":
            out.append(fg(p["warning"]) + "⚠ response hit the output token limit" + RESET)
        if len(out) == 1:
            return []
        return out


# ------------------------------------------------------------------------------------------------- tool
def _short(path: str, cwd: str) -> str:
    try:
        if cwd and os.path.isabs(path) and os.path.commonpath([os.path.abspath(path), cwd]) == cwd:
            return os.path.relpath(path, cwd)
    except ValueError:
        pass
    return path


def tool_summary(name: str, args: Dict[str, Any], cwd: str) -> str:
    a = args or {}
    if name == "bash":
        return "$ " + str(a.get("command", "")).replace("\n", " ⏎ ")
    if name == "read":
        rng = ""
        if a.get("offset") or a.get("limit"):
            start = int(a.get("offset") or 1)
            rng = f":{start}" + (f"-{start + int(a['limit']) - 1}" if a.get("limit") else "")
        return _short(str(a.get("path", "")), cwd) + rng
    if name == "edit" and isinstance(a.get("input"), str):  # hashline patch: show the files it names
        import re as _re
        heads = _re.findall(r"^\[(.+?)(?:#\w+)?\]\s*$", a["input"], _re.M)
        ops = len(_re.findall(r"^(?:PUT|CUT|REM|MV)\b", a["input"], _re.M))
        return ", ".join(_short(h, cwd) for h in heads[:3]) + f"  ({ops} op{'s' if ops != 1 else ''})"
    if name in ("write", "edit"):
        extra = f"  ({len(a.get('edits') or [])} edit{'s' if len(a.get('edits') or []) != 1 else ''})" if name == "edit" else (
            f"  ({str(a.get('content', '')).count(chr(10)) + 1} lines)" if name == "write" else "")
        return _short(str(a.get("path", "")), cwd) + extra
    if name == "grep":
        return f"/{a.get('pattern', '')}/" + (f" in {a['path']}" if a.get("path") else "") + (f"  {a['glob']}" if a.get("glob") else "")
    if name == "find":
        return str(a.get("pattern", "")) + (f" in {a['path']}" if a.get("path") else "")
    if name == "ls":
        return str(a.get("path") or ".")
    s = json.dumps(a, ensure_ascii=False)
    return s if len(s) < 200 else s[:197] + "…"


TOOL_ICONS = {"bash": "❯", "read": "◉", "write": "✎", "edit": "✎", "grep": "⌕", "find": "⌕", "ls": "▤"}


class ToolBlock(Block):
    kind = "tool"

    def __init__(self, call_id: str, name: str, args: Dict[str, Any], has_renderer: bool = False):
        super().__init__()
        self.call_id, self.name, self.args = call_id, name, args or {}
        self.result: Optional[Dict[str, Any]] = None
        self.partial: Optional[Dict[str, Any]] = None
        self.status = "pending"  # pending | done | error
        self.started = time.time()
        self.ended: Optional[float] = None
        self.has_renderer = has_renderer
        self.expanded_local = False

    def animated(self):
        return self.status == "pending"

    def render(self, cols, ctx):
        p = ctx.pal
        expanded = ctx.expanded or self.expanded_local
        if self.has_renderer and ctx.ext_render:
            call = ctx.ext_render({"kind": "toolCall", "name": self.name, "args": self.args, "toolCallId": self.call_id, "width": cols - 4})
            res = None
            if self.result or self.partial:
                res = ctx.ext_render({"kind": "toolResult", "name": self.name, "args": self.args, "toolCallId": self.call_id,
                                      "result": self.result or self.partial, "isPartial": self.result is None, "expanded": expanded,
                                      "width": cols - 4})
            if call is not None or res is not None:
                body = (call or []) + (res or [])
                return [""] + self._card(body, cols, ctx, show_summary=call is None)
        result = self.result or self.partial
        body: List[str] = []
        img_rows: List[str] = []
        if result:
            if self.name == "edit" and self.result and (self.result.get("details") or {}).get("diff"):
                body = self._diff(self.result["details"]["diff"], p)
            else:
                txt = text_of(result.get("content"))
                imgs = sum(1 for b in result.get("content") or [] if b.get("type") == "image")
                if self.name == "write" and self.status == "done" and not txt.startswith("Successfully"):
                    pass
                raw = txt.rstrip("\n").split("\n") if txt else []
                colour = fg(p["error"]) if self.status == "error" else fg(p["toolOutput"])
                body = [colour + l + RESET for l in raw]
                if imgs and ctx.image_protocol:
                    for im in (b for b in result.get("content") or [] if b.get("type") == "image"):
                        img_rows.extend(ctx.image_lines(im.get("data", ""), im.get("mimeType", "image/png"), cols - 6))
                elif imgs:
                    body.append(fg(p["muted"]) + f"[{imgs} image attachment(s)]" + RESET)
        elif self.name == "write" and self.args.get("content"):
            lang = os.path.splitext(str(self.args.get("path", "")))[1].lstrip(".")
            body = [highlight(l, lang, p) for l in str(self.args["content"]).split("\n")]
        wrapped: List[str] = []
        for l in body:
            wrapped.extend(wrap(l.expandtabs(4), cols - 4))
        limit = COLLAPSED_LINES if not expanded else 10 ** 9
        if len(wrapped) > limit:
            hidden = len(wrapped) - limit
            if self.name == "bash" and self.status != "pending":  # bash: show the tail, like Pi
                wrapped = [fg(p["dim"]) + f"… {hidden} earlier lines (ctrl+o to expand)" + RESET] + wrapped[-limit:]
            else:
                wrapped = wrapped[:limit] + [fg(p["dim"]) + f"… {hidden} more lines (ctrl+o to expand)" + RESET]
        return [""] + self._card(wrapped + img_rows, cols, ctx)

    def _card(self, body: List[str], cols: int, ctx: RenderCtx, show_summary: bool = True) -> List[str]:
        p = ctx.pal
        if self.status == "pending":
            icon, col = fg(p["accent"]) + spinner_frame() + RESET, p["borderAccent"]
        elif self.status == "error":
            icon, col = fg(p["error"]) + "✗" + RESET, p["error"]
        else:
            icon, col = fg(p["success"]) + "✓" + RESET, p["border"]
        title = icon + " " + fg(p["toolTitle"]) + BOLD + self.name + RESET
        summary = tool_summary(self.name, self.args, ctx.cwd) if show_summary else ""
        dur = (self.ended or time.time()) - self.started
        right = fg(p["dim"]) + (f"{dur:.1f}s" if dur >= 0.1 else "") + RESET
        lines = []
        if summary:
            colour = fg(p["bashMode"]) if self.name == "bash" else fg(p["text"])
            for i, l in enumerate(wrap(summary, cols - 4)):
                lines.append(colour + (BOLD if i == 0 and self.name == "bash" else "") + l + RESET)
        if body:
            if lines:
                lines.append(fg(p["borderMuted"]) + "┄" * (cols - 4) + RESET)
            lines.extend(body)
        bgc = p["toolErrorBg"] if self.status == "error" else p["toolPendingBg"] if self.status == "pending" else None
        return panel(lines, cols, p, col, title=title, right=right, bgc=bgc)

    @staticmethod
    def _diff(diff: str, p: Dict[str, str]) -> List[str]:
        out = []
        for l in diff.split("\n"):
            if l.startswith(("---", "+++")):
                continue
            if l.startswith("@@"):
                out.append(fg(p["accentAlt"]) + l + RESET)
            elif l.startswith("+"):
                out.append(fg(p["toolDiffAdded"]) + bg("#14301f" if p["text"] != "#1f2335" else "#e6f4ea") + l + RESET)
            elif l.startswith("-"):
                out.append(fg(p["toolDiffRemoved"]) + bg("#33161c" if p["text"] != "#1f2335" else "#fbeaea") + l + RESET)
            elif l:
                out.append(fg(p["toolDiffContext"]) + l + RESET)
        return out


# ------------------------------------------------------------------------------------------------- misc
class BashBlock(Block):
    kind = "bash"

    def __init__(self, command: str, exclude: bool = False):
        super().__init__()
        self.command, self.exclude = command, exclude
        self.output = ""
        self.exit_code: Optional[int] = None
        self.running = True

    def animated(self):
        return self.running

    def render(self, cols, ctx):
        p = ctx.pal
        lines = wrap_text(self.output.rstrip("\n"), cols - 4) if self.output else []
        if not ctx.expanded and len(lines) > COLLAPSED_LINES * 2:
            lines = [fg(p["dim"]) + f"… {len(lines) - COLLAPSED_LINES * 2} earlier lines" + RESET] + lines[-COLLAPSED_LINES * 2:]
        icon = fg(p["accent"]) + spinner_frame() + RESET if self.running else (
            fg(p["success"]) + "✓" + RESET if self.exit_code == 0 else fg(p["error"]) + f"✗ {self.exit_code}" + RESET)
        title = fg(p["bashMode"]) + BOLD + ("!!" if self.exclude else "!") + " " + self.command + RESET
        right = icon + (fg(p["dim"]) + "  not sent to model" + RESET if self.exclude else "")
        return [""] + panel([fg(p["toolOutput"]) + l + RESET for l in lines] or [fg(p["dim"]) + "(no output)" + RESET],
                            cols, p, p["bashMode"] if self.running else p["border"], title=title, right=right)


class CustomBlock(Block):
    kind = "custom"

    def __init__(self, message: Dict[str, Any]):
        super().__init__()
        self.message = message

    def render(self, cols, ctx):
        p = ctx.pal
        m = self.message
        if ctx.ext_render:
            lines = ctx.ext_render({"kind": "message", "customType": m.get("customType"), "message": m, "expanded": ctx.expanded, "width": cols})
            if lines is not None:
                return [""] + lines
        body = wrap_text(ctx.md(text_of(m.get("content")) or "", "custom", False, cols - 4, self.entry_id), cols - 4)
        if not ctx.expanded and len(body) > COLLAPSED_LINES:
            body = body[:COLLAPSED_LINES] + [fg(p["dim"]) + "… (ctrl+o to expand)" + RESET]
        title = fg(p["customMessageLabel"]) + BOLD + f"◈ {m.get('customType', 'extension')}" + RESET
        return [""] + panel([fg(p["customMessageText"]) + l + RESET for l in body], cols, p, p["customMessageLabel"], title=title,
                            bgc=p["customMessageBg"])


class NoticeBlock(Block):
    kind = "notice"

    def __init__(self, text: str, level: str = "info"):
        super().__init__()
        self.text, self.level = text, level

    def render(self, cols, ctx):
        p = ctx.pal
        icon, colour = {"error": ("✗", p["error"]), "warning": ("⚠", p["warning"]), "success": ("✓", p["success"])}.get(
            self.level, ("•", p["muted"]))
        lines = wrap_text(self.text, cols - 3)
        return [""] + [fg(colour) + (icon + " " if i == 0 else "  ") + RESET + fg(p["muted"] if self.level == "info" else colour) + l + RESET
                       for i, l in enumerate(lines)]


class SummaryBlock(Block):
    kind = "summary"

    def __init__(self, title: str, summary: str):
        super().__init__()
        self.title, self.summary = title, summary

    def render(self, cols, ctx):
        p = ctx.pal
        head = fg(p["accentAlt"]) + "◇ " + BOLD + self.title + RESET
        if not ctx.expanded:
            return ["", head + fg(p["dim"]) + "  (ctrl+o to view summary)" + RESET]
        return [""] + panel(render_markdown(ctx.md(self.summary, "summary", False, cols - 4, self.entry_id), cols - 4, p), cols, p,
                            p["accentAlt"], title=head)


class LinesBlock(Block):
    """Pre-rendered lines (extension custom output, command output)."""
    kind = "lines"

    def __init__(self, lines: List[str], title: str = ""):
        super().__init__()
        self.raw, self.title = lines, title

    def render(self, cols, ctx):
        p = ctx.pal
        if self.title:
            return [""] + panel([truncate(l, cols - 4) for l in self.raw], cols, p, p["border"],
                                title=fg(p["accent"]) + BOLD + self.title + RESET)
        return [""] + [truncate(l, cols) for l in self.raw]
