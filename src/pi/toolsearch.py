"""BM25 tool ranking shared by codemode's `searchTools()` and the `tool_search` tool (port of Pi's tool-search/tool.ts)."""
from __future__ import annotations

import math
import re
import unicodedata
from typing import Any, Callable, Dict, List, Optional

from .tools.base import ToolDef, ToolError

TOOL_SEARCH_TOOL_NAME = "tool_search"
DEFAULT_LIMIT = 8
STOP_WORDS = {"a", "an", "and", "are", "as", "at", "be", "by", "for", "from", "in", "is", "it", "of", "on", "or", "that",
              "the", "this", "to", "with"}
NAME_WEIGHT = 3.0
NAMESPACE_WEIGHT = 0.5

TOOL_SEARCH_DESCRIPTION = (
    "# Tool discovery\n\nSearches over deferred tool metadata with BM25 and exposes matching tools for the next model call.\n\n"
    "Some of the tools, such as tools of MCP servers, may not have been provided to you upfront, and you should use this tool "
    "(`tool_search`) to search for the required tools. For MCP tool discovery, always use `tool_search`.")


def _stem(t: str) -> str:
    if len(t) > 4 and t.endswith("ies"):
        return t[:-3] + "y"
    if len(t) > 4 and re.search(r"(ches|shes|sses|xes|zes)$", t):
        return t[:-2]
    if len(t) > 3 and t.endswith("s") and not t.endswith("ss"):
        return t[:-1]
    return t


def tokenize(text: str) -> List[str]:
    s = unicodedata.normalize("NFC", text or "")
    s = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", s)
    s = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1 \2", s)
    s = s.lower()
    s = "".join(ch if (unicodedata.category(ch)[0] in "LMN") else " " for ch in s)
    out = []
    for w in s.split():
        if w in STOP_WORDS:
            continue
        out.append(_stem(w) if re.fullmatch(r"[a-z0-9]+", w) else w)
    return out


def _schema_text(schema: Any, parts: List[str], depth: int = 0) -> None:
    if not isinstance(schema, dict) or depth > 8:
        return
    if isinstance(schema.get("description"), str):
        parts.append(schema["description"])
    for name, prop in (schema.get("properties") or {}).items() if isinstance(schema.get("properties"), dict) else []:
        parts.append(name)
        _schema_text(prop, parts, depth + 1)
    _schema_text(schema.get("items"), parts, depth + 1)
    for k in ("anyOf", "oneOf", "allOf"):
        for v in schema.get(k) or []:
            _schema_text(v, parts, depth + 1)
    for v in (schema.get("$defs") or schema.get("definitions") or {}).values():
        _schema_text(v, parts, depth + 1)


def document(tool: ToolDef, namespace: Optional[Dict[str, Any]] = None) -> Dict[str, str]:
    parts = [tool.description or ""]
    _schema_text(tool.parameters, parts)
    ns = " ".join([namespace.get("name", ""), namespace.get("description") or "", namespace.get("instructions") or ""]) if namespace else ""
    return {"name": tool.name, "text": " ".join(p for p in parts if p.strip()), "namespaceText": ns}


def _norm_name(n: str) -> str:
    return re.sub(r"[^a-z0-9_$]", "_", n.strip().lower())


def rank(query: str, docs: List[Dict[str, str]], limit: int = DEFAULT_LIMIT, k1: float = 1.2, b: float = 0.75) -> List[Dict[str, Any]]:
    qterms = list(dict.fromkeys(tokenize(query)))
    if not qterms or not docs or limit <= 0:
        return []
    idx = []
    for d in docs:
        counts: Dict[str, int] = {}
        for t in tokenize(d["text"]):
            counts[t] = counts.get(t, 0) + 1
        segs = d["name"].split("__")
        idx.append({"name": d["name"], "nameTerms": set(tokenize(d["name"])), "local": list(dict.fromkeys(tokenize(segs[-1]))),
                    "counts": counts, "len": sum(counts.values()), "ns": set(tokenize(d.get("namespaceText", "")))})
    avg = (sum(x["len"] for x in idx) / len(idx)) or 1
    idf = {}
    for t in qterms:
        f = sum(1 for x in idx if t in x["nameTerms"] or t in x["counts"] or t in x["ns"])
        idf[t] = math.log(1 + (len(idx) - f + 0.5) / (f + 0.5))
    bonus = sum(idf.values())
    exact_q = _norm_name(query)
    qset = set(qterms)
    matches = []
    for order, x in enumerate(idx):
        norm = k1 * (1 - b + b * x["len"] / avg)
        score, covered = 0.0, 0
        for t in qterms:
            c = x["counts"].get(t, 0)
            w = (NAME_WEIGHT if t in x["nameTerms"] else 0) + (NAMESPACE_WEIGHT if t in x["ns"] else 0) + (c * (k1 + 1)) / (c + norm)
            if w > 0:
                covered += 1
            score += idf[t] * w
        if score == 0:
            continue
        if x["local"] and all(t in qset for t in x["local"]):
            score += bonus
        n = _norm_name(x["name"])
        exact = n == exact_q or n.endswith("__" + exact_q)
        matches.append((not exact, -covered, -score, order, x["name"], score))
    matches.sort()
    return [{"name": m[4], "score": m[5]} for m in matches[:limit]]


def make_tool_search(session: Any) -> ToolDef:
    def execute(_id, params, signal=None, on_update=None, ctx=None):
        query = str(params.get("query") or "")
        if not query.strip():
            raise ToolError("query must not be empty")
        limit = params.get("limit", DEFAULT_LIMIT)
        if not isinstance(limit, int) or isinstance(limit, bool) or limit <= 0:
            if isinstance(limit, float) and limit.is_integer() and limit > 0:
                limit = int(limit)
            else:
                raise ToolError("limit must be a positive integer")
        if session.mcp:
            session.mcp.wait_all(30)
        active = set(session.active_tool_names)
        cands = [(t, ns) for name, (t, exp, ns) in session.indirect_tools().items()
                 if exp in ("codemode", "deferred") and name not in active]
        found = rank(query, [document(t, ns) for t, ns in cands], limit)
        by = {t.name: t for t, _ in cands}
        if found:
            session.set_active_tools(session.active_tool_names + [m["name"] for m in found], record=True)
        if not found:
            text = "No matching tools found."
        else:
            text = f"Loaded {len(found)} tool{'' if len(found) == 1 else 's'}. They are available from your next call:\n" + "\n".join(
                f"- {m['name']}: {(by[m['name']].description or '').strip().splitlines()[0] if (by[m['name']].description or '').strip() else ''}"
                for m in found)
        return {"content": [{"type": "text", "text": text}], "details": {"loaded": [m["name"] for m in found]}}

    td = ToolDef(name=TOOL_SEARCH_TOOL_NAME, label=TOOL_SEARCH_TOOL_NAME, description=TOOL_SEARCH_DESCRIPTION,
                 parameters={"type": "object", "properties": {
                     "query": {"type": "string", "description": "Search query for deferred tools."},
                     "limit": {"type": "number", "description": f"Maximum number of tools to return. Defaults to {DEFAULT_LIMIT}."}},
                     "required": ["query"]},
                 execute=execute, prompt_snippet="Search for tools that are not loaded yet and load the matches")
    td.__dict__["exposure"] = "model-only"
    return td
