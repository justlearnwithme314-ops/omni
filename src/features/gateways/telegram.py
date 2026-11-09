"""
Telegram Bot Gateway Client (OpenClaw Telegram Integration).
Supports long-polling and webhook ingestion for 2-way AI chat on Telegram.
"""

import json
import logging
import threading
import time
from typing import Callable, Optional
import urllib.request
import urllib.error

from .base import BaseChatGateway, InboundChatMessage

logger = logging.getLogger(__name__)


class TelegramChatGateway(BaseChatGateway):
    def __init__(self, bot_token: Optional[str] = None, message_handler: Optional[Callable[[InboundChatMessage], str]] = None):
        super().__init__("telegram", message_handler)
        self.bot_token = bot_token or "MOCK_TELEGRAM_BOT_TOKEN"
        self._thread: Optional[threading.Thread] = None
        self._last_update_id = 0

    def start(self):
        """Starts background long-polling loop."""
        if self._running:
            return
        self._running = True
        self.is_connected = True
        self._thread = threading.Thread(target=self._polling_loop, daemon=True)
        self._thread.start()
        logger.info("TelegramChatGateway started polling")

    def stop(self):
        self._running = False
        self.is_connected = False

    def send_message(self, channel_id: str, content: str) -> bool:
        """Send message back to Telegram chat."""
        if self.bot_token == "MOCK_TELEGRAM_BOT_TOKEN":
            logger.info(f"[Mock Telegram -> Chat {channel_id}]: {content[:80]}")
            return True

        url = f"https://api.telegram.org/bot{self.bot_token}/sendMessage"
        payload = json.dumps({"chat_id": channel_id, "text": content, "parse_mode": "Markdown"}).encode("utf-8")
        try:
            req = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=5.0) as resp:
                return resp.status == 200
        except Exception as e:
            logger.error(f"Failed to send Telegram message: {e}")
            return False

    def handle_raw_update(self, update: dict) -> Optional[str]:
        """Processes an incoming Telegram Update object (from webhook or polling)."""
        msg = update.get("message") or update.get("channel_post")
        if not msg:
            return None

        text = msg.get("text", "")
        chat = msg.get("chat", {})
        sender = msg.get("from", {})

        inbound = InboundChatMessage(
            platform="telegram",
            message_id=str(msg.get("message_id", "")),
            channel_id=str(chat.get("id", "")),
            sender_id=str(sender.get("id", "")),
            sender_name=sender.get("username") or sender.get("first_name", "Anonymous"),
            content=text,
        )
        reply = self.dispatch_inbound(inbound)
        self.send_message(inbound.channel_id, reply)
        return reply

    def _polling_loop(self):
        while self._running:
            if self.bot_token == "MOCK_TELEGRAM_BOT_TOKEN":
                time.sleep(1.0)
                continue

            try:
                url = f"https://api.telegram.org/bot{self.bot_token}/getUpdates?offset={self._last_update_id + 1}&timeout=10"
                req = urllib.request.Request(url)
                with urllib.request.urlopen(req, timeout=15.0) as resp:
                    data = json.loads(resp.read().decode("utf-8"))
                for u in data.get("result", []):
                    self._last_update_id = u["update_id"]
                    self.handle_raw_update(u)
            except Exception:
                time.sleep(3.0)
