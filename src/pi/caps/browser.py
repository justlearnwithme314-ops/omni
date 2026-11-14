"""`browser`: drive a real browser by element index (Browser Use's key idea).

Instead of asking the model for CSS selectors or pixel coordinates, the page is shown as a numbered list of its
visible interactive elements:

    Example Domain — https://example.com/login
    [1]<input type=email placeholder=Email>
    [2]<input type=password placeholder=Password>
    [3]<button type=submit>Sign in</button>
    [4]<a href=/forgot>Forgot password?</a>

and the model acts on a number: `click 3`, `type 1 "me@example.com"`. Every action returns the page's new state,
so each step is one tool call. The list is capped (120 elements, 80 characters each) to keep it cheap; `text`
pages through the readable content when the task is reading rather than acting.

One tool, `browser`, with actions: open, state, click, type, select, scroll, back, text, tabs, close. It talks to
Chrome, Edge or Brave over the DevTools protocol, launching one with its own profile under `~/.omni` if none is
listening on port 9222 (`"browser": {"headless": true, "port": 9222}` in the profile). Needs the `websockets`
package.
"""
from __future__ import annotations

import json
import time
from typing import Any, Callable, Dict, List, Optional

from ..tools.base import ToolDef, ToolError, check_abort, text_result
from . import Capability

MAX_ELEMENTS, MAX_TEXT = 120, 6000

STATE_JS = """(() => {
  const MAX = %d;
  const vis = el => { const r = el.getBoundingClientRect(); const st = getComputedStyle(el);
    return r.width > 0 && r.height > 0 && st.visibility !== 'hidden' && st.display !== 'none'; };
  document.querySelectorAll('[data-omni-idx]').forEach(e => e.removeAttribute('data-omni-idx'));
  const sel = 'a[href],button,input:not([type=hidden]),textarea,select,summary,[role=button],[role=link],[role=tab],' +
              '[role=menuitem],[role=checkbox],[role=radio],[role=option],[onclick],[contenteditable=true],[tabindex]:not([tabindex="-1"])';
  const all = [...document.querySelectorAll(sel)].filter(vis);
  const items = all.slice(0, MAX).map((e, i) => {
    e.setAttribute('data-omni-idx', String(i + 1));
    const tag = e.tagName.toLowerCase();
    const text = (e.innerText || e.value || e.getAttribute('aria-label') || e.title || e.alt || '').trim().replace(/\\s+/g, ' ').slice(0, 80);
    const attrs = {};
    for (const k of ['type', 'name', 'placeholder', 'href', 'role', 'aria-label']) {
      const v = e.getAttribute(k); if (v) attrs[k] = v.slice(0, k === 'href' ? 80 : 40); }
    if (e.disabled) attrs.disabled = 'true';
    if (tag === 'input' && (e.type === 'checkbox' || e.type === 'radio')) attrs.checked = String(e.checked);
    if (tag === 'select') attrs.options = [...e.options].slice(0, 12).map(o => o.text.trim()).join('|').slice(0, 120);
    return {i: i + 1, tag, text, attrs};
  });
  return {title: document.title, url: location.href, items, total: all.length, scrollY: Math.round(scrollY),
          height: document.documentElement.scrollHeight, viewport: innerHeight};
})()"""

FIND = "document.querySelector('[data-omni-idx=\"%d\"]')"
CLICK_JS = "(() => { const e = " + FIND + "; if (!e) return 'missing'; e.scrollIntoView({block: 'center'}); e.click(); return 'ok'; })()"
TYPE_JS = """(() => { const e = """ + FIND + """; if (!e) return 'missing';
  e.scrollIntoView({block: 'center'}); e.focus();
  const text = %s, submit = %s;
  if (e.isContentEditable) { e.textContent = text; }
  else { const proto = e.tagName === 'TEXTAREA' ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
         const set = Object.getOwnPropertyDescriptor(proto, 'value'); set && set.set ? set.set.call(e, text) : (e.value = text); }
  e.dispatchEvent(new Event('input', {bubbles: true})); e.dispatchEvent(new Event('change', {bubbles: true}));
  if (submit) { const o = {key: 'Enter', code: 'Enter', keyCode: 13, which: 13, bubbles: true};
    e.dispatchEvent(new KeyboardEvent('keydown', o)); e.dispatchEvent(new KeyboardEvent('keyup', o));
    if (e.form) { e.form.requestSubmit ? e.form.requestSubmit() : e.form.submit(); } }
  return 'ok'; })()"""
