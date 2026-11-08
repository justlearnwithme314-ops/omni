from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import subprocess
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

from .base import CanonicalTool, DiscoveryResult

logger = logging.getLogger("omni.adapters.mcp")


class MCPServerConnection:
    """Manages an active MCP (Model Context Protocol) subprocess server over JSON-RPC 2.0 stdio."""

    def __init__(
        self,
        name: str,
        command: str,
        args: Optional[List[str]] = None,
        env: Optional[Dict[str, str]] = None,
        cwd: Optional[str] = None,
    ):
        self.name = name
        self.command = command
        self.args = args or []
        self.env = env or {}
        self.cwd = cwd
        self.server_info: Dict[str, Any] = {}
        self._proc: Optional[subprocess.Popen] = None
        self._lock = threading.Lock()
        self._req_id = 0

    def start(self, timeout: float = 20.0) -> bool:
        """Start server process and execute JSON-RPC 2.0 initialize handshake."""
        with self._lock:
            if self._proc is not None and self._proc.poll() is None:
                return True

            executable = shutil.which(self.command)
            # On Windows, also check executable or command as-is
            cmd_path = executable or self.command

            cmd_list = [cmd_path] + self.args
            merged_env = os.environ.copy()
            merged_env.update(self.env)

            try:
                self._proc = subprocess.Popen(
                    cmd_list,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    cwd=self.cwd,
                    env=merged_env,
                    bufsize=1,
                )
            except Exception as e:
                logger.error(f"MCP: Failed to spawn process '{self.command}' for server '{self.name}': {e}")
                self._proc = None
                return False

            try:
                # 1. Send initialize request
                init_resp = self._send_request_locked(
                    "initialize",
                    {
                        "protocolVersion": "2024-11-05",
                        "capabilities": {
                            "roots": {"listChanged": True},
                            "sampling": {},
                        },
                        "clientInfo": {
                            "name": "omni-harness",
                            "version": "1.0.0",
                        },
                    },
                )
                self.server_info = init_resp.get("serverInfo", {}) if isinstance(init_resp, dict) else {}

                # 2. Send initialized notification (notification has no id, no response expected)
                init_notif = {
                    "jsonrpc": "2.0",
                    "method": "notifications/initialized",
                }
                self._write_line(json.dumps(init_notif))
                logger.info(f"MCP: Server '{self.name}' initialized successfully ({self.server_info})")
                return True
            except Exception as e:
                logger.error(f"MCP: Handshake failed for server '{self.name}': {e}")
                self._close_locked()
                return False

    def _write_line(self, line: str):
        if not self._proc or not self._proc.stdin:
            raise RuntimeError(f"Server '{self.name}' process is not running")
        self._proc.stdin.write(line + "\n")
        self._proc.stdin.flush()

    def _read_response(self, expected_id: int) -> Dict[str, Any]:
        if not self._proc or not self._proc.stdout:
            raise RuntimeError(f"Server '{self.name}' process is not running")

        while True:
            line = self._proc.stdout.readline()
            if not line:
                stderr_output = self._proc.stderr.read() if self._proc.stderr else ""
                raise RuntimeError(
                    f"MCP server '{self.name}' closed connection unexpectedly: {stderr_output}"
                )

            line_str = line.strip()
            if not line_str:
                continue

            # Skip non-JSON log lines
            if not (line_str.startswith("{") and line_str.endswith("}")):
                continue

            try:
                msg = json.loads(line_str)
            except Exception:
                continue

            # Check if this message is our response
            if msg.get("id") == expected_id:
                if "error" in msg:
                    err = msg["error"]
                    err_msg = err.get("message") if isinstance(err, dict) else str(err)
                    raise RuntimeError(f"MCP Server Error: {err_msg}")
                return msg.get("result", {})

    def _send_request_locked(self, method: str, params: Optional[Dict[str, Any]] = None) -> Any:
        self._req_id += 1
        req_id = self._req_id

        msg = {
            "jsonrpc": "2.0",
            "id": req_id,
            "method": method,
            "params": params or {},
        }
        self._write_line(json.dumps(msg))
        return self._read_response(req_id)

    def list_tools(self) -> List[Dict[str, Any]]:
        """Request list of available tools from MCP server."""
        with self._lock:
            result = self._send_request_locked("tools/list", {})
            if isinstance(result, dict):
                return result.get("tools", [])
            elif isinstance(result, list):
                return result
            return []

    def call_tool(self, name: str, arguments: Dict[str, Any]) -> str:
        """Call a tool on MCP server and format content response."""
        with self._lock:
            result = self._send_request_locked("tools/call", {
                "name": name,
                "arguments": arguments or {},
            })

            # Format result content
            if isinstance(result, dict):
                content = result.get("content")
                if isinstance(content, list):
                    parts = []
                    for item in content:
                        if isinstance(item, dict):
                            if item.get("type") == "text":
                                parts.append(item.get("text", ""))
                            elif "text" in item:
                                parts.append(str(item["text"]))
                            else:
                                parts.append(json.dumps(item))
                        else:
                            parts.append(str(item))
                    return "\n".join(parts)
                elif isinstance(content, str):
                    return content
                return json.dumps(result)
            elif isinstance(result, str):
                return result
            elif result is not None:
                return json.dumps(result)
            return ""

    def _close_locked(self):
        if self._proc is not None:
            try:
                self._proc.terminate()
                self._proc.wait(timeout=2.0)
            except Exception:
                try:
                    self._proc.kill()
                except Exception:
                    pass
            self._proc = None

    def close(self):
        with self._lock:
            self._close_locked()


