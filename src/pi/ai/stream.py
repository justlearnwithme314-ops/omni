"""Streaming provider adapters (pi-ai `stream()`), stdlib only.

`stream(model, context, options)` yields events and finally returns the assistant message:

    {"type": "start"}
    {"type": "text_delta", "delta": str}
    {"type": "thinking_delta", "delta": str}
    {"type": "toolcall_start", "id", "name"}
    {"type": "toolcall_end", "toolCall": {...}}
    {"type": "done", "message": AssistantMessage}        # stopReason: stop | length | toolUse | error | aborted

`context` = {"systemPrompt": str, "messages": [LLM messages: user | assistant | toolResult], "tools": [ToolDecl]}
"""
from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request
import uuid
from typing import Any, Callable, Dict, Iterator, List, Optional

from .models import calculate_cost, empty_usage

THINKING_BUDGETS = {"minimal": 1024, "low": 4096, "medium": 10240, "high": 24576, "xhigh": 32768, "max": 63999}
OPENAI_EFFORT = {"minimal": "minimal", "low": "low", "medium": "medium", "high": "high", "xhigh": "high", "max": "high"}
ANTIGRAVITY_SUFFIX = {"minimal": "low", "low": "low", "medium": "medium", "high": "high", "xhigh": "high", "max": "high"}
ANTIGRAVITY_SUFFIXED = ("gemini-3.8-flash", "gemini-3.7-flash", "gemini-4-argon", "claude-sonnet-5-5", "claude-opus-5-5")
ANTIGRAVITY_RENAMES = {"gemini-3-flash": "gemini-3-flash-preview", "gemini-3-pro": "gemini-3-pro-preview"}
USER_AGENT = "omni-pi/0.78"


class AbortSignal:
    def __init__(self) -> None:
        self.aborted = False
        self._listeners: List[Callable[[], None]] = []
        self._lock = threading.Lock()

    def abort(self) -> None:
        with self._lock:
            if self.aborted:
                return
            self.aborted = True
            listeners = list(self._listeners)
        for fn in listeners:
            try:
                fn()
            except Exception:
                pass

    def add_listener(self, fn: Callable[[], None]) -> Callable[[], None]:
        with self._lock:
            if self.aborted:
                fn()
                return lambda: None
            self._listeners.append(fn)
        def remove() -> None:
            with self._lock:
                if fn in self._listeners:
                    self._listeners.remove(fn)
        return remove


class ProviderError(Exception):
    def __init__(self, message: str, status: Optional[int] = None, retry_after: Optional[float] = None, body: str = ""):
        super().__init__(message)
        self.status = status
        self.retry_after = retry_after
        self.body = body

    @property
    def transient(self) -> bool:
        if self.status is None:
            return True  # network errors
        return self.status in (408, 409, 425, 429, 500, 502, 503, 504, 529)


class AbortedError(Exception):
    pass


# ---------------------------------------------------------------------------------------------------------
# HTTP + SSE
# ---------------------------------------------------------------------------------------------------------

def _sse(url: str, headers: Dict[str, str], body: Dict[str, Any], signal: Optional[AbortSignal],
         timeout: float = 300.0, on_response: Optional[Callable[[int, Dict[str, str]], None]] = None) -> Iterator[Dict[str, Any]]:
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST",
                                 headers={"Content-Type": "application/json", "Accept": "text/event-stream",
                                          "User-Agent": USER_AGENT, **headers})
    if signal and signal.aborted:
        raise AbortedError()
    try:
        resp = urllib.request.urlopen(req, timeout=timeout)
    except urllib.error.HTTPError as e:
        text = e.read().decode("utf-8", "replace")
        ra = e.headers.get("retry-after") if e.headers else None
        if on_response:
            on_response(e.code, dict(e.headers or {}))
        raise ProviderError(f"{e.code} {_error_text(text) or e.reason}", status=e.code,
                            retry_after=float(ra) if ra and ra.replace(".", "", 1).isdigit() else None, body=text)
    except urllib.error.URLError as e:
        raise ProviderError(f"Connection error: {e.reason}")
    if on_response:
        on_response(resp.status, dict(resp.headers))
    remove = signal.add_listener(resp.close) if signal else (lambda: None)
    try:
        event_data: List[str] = []
        for raw in resp:
            if signal and signal.aborted:
                raise AbortedError()
            line = raw.decode("utf-8", "replace").rstrip("\r\n")
            if line.startswith("data:"):
                event_data.append(line[5:].lstrip())
            elif line == "" and event_data:
                payload = "\n".join(event_data)
                event_data = []
                if payload == "[DONE]":
                    return
                try:
                    yield json.loads(payload)
                except json.JSONDecodeError:
                    continue
        if event_data:
            payload = "\n".join(event_data)
            if payload != "[DONE]":
                try:
                    yield json.loads(payload)
                except json.JSONDecodeError:
                    pass
    except (OSError, ValueError, AttributeError) as e:
        if signal and signal.aborted:
            raise AbortedError()
        raise ProviderError(f"Stream interrupted: {e}")
    finally:
        remove()
        try:
            resp.close()
        except Exception:
            pass


def _error_text(text: str) -> str:
    try:
        d = json.loads(text)
        if isinstance(d, list) and d:
            d = d[0]
        err = d.get("error", d)
        if isinstance(err, dict):
            return str(err.get("message") or err)
        return str(err)
    except Exception:
        return text.strip()[:500]


def _text_of(content: Any) -> str:
    if isinstance(content, str):
        return content
    return "".join(b.get("text", "") for b in content or [] if b.get("type") == "text")


