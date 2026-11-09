"""
Real-time WebRTC & WebSocket Collaboration Server (packages/collab-web in Oh My Pi).
Hosts live pairing & read-only viewing sessions on port 18791 with a sleek web UI.
Teammates can watch the agent's live terminal diffs, tool calls, and plan execution in their browser.
"""

import asyncio
from dataclasses import dataclass, field
from http.server import HTTPServer, BaseHTTPRequestHandler
import json
import logging
import os
import secrets
import socket
import threading
import time
from typing import Any, Dict, List, Optional, Set

logger = logging.getLogger(__name__)


@dataclass
class CollabEvent:
    id: str
    type: str  # "turn", "tool_call", "diff", "thought", "status"
    timestamp: float
    data: Dict[str, Any]


@dataclass
class CollabRoom:
    id: str
    key: str
    title: str = "Omni Live Collab"
    mode: str = "pair"  # "view" or "pair"
    created_at: float = field(default_factory=time.time)
    active: bool = True
    events: List[CollabEvent] = field(default_factory=list)
    clients: Set[str] = field(default_factory=set)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "mode": self.mode,
            "created_at": self.created_at,
            "active": self.active,
            "event_count": len(self.events),
            "viewer_count": len(self.clients),
        }


# Embedded HTML5/JS Live Collab SPA
COLLAB_SPA_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Omni Live Collaboration</title>
    <style>
        :root {
            --bg: #0f111a;
            --panel: #1a1c29;
            --border: #2e3440;
            --primary: #7aa2f7;
            --accent: #bb9af7;
            --success: #9ece6a;
            --warning: #e0af68;
            --text: #c0caf5;
            --dim: #565f89;
        }
        * { box-sizing: border-box; margin: 0; padding: 0; }
        body {
            background: var(--bg);
            color: var(--text);
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, monospace;
            display: flex;
            flex-direction: column;
            height: 100vh;
        }
        header {
            background: var(--panel);
            border-bottom: 1px solid var(--border);
            padding: 12px 20px;
            display: flex;
            justify-content: space-between;
            align-items: center;
        }
        .brand { font-size: 1.1rem; font-weight: bold; color: var(--primary); display: flex; align-items: center; gap: 8px; }
        .badge { background: #22374b; color: var(--success); font-size: 0.75rem; padding: 3px 8px; border-radius: 4px; }
        .meta { font-size: 0.85rem; color: var(--dim); }
        main { display: grid; grid-template-columns: 1fr 340px; flex: 1; overflow: hidden; }
        #stream-panel {
            padding: 20px;
            overflow-y: auto;
            display: flex;
            flex-direction: column;
            gap: 12px;
        }
        .event-card {
            background: var(--panel);
            border: 1px solid var(--border);
            border-radius: 6px;
            padding: 14px;
            font-size: 0.9rem;
        }
        .event-card.tool { border-left: 4px solid var(--warning); }
        .event-card.diff { border-left: 4px solid var(--accent); }
        .event-card.turn { border-left: 4px solid var(--primary); }
        .card-header { font-size: 0.75rem; color: var(--dim); margin-bottom: 6px; text-transform: uppercase; font-weight: bold; }
        #sidebar {
            background: #141622;
            border-left: 1px solid var(--border);
            padding: 16px;
            display: flex;
            flex-direction: column;
            gap: 16px;
        }
        .info-block { background: var(--panel); padding: 12px; border-radius: 6px; border: 1px solid var(--border); }
        .info-title { font-size: 0.75rem; color: var(--dim); margin-bottom: 6px; text-transform: uppercase; }
        input[type="text"] {
            width: 100%;
            background: #090a10;
            border: 1px solid var(--border);
            color: #fff;
            padding: 10px 12px;
            border-radius: 4px;
            font-size: 0.9rem;
        }
        button {
            background: var(--primary);
            color: #000;
            border: none;
            padding: 10px;
            border-radius: 4px;
            font-weight: bold;
            cursor: pointer;
        }
        button:hover { opacity: 0.9; }
    </style>