SELECT_JS = """(() => { const e = """ + FIND + """; if (!e) return 'missing'; if (e.tagName !== 'SELECT') return 'not-select';
  const want = %s.toLowerCase(); const o = [...e.options].find(o => o.text.trim().toLowerCase() === want || o.value.toLowerCase() === want)
    || [...e.options].find(o => o.text.toLowerCase().includes(want));
  if (!o) return 'no-option'; e.value = o.value; e.dispatchEvent(new Event('input', {bubbles: true}));
  e.dispatchEvent(new Event('change', {bubbles: true})); return 'ok'; })()"""
TEXT_JS = "(document.body ? document.body.innerText : '').replace(/[ \\t]+/g, ' ').replace(/\\n{3,}/g, '\\n\\n')"


def _default_relay(session: Any) -> Any:
    from ...features.browser_relay import ChromeDevToolsRelay
    opt = session.profile.get("browser") if isinstance(session.profile.get("browser"), dict) else {}
    return ChromeDevToolsRelay(port=int(opt.get("port") or 9222))


make_relay: Callable[[Any], Any] = _default_relay   # tests swap this for a fake


def format_state(st: Dict[str, Any]) -> str:
    rows = [f"{st.get('title') or '(untitled)'} — {st.get('url')}"]
    for it in st.get("items") or []:
        attrs = "".join(f" {k}={v}" for k, v in (it.get("attrs") or {}).items())
        rows.append(f"[{it['i']}]<{it['tag']}{attrs}>{it.get('text') or ''}</{it['tag']}>" if it.get("text")
                    else f"[{it['i']}]<{it['tag']}{attrs}>")
    if not st.get("items"):
        rows.append("(no interactive elements visible)")
    extra = (st.get("total") or 0) - len(st.get("items") or [])
    if extra > 0:
        rows.append(f"… {extra} more elements not listed")
    h, y, v = st.get("height") or 0, st.get("scrollY") or 0, st.get("viewport") or 0
    if h > v > 0:
        rows.append(f"(scrolled {min(100, round(100 * (y + v) / h))}% of the page; `scroll down` for more, `text` to read the content)")
    return "\n".join(rows)


class Browser:
    def __init__(self, session: Any):
        self.session = session
        self.relay: Any = None
        opt = session.profile.get("browser") if isinstance(session.profile.get("browser"), dict) else {}
        self.headless = bool(opt.get("headless", False))
        self.allowed = [str(d).lower() for d in opt.get("allowed_domains") or []]   # e.g. ["*.example.com", "docs.python.org"]

    def check_url(self, url: str) -> None:
        """Refuse to visit a site outside the profile's allowlist (no list = everything allowed)."""
        if not self.allowed or url.startswith("about:"):
            return
        import fnmatch
        import urllib.parse
        host = (urllib.parse.urlparse(url).hostname or "").lower()
        if not any(fnmatch.fnmatch(host, pat) or (pat.startswith("*.") and host == pat[2:]) for pat in self.allowed):
            raise ToolError(f"{host or url} is not in this profile's allowed browser domains ({', '.join(self.allowed)})")

    def connect(self) -> Any:
        if self.relay is None:
            self.relay = make_relay(self.session)
        if not self.relay.is_connected():
            self.relay.launch("about:blank", headless=self.headless)
        return self.relay

    def js(self, expr: str) -> Any:
        try:
            return self.connect().evaluate_js(expr)
        except ToolError:
            raise
        except Exception as e:
            raise ToolError(f"browser: {type(e).__name__}: {e}") from None

    def settle(self, seconds: float = 6.0) -> None:
        deadline = time.time() + seconds
        time.sleep(0.25)
        while time.time() < deadline:
            try:
                if self.connect().evaluate_js("document.readyState") == "complete":
                    return
            except Exception:
                pass
            time.sleep(0.2)

    def state(self) -> str:
        st = self.js(STATE_JS % MAX_ELEMENTS)
        if not isinstance(st, dict):
            raise ToolError("browser: the page did not return a state (is a tab open?)")
        try:
            self.check_url(str(st.get("url") or ""))
        except ToolError:
            self.js("history.back()")  # a click or redirect left the allowed sites: go back and say so
            raise
        return format_state(st)

    def act(self, result: Any, index: int) -> None:
        if result == "missing":
            raise ToolError(f"no element [{index}] on the page now; call browser(action='state') for the current list")
        if result == "not-select":
            raise ToolError(f"element [{index}] is not a <select>; use click or type")
        if result == "no-option":
            raise ToolError(f"element [{index}] has no such option; its options are listed in the state")


