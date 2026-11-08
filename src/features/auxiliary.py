"""
Auxiliary LLM Client Module (Hermes Architecture).

Handles background intelligence without polluting the main user conversation state:
- Generates concise 3-5 word session titles
- Scores tool trajectory outcomes via LLM-as-a-judge (success, failure_cause, score 0.0-1.0)
- Summarizes older conversation context for Pi/Hermes context compaction

Uses fast/cheap model tier (e.g. google-antigravity/gemini-2.5-flash or mock).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from typing import Any, Dict, List, Optional, Tuple, Union

logger = logging.getLogger("omni.features.auxiliary")

DEFAULT_AUXILIARY_MODEL = "google-antigravity/gemini-2.5-flash"
DEFAULT_AUXILIARY_TIER = "fast"


def _extract_message_role_content(msg: Any) -> Tuple[str, str, Optional[List[Dict[str, Any]]]]:
    """Extract (role, content, tool_calls) from dict, Message object, or raw text."""
    if isinstance(msg, dict):
        role = str(msg.get("role", "user"))
        content = str(msg.get("content") or "")
        tool_calls = msg.get("tool_calls")
        return role, content, tool_calls if isinstance(tool_calls, list) else None

    # Handle dataclass or object with attributes
    if hasattr(msg, "role"):
        role = str(getattr(msg, "role", "user"))
        content = str(getattr(msg, "content", "") or "")
        tool_calls = getattr(msg, "tool_calls", None)
        return role, content, tool_calls if isinstance(tool_calls, list) else None

    # Raw string or fallback
    return "user", str(msg or ""), None


def _clean_json_markdown(text: str) -> str:
    """Strip markdown code fence wrappers from JSON response."""
    text = text.strip()
    match = re.search(r"```(?:json)?\s*([\s\S]*?)\s*```", text, re.IGNORECASE)
    if match:
        return match.group(1).strip()
    return text


class AuxiliaryLLMClient:
    """
    Client for background intelligence tasks (titling, evaluation, compaction).
    Maintains complete isolation from the primary conversation context.
    """

    def __init__(
        self,
        provider_router: Optional[Any] = None,
        llm_client: Optional[Any] = None,
        model: str = DEFAULT_AUXILIARY_MODEL,
        tier: str = DEFAULT_AUXILIARY_TIER,
        mock_mode: Optional[bool] = None,
    ) -> None:
        """
        Initialize AuxiliaryLLMClient with provider router or unified client.

        Args:
            provider_router: Optional ProviderRouter instance.
            llm_client: Optional UnifiedLLMClient instance.
            model: Model identifier or alias (defaults to google-antigravity/gemini-2.5-flash).
            tier: Model tier mapping ('fast' by default).
            mock_mode: Force offline deterministic mock mode. Defaults to OMNI_MOCK_LLM env var.
        """
        self.model = model
        self.tier = tier

        if mock_mode is not None:
            self.mock_mode = mock_mode
        else:
            self.mock_mode = os.environ.get("OMNI_MOCK_LLM", "").lower() in ("1", "true", "yes")

        self.provider_router = provider_router
        self.llm_client = llm_client

        # Lazy initialize router or client if neither was supplied
        if self.provider_router is None and self.llm_client is None:
            try:
                from ..providers.router import ProviderRouter
                self.provider_router = ProviderRouter()
            except Exception:
                try:
                    from ..providers.client import UnifiedLLMClient
                    self.llm_client = UnifiedLLMClient(mock_mode=self.mock_mode)
                except Exception:
                    pass

    async def _call_llm(
        self,
        messages: List[Dict[str, Any]],
        system_instruction: Optional[str] = None,
    ) -> Optional[str]:
        """
        Execute an isolated completion call without touching user conversation state.
        Returns None on failure or mock mode to allow deterministic fallbacks.
        """
        if self.mock_mode:
            return None

        call_messages: List[Dict[str, Any]] = []
        if system_instruction:
            call_messages.append({"role": "system", "content": system_instruction})
        call_messages.extend(messages)

        # 1. Try ProviderRouter
        if self.provider_router is not None and hasattr(self.provider_router, "complete"):
            try:
                res = await self.provider_router.complete(
                    messages=call_messages,
                    tier=self.tier,
                    model_override=self.model,
                )
                if isinstance(res, dict):
                    content = res.get("content")
                    if content:
                        return str(content).strip()
                elif hasattr(res, "content"):
                    return str(res.content).strip()
            except Exception as e:
                logger.debug("Auxiliary call via ProviderRouter failed: %s", e)

        # 2. Try UnifiedLLMClient
        if self.llm_client is not None and hasattr(self.llm_client, "complete"):
            try:
                # Resolve provider / model if combined
                provider = "google-antigravity" if "google" in self.model else "openai"
                res = await self.llm_client.complete(
                    messages=call_messages,
                    provider=provider,
                    model=self.model,
                )
                if isinstance(res, dict):
                    content = res.get("content")
                    if content:
                        return str(content).strip()
            except Exception as e:
                logger.debug("Auxiliary call via UnifiedLLMClient failed: %s", e)

        return None

    async def generate_session_title(self, messages: list) -> str:
        """
        Generate a concise 3-5 word title for the session based on conversation messages.

        Args:
            messages: List of message dictionaries or Message instances.

        Returns:
            Concise 3-5 word title string.
        """
        if not messages:
            return "New Agent Session"

        # Format brief dialog transcript
        transcript_lines: List[str] = []
        first_user_content = ""
        for m in messages:
            role, content, _ = _extract_message_role_content(m)
            if role == "user" and not first_user_content and content:
                first_user_content = content.strip()
            if content:
                snippet = content[:300].replace("\n", " ")
                transcript_lines.append(f"{role.capitalize()}: {snippet}")

        if not transcript_lines and not first_user_content:
            return "New Agent Session"

        transcript = "\n".join(transcript_lines[:6])

        system_instruction = (
            "You are an auxiliary assistant that generates concise 3-5 word session titles. "
            "Output strictly 3 to 5 words summarizing the user's primary goal. "
            "Do not use quotes, punctuation, markdown, prefixes, or pleasantries."
        )

        user_prompt = f"Transcript:\n{transcript}\n\nConcise 3-5 word title:"

        llm_response = await self._call_llm(
            messages=[{"role": "user", "content": user_prompt}],
            system_instruction=system_instruction,
        )

        if llm_response:
            cleaned = self._sanitize_title_response(llm_response)
            if cleaned:
                return cleaned

        # Deterministic fallback: extract title from user request
        return self._heuristic_title_from_text(first_user_content)

    def _sanitize_title_response(self, text: str) -> Optional[str]:
        """Sanitize LLM output and enforce 3-5 word count constraint."""
        # Strip prefixes like "Title:", "Session Title:", quotes, markdown
        text = re.sub(r"^(?:title|session title|summary)\s*:\s*", "", text, flags=re.IGNORECASE)
        text = re.sub(r"[\"\'`*_#~]", "", text)
        text = re.sub(r"[.!?,;:()[\]{}]", " ", text)
        words = [w for w in text.split() if w]

        if not words:
            return None

        # Clamp or adjust word count to exactly 3-5 words
        if len(words) > 5:
            words = words[:5]
        elif len(words) < 3:
            padding = ["Session", "Task", "Workspace"]
            while len(words) < 3:
                words.append(padding[len(words) - 1])

        return " ".join(words).title()

    def _heuristic_title_from_text(self, text: str) -> str:
        """Deterministically derive a 3-5 word title from text."""
        if not text:
            return "Agent Conversation Session"

        # Remove common stop words
        stop_words = {
            "a", "an", "the", "and", "or", "but", "if", "is", "are", "was", "were",
            "in", "on", "at", "to", "for", "with", "about", "of", "by", "from",
            "can", "you", "please", "help", "me", "how", "do", "i", "want", "need",
            "my", "this", "that", "these", "those", "what", "which", "would", "like"
        }

        # Tokenize words
        tokens = re.findall(r"[A-Za-z0-9_-]+", text)
        filtered = [t.capitalize() for t in tokens if t.lower() not in stop_words and len(t) > 1]

        if len(filtered) >= 3:
            chosen = filtered[:min(len(filtered), 4)]
        elif filtered:
            chosen = filtered[:]
            padding = ["Task", "Workflow", "Session"]
            while len(chosen) < 3:
                chosen.append(padding[len(chosen) - 1])
        else:
            chosen = ["General", "User", "Session"]

        return " ".join(chosen[:5]).title()

    async def score_trajectory(self, turn_trace: dict) -> dict:
        """
        LLM-as-a-judge scoring of a tool outcome and turn trajectory.

        Evaluates whether the tool call and outcome advanced the user's intent.

        Args:
            turn_trace: Trace dictionary containing:
                - user_input: str
                - assistant_response: str
                - tool_calls / tool_calls_json: Optional list or JSON string
                - tool_results / tool_results_json: Optional list or JSON string
                - error: Optional error string

        Returns:
            Dictionary matching LLM-as-a-judge schema:
            {
                "success": bool,
                "failure_cause": Optional[str],
                "score": float (0.0 to 1.0),
                "reasoning": Optional[str]
            }
        """
        user_input = str(turn_trace.get("user_input") or "")
        assistant_resp = str(turn_trace.get("assistant_response") or "")
        error = turn_trace.get("error")

        # Extract tool calls and results
        tool_calls = turn_trace.get("tool_calls")
        if tool_calls is None and "tool_calls_json" in turn_trace:
            try:
                tool_calls = json.loads(turn_trace["tool_calls_json"])
            except Exception:
                tool_calls = turn_trace["tool_calls_json"]

        tool_results = turn_trace.get("tool_results")
        if tool_results is None and "tool_results_json" in turn_trace:
            try:
                tool_results = json.loads(turn_trace["tool_results_json"])
            except Exception:
                tool_results = turn_trace["tool_results_json"]

        # Call LLM judge
        system_instruction = (
            "You are an expert AI trajectory judge. Evaluate the tool outcome and turn trace.\n"
            "Assess:\n"
            "1. success: boolean (true if the tool and turn successfully progressed the user's objective)\n"
            "2. failure_cause: string or null (if failed, state concise root cause e.g. 'tool_execution_error', "
            "'syntax_error', 'hallucination', 'timeout', 'permission_boundary'; if success, null)\n"
            "3. score: float between 0.0 (total failure/error) and 1.0 (flawless execution)\n"
            "4. reasoning: concise sentence explaining the judgment\n"
            "Respond strictly with valid JSON."
        )

        trace_summary = {
            "user_input": user_input[:1000],
            "assistant_response": assistant_resp[:1000],
            "tool_calls": tool_calls,
            "tool_results": tool_results,
            "error": error,
        }

        user_prompt = f"Evaluate this turn trajectory:\n{json.dumps(trace_summary, default=str, indent=2)}"

        llm_response = await self._call_llm(
            messages=[{"role": "user", "content": user_prompt}],
            system_instruction=system_instruction,
        )

        if llm_response:
            parsed = self._parse_judge_json(llm_response)
            if parsed is not None:
                return parsed

        # Deterministic offline judge fallback
        return self._heuristic_judge(turn_trace, error, tool_results)

    def _parse_judge_json(self, response_text: str) -> Optional[Dict[str, Any]]:
        """Parse and validate LLM-as-a-judge JSON output."""
        cleaned = _clean_json_markdown(response_text)
        try:
            data = json.loads(cleaned)
            if not isinstance(data, dict):
                return None

            success = bool(data.get("success", False))
            failure_cause = data.get("failure_cause")
            if failure_cause is not None:
                failure_cause = str(failure_cause).strip()
                if not failure_cause or failure_cause.lower() in ("none", "null"):
                    failure_cause = None

            raw_score = data.get("score")
            try:
                score = float(raw_score) if raw_score is not None else (1.0 if success else 0.0)
            except (ValueError, TypeError):
                score = 1.0 if success else 0.0

            # Clamp score between 0.0 and 1.0
            clamped_score = max(0.0, min(1.0, score))

            if success and failure_cause is not None:
                failure_cause = None

            return {
                "success": success,
                "failure_cause": failure_cause,
                "score": round(clamped_score, 2),
                "reasoning": str(data.get("reasoning", "")),
            }
        except Exception:
            return None

    def _heuristic_judge(
        self,
        turn_trace: Dict[str, Any],
        error: Optional[Any],
        tool_results: Optional[Any],
    ) -> Dict[str, Any]:
        """Deterministic heuristic scoring when LLM is offline or mock mode."""
        if error:
            err_str = str(error)
            cause = "execution_error"
            if "not found" in err_str.lower():
                cause = "tool_discovery_failure"
            elif "permission" in err_str.lower() or "denied" in err_str.lower():
                cause = "permission_boundary_error"
            elif "timeout" in err_str.lower():
                cause = "timeout_error"

            return {
                "success": False,
                "failure_cause": cause,
                "score": 0.0,
                "reasoning": f"Turn terminated with error: {err_str[:150]}",
            }

        # Check tool results for errors
        res_str = str(tool_results or "")
        lower_res = res_str.lower()
        if "traceback" in lower_res or "exception" in lower_res or "syntaxerror" in lower_res:
            return {
                "success": False,
                "failure_cause": "tool_execution_failure",
                "score": 0.2,
                "reasoning": "Tool execution returned exception or traceback in output.",
            }

        if "permission denied" in lower_res or "access denied" in lower_res:
            return {
                "success": False,
                "failure_cause": "permission_boundary_error",
                "score": 0.1,
                "reasoning": "Tool execution was blocked by security permission boundary.",
            }

        # Successful outcome
        return {
            "success": True,
            "failure_cause": None,
            "score": 1.0,
            "reasoning": "Turn and tool executions completed without detected errors.",
        }

    async def summarize_older_turns(self, messages: list) -> str:
        """
        Summarize older conversation turns for Pi/Hermes context compaction.

        Preserves key facts, tool execution records, and user decisions while
        stripping conversational filler.

        Args:
            messages: List of older turn message dictionaries or Message instances.

        Returns:
            Dense, factual context summary string.
        """
        if not messages:
            return "No historical turns to summarize."

        # Reconstruct dialog turns
        dialog_lines: List[str] = []
        turn_count = 0
        for m in messages:
            role, content, tool_calls = _extract_message_role_content(m)
            if role == "user":
                turn_count += 1
                dialog_lines.append(f"User: {content.strip()}")
            elif role == "assistant":
                text = content.strip()
                if tool_calls:
                    t_names = []
                    for tc in tool_calls:
                        if isinstance(tc, dict):
                            fn = tc.get("function", {})
                            name = fn.get("name") if isinstance(fn, dict) else tc.get("name", "tool")
                        else:
                            name = str(tc)
                        t_names.append(str(name))
                    text = f"{text} [Tools invoked: {', '.join(t_names)}]".strip()
                if text:
                    dialog_lines.append(f"Assistant: {text}")
            elif role == "tool":
                snippet = content[:200] + ("..." if len(content) > 200 else "")
                dialog_lines.append(f"Tool Output: {snippet.strip()}")

        transcript = "\n".join(dialog_lines)

        system_instruction = (
            "You are an auxiliary memory compaction engine. Summarize the provided older conversation turns "
            "into a dense, factual bullet-point summary. Retain user instructions, decisions, tool names, "
            "file modifications, and error outcomes. Do not use filler or commentary."
        )

        user_prompt = f"Transcript to compact ({turn_count} turns):\n{transcript}\n\nCompacted Summary:"

        llm_response = await self._call_llm(
            messages=[{"role": "user", "content": user_prompt}],
            system_instruction=system_instruction,
        )

        if llm_response and len(llm_response.strip()) > 20:
            return llm_response.strip()

        # Deterministic offline compaction fallback
        return self._heuristic_compaction_summary(messages, turn_count)

    def _heuristic_compaction_summary(self, messages: list, turn_count: int) -> str:
        """Generate structured factual summary when LLM is offline or mock mode."""
        lines: List[str] = [
            f"[Historical Context Summary: {max(1, turn_count)} earlier turns condensed]"
        ]

        current_user = ""
        tools_called: List[str] = []
        outcomes: List[str] = []

        for m in messages:
            role, content, tool_calls = _extract_message_role_content(m)
            if role == "user" and content:
                if current_user:
                    entry = f"- User: {current_user}"
                    if tools_called:
                        entry += f" | Tools: {', '.join(tools_called)}"
                    lines.append(entry)
                    tools_called = []
                current_user = content.strip()[:150]
            elif role == "assistant":
                if tool_calls:
                    for tc in tool_calls:
                        if isinstance(tc, dict):
                            fn = tc.get("function", {})
                            tname = fn.get("name") if isinstance(fn, dict) else tc.get("name", "tool")
                        else:
                            tname = str(tc)
                        tools_called.append(str(tname))
                elif content and not outcomes:
                    outcomes.append(content.strip()[:120])
            elif role == "tool" and content:
                if "error" in content.lower():
                    tools_called.append("(error reported)")

        if current_user:
            entry = f"- User: {current_user}"
            if tools_called:
                entry += f" | Tools: {', '.join(tools_called)}"
            lines.append(entry)

        if outcomes:
            lines.append(f"- Last Resolution: {outcomes[-1]}")

        return "\n".join(lines)
