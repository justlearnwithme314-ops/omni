"""`keywords` and `feeds`: two small conveniences.

`keywords` (Oh My Pi's magic keywords): a word in your message changes how that one request is handled.

    ultrathink     maximum reasoning effort for this request (restored afterwards) and an instruction to be thorough
    chillax        the opposite: answer briefly, no over-engineering, no unrequested extras
    orchestrate    split the work into independent parts and delegate them to parallel subagents
    readonly       investigate and report only; change nothing

Add your own in a profile: `"keywords": {"shipit": "Run the tests, then commit and push without asking."}`.
The keyword stays in your message; the instruction is appended to it.

`feeds` (OpenClaw's blogwatcher skill): the `feeds` tool follows RSS / Atom feeds and reports only what is new since
the last check. Pair it with a heartbeat job ("check my feeds; reply HEARTBEAT_OK if nothing is new") to be told
about new posts and releases. State is kept in `<agent-dir>/feeds.json`.
"""
from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..tools.base import ToolDef, ToolError, check_abort, text_result
from . import Capability

BUILTIN_KEYWORDS = {
    "ultrathink": "Take this slowly and carefully. Consider alternatives and edge cases, verify your assumptions against "
                  "the actual code or data before relying on them, and check your result before answering.",
    "chillax": "Keep this quick: the shortest correct answer or the smallest change that works. No extras, no refactoring, "
               "no long explanation.",
    "orchestrate": "Break this into independent parts and delegate them to subagents in parallel (the task tool with "
                   "`tasks`), then combine their reports. Do the coordination yourself; do not do the parts serially.",
    "readonly": "Investigate and report only. Do not create, edit or delete anything, and do not run commands that change state.",
}


def _keywords(session: Any) -> Dict[str, str]:
    table = dict(BUILTIN_KEYWORDS)
    extra = session.profile.get("keywords")
    if isinstance(extra, dict):
        table.update({str(k).lower(): str(v) for k, v in extra.items() if v})
    return table


def _on_prompt(session: Any, text: str) -> Optional[str]:
    if text.lstrip().startswith("/"):
        return None
    found = [(k, v) for k, v in _keywords(session).items() if re.search(rf"(?<![\w-]){re.escape(k)}(?![\w-])", text, re.I)]
    if not found:
        return None
    if any(k == "ultrathink" for k, _ in found) and session.model and session.model.get("reasoning"):
        st = getattr(session, "_keyword_state", None) or {}
        if "restore" not in st:
            st["restore"] = session.thinking_level
            session.set_thinking_level("max" if "max" in getattr(session, "THINKING_LEVELS", ["max"]) else "high")

            def on_event(ev: Dict[str, Any]) -> None:
                if ev.get("type") == "agent_end" and "restore" in st:
                    level = st.pop("restore")
                    unsub = st.pop("unsub", None)
                    if unsub:
                        unsub()
                    session.set_thinking_level(level)
            st["unsub"] = session.subscribe(on_event)
            session._keyword_state = st
    return "\n".join(f"[{k}] {v}" for k, v in found)


# ------------------------------------------------------------------------------------------- feeds
def _feeds_path(session: Any) -> Path:
    return Path(session.agent_dir) / "feeds.json"


def _load(session: Any) -> Dict[str, Dict[str, Any]]:
    try:
        d = json.loads(_feeds_path(session).read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def _save(session: Any, feeds: Dict[str, Dict[str, Any]]) -> None:
    _feeds_path(session).write_text(json.dumps(feeds, indent=2), encoding="utf-8")


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].lower()


def parse_feed(xml_text: str) -> List[Dict[str, str]]:
    """RSS 2.0 and Atom entries, newest first as the feed lists them: [{id, title, link, date}]."""
    try:
        root = ET.fromstring(xml_text.encode("utf-8") if isinstance(xml_text, str) else xml_text)
    except ET.ParseError as e:
        raise ToolError(f"not a valid feed: {e}") from None
    items = []
    for node in root.iter():
        if _local(node.tag) not in ("item", "entry"):
            continue
        f: Dict[str, str] = {}
        for child in node:
            name = _local(child.tag)
            if name == "link":
                href = child.attrib.get("href")
                if href and (child.attrib.get("rel") in (None, "alternate") or "link" not in f):
                    f["link"] = href
                elif child.text and child.text.strip():
                    f.setdefault("link", child.text.strip())
            elif name in ("title", "guid", "id", "pubdate", "updated", "published", "date") and child.text:
                f.setdefault({"guid": "id", "pubdate": "date", "updated": "date", "published": "date"}.get(name, name), child.text.strip())
        if f.get("title") or f.get("link"):
            f.setdefault("id", f.get("link") or f.get("title", ""))
            items.append({"id": f["id"], "title": re.sub(r"\s+", " ", f.get("title", "(untitled)")), "link": f.get("link", ""), "date": f.get("date", "")})
    return items