def _blocks(content: Any) -> List[Dict[str, Any]]:
    return [{"type": "text", "text": content}] if isinstance(content, str) else list(content or [])


class _Builder:
    """Accumulates streamed blocks into a Pi AssistantMessage."""

    def __init__(self, model: Dict[str, Any]):
        self.model = model
        self.content: List[Dict[str, Any]] = []
        self.usage = empty_usage()
        self.stop_reason = "stop"
        self.error: Optional[str] = None

    def text(self, delta: str) -> Dict[str, Any]:
        if not self.content or self.content[-1]["type"] != "text":
            self.content.append({"type": "text", "text": ""})
        self.content[-1]["text"] += delta
        return {"type": "text_delta", "delta": delta}

    def thinking(self, delta: str, signature: Optional[str] = None) -> Dict[str, Any]:
        if not self.content or self.content[-1]["type"] != "thinking":
            self.content.append({"type": "thinking", "thinking": ""})
        self.content[-1]["thinking"] += delta
        if signature:
            self.content[-1]["thinkingSignature"] = signature
        return {"type": "thinking_delta", "delta": delta}

    def tool(self, tc_id: str, name: str, args: Any, extra: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        if isinstance(args, str):
            try:
                args = json.loads(args) if args.strip() else {}
            except json.JSONDecodeError:
                args = {"__raw": args}
        call = {"type": "toolCall", "id": tc_id or f"call_{uuid.uuid4().hex[:12]}", "name": name, "arguments": args or {}}
        if extra:
            call.update(extra)
        self.content.append(call)
        return {"type": "toolcall_end", "toolCall": call}

    def message(self) -> Dict[str, Any]:
        u = self.usage
        u["totalTokens"] = u["input"] + u["output"] + u["cacheRead"] + u["cacheWrite"]
        u["cost"] = calculate_cost(self.model, u)
        if any(b["type"] == "toolCall" for b in self.content) and self.stop_reason == "stop":
            self.stop_reason = "toolUse"
        msg = {"role": "assistant", "content": self.content, "api": self.model.get("api"),
               "provider": self.model.get("provider"), "model": self.model.get("id"), "usage": u,
               "stopReason": self.stop_reason, "timestamp": int(time.time() * 1000)}
        if self.error:
            msg["errorMessage"] = self.error
        return msg


# ---------------------------------------------------------------------------------------------------------
# Anthropic Messages
# ---------------------------------------------------------------------------------------------------------

def _anthropic_messages(messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for m in messages:
        if m["role"] == "user":
            blocks = []
            for b in _blocks(m["content"]):
                if b["type"] == "text" and b.get("text"):
                    blocks.append({"type": "text", "text": b["text"]})
                elif b["type"] == "image":
                    blocks.append({"type": "image", "source": {"type": "base64", "media_type": b["mimeType"], "data": b["data"]}})
            if blocks:
                out.append({"role": "user", "content": blocks})
        elif m["role"] == "assistant":
            blocks = []
            for b in m["content"]:
                if b["type"] == "text" and b.get("text"):
                    blocks.append({"type": "text", "text": b["text"]})
                elif b["type"] == "thinking" and b.get("thinkingSignature") and m.get("provider") == "anthropic":
                    blocks.append({"type": "thinking", "thinking": b["thinking"], "signature": b["thinkingSignature"]})
                elif b["type"] == "toolCall":
                    blocks.append({"type": "tool_use", "id": b["id"], "name": b["name"], "input": b.get("arguments") or {}})
            if blocks:
                out.append({"role": "assistant", "content": blocks})
        elif m["role"] == "toolResult":
            content = []
            for b in _blocks(m["content"]):
                if b["type"] == "text":
                    content.append({"type": "text", "text": b.get("text", "")})
                elif b["type"] == "image":
                    content.append({"type": "image", "source": {"type": "base64", "media_type": b["mimeType"], "data": b["data"]}})
            block = {"type": "tool_result", "tool_use_id": m["toolCallId"], "content": content or [{"type": "text", "text": ""}],
                     "is_error": bool(m.get("isError"))}
            if out and out[-1]["role"] == "user" and all(x.get("type") == "tool_result" for x in out[-1]["content"]):
                out[-1]["content"].append(block)
            else:
                out.append({"role": "user", "content": [block]})
    # prompt caching: mark the last user block
    for msg in reversed(out):
        if msg["role"] == "user" and msg["content"]:
            msg["content"][-1]["cache_control"] = {"type": "ephemeral"}
            break
    return out


def stream_anthropic(model, context, options) -> Iterator[Dict[str, Any]]:
    key = options.get("apiKey") or ""
    headers = {"anthropic-version": "2023-06-01", **(options.get("headers") or {})}
    if key.startswith("sk-ant-oat"):
        headers["Authorization"] = f"Bearer {key}"
        headers["anthropic-beta"] = "oauth-2025-04-20,interleaved-thinking-2025-05-14"
    else:
        headers["x-api-key"] = key
        headers["anthropic-beta"] = "interleaved-thinking-2025-05-14"
    level = options.get("reasoning")
    max_tokens = int(options.get("maxTokens") or model.get("maxTokens") or 16_384)
    body: Dict[str, Any] = {"model": model["id"], "max_tokens": max_tokens, "stream": True,
                            "messages": _anthropic_messages(context["messages"])}
    system = context.get("systemPrompt")
    if key.startswith("sk-ant-oat"):
        system_blocks = [{"type": "text", "text": "You are Claude Code, Anthropic's official CLI for Claude."}]
    else:
        system_blocks = []
    if system:
        system_blocks.append({"type": "text", "text": system, "cache_control": {"type": "ephemeral"}})
    if system_blocks:
        body["system"] = system_blocks
    if context.get("tools"):
        body["tools"] = [{"name": t["name"], "description": t.get("description", ""), "input_schema": t.get("parameters") or {"type": "object"}}
                         for t in context["tools"]]
    if level and level != "off" and model.get("reasoning"):
        budget = min(THINKING_BUDGETS.get(level, 10240), max_tokens - 1024)
        if budget >= 1024:
            body["thinking"] = {"type": "enabled", "budget_tokens": budget}
    url = model.get("baseUrl", "https://api.anthropic.com").rstrip("/")
    url = url + ("/messages" if url.endswith("/v1") else "/v1/messages")
    b = _Builder(model)
    yield {"type": "start"}
    tools: Dict[int, Dict[str, Any]] = {}
    for ev in _sse(url, headers, body, options.get("signal"), on_response=options.get("onResponse")):
        t = ev.get("type")
        if t == "message_start":
            u = ev.get("message", {}).get("usage", {})
            b.usage["input"] = u.get("input_tokens", 0)
            b.usage["cacheRead"] = u.get("cache_read_input_tokens", 0) or 0
            b.usage["cacheWrite"] = u.get("cache_creation_input_tokens", 0) or 0
        elif t == "content_block_start":
            cb = ev.get("content_block", {})
            if cb.get("type") == "tool_use":
                tools[ev["index"]] = {"id": cb.get("id"), "name": cb.get("name"), "json": ""}
                yield {"type": "toolcall_start", "id": cb.get("id"), "name": cb.get("name")}
            elif cb.get("type") == "thinking":
                b.content.append({"type": "thinking", "thinking": ""})
        elif t == "content_block_delta":
            d = ev.get("delta", {})
            if d.get("type") == "text_delta":
                yield b.text(d.get("text", ""))
            elif d.get("type") == "thinking_delta":
                yield b.thinking(d.get("thinking", ""))
            elif d.get("type") == "signature_delta":
                if b.content and b.content[-1]["type"] == "thinking":
                    b.content[-1]["thinkingSignature"] = b.content[-1].get("thinkingSignature", "") + d.get("signature", "")
            elif d.get("type") == "input_json_delta" and ev.get("index") in tools:
                tools[ev["index"]]["json"] += d.get("partial_json", "")
        elif t == "content_block_stop" and ev.get("index") in tools:
            tc = tools.pop(ev["index"])
            yield b.tool(tc["id"], tc["name"], tc["json"])
        elif t == "message_delta":
            sr = ev.get("delta", {}).get("stop_reason")
            b.stop_reason = {"end_turn": "stop", "tool_use": "toolUse", "max_tokens": "length", "stop_sequence": "stop",
                             "pause_turn": "stop", "refusal": "error"}.get(sr, b.stop_reason)
            b.usage["output"] = ev.get("usage", {}).get("output_tokens", b.usage["output"])
        elif t == "error":
            raise ProviderError(str(ev.get("error", {}).get("message", ev)), status=529 if "overloaded" in str(ev) else None)
    yield {"type": "done", "message": b.message()}


# ---------------------------------------------------------------------------------------------------------
# OpenAI-compatible chat completions
# ---------------------------------------------------------------------------------------------------------

def _openai_messages(system: Optional[str], messages: List[Dict[str, Any]], supports_images: bool) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    if system:
        out.append({"role": "system", "content": system})
    for m in messages:
        if m["role"] == "user":
            blocks = _blocks(m["content"])
            if all(b["type"] == "text" for b in blocks):
                out.append({"role": "user", "content": _text_of(blocks)})
            else:
                parts = []
                for b in blocks:
                    if b["type"] == "text":
                        parts.append({"type": "text", "text": b["text"]})
                    elif b["type"] == "image" and supports_images:
                        parts.append({"type": "image_url", "image_url": {"url": f"data:{b['mimeType']};base64,{b['data']}"}})
                out.append({"role": "user", "content": parts})
        elif m["role"] == "assistant":
            msg: Dict[str, Any] = {"role": "assistant", "content": _text_of(m["content"]) or None}
            calls = [b for b in m["content"] if b["type"] == "toolCall"]
            if calls:
                msg["tool_calls"] = [{"id": c["id"], "type": "function",
                                      "function": {"name": c["name"], "arguments": json.dumps(c.get("arguments") or {})}} for c in calls]
            if msg["content"] is None and not calls:
                continue
            out.append(msg)
        elif m["role"] == "toolResult":
            text = _text_of(m["content"]) or ("(image)" if m["content"] else "")
            out.append({"role": "tool", "tool_call_id": m["toolCallId"], "content": text})
            imgs = [b for b in _blocks(m["content"]) if b["type"] == "image"]
            if imgs and supports_images:
                out.append({"role": "user", "content": [{"type": "text", "text": "Attached image(s) from tool result:"}] +
                            [{"type": "image_url", "image_url": {"url": f"data:{b['mimeType']};base64,{b['data']}"}} for b in imgs]})
    return out


def stream_openai(model, context, options) -> Iterator[Dict[str, Any]]:
    key = options.get("apiKey") or ""
    headers = {**({"Authorization": f"Bearer {key}"} if key and key != "local" else {}), **(model.get("headers") or {}),
               **(options.get("headers") or {})}
    body: Dict[str, Any] = {"model": model["id"], "stream": True, "stream_options": {"include_usage": True},
                            "messages": _openai_messages(context.get("systemPrompt"), context["messages"],
                                                         "image" in (model.get("input") or []))}
    if context.get("tools"):
        body["tools"] = [{"type": "function", "function": {"name": t["name"], "description": t.get("description", ""),
                                                           "parameters": t.get("parameters") or {"type": "object", "properties": {}}}}
                         for t in context["tools"]]
    level = options.get("reasoning")
    if model["provider"] == "openai":
        body["max_completion_tokens"] = int(options.get("maxTokens") or model.get("maxTokens") or 16_384)
        if level and level != "off" and model.get("reasoning"):
            body["reasoning_effort"] = OPENAI_EFFORT.get(level, "medium")
    elif options.get("maxTokens"):
        body["max_tokens"] = int(options["maxTokens"])
    if model["provider"] == "openrouter" and level and level != "off":
        body["reasoning"] = {"effort": OPENAI_EFFORT.get(level, "medium")}
    url = model.get("baseUrl", "").rstrip("/") + "/chat/completions"
    b = _Builder(model)
    yield {"type": "start"}
    pending: Dict[int, Dict[str, Any]] = {}
    for ev in _sse(url, headers, body, options.get("signal"), on_response=options.get("onResponse")):
        if ev.get("error"):
            raise ProviderError(_error_text(json.dumps(ev)))
        if ev.get("usage"):
            u = ev["usage"]
            cached = (u.get("prompt_tokens_details") or {}).get("cached_tokens", 0) or 0
            b.usage["input"] = (u.get("prompt_tokens") or 0) - cached
            b.usage["cacheRead"] = cached
            b.usage["output"] = u.get("completion_tokens") or 0
        for ch in ev.get("choices") or []:
            d = ch.get("delta") or {}
            if d.get("reasoning_content") or d.get("reasoning"):
                yield b.thinking(d.get("reasoning_content") or d.get("reasoning") or "")
            if d.get("content"):
                yield b.text(d["content"])
            for tc in d.get("tool_calls") or []:
                idx = tc.get("index", 0)
                cur = pending.setdefault(idx, {"id": None, "name": "", "args": ""})
                if tc.get("id"):
                    cur["id"] = tc["id"]
                fn = tc.get("function") or {}
                if fn.get("name"):
                    if not cur["name"]:
                        yield {"type": "toolcall_start", "id": cur["id"], "name": fn["name"]}
                    cur["name"] += fn["name"] if not cur["name"] else ""
                if fn.get("arguments"):
                    cur["args"] += fn["arguments"]
            fr = ch.get("finish_reason")
            if fr:
                b.stop_reason = {"stop": "stop", "length": "length", "tool_calls": "toolUse", "function_call": "toolUse",
                                 "content_filter": "error"}.get(fr, "stop")
    for idx in sorted(pending):
        tc = pending[idx]
        yield b.tool(tc["id"], tc["name"], tc["args"])
    yield {"type": "done", "message": b.message()}


# ---------------------------------------------------------------------------------------------------------
# Google Gemini (generative language API) + Antigravity CloudCode wrapper
# ---------------------------------------------------------------------------------------------------------

_GEMINI_KEYS = {"type", "description", "properties", "required", "items", "enum", "nullable", "minimum", "maximum",
                "minItems", "maxItems", "anyOf", "format", "minLength", "maxLength", "pattern", "default", "title"}


def gemini_schema(s: Any) -> Any:
    if isinstance(s, list):
        return [gemini_schema(x) for x in s]
    if not isinstance(s, dict):
        return s
    s = dict(s)
    if "const" in s:
        s["enum"] = [s.pop("const")]
    t = s.get("type")
    if isinstance(t, list):  # ["string","null"] -> string + nullable
        non_null = [x for x in t if x != "null"]
        s["type"] = non_null[0] if non_null else "string"
        if "null" in t:
            s["nullable"] = True
    if "anyOf" in s and all(isinstance(x, dict) and "const" in x for x in s["anyOf"]):
        s = {"type": "string", "enum": [x["const"] for x in s["anyOf"]], **({"description": s["description"]} if "description" in s else {})}
    out = {}
    for k, v in s.items():
        if k not in _GEMINI_KEYS:
            continue
        if k == "properties":
            out[k] = {pk: gemini_schema(pv) for pk, pv in v.items()}
        elif k in ("items",):
            out[k] = gemini_schema(v)
        elif k == "anyOf":
            out[k] = [gemini_schema(x) for x in v]
        elif k == "format" and v not in ("enum", "date-time"):
            continue
        else:
            out[k] = v
    if out.get("type") == "object" and "properties" not in out:
        out["properties"] = {}
    return out


def _gemini_contents(messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    names: Dict[str, str] = {}
    for m in messages:
        if m["role"] == "user":
            parts = []
            for b in _blocks(m["content"]):
                if b["type"] == "text" and b.get("text"):
                    parts.append({"text": b["text"]})
                elif b["type"] == "image":
                    parts.append({"inlineData": {"mimeType": b["mimeType"], "data": b["data"]}})
            if parts:
                out.append({"role": "user", "parts": parts})
        elif m["role"] == "assistant":
            parts = []
            for b in m["content"]:
                if b["type"] == "text" and b.get("text"):
                    parts.append({"text": b["text"]})
                elif b["type"] == "toolCall":
                    names[b["id"]] = b["name"]
                    parts.append({"functionCall": {"name": b["name"], "args": b.get("arguments") or {}},
                                  "thoughtSignature": b.get("thoughtSignature") or "skip_thought_signature_validator"})
            if parts:
                out.append({"role": "model", "parts": parts})
        elif m["role"] == "toolResult":
            text = _text_of(m["content"])
            part = {"functionResponse": {"name": m.get("toolName") or names.get(m["toolCallId"], "tool"),
                                         "response": {"error": text} if m.get("isError") else {"output": text}}}
            parts = [part] + [{"inlineData": {"mimeType": b["mimeType"], "data": b["data"]}}
                              for b in _blocks(m["content"]) if b["type"] == "image"]
            if out and out[-1]["role"] == "user" and all("functionResponse" in p or "inlineData" in p for p in out[-1]["parts"]):
                out[-1]["parts"].extend(parts)
            else:
                out.append({"role": "user", "parts": parts})
    return out


def _gemini_request(model, context, options) -> Dict[str, Any]:
    req: Dict[str, Any] = {"contents": _gemini_contents(context["messages"])}
    if context.get("systemPrompt"):
        req["systemInstruction"] = {"parts": [{"text": context["systemPrompt"]}]}
    if context.get("tools"):
        req["tools"] = [{"functionDeclarations": [{"name": t["name"], "description": t.get("description", ""),
                                                    "parameters": gemini_schema(t.get("parameters") or {"type": "object"})}
                                                   for t in context["tools"]]}]
    gen: Dict[str, Any] = {"maxOutputTokens": int(options.get("maxTokens") or model.get("maxTokens") or 32_768)}
    level = options.get("reasoning")
    if model.get("reasoning"):
        if level and level != "off":
            gen["thinkingConfig"] = {"includeThoughts": True, "thinkingBudget": THINKING_BUDGETS.get(level, 10240)}
        elif level == "off" and "flash" in model["id"]:
            gen["thinkingConfig"] = {"thinkingBudget": 0}
    req["generationConfig"] = gen
    return req


def _consume_gemini(events: Iterator[Dict[str, Any]], b: _Builder, unwrap: bool) -> Iterator[Dict[str, Any]]:
    for ev in events:
        if unwrap and "response" in ev:
            ev = ev["response"]
        if ev.get("error"):
            raise ProviderError(_error_text(json.dumps(ev)))
        um = ev.get("usageMetadata")
        if um:
            cached = um.get("cachedContentTokenCount", 0) or 0
            b.usage["input"] = (um.get("promptTokenCount", 0) or 0) - cached
            b.usage["cacheRead"] = cached
            b.usage["output"] = (um.get("candidatesTokenCount", 0) or 0) + (um.get("thoughtsTokenCount", 0) or 0)
        for cand in ev.get("candidates") or []:
            for p in (cand.get("content") or {}).get("parts") or []:
                if "functionCall" in p:
                    fc = p["functionCall"]
                    extra = {"thoughtSignature": p["thoughtSignature"]} if p.get("thoughtSignature") else None
                    yield {"type": "toolcall_start", "id": None, "name": fc.get("name")}
                    yield b.tool(fc.get("id") or f"call_{uuid.uuid4().hex[:12]}", fc.get("name", ""), fc.get("args") or {}, extra)
                elif p.get("thought") and p.get("text"):
                    yield b.thinking(p["text"], p.get("thoughtSignature"))
                elif "text" in p and p["text"]:
                    yield b.text(p["text"])
            fr = cand.get("finishReason")
            if fr:
                b.stop_reason = {"STOP": "stop", "MAX_TOKENS": "length"}.get(fr, "error" if fr in ("SAFETY", "RECITATION") else "stop")


def stream_google(model, context, options) -> Iterator[Dict[str, Any]]:
    key = options.get("apiKey") or ""
    base = model.get("baseUrl", "https://generativelanguage.googleapis.com/v1beta").rstrip("/")
    headers = dict(options.get("headers") or {})
    if key.startswith("ya29."):
        headers["Authorization"] = f"Bearer {key}"
        url = f"{base}/models/{model['id']}:streamGenerateContent?alt=sse"
    else:
        headers["x-goog-api-key"] = key
        url = f"{base}/models/{model['id']}:streamGenerateContent?alt=sse"
    b = _Builder(model)
    yield {"type": "start"}
    yield from _consume_gemini(_sse(url, headers, _gemini_request(model, context, options), options.get("signal"),
                                    on_response=options.get("onResponse")), b, unwrap=False)
    yield {"type": "done", "message": b.message()}


def antigravity_wire_model(model_id: str, level: Optional[str]) -> str:
    mid = ANTIGRAVITY_RENAMES.get(model_id, model_id)
    if mid in ANTIGRAVITY_SUFFIXED:
        mid = f"{mid}-{ANTIGRAVITY_SUFFIX.get(level or 'medium', 'medium') if level and level != 'off' else 'low'}"
    return mid


def stream_antigravity(model, context, options) -> Iterator[Dict[str, Any]]:
    import os
    key = options.get("apiKey") or ""
    base = (model.get("baseUrl") or "https://daily-cloudcode-pa.googleapis.com").rstrip("/")
    headers = {"Authorization": f"Bearer {key}",
               "User-Agent": "antigravity/hub/2.26.0 (aidev_client; os_type=windows; arch=amd64; cl=1005016104)",
               **(options.get("headers") or {})}
    level = options.get("reasoning")
    request = _gemini_request(model, context, options)
    request["generationConfig"].pop("thinkingConfig", None)  # thinking level is encoded in the wire model name
    payload = {"project": options.get("projectId") or os.environ.get("GOOGLE_CLOUD_PROJECT") or "aicode-consumers",
               "model": antigravity_wire_model(model["id"], level), "request": request}
    b = _Builder(model)
    yield {"type": "start"}
    try:
        yield from _consume_gemini(_sse(f"{base}/v1internal:streamGenerateContent?alt=sse", headers, payload,
                                        options.get("signal"), on_response=options.get("onResponse")), b, unwrap=True)
    except ProviderError as e:
        if e.status not in (404, 405, 501):
            raise
        # endpoint without streaming: fall back to the unary call Omni's legacy client uses
        yield from _antigravity_unary(f"{base}/v1internal:generateContent", headers, payload, b, options)
    yield {"type": "done", "message": b.message()}


def _antigravity_unary(url, headers, payload, b: _Builder, options) -> Iterator[Dict[str, Any]]:
    req = urllib.request.Request(url, data=json.dumps(payload).encode(), method="POST",
                                 headers={"Content-Type": "application/json", **headers})
    try:
        with urllib.request.urlopen(req, timeout=300) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        text = e.read().decode("utf-8", "replace")
        raise ProviderError(f"{e.code} {_error_text(text)}", status=e.code, body=text)
    yield from _consume_gemini(iter([data]), b, unwrap=True)


# ---------------------------------------------------------------------------------------------------------
# OpenAI Responses API (ChatGPT Codex backend, Copilot /responses)
# ---------------------------------------------------------------------------------------------------------

RESPONSES_EFFORT = {"minimal": "low", "low": "low", "medium": "medium", "high": "high", "xhigh": "xhigh", "max": "xhigh"}


def _responses_input(model: Dict[str, Any], messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Pi messages -> Responses `input` items. Tool call ids are `call_id|item_id` (Pi's convention); reasoning
    items (with encrypted_content) are replayed only to the model that produced them."""
    out: List[Dict[str, Any]] = []

    def image(b: Dict[str, Any]) -> Dict[str, Any]:
        return {"type": "input_image", "detail": "auto", "image_url": f"data:{b['mimeType']};base64,{b['data']}"}

    vision = "image" in (model.get("input") or [])
    for m in messages:
        if m["role"] == "user":
            parts = []
            for b in _blocks(m["content"]):
                if b["type"] == "text" and b.get("text"):
                    parts.append({"type": "input_text", "text": b["text"]})
                elif b["type"] == "image" and vision:
                    parts.append(image(b))
            if parts:
                out.append({"role": "user", "content": parts})
        elif m["role"] == "assistant":
            same = m.get("provider") == model.get("provider") and m.get("model") == model.get("id")
            for b in m["content"]:
                if b["type"] == "thinking" and same and b.get("thinkingSignature"):
                    try:
                        item = json.loads(b["thinkingSignature"])
                    except (TypeError, ValueError):
                        continue
                    if isinstance(item, dict) and item.get("type") == "reasoning":
                        out.append(item)
                elif b["type"] == "text" and b.get("text"):
                    out.append({"role": "assistant", "content": [{"type": "output_text", "text": b["text"]}]})
                elif b["type"] == "toolCall":
                    call_id, _, item_id = str(b["id"]).partition("|")
                    fc = {"type": "function_call", "call_id": call_id, "name": b["name"],
                          "arguments": json.dumps(b.get("arguments") or {})}
                    if same and item_id.startswith("fc_"):
                        fc["id"] = item_id
                    out.append(fc)
        elif m["role"] == "toolResult":
            call_id = str(m["toolCallId"]).partition("|")[0]
            blocks = _blocks(m["content"])
            imgs = [b for b in blocks if b["type"] == "image"] if vision else []
            text = _text_of(blocks) or ("(see attached image)" if imgs else "")
            output: Any = text
            if imgs:
                output = [{"type": "input_text", "text": text}] + [image(b) for b in imgs]
            out.append({"type": "function_call_output", "call_id": call_id, "output": output})
    return out


def _responses_body(model, context, options) -> Dict[str, Any]:
    body: Dict[str, Any] = {"model": model["id"], "store": False, "stream": True,
                            "instructions": context.get("systemPrompt") or "You are a helpful assistant.",
                            "input": _responses_input(model, context["messages"]),
                            "tool_choice": "auto", "parallel_tool_calls": True}
    if context.get("tools"):
        body["tools"] = [{"type": "function", "name": t["name"], "description": t.get("description", ""),
                          "parameters": t.get("parameters") or {"type": "object", "properties": {}}, "strict": False}
                         for t in context["tools"]]
    if model.get("reasoning"):
        level = options.get("reasoning")
        body["reasoning"] = ({"effort": RESPONSES_EFFORT.get(level, "medium"), "summary": "auto"}
                             if level and level != "off" else {"effort": "none"})
        body["include"] = ["reasoning.encrypted_content"]
    if options.get("sessionId"):
        body["prompt_cache_key"] = options["sessionId"]
    return body


def _consume_responses(events: Iterator[Dict[str, Any]], b: _Builder) -> Iterator[Dict[str, Any]]:
    slots: Dict[Any, Dict[str, Any]] = {}  # output_index -> in-flight item
    finished = False
    for ev in events:
        t = ev.get("type") or ""
        idx = ev.get("output_index")
        if t == "response.output_item.added":
            item = ev.get("item") or {}
            if item.get("type") == "reasoning":
                block = {"type": "thinking", "thinking": ""}
                b.content.append(block)
                slots[idx] = {"kind": "thinking", "block": block}
            elif item.get("type") == "function_call":
                slots[idx] = {"kind": "tool", "call_id": item.get("call_id"), "id": item.get("id"),
                              "name": item.get("name") or "", "args": item.get("arguments") or ""}
                yield {"type": "toolcall_start", "id": item.get("call_id"), "name": item.get("name")}
            else:
                slots[idx] = {"kind": item.get("type")}
        elif t in ("response.reasoning_summary_text.delta", "response.reasoning_text.delta"):
            yield b.thinking(ev.get("delta") or "")
        elif t == "response.reasoning_summary_part.added":
            if b.content and b.content[-1]["type"] == "thinking" and b.content[-1]["thinking"]:
                yield b.thinking("\n\n")
        elif t in ("response.output_text.delta", "response.refusal.delta"):
            yield b.text(ev.get("delta") or "")
        elif t == "response.function_call_arguments.delta":
            if slots.get(idx, {}).get("kind") == "tool":
                slots[idx]["args"] += ev.get("delta") or ""
        elif t == "response.function_call_arguments.done":
            if slots.get(idx, {}).get("kind") == "tool" and isinstance(ev.get("arguments"), str):
                slots[idx]["args"] = ev["arguments"]
        elif t == "response.output_item.done":
            item = ev.get("item") or {}
            slot = slots.pop(idx, None) or {}
            if item.get("type") == "reasoning":
                block = slot.get("block")
                if block is None:
                    block = {"type": "thinking", "thinking": ""}
                    b.content.append(block)
                if not block["thinking"]:
                    block["thinking"] = "\n\n".join(s.get("text", "") for s in item.get("summary") or [] if s.get("text"))
                block["thinkingSignature"] = json.dumps(item)
            elif item.get("type") == "function_call":
                call_id = item.get("call_id") or slot.get("call_id") or f"call_{uuid.uuid4().hex[:12]}"
                item_id = item.get("id") or slot.get("id")
                args = item.get("arguments") if isinstance(item.get("arguments"), str) else slot.get("args", "")
                yield b.tool(f"{call_id}|{item_id}" if item_id else call_id, item.get("name") or slot.get("name", ""),
                             args)
        elif t in ("response.completed", "response.incomplete", "response.done"):
            finished = True
            resp = ev.get("response") or {}
            u = resp.get("usage") or {}
            cached = (u.get("input_tokens_details") or {}).get("cached_tokens", 0) or 0
            b.usage["input"] = (u.get("input_tokens") or 0) - cached
            b.usage["cacheRead"] = cached
            b.usage["output"] = u.get("output_tokens") or 0
            status = resp.get("status")
            if status == "incomplete":
                reason = (resp.get("incomplete_details") or {}).get("reason")
                b.stop_reason = "length" if reason == "max_output_tokens" else "error"
                if reason != "max_output_tokens":
                    b.error = f"Response incomplete: {reason or 'no reason given'}"
            elif status in ("failed", "cancelled"):
                b.stop_reason = "error"
        elif t == "response.failed":
            resp = ev.get("response") or {}
            err = resp.get("error") or {}
            raise ProviderError(f"{err.get('code') or 'failed'}: {err.get('message') or resp.get('incomplete_details') or 'no details'}")
        elif t == "error":
            code = ev.get("code") or (ev.get("error") or {}).get("code")
            msg = ev.get("message") or (ev.get("error") or {}).get("message") or json.dumps(ev)[:300]
            limited = code in ("rate_limit_exceeded", "usage_limit_reached", "usage_not_included")
            raise ProviderError(f"{code or 'error'}: {msg}", status=429 if limited else None)
    if not finished:
        raise ProviderError("Responses stream ended before response.completed")


def stream_responses(model, context, options, url: str, headers: Dict[str, str]) -> Iterator[Dict[str, Any]]:
    b = _Builder(model)
    yield {"type": "start"}
    yield from _consume_responses(_sse(url, headers, _responses_body(model, context, options), options.get("signal"),
                                       on_response=options.get("onResponse")), b)
    yield {"type": "done", "message": b.message()}


def codex_url(base: Optional[str]) -> str:
    u = (base or "https://chatgpt.com/backend-api").rstrip("/")
    if u.endswith("/codex/responses"):
        return u
    return u + ("/responses" if u.endswith("/codex") else "/codex/responses")


def stream_codex(model, context, options) -> Iterator[Dict[str, Any]]:
    """ChatGPT Plus/Pro subscription through the Codex backend (`chatgpt.com/backend-api/codex/responses`)."""
    from .oauth import CODEX_ORIGINATOR, codex_account_id
    key = options.get("apiKey") or ""
    account = options.get("accountId") or codex_account_id(key)
    if not account:
        raise ProviderError("openai-codex: the token carries no ChatGPT account id; sign in again with /login openai-codex",
                            status=401)
    session = options.get("sessionId") or str(uuid.uuid4())
    headers = {"Authorization": f"Bearer {key}", "chatgpt-account-id": account, "OpenAI-Beta": "responses=experimental",
               "originator": CODEX_ORIGINATOR, "session_id": session, **(model.get("headers") or {}),
               **(options.get("headers") or {})}
    return stream_responses(model, context, dict(options, sessionId=session), codex_url(model.get("baseUrl")), headers)


# ---------------------------------------------------------------------------------------------------------
# GitHub Copilot (OpenAI-compatible chat completions, or /responses for `wire: responses` models)
# ---------------------------------------------------------------------------------------------------------

# Copilot CLI identity, for a plain GitHub token (COPILOT_GITHUB_TOKEN, OMP keyring) that was never exchanged
COPILOT_RAW_TOKEN_HEADERS = {"User-Agent": "copilot/1.0.82", "Editor-Version": "copilot/1.0.82",
                             "Copilot-Integration-Id": "copilot-developer-cli", "X-GitHub-Api-Version": "2026-08-01"}
COPILOT_RAW_TOKEN_BASE = "https://api.githubcopilot.com"


def _has_images(messages: List[Dict[str, Any]]) -> bool:
    return any(m["role"] in ("user", "toolResult") and isinstance(m.get("content"), list)
               and any(b.get("type") == "image" for b in m["content"]) for m in messages)


def stream_copilot(model, context, options) -> Iterator[Dict[str, Any]]:
    from . import oauth
    from .models import PROVIDERS
    key = options.get("apiKey") or ""
    exchanged = "proxy-ep=" in key or key.startswith("tid=")
    if model.get("baseUrl") and model["baseUrl"] != PROVIDERS["github-copilot"]["baseUrl"]:
        base = model["baseUrl"]  # models.json override
    elif exchanged:
        base = oauth.copilot_base_url(key, options.get("enterpriseUrl"))
    else:
        base = COPILOT_RAW_TOKEN_BASE
    msgs = context["messages"]
    headers = {**(oauth.COPILOT_HEADERS if exchanged else COPILOT_RAW_TOKEN_HEADERS),
               "X-Initiator": "agent" if msgs and msgs[-1]["role"] != "user" else "user",
               "Openai-Intent": "conversation-edits", **(model.get("headers") or {}), **(options.get("headers") or {})}
    if _has_images(msgs):
        headers["Copilot-Vision-Request"] = "true"
    base = base.rstrip("/")
    if model.get("wire") == "responses":
        return stream_responses(model, context, options, base + "/responses", {"Authorization": f"Bearer {key}", **headers})
    return stream_openai(dict(model, baseUrl=base, headers=None), context, dict(options, headers=headers))


# ---------------------------------------------------------------------------------------------------------
# Faux provider (offline, deterministic) - used by tests and `--model faux/faux-1`
# ---------------------------------------------------------------------------------------------------------

class Faux:
    """Scripted responses. Push dicts like {"text": "..."} or {"tool": "read", "args": {...}} onto `queue`.

    With an empty queue, a user message `!tool name {json}` calls that tool; anything else is echoed.
    """
    queue: List[Dict[str, Any]] = []
    requests: List[Dict[str, Any]] = []


def stream_faux(model, context, options) -> Iterator[Dict[str, Any]]:
    Faux.requests.append({"systemPrompt": context.get("systemPrompt"), "messages": context["messages"],
                          "tools": [t["name"] for t in context.get("tools") or []], "options": {k: v for k, v in options.items() if k != "signal"}})
    b = _Builder(model)
    yield {"type": "start"}
    last = context["messages"][-1] if context["messages"] else {"role": "user", "content": ""}
    if Faux.queue:
        step = Faux.queue.pop(0)
    elif last["role"] == "toolResult":
        step = {"text": f"Tool {last.get('toolName')} returned: " + _text_of(last["content"])[:200]}
    else:
        text = _text_of(last.get("content", ""))
        if text.startswith("!tool "):
            parts = text[6:].split(" ", 1)
            step = {"tool": parts[0], "args": json.loads(parts[1]) if len(parts) > 1 else {}}
        else:
            step = {"text": f"faux: {text}"}
    if step.get("error"):
        raise ProviderError(step["error"], status=step.get("status"))
    if step.get("thinking"):
        yield b.thinking(step["thinking"])
    if step.get("text"):
        for i in range(0, len(step["text"]), 16):
            if options.get("signal") and options["signal"].aborted:
                raise AbortedError()
            yield b.text(step["text"][i:i + 16])
    for call in ([{"tool": step["tool"], "args": step.get("args"), "id": step.get("id")}] if step.get("tool") else []) + list(step.get("tools") or []):
        name = call.get("tool") or call.get("name")
        yield {"type": "toolcall_start", "id": None, "name": name}
        yield b.tool(call.get("id") or f"call_{uuid.uuid4().hex[:8]}", name, call.get("args") or {})
    b.usage["input"] = sum(len(json.dumps(m)) for m in context["messages"]) // 4
    b.usage["output"] = len(step.get("text", "")) // 4 + 1
    yield {"type": "done", "message": b.message()}


APIS: Dict[str, Callable[..., Iterator[Dict[str, Any]]]] = {
    "anthropic-messages": stream_anthropic,
    "openai-completions": stream_openai,
    "openai-responses": stream_openai,  # served through chat completions
    "google-generative-ai": stream_google,
    "google-antigravity": stream_antigravity,
    "faux": stream_faux,
}


def register_api(name: str, fn: Callable[..., Iterator[Dict[str, Any]]]) -> None:
    APIS[name] = fn


register_api("openai-codex-responses", stream_codex)   # /login openai-codex
register_api("github-copilot", stream_copilot)         # /login github-copilot


def stream(model: Dict[str, Any], context: Dict[str, Any], options: Dict[str, Any]) -> Iterator[Dict[str, Any]]:
    fn = APIS.get(model.get("api") or "")
    if fn is None:
        raise ProviderError(f"Unsupported API '{model.get('api')}' for {model.get('provider')}/{model.get('id')}")
    return fn(model, context, options)


def complete(model: Dict[str, Any], context: Dict[str, Any], options: Dict[str, Any]) -> Dict[str, Any]:
    """Non-streaming convenience used by compaction and branch summaries."""
    msg = None
    for ev in stream(model, context, options):
        if ev["type"] == "done":
            msg = ev["message"]
    return msg or {"role": "assistant", "content": [], "stopReason": "error", "errorMessage": "no response",
                   "usage": empty_usage(), "timestamp": int(time.time() * 1000)}