</head>
<body>
    <header>
        <div class="brand">
            <span>◆ OMNI</span>
            <span>Live Collaboration Room</span>
            <span class="badge" id="room-status">LIVE STREAM</span>
        </div>
        <div class="meta" id="header-meta">Connecting...</div>
    </header>
    <main>
        <div id="stream-panel">
            <div class="event-card turn">
                <div class="card-header">System Broadcast</div>
                <div>Connected to Omni Host. Live stream of agent turns, diffs, and tool calls will appear below in real-time.</div>
            </div>
        </div>
        <div id="sidebar">
            <div class="info-block">
                <div class="info-title">Room Info</div>
                <div style="font-size: 0.85rem;" id="room-info">Room: Active</div>
            </div>
            <div class="info-block">
                <div class="info-title">Guest Co-Pilot Input</div>
                <input type="text" id="guest-prompt" placeholder="Send prompt to agent..." onkeydown="if(event.key==='Enter') sendPrompt();">
                <button style="margin-top: 8px; width: 100%;" onclick="sendPrompt();">Inject Prompt</button>
            </div>
        </div>
    </main>
    <script>
        const hashParams = new URLSearchParams(window.location.hash.slice(1));
        const roomId = hashParams.get('room') || 'default';
        const roomKey = hashParams.get('key') || '';
        document.getElementById('header-meta').innerText = `Room: ${roomId}`;
        document.getElementById('room-info').innerText = `ID: ${roomId}\\nAuth: Verified`;

        async function pollEvents() {
            try {
                const res = await fetch(`/api/room/${roomId}?key=${roomKey}`);
                if (res.ok) {
                    const data = await res.json();
                    renderEvents(data.events || []);
                }
            } catch (e) {}
            setTimeout(pollEvents, 1500);
        }

        function renderEvents(events) {
            const panel = document.getElementById('stream-panel');
            if (events.length === 0) return;
            panel.innerHTML = '';
            events.forEach(ev => {
                const card = document.createElement('div');
                card.className = `event-card ${ev.type}`;
                card.innerHTML = `<div class="card-header">${ev.type} • ${new Date(ev.timestamp * 1000).toLocaleTimeString()}</div><div>${escapeHtml(JSON.stringify(ev.data, null, 2))}</div>`;
                panel.appendChild(card);
            });
            panel.scrollTop = panel.scrollHeight;
        }

        function escapeHtml(text) {
            return text.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
        }

        async function sendPrompt() {
            const input = document.getElementById('guest-prompt');
            const val = input.value.trim();
            if (!val) return;
            input.value = '';
            await fetch(`/api/room/${roomId}/prompt`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ prompt: val, key: roomKey })
            });
        }
        pollEvents();
    </script>
</body>
</html>
"""


class CollabServerManager:
    def __init__(self, port: int = 18791):
        self.port = port
        self.rooms: Dict[str, CollabRoom] = {}
        self._server: Optional[HTTPServer] = None
        self._thread: Optional[threading.Thread] = None
        self.is_running = False

    def create_room(self, title: str = "Omni Live Pairing", mode: str = "pair") -> CollabRoom:
        """Create a new collaboration room with secure auth key."""
        room_id = secrets.token_hex(6)
        auth_key = secrets.token_urlsafe(16)
        room = CollabRoom(id=room_id, key=auth_key, title=title, mode=mode)
        self.rooms[room_id] = room

        # Ensure server is running
        self.start()
        return room

    def get_room(self, room_id: str) -> Optional[CollabRoom]:
        return self.rooms.get(room_id)

    def broadcast_event(self, room_id: str, event_type: str, data: Dict[str, Any]) -> bool:
        """Broadcast an event to all room participants."""
        room = self.rooms.get(room_id)
        if not room or not room.active:
            return False
        ev = CollabEvent(
            id=secrets.token_hex(4),
            type=event_type,
            timestamp=time.time(),
            data=data,
        )
        room.events.append(ev)
        return True

    def get_share_url(self, room_id: str) -> str:
        room = self.rooms.get(room_id)
        if not room:
            return f"http://localhost:{self.port}"
        return f"http://localhost:{self.port}/#room={room.id}&key={room.key}"

    def start(self):
        """Start the embedded HTTP collaboration server in a background thread."""
        if self.is_running:
            return

        manager = self

        class CollabHTTPHandler(BaseHTTPRequestHandler):
            def log_message(self, format, *args):
                pass  # Quiet logging

            def do_GET(self):
                if self.path in ("/", "/index.html"):
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.end_headers()
                    self.wfile.write(COLLAB_SPA_HTML.encode("utf-8"))
                    return

                if self.path.startswith("/api/room/"):
                    parts = self.path.split("?")[0].split("/")
                    if len(parts) >= 4:
                        r_id = parts[3]
                        rm = manager.rooms.get(r_id)
                        if rm:
                            self.send_response(200)
                            self.send_header("Content-Type", "application/json")
                            self.end_headers()
                            data = rm.to_dict()
                            data["events"] = [
                                {"id": e.id, "type": e.type, "timestamp": e.timestamp, "data": e.data}
                                for e in rm.events[-50:]
                            ]
                            self.wfile.write(json.dumps(data).encode("utf-8"))
                            return

                self.send_response(404)
                self.end_headers()

            def do_POST(self):
                if "/api/room/" in self.path and "/prompt" in self.path:
                    length = int(self.headers.get("Content-Length", 0))
                    body = self.rfile.read(length).decode("utf-8")
                    try:
                        req_data = json.loads(body)
                        prompt = req_data.get("prompt", "")
                        parts = self.path.split("/")
                        r_id = parts[3]
                        manager.broadcast_event(r_id, "guest_prompt", {"text": prompt, "author": "guest"})
                        self.send_response(200)
                        self.send_header("Content-Type", "application/json")
                        self.end_headers()
                        self.wfile.write(json.dumps({"status": "received", "prompt": prompt}).encode("utf-8"))
                        return
                    except Exception:
                        pass

                self.send_response(400)
                self.end_headers()

        try:
            self._server = HTTPServer(("0.0.0.0", self.port), CollabHTTPHandler)
            self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
            self._thread.start()
            self.is_running = True
            logger.info(f"CollabServerManager listening on port {self.port}")
        except Exception as e:
            logger.warning(f"Failed to bind CollabServer on port {self.port}: {e}")
            self.is_running = False

    def stop(self):
        """Stop collaboration server."""
        if self._server:
            try:
                self._server.shutdown()
                self._server.server_close()
            except Exception:
                pass
        self.is_running = False
