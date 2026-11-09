"""
Live browser relay over the Chrome DevTools Protocol (CDP).

Talks to a *real* Chromium-family browser (Chrome, Edge, Brave) that was started with
``--remote-debugging-port``. Every operation is a genuine CDP round-trip over the page's
WebSocket debugger URL. There are no fake tabs or canned results: if no browser is
reachable a :class:`BrowserNotConnected` error explains how to start one.

Firefox exposes WebDriver BiDi instead of CDP; it is detected and reported clearly but
is not driven by this module.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import time
from typing import Any, Dict, List, Optional
import urllib.error
import urllib.parse
import urllib.request
from ..paths import user_data_dir

logger = logging.getLogger(__name__)

try:  # `websockets` is the only runtime dependency of this module.
    from websockets.sync.client import connect as _ws_connect
except Exception:  # pragma: no cover - exercised only when the package is missing
    _ws_connect = None


class BrowserRelayError(RuntimeError):
    """Base class for relay failures."""


class BrowserNotConnected(BrowserRelayError):
    """No CDP endpoint is reachable on the configured host/port."""


class CdpError(BrowserRelayError):
    """The browser returned a protocol-level error for a command."""


@dataclass
class BrowserTab:
    id: str
    title: str
    url: str
    type: str = "page"
    ws_url: Optional[str] = None


_CHROME_CANDIDATES = [
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    "/usr/bin/google-chrome",
    "/usr/bin/chromium",
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
]


def find_chromium() -> Optional[str]:
    """Locate a Chromium-family executable, or None."""
    for name in ("chrome", "google-chrome", "chromium", "msedge", "brave"):
        found = shutil.which(name)
        if found:
            return found
    for path in _CHROME_CANDIDATES:
        if os.path.exists(path):
            return path
    return None


class ChromeDevToolsRelay:
    def __init__(self, host: str = "127.0.0.1", port: int = 9222, timeout: float = 10.0):
        self.host = host
        self.port = port
        self.timeout = timeout
        self.base_url = f"http://{host}:{port}"
        self.active_tab_id: Optional[str] = None
        self._proc: Optional[subprocess.Popen] = None

    # ------------------------------------------------------------------ connection
    def _http(self, path: str, method: str = "GET", timeout: Optional[float] = None) -> Any:
        req = urllib.request.Request(
            f"{self.base_url}{path}", method=method, headers={"User-Agent": "OmniBrowserRelay/2.0"}
        )
        with urllib.request.urlopen(req, timeout=timeout or self.timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
        return json.loads(raw) if raw.strip().startswith(("{", "[")) else raw

    def browser_kind(self) -> Optional[str]:
        """Return 'chromium', 'firefox', or None when nothing is listening."""
        try:
            self._http("/json/version", timeout=1.5)
            return "chromium"
        except urllib.error.HTTPError as e:
            if e.code == 404:  # Firefox's httpd.js answers 404 for CDP discovery paths
                return "firefox"
            return None
        except Exception:
            return None

    def is_connected(self) -> bool:
        return self.browser_kind() == "chromium"

    def _require_connection(self) -> None:
        kind = self.browser_kind()
        if kind == "chromium":
            return
        if kind == "firefox":
            raise BrowserNotConnected(
                f"Firefox is listening on {self.host}:{self.port}, but it speaks WebDriver BiDi, "
                "not CDP. Use Chrome, Edge or Brave (try `/browser launch`)."
            )
        raise BrowserNotConnected(
            f"No browser is listening on {self.host}:{self.port}. Run `/browser launch`, or start one with: "
            f'"chrome.exe" --remote-debugging-port={self.port} --user-data-dir=<some dir>'
        )

    def launch(self, url: str = "about:blank", headless: bool = False) -> str:
        """Start a Chromium browser with remote debugging and a dedicated profile."""
        if self.is_connected():
            return f"Browser already reachable on {self.host}:{self.port}"
        exe = find_chromium()
        if not exe:
            raise BrowserRelayError("No Chrome/Edge/Brave executable found on this machine.")
        profile = str(user_data_dir() / "browser-profile")
        os.makedirs(profile, exist_ok=True)
        args = [
            exe,
            f"--remote-debugging-port={self.port}",
            f"--user-data-dir={profile}",
            "--no-first-run",
            "--no-default-browser-check",
        ]
        if headless:
            args.append("--headless=new")
        args.append(url)
        self._proc = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if self.is_connected():
                return f"Launched {os.path.basename(exe)} (debug port {self.port}, profile {profile})"
            time.sleep(0.3)
        raise BrowserNotConnected("Browser started but the debugging port never opened.")

    # ------------------------------------------------------------------ tabs
    def list_tabs(self) -> List[BrowserTab]:
        self._require_connection()
        items = self._http("/json/list")
        tabs = [
            BrowserTab(
                id=it.get("id", ""),
                title=it.get("title", ""),
                url=it.get("url", ""),
                type=it.get("type", "page"),
                ws_url=it.get("webSocketDebuggerUrl"),
            )
            for it in items
            if it.get("type") == "page"
        ]
        if tabs and self.active_tab_id not in {t.id for t in tabs}:
            self.active_tab_id = tabs[0].id
        return tabs

    def open_tab(self, url: str) -> BrowserTab:
        self._require_connection()
        data = self._http(f"/json/new?{urllib.parse.quote(url, safe=':/?&=#%')}", method="PUT")
        tab = BrowserTab(
            id=data["id"],
            title=data.get("title", ""),
            url=data.get("url", url),
            ws_url=data.get("webSocketDebuggerUrl"),
        )
        self.active_tab_id = tab.id
        return tab

    def close_tab(self, tab_id: str) -> bool:
        self._require_connection()
        self._http(f"/json/close/{tab_id}")
        if self.active_tab_id == tab_id:
            self.active_tab_id = None
        return True

    def _tab(self, tab_id: Optional[str]) -> BrowserTab:
        tabs = self.list_tabs()
        wanted = tab_id or self.active_tab_id
        for t in tabs:
            if t.id == wanted:
                return t
        if not tabs:
            raise BrowserRelayError("The browser has no open page tabs.")
        return tabs[0]

    # ------------------------------------------------------------------ CDP transport
    def call(self, method: str, params: Optional[Dict[str, Any]] = None, tab_id: Optional[str] = None,
             timeout: Optional[float] = None) -> Dict[str, Any]:
        """Send one CDP command to a tab and return its ``result`` object."""
        if _ws_connect is None:
            raise BrowserRelayError("The 'websockets' package is required: pip install websockets")
        tab = self._tab(tab_id)
        if not tab.ws_url:
            raise BrowserRelayError(
                f"Tab {tab.id} has no debugger URL (another DevTools client may be attached to it)."
            )
        limit = timeout or self.timeout
        with _ws_connect(tab.ws_url, open_timeout=limit, max_size=64 * 1024 * 1024) as ws:
            ws.send(json.dumps({"id": 1, "method": method, "params": params or {}}))
            deadline = time.monotonic() + limit
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError(f"CDP command {method} timed out after {limit}s")
                msg = json.loads(ws.recv(timeout=remaining))
                if msg.get("id") == 1:  # skip unsolicited protocol events
                    if "error" in msg:
                        raise CdpError(f"{method}: {msg['error'].get('message', msg['error'])}")
                    return msg.get("result", {})

    # ------------------------------------------------------------------ high-level actions
    def evaluate_js(self, expression: str, tab_id: Optional[str] = None) -> Any:
        res = self.call(
            "Runtime.evaluate",
            {"expression": expression, "returnByValue": True, "awaitPromise": True},
            tab_id=tab_id,
        )
        if "exceptionDetails" in res:
            details = res["exceptionDetails"]
            text = details.get("exception", {}).get("description") or details.get("text", "JS exception")
            raise CdpError(text)
        return res.get("result", {}).get("value")

    def navigate(self, url: str, tab_id: Optional[str] = None) -> Dict[str, Any]:
        res = self.call("Page.navigate", {"url": url}, tab_id=tab_id)
        if res.get("errorText"):
            raise CdpError(f"navigation failed: {res['errorText']}")
        return res

    _SNAPSHOT_JS = """(() => {
        const vis = el => !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
        const links = [...document.querySelectorAll('a[href]')].filter(vis).slice(0, 30)
            .map(a => ({text: (a.innerText || a.getAttribute('aria-label') || '').trim().slice(0, 80), href: a.href}))
            .filter(l => l.text);
        const controls = [...document.querySelectorAll('input,button,textarea,select')].filter(vis).slice(0, 30)
            .map(e => ({tag: e.tagName.toLowerCase(), id: e.id, name: e.name, type: e.type || '',
                        text: (e.innerText || e.value || e.placeholder || '').trim().slice(0, 60)}));
        return {title: document.title, url: location.href,
                text: (document.body ? document.body.innerText : '').replace(/\\s+/g, ' ').slice(0, 3000),
                links, controls};
    })()"""

    def snapshot_dom(self, tab_id: Optional[str] = None) -> Dict[str, Any]:
        return self.evaluate_js(self._SNAPSHOT_JS, tab_id=tab_id) or {}

    def click_element(self, selector: str, tab_id: Optional[str] = None) -> bool:
        return bool(self.evaluate_js(
            f"(() => {{ const e = document.querySelector({json.dumps(selector)}); "
            "if (!e) return false; e.scrollIntoView({block:'center'}); e.click(); return true; })()",
            tab_id=tab_id,
        ))

    def fill_input(self, selector: str, text: str, tab_id: Optional[str] = None) -> bool:
        return bool(self.evaluate_js(
            f"(() => {{ const e = document.querySelector({json.dumps(selector)}); if (!e) return false; "
            f"e.focus(); e.value = {json.dumps(text)}; "
            "e.dispatchEvent(new Event('input',{bubbles:true})); "
            "e.dispatchEvent(new Event('change',{bubbles:true})); return true; })()",
            tab_id=tab_id,
        ))

    def capture_screenshot(self, path: Optional[str] = None, tab_id: Optional[str] = None) -> str:
        """Save a PNG screenshot and return its file path."""
        import base64

        data = self.call("Page.captureScreenshot", {"format": "png"}, tab_id=tab_id)["data"]
        if path is None:
            path = os.path.join(tempfile.gettempdir(), f"omni-shot-{int(time.time())}.png")
        with open(path, "wb") as f:
            f.write(base64.b64decode(data))
        return path

    def read_console_errors(self, tab_id: Optional[str] = None, window: float = 1.0) -> List[str]:
        """Return buffered console errors/warnings and uncaught exceptions.

        ``Runtime.enable`` replays console API calls and exceptions the page already
        emitted; ``Log.enable`` replays browser-generated entries (network, CSP, ...).
        """
        if _ws_connect is None:
            raise BrowserRelayError("The 'websockets' package is required: pip install websockets")
        tab = self._tab(tab_id)
        out: List[str] = []
        with _ws_connect(tab.ws_url, open_timeout=self.timeout) as ws:
            ws.send(json.dumps({"id": 1, "method": "Runtime.enable"}))
            ws.send(json.dumps({"id": 2, "method": "Log.enable"}))
            deadline = time.monotonic() + window
            while time.monotonic() < deadline:
                try:
                    msg = json.loads(ws.recv(timeout=max(0.05, deadline - time.monotonic())))
                except TimeoutError:
                    break
                method, params = msg.get("method"), msg.get("params", {})
                if method == "Runtime.consoleAPICalled" and params.get("type") in ("error", "warning", "assert"):
                    text = " ".join(str(a.get("value", a.get("description", ""))) for a in params.get("args", []))
                    out.append(f"[console.{params['type']}] {text}")
                elif method == "Runtime.exceptionThrown":
                    d = params.get("exceptionDetails", {})
                    out.append(f"[exception] {d.get('exception', {}).get('description') or d.get('text', '')}")
                elif method == "Log.entryAdded" and params["entry"].get("level") in ("error", "warning"):
                    e = params["entry"]
                    out.append(f"[{e['level']}] {e.get('text', '')} ({e.get('url', '')})")
        return out

    def close(self) -> None:
        """Terminate a browser that this relay launched (never touches one it did not start)."""
        if self._proc and self._proc.poll() is None:
            self._proc.terminate()
        self._proc = None
