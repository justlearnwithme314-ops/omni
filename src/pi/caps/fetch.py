"""`fetch`: read a web page or API response as text (Oh My Pi `fetch`, Hermes `web_extract`), stdlib only.

Output is paged (`max_chars` / `offset`) so a large page never floods the context window.
"""
from __future__ import annotations

import html
import json
import re
import urllib.error
import urllib.parse
import urllib.request
from html.parser import HTMLParser
from typing import Any, Dict, List

from ..tools.base import ToolDef, ToolError, check_abort, text_result
from . import Capability

DEFAULT_CHARS, MAX_CHARS, MAX_BYTES = 12_000, 60_000, 5_000_000
UA = "Mozilla/5.0 (compatible; omni-fetch/1.0)"
_SKIP = {"script", "style", "noscript", "svg", "template", "head", "iframe"}
_BLOCK = {"p", "div", "section", "article", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "pre", "blockquote",
          "ul", "ol", "table", "header", "footer", "nav", "main"}


class _Text(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.out: List[str] = []
        self.skip = 0
        self.title = ""
        self._in_title = False
        self._href: List[str] = []

    def handle_starttag(self, tag, attrs):
        if tag in _SKIP:
            self.skip += 1
            self._in_title = False
        if tag == "title":
            self._in_title = True
        if self.skip:
            return
        if tag in _BLOCK:
            self.out.append("\n")
        if tag in ("h1", "h2", "h3"):
            self.out.append("#" * int(tag[1]) + " ")
        elif tag == "li":
            self.out.append("- ")
        elif tag == "a":
            self._href.append(dict(attrs).get("href") or "")

    def handle_endtag(self, tag):
        if tag == "title":
            self._in_title = False
        if tag in _SKIP and self.skip:
            self.skip -= 1
            return
        if self.skip:
            return
        if tag == "a" and self._href:
            href = self._href.pop()
            if href.startswith(("http://", "https://")):
                self.out.append(f" ({href})")
        if tag in _BLOCK and tag not in ("li", "tr"):  # list items / rows stay on consecutive lines
            self.out.append("\n")

    def handle_data(self, data):
        if self._in_title:
            self.title += data
        elif not self.skip:
            self.out.append(data)


def html_to_text(doc: str) -> str:
    p = _Text()
    try:
        p.feed(doc)
    except Exception:
        return re.sub(r"<[^>]+>", " ", html.unescape(doc))
    text = "".join(p.out)
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    text = re.sub(r" ?\n ?", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    title = p.title.strip()
    return f"# {title}\n\n{text}" if title and not text.startswith(f"# {title}") else text


def fetch_text(url: str, raw: bool = False, timeout: float = 30.0) -> Dict[str, Any]:
    scheme = urllib.parse.urlparse(url).scheme.lower()
    if scheme not in ("http", "https"):
        raise ToolError("fetch only supports http:// and https:// URLs")
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "text/html,application/json,text/*;q=0.9,*/*;q=0.5"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            ctype = (r.headers.get("Content-Type") or "").lower()
            data = r.read(MAX_BYTES + 1)
            final = r.geturl()
            charset = r.headers.get_content_charset() or "utf-8"
    except urllib.error.HTTPError as e:
        raise ToolError(f"HTTP {e.code} {e.reason} for {url}") from None
    except (urllib.error.URLError, OSError, ValueError) as e:
        raise ToolError(f"could not fetch {url}: {getattr(e, 'reason', e)}") from None
    if not any(k in ctype for k in ("text", "json", "xml", "javascript", "html")) and ctype:
        raise ToolError(f"{url} is {ctype or 'binary'} ({len(data)} bytes); fetch reads text only")
    text = data[:MAX_BYTES].decode(charset, "replace")
    if "json" in ctype and not raw:
        try:
            text = json.dumps(json.loads(text), indent=1, ensure_ascii=False)
        except ValueError:
            pass
    elif ("html" in ctype or text.lstrip()[:15].lower().startswith(("<!doctype html", "<html"))) and not raw:
        text = html_to_text(text)
    return {"url": final, "contentType": ctype, "text": text, "truncatedBytes": len(data) > MAX_BYTES}


def _tools(session: Any) -> Dict[str, ToolDef]:
    def execute(_id, p, signal=None, on_update=None, ctx=None):
        check_abort(signal)
        url = str(p.get("url") or "").strip()
        if not url:
            raise ToolError("url is required")
        limit = max(500, min(int(p.get("max_chars") or DEFAULT_CHARS), MAX_CHARS))
        offset = max(0, int(p.get("offset") or 0))
        r = fetch_text(url, raw=bool(p.get("raw")))
        text = r["text"]
        chunk = text[offset:offset + limit]
        rest = len(text) - offset - len(chunk)
        note = f"\n\n[{rest} more characters. Call fetch again with offset={offset + len(chunk)} to continue.]" if rest > 0 else ""
        if not chunk and offset:
            raise ToolError(f"offset {offset} is beyond the end of the page ({len(text)} characters)")
        return text_result(chunk + note, {"url": r["url"], "contentType": r["contentType"], "totalChars": len(text),
                                          "offset": offset, "returned": len(chunk)})

    return {"fetch": ToolDef(
        name="fetch", label="Fetch",
        description="Fetch a URL and return its content as readable text (HTML is converted, JSON is pretty-printed). "
                    f"Returns up to max_chars (default {DEFAULT_CHARS}); use offset to page through long documents.",
        parameters={"type": "object", "properties": {
            "url": {"type": "string"}, "max_chars": {"type": "integer"}, "offset": {"type": "integer"},
            "raw": {"type": "boolean", "description": "return the body unconverted"}}, "required": ["url"]},
        execute=execute, prompt_snippet="Fetch a web page or API response as text", read_only=True)}


CAPABILITIES = [Capability("fetch", "Web fetch", "Read web pages and API responses as paged text.",
                           "Oh My Pi fetch, Hermes web_extract", tools=_tools)]
