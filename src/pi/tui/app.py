"""Interactive UI. Full-screen by default; `"tuiMode": "regular"` (settings) or OMNI_TUI_MODE=regular keeps the
terminal's own scrollback: finished transcript blocks are printed once and only the live area is redrawn.

  ┌ header ─ brand · session ───────────────────────────── model · thinking ┐
  │ transcript (scrolls)                                                     │
  │ widgets from extensions                                                  │
  │ status: spinner · working message · queue · toasts                      │
  ╭ editor (border colour = thinking level) ────────────────────────────────╮
  ╰──────────────────────────────────────────────────────────────────────────╯
  footer: cwd · branch · session                      extension statuses
          tokens · cost · context bar                 provider/model
"""
from __future__ import annotations

import os
import queue
import subprocess
import tempfile
import threading
import time
import traceback
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from ..agent import AgentSession, SessionOptions
from ..ai import model_key
from ..commands import Commands, copy_to_clipboard
from ..session import bash_execution_to_text
from ..tools.search import walk_files
from .blocks import (AssistantBlock, BashBlock, Block, CustomBlock, LinesBlock, NoticeBlock, RenderCtx, SummaryBlock, ToolBlock,
                     UserBlock, WelcomeBlock, panel, spinner_frame, text_of)
from .editor import Editor
from .term import Key, Terminal
from .text import BOLD, DIM, ITALIC, RESET, bg, fg, gradient, pad, strip, truncate, width, wrap
from .theme import BRAND_GRADIENT, palette_for, thinking_color
from . import inline_images

PI_CURSOR = "\x1b_pi:c\x07"


def human(n: float) -> str:
    n = float(n or 0)
    for unit, div in (("M", 1e6), ("k", 1e3)):
        if n >= div:
            return f"{n / div:.1f}{unit}"
    return str(int(n))


def git_branch(cwd: str) -> Optional[str]:
    cur = Path(cwd)
    while True:
        head = cur / ".git"
        if head.is_file():  # worktree: "gitdir: ..."
            try:
                gd = head.read_text().split(":", 1)[1].strip()
                head = (cur / gd) if not os.path.isabs(gd) else Path(gd)
            except Exception:
                return None
        if (head / "HEAD").is_file():
            try:
                ref = (head / "HEAD").read_text().strip()
                return ref.rsplit("/", 1)[-1] if ref.startswith("ref:") else ref[:8]
            except Exception:
                return None
        if cur.parent == cur:
            return None
        cur = cur.parent


class Dialog:
    def __init__(self, kind: str, title: str, done: Callable[[Any], None], **kw: Any):
        self.kind, self.title, self.done = kind, title, done
        self.options: List[str] = kw.get("options") or []
        self.message: str = kw.get("message") or ""
        self.lines: List[str] = kw.get("lines") or []
        self.selected = int(kw.get("initial") or 0)
        self.filter = ""
        self.editor = Editor()
        self.secret = bool(kw.get("secret"))
        self.multiline = bool(kw.get("multiline"))
        self.placeholder = kw.get("placeholder") or ""
        self.custom_id: Optional[str] = kw.get("custom_id")
        self.overlay = bool(kw.get("overlay"))
        self.scroll = 0
        if kw.get("prefill"):
            self.editor.set_text(kw["prefill"])

    def visible_options(self) -> List[Tuple[int, str]]:
        q = self.filter.lower()
        return [(i, o) for i, o in enumerate(self.options) if q in o.lower()] if q else list(enumerate(self.options))


