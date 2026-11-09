"""
Base Chat Gateway Interface (OpenClaw Multi-Platform Chat Gateways).
Defines standard contracts for Telegram, Discord, Slack, and WhatsApp.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
import logging
import time
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)


@dataclass
class InboundChatMessage:
    platform: str
    message_id: str
    channel_id: str
    sender_id: str
    sender_name: str
    content: str
    timestamp: float = field(default_factory=time.time)
    metadata: Dict[str, Any] = field(default_factory=dict)


class BaseChatGateway(ABC):
    def __init__(self, platform_name: str, message_handler: Optional[Callable[[InboundChatMessage], str]] = None):
        self.platform_name = platform_name
        self.message_handler = message_handler
        self.is_connected = False
        self._running = False

    @abstractmethod
    def start(self):
        """Start listening for incoming messages (polling or websocket)."""
        pass

    @abstractmethod
    def stop(self):
        """Stop gateway connection gracefully."""
        pass

    @abstractmethod
    def send_message(self, channel_id: str, content: str) -> bool:
        """Send a message out to the chat platform."""
        pass

    def dispatch_inbound(self, msg: InboundChatMessage) -> str:
        """Processes inbound message through the registered Omni message handler."""
        if self.message_handler:
            try:
                return self.message_handler(msg)
            except Exception as e:
                logger.error(f"[{self.platform_name}] Error in message handler: {e}")
                return f"[Omni Error]: {e}"
        return f"[Omni Echo from {self.platform_name}]: {msg.content}"
