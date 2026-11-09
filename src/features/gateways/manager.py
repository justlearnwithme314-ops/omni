"""
Multi-Platform Chat Gateway Manager (OpenClaw Multi-Channel Gateways).
Controls lifecycle and dispatch for Telegram, Discord, Slack, and WhatsApp bots.
"""

from dataclasses import dataclass
import logging
from typing import Callable, Dict, List, Optional

from .base import BaseChatGateway, InboundChatMessage
from .telegram import TelegramChatGateway
from .discord import DiscordChatGateway
from .slack_whatsapp import SlackChatGateway, WhatsAppChatGateway

logger = logging.getLogger(__name__)


class ChatGatewayManager:
    def __init__(self, message_router: Optional[Callable[[InboundChatMessage], str]] = None):
        self.message_router = message_router or (lambda m: f"Omni Agent: Received '{m.content}' via {m.platform}")
        self.gateways: Dict[str, BaseChatGateway] = {
            "telegram": TelegramChatGateway(message_handler=self._on_inbound),
            "discord": DiscordChatGateway(message_handler=self._on_inbound),
            "slack": SlackChatGateway(message_handler=self._on_inbound),
            "whatsapp": WhatsAppChatGateway(message_handler=self._on_inbound),
        }

    def _on_inbound(self, msg: InboundChatMessage) -> str:
        logger.info(f"GatewayManager: Inbound message from {msg.platform} user {msg.sender_name}: {msg.content}")
        return self.message_router(msg)

    def start_gateway(self, platform: str) -> bool:
        """Start a specific platform gateway."""
        gw = self.gateways.get(platform.lower())
        if gw:
            gw.start()
            return True
        return False

    def stop_gateway(self, platform: str) -> bool:
        """Stop a specific platform gateway."""
        gw = self.gateways.get(platform.lower())
        if gw:
            gw.stop()
            return True
        return False

    def start_all(self):
        for gw in self.gateways.values():
            gw.start()

    def stop_all(self):
        for gw in self.gateways.values():
            gw.stop()

    def get_status(self) -> Dict[str, bool]:
        """Returns connection status for all registered gateways."""
        return {name: gw.is_connected for name, gw in self.gateways.items()}