class MCPAdapter:
    """Adapter for Model Context Protocol (MCP) servers defined in JSON config files."""

    def __init__(
        self,
        root_dir: Optional[Union[str, Path]] = None,
        config: Optional[Any] = None,
        event_bus: Optional[Any] = None,
    ):
        if root_dir is not None:
            self.root_dir = Path(root_dir).resolve()
        else:
            self.root_dir = Path(__file__).resolve().parent.parent.parent

        self.config = config
        self.event_bus = event_bus
        self.tools: List[CanonicalTool] = []
        self.connections: Dict[str, MCPServerConnection] = {}

    def __del__(self):
        self.close_all()

    def close_all(self):
        """Terminate all active MCP server connections."""
        for conn in self.connections.values():
            try:
                conn.close()
            except Exception:
                pass
        self.connections.clear()

    def _parse_server_configs(self, config_file: Path) -> List[Dict[str, Any]]:
        """Extract server definitions from various JSON config formats."""
        try:
            content = config_file.read_text(encoding="utf-8")
            data = json.loads(content)
        except Exception as e:
            logger.error(f"MCP: Failed to read JSON config at {config_file}: {e}")
            return []

        servers: List[Dict[str, Any]] = []

        if isinstance(data, dict):
            # Format 1: Claude Desktop format with "mcpServers"
            if "mcpServers" in data and isinstance(data["mcpServers"], dict):
                for s_name, s_cfg in data["mcpServers"].items():
                    if isinstance(s_cfg, dict) and "command" in s_cfg:
                        servers.append({
                            "name": s_name,
                            "command": s_cfg["command"],
                            "args": s_cfg.get("args", []),
                            "env": s_cfg.get("env", {}),
                            "cwd": s_cfg.get("cwd"),
                        })
            # Format 2: "servers" list or dict
            elif "servers" in data:
                s_list = data["servers"]
                if isinstance(s_list, list):
                    for idx, s_cfg in enumerate(s_list):
                        if isinstance(s_cfg, dict) and "command" in s_cfg:
                            servers.append({
                                "name": s_cfg.get("name", f"server-{idx}"),
                                "command": s_cfg["command"],
                                "args": s_cfg.get("args", []),
                                "env": s_cfg.get("env", {}),
                                "cwd": s_cfg.get("cwd"),
                            })
                elif isinstance(s_list, dict):
                    for s_name, s_cfg in s_list.items():
                        if isinstance(s_cfg, dict) and "command" in s_cfg:
                            servers.append({
                                "name": s_name,
                                "command": s_cfg["command"],
                                "args": s_cfg.get("args", []),
                                "env": s_cfg.get("env", {}),
                                "cwd": s_cfg.get("cwd"),
                            })
            # Format 3: Direct single server definition {"command": "npx", "args": [...]}
            elif "command" in data:
                s_name = data.get("name") or config_file.stem
                servers.append({
                    "name": s_name,
                    "command": data["command"],
                    "args": data.get("args", []),
                    "env": data.get("env", {}),
                    "cwd": data.get("cwd"),
                })
        elif isinstance(data, list):
            for idx, item in enumerate(data):
                if isinstance(item, dict) and "command" in item:
                    servers.append({
                        "name": item.get("name", f"{config_file.stem}-{idx}"),
                        "command": item["command"],
                        "args": item.get("args", []),
                        "env": item.get("env", {}),
                        "cwd": item.get("cwd"),
                    })

        return servers

    def _make_tool_handler(self, conn: MCPServerConnection, tool_name: str) -> Any:
        async def handler(args: dict, context: dict = None) -> str:
            def _call():
                return conn.call_tool(tool_name, args if args is not None else {})

            return await asyncio.to_thread(_call)

        return handler

    def discover(self) -> DiscoveryResult:
        """Scans plugins/mcp/ for .json configs, initializes servers, and lists tools."""
        discovered: List[CanonicalTool] = []
        plugins_dir = self.root_dir / "plugins" / "mcp"

        if not plugins_dir.is_dir():
            self.tools = []
            return DiscoveryResult([])

        for config_file in plugins_dir.glob("*.json"):
            if not config_file.is_file():
                continue

            servers = self._parse_server_configs(config_file)
            for s_cfg in servers:
                s_name = s_cfg["name"]
                cmd = s_cfg["command"]
                args = s_cfg.get("args", [])
                env = s_cfg.get("env", {})
                cwd = s_cfg.get("cwd")

                conn = MCPServerConnection(
                    name=s_name,
                    command=cmd,
                    args=args,
                    env=env,
                    cwd=cwd,
                )

                started = conn.start()
                if not started:
                    continue

                self.connections[s_name] = conn

                try:
                    tools_list = conn.list_tools()
                    for t_def in tools_list:
                        tool_name = t_def.get("name")
                        if not tool_name:
                            continue

                        description = t_def.get("description", "")
                        parameters = (
                            t_def.get("inputSchema")
                            or t_def.get("parameters")
                            or {"type": "object", "properties": {}}
                        )

                        tool = CanonicalTool(
                            name=tool_name,
                            description=description,
                            parameters=parameters,
                            handler=self._make_tool_handler(conn, tool_name),
                            source="mcp",
                            metadata={
                                "server_name": s_name,
                                "config_file": str(config_file),
                                "server_info": conn.server_info,
                            },
                        )
                        discovered.append(tool)
                        logger.info(f"MCPAdapter: Registered tool '{tool_name}' from server '{s_name}'")
                except Exception as e:
                    logger.error(f"MCPAdapter: Failed listing tools from server '{s_name}': {e}", exc_info=True)

        self.tools = discovered
        return DiscoveryResult(discovered)