def fetch_feed(url: str) -> List[Dict[str, str]]:
    if not url.lower().startswith(("http://", "https://")):
        raise ToolError("feed URLs must be http(s)")
    req = urllib.request.Request(url, headers={"User-Agent": "omni-feeds/1.0", "Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml"})
    try:
        with urllib.request.urlopen(req, timeout=25) as r:
            data = r.read(3_000_000)
    except urllib.error.HTTPError as e:
        raise ToolError(f"HTTP {e.code} for {url}") from None
    except (urllib.error.URLError, OSError) as e:
        raise ToolError(f"could not fetch {url}: {getattr(e, 'reason', e)}") from None
    return parse_feed(data.decode("utf-8", "replace"))


def _feed_tools(session: Any) -> Dict[str, ToolDef]:
    def execute(_id, p, signal=None, on_update=None, ctx=None):
        check_abort(signal)
        action = str(p.get("action") or "check").lower()
        feeds = _load(session)
        if action == "add":
            url = str(p.get("url") or "").strip()
            name = (str(p.get("name") or "").strip() or re.sub(r"^https?://(www\.)?", "", url).split("/")[0]).lower()
            items = fetch_feed(url)
            feeds[name] = {"url": url, "seen": [i["id"] for i in items][:500]}
            _save(session, feeds)
            latest = "\n".join(f"  {i['title']} — {i['link']}" for i in items[:3])
            return text_result(f"Following {name} ({len(items)} existing items marked as seen). Latest:\n{latest}", {"name": name})
        if action == "remove":
            name = str(p.get("name") or "").lower()
            if name not in feeds:
                raise ToolError(f"no feed '{name}'. Following: {', '.join(feeds) or 'none'}")
            del feeds[name]
            _save(session, feeds)
            return text_result(f"Stopped following {name}.")
        if action == "list":
            return text_result("\n".join(f"{n}  {f['url']}" for n, f in feeds.items()) or "Not following any feeds.", {"count": len(feeds)})
        if action != "check":
            raise ToolError("action must be add, list, check or remove")
        names = [str(p["name"]).lower()] if p.get("name") else list(feeds)
        if not names:
            return text_result("Not following any feeds. Add one with action=add, url=...", {"new": 0})
        rows, total, errors = [], 0, []
        for name in names:
            f = feeds.get(name)
            if f is None:
                raise ToolError(f"no feed '{name}'")
            try:
                items = fetch_feed(f["url"])
            except ToolError as e:
                errors.append(f"{name}: {e}")
                continue
            seen = set(f.get("seen") or [])
            new = [i for i in items if i["id"] not in seen]
            if new:
                total += len(new)
                rows.append(f"{name} — {len(new)} new")
                rows += [f"  {i['title']}" + (f" ({i['date'][:16]})" if i["date"] else "") + f"\n    {i['link']}" for i in new[:10]]
                if len(new) > 10:
                    rows.append(f"  … and {len(new) - 10} more")
                f["seen"] = ([i["id"] for i in new] + list(f.get("seen") or []))[:500]
        _save(session, feeds)
        text = "\n".join(rows) if rows else "Nothing new."
        if errors:
            text += "\nCould not check: " + "; ".join(errors)
        return text_result(text, {"new": total, "errors": len(errors)})

    return {"feeds": ToolDef(
        name="feeds", label="Feeds",
        description="Follow RSS / Atom feeds and see only what is new since the last check. Actions: add(url, name?), "
                    "list, check(name?), remove(name).",
        parameters={"type": "object", "properties": {
            "action": {"type": "string", "enum": ["add", "list", "check", "remove"]}, "url": {"type": "string"}, "name": {"type": "string"}},
            "required": ["action"]},
        execute=execute, prompt_snippet="Follow RSS / Atom feeds")}


CAPABILITIES = [
    Capability("keywords", "Magic keywords", "ultrathink, chillax, orchestrate, readonly (and your own) change how one request "
               "is handled.", "Oh My Pi magic keywords", on_prompt=_on_prompt),
    Capability("feeds", "Feed watcher", "Follow RSS / Atom feeds and report only new items.", "OpenClaw blogwatcher", tools=_feed_tools),
]
