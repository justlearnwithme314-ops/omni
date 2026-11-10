"""
Unified LLM Client providing async completions across OpenAI, Anthropic,
Google, and Ollama/Local REST endpoints.
Includes XML <tool_call> parser for Hermes-style tool calling and deterministic
offline mock mode.
"""

from __future__ import annotations

import asyncio
import json
import time
import logging
import os
import re
import urllib.error
import urllib.request
import uuid
from typing import Any

logger = logging.getLogger(__name__)

try:
    import aiohttp

    HAVE_AIOHTTP = True
except ImportError:
    HAVE_AIOHTTP = False


# ============================================================================
# Exceptions
# ============================================================================


class LLMError(Exception):
    """Base exception for LLM completion errors."""

    def __init__(
        self,
        message: str,
        status_code: int | None = None,
        provider: str = "",
        model: str = "",
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.provider = provider
        self.model = model


class RateLimitError(LLMError):
    """Raised when provider returns HTTP 429 or rate limit is reached."""

    pass


class QuotaExceededError(LLMError):
    """Raised when provider quota or credits have been exhausted."""

    pass


class AuthenticationError(LLMError):
    """Raised when API key is invalid, rejected, or missing permissions (401/403)."""

    pass


class ProviderUnavailableError(LLMError):
    """Raised when provider returns 5xx error or connection fails."""

    pass


# ============================================================================
# XML Hermes-Style Tool Call Parser
# ============================================================================


def parse_hermes_tool_calls(content: str | None) -> tuple[str, list[dict[str, Any]]]:
    """
    Parses Hermes-style <tool_call>{"name": "...", "arguments": {...}}</tool_call>
    from model content and normalizes into standard tool_calls list:
    [{"id": str, "name": str, "arguments": dict}].

    Returns (cleaned_content, tool_calls_list).
    """
    if not content or "<tool_call>" not in content:
        return content or "", []

    tool_calls: list[dict[str, Any]] = []
    pattern = re.compile(r"<tool_call>\s*(.*?)\s*</tool_call>", re.DOTALL)

    for match in pattern.finditer(content):
        raw_call = match.group(1).strip()
        try:
            parsed = json.loads(raw_call)
            if isinstance(parsed, dict):
                # Extract name
                name = parsed.get("name") or parsed.get("function")
                if not name and "tool" in parsed:
                    name = parsed.get("tool")

                # Extract arguments / parameters
                args = parsed.get("arguments", parsed.get("parameters", {}))
                if isinstance(args, str):
                    try:
                        args = json.loads(args)
                    except Exception:
                        args = {"raw": args}
                elif not isinstance(args, dict):
                    args = {"value": args}

                call_id = parsed.get("id") or f"call_{uuid.uuid4().hex[:8]}"

                if name:
                    tool_calls.append(
                        {
                            "id": str(call_id),
                            "name": str(name),
                            "arguments": args,
                        }
                    )
        except Exception as e:
            logger.debug("Failed to parse tool_call block: %s (%s)", raw_call, e)

    cleaned_content = pattern.sub("", content).strip()
    return cleaned_content, tool_calls


# ============================================================================
# Tool Format Helpers
# ============================================================================


def normalize_tools_for_openai(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Format generic tools list to OpenAI function calling schema."""
    formatted = []
    for t in tools:
        if "type" in t and t["type"] == "function" and "function" in t:
            formatted.append(t)
        elif "name" in t:
            params = t.get("parameters", t.get("input_schema", {"type": "object", "properties": {}}))
            formatted.append(
                {
                    "type": "function",
                    "function": {
                        "name": t["name"],
                        "description": t.get("description", ""),
                        "parameters": params,
                    },
                }
            )
        elif "function" in t:
            fn = t["function"]
            formatted.append(
                {
                    "type": "function",
                    "function": {
                        "name": fn.get("name"),
                        "description": fn.get("description", ""),
                        "parameters": fn.get("parameters", {"type": "object", "properties": {}}),
                    },
                }
            )
    return formatted


def normalize_tools_for_anthropic(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Format generic tools list to Anthropic tool schema."""
    formatted = []
    for t in tools:
        if "input_schema" in t and "name" in t:
            formatted.append(
                {
                    "name": t["name"],
                    "description": t.get("description", ""),
                    "input_schema": t["input_schema"],
                }
            )
        elif "function" in t:
            fn = t["function"]
            formatted.append(
                {
                    "name": fn.get("name"),
                    "description": fn.get("description", ""),
                    "input_schema": fn.get("parameters", {"type": "object", "properties": {}}),
                }
            )
        elif "name" in t:
            params = t.get("parameters", {"type": "object", "properties": {}})
            formatted.append(
                {
                    "name": t["name"],
                    "description": t.get("description", ""),
                    "input_schema": params,
                }
            )
    return formatted


_GEMINI_SCHEMA_KEYS = {"type", "format", "description", "nullable", "enum", "properties", "required", "items",
                       "minItems", "maxItems", "minimum", "maximum", "minLength", "maxLength", "pattern", "anyOf", "title"}


def _gemini_schema(s: Any) -> Any:
    """Reduce a JSON Schema to the subset Gemini accepts (drops patternProperties, $ref, additionalProperties...)."""
    if isinstance(s, list):
        return [_gemini_schema(x) for x in s]
    if not isinstance(s, dict):
        return s
    out: dict[str, Any] = {}
    for k, v in s.items():
        if k not in _GEMINI_SCHEMA_KEYS:
            continue
        if k == "properties" and isinstance(v, dict):
            out[k] = {n: _gemini_schema(sub) for n, sub in v.items()}
        elif k == "type" and isinstance(v, list):
            real = [x for x in v if x != "null"]
            out["type"] = real[0] if real else "string"
            if "null" in v:
                out["nullable"] = True
        else:
            out[k] = _gemini_schema(v)
    if out.get("type") == "object" and "properties" not in out:
        out["properties"] = {}
    if "required" in out and isinstance(out.get("properties"), dict):
        out["required"] = [r for r in out["required"] if r in out["properties"]]
    return out


def normalize_tools_for_google(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Format generic tools list to Google Gemini function declaration schema."""
    declarations = []
    for t in tools:
        if "function" in t:
            fn = t["function"]
            declarations.append(
                {
                    "name": fn.get("name"),
                    "description": fn.get("description", ""),
                    "parameters": _gemini_schema(fn.get("parameters", {"type": "object", "properties": {}})),
                }
            )
        elif "name" in t:
            params = t.get("parameters", t.get("input_schema", {"type": "object", "properties": {}}))
            declarations.append(
                {
                    "name": t["name"],
                    "description": t.get("description", ""),
                    "parameters": _gemini_schema(params),
                }
            )
    seen, unique = set(), []
    for d in declarations:
        if d["name"] not in seen:
            seen.add(d["name"])
            unique.append(d)
    declarations = unique
    return [{"functionDeclarations": declarations}] if declarations else []


# ============================================================================
# Unified LLM Client
# ============================================================================



ANTIGRAVITY_WIRE_MODELS: dict[str, str] = {
    "gemini-3.8-flash": "gemini-3.8-flash-high",
    "gemini-3.8-flash-high": "gemini-3.8-flash-high",
    "gemini-3.8-flash-medium": "gemini-3.8-flash-medium",
    "gemini-3.8-flash-low": "gemini-3.8-flash-low",
    "gemini-3.7-flash": "gemini-3.7-flash-high",
    "gemini-3.7-flash-high": "gemini-3.7-flash-high",
    "gemini-3-flash": "gemini-3-flash-preview",
    "gemini-3-pro": "gemini-3-pro-preview",
    "gemini-2.5-flash": "gemini-2.5-flash",
    "gemini-4-argon": "gemini-4-argon-low",
    "claude-sonnet-5-5": "claude-sonnet-5-5-medium",
    "claude-sonnet": "claude-sonnet-5-5-medium",
    "claude-opus-5-5": "claude-opus-5-5-medium",
    "claude-opus": "claude-opus-5-5-medium",
}


class UnifiedLLMClient:
    """
    Unified LLM Client providing async completions for OpenAI, Anthropic,
    Google, and Ollama/Local REST endpoints.
    """

    def __init__(
        self,
        mock_mode: bool = False,
        fallback_to_mock: bool = True,
        timeout: float = 60.0,
    ) -> None:
        self.mock_mode = mock_mode or os.environ.get("OMNI_MOCK_LLM", "").lower() in ("1", "true", "yes")
        self.fallback_to_mock = fallback_to_mock
        self.timeout = timeout

    @staticmethod
    def parse_tool_calls(content: str | None) -> tuple[str, list[dict[str, Any]]]:
        """Convenience accessor to XML tool call parser."""
        return parse_hermes_tool_calls(content)

    def _mock_response(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
        provider: str,
        model: str,
    ) -> dict[str, Any]:
        """
        Generate a deterministic mock response for testing and offline environments.
        """
        # Find latest user prompt
        user_prompt = ""
        for m in reversed(messages):
            if m.get("role") == "user":
                content = m.get("content", "")
                if isinstance(content, list):
                    user_prompt = " ".join(
                        p.get("text", "") for p in content if isinstance(p, dict) and p.get("type") == "text"
                    )
                else:
                    user_prompt = str(content)
                break

        user_lower = user_prompt.lower()
        tool_calls: list[dict[str, Any]] = []

        # If tools are provided and user prompt mentions tool or tool calling is appropriate
        if tools:
            for t in tools:
                tool_name = (
                    t.get("name")
                    or (t.get("function", {}).get("name") if isinstance(t.get("function"), dict) else None)
                )
                if not tool_name:
                    continue

                # Match if tool name appears in user prompt or prompt asks to calculate / hash
                if tool_name.lower() in user_lower or any(
                    k in user_lower for k in ("calc", "hash", "sha256", "call", "execute")
                ):
                    # Deterministically construct mock arguments from schema
                    params = (
                        t.get("parameters")
                        or t.get("input_schema")
                        or (t.get("function", {}).get("parameters") if isinstance(t.get("function"), dict) else {})
                        or {}
                    )
                    props = params.get("properties", {})
                    mock_args = {}
                    for prop_name, prop_spec in props.items():
                        prop_type = prop_spec.get("type", "string")
                        if "expression" in prop_name:
                            mock_args[prop_name] = "2 + 2"
                        elif "text" in prop_name:
                            mock_args[prop_name] = "test-input"
                        elif prop_type == "integer" or prop_type == "number":
                            mock_args[prop_name] = 42
                        elif prop_type == "boolean":
                            mock_args[prop_name] = True
                        elif prop_type == "array":
                            mock_args[prop_name] = ["item"]
                        else:
                            mock_args[prop_name] = "mock_value"

                    tool_calls.append(
                        {
                            "id": f"call_mock_{uuid.uuid4().hex[:8]}",
                            "name": tool_name,
                            "arguments": mock_args,
                        }
                    )
                    break

        if tool_calls:
            content = f"Invoking tool {tool_calls[0]['name']} for request."
        else:
            content = f"[MockResponse {provider}/{model}] Deterministic completion: {user_prompt[:60]}"

        prompt_len = sum(len(str(m.get("content", ""))) for m in messages)
        prompt_tokens = max(1, prompt_len // 4)
        completion_tokens = max(1, len(content) // 4)

        return {
            "content": content,
            "tool_calls": tool_calls,
            "model": model,
            "provider": provider,
            "usage": {
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "total_tokens": prompt_tokens + completion_tokens,
            },
        }

    async def _http_post(
        self,
        url: str,
        headers: dict[str, str],
        payload: dict[str, Any],
        provider: str,
        model: str,
    ) -> tuple[int, str]:
        """
        Execute async HTTP POST request using aiohttp, or fallback to urllib in a thread.
        """
        json_data = json.dumps(payload)

        if HAVE_AIOHTTP:
            timeout_cfg = aiohttp.ClientTimeout(total=self.timeout)
            try:
                async with aiohttp.ClientSession(timeout=timeout_cfg) as session:
                    async with session.post(url, headers=headers, data=json_data) as resp:
                        text = await resp.text()
                        return resp.status, text
            except (aiohttp.ClientConnectorError, aiohttp.ServerConnectionError, aiohttp.ClientPayloadError) as e:
                raise ProviderUnavailableError(
                    f"Connection error reaching {provider} endpoint: {e}",
                    provider=provider,
                    model=model,
                ) from e
            except asyncio.TimeoutError as e:
                raise ProviderUnavailableError(
                    f"Request to {provider} timed out after {self.timeout}s",
                    provider=provider,
                    model=model,
                ) from e
        else:
            # Fallback to urllib.request via asyncio.to_thread
            def _sync_req() -> tuple[int, str]:
                req = urllib.request.Request(
                    url,
                    data=json_data.encode("utf-8"),
                    headers=headers,
                    method="POST",
                )
                try:
                    with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                        status = resp.status
                        body = resp.read().decode("utf-8")
                        return status, body
                except urllib.error.HTTPError as he:
                    body = he.read().decode("utf-8", errors="replace")
                    return he.code, body
                except Exception as e:
                    raise ProviderUnavailableError(
                        f"URLLib connection error reaching {provider}: {e}",
                        provider=provider,
                        model=model,
                    ) from e

            return await asyncio.to_thread(_sync_req)

    def _check_and_raise_error(
        self,
        status: int,
        response_text: str,
        provider: str,
        model: str,
    ) -> None:
        """Inspect HTTP status code and response body, raising appropriate exception."""
        if status < 400:
            return

        lower_text = response_text.lower()
        is_quota = (
            status == 402
            or "insufficient_quota" in lower_text
            or "quota" in lower_text
            or "credit_balance" in lower_text
            or "exceeded your current quota" in lower_text
            or "billing" in lower_text
        )

        if status == 429:
            if is_quota:
                raise QuotaExceededError(
                    f"Provider '{provider}' quota exceeded (429): {response_text}",
                    status_code=429,
                    provider=provider,
                    model=model,
                )
            raise RateLimitError(
                f"Provider '{provider}' rate limit exceeded (429): {response_text}",
                status_code=429,
                provider=provider,
                model=model,
            )

        if status in (401, 403):
            raise AuthenticationError(
                f"Provider '{provider}' authentication error ({status}): {response_text}",
                status_code=status,
                provider=provider,
                model=model,
            )

        if status >= 500:
            raise ProviderUnavailableError(
                f"Provider '{provider}' server error ({status}): {response_text}",
                status_code=status,
                provider=provider,
                model=model,
            )

        if is_quota:
            raise QuotaExceededError(
                f"Provider '{provider}' quota exhausted ({status}): {response_text}",
                status_code=status,
                provider=provider,
                model=model,
            )

        raise LLMError(
            f"Provider '{provider}' request failed with status {status}: {response_text}",
            status_code=status,
            provider=provider,
            model=model,
        )

    # ------------------------------------------------------------------------
    # Provider-Specific Handlers
    # ------------------------------------------------------------------------

    async def _complete_openai_compatible(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
        provider: str,
        model: str,
        api_key: str,
        base_url: str | None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """OpenAI-compatible chat completions endpoint (OpenAI, Ollama, Local)."""
        url = f"{base_url.rstrip('/')}/chat/completions" if base_url else "https://api.openai.com/v1/chat/completions"

        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}" if api_key else "Bearer local",
        }

        payload: dict[str, Any] = {
            "model": model,
            "messages": messages,
        }
        if tools:
            payload["tools"] = normalize_tools_for_openai(tools)

        for opt in ("temperature", "max_tokens", "top_p"):
            if opt in kwargs and kwargs[opt] is not None:
                payload[opt] = kwargs[opt]

        status, text = await self._http_post(url, headers, payload, provider, model)
        self._check_and_raise_error(status, text, provider, model)

        data = json.loads(text)
        choices = data.get("choices", [])
        if not choices:
            return {
                "content": "",
                "tool_calls": [],
                "model": model,
                "provider": provider,
                "usage": data.get("usage", {}),
            }

        msg = choices[0].get("message", {})
        raw_content = msg.get("content") or ""
        tool_calls: list[dict[str, Any]] = []

        # Native tool calls
        if "tool_calls" in msg and msg["tool_calls"]:
            for tc in msg["tool_calls"]:
                fn = tc.get("function", {})
                args = fn.get("arguments", {})
                if isinstance(args, str):
                    try:
                        args = json.loads(args)
                    except Exception:
                        args = {"raw": args}
                tool_calls.append(
                    {
                        "id": tc.get("id") or f"call_{uuid.uuid4().hex[:8]}",
                        "name": fn.get("name", ""),
                        "arguments": args,
                    }
                )
        else:
            # Check for Hermes-style XML tool calls in content
            clean_content, hermes_calls = parse_hermes_tool_calls(raw_content)
            if hermes_calls:
                raw_content = clean_content
                tool_calls = hermes_calls

        usage_data = data.get("usage", {})
        usage = {
            "prompt_tokens": usage_data.get("prompt_tokens", 0),
            "completion_tokens": usage_data.get("completion_tokens", 0),
            "total_tokens": usage_data.get("total_tokens", 0),
        }

        return {
            "content": raw_content,
            "tool_calls": tool_calls,
            "model": model,
            "provider": provider,
            "usage": usage,
        }

    async def _complete_anthropic(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
        provider: str,
        model: str,
        api_key: str,
        base_url: str | None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Anthropic Messages REST endpoint."""
        root = (base_url or "https://api.anthropic.com").rstrip("/")
        root = root[:-3] if root.endswith("/v1") else root  # endpoint config may already include /v1
        url = f"{root}/v1/messages"
        headers = {
            "Content-Type": "application/json",
            "anthropic-version": "2023-06-01",
        }
        oauth = api_key.startswith("sk-ant-oat")  # Claude Pro/Max subscription token
        if oauth:
            headers["Authorization"] = f"Bearer {api_key}"
            headers["anthropic-beta"] = "oauth-2025-04-20"
        else:
            headers["x-api-key"] = api_key
        system_parts: list[str] = []
        anthropic_msgs: list[dict[str, Any]] = []
        for m in messages:
            role = m.get("role", "user")
            content = m.get("content", "")
            if role == "system":
                system_parts.append(str(content))
            elif role == "tool":
                anthropic_msgs.append({
                    "role": "user",
                    "content": [{
                        "type": "tool_result",
                        "tool_use_id": m.get("tool_call_id") or "call_0",
                        "content": str(content),
                    }]
                })
            elif role == "assistant":
                blocks: list[dict[str, Any]] = [{"type": "text", "text": str(content)}] if content else []
                for tc in m.get("tool_calls") or []:
                    fn = tc.get("function", tc)
                    args = fn.get("arguments", {})
                    if isinstance(args, str):
                        try:
                            args = json.loads(args or "{}")
                        except ValueError:
                            args = {}
                    blocks.append({"type": "tool_use", "id": tc.get("id") or "call_0", "name": fn.get("name"), "input": args})
                anthropic_msgs.append({"role": "assistant", "content": blocks or [{"type": "text", "text": "(no content)"}]})
            else:
                anthropic_msgs.append({"role": "user", "content": [{"type": "text", "text": str(content)}]})

        payload: dict[str, Any] = {
            "model": model,
            "messages": anthropic_msgs,
            "max_tokens": kwargs.get("max_tokens") or 4096,
        }
        if oauth:  # subscription tokens are only accepted when the system prompt starts with this identity
            payload["system"] = [{"type": "text", "text": "You are Claude Code, Anthropic's official CLI for Claude."}] + \
                ([{"type": "text", "text": "\n\n".join(system_parts)}] if system_parts else [])
        elif system_parts:
            payload["system"] = "\n\n".join(system_parts)
        if tools:
            payload["tools"] = normalize_tools_for_anthropic(tools)

        status, text = await self._http_post(url, headers, payload, provider, model)
        self._check_and_raise_error(status, text, provider, model)
        data = json.loads(text)
        content_blocks = data.get("content", [])
        text_parts = [b.get("text", "") for b in content_blocks if b.get("type") == "text"]
        tool_calls = []
        for b in content_blocks:
            if b.get("type") == "tool_use":
                tool_calls.append({
                    "id": b.get("id"),
                    "name": b.get("name"),
                    "arguments": b.get("input", {}),
                })
        full_text = "\n".join(text_parts).strip()
        return {
            "content": full_text,
            "tool_calls": tool_calls,
            "usage": data.get("usage", {}),
        }

    async def _complete_google_antigravity(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
        provider: str,
        model: str,
        api_key: str,
        base_url: str | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Google Antigravity CloudCode assist endpoint via OAuth Bearer token."""
        clean_model = model.split("/")[-1]
        clean_model = ANTIGRAVITY_WIRE_MODELS.get(clean_model, clean_model)
        endpoint = base_url.rstrip("/") if base_url else "https://daily-cloudcode-pa.googleapis.com"
        url = f"{endpoint}/v1internal:generateContent"

        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
            "User-Agent": "antigravity/hub/2.26.0 (aidev_client; os_type=windows; arch=amd64; cl=1005016104)",
        }

        project_id = kwargs.get("project_id") or os.environ.get("GOOGLE_CLOUD_PROJECT") or "aicode-consumers"

        contents: list[dict[str, Any]] = []
        system_instruction: dict[str, Any] | None = None

        for m in messages:
            role = m.get("role", "user")
            content = m.get("content", "")
            tool_calls = m.get("tool_calls")

            if role == "system":
                system_instruction = {"parts": [{"text": str(content)}]}
            elif role == "assistant":
                parts = []
                if content:
                    parts.append({"text": str(content)})
                if tool_calls:
                    for tc in tool_calls:
                        sig = tc.get("thoughtSignature") or "skip_thought_signature_validator"
                        parts.append({
                            "functionCall": {
                                "name": tc.get("name", "tool"),
                                "args": tc.get("arguments", {}) if isinstance(tc.get("arguments"), dict) else {},
                            },
                            "thoughtSignature": sig,
                        })
                contents.append({"role": "model", "parts": parts or [{"text": ""}]})
            elif role == "tool":
                tool_name = m.get("name") or "tool"
                res_content = content
                if isinstance(content, str):
                    try:
                        res_content = json.loads(content)
                    except Exception:
                        res_content = {"output": content}
                if not isinstance(res_content, dict):
                    res_content = {"output": str(res_content)}

                contents.append({
                    "role": "user",
                    "parts": [{
                        "functionResponse": {
                            "name": tool_name,
                            "response": res_content,
                        }
                    }]
                })
            else:
                contents.append({"role": "user", "parts": [{"text": str(content)}]})

        request_body: dict[str, Any] = {"contents": contents}
        if system_instruction:
            request_body["systemInstruction"] = system_instruction
        if tools:
            formatted_tools = normalize_tools_for_google(tools)
            if formatted_tools:
                request_body["tools"] = formatted_tools

        payload = {
            "project": project_id,
            "model": clean_model,
            "request": request_body,
        }

        status, text = await self._http_post(url, headers, payload, provider, model)
        self._check_and_raise_error(status, text, provider, model)
        raw_resp = json.loads(text)
        candidates = raw_resp.get("response", {}).get("candidates", [])
        if not candidates:
            candidates = raw_resp.get("candidates", [])
        if not candidates:
            return {"content": "", "tool_calls": [], "usage": {}}

        cand = candidates[0]
        content_obj = cand.get("content", {})
        parts = content_obj.get("parts", [])

        text_parts = [p.get("text", "") for p in parts if not p.get("thought", False) and "text" in p]
        full_text = "".join(text_parts).strip()

        tool_calls: list[dict[str, Any]] = []
        for p in parts:
            if "functionCall" in p:
                fc = p["functionCall"]
                sig = p.get("thoughtSignature") or "skip_thought_signature_validator"
                tool_calls.append({
                    "id": f"call_{fc.get("name")}_{int(time.time()*1000)}",
                    "name": fc.get("name"),
                    "arguments": fc.get("args", {}),
                    "thoughtSignature": sig,
                })

        cleaned_text, xml_calls = parse_hermes_tool_calls(full_text)
        if xml_calls:
            tool_calls.extend(xml_calls)
            full_text = cleaned_text

        return {
            "content": full_text,
            "tool_calls": tool_calls,
            "usage": raw_resp.get("response", {}).get("usageMetadata", {}),
        }

    async def complete(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        provider: str = "openai",
        model: str = "gpt-4o",
        api_key: str | None = None,
        base_url: str | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """
        Unified completion call across OpenAI, Anthropic, Google, and Ollama/Local.
        Falls back to deterministic mock response if api_key == "mock", mock_mode is active,
        or if network is unreachable and fallback_to_mock is True.
        """
        provider = provider.lower().strip()

        # Offline / Mock mode check
        if self.mock_mode or api_key == "mock" or not api_key:
            logger.info("Using mock/offline response for provider '%s' (key: %s)", provider, api_key)
            return self._mock_response(messages, tools, provider, model)

        try:
            if provider == "openai":
                return await self._complete_openai_compatible(
                    messages=messages,
                    tools=tools,
                    provider=provider,
                    model=model,
                    api_key=api_key,
                    base_url=base_url,
                    **kwargs,
                )
            elif provider == "anthropic":
                return await self._complete_anthropic(
                    messages=messages,
                    tools=tools,
                    provider=provider,
                    model=model,
                    api_key=api_key,
                    base_url=base_url,
                    **kwargs,
                )
            elif provider in ("google", "google-antigravity"):
                if provider == "google-antigravity" or (api_key and api_key.startswith("ya29.")):
                    return await self._complete_google_antigravity(
                        messages=messages,
                        tools=tools,
                        provider=provider,
                        model=model,
                        api_key=api_key,
                        base_url=base_url,
                        **kwargs,
                    )
                return await self._complete_google(
                    messages=messages,
                    tools=tools,
                    provider=provider,
                    model=model,
                    api_key=api_key,
                    base_url=base_url,
                    **kwargs,
                )
            elif provider in ("local", "ollama"):
                return await self._complete_openai_compatible(
                    messages=messages,
                    tools=tools,
                    provider=provider,
                    model=model,
                    api_key=api_key or "local",
                    base_url=base_url or "http://localhost:11434/v1",
                    **kwargs,
                )
            else:
                # Default to OpenAI compatible for custom providers
                return await self._complete_openai_compatible(
                    messages=messages,
                    tools=tools,
                    provider=provider,
                    model=model,
                    api_key=api_key,
                    base_url=base_url,
                    **kwargs,
                )
        except (RateLimitError, QuotaExceededError, AuthenticationError):
            # These are explicit LLM provider responses (e.g. 429), re-raise for Keyring rotation
            raise
        except ProviderUnavailableError:
            if self.fallback_to_mock:
                logger.warning("Provider '%s' unavailable. Falling back to deterministic mock response.", provider)
                return self._mock_response(messages, tools, provider, model)
            raise
        except Exception as e:
            if self.fallback_to_mock:
                logger.warning(
                    "Network error reaching provider '%s' (%s). Falling back to mock response.",
                    provider,
                    e,
                )
                return self._mock_response(messages, tools, provider, model)
            raise
