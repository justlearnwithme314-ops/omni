"""OmniHarness Kernel State Management Module.

Provides Message dataclass and SessionState with Pi-style context compaction,
preserving prefix cache and sacred system prompt.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import json
from typing import Any, Dict, List, Literal, Optional, Union
import uuid

RoleType = Literal["system", "user", "assistant", "tool"]
VALID_ROLES: set[str] = {"system", "user", "assistant", "tool"}


@dataclass
class Message:
    """Represents a conversation turn message."""

    role: str  # 'system' | 'user' | 'assistant' | 'tool'
    content: str = ""
    tool_calls: Optional[List[Dict[str, Any]]] = None
    tool_call_id: Optional[str] = None
    name: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        """Full serialization including metadata."""
        data: Dict[str, Any] = {
            "role": self.role,
            "content": self.content,
        }
        if self.tool_calls is not None:
            data["tool_calls"] = self.tool_calls
        if self.tool_call_id is not None:
            data["tool_call_id"] = self.tool_call_id
        if self.name is not None:
            data["name"] = self.name
        if self.metadata:
            data["metadata"] = dict(self.metadata)
        return data

    def to_llm_dict(self) -> Dict[str, Any]:
        """Format message dictionary for standard LLM chat completion APIs."""
        msg: Dict[str, Any] = {
            "role": self.role,
            "content": self.content or "",
        }
        if self.tool_calls is not None:
            msg["tool_calls"] = self.tool_calls
        if self.tool_call_id is not None:
            msg["tool_call_id"] = self.tool_call_id
        if self.name is not None:
            msg["name"] = self.name
        return msg

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Message":
        """Reconstruct Message instance from a dictionary."""
        return cls(
            role=data.get("role", "user"),
            content=data.get("content", ""),
            tool_calls=data.get("tool_calls"),
            tool_call_id=data.get("tool_call_id"),
            name=data.get("name"),
            metadata=dict(data.get("metadata", {})),
        )


@dataclass
class SessionState:
    """Manages active session context, message history, scratchpad, and compaction."""

    session_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    messages: List[Message] = field(default_factory=list)
    scratchpad: Dict[str, Any] = field(default_factory=dict)
    token_count: int = 0
    turn_index: int = 0

    def add_message(
        self,
        role: Union[Message, str] = "user",
        content: str = "",
        tool_calls: Optional[List[Dict[str, Any]]] = None,
        tool_call_id: Optional[str] = None,
        name: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Message:
        """Append a message to the session state."""
        if isinstance(role, Message):
            msg = role
        else:
            msg = Message(
                role=role,
                content=content,
                tool_calls=tool_calls,
                tool_call_id=tool_call_id,
                name=name,
                metadata=metadata if metadata is not None else {},
            )
        self.messages.append(msg)
        self._increment_token_estimate(msg)
        return msg

    def get_messages_for_llm(self) -> List[Dict[str, Any]]:
        """Return message list serialized for LLM completion requests."""
        return [m.to_llm_dict() for m in self.messages]

    def _increment_token_estimate(self, msg: Message) -> None:
        """Rough heuristic token accumulation (~4 chars per token)."""
        content_len = len(msg.content or "")
        if msg.tool_calls:
            content_len += len(json.dumps(msg.tool_calls))
        self.token_count += max(1, content_len // 4) if content_len > 0 else 0

    def _recalculate_token_count(self) -> int:
        """Recalculate token count across all messages."""
        total_chars = 0
        for m in self.messages:
            total_chars += len(m.content or "")
            if m.tool_calls:
                total_chars += len(json.dumps(m.tool_calls))
        self.token_count = max(1, total_chars // 4) if total_chars > 0 else 0
        return self.token_count

    def compact_history(
        self,
        max_turns: int = 10,
        preserve_system: bool = True,
        custom_summary: Optional[str] = None,
    ) -> bool:
        """Pi-style context compaction.

        Preserves the sacred system prompt at the beginning to avoid invalidating
        the LLM prefix cache, keeps the latest N turns intact, and condenses older
        turns into a single concise summary note.

        Returns True if compaction occurred, False if history was within max_turns.
        """
        if max_turns < 1:
            max_turns = 1

        # 1. Identify and preserve the sacred system prompt(s) at the start
        system_messages: List[Message] = []
        idx = 0
        if preserve_system:
            while idx < len(self.messages) and self.messages[idx].role == "system":
                system_messages.append(self.messages[idx])
                idx += 1

        conversation = self.messages[idx:]

        # 2. Group conversation into turns based on user message boundaries
        turns: List[List[Message]] = []
        current_turn: List[Message] = []
        for msg in conversation:
            if msg.role == "user" and current_turn:
                turns.append(current_turn)
                current_turn = [msg]
            else:
                current_turn.append(msg)
        if current_turn:
            turns.append(current_turn)

        # Compaction not needed if turns are within threshold
        if len(turns) <= max_turns:
            return False

        older_turns = turns[:-max_turns]
        recent_turns = turns[-max_turns:]

        # 3. Build a condensed summary note of the older turns
        summary_entries: List[str] = [
            f"[Historical Context Summary: {len(older_turns)} earlier turns condensed]"
        ]

        for turn_idx, turn_msgs in enumerate(older_turns, 1):
            turn_lines: List[str] = []
            for m in turn_msgs:
                if m.role == "user":
                    turn_lines.append(f"User: {m.content}")
                elif m.role == "assistant":
                    text = m.content or ""
                    if m.tool_calls:
                        tool_names = []
                        for tc in m.tool_calls:
                            if isinstance(tc, dict):
                                fn = tc.get("function", {})
                                tname = (
                                    fn.get("name")
                                    if isinstance(fn, dict)
                                    else tc.get("name", "tool")
                                )
                            else:
                                tname = str(tc)
                            tool_names.append(str(tname))
                        text = (
                            f"{text} [Tool Calls: {', '.join(tool_names)}]".strip()
                        )
                    if text:
                        turn_lines.append(f"Assistant: {text}")
                elif m.role == "tool":
                    tool_ident = m.name or m.tool_call_id or "tool"
                    snippet = (
                        m.content[:200] + "..."
                        if len(m.content or "") > 200
                        else (m.content or "")
                    )
                    turn_lines.append(f"Tool ({tool_ident}): {snippet}")
                else:
                    turn_lines.append(f"{m.role.capitalize()}: {m.content}")

            if turn_lines:
                summary_entries.append(
                    f"--- Turn {turn_idx} ---\n" + "\n".join(turn_lines)
                )

        if custom_summary is not None:
            summary_note = custom_summary
        else:
            summary_note = "\n\n".join(summary_entries)

        summary_user_msg = Message(
            role="user",
            content=summary_note,
            metadata={
                "compacted": True,
                "condensed_turns": len(older_turns),
                "type": "history_summary",
            },
        )

        assistant_ack = Message(
            role="assistant",
            content="Understood. I have retained the earlier conversation context.",
            metadata={"compacted": True},
        )

        # 4. Reassemble messages: Sacred system prompt -> Summary note -> Ack -> Latest N turns
        new_messages: List[Message] = []
        new_messages.extend(system_messages)
        new_messages.append(summary_user_msg)
        new_messages.append(assistant_ack)

        for turn in recent_turns:
            new_messages.extend(turn)

        self.messages = new_messages
        self._recalculate_token_count()
        return True

    def to_dict(self) -> Dict[str, Any]:
        """Serialize state to dictionary."""
        return {
            "session_id": self.session_id,
            "messages": [m.to_dict() for m in self.messages],
            "scratchpad": dict(self.scratchpad),
            "token_count": self.token_count,
            "turn_index": self.turn_index,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "SessionState":
        """Instantiate SessionState from a dictionary."""
        raw_msgs = data.get("messages", [])
        messages = [
            Message.from_dict(m) if isinstance(m, dict) else m
            for m in raw_msgs
        ]
        return cls(
            session_id=data.get("session_id", str(uuid.uuid4())),
            messages=messages,
            scratchpad=dict(data.get("scratchpad", {})),
            token_count=int(data.get("token_count", 0)),
            turn_index=int(data.get("turn_index", 0)),
        )
