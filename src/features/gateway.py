"""Omni Multi-Channel Gateway Server (OpenClaw Standard).

Provides a lightweight async HTTP/WebSocket gateway server listening on port 18789.
Supports:
- GET  /health       -> {"status": "ok", "app": "Omni"}
- GET  /api/tools    -> Discovered tool list
- POST /api/chat     -> Runs a turn and returns {"response": str, "tool_calls": list}
- GET  /api/sessions -> Lists saved sessions
- Full RFC 6455 WebSocket communication on the same port
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import os
from pathlib import Path
import time
from typing import Any, Dict, List, Optional, Tuple, Union
import urllib.parse
import uuid

from ..kernel.state import SessionState
from .persistence import SessionStore

logger = logging.getLogger("omni.features.gateway")

DEFAULT_GATEWAY_PORT = 18789
DEFAULT_GATEWAY_HOST = "127.0.0.1"
WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


class GatewayServer:
    """Lightweight async HTTP and WebSocket gateway server listening on port 18789.

    Complies with OpenClaw Gateway specifications for multi-channel clients,
    external web UIs, and real-time agent dispatching.
    """

    def __init__(
        self,
        host: str = DEFAULT_GATEWAY_HOST,
        port: int = DEFAULT_GATEWAY_PORT,
        engine: Optional[Any] = None,
        tool_registry: Optional[Any] = None,
        session_store: Optional[SessionStore] = None,
        config: Optional[Any] = None,
        event_bus: Optional[Any] = None,
        router: Optional[Any] = None,
    ) -> None:
        self.host = host
        self.port = port
        self.config = config
        self.event_bus = event_bus
        self.router = router
        self.engine = engine
        self.tool_registry = tool_registry
        self.session_store = session_store

        self.server: Optional[asyncio.Server] = None
        self._active_sessions: Dict[str, SessionState] = {}
        self._running = False

    def _ensure_dependencies(self) -> None:
        """Lazily initialize missing subsystems if not injected."""
        if self.config is None:
            from ..kernel.config import HarnessConfig
            self.config = HarnessConfig()

        if self.event_bus is None:
            from ..kernel.events import EventBus
            self.event_bus = EventBus()

        if self.router is None:
            from ..providers.keyring import Keyring
            from ..providers.router import ProviderRouter
            keyring = Keyring()
            self.router = ProviderRouter(keyring=keyring, config=self.config)

        if self.tool_registry is None:
            from ..adapters.registry import MasterToolRegistry
            self.tool_registry = MasterToolRegistry(config=self.config, event_bus=self.event_bus)
            self.tool_registry.discover_all()

        if self.session_store is None:
            self.session_store = SessionStore()

        if self.engine is None:
            from ..kernel.engine import KernelEngine
            self.engine = KernelEngine(
                config=self.config,
                event_bus=self.event_bus,
                provider_router=self.router,
                tool_registry=self.tool_registry,
            )

    async def _get_tools_data(self) -> List[Dict[str, Any]]:
        """Retrieve list of discovered tools formatted for API consumers."""
        self._ensure_dependencies()
        tools: List[Any] = []
        if hasattr(self.tool_registry, "list_tools"):
            tools = self.tool_registry.list_tools()
        elif hasattr(self.tool_registry, "_tools"):
            tools = list(self.tool_registry._tools.values())

        tool_list: List[Dict[str, Any]] = []
        for t in tools:
            if isinstance(t, dict):
                tool_list.append(t)
            else:
                tool_list.append({
                    "name": getattr(t, "name", ""),
                    "description": getattr(t, "description", ""),
                    "parameters": getattr(t, "parameters", {}),
                    "source": getattr(t, "source", "native"),
                    "enabled": getattr(t, "enabled", True),
                })
        return tool_list

    async def _handle_chat_request(self, body_bytes: bytes) -> Dict[str, Any]:
        """Process turn request, invoking KernelEngine and updating SessionStore."""
        self._ensure_dependencies()
        try:
            data = json.loads(body_bytes.decode("utf-8")) if body_bytes else {}
        except Exception as e:
            return {"error": f"Invalid JSON payload: {e}", "response": "", "tool_calls": []}

        user_input = (
            data.get("message")
            or data.get("prompt")
            or data.get("content")
            or data.get("input")
            or ""
        )
        session_id = data.get("session_id")
        model = data.get("model")
        mode = data.get("mode")
        max_iterations = int(data.get("max_tool_iterations", 10))

        # Retrieve or initialize SessionState
        state: Optional[SessionState] = None
        if session_id:
            if session_id in self._active_sessions:
                state = self._active_sessions[session_id]
            elif self.session_store:
                state = self.session_store.load_session(session_id)

        if state is None:
            state = SessionState(session_id=session_id or str(uuid.uuid4()))
            if model:
                state.scratchpad["model"] = model
            if mode:
                state.scratchpad["mode"] = mode

        self._active_sessions[state.session_id] = state
        pre_turn_message_count = len(state.messages)

        # Run conversational turn
        try:
            response_text = await self.engine.run_turn(
                state=state,
                user_input=user_input,
                max_tool_iterations=max_iterations,
                model=model,
            )
        except Exception as err:
            logger.error(f"Error during engine turn execution: {err}")
            response_text = f"Error during execution: {err}"

        # Collect tool calls made during this turn
        tool_calls: List[Dict[str, Any]] = []
        for msg in state.messages[pre_turn_message_count:]:
            tc = getattr(msg, "tool_calls", None)
            if tc:
                if isinstance(tc, list):
                    tool_calls.extend(tc)
                else:
                    tool_calls.append(tc)

        # Persist updated session state
        if self.session_store:
            try:
                self.session_store.save_session(state=state, model=model, mode=mode)
            except Exception as e:
                logger.warning(f"Could not persist session {state.session_id}: {e}")

        return {
            "response": response_text,
            "tool_calls": tool_calls,
            "session_id": state.session_id,
        }

    # -----------------------------------------------------------------------
    # HTTP Handlers & Serialization Helpers
    # -----------------------------------------------------------------------
    def _send_json(self, writer: asyncio.StreamWriter, status: int, data: Any) -> None:
        """Write HTTP JSON response with CORS headers."""
        body = json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")
        status_text = {
            200: "OK",
            201: "Created",
            204: "No Content",
            400: "Bad Request",
            403: "Forbidden",
            404: "Not Found",
            500: "Internal Server Error",
        }.get(status, "OK")

        headers = [
            f"HTTP/1.1 {status} {status_text}",
            "Content-Type: application/json; charset=utf-8",
            f"Content-Length: {len(body)}",
            "Connection: close",
            "Access-Control-Allow-Origin: *",
            "Access-Control-Allow-Methods: GET, POST, OPTIONS, DELETE, PUT",
            "Access-Control-Allow-Headers: Content-Type, Authorization, X-Requested-With",
        ]
        raw = "\r\n".join(headers) + "\r\n\r\n"
        writer.write(raw.encode("utf-8") + body)

    def _send_cors_preflight(self, writer: asyncio.StreamWriter) -> None:
        """Write HTTP 204 No Content for CORS preflight OPTIONS."""
        headers = [
            "HTTP/1.1 204 No Content",
            "Connection: close",
            "Access-Control-Allow-Origin: *",
            "Access-Control-Allow-Methods: GET, POST, OPTIONS, DELETE, PUT",
            "Access-Control-Allow-Headers: Content-Type, Authorization, X-Requested-With",
            "Access-Control-Max-Age: 86400",
            "Content-Length: 0",
        ]
        raw = "\r\n".join(headers) + "\r\n\r\n"
        writer.write(raw.encode("utf-8"))

    # -----------------------------------------------------------------------
    # WebSocket RFC 6455 Framing & Loop
    # -----------------------------------------------------------------------
    async def _handle_websocket_handshake(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        headers: Dict[str, str],
    ) -> None:
        """Perform standard WebSocket upgrade handshake."""
        key = headers.get("sec-websocket-key", "")
        if not key:
            self._send_json(writer, 400, {"error": "Missing Sec-WebSocket-Key"})
            await writer.drain()
            writer.close()
            await writer.wait_closed()
            return

        accept_val = base64.b64encode(
            hashlib.sha1((key + WS_GUID).encode("utf-8")).digest()
        ).decode("utf-8")

        handshake_resp = (
            "HTTP/1.1 101 Switching Protocols\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Accept: {accept_val}\r\n"
            "\r\n"
        )
        writer.write(handshake_resp.encode("utf-8"))
        await writer.drain()

        await self._websocket_loop(reader, writer)

    async def _read_ws_frame(
        self, reader: asyncio.StreamReader
    ) -> Tuple[Optional[int], Optional[bytes]]:
        """Read and decode a single WebSocket RFC 6455 frame."""
        try:
            b1_b2 = await reader.readexactly(2)
        except Exception:
            return None, None

        b1, b2 = b1_b2[0], b1_b2[1]
        opcode = b1 & 0x0F
        is_masked = bool(b2 & 0x80)
        payload_len = b2 & 0x7F

        if payload_len == 126:
            try:
                ext_len_bytes = await reader.readexactly(2)
                payload_len = int.from_bytes(ext_len_bytes, "big")
            except Exception:
                return None, None
        elif payload_len == 127:
            try:
                ext_len_bytes = await reader.readexactly(8)
                payload_len = int.from_bytes(ext_len_bytes, "big")
            except Exception:
                return None, None

        mask_key = b""
        if is_masked:
            try:
                mask_key = await reader.readexactly(4)
            except Exception:
                return None, None

        try:
            payload = await reader.readexactly(payload_len) if payload_len > 0 else b""
        except Exception:
            return None, None

        if is_masked and mask_key:
            payload = bytes(b ^ mask_key[i % 4] for i, b in enumerate(payload))

        return opcode, payload

    def _send_ws_frame(
        self, writer: asyncio.StreamWriter, opcode: int, payload: bytes
    ) -> None:
        """Encode and send unmasked server-to-client WebSocket frame."""
        b1 = 0x80 | (opcode & 0x0F)
        payload_len = len(payload)
        if payload_len <= 125:
            header = bytes([b1, payload_len])
        elif payload_len <= 65535:
            header = bytes([b1, 126]) + payload_len.to_bytes(2, "big")
        else:
            header = bytes([b1, 127]) + payload_len.to_bytes(8, "big")

        writer.write(header + payload)

    async def _websocket_loop(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        """Bidirectional WebSocket dispatching loop."""
        try:
            while True:
                opcode, payload = await self._read_ws_frame(reader)
                if opcode is None or opcode == 0x8:  # Connection close
                    self._send_ws_frame(writer, 0x8, b"")
                    await writer.drain()
                    break
                elif opcode == 0x9:  # Ping frame
                    self._send_ws_frame(writer, 0xA, payload or b"")  # Pong frame
                    await writer.drain()
                elif opcode == 0x1:  # Text frame
                    text = (payload or b"").decode("utf-8", errors="replace")
                    try:
                        msg_data = json.loads(text)
                    except Exception:
                        msg_data = {"message": text}

                    action = msg_data.get("action", "chat")
                    if action == "ping":
                        resp_data = {"event": "pong", "time": time.time()}
                    elif action == "health":
                        resp_data = {"status": "ok", "app": "Omni"}
                    elif action == "tools":
                        resp_data = {"tools": await self._get_tools_data()}
                    elif action == "sessions":
                        self._ensure_dependencies()
                        sessions = (
                            self.session_store.list_sessions()
                            if self.session_store
                            else []
                        )
                        resp_data = {"sessions": sessions}
                    else:
                        resp_data = await self._handle_chat_request(payload or b"")

                    resp_bytes = json.dumps(resp_data, ensure_ascii=False).encode("utf-8")
                    self._send_ws_frame(writer, 0x1, resp_bytes)
                    await writer.drain()
        except Exception as e:
            logger.debug(f"WebSocket session terminated: {e}")
        finally:
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass

    # -----------------------------------------------------------------------
    # Connection Dispatcher
    # -----------------------------------------------------------------------
    async def _handle_client(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        """Handle incoming TCP client connection (HTTP or WebSocket)."""
        try:
            line_bytes = await reader.readline()
            if not line_bytes:
                writer.close()
                await writer.wait_closed()
                return

            req_line = line_bytes.decode("utf-8", errors="replace").strip()
            if not req_line:
                writer.close()
                await writer.wait_closed()
                return

            parts = req_line.split()
            if len(parts) < 2:
                self._send_json(writer, 400, {"error": "Malformed request line"})
                await writer.drain()
                writer.close()
                await writer.wait_closed()
                return

            method = parts[0].upper()
            full_url = parts[1]

            # Read headers
            headers: Dict[str, str] = {}
            while True:
                hdr_line = await reader.readline()
                if not hdr_line or hdr_line in (b"\r\n", b"\n"):
                    break
                decoded_hdr = hdr_line.decode("utf-8", errors="replace").strip()
                if ":" in decoded_hdr:
                    k, v = decoded_hdr.split(":", 1)
                    headers[k.strip().lower()] = v.strip()

            # Handle WebSocket upgrade
            if headers.get("upgrade", "").lower() == "websocket":
                await self._handle_websocket_handshake(reader, writer, headers)
                return

            # Read body for POST/PUT requests
            content_length = int(headers.get("content-length", 0))
            body_bytes = b""
            if content_length > 0:
                body_bytes = await reader.readexactly(content_length)

            # URL parsing
            parsed_url = urllib.parse.urlparse(full_url)
            path = parsed_url.path.rstrip("/") or "/"
            query_params = urllib.parse.parse_qs(parsed_url.query)

            # Handle CORS preflight
            if method == "OPTIONS":
                self._send_cors_preflight(writer)
                await writer.drain()
                writer.close()
                await writer.wait_closed()
                return

            # Route Endpoints
            if method == "GET" and path in ("/health", "/"):
                self._send_json(writer, 200, {"status": "ok", "app": "Omni"})

            elif method == "GET" and path == "/api/tools":
                tools = await self._get_tools_data()
                self._send_json(writer, 200, tools)

            elif method == "POST" and path == "/api/chat":
                chat_res = await self._handle_chat_request(body_bytes)
                self._send_json(writer, 200, chat_res)

            elif method == "GET" and path == "/api/sessions":
                self._ensure_dependencies()
                limit_val = query_params.get("limit", [20])[0]
                try:
                    limit = int(limit_val)
                except ValueError:
                    limit = 20
                sessions = (
                    self.session_store.list_sessions(limit=limit)
                    if self.session_store
                    else []
                )
                self._send_json(writer, 200, sessions)

            else:
                self._send_json(
                    writer,
                    404,
                    {"error": f"Endpoint not found: {method} {path}"},
                )

            await writer.drain()
        except Exception as err:
            logger.error(f"Error handling HTTP connection: {err}")
            try:
                self._send_json(writer, 500, {"error": str(err)})
                await writer.drain()
            except Exception:
                pass
        finally:
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass

    # -----------------------------------------------------------------------
    # Server Lifecycle Management
    # -----------------------------------------------------------------------
    async def start(self) -> asyncio.Server:
        """Start async server listening on configured host and port."""
        self._ensure_dependencies()
        self.server = await asyncio.start_server(
            self._handle_client,
            host=self.host,
            port=self.port,
        )
        self._running = True
        logger.info(f"GatewayServer listening on http://{self.host}:{self.port}")
        return self.server

    async def stop(self) -> None:
        """Gracefully terminate server and pending sockets."""
        self._running = False
        if self.server is not None:
            self.server.close()
            await self.server.wait_closed()
            self.server = None

    async def serve_forever(self) -> None:
        """Start server and keep loop active until interrupted."""
        await self.start()
        if self.server is not None:
            async with self.server:
                await self.server.serve_forever()

    def run(self) -> None:
        """Synchronous convenience entrypoint for running server."""
        try:
            asyncio.run(self.serve_forever())
        except KeyboardInterrupt:
            pass
