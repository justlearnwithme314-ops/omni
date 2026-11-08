"""
Prompt Prefix Guardian Module (Hermes Architecture).

Enforces Hermes's core invariant: System prompt and registered tool schemas
must remain byte-stable across a session to preserve provider KV prefix cache.
Dynamic mid-session updates (such as newly discovered skills or runtime hints)
must be injected into the user or tool message stream, preserving the initial
prefix cache.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
import logging
import time
from typing import Any, Dict, List, Optional, Tuple, Union

logger = logging.getLogger("omni.features.prefix_cache")

HERMES_PREFIX_DELIMITER = b"\x00::HERMES_PREFIX_DELIMITER::\x00"


@dataclass
class PrefixFingerprint:
    """Stores cryptographic digests and metadata for a session's initial prefix."""

    session_id: str
    sha256: str
    md5: str
    system_prompt_hash: str
    system_prompt_md5: str
    tools_hash: str
    tools_md5: str
    system_prompt_len: int
    tool_count: int
    recorded_at: float = field(default_factory=time.time)

    def to_dict(self) -> Dict[str, Any]:
        """Convert fingerprint record to dictionary."""
        return {
            "session_id": self.session_id,
            "sha256": self.sha256,
            "md5": self.md5,
            "system_prompt_hash": self.system_prompt_hash,
            "system_prompt_md5": self.system_prompt_md5,
            "tools_hash": self.tools_hash,
            "tools_md5": self.tools_md5,
            "system_prompt_len": self.system_prompt_len,
            "tool_count": self.tool_count,
            "recorded_at": self.recorded_at,
        }


def _canonicalize_tool(tool: Any) -> Dict[str, Any]:
    """Convert any tool representation (CanonicalTool, dict, Pydantic) to standard dict."""
    if hasattr(tool, "to_openai_schema") and callable(tool.to_openai_schema):
        res = tool.to_openai_schema()
        if isinstance(res, dict):
            return res
    if hasattr(tool, "to_dict") and callable(tool.to_dict):
        res = tool.to_dict()
        if isinstance(res, dict):
            return res
    if hasattr(tool, "model_dump") and callable(tool.model_dump):
        res = tool.model_dump()
        if isinstance(res, dict):
            return res
    if isinstance(tool, dict):
        return tool
    if hasattr(tool, "__dict__"):
        return {k: v for k, v in tool.__dict__.items() if not k.startswith("_")}
    return {"raw": str(tool)}


def _canonicalize_tools_payload(tools: Optional[Union[List[Any], Tuple[Any, ...]]]) -> bytes:
    """
    Produce a deterministic byte representation of registered tool schemas.
    Preserves schema order as defined to ensure exact token sequence match.
    """
    if not tools:
        return b"[]"

    normalized: List[Dict[str, Any]] = [_canonicalize_tool(t) for t in tools]
    raw_json = json.dumps(normalized, sort_keys=True, ensure_ascii=False)
    return raw_json.encode("utf-8")


def _canonicalize_system_prompt_payload(system_prompt: Optional[str]) -> bytes:
    """Produce deterministic UTF-8 byte representation of system prompt."""
    if system_prompt is None:
        return b""
    return system_prompt.encode("utf-8")


def compute_prefix_hashes(
    system_prompt: Optional[str],
    tools: Optional[Union[List[Any], Tuple[Any, ...]]],
) -> Tuple[str, str, str, str, str, str]:
    """
    Compute MD5 and SHA-256 digests for system prompt, tools, and combined prefix.

    Returns:
        (combined_sha256, combined_md5, sys_sha256, sys_md5, tools_sha256, tools_md5)
    """
    sys_bytes = _canonicalize_system_prompt_payload(system_prompt)
    tools_bytes = _canonicalize_tools_payload(tools)

    combined_bytes = sys_bytes + HERMES_PREFIX_DELIMITER + tools_bytes

    sys_sha256 = hashlib.sha256(sys_bytes).hexdigest()
    sys_md5 = hashlib.md5(sys_bytes).hexdigest()

    tools_sha256 = hashlib.sha256(tools_bytes).hexdigest()
    tools_md5 = hashlib.md5(tools_bytes).hexdigest()

    combined_sha256 = hashlib.sha256(combined_bytes).hexdigest()
    combined_md5 = hashlib.md5(combined_bytes).hexdigest()

    return combined_sha256, combined_md5, sys_sha256, sys_md5, tools_sha256, tools_md5


