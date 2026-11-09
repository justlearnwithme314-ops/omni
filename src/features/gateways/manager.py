"""
Multi-Platform Chat Gateway Manager (OpenClaw Multi-Channel Gateways).
Controls lifecycle and dispatch for Telegram, Discord, Slack, and WhatsApp bots.
"""

from dataclasses import dataclass
import logging
from typing import Callable, Dict, List, Optional

from .base import BaseChatGateway, GatewayNotConfigured, InboundChatMessage
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

    def start_gateway(self, platform: str) -> str:
        """Start one gateway; returns a human-readable status line (raises KeyError for unknown platforms)."""
        gw = self.gateways[platform.lower()]
        try:
            gw.start()
        except GatewayNotConfigured as e:
            return f"{platform}: not started - {e}"
        mode = "receiving + sending" if gw.inbound else f"sending only ({gw.inbound_note})"
        return f"{platform}: started, {mode}"

    def stop_gateway(self, platform: str) -> None:
        self.gateways[platform.lower()].stop()

    def start_all(self) -> List[str]:
        return [self.start_gateway(p) for p in self.gateways]

    def stop_all(self) -> None:
        for gw in self.gateways.values():
            gw.stop()

    def get_status(self) -> Dict[str, Dict[str, object]]:
        return {
            name: {"configured": gw.configured, "connected": gw.is_connected, "inbound": gw.inbound,
                   "token_env": gw.token_env, "note": gw.inbound_note}
            for name, gw in self.gateways.items()
        }