def browser_of(session: Any) -> Optional[Browser]:
    return getattr(session, "_browser", None)


def _tools(session: Any) -> Dict[str, ToolDef]:
    b = session._browser = Browser(session)

    def execute(_id, p, signal=None, on_update=None, ctx=None):
        check_abort(signal)
        action = str(p.get("action") or "").lower()
        index = int(p.get("index") or 0)
        if action == "open":
            url = str(p.get("url") or "").strip()
            if not url:
                raise ToolError("url is required for open")
            if "://" not in url:
                url = "https://" + url
            b.check_url(url)
            try:
                b.connect().navigate(url)
            except ToolError:
                raise
            except Exception as e:
                raise ToolError(f"browser: {type(e).__name__}: {e}") from None
            b.settle()
            return text_result(b.state(), {"action": action})
        if action == "state":
            return text_result(b.state(), {"action": action})
        if action == "click":
            b.act(b.js(CLICK_JS % index), index)
            b.settle(4.0)
            return text_result(b.state(), {"action": action, "index": index})
        if action == "type":
            if "text" not in p:
                raise ToolError("text is required for type")
            b.act(b.js(TYPE_JS % (index, json.dumps(str(p["text"])), "true" if p.get("submit") else "false")), index)
            if p.get("submit"):
                b.settle(4.0)
            return text_result(b.state(), {"action": action, "index": index})
        if action == "select":
            b.act(b.js(SELECT_JS % (index, json.dumps(str(p.get("text") or "")))), index)
            return text_result(b.state(), {"action": action, "index": index})
        if action == "scroll":
            sign = "-" if str(p.get("direction") or "down").lower() == "up" else ""
            b.js(f"window.scrollBy(0, {sign}Math.round(innerHeight * 0.85))")
            time.sleep(0.2)
            return text_result(b.state(), {"action": action})
        if action == "back":
            b.js("history.back()")
            b.settle(4.0)
            return text_result(b.state(), {"action": action})
        if action == "text":
            text = str(b.js(TEXT_JS) or "")
            offset = max(0, int(p.get("offset") or 0))
            chunk = text[offset:offset + MAX_TEXT]
            rest = len(text) - offset - len(chunk)
            note = f"\n\n[{rest} more characters; call again with offset={offset + len(chunk)}]" if rest > 0 else ""
            return text_result((chunk or "(the page has no text)") + note, {"action": action, "totalChars": len(text)})
        if action == "screenshot":
            import base64
            import os
            import tempfile
            path = os.path.join(tempfile.gettempdir(), f"omni-shot-{int(time.time() * 1000)}.png")
            try:
                saved = b.connect().capture_screenshot(path)
                with open(saved or path, "rb") as f:
                    data = base64.b64encode(f.read()).decode("ascii")
            except ToolError:
                raise
            except Exception as e:
                raise ToolError(f"browser: {type(e).__name__}: {e}") from None
            finally:
                try:
                    os.remove(path)
                except OSError:
                    pass
            return {"content": [{"type": "text", "text": "Screenshot of the current page."},
                                {"type": "image", "data": data, "mimeType": "image/png"}], "details": {"action": action}}
        if action == "key":
            key = str(p.get("text") or "Enter")
            codes = {"Enter": 13, "Escape": 27, "Tab": 9, "Backspace": 8, "ArrowDown": 40, "ArrowUp": 38, "ArrowLeft": 37,
                     "ArrowRight": 39, "PageDown": 34, "PageUp": 33, "Home": 36, "End": 35, "Space": 32, "Delete": 46}
            if key not in codes:
                raise ToolError("key must be one of: " + ", ".join(codes))
            b.js("(() => { const e = document.activeElement || document.body; const o = {key: %s, code: %s, keyCode: %d, which: %d, "
                 "bubbles: true, cancelable: true}; for (const t of ['keydown', 'keypress', 'keyup']) e.dispatchEvent(new KeyboardEvent(t, o)); "
                 "if (%s === 'Enter' && e.form) { e.form.requestSubmit ? e.form.requestSubmit() : e.form.submit(); } return 'ok'; })()"
                 % (json.dumps(" " if key == "Space" else key), json.dumps(key), codes[key], codes[key], json.dumps(key)))
            b.settle(3.0)
            return text_result(b.state(), {"action": action, "key": key})
        if action == "wait":
            want, limit = str(p.get("text") or ""), max(0.2, min(float(p.get("seconds") or (10 if p.get("text") else 2)), 60))
            if not want:
                time.sleep(limit)
                return text_result(b.state(), {"action": action, "waited": limit})
            deadline = time.time() + limit
            while time.time() < deadline:
                if want.lower() in str(b.js(TEXT_JS) or "").lower():
                    return text_result(b.state(), {"action": action, "found": True})
                time.sleep(0.4)
            raise ToolError(f"'{want}' did not appear within {limit:g} seconds")
        if action == "tabs":
            tabs = b.connect().list_tabs()
            return text_result("\n".join(f"{t.id[:8]}  {t.title[:60]}  {t.url[:100]}" for t in tabs) or "(no tabs)", {"action": action})
        if action == "close":
            _unload(session)
            session._browser = Browser(session)
            return text_result("Browser connection closed.", {"action": action})
        raise ToolError("action must be one of: open, state, click, type, select, scroll, back, text, key, wait, screenshot, tabs, close")

    return {"browser": ToolDef(
        name="browser", label="Browser",
        description="Control a real web browser. Pages are shown as numbered interactive elements, e.g. "
                    "`[3]<button type=submit>Sign in</button>`; act on the number. Actions: open(url), state, "
                    "click(index), type(index, text, submit?), select(index, text), scroll(direction up|down), back, "
                    "text(offset?) to read the page content, key(text: Enter|Escape|Tab|ArrowDown|...) on the focused element, wait(text?, seconds?) until text appears, screenshot (an image, for visual checks), tabs, close. Every action returns the page's new state; "
                    "indexes are valid only for the latest state.",
        parameters={"type": "object", "properties": {
            "action": {"type": "string", "enum": ["open", "state", "click", "type", "select", "scroll", "back", "text", "key", "wait", "screenshot", "tabs", "close"]},
            "url": {"type": "string"}, "index": {"type": "integer"}, "text": {"type": "string"},
            "submit": {"type": "boolean"}, "direction": {"type": "string"}, "offset": {"type": "integer"},
            "seconds": {"type": "number"}},
            "required": ["action"]},
        execute=execute, prompt_snippet="Browse and operate web pages")}


def _unload(session: Any) -> None:
    b = browser_of(session)
    if b is not None and b.relay is not None:
        try:
            b.relay.close()
        except Exception:
            pass
    session._browser = None


def _available(session: Any) -> Optional[str]:
    if make_relay is not _default_relay:
        return None
    import importlib.util
    # cheap on purpose: importing the relay (and `websockets`) is left to the first browser action
    if importlib.util.find_spec("websockets") is None:
        return "needs the `websockets` package (pip install websockets)"
    return None


CAPABILITIES = [Capability("browser", "Browser control", "Operate a real browser through numbered page elements.",
                           "Browser Use", tools=_tools, unload=_unload, available=_available)]
