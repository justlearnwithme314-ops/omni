"""`web-search` and `session-search`.

`web_search`      (Oh My Pi `web_search`, Hermes `web_search`) returns titles, URLs and snippets; read a result with
                  `fetch`. Backends, first one configured wins (or set OMNI_SEARCH_BACKEND):
                    tavily  TAVILY_API_KEY      brave  BRAVE_API_KEY      exa  EXA_API_KEY
                    duckduckgo                  no key; HTML scraping, best effort
`session_search`  (Hermes `session_search`) finds earlier conversations by text, across all projects, by scanning
                  the JSONL session files. Also available as `/search <words>`.
"""
from __future__ import annotations

import html
import json
import os
import re
import time
import urllib.parse
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from ..tools.base import ToolDef, ToolError, check_abort, text_result
from . import Capability

ENDPOINTS = {"tavily": "https://api.tavily.com/search", "brave": "https://api.search.brave.com/res/v1/web/search",
             "exa": "https://api.exa.ai/search", "duckduckgo": "https://html.duckduckgo.com/html/"}
KEYS = {"tavily": "TAVILY_API_KEY", "brave": "BRAVE_API_KEY", "exa": "EXA_API_KEY"}
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"


def _http(url: str, body: Optional[Dict[str, Any]] = None, headers: Optional[Dict[str, str]] = None, timeout: float = 25.0) -> str:
    import urllib.error
    import urllib.request
    data = json.dumps(body).encode() if body is not None else None
    h = {"User-Agent": UA, "Accept": "application/json, text/html;q=0.9"}
    if data is not None:
        h["Content-Type"] = "application/json"
    h.update(headers or {})
    try:
        with urllib.request.urlopen(urllib.request.Request(url, data=data, headers=h), timeout=timeout) as r:
            return r.read(2_000_000).decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        raise ToolError(f"search backend returned HTTP {e.code}: {e.read(300).decode('utf-8', 'replace')}") from None
    except (urllib.error.URLError, OSError) as e:
        raise ToolError(f"search backend unreachable: {getattr(e, 'reason', e)}") from None


def _tavily(q: str, n: int) -> List[Dict[str, str]]:
    d = json.loads(_http(ENDPOINTS["tavily"], {"api_key": os.environ["TAVILY_API_KEY"], "query": q, "max_results": n}))
    return [{"title": r.get("title", ""), "url": r.get("url", ""), "snippet": r.get("content", "")} for r in d.get("results") or []]


def _brave(q: str, n: int) -> List[Dict[str, str]]:
    url = ENDPOINTS["brave"] + "?" + urllib.parse.urlencode({"q": q, "count": n})
    d = json.loads(_http(url, headers={"X-Subscription-Token": os.environ["BRAVE_API_KEY"]}))
    return [{"title": r.get("title", ""), "url": r.get("url", ""), "snippet": re.sub(r"<[^>]+>", "", r.get("description", ""))}
            for r in (d.get("web") or {}).get("results") or []]


def _exa(q: str, n: int) -> List[Dict[str, str]]:
    d = json.loads(_http(ENDPOINTS["exa"], {"query": q, "numResults": n, "contents": {"highlights": True}},
                         headers={"x-api-key": os.environ["EXA_API_KEY"]}))
    return [{"title": r.get("title") or "", "url": r.get("url", ""),
             "snippet": " ".join(r.get("highlights") or []) or (r.get("text") or "")[:300]} for r in d.get("results") or []]


def parse_duckduckgo(page: str, n: int) -> List[Dict[str, str]]:
    out = []
    clean = lambda s: html.unescape(re.sub(r"<[^>]+>", "", s)).strip()  # noqa: E731
    snippets = re.findall(r'class="result__snippet"[^>]*>(.*?)</a>', page, re.S)
    for i, m in enumerate(re.finditer(r'class="result__a"[^>]*href="([^"]+)"[^>]*>(.*?)</a>', page, re.S)):
        url = html.unescape(m.group(1))
        if "uddg=" in url:  # DuckDuckGo wraps results in a redirect
            url = urllib.parse.unquote(urllib.parse.parse_qs(urllib.parse.urlparse(url).query).get("uddg", [url])[0])
        if url.startswith("//"):
            url = "https:" + url
        out.append({"title": clean(m.group(2)), "url": url, "snippet": clean(snippets[i]) if i < len(snippets) else ""})
        if len(out) >= n:
            break
    return out


def _duckduckgo(q: str, n: int) -> List[Dict[str, str]]:
    return parse_duckduckgo(_http(ENDPOINTS["duckduckgo"] + "?" + urllib.parse.urlencode({"q": q})), n)


BACKENDS: Dict[str, Callable[[str, int], List[Dict[str, str]]]] = {"tavily": _tavily, "brave": _brave, "exa": _exa,
                                                                  "duckduckgo": _duckduckgo}


def pick_backend() -> str:
    forced = (os.environ.get("OMNI_SEARCH_BACKEND") or "").lower()
    if forced in BACKENDS:
        return forced
    return next((name for name, key in KEYS.items() if os.environ.get(key)), "duckduckgo")


