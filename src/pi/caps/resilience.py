"""`context-trim` and `consensus`.

`context-trim` (OpenClaw's session pruning): long tool results are the bulk of a long conversation, and the model
rarely needs an old one in full. Before each request, results older than the most recent turns are shortened to
their beginning and end with a note saying how much was left out. Only what is sent to the model changes; the
session file keeps everything, so `/tree`, export and resume are unaffected.

    "context_trim": {"keep_turns": 8, "max_chars": 1500, "step": 4}      # profile options; all optional

`keep_turns` assistant turns stay untouched. The boundary moves in steps of `step` turns, not every turn, so the
provider's prompt cache keeps working between steps. Results at most `max_chars` long are never touched, and
neither are images. The agent can run a tool again if it needs a trimmed result in full.

`consensus` (Hermes' mixture of agents): the `consensus` tool sends one question to several models at the same
time and returns their answers side by side, so the working model can compare them and decide. Use it for hard
calls: a tricky bug diagnosis, a design choice, checking a risky conclusion. Models come from the profile:

    "consensus": {"models": ["anthropic/claude-opus-5-5", "openai/gpt-5.1"], "max_tokens": 1200}

Only models you are signed in to are asked. Each call costs one request per model; the models see the question
only, not the conversation, so the question must stand on its own.
"""
from __future__ import annotations

import threading
import time
from typing import Any, Dict, List, Optional

from ..tools.base import ToolDef, ToolError, check_abort, text_result
from . import Capability


def _opts(session: Any, key: str) -> Dict[str, Any]:
    o = session.profile.get(key)
    return o if isinstance(o, dict) else {}


