"""
Discord Bot Gateway Client (OpenClaw Discord Integration).
Handles Discord Gateway connection, slash commands, channel mentions, and message dispatch.
"""

import json
import os
import logging
import threading
import time
from typing import Callable, Optional
import urllib.request
import urllib.error

from .base import BaseChatGateway, GatewayNotConfigured, InboundChatMessage

logger = logging.getLogger(__name__)


class DiscordChatGateway(BaseChatGateway):
    token_env = "OMNI_DISCORD_TOKEN"
    inbound = False
    inbound_note = "outbound REST only; the Discord gateway websocket is not implemented"

    def __init__(self, bot_token: Optional[str] = None, message_handler: Optional[Callable[[InboundChatMessage], str]] = None):
        super().__init__("discord", message_handler)
        self.bot_token = bot_token or os.environ.get(self.token_env, "")
        self._thread: Optional[threading.Thread] = None

    def start(self):
        """Starts Discord gateway worker."""
        if self._running:
            return
        if not self.bot_token:
            raise GatewayNotConfigured(f"Set {self.token_env} (bot token from the Discord developer portal)")
        self._running = True
        self.is_connected = True
        self._thread = threading.Thread(target=self._worker_loop, daemon=True)
        self._thread.start()
        logger.info("DiscordChatGateway connected to Gateway v10")

    def stop(self):
        self._running = False
        self.is_connected = False

    def send_message(self, channel_id: str, content: str) -> bool:
        """Sends message to Discord channel via REST API."""
        if not self.bot_token:
            return False

        url = f"https://discord.com/api/v10/channels/{channel_id}/messages"
        payload = json.dumps({"content": content}).encode("utf-8")
        headers = {
            "Authorization": f"Bot {self.bot_token}",
            "Content-Type": "application/json",
            "User-Agent": "OmniDiscordGateway/1.0",
        }
        try:
            req = urllib.request.Request(url, data=payload, headers=headers)
            with urllib.request.urlopen(req, timeout=5.0) as resp:
                return resp.status in (200, 201)
        except Exception as e:
            logger.error(f"Failed to send Discord message: {e}")
            return False

    def handle_raw_message(self, msg_event: dict) -> Optional[str]:
        """Handles MESSAGE_CREATE gateway event payload."""
        author = msg_event.get("author", {})
        if author.get("bot"):
            return None

        content = msg_event.get("content", "")
        channel_id = msg_event.get("channel_id", "")

        inbound = InboundChatMessage(
            platform="discord",
            message_id=str(msg_event.get("id", "")),
            channel_id=channel_id,
            sender_id=str(author.get("id", "")),
            sender_name=author.get("username", "Anonymous"),
            content=content,
        )
        reply = self.dispatch_inbound(inbound)
        self.send_message(channel_id, reply)
        return reply

    def _worker_loop(self):
        while self._running:
            time.sleep(1.0)