def _web_tools(session: Any) -> Dict[str, ToolDef]:
    def execute(_id, p, signal=None, on_update=None, ctx=None):
        check_abort(signal)
        query = str(p.get("query") or "").strip()
        if not query:
            raise ToolError("query is required")
        n = max(1, min(int(p.get("max_results") or 6), 15))
        backend = pick_backend()
        if backend in KEYS and not os.environ.get(KEYS[backend]):
            raise ToolError(f"{backend} needs {KEYS[backend]} to be set")
        results = [r for r in BACKENDS[backend](query, n) if r.get("url")][:n]
        if not results:
            return text_result(f"No results for: {query}", {"backend": backend, "results": 0})
        rows = [f"{i}. {r['title'] or r['url']}\n   {r['url']}" + (f"\n   {r['snippet'][:300]}" if r.get("snippet") else "")
                for i, r in enumerate(results, 1)]
        return text_result("\n".join(rows), {"backend": backend, "results": len(results)})

    return {"web_search": ToolDef(
        name="web_search", label="Web search",
        description="Search the web. Returns titles, URLs and short snippets; open a result with `fetch` when you "
                    "need its content. Use for current facts, documentation and error messages.",
        parameters={"type": "object", "properties": {"query": {"type": "string"}, "max_results": {"type": "integer"}},
                    "required": ["query"]},
        execute=execute, prompt_snippet="Search the web", read_only=True)}


# ------------------------------------------------------------------------------------- session search
def _text_of(message: Dict[str, Any]) -> str:
    c = message.get("content")
    if isinstance(c, str):
        return c
    return " ".join(b.get("text", "") for b in c or [] if isinstance(b, dict) and b.get("type") == "text")


def search_sessions(agent_dir: Path, query: str, limit: int = 8, max_files: int = 400, skip: Optional[str] = None) -> List[Dict[str, Any]]:
    from ..session import SessionManager
    words = [w.lower() for w in re.findall(r"\S+", query)]
    if not words:
        return []
    hits: List[Dict[str, Any]] = []
    for info in SessionManager.list_all_sessions(Path(agent_dir))[:max_files]:
        path = str(info.get("path") or "")
        if not path or path == skip:
            continue
        snippets, cwd, name, count = [], "", info.get("name") or "", 0
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                for line in f:
                    low = line.lower()
                    if '"type": "session"' in line[:40] or '"type":"session"' in line[:40]:
                        try:
                            cwd = json.loads(line).get("cwd", "")
                        except ValueError:
                            pass
                        continue
                    if not all(w in low for w in words):
                        continue
                    try:
                        e = json.loads(line)
                    except ValueError:
                        continue
                    m = e.get("message") if e.get("type") == "message" else None
                    if not m or m.get("role") not in ("user", "assistant"):
                        continue
                    text = _text_of(m)
                    tl = text.lower()
                    if not all(w in tl for w in words):
                        continue
                    count += 1
                    if len(snippets) < 2:
                        i = max(0, tl.find(words[0]) - 80)
                        snippets.append(f"{m['role']}: " + re.sub(r"\s+", " ", text[i:i + 240]).strip())
        except OSError:
            continue
        if count:
            hits.append({"path": path, "id": info.get("id"), "name": name, "cwd": cwd, "mtime": info.get("mtime"),
                         "matches": count, "snippets": snippets})
        if len(hits) >= limit:
            break
    return hits


def format_hits(hits: List[Dict[str, Any]], query: str) -> str:
    if not hits:
        return f"No earlier session mentions: {query}"
    rows = []
    for h in hits:
        when = time.strftime("%Y-%m-%d", time.localtime(h["mtime"])) if h.get("mtime") else "?"
        rows.append(f"{when}  {h.get('name') or (h.get('id') or '')[:8]}  ({h['matches']} match{'es' if h['matches'] != 1 else ''})"
                    f"  {h.get('cwd', '')}\n   " + "\n   ".join(h["snippets"]) + f"\n   resume: omni --session {h.get('id')}")
    return "\n".join(rows)


def _session_tools(session: Any) -> Dict[str, ToolDef]:
    def execute(_id, p, signal=None, on_update=None, ctx=None):
        query = str(p.get("query") or "").strip()
        if not query:
            raise ToolError("query is required")
        hits = search_sessions(session.agent_dir, query, max(1, min(int(p.get("limit") or 6), 20)),
                               skip=str(session.sessions.file or ""))
        return text_result(format_hits(hits, query), {"sessions": len(hits)})

    return {"session_search": ToolDef(
        name="session_search", label="Session search",
        description="Search earlier conversations (all projects) for the given words. Use it when the user refers "
                    "to something done before, or to check whether a problem was already solved.",
        parameters={"type": "object", "properties": {"query": {"type": "string"}, "limit": {"type": "integer"}},
                    "required": ["query"]},
        execute=execute, prompt_snippet="Search earlier conversations", read_only=True)}


def _session_commands(session: Any) -> Dict[str, Any]:
    def run(args: str) -> str:
        if not args.strip():
            return "Usage: /search <words>"
        return format_hits(search_sessions(session.agent_dir, args, 10, skip=str(session.sessions.file or "")), args)
    return {"search": ("Search earlier sessions by text", run)}


CAPABILITIES = [
    Capability("web-search", "Web search", "web_search through Tavily, Brave, Exa (with a key) or DuckDuckGo (no key).",
               "Oh My Pi web_search, Hermes web_search", tools=_web_tools),
    Capability("session-search", "Session search", "Find earlier conversations by text (tool + /search).",
               "Hermes session_search", tools=_session_tools, commands=_session_commands),
]