def trim(messages: List[Dict[str, Any]], keep_turns: int = 8, max_chars: int = 1500, step: int = 4) -> List[Dict[str, Any]]:
    """A copy of `messages` with old, long tool results shortened. Returns the same list when nothing changes."""
    turns = [i for i, m in enumerate(messages) if m.get("role") == "assistant"]
    step = max(1, step)
    old_turns = ((len(turns) - keep_turns) // step) * step      # moves in steps, to keep the cached prefix stable
    if old_turns <= 0:
        return messages
    boundary = turns[old_turns]                                # messages before this index are "old"
    head, tail = max_chars * 2 // 3, max_chars // 3
    out, changed = list(messages), False
    for i in range(boundary):
        m = messages[i]
        if m.get("role") != "toolResult" or not isinstance(m.get("content"), list):
            continue
        blocks, touched = [], False
        for b in m["content"]:
            text = b.get("text") if isinstance(b, dict) and b.get("type") == "text" else None
            if text is not None and len(text) > max_chars:
                cut = len(text) - head - tail
                blocks.append(dict(b, text=f"{text[:head]}\n[... {cut:,} characters of this older result were trimmed to save context; "
                                           f"run the tool again if you need them ...]\n{text[-tail:]}"))
                touched = True
            else:
                blocks.append(b)
        if touched:
            out[i] = dict(m, content=blocks)
            changed = True
    return out if changed else messages


def _trim_context(session: Any, messages: List[Dict[str, Any]]) -> Optional[List[Dict[str, Any]]]:
    o = _opts(session, "context_trim")
    out = trim(messages, int(o.get("keep_turns") or 8), max(200, int(o.get("max_chars") or 1500)), int(o.get("step") or 4))
    return None if out is messages else out


# ------------------------------------------------------------------------------------------- consensus
MAX_MODELS = 5


def _models(session: Any, names: List[str]) -> List[Dict[str, Any]]:
    found, seen = [], set()
    for name in names:
        m = session.registry.resolve(str(name))
        if m is None:
            continue
        key = f"{m['provider']}/{m['id']}"
        if key not in seen and session.auth.get_api_key(m["provider"]):
            seen.add(key)
            found.append(m)
    return found[:MAX_MODELS]


def _consensus_tools(session: Any) -> Dict[str, ToolDef]:
    def execute(_id, p, signal=None, on_update=None, ctx=None):
        from ..ai.stream import complete
        check_abort(signal)
        question = str(p.get("question") or "").strip()
        if not question:
            raise ToolError("question is required, and must stand on its own: the other models do not see this conversation")
        o = _opts(session, "consensus")
        names = [str(n) for n in (p.get("models") or o.get("models") or [])]
        if not names:
            raise ToolError("No models to ask. Set them in the profile: \"consensus\": {\"models\": [\"provider/model-id\", ...]}, "
                            "or pass `models`.")
        models = _models(session, names)
        if not models:
            raise ToolError(f"None of these models can be used (unknown, or not signed in): {', '.join(names)}")
        max_tokens = max(100, min(int(o.get("max_tokens") or 1200), 4000))
        system = ("You are one of several experts asked the same question independently. Give your own best answer with your "
                  "reasoning, state how confident you are, and say what would change your mind. Be concise.")
        answers: List[Optional[Dict[str, Any]]] = [None] * len(models)

        def ask(i: int, m: Dict[str, Any]) -> None:
            started = time.time()
            try:
                msg = complete(m, {"systemPrompt": system, "tools": [],
                                   "messages": [{"role": "user", "content": question, "timestamp": int(started * 1000)}]},
                               {"apiKey": session.auth.get_api_key(m["provider"]), "maxTokens": max_tokens, "signal": signal})
                text = "".join(b.get("text", "") for b in msg.get("content") or [] if b.get("type") == "text").strip()
                error = msg.get("errorMessage") if msg.get("stopReason") == "error" else None
                cost = ((msg.get("usage") or {}).get("cost") or {}).get("total") or 0
            except Exception as e:
                text, error, cost = "", f"{type(e).__name__}: {e}", 0
            answers[i] = {"text": text, "error": error, "seconds": time.time() - started, "cost": cost}

        threads = [threading.Thread(target=ask, args=(i, m), daemon=True) for i, m in enumerate(models)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(180)
        parts, ok = [], 0
        for m, a in zip(models, answers):
            label = f"{m['provider']}/{m['id']}"
            if a is None:
                parts.append(f"## {label}\n(no answer within 3 minutes)")
            elif a["error"] or not a["text"]:
                parts.append(f"## {label}\n(failed: {a['error'] or 'empty answer'})")
            else:
                ok += 1
                parts.append(f"## {label}  ({a['seconds']:.0f}s)\n{a['text']}")
        if not ok:
            raise ToolError("No model answered.\n" + "\n".join(parts))
        skipped = len(names) - len(models)
        note = f"\n\n({skipped} listed model(s) were skipped: unknown, duplicate or not signed in.)" if skipped > 0 else ""
        return text_result("\n\n".join(parts) + note + "\n\nCompare these answers: where they agree, where they differ and why, then decide.",
                           {"asked": len(models), "answered": ok, "cost": sum((a or {}).get("cost") or 0 for a in answers)})

    return {"consensus": ToolDef(
        name="consensus", label="Consensus",
        description="Ask several other models the same question at once and get their answers side by side. For hard calls "
                    "only (a difficult diagnosis, a design decision, checking a risky conclusion): it costs one request per "
                    "model. The question must be self-contained; include the relevant code or facts in it.",
        parameters={"type": "object", "properties": {
            "question": {"type": "string"},
            "models": {"type": "array", "items": {"type": "string"}, "description": "provider/model-id; default: the profile's list"}},
            "required": ["question"]},
        execute=execute, prompt_snippet="Get a second opinion from several models")}


CAPABILITIES = [
    Capability("context-trim", "Context trimming", "Shorten old, long tool results in what is sent to the model; the session "
               "file keeps everything.", "OpenClaw session pruning", context=_trim_context),
    Capability("consensus", "Consensus", "Ask several models the same question in parallel and compare their answers.",
               "Hermes mixture of agents", tools=_consensus_tools),
]