class PromptPrefixGuardian:
    """
    Enforces Hermes's core invariant: System prompt and registered tool schemas
    must remain byte-stable across a session to preserve provider KV prefix cache.
    """

    def __init__(self) -> None:
        self._fingerprints: Dict[str, PrefixFingerprint] = {}
        # Alias mapping for backwards compatibility with session_fingerprints dict
        self.session_fingerprints: Dict[str, str] = {}

    def _compute_fingerprint(
        self,
        system_prompt: str,
        tools: Optional[Union[List[Any], Tuple[Any, ...]]] = None,
    ) -> str:
        """Helper computing combined SHA-256 hex string."""
        c_sha256, _, _, _, _, _ = compute_prefix_hashes(system_prompt, tools)
        return c_sha256

    def record_initial_prefix(
        self,
        session_id: str,
        system_prompt: str,
        tools: Optional[list] = None,
    ) -> PrefixFingerprint:
        """
        Record the MD5/SHA256 fingerprint for a session's initial prompt prefix.

        Args:
            session_id: Unique session identifier.
            system_prompt: The base system instruction.
            tools: Registered tool schema definitions.

        Returns:
            The recorded PrefixFingerprint instance.
        """
        tools_list = tools if tools is not None else []
        c_sha256, c_md5, s_sha256, s_md5, t_sha256, t_md5 = compute_prefix_hashes(
            system_prompt, tools_list
        )

        fp = PrefixFingerprint(
            session_id=str(session_id),
            sha256=c_sha256,
            md5=c_md5,
            system_prompt_hash=s_sha256,
            system_prompt_md5=s_md5,
            tools_hash=t_sha256,
            tools_md5=t_md5,
            system_prompt_len=len(system_prompt or ""),
            tool_count=len(tools_list),
            recorded_at=time.time(),
        )

        sid = str(session_id)
        self._fingerprints[sid] = fp
        self.session_fingerprints[sid] = c_sha256

        logger.debug(
            "Recorded initial prefix for session '%s' (sha256: %s, md5: %s, tools: %d)",
            session_id,
            c_sha256[:12],
            c_md5[:12],
            len(tools_list),
        )
        return fp

    def validate_turn_prefix(
        self,
        session_id: str,
        system_prompt: str,
        tools: Optional[list] = None,
    ) -> Tuple[bool, Optional[str]]:
        """
        Validate that the incoming turn's system prompt and tool definitions match
        the session's recorded initial prefix byte-for-byte.

        Args:
            session_id: Unique session identifier.
            system_prompt: Current turn's system instruction.
            tools: Current turn's registered tool schema definitions.

        Returns:
            (True, None) if the prefix matches byte-for-byte.
            (False, warning_reason) if the prefix was mutated.
        """
        sid = str(session_id)
        if sid not in self._fingerprints:
            # Auto-record initial prefix on first turn of session if not yet recorded
            self.record_initial_prefix(session_id, system_prompt, tools)
            return True, None

        tools_list = tools if tools is not None else []
        recorded = self._fingerprints[sid]
        c_sha256, c_md5, s_sha256, _, t_sha256, _ = compute_prefix_hashes(
            system_prompt, tools_list
        )

        if c_sha256 == recorded.sha256:
            return True, None

        # Prefix was mutated: diagnose specific cause(s)
        mutations: List[str] = []
        if s_sha256 != recorded.system_prompt_hash:
            curr_len = len(system_prompt or "")
            mutations.append(
                f"system prompt mutated (length {recorded.system_prompt_len} -> {curr_len}, "
                f"sha256 {recorded.system_prompt_hash[:8]} -> {s_sha256[:8]})"
            )

        if t_sha256 != recorded.tools_hash:
            curr_count = len(tools_list)
            mutations.append(
                f"registered tool schemas mutated (count {recorded.tool_count} -> {curr_count}, "
                f"sha256 {recorded.tools_hash[:8]} -> {t_sha256[:8]})"
            )

        if not mutations:
            mutations.append(
                f"delimiter/hash mismatch (expected {recorded.sha256[:8]}, got {c_sha256[:8]})"
            )

        warning_reason = (
            f"[Hermes Invariant Warning] System prompt or tool definitions were mutated mid-session! "
            f"KV Prefix Cache Invalidation Warning for session '{session_id}': "
            + "; ".join(mutations)
            + ". Inject mid-session skills/hints via user messages or tool results, not system prompt."
        )
        logger.warning(warning_reason)
        return False, warning_reason

    @staticmethod
    def format_mid_session_injection(
        injection_text: str,
        as_tool_result: bool = False,
        tool_call_id: Optional[str] = None,
        name: Optional[str] = None,
        role: Optional[str] = None,
        source: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Format mid-turn skills, runtime hints, or memory injections as user messages
        or tool results (the only cache-preserving injection method).

        Injecting dynamic context into user or tool messages ensures the system prompt
        and tool definitions remain untouched, protecting provider KV cache reuse.

        Args:
            injection_text: Content of the skill, hint, or dynamic context.
            as_tool_result: If True, format as a 'tool' role result message.
            tool_call_id: Optional tool call ID if formatting as tool result.
            name: Optional tool name if formatting as tool result.
            role: Explicit role override ('user' or 'tool'). Takes precedence over as_tool_result.
            source: Optional source tag for hint/skill context.

        Returns:
            Standard LLM chat completion message dictionary.
        """
        cleaned_text = injection_text.strip() if injection_text else ""

        is_tool = role == "tool" or (as_tool_result and role != "user")

        if is_tool:
            if not tool_call_id:
                digest = hashlib.md5(cleaned_text.encode("utf-8")).hexdigest()[:8]
                call_id = f"inj_{digest}"
            else:
                call_id = tool_call_id

            msg: Dict[str, Any] = {
                "role": "tool",
                "tool_call_id": call_id,
                "content": cleaned_text,
            }
            if name:
                msg["name"] = name
            return msg

        # Default: Format as user context message
        tag = f"Context Injection: {source}" if source else "Context Injection"
        return {
            "role": "user",
            "content": f"[{tag}]\n{cleaned_text}\n[End Context Injection]" if source else f"[{tag}: {cleaned_text}]",
        }

    def get_fingerprint(self, session_id: str) -> Optional[PrefixFingerprint]:
        """Retrieve the recorded PrefixFingerprint for a session if present."""
        return self._fingerprints.get(str(session_id))

    def has_prefix(self, session_id: str) -> bool:
        """Check if an initial prefix is recorded for a session."""
        return str(session_id) in self._fingerprints

    def clear_session(self, session_id: str) -> bool:
        """Clear recorded prefix for a session."""
        sid = str(session_id)
        self.session_fingerprints.pop(sid, None)
        return self._fingerprints.pop(sid, None) is not None

    def clear_all(self) -> None:
        """Clear all recorded session prefixes."""
        self._fingerprints.clear()
        self.session_fingerprints.clear()