class InteractiveApp:
    has_ui = True

    def __init__(self, opts: SessionOptions, initial_messages: Optional[List[str]] = None):
        self.inbox: "queue.Queue[Any]" = queue.Queue()
        self.term = Terminal()
        self.term.keys = self.inbox  # keys and app events share one queue
        self.width, self.height = self.term.size
        self.opts = opts
        self.initial_messages = initial_messages or []
        self.session = AgentSession(opts, ui=self)
        self.commands = Commands(self.session, self)
        self.theme_name = os.environ.get("OMNI_PI_THEME") or self.session.settings.get("theme", "omni")
        self.pal = palette_for(self.theme_name)
        self.blocks: List[Block] = []
        self.tool_blocks: Dict[str, ToolBlock] = {}
        self.current: Optional[AssistantBlock] = None
        self.bash_block: Optional[BashBlock] = None
        self.editor = Editor()
        self.scroll = 0
        self.tools_expanded = False
        self.show_thinking = not self.session.settings.get("hideThinkingBlock", False)
        self.dialogs: List[Dialog] = []
        self.widgets: Dict[str, Tuple[List[str], Dict[str, Any]]] = {}
        self.statuses: Dict[str, str] = {}
        self.working = False
        self.working_message: Optional[str] = None
        self.working_visible = True
        self.toast: Optional[Tuple[str, str, float]] = None
        self.title: Optional[str] = None
        self.queue_counts = (0, 0)
        self.retry_note: Optional[str] = None
        self.prev_frame: List[str] = []
        self.dirty = True
        self.quit_flag = False
        self.last_ctrl_c = 0.0
        self.last_esc = 0.0
        self.ac_items: List[Dict[str, str]] = []
        self.ac_index = 0
        self.ac_start = 0
        self.file_cache: Optional[List[str]] = None
        self.branch = git_branch(self.session.cwd)
        self.customs: Dict[str, Dialog] = {}
        st = self.session.settings
        self.regular = (os.environ.get("OMNI_TUI_MODE") or st.get("tuiMode", "fullscreen")) == "regular"
        self.image_store = inline_images.ImageStore()
        self.image_protocol = inline_images.detect_protocol(lambda k, d=None: st.get(k, d))
        self.ext_editor_lines: Optional[List[str]] = None   # ctx.ui.setEditorComponent
        self.committed = 0                                  # regular mode: transcript lines already in scrollback
        self.live_height = 0
        self.ac_seq = 0

    # ===================================================================================== lifecycle
    def confirm_trust(self, cwd: str) -> Optional[bool]:
        try:
            ans = input(f"\n  This folder has project resources (.pi/). Trust {cwd}?\n  Trusted projects load settings, "
                        f"extensions, skills and prompts from .pi/  [y/N/a=always] ").strip().lower()
        except EOFError:
            return False
        if ans in ("a", "always"):
            from ..resources import TrustStore, agent_dir
            TrustStore(Path(self.opts.agent_dir) if self.opts.agent_dir else agent_dir()).save(cwd, True)
            return True
        return ans in ("y", "yes")

    def run(self) -> int:
        s = self.session
        try:
            s.start()
        except SystemExit as e:
            print(e)
            return 1
        s.subscribe(lambda ev: self.inbox.put(("agent", ev)))
        self.reset_transcript()
        self.term.enter(alt_screen=not self.regular, mouse=not self.regular)
        self.term.set_title(self.title or f"omni · {s.profile.get('name', 'pi')} — {os.path.basename(s.cwd)}")
        for m in self.initial_messages:
            self.submit(m)
        try:
            self.loop()
        except KeyboardInterrupt:
            pass
        except Exception:
            err = traceback.format_exc()
        else:
            err = None
        finally:
            self.term.leave()
            s.shutdown("quit")
        if err:
            print(err)
        if s.sessions.file and s.sessions.file.exists():
            print(f"\n  {fg(self.pal['muted'])}session saved · resume with {RESET}{fg(self.pal['accent'])}omni pi -c{RESET}"
                  f"{fg(self.pal['muted'])} or {RESET}{fg(self.pal['accent'])}omni pi --session {s.sessions.session_id[:8]}{RESET}\n")
        return 0

    def loop(self) -> None:
        last_render = 0.0
        while not self.quit_flag:
            try:
                item = self.inbox.get(timeout=0.05)
                self.handle(item)
                for _ in range(500):  # drain bursts (streaming deltas, paste)
                    try:
                        self.handle(self.inbox.get_nowait())
                    except queue.Empty:
                        break
            except queue.Empty:
                pass
            size = self.term.get_size()
            if size != (self.width, self.height):
                self.width, self.height = size
                self.prev_frame = []
                self.dirty = True
                self._sync_ext_size()
            now = time.time()
            animating = self.working or any(b.animated() for b in self.blocks[-6:])
            if self.toast and now > self.toast[2]:
                self.toast = None
                self.dirty = True
            if (self.dirty or (animating and now - last_render > 0.1)) and now - last_render > 0.03:
                self.render()
                last_render = now
                self.dirty = False

    def _sync_ext_size(self) -> None:
        ext = self.session.ext
        if hasattr(ext, "sync_state"):
            threading.Thread(target=ext.sync_state, daemon=True).start()

    def handle(self, item: Any) -> None:
        self.dirty = True
        if isinstance(item, Key):
            self.on_key(item)
        elif isinstance(item, tuple):
            kind = item[0]
            if kind == "agent":
                self.on_agent_event(item[1])
            elif kind == "call":
                fn = item[1]
                try:
                    fn()
                except Exception as e:
                    self.notify(f"{type(e).__name__}: {e}", "error")

    def post(self, fn: Callable[[], None]) -> None:
        self.inbox.put(("call", fn))

    def call_blocking(self, make_dialog: Callable[[Callable[[Any], None]], Dialog]) -> Any:
        """From a worker thread: open a dialog on the UI thread and wait for its result."""
        done = threading.Event()
        box: Dict[str, Any] = {}

        def finish(v: Any) -> None:
            box["v"] = v
            done.set()

        self.post(lambda: self.dialogs.append(make_dialog(finish)))
        done.wait()
        return box.get("v")

    def in_worker(self, fn: Callable[[], None], name: str = "pi-worker") -> None:
        def run() -> None:
            try:
                fn()
            except Exception as e:
                self.post(lambda: self.notify(f"{type(e).__name__}: {e}", "error"))
        threading.Thread(target=run, name=name, daemon=True).start()

    # ======================================================================================== port
    def notify(self, text: str, level: str = "info") -> None:
        if threading.current_thread() is not threading.main_thread():
            self.post(lambda: self.notify(text, level))
            return
        if level in ("error", "warning") or len(text) > 90:
            self.blocks.append(NoticeBlock(text, level))
            self.scroll = 0
        self.toast = (text, level, time.time() + 4)
        self.dirty = True

    def select(self, title: str, options: List[str], initial: Optional[int] = None) -> Optional[int]:
        return self.call_blocking(lambda done: Dialog("select", title, done, options=options, initial=initial or 0))

    def confirm(self, title: str, message: str) -> bool:
        return bool(self.call_blocking(lambda done: Dialog("confirm", title, done, message=message, initial=0)))

    def input(self, title: str, placeholder: str = "", prefill: str = "", secret: bool = False) -> Optional[str]:
        return self.call_blocking(lambda done: Dialog("input", title, done, placeholder=placeholder, prefill=prefill, secret=secret))

    def input_cancellable(self, title: str, placeholder: str = "", cancel: Optional[threading.Event] = None) -> Optional[str]:
        """Like `input`, but the dialog closes itself (returning None) once `cancel` is set - used by OAuth login
        to drop the paste-the-code prompt when the browser callback arrives first."""
        done = threading.Event()
        box: Dict[str, Any] = {}
        holder: Dict[str, Dialog] = {}

        def finish(v: Any) -> None:
            box.setdefault("v", v)
            done.set()

        def open_dialog() -> None:
            d = Dialog("input", title, finish, placeholder=placeholder)
            holder["d"] = d
            self.dialogs.append(d)
            self.dirty = True

        def close_dialog() -> None:
            d = holder.get("d")
            if d is not None and d in self.dialogs:
                self.dialogs.remove(d)
                self.dirty = True
            finish(None)

        self.post(open_dialog)
        while not done.wait(0.2):
            if cancel is not None and cancel.is_set():
                self.post(close_dialog)
                done.wait(2)
                break
        return box.get("v")

    def show(self, title: str, lines: List[str]) -> None:
        self.post(lambda: self.dialogs.append(Dialog("show", title, lambda v: None, lines=lines)))

    def set_editor_text(self, text: str) -> None:
        self.post(lambda: self.editor.set_text(text))

    def copy(self, text: str) -> bool:
        if copy_to_clipboard(text):
            return True
        import base64
        self.term.write(f"\x1b]52;c;{base64.b64encode(text.encode()).decode()}\x07")  # OSC 52 fallback
        return True

    def quit(self) -> None:
        self.post(self._quit)

    def request_quit(self) -> None:
        self.quit()

    def _quit(self) -> None:
        self.quit_flag = True

    def reset_transcript(self) -> None:
        if threading.current_thread() is not threading.main_thread():
            self.post(self.reset_transcript)
            return
        self.rebuild_transcript()

    def apply_theme(self, name: str) -> None:
        def go() -> None:
            self.theme_name = name
            self.pal = palette_for(name)
            self.prev_frame = []
        self.post(go)

    def screen_dump(self) -> List[str]:
        return [strip(l) for l in self.prev_frame]

    # ------------------------------------------------------------------------------------ extension UI
    def ext_select(self, title: str = "", options: Optional[List[str]] = None, opts: Any = None, **_: Any) -> Optional[str]:
        opts_list = [str(o) for o in options or []]
        idx = self.select(title, opts_list)
        return opts_list[idx] if idx is not None else None

    def ext_confirm(self, title: str = "", message: str = "", opts: Any = None, **_: Any) -> bool:
        return self.confirm(title, message)

    def ext_input(self, title: str = "", placeholder: str = "", opts: Any = None, **_: Any) -> Optional[str]:
        return self.input(title, placeholder or "")

    def ext_editor(self, title: str = "", prefill: str = "", **_: Any) -> Optional[str]:
        return self.call_blocking(lambda done: Dialog("input", title, done, prefill=prefill or "", multiline=True))

    def ext_notify(self, message: str = "", type: str = "info", **_: Any) -> None:
        self.notify(message, {"warning": "warning", "error": "error"}.get(type, "info"))

    def ext_setStatus(self, key: str = "", text: Optional[str] = None, **_: Any) -> None:
        def go() -> None:
            if text:
                self.statuses[key] = text
            else:
                self.statuses.pop(key, None)
        self.post(go)

    def ext_setWorkingMessage(self, message: Optional[str] = None, **_: Any) -> None:
        self.post(lambda: setattr(self, "working_message", message))

    def ext_setWorkingVisible(self, visible: bool = True, **_: Any) -> None:
        self.post(lambda: setattr(self, "working_visible", bool(visible)))

    def ext_setWorkingIndicator(self, options: Any = None, **_: Any) -> None:
        pass

    def ext_setHiddenThinkingLabel(self, label: Optional[str] = None, **_: Any) -> None:
        pass

    def ext_setWidget(self, key: str = "", lines: Optional[List[str]] = None, options: Any = None, **_: Any) -> None:
        def go() -> None:
            if lines is None:
                self.widgets.pop(key, None)
            else:
                self.widgets[key] = (list(lines), options or {})
        self.post(go)

    def ext_setTitle(self, title: str = "", **_: Any) -> None:
        self.post(lambda: (setattr(self, "title", title), self.term.set_title(title)))

    def ext_pasteToEditor(self, text: str = "", **_: Any) -> None:
        self.post(lambda: self.editor.insert(text))

    def ext_setEditorText(self, text: str = "", **_: Any) -> None:
        self.post(lambda: (self.editor.set_text(text), self._sync_ext_editor()))

    def ext_setToolsExpanded(self, expanded: bool = False, **_: Any) -> None:
        self.post(lambda: setattr(self, "tools_expanded", bool(expanded)))

    def ext_setTheme(self, theme: str = "", **_: Any) -> None:
        if theme:
            self.apply_theme(theme)

    def ext_custom_open(self, id: str = "", overlay: bool = False, lines: Optional[List[str]] = None, **_: Any) -> None:
        def go() -> None:
            d = Dialog("custom", "", lambda v: None, custom_id=id, overlay=overlay, lines=lines or [])
            self.customs[id] = d
            self.dialogs.append(d)
        self.post(go)

    def ext_custom_render(self, id: str = "", lines: Optional[List[str]] = None, **_: Any) -> None:
        def go() -> None:
            d = self.customs.get(id)
            if d:
                d.lines = lines or []
        self.post(go)

    def ext_custom_close(self, id: str = "", **_: Any) -> None:
        def go() -> None:
            d = self.customs.pop(id, None)
            if d in self.dialogs:
                self.dialogs.remove(d)
        self.post(go)

    def ext_copy(self, text: str = "", **_: Any) -> bool:
        return self.copy(text)

    # custom editor (ctx.ui.setEditorComponent) ---------------------------------------------------------------
    def ext_editor_render(self, active: bool = False, lines: Optional[List[str]] = None, text: str = "", **_: Any) -> None:
        def go() -> None:
            self.ext_editor_lines = [l.replace(PI_CURSOR, "") for l in lines or []] if active else None
            if active and text is not None:
                self.editor.set_text(text)
        self.post(go)

    def ext_editor_action(self, key: str = "", data: str = "", text: str = "", **_: Any) -> None:
        def go() -> None:
            self.editor.set_text(text or "")
            self._app_key(Key(key, raw=data))
        self.post(go)

    def ext_editor_submit(self, text: str = "", **_: Any) -> None:
        def go() -> None:
            if not text.strip():
                return
            self.editor.remember(text)
            self.editor.clear()
            self._sync_ext_editor()
            self.submit(text)
        self.post(go)

    def editor_text(self) -> str:
        return self.editor.text

    # autocomplete providers ------------------------------------------------------------------------------------
    def ext_autocomplete_base(self, lines: Optional[List[str]] = None, line: int = 0, col: int = 0, options: Any = None, **_: Any):
        """The built-in provider (slash commands, @files, paths) for providers stacked by extensions."""
        items, prefix = self._builtin_completions(list(lines or [""]), line, col, bool((options or {}).get("force")))
        return {"prefix": prefix, "items": [{"value": i["insert"].rstrip(), "label": i["label"], "description": i["desc"]} for i in items]} if items else None

    def ext_terminalInput(self, **_: Any) -> None:
        pass

    def ext_autocomplete(self, **_: Any) -> None:
        pass

    def ext_markdownTransformers(self, **_: Any) -> None:
        def go() -> None:
            for b in self.blocks:
                b.touch()
        self.post(go)

    def ext_render(self, payload: Dict[str, Any]) -> Optional[List[str]]:
        ext = self.session.ext
        if not hasattr(ext, "render"):
            return None
        return ext.render(payload, timeout=0.3)

    # ================================================================================ transcript model
    def welcome_info(self) -> Dict[str, Any]:
        s = self.session
        ext = s.ext
        exts = [e.get("name") + ("" if e.get("ok") else " ✗") for e in getattr(ext, "extensions", [])]
        home = str(Path.home())
        cwd = s.cwd.replace(home, "~", 1) if s.cwd.startswith(home) else s.cwd
        return {
            "model": f"{model_key(s.model)} · thinking {s.thinking_level}" if s.model else "(none) — use /login or /model",
            "cwd": cwd + (f"  git:{self.branch}" if self.branch else ""),
            "subtitle": s.profile.get("description", ""), "profile": s.profile.get("name", "pi"),
            "context": [os.path.relpath(f["path"], s.cwd) if f["path"].startswith(s.cwd) else f["path"].replace(home, "~")
                        for f in s.context_files],
            "skills": [k.name for k in s.skills], "prompts": ["/" + t.name for t in s.templates], "extensions": exts,
            "tools": [n for n in s.active_tool_names if n not in s.omni_tools],
            "omni tools": list(s.omni_tools), "diagnostics": s.diagnostics[-6:],
        }

    def rebuild_transcript(self) -> None:
        s = self.session
        self.blocks, self.tool_blocks, self.current = [], {}, None
        if not s.settings.get("quietStartup", False):
            self.blocks.append(WelcomeBlock(self.welcome_info()))
        renderers = {n: t.has_renderer for n, t in s.all_tools().items()}
        for e in s.sessions.get_branch():
            t = e["type"]
            if t == "message":
                m = e["message"]
                self._block_for_message(m, renderers, final=True)
            elif t == "custom_message" and e.get("display", True):
                self.blocks.append(CustomBlock({"customType": e.get("customType"), "content": e.get("content"), "details": e.get("details")}))
            elif t == "compaction":
                self.blocks.append(SummaryBlock(f"context compacted · {e.get('tokensBefore', 0):,} tokens summarized", e.get("summary", "")))
            elif t == "branch_summary":
                self.blocks.append(SummaryBlock("returned from another branch", e.get("summary", "")))
        self.scroll = 0
        self.dirty = True

    def _block_for_message(self, m: Dict[str, Any], renderers: Dict[str, bool], final: bool) -> None:
        role = m.get("role")
        if role == "user":
            c = m.get("content")
            imgs = [] if isinstance(c, str) else [b for b in c or [] if b.get("type") == "image"]
            ub = UserBlock(text_of(c), imgs)
            ub.entry_id = m.get("__entry_id")
            self.blocks.append(ub)
        elif role == "assistant":
            ab = AssistantBlock(m)
            self.blocks.append(ab)
            for b in m.get("content") or []:
                if b.get("type") == "toolCall":
                    tb = ToolBlock(b["id"], b["name"], b.get("arguments") or {}, renderers.get(b["name"], False))
                    tb.status = "pending" if not final else "done"
                    self.tool_blocks[b["id"]] = tb
                    self.blocks.append(tb)
        elif role == "toolResult":
            tb = self.tool_blocks.get(m.get("toolCallId"))
            if tb:
                tb.result = {"content": m.get("content"), "details": m.get("details"), "isError": m.get("isError")}
                tb.status = "error" if m.get("isError") else "done"
                tb.ended = tb.started
                tb.touch()
        elif role == "custom" and m.get("display", True):
            self.blocks.append(CustomBlock(m))
        elif role == "bashExecution":
            bb = BashBlock(m.get("command", ""), bool(m.get("excludeFromContext")))
            bb.output, bb.exit_code, bb.running = m.get("output", ""), m.get("exitCode"), False
            self.blocks.append(bb)

    def on_agent_event(self, ev: Dict[str, Any]) -> None:
        t = ev.get("type")
        s = self.session
        if t == "agent_start":
            self.working = True
        elif t == "agent_settled":
            self.working = False
            self.working_message = None
            self.retry_note = None
            if self.current:
                self.current.streaming = False
                self.current.touch()
                self.current = None
        elif t == "message_start":
            m = ev["message"]
            role = m.get("role")
            if role == "assistant":
                self.current = AssistantBlock(dict(m))
                self.current.streaming = True
                self.blocks.append(self.current)
                self.scroll = 0
            elif role == "bashExecution":
                if self.bash_block:
                    self.bash_block.output = m.get("output", self.bash_block.output)
                    self.bash_block.exit_code = m.get("exitCode")
                    self.bash_block.running = False
                    self.bash_block.touch()
                    self.bash_block = None
                else:
                    self._block_for_message(m, {}, final=True)
            elif role in ("user", "custom"):
                self._block_for_message(m, {}, final=True)
                self.scroll = 0
        elif t in ("message_update", "message_end"):
            m = ev["message"]
            if m.get("role") == "assistant" and self.current is not None:
                self.current.message = dict(m, content=[dict(b) for b in m.get("content") or []])
                if t == "message_end":
                    self.current.streaming = False
                self.current.touch()
                renderers = {n: tl.has_renderer for n, tl in s.all_tools().items()}
                for b in m.get("content") or []:
                    if b.get("type") == "toolCall" and b["id"] not in self.tool_blocks:
                        tb = ToolBlock(b["id"], b["name"], b.get("arguments") or {}, renderers.get(b["name"], False))
                        self.tool_blocks[b["id"]] = tb
                        self.blocks.append(tb)
                if t == "message_end":
                    self.current = None
        elif t == "tool_execution_start":
            tb = self.tool_blocks.get(ev["toolCallId"])
            if tb is None:
                tb = ToolBlock(ev["toolCallId"], ev["toolName"], ev.get("args") or {}, s.all_tools().get(ev["toolName"], None) is not None
                               and s.all_tools()[ev["toolName"]].has_renderer)
                self.tool_blocks[ev["toolCallId"]] = tb
                self.blocks.append(tb)
            tb.args = ev.get("args") or tb.args
            tb.started = time.time()
            tb.touch()
        elif t == "tool_execution_update":
            tb = self.tool_blocks.get(ev["toolCallId"])
            if tb:
                tb.partial = ev.get("partialResult")
                tb.touch()
        elif t == "tool_execution_end":
            tb = self.tool_blocks.get(ev["toolCallId"])
            if tb:
                tb.result = ev.get("result")
                tb.status = "error" if ev.get("isError") else "done"
                tb.ended = time.time()
                tb.touch()
        elif t == "bash_start":
            self.bash_block = BashBlock(ev["command"])
            self.blocks.append(self.bash_block)
            self.scroll = 0
        elif t == "bash_output" and self.bash_block:
            self.bash_block.output += ev.get("chunk", "")
            self.bash_block.touch()
        elif t == "queue_update":
            self.queue_counts = (ev.get("steering", 0), ev.get("followUp", 0))
        elif t == "auto_retry_start":
            self.retry_note = f"retrying in {ev['delayMs'] / 1000:.0f}s ({ev['attempt']}/{ev['maxAttempts']}): {ev.get('errorMessage', '')[:80]}"
        elif t == "auto_retry_end":
            self.retry_note = None
        elif t in ("compaction_start", "auto_compaction_start"):
            self.working_message = "compacting context…"
        elif t == "compaction_end":
            self.working_message = None
            e = ev.get("entry") or {}
            self.blocks.append(SummaryBlock(f"context compacted · {e.get('tokensBefore', 0):,} tokens summarized", e.get("summary", "")))
        elif t == "notice":
            self.notify(ev.get("message", ""), ev.get("level", "info"))
        elif t == "error":
            self.blocks.append(NoticeBlock(ev.get("message", "error"), "error"))
        elif t == "extension_error":
            err = str(ev.get("error", "")).split("\n")[0][:200]
            self.blocks.append(NoticeBlock(f"extension {os.path.basename(str(ev.get('path')))} [{ev.get('event')}]: {err}", "warning"))
        elif t == "session_replaced":
            self.rebuild_transcript()
        elif t in ("tools_changed", "model_changed", "model_select", "thinking_level_select"):
            pass

    # ========================================================================================= input
    def on_key(self, k: Key) -> None:
        if self.dialogs:
            self.dialog_key(self.dialogs[-1], k)
            return
        ext = self.session.ext
        feats = getattr(ext, "features", {}) or {}
        raw = k.raw or k.text or (k.name if len(k.name) == 1 else "")
        if feats.get("terminalInput") and k.name != "mouse" and raw:  # ctx.ui.onTerminalInput listeners see raw input first
            data = ext.terminal_input(raw)
            if data is None:
                return
            if data != raw:
                from .term import parse_keys
                for k2 in parse_keys(data):
                    self._route_key(k2)
                return
        self._route_key(k)

    def _route_key(self, k: Key) -> None:
        ext = self.session.ext
        nav = self.ac_items and k.name in ("up", "down", "tab", "enter", "escape", "shift+tab")
        if self.ext_editor_lines is not None and not nav and k.name not in ("wheelup", "wheeldown", "mouse", "pageup", "pagedown") \
                and hasattr(ext, "editor_input"):
            raw = k.raw or k.text or (k.name if len(k.name) == 1 else "")
            if raw:
                r = ext.editor_input(raw)
                if r and r.get("active"):
                    self._apply_ext_editor(r)
                    self.update_completions()
                    return
                self.ext_editor_lines = None
        self._app_key(k)
        if self.ext_editor_lines is not None and nav:
            self._sync_ext_editor()

    def _apply_ext_editor(self, r: Optional[Dict[str, Any]]) -> None:
        if r and r.get("active"):
            self.ext_editor_lines = [l.replace(PI_CURSOR, "") for l in r.get("lines") or []]
            self.editor.set_text(r.get("text") or "")

    def _sync_ext_editor(self) -> None:
        """Push the app's editor text (completion accepted, history, /tree prefill) into the extension's editor."""
        ext = self.session.ext
        if self.ext_editor_lines is None or not hasattr(ext, "editor_set_text"):
            return
        text = self.editor.text
        threading.Thread(target=lambda: self.post(lambda: self._apply_ext_editor(ext.editor_set_text(text))), daemon=True).start()

    def _app_key(self, k: Key) -> None:
        n = k.name
        ed = self.editor
        s = self.session
        if n in ("wheelup", "wheeldown"):
            self.scroll = max(0, self.scroll + (3 if n == "wheelup" else -3))
            return
        if n in ("pageup", "pagedown"):
            page = max(3, self.height - 12)
            self.scroll = max(0, self.scroll + (page if n == "pageup" else -page))
            return
        if self.ac_items and n in ("up", "down", "tab", "enter", "escape", "shift+tab"):
            if n == "up":
                self.ac_index = (self.ac_index - 1) % len(self.ac_items)
            elif n in ("down", "shift+tab"):
                self.ac_index = (self.ac_index + 1) % len(self.ac_items)
            elif n == "escape":
                self.ac_items = []
            else:
                self.accept_completion(submit=(n == "enter"))
            return
        ext_sc = getattr(s.ext, "shortcuts", {})
        if n in ext_sc:
            self.in_worker(lambda: s.ext.run_shortcut(n))
            return
        if n == "enter":
            text = ed.text
            if text.endswith("\\"):
                ed.backspace()
                ed.newline()
                return
            if text.strip():
                ed.remember(text)
                ed.clear()
                self.ac_items = []
                self.submit(text)
            return
        if n in ("shift+enter", "ctrl+j", "ctrl+enter"):
            ed.newline()
        elif n == "alt+enter":
            if self.working and ed.text.strip():
                text = ed.text
                ed.remember(text)
                ed.clear()
                s.follow_up(text)
                self.notify("queued as follow-up", "info")
            else:
                ed.newline()
        elif n == "paste":
            ed.insert(k.text)
        elif n == "backspace":
            ed.backspace()
        elif n == "delete":
            ed.delete()
        elif n in ("left",):
            ed.left()
        elif n in ("right",):
            ed.right()
        elif n in ("alt+left", "ctrl+left", "alt+b"):
            ed.word_left()
        elif n in ("alt+right", "ctrl+right", "alt+f"):
            ed.word_right()
        elif n in ("home", "ctrl+a"):
            ed.home()
        elif n in ("end", "ctrl+e"):
            ed.end()
        elif n in ("ctrl+w", "alt+backspace", "ctrl+backspace"):
            ed.delete_word_back()
        elif n == "ctrl+u":
            ed.kill_to_start()
        elif n == "ctrl+k":
            ed.kill_to_end()
        elif n == "up":
            if not ed.up():
                ed.history_prev()
        elif n == "down":
            if not ed.down():
                ed.history_next()
        elif n == "tab":
            self.update_completions(force=True)
            if len(self.ac_items) == 1:
                self.accept_completion(submit=False)
            return
        elif n == "escape":
            now = time.time()
            if self.working:
                s.abort()
                self.notify("interrupted", "warning")
            elif ed.is_empty() and now - self.last_esc < 0.6:
                action = s.settings.get("doubleEscapeAction", "tree")
                if action in ("tree", "fork"):
                    self.in_worker(lambda: self.commands.run(action, ""))
            self.last_esc = now
            return
        elif n == "ctrl+c":
            now = time.time()
            if not ed.is_empty():
                ed.clear()
            elif self.working:
                s.abort()
            elif now - self.last_ctrl_c < 1.2:
                self.quit_flag = True
            else:
                self.toast = ("press ctrl+c again to quit", "info", now + 1.2)
            self.last_ctrl_c = now
            return
        elif n == "ctrl+d":
            if ed.is_empty():
                self.quit_flag = True
            else:
                ed.delete()
        elif n == "ctrl+l":
            self.in_worker(lambda: self.commands.run("model", ""))
        elif n == "ctrl+p":
            self.in_worker(lambda: self._cycle_model(1))
        elif n in ("ctrl+shift+p", "alt+p"):
            self.in_worker(lambda: self._cycle_model(-1))
        elif n == "shift+tab":
            lvl = s.cycle_thinking()
            self.toast = (f"thinking: {lvl}", "info", time.time() + 1.5)
        elif n == "ctrl+o":
            self.tools_expanded = not self.tools_expanded
        elif n == "ctrl+t":
            self.show_thinking = not self.show_thinking
        elif n == "ctrl+x":
            self.in_worker(lambda: self.commands.run("copy", ""))
        elif n == "alt+up":
            items = s.dequeue_all()
            if items:
                ed.set_text("\n\n".join(items + ([ed.text] if ed.text else [])))
        elif n == "ctrl+g":
            self.external_editor()
        elif len(n) == 1 or (k.text and not n.startswith(("ctrl+", "alt+"))):
            ed.insert(k.text or n)
        else:
            return
        self.update_completions()

    def _cycle_model(self, d: int) -> None:
        m = self.session.cycle_model(d)
        self.notify(f"model: {model_key(m)}" if m else "no other authenticated models", "info")

    def external_editor(self) -> None:
        editor = os.environ.get("VISUAL") or os.environ.get("EDITOR") or ("notepad" if os.name == "nt" else "vi")
        with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False, encoding="utf-8") as f:
            f.write(self.editor.text)
            path = f.name
        self.term.leave()
        try:
            subprocess.call(f'{editor} "{path}"', shell=True)
            self.editor.set_text(Path(path).read_text(encoding="utf-8").rstrip("\n"))
        finally:
            os.unlink(path)
            self.term.enter(alt_screen=not self.regular, mouse=not self.regular)
            self.prev_frame = []
            self.live_height = 0

    # --------------------------------------------------------------------------------- autocomplete
    def update_completions(self, force: bool = False) -> None:
        ed = self.editor
        lines = ed.text.split("\n")
        ext = self.session.ext
        if (getattr(ext, "features", {}) or {}).get("autocomplete") and hasattr(ext, "autocomplete"):
            # stacked extension providers (with the built-in one at the bottom) answer asynchronously
            self.ac_seq += 1
            seq, row, col = self.ac_seq, ed.row, ed.col
            snapshot = list(lines)

            def ask() -> None:
                r = ext.autocomplete(snapshot, row, col, {"force": force})

                def apply() -> None:
                    if seq != self.ac_seq:
                        return
                    items = (r or {}).get("items") or []
                    prefix = (r or {}).get("prefix") or ""
                    self.ac_items = [{"label": str(i.get("label") or i.get("value")), "insert": str(i.get("value") or i.get("label")),
                                      "desc": str(i.get("description") or ""), "tag": "provider", "raw": i, "prefix": prefix}
                                     for i in items[:50]]
                    self.ac_index = 0
                    if force and len(self.ac_items) == 1:
                        self.accept_completion(submit=False)
                self.post(apply)
            threading.Thread(target=ask, daemon=True).start()
            return
        self.ac_items, self.ac_start = [], 0
        items, _prefix = self._builtin_completions(lines, ed.row, ed.col, force)
        self.ac_items = items
        self.ac_index = 0

    def _builtin_completions(self, lines: List[str], row: int, col: int, force: bool) -> Tuple[List[Dict[str, Any]], str]:
        text = "\n".join(lines)
        if row == 0 and text.startswith("/") and " " not in text and "\n" not in text:
            # a command several sources provide is offered once per source (inserted as /<prefix>:<name>)
            items = self.commands.sources().completions(text[1:])
            self.ac_start = 0
            return items[:50], text
        line = lines[row] if row < len(lines) else ""
        before = line[:col]
        start = max(before.rfind(" "), before.rfind("\t")) + 1
        tok = before[start:]
        if tok.startswith("@") or (force and ("/" in tok or "\\" in tok or tok.startswith("."))):
            q = tok[1:] if tok.startswith("@") else tok
            files = self._files()
            ql = q.lower()
            matches = [f for f in files if ql in f.lower()] if ql else files
            matches.sort(key=lambda f: (not os.path.basename(f).lower().startswith(os.path.basename(ql)), len(f)))
            prefix = "@" if tok.startswith("@") else ""
            if row == self.editor.row:
                _t, self.ac_start = self.editor.token_before_cursor()
            return [{"label": prefix + f, "insert": prefix + f + " ", "desc": "", "tag": "file"} for f in matches[:50]], tok
        return [], ""

    def _files(self) -> List[str]:
        if self.file_cache is None:
            try:
                out = []
                for f in walk_files(self.session.cwd):
                    out.append(f)
                    if len(out) > 20000:
                        break
                self.file_cache = out
            except Exception:
                self.file_cache = []
        return self.file_cache

    def accept_completion(self, submit: bool) -> None:
        if not self.ac_items:
            return
        it = self.ac_items[self.ac_index]
        ed = self.editor
        if it["tag"] == "provider":
            ext = self.session.ext
            lines = ed.text.split("\n")
            r = ext.autocomplete_apply(lines, ed.row, ed.col, it.get("raw") or {}, it.get("prefix") or "") if hasattr(ext, "autocomplete_apply") else None
            self.ac_items = []
            if r and isinstance(r.get("lines"), list):
                ed.set_text("\n".join(r["lines"]))
                ed.set_cursor(int(r.get("cursorLine", 0)), int(r.get("cursorCol", 0)))
                if submit and ed.text.startswith("/") and self.commands.is_builtin(ed.text[1:].split(" ")[0].strip()):
                    text = ed.text.strip()
                    ed.remember(text)
                    ed.clear()
                    self.submit(text)
            return
        if it.get("kind") in ("builtin", "omni", "extension", "prompt", "skill"):
            ed.set_text(it["insert"])
            self.ac_items = []
            if submit and it["kind"] in ("builtin", "omni"):
                text = ed.text.strip()
                ed.remember(text)
                ed.clear()
                self.submit(text)
            return
        ed.replace_token(self.ac_start, it["insert"])
        self.ac_items = []

    # ---------------------------------------------------------------------------------------- submit
    def expand_mentions(self, text: str) -> Tuple[str, List[Dict[str, Any]]]:
        """`@path` mentions: text files are inlined as <file> blocks (like Pi's @file), images attached."""
        import base64
        import re
        images: List[Dict[str, Any]] = []
        files = []
        for m in re.finditer(r"(?<!\S)@([^\s]+)", text):
            p = Path(self.session.cwd, m.group(1))
            if p.is_file():
                ext = p.suffix.lower()
                if ext in (".png", ".jpg", ".jpeg", ".gif", ".webp"):
                    mime = {"jpg": "image/jpeg"}.get(ext[1:], f"image/{ext[1:]}")
                    images.append({"type": "image", "data": base64.b64encode(p.read_bytes()).decode(), "mimeType": mime})
                elif p.stat().st_size < 512 * 1024:
                    try:
                        files.append(f'<file name="{m.group(1)}">\n{p.read_text(encoding="utf-8")}\n</file>')
                    except UnicodeDecodeError:
                        pass
        if files:
            text = "\n".join(files) + "\n\n" + text
        return text, images

    def _run_entry(self, entry, args: str, text: str) -> bool:
        """Runs a built-in / Omni / extension command. False for prompt templates and skills (they become a prompt)."""
        if entry.kind in ("builtin", "omni"):
            self.in_worker(lambda: self.commands.run(entry.name, args), name="pi-command")
            return True
        if entry.kind == "extension":
            ref = entry.ref or {}
            self.in_worker(lambda: self.session.ext.run_command(entry.name, args, ref.get("id")), name="pi-ext-command")
            return True
        return False

    def _run_ambiguous(self, cs, name: str, args: str, cands, text: str) -> None:
        """Worker thread: `/name` comes from several sources - let the user pick (and optionally remember) one."""
        labels = [f"/{e.qualified:<28} {e.origin}" + (f" - {e.description[:60]}" if e.description else "") for e in cands]
        idx = self.select(f"/{name} is provided by {len(cands)} sources. Run which one?", labels)
        if idx is None:
            return
        chosen = cands[idx]
        how = self.select(f"Use /{chosen.qualified} for /{name}", ["Just this time", f"Always (change later with /commands)"])
        if how is None:
            return
        if how == 1:
            cs.remember(name, chosen.qualified)
            self.notify(f"/{name} now runs /{chosen.qualified}", "success")
        self.submit(f"/{chosen.qualified}" + (f" {args}" if args else ""))

    def submit(self, text: str) -> None:
        s = self.session
        self.scroll = 0
        stripped = text.strip()
        if stripped.startswith("!"):
            exclude = stripped.startswith("!!")
            cmd = stripped[2:] if exclude else stripped[1:]
            if cmd.strip():
                self.in_worker(lambda: s.user_bash(cmd.strip(), exclude))
            return
        if stripped.startswith("/"):
            name = stripped[1:].partition(" ")[0]
            cs = self.commands.sources()
            res = cs.resolve(stripped)
            if not res.candidates:
                self.notify(f"Unknown command /{name}. Type / to see commands.", "warning")
                return
            if len(res.candidates) > 1 and not res.remembered:
                # same name from several sources and no remembered choice: ask (on a worker, dialogs block)
                self.in_worker(lambda: self._run_ambiguous(cs, name, res.args, res.candidates, text), name="pi-command")
                return
            if self._run_entry(res.entry, res.args, text):
                return
            unique = len(res.candidates) == 1 and not res.explicit
            stripped = text = cs.prompt_text(res.entry, res.args, unique=unique)
        if self.working:
            s.steer(stripped)
            self.toast = ("message will steer the current run", "info", time.time() + 2)
            return
        body, images = self.expand_mentions(text)
        self.in_worker(lambda: s.prompt(body, images or None), name="pi-agent")

    # =============================================================================== dialog handling
    def dialog_key(self, d: Dialog, k: Key) -> None:
        n = k.name
        if d.kind == "custom":
            if n in ("ctrl+c",):
                self.session.ext.custom_cancel(d.custom_id)
                return
            self.session.ext.custom_input(d.custom_id, k.raw or k.text)
            return
        close = lambda v: (self.dialogs.remove(d), d.done(v))  # noqa: E731
        if d.kind == "show":
            if n in ("escape", "enter", "q", "ctrl+c"):
                close(None)
            elif n in ("down", "j", "wheeldown"):
                d.scroll += 1
            elif n in ("up", "k", "wheelup"):
                d.scroll = max(0, d.scroll - 1)
            elif n == "pagedown":
                d.scroll += 10
            elif n == "pageup":
                d.scroll = max(0, d.scroll - 10)
            return
        if d.kind == "confirm":
            if n in ("y",):
                close(True)
            elif n in ("n", "escape", "ctrl+c"):
                close(False)
            elif n in ("left", "right", "tab", "h", "l"):
                d.selected = 1 - d.selected
            elif n == "enter":
                close(d.selected == 0)
            return
        if d.kind == "select":
            vis = d.visible_options()
            if n in ("escape", "ctrl+c"):
                close(None)
            elif n == "enter":
                if vis:
                    pos = min(d.selected, len(vis) - 1)
                    close(vis[pos][0])
            elif n in ("up", "ctrl+p", "wheelup"):
                d.selected = (d.selected - 1) % max(1, len(vis))
            elif n in ("down", "ctrl+n", "wheeldown", "tab"):
                d.selected = (d.selected + 1) % max(1, len(vis))
            elif n == "pageup":
                d.selected = max(0, d.selected - 10)
            elif n == "pagedown":
                d.selected = min(len(vis) - 1, d.selected + 10)
            elif n == "backspace":
                d.filter = d.filter[:-1]
                d.selected = 0
            elif k.text and len(n) == 1:
                d.filter += k.text
                d.selected = 0
            return
        if d.kind == "input":
            ed = d.editor
            if n in ("escape", "ctrl+c"):
                close(None)
            elif n == "enter" and not d.multiline:
                close(ed.text)
            elif n in ("ctrl+s", "alt+enter") or (n == "enter" and d.multiline and ed.text.endswith("\n\n")):
                close(ed.text.rstrip("\n"))
            elif n in ("enter", "shift+enter", "ctrl+j"):
                ed.newline()
            elif n == "paste":
                ed.insert(k.text)
            elif n == "backspace":
                ed.backspace()
            elif n == "left":
                ed.left()
            elif n == "right":
                ed.right()
            elif n == "up":
                ed.up()
            elif n == "down":
                ed.down()
            elif n in ("home", "ctrl+a"):
                ed.home()
            elif n in ("end", "ctrl+e"):
                ed.end()
            elif n in ("ctrl+w", "alt+backspace"):
                ed.delete_word_back()
            elif k.text and len(n) == 1:
                ed.insert(k.text)

    # ========================================================================================= render
    def render(self) -> None:
        if self.regular:
            return self.render_regular()
        W, H = self.width, self.height
        p = self.pal
        s = self.session
        header = [self.render_header(W)]
        footer = self.render_footer(W)
        editor = self.render_editor(W)
        above = [l for key, (lines, o) in self.widgets.items() if (o or {}).get("placement") not in ("belowEditor", "footer", "header")
                 for l in lines]
        below = [l for key, (lines, o) in self.widgets.items() if (o or {}).get("placement") == "belowEditor" for l in lines]
        ext_header = self.widgets.get("__header", ([], {}))[0]
        ext_footer = self.widgets.get("__footer", ([], {}))[0]
        above = above[: max(0, H // 3)]
        status = [self.render_status(W)]
        fixed = len(header) + len(ext_header) + len(above) + len(status) + len(editor) + len(below) + len(footer) + len(ext_footer)
        T = max(3, H - fixed)
        body = self.render_transcript(W, T)
        frame = header + [pad(l, W) for l in ext_header] + body + [pad(" " + l, W) for l in above] + status + editor + \
            [pad(" " + l, W) for l in below] + footer + [pad(l, W) for l in ext_footer]
        frame = frame[:H] + [""] * max(0, H - len(frame))
        top_of_status = len(header) + len(ext_header) + T + len(above)
        if self.ac_items and not self.dialogs:
            frame = self.overlay_completions(frame, W, top_of_status)
        if self.dialogs:
            frame = self.overlay_dialog(frame, self.dialogs[-1], W, H)
        frame, placements = inline_images.extract([pad(l, W) + RESET for l in frame])
        if self.dialogs:  # images would cover dialogs
            placements = {}
        out = []
        if len(self.prev_frame) != len(frame):
            out.append("\x1b[2J")
            self.prev_frame = [""] * len(frame)
        dirty_rows = set()
        for i, line in enumerate(frame):
            if line != self.prev_frame[i]:
                out.append(f"\x1b[{i + 1};1H{line}")
                dirty_rows.add(i)
        img = inline_images.paint(self.image_store, self.image_protocol, placements, dirty_rows, H) if self.image_protocol else ""
        if out or img:
            self.term.write("\x1b[?2026h" + "".join(out) + img + "\x1b[?2026l")  # synchronized update where supported
            self.term.flush()
        self.prev_frame = frame

    # ------------------------------------------------------------------------------- regular (inline) mode
    def _live_index(self) -> int:
        """Index of the first block that may still change; blocks before it are final."""
        for i, b in enumerate(self.blocks):
            if b.animated() or b is self.current or (isinstance(b, ToolBlock) and b.status == "pending") or \
                    (isinstance(b, BashBlock) and getattr(b, "running", False)):
                return i
        return len(self.blocks)

    def render_regular(self) -> None:
        W, H = self.width, self.height
        ctx = self.render_ctx()
        inner = W - 4
        live_at = self._live_index()
        stable: List[str] = []
        for b in self.blocks[:live_at]:
            try:
                stable.extend(b.lines(inner, ctx))
            except Exception as e:
                stable.append(f"[render error in {b.kind}: {e}]")
        live: List[str] = []
        for b in self.blocks[live_at:]:
            try:
                live.extend(b.lines(inner, ctx))
            except Exception as e:
                live.append(f"[render error in {b.kind}: {e}]")
        if self.committed > len(stable):  # transcript was reset (/new, /resume, /tree): start a fresh screen
            self.committed = 0
            self.term.write("\x1b[2J\x1b[H")
            self.live_height = 0
        new_lines = stable[self.committed:]
        editor = self.render_editor(W)
        above = [l for key, (lines, o) in self.widgets.items() if (o or {}).get("placement") not in ("belowEditor", "footer", "header")
                 for l in lines][: max(0, H // 3)]
        below = [l for key, (lines, o) in self.widgets.items() if (o or {}).get("placement") == "belowEditor" for l in lines]
        bottom = [pad(" " + l, W) for l in above] + [self.render_status(W)] + editor + [pad(" " + l, W) for l in below] + self.render_footer(W)
        room = max(0, H - len(bottom) - 1)
        live_view = ["  " + pad(l, inner) for l in live[-room:]] if room else []
        frame = live_view + bottom
        if self.ac_items and not self.dialogs:
            need = min(len(self.ac_items), 8) + 2
            pad_rows = max(0, need - len(live_view))
            frame = [""] * pad_rows + frame
            frame = self.overlay_completions(frame, W, pad_rows + len(live_view) + len(above))
        if self.dialogs:
            dh = min(H, max(len(frame), 16))
            frame = [""] * (dh - len(frame)) + frame
            frame = self.overlay_dialog(frame, self.dialogs[-1], W, dh)
        frame, _ = inline_images.extract([pad(l, W) + RESET for l in frame])
        out: List[str] = []
        if self.live_height:
            out.append("\r" + (f"\x1b[{self.live_height - 1}A" if self.live_height > 1 else ""))
        out.append("\x1b[J")
        for line in new_lines:  # finished output goes to the terminal's scrollback once
            clean, placements = inline_images.extract(["  " + pad(line, inner)])
            out.append(clean[0] + RESET + "\r\n")
            if placements and self.image_protocol:
                out.append(self._inline_image_now(list(placements)[0]))
        self.committed = len(stable)
        out.append("\r\n".join(frame))
        self.live_height = len(frame)
        self.term.write("\x1b[?2026h" + "".join(out) + "\x1b[?2026l")
        self.term.flush()
        self.prev_frame = frame

    def _inline_image_now(self, iid: int) -> str:
        """Regular mode: draw an image at the cursor (the rows reserved after it scroll with the text)."""
        it = self.image_store.items.get(iid)
        if not it:
            return ""
        cols, rows = it.get("cells", (20, 8))
        if self.image_protocol == "kitty":
            png = inline_images._png(it["data"], it["mime"])
            if png is None:
                return ""
            seq, first = [], True
            for chunk, last in inline_images._chunks(png):
                head = f"a=T,f=100,c={cols},r={rows},C=1,q=2," if first else ""
                seq.append(f"\x1b_G{head}m={0 if last else 1};{chunk}\x1b\\")
                first = False
            return "\x1b7\x1b[1A\x1b[2C" + "".join(seq) + "\x1b8"
        return f"\x1b7\x1b[1A\x1b[2C\x1b]1337;File=inline=1;width={cols};height={rows};preserveAspectRatio=1:{it['data']}\x07\x1b8"

    def render_ctx(self) -> RenderCtx:
        ext = self.session.ext
        md = None
        if (getattr(ext, "features", {}) or {}).get("markdownTransformers"):
            md = lambda text, typ, streaming, cols, eid: ext.markdown_transform(text, typ, streaming, cols, eid)  # noqa: E731
        return RenderCtx(self.pal, self.theme_name, self.tools_expanded, self.show_thinking, self.ext_render, self.session.cwd,
                         md_transform=md, images=self.image_store, image_protocol=self.image_protocol)

    def render_header(self, W: int) -> str:
        p = self.pal
        s = self.session
        hb = bg(p["headerBg"])
        brand = gradient(" ◆ omni ", BRAND_GRADIENT)
        prof = s.profile.get("name", "pi")
        pill = bg(s.profile.get("accent") or p["accent"]) + fg(p["headerBg"]) + BOLD + f" {prof} " + RESET
        name = s.sessions.session_name() or ("new session" if not s.sessions.entries else s.sessions.session_id[:8])
        left = hb + brand + hb + " " + pill + hb + fg(p["muted"]) + "  " + name + RESET
        tcol = thinking_color(p, s.thinking_level)
        model = (fg(p["text"]) + model_key(s.model)) if s.model else (fg(p["warning"]) + "no model · /login")
        right = hb + model + fg(p["dim"]) + "  " + bg(tcol) + fg(p["headerBg"]) + BOLD + f" {s.thinking_level} " + RESET + hb + " "
        gap = max(1, W - width(left) - width(right))
        return left + hb + " " * gap + right + RESET

    def render_transcript(self, W: int, T: int) -> List[str]:
        ctx = self.render_ctx()
        inner = W - 4
        lines: List[str] = []
        for b in self.blocks:
            try:
                lines.extend(b.lines(inner, ctx))
            except Exception as e:
                lines.append(fg(self.pal["error"]) + f"[render error in {b.kind}: {e}]" + RESET)
        total = len(lines)
        max_scroll = max(0, total - T)
        self.scroll = min(self.scroll, max_scroll)
        end = total - self.scroll
        view = lines[max(0, end - T):end]
        view = [""] * (T - len(view)) + view if total < T else view
        # scrollbar
        bar = [""] * T
        if total > T:
            thumb = max(1, T * T // total)
            pos = int((T - thumb) * (1 - self.scroll / max_scroll)) if max_scroll else T - thumb
            for i in range(T):
                bar[i] = fg(self.pal["borderAccent"] if pos <= i < pos + thumb else self.pal["borderMuted"]) + ("┃" if pos <= i < pos + thumb else "│") + RESET
        out = []
        for i, l in enumerate(view):
            out.append("  " + pad(l, inner) + " " + (bar[i] if bar[i] else " "))
        if self.scroll and out:
            note = fg(self.pal["accent"]) + f" ↓ {self.scroll} more lines · pgdn / wheel " + RESET
            out[-1] = pad(" " * (W - width(note) - 2) + note, W)
        return out

    def render_status(self, W: int) -> str:
        p = self.pal
        parts = []
        if self.working and self.working_visible:
            msg = self.working_message or ("working" + "." * (int(time.time() * 2) % 4))
            parts.append(fg(p["accent"]) + spinner_frame() + " " + RESET + fg(p["muted"]) + msg + RESET + fg(p["dim"]) + "  esc to interrupt" + RESET)
        if self.retry_note:
            parts.append(fg(p["warning"]) + "↻ " + self.retry_note + RESET)
        st, fu = self.queue_counts
        if st or fu:
            parts.append(fg(p["accentAlt"]) + f"⧗ queued: {st} steer · {fu} follow-up (alt+↑ to edit)" + RESET)
        if self.toast:
            text, level, _ = self.toast
            col = {"error": p["error"], "warning": p["warning"], "success": p["success"]}.get(level, p["accent"])
            parts.append(fg(col) + ("✓ " if level == "success" else "• ") + text + RESET)
        return " " + "   ".join(parts)

    def render_editor(self, W: int) -> List[str]:
        if self.ext_editor_lines is not None and not self.dialogs:  # an extension's editor component draws itself
            return [pad(l, W) for l in self.ext_editor_lines[-12:]] or [""]
        p = self.pal
        s = self.session
        ed = self.editor
        bash_mode = ed.text.lstrip().startswith("!")
        border = p["bashMode"] if bash_mode else thinking_color(p, s.thinking_level)
        if s.thinking_level == "off" and not bash_mode:
            border = p["border"]
        b = fg(border)
        ebg = bg(p["editorBg"])
        inner = W - 4
        if ed.is_empty() and not self.dialogs:
            body = [fg(p["accent"]) + "|" + RESET + ebg + fg(p["dim"]) + ITALIC + (
                "steer the agent · alt+enter queues a follow-up" if self.working else
                "ask anything · / commands · @ files · ! shell · shift+enter newline") + RESET]
        else:
            style = ebg + (fg(p["bashMode"]) if bash_mode else fg(p["text"]))
            body = ed.render(inner, style, cursor=not self.dialogs)
        if len(body) > 10:
            cur = min(len(body) - 1, sum(1 for _ in range(ed.row)) + 0)
            start = max(0, min(len(body) - 10, cur - 5))
            body = body[start:start + 10]
        label = (fg(p["bashMode"]) + BOLD + " $ shell " if bash_mode else fg(border) + BOLD + " > " + RESET + fg(p["dim"]) + s.profile.get("name", "pi") + " ") + RESET
        right = ""
        if self.working:
            right = fg(p["accent"]) + " steering " + RESET
        top = b + "╭─" + RESET + label + b + "─" * max(0, W - 4 - width(label) - width(right)) + RESET + right + b + "─╮" + RESET
        out = [top]
        for l in body:
            out.append(b + "│" + RESET + ebg + " " + pad(l + ebg, inner) + ebg + " " + RESET + b + "│" + RESET)
        out.append(b + "╰" + "─" * (W - 2) + "╯" + RESET)
        return out

    def render_footer(self, W: int) -> List[str]:
        p = self.pal
        s = self.session
        fb = bg(p["footerBg"])
        home = str(Path.home())
        cwd = s.cwd.replace(home, "~", 1) if s.cwd.startswith(home) else s.cwd
        l1 = fb + fg(p["accent"]) + " ⌂ " + fg(p["text"]) + cwd + RESET
        if self.branch:
            l1 += fb + fg(p["dim"]) + "  git:" + fg(p["accentAlt"]) + self.branch + RESET
        if s.sessions.session_name():
            l1 += fb + fg(p["muted"]) + "  · " + s.sessions.session_name() + RESET
        stats = "  ".join(fb + fg(p["muted"]) + v + RESET for v in self.statuses.values())
        line1 = l1 + fb + " " * max(1, W - width(l1) - width(stats) - 1) + stats + fb + " " + RESET
        t = s.totals()
        u = s.context_usage() if s.model else {"percent": None, "tokens": 0, "contextWindow": 0}
        seg = fb + fg(p["dim"]) + " ↑" + fg(p["text"]) + human(t["input"]) + fg(p["dim"]) + " ↓" + fg(p["text"]) + human(t["output"])
        if t["cacheRead"] or t["cacheWrite"]:
            seg += fg(p["dim"]) + " R" + fg(p["text"]) + human(t["cacheRead"]) + fg(p["dim"]) + " W" + fg(p["text"]) + human(t["cacheWrite"])
        seg += fg(p["dim"]) + "  $" + fg(p["success"]) + f"{t['cost']:.3f}" + RESET
        if u.get("percent") is not None:
            pct = u["percent"]
            cells = 10
            fill = min(cells, int(round(pct / 100 * cells)))
            col = p["success"] if pct < 60 else p["warning"] if pct < 85 else p["error"]
            seg += fb + fg(p["dim"]) + "  ctx " + fg(col) + "▰" * fill + fg(p["borderMuted"]) + "▱" * (cells - fill) + \
                fg(p["text"]) + f" {pct:.0f}%" + fg(p["dim"]) + f" of {human(u['contextWindow'])}" + RESET
        mk = model_key(s.model) if s.model else ""
        routed = getattr(s, "routed", None)
        if s.model and s.model.get("virtual") and routed:  # virtual model: show where the router sent the last request
            mk += f" • {s.thinking_level} → {routed[0]['id']} • {routed[1]}"
        right = fb + fg(p["muted"]) + mk + fg(p["dim"]) + f" · {len(s.active_tool_names)} tools" + RESET
        line2 = seg + fb + " " * max(1, W - width(seg) - width(right) - 1) + right + fb + " " + RESET
        return [line1, line2]

    def overlay_completions(self, frame: List[str], W: int, bottom: int) -> List[str]:
        p = self.pal
        n = min(len(self.ac_items), int(self.session.settings.get("autocompleteMaxVisible", 5)) + 3)
        start = max(0, min(self.ac_index - n // 2, len(self.ac_items) - n))
        items = self.ac_items[start:start + n]
        box_w = min(W - 4, max(30, max(width(i["label"]) + width(i["desc"]) + 8 for i in items)))
        lines = []
        for j, it in enumerate(items):
            sel = start + j == self.ac_index
            label = (fg(p["accent"]) + BOLD if sel else fg(p["text"])) + it["label"] + RESET
            desc = fg(p["dim"]) + "  " + it["desc"] + RESET if it["desc"] else ""
            tag = fg(p["muted"]) + it["tag"] + RESET if it["tag"] not in ("builtin", "file") else ""
            row = label + desc
            row = pad(row, box_w - 6 - width(tag)) + tag
            lines.append((bg(p["selectedBg"]) if sel else "") + row)
        box = panel(lines, box_w, p, p["borderAccent"], title=fg(p["muted"]) + "tab/enter to accept" + RESET, bgc=p["panelBg"])
        top = max(1, bottom - len(box))
        for i, l in enumerate(box):
            if 0 <= top + i < len(frame):
                frame[top + i] = "  " + l
        return frame

    def overlay_dialog(self, frame: List[str], d: Dialog, W: int, H: int) -> List[str]:
        p = self.pal
        box_w = min(W - 6, 100)
        inner = box_w - 4
        lines: List[str] = []
        hint = ""
        if d.kind == "custom":
            lines = [truncate(l, inner) for l in d.lines] or [""]
            box_w = min(W - 2, max(20, max(width(l) for l in lines) + 4))
            box = panel(lines[: H - 4], box_w, p, p["borderAccent"], bgc=p["panelBg"])
        else:
            if d.kind == "select":
                vis = d.visible_options()
                lines.append(fg(p["muted"]) + "filter: " + RESET + fg(p["text"]) + d.filter + RESET + fg(p["accent"]) + "_" + RESET)
                lines.append("")
                n = max(3, min(len(vis), H - 12))
                d.selected = min(d.selected, max(0, len(vis) - 1))
                start = max(0, min(d.selected - n // 2, len(vis) - n))
                for j, (_, o) in enumerate(vis[start:start + n]):
                    sel = start + j == d.selected
                    lines.append((bg(p["selectedBg"]) + fg(p["accent"]) + BOLD + "› " if sel else fg(p["text"]) + "  ") + truncate(o, inner - 2) + RESET)
                if not vis:
                    lines.append(fg(p["dim"]) + "  no matches" + RESET)
                if len(vis) > n:
                    lines.append(fg(p["dim"]) + f"  {d.selected + 1}/{len(vis)}" + RESET)
                hint = "↑↓ move · type to filter · enter select · esc cancel"
            elif d.kind == "confirm":
                for l in wrap(d.message, inner):
                    lines.append(fg(p["text"]) + l + RESET)
                lines.append("")
                yes = (bg(p["accent"]) + fg(p["panelBg"]) + BOLD if d.selected == 0 else fg(p["muted"])) + "  Yes  " + RESET
                no = (bg(p["accent"]) + fg(p["panelBg"]) + BOLD if d.selected == 1 else fg(p["muted"])) + "  No  " + RESET
                lines.append(yes + "   " + no)
                hint = "y/n · ←→ · enter"
            elif d.kind == "input":
                ed = d.editor
                if d.secret:
                    shown = "•" * len(ed.text)
                    lines.append(fg(p["text"]) + shown + RESET + fg(p["accent"]) + "_" + RESET)
                elif ed.is_empty() and d.placeholder:
                    lines.append(fg(p["dim"]) + ITALIC + d.placeholder + RESET + fg(p["accent"]) + "_" + RESET)
                else:
                    lines.extend(ed.render(inner, fg(p["text"]))[: H - 10])
                hint = "ctrl+s / alt+enter save · esc cancel" if d.multiline else "enter confirm · esc cancel"
            elif d.kind == "show":
                body: List[str] = []
                for l in d.lines:
                    body.extend(wrap(fg(p["text"]) + l + RESET, inner))
                n = max(3, H - 8)
                d.scroll = min(d.scroll, max(0, len(body) - n))
                lines = body[d.scroll:d.scroll + n]
                hint = "↑↓ scroll · esc close" if len(body) > n else "esc / enter close"
            if hint:
                lines += ["", fg(p["dim"]) + hint + RESET]
            title = fg(p["accent"]) + BOLD + d.title + RESET if d.title else ""
            box = panel(lines, box_w, p, p["borderAccent"], title=title, bgc=p["panelBg"])
        top = max(1, (H - len(box)) // 2 - 1)
        left = max(0, (W - box_w) // 2)
        shadow = fg(p["borderMuted"])
        for i, l in enumerate(box):
            r = top + i
            if 0 <= r < len(frame):
                base = strip(frame[r])
                frame[r] = DIM + fg(p["dim"]) + pad(base[:left], left) + RESET + l + DIM + fg(p["dim"]) + base[left + box_w:] + RESET
        return frame


def run_interactive(opts: SessionOptions, messages: Optional[List[str]] = None) -> int:
    if not Terminal.is_tty():
        print("Interactive mode needs a terminal. Use -p/--print or --mode json|rpc for non-interactive use.")
        return 2
    return InteractiveApp(opts, messages).run()
