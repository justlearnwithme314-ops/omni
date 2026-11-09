"""
Multi-Channel Webhook Hooking Engine (OpenClaw Architecture Standout)
"""

import json
from typing import Any, Dict, Optional
import urllib.request


class WebhookDispatcher:
    def __init__(self, engine: Any):
        self.engine = engine

    async def handle_telegram_webhook(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        message = payload.get("message") or payload.get("edited_message") or {}
        chat_id = message.get("chat", {}).get("id")
        text = message.get("text", "").strip()

        if not text:
            return {"status": "ignored", "reason": "No text message"}

        from ..kernel.state import SessionState
        session_id = f"tg_chat_{chat_id}"
        state = SessionState(session_id=session_id)

        response = await self.engine.run_turn(state, text)
        return {"status": "ok", "chat_id": chat_id, "response": response}

    async def handle_slack_webhook(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        if payload.get("type") == "url_verification":
            return {"challenge": payload.get("challenge")}

        event = payload.get("event", {})
        channel = event.get("channel")
        text = event.get("text", "").strip()

        if not text:
            return {"status": "ignored"}

        from ..kernel.state import SessionState
        session_id = f"slack_chan_{channel}"
        state = SessionState(session_id=session_id)

        response = await self.engine.run_turn(state, text)
        return {"status": "ok", "channel": channel, "response": response}

    async def handle_generic_webhook(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        prompt = payload.get("prompt") or payload.get("message") or payload.get("text")
        if not prompt:
            return {"error": "Missing 'prompt' or 'text' in payload"}

        session_id = payload.get("session_id") or "webhook_generic"
        model = payload.get("model")

        from ..kernel.state import SessionState
        state = SessionState(session_id=session_id)
        response = await self.engine.run_turn(state, str(prompt), model=model)

        reply_url = payload.get("reply_url")
        if reply_url:
            try:
                data = json.dumps({"response": response, "session_id": session_id}).encode("utf-8")
                req = urllib.request.Request(reply_url, data=data, headers={"Content-Type": "application/json"})
                urllib.request.urlopen(req, timeout=5)
            except Exception:
                pass

        return {"status": "success", "session_id": session_id, "response": response}
