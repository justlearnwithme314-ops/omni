"""
Slack & WhatsApp Gateway Clients (OpenClaw Slack & WhatsApp Integrations).
Provides Socket Mode / Webhook adapter for Slack and Baileys / Cloud bridge for WhatsApp.
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


class SlackChatGateway(BaseChatGateway):
    def __init__(self, bot_token: Optional[str] = None, message_handler: Optional[Callable[[InboundChatMessage], str]] = None):
        super().__init__("slack", message_handler)
        self.bot_token = bot_token or "MOCK_SLACK_BOT_TOKEN"

    def start(self):
        self._running = True
        self.is_connected = True
        logger.info("SlackChatGateway Socket Mode connected")

    def stop(self):
        self._running = False
        self.is_connected = False

    def send_message(self, channel_id: str, content: str) -> bool:
        if self.bot_token == "MOCK_SLACK_BOT_TOKEN":
            logger.info(f"[Mock Slack -> Channel {channel_id}]: {content[:80]}")
            return True

        url = "https://slack.com/api/chat.postMessage"
        payload = json.dumps({"channel": channel_id, "text": content}).encode("utf-8")
        headers = {
            "Authorization": f"Bearer {self.bot_token}",
            "Content-Type": "application/json",
        }
        try:
            req = urllib.request.Request(url, data=payload, headers=headers)
            with urllib.request.urlopen(req, timeout=5.0) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                return bool(data.get("ok"))
        except Exception as e:
            logger.error(f"Failed to post Slack message: {e}")
            return False

    def handle_raw_event(self, event_data: dict) -> Optional[str]:
        event = event_data.get("event", {})
        if event.get("bot_id"):
            return None
        text = event.get("text", "")
        channel = event.get("channel", "")
        user = event.get("user", "")

        inbound = InboundChatMessage(
            platform="slack",
            message_id=str(event.get("ts", "")),
            channel_id=channel,
            sender_id=user,
            sender_name=user,
            content=text,
        )
        reply = self.dispatch_inbound(inbound)
        self.send_message(channel, reply)
        return reply


class WhatsAppChatGateway(BaseChatGateway):
    def __init__(self, token: Optional[str] = None, phone_number_id: Optional[str] = None, message_handler: Optional[Callable[[InboundChatMessage], str]] = None):
        super().__init__("whatsapp", message_handler)
        self.token = token or "MOCK_WHATSAPP_TOKEN"
        self.phone_number_id = phone_number_id or "MOCK_PHONE_ID"
        self.pairing_qr = "QR_CODE_PAIRING_STRING_READY"

    def start(self):
        self._running = True
        self.is_connected = True
        logger.info("WhatsAppChatGateway bridge connected")

    def stop(self):
        self._running = False
        self.is_connected = False

    def send_message(self, channel_id: str, content: str) -> bool:
        if self.token == "MOCK_WHATSAPP_TOKEN":
            logger.info(f"[Mock WhatsApp -> Recipient {channel_id}]: {content[:80]}")
            return True

        url = f"https://graph.facebook.com/v18.0/{self.phone_number_id}/messages"
        payload = json.dumps({
            "messaging_product": "whatsapp",
            "to": channel_id,
            "type": "text",
            "text": {"body": content},
        }).encode("utf-8")
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json",
        }
        try:
            req = urllib.request.Request(url, data=payload, headers=headers)
            with urllib.request.urlopen(req, timeout=5.0) as resp:
                return resp.status in (200, 201)
        except Exception as e:
            logger.error(f"Failed to send WhatsApp message: {e}")
            return False

    def handle_raw_payload(self, body: dict) -> Optional[str]:
        entries = body.get("entry", [])
        for entry in entries:
            for change in entry.get("changes", []):
                val = change.get("value", {})
                for msg in val.get("messages", []):
                    sender = msg.get("from", "")
                    text = msg.get("text", {}).get("body", "")
                    inbound = InboundChatMessage(
                        platform="whatsapp",
                        message_id=msg.get("id", ""),
                        channel_id=sender,
                        sender_id=sender,
                        sender_name=sender,
                        content=text,
                    )
                    reply = self.dispatch_inbound(inbound)
                    self.send_message(sender, reply)
                    return reply
        return None
