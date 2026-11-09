from __future__ import annotations
from .omni import OmniAdapter

import asyncio
import json
import logging
import os
import shutil
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

from .base import CanonicalTool, DiscoveryResult
from .hermes import HermesAdapter
from .mcp import MCPAdapter
from .openclaw import OpenClawAdapter
from .pi import PiAdapter

logger = logging.getLogger("omni.adapters.registry")


class MasterToolRegistry:
    """Master registry aggregating tools across OpenClaw, Hermes, Pi, MCP, and Native adapters."""

    def __init__(
        self,
        config: Optional[Any] = None,
        event_bus: Optional[Any] = None,
        root_dir: Optional[Union[str, Path]] = None,
    ):
        if root_dir is not None:
            self.root_dir = Path(root_dir).resolve()
        else:
            self.root_dir = Path(__file__).resolve().parent.parent.parent

        self.config = config
        self.event_bus = event_bus
        self._tools: Dict[str, CanonicalTool] = {}

        # If config is None, attempt loading harness.config.json from root_dir
        if self.config is None:
            config_file = self.root_dir / "harness.config.json"
            if config_file.is_file():
                try:
                    self.config = json.loads(config_file.read_text(encoding="utf-8"))
                except Exception as e:
                    logger.warning(f"Could not load harness.config.json: {e}")

        # Adapters
        self.openclaw_adapter = OpenClawAdapter(
            root_dir=self.root_dir,
            config=self.config,
            event_bus=self.event_bus,
        )
        self.hermes_adapter = HermesAdapter(
            root_dir=self.root_dir,
            config=self.config,
            event_bus=self.event_bus,
        )
        self.pi_adapter = PiAdapter(
            root_dir=self.root_dir,
            config=self.config,
            event_bus=self.event_bus,
        )
        self.mcp_adapter = MCPAdapter(
            root_dir=self.root_dir,
            config=self.config,
            event_bus=self.event_bus,
        )

    def _is_feature_enabled(self, feature_name: str) -> bool:
        """Checks if a feature is enabled on config object, config dict, or default."""
        cfg = self.config
        if cfg is not None:
            # 1. Config object method is_feature_enabled()
            if hasattr(cfg, "is_feature_enabled") and callable(cfg.is_feature_enabled):
                return bool(cfg.is_feature_enabled(feature_name))

            # 2. Config object attribute .features
            if hasattr(cfg, "features"):
                feat = cfg.features
                if isinstance(feat, dict):
                    return bool(feat.get(feature_name, False))
                if hasattr(feat, feature_name):
                    return bool(getattr(feat, feature_name, False))

            # 3. Config dictionary
            if isinstance(cfg, dict):
                feat = cfg.get("features", {})
                if isinstance(feat, dict) and feature_name in feat:
                    return bool(feat.get(feature_name))
                if feature_name in cfg:
                    return bool(cfg.get(feature_name))

        # Default to enabled if not explicitly specified
        return True

    def _register_native_tools(self):
        """Registers native core tools: read_file, write_file, execute_bash."""

        async def read_file_handler(args: dict, context: dict = None) -> str:
            path_str = args.get("path")
            if not path_str:
                raise ValueError("Parameter 'path' is required for read_file")

            file_path = Path(path_str).expanduser().resolve()
            if not file_path.is_file():
                raise FileNotFoundError(f"File not found: {path_str}")

            offset = int(args.get("offset", 1))
            limit = args.get("limit")
            limit_val = int(limit) if limit is not None else None

            text = file_path.read_text(encoding="utf-8", errors="replace")
            lines = text.splitlines(keepends=True)

            start_idx = max(0, offset - 1)
            if limit_val is not None:
                end_idx = start_idx + max(0, limit_val)
                sliced = lines[start_idx:end_idx]
            else:
                sliced = lines[start_idx:]

            return "".join(sliced)

        async def write_file_handler(args: dict, context: dict = None) -> str:
            path_str = args.get("path")
            if not path_str:
                raise ValueError("Parameter 'path' is required for write_file")

            content = args.get("content", "")
            file_path = Path(path_str).expanduser().resolve()
            file_path.parent.mkdir(parents=True, exist_ok=True)
            file_path.write_text(content, encoding="utf-8")
            return f"Successfully wrote {len(content)} characters to {path_str}"

        async def execute_bash_handler(args: dict, context: dict = None) -> str:
            cmd = args.get("command")
            if not cmd:
                raise ValueError("Parameter 'command' is required for execute_bash")

            cwd = args.get("cwd")
            timeout = args.get("timeout")
            timeout_sec = float(timeout) if timeout is not None else None

            bash_bin = shutil.which("bash")
            if bash_bin:
                proc = await asyncio.create_subprocess_exec(
                    bash_bin,
                    "-c",
                    cmd,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    cwd=cwd,
                )
            else:
                proc = await asyncio.create_subprocess_shell(
                    cmd,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    cwd=cwd,
                )

            try:
                if timeout_sec:
                    stdout, stderr = await asyncio.wait_for(
                        proc.communicate(),
                        timeout=timeout_sec,
                    )
                else:
                    stdout, stderr = await proc.communicate()
            except asyncio.TimeoutError:
                try:
                    proc.kill()
                except Exception:
                    pass
                raise TimeoutError(f"Command '{cmd}' timed out after {timeout_sec} seconds")

            out_text = stdout.decode("utf-8", errors="replace").strip()
            err_text = stderr.decode("utf-8", errors="replace").strip()

            if proc.returncode != 0:
                return f"Exit code {proc.returncode}:\n{err_text or out_text}"
            return out_text if out_text else err_text

        # 1. read_file
        self.register_tool(
            CanonicalTool(
                name="read_file",
                description="Reads content from a local file, with optional offset and limit for line slicing.",
                parameters={
                    "type": "object",
                    "properties": {
                        "path": {
                            "type": "string",
                            "description": "Path to the file to read",
                        },
                        "offset": {
                            "type": "integer",
                            "description": "Line number to begin reading from (1-indexed)",
                            "default": 1,
                        },
                        "limit": {
                            "type": "integer",
                            "description": "Maximum number of lines to read",
                        },
                    },
                    "required": ["path"],
                },
                handler=read_file_handler,
                source="native",
            )
        )

        # 2. write_file
        self.register_tool(
            CanonicalTool(
                name="write_file",
                description="Writes content to a file, creating parent directories if they do not exist.",
                parameters={
                    "type": "object",
                    "properties": {
                        "path": {
                            "type": "string",
                            "description": "Path to the file to write",
                        },
                        "content": {
                            "type": "string",
                            "description": "Content to write into the file",
                        },
                    },
                    "required": ["path", "content"],
                },
                handler=write_file_handler,
                source="native",
            )
        )

        # 3. execute_bash
        self.register_tool(
            CanonicalTool(
                name="execute_bash",
                description="Executes a bash shell command and returns stdout/stderr.",
                parameters={
                    "type": "object",
                    "properties": {
                        "command": {
                            "type": "string",
                            "description": "Bash command to execute",
                        },
                        "cwd": {
                            "type": "string",
                            "description": "Optional working directory for the command",
                        },
                        "timeout": {
                            "type": "number",
                            "description": "Optional execution timeout in seconds",
                        },
                    },
                    "required": ["command"],
                },
                handler=execute_bash_handler,
                source="native",
            )
        )

    def register_tool(self, tool: CanonicalTool) -> None:
        """Register a canonical tool into the master registry."""
        if not isinstance(tool, CanonicalTool):
            raise TypeError(f"Expected CanonicalTool instance, got {type(tool).__name__}")
        self._tools[tool.name] = tool

    def get_tool(self, name: str) -> Optional[CanonicalTool]:
        """Retrieve a registered tool by name."""
        return self._tools.get(name)

    def list_tools(self) -> List[CanonicalTool]:
        """List all registered tools."""
        return list(self._tools.values())


    def apply_feature_toggles(self, feature_registry: Any) -> Dict[str, int]:
        """Dynamically enables or disables registered tools based on active feature toggles."""
        is_enabled_fn = getattr(feature_registry, "is_enabled", None) or getattr(feature_registry, "is_feature_enabled", None)
        if is_enabled_fn is None and isinstance(feature_registry, dict):
            is_enabled_fn = lambda k: feature_registry.get(k, True)

        def check_feat(canonical, alias):
            if is_enabled_fn:
                v1 = is_enabled_fn(canonical)
                v2 = is_enabled_fn(alias)
                return bool(v1 or v2)
            return True

        openclaw_active = check_feat("openclaw_skills", "plugins_openclaw")
        hermes_active = check_feat("hermes_plugins", "plugins_hermes")
        pi_active = check_feat("pi_extensions", "plugins_pi")
        mcp_active = check_feat("mcp_servers", "plugins_mcp")

        enabled_counts = {}
        for tool in self._tools.values():
            if tool.source == "openclaw":
                tool.enabled = bool(openclaw_active)
            elif tool.source == "hermes":
                tool.enabled = bool(hermes_active)
            elif tool.source == "pi":
                tool.enabled = bool(pi_active)
            elif tool.source == "mcp":
                tool.enabled = bool(mcp_active)
            elif tool.source in ("native", "omni"):
                tool.enabled = True
            enabled_counts[tool.source] = enabled_counts.get(tool.source, 0) + (1 if tool.enabled else 0)

        return enabled_counts

    def get_openai_schemas(self) -> List[Dict[str, Any]]:
        """Retrieve OpenAI function schemas for all enabled tools."""
        return [
            tool.to_openai_schema()
            for tool in self._tools.values()
            if tool.enabled and tool.is_available()
        ]

    def discover_all(self) -> DiscoveryResult:
        """Discover tools across all plugin systems and apply active feature toggles."""
        # 1. Native core tools
        self._register_native_tools()

        
        from .native_tools import register as _register_core_tools
        _register_core_tools(self)

        # --- 4. Browser DevTools Protocol (CDP) Relay Tool ---
        async def browser_cdp_handler(args: dict, context: dict = None) -> str:
            from ..kernel.services import services
            relay = services.browser
            action = args.get("action", "list_tabs")
            try:
                return await asyncio.to_thread(_browser_action, relay, action, args)
            except Exception as e:  # BrowserNotConnected, CdpError, ...
                return json.dumps({"error": str(e)})

        def _browser_action(relay, action, args):
            if action == "list_tabs":
                tabs = [t.__dict__ for t in relay.list_tabs()]
                return json.dumps({"tabs": tabs})
            elif action == "snapshot_dom":
                return json.dumps(relay.snapshot_dom(tab_id=args.get("tab_id")))
            elif action == "evaluate_js":
                expr = args.get("expression", "document.title")
                return json.dumps({"result": relay.evaluate_js(expr, tab_id=args.get("tab_id"))})
            elif action == "click":
                return json.dumps({"success": relay.click_element(args.get("selector", "body"), tab_id=args.get("tab_id"))})
            return json.dumps({"error": f"Unknown action '{action}'"})

        self.register_tool(
            CanonicalTool(
                name="browser_cdp",
                description="Chrome DevTools Protocol (CDP) bridge to inspect live tabs, snapshot DOM, and evaluate JS in your desktop browser.",
                parameters={
                    "type": "object",
                    "properties": {
                        "action": {"type": "string", "enum": ["list_tabs", "snapshot_dom", "evaluate_js", "click"], "default": "list_tabs"},
                        "expression": {"type": "string"},
                        "selector": {"type": "string"},
                        "tab_id": {"type": "string"},
                    },
                },
                handler=browser_cdp_handler,
                source="omni",
                enabled=True,
            )
        )

        # --- 5. Language Server Protocol (LSP) Intelligence Tool ---
        async def lsp_query_handler(args: dict, context: dict = None) -> str:
            from ..kernel.services import services
            client = services.lsp
            op = args.get("operation", "diagnostics")
            target = args.get("file_path", "src/cli.py")
            res = client.execute_lsp_operation(op, target, query=args.get("query", ""), symbol=args.get("symbol", ""))
            return json.dumps(res)

        self.register_tool(
            CanonicalTool(
                name="lsp_query",
                description="Language Server Protocol (LSP) tool for symbol diagnostics, definition lookup, and symbols.",
                parameters={
                    "type": "object",
                    "properties": {
                        "operation": {"type": "string", "enum": ["diagnostics", "symbols", "definition", "rename"], "default": "diagnostics"},
                        "file_path": {"type": "string"},
                        "symbol": {"type": "string"},
                        "query": {"type": "string"},
                    },
                    "required": ["file_path"],
                },
                handler=lsp_query_handler,
                source="omni",
                enabled=True,
            )
        )

        # --- 6. Visual Canvas Renderer Tool ---
        async def canvas_render_handler(args: dict, context: dict = None) -> str:
            from ..kernel.services import services
            vcm = services.canvas
            try:
                doc = vcm.create_document(
                    title=args.get("title", "Visual Artifact"),
                    content=args.get("content", ""),
                    kind=args.get("kind", "mermaid"),
                )
            except Exception as e:
                return json.dumps({"error": str(e)})
            return json.dumps(dict(doc.to_dict(), url=vcm.url_for(doc.id)))

        self.register_tool(
            CanonicalTool(
                name="canvas_render",
                description="Renders visual diagrams, Mermaid flowcharts, or interactive HTML bundles onto the live visual canvas.",
                parameters={
                    "type": "object",
                    "properties": {
                        "title": {"type": "string"},
                        "content": {"type": "string"},
                        "kind": {"type": "string", "enum": ["mermaid", "html_bundle", "svg"], "default": "mermaid"},
                    },
                    "required": ["title", "content"],
                },
                handler=canvas_render_handler,
                source="omni",
                enabled=True,
            )
        )

        # --- 7. Companion Device Node Task Tool ---
        async def remote_node_task_handler(args: dict, context: dict = None) -> str:
            from ..kernel.services import services
            npm = services.nodes
            node_id = args.get("node_id", "node_local_host")
            cap = args.get("capability", "system.run")
            res = npm.dispatch_remote_task(node_id, cap, args.get("params", {"command": "echo 'ok'"}))
            return json.dumps(res)

        self.register_tool(
            CanonicalTool(
                name="remote_node_task",
                description="Dispatches a capability task (screen capture, notification, command run) to a paired companion device.",
                parameters={
                    "type": "object",
                    "properties": {
                        "node_id": {"type": "string"},
                        "capability": {"type": "string", "enum": ["screen.capture", "system.run", "notification"], "default": "system.run"},
                        "params": {"type": "object"},
                    },
                },
                handler=remote_node_task_handler,
                source="omni",
                enabled=True,
            )
        )

        # 2. OpenClaw adapter (discover all available skills into registry)
        try:
            claw_tools = self.openclaw_adapter.discover()
            for tool in claw_tools:
                self.register_tool(tool)
        except Exception as e:
            logger.error(f"Error during OpenClaw tool discovery: {e}", exc_info=True)

        # 3. Hermes adapter (discover all available plugins into registry)
        try:
            hermes_tools = self.hermes_adapter.discover()
            for tool in hermes_tools:
                self.register_tool(tool)
        except Exception as e:
            logger.error(f"Error during Hermes tool discovery: {e}", exc_info=True)

        # 4. Pi adapter (discover all available extensions into registry)
        try:
            pi_tools = self.pi_adapter.discover()
            for tool in pi_tools:
                self.register_tool(tool)
        except Exception as e:
            logger.error(f"Error during Pi tool discovery: {e}", exc_info=True)

        # 5. MCP adapter
        try:
            mcp_tools = self.mcp_adapter.discover()
            for tool in mcp_tools:
                self.register_tool(tool)
        except Exception as e:
            logger.error(f"Error during MCP tool discovery: {e}", exc_info=True)

        # 6. Native Omni Custom Plugins (plugins/omni/)
        try:
            from .omni import OmniAdapter
            omni_adapter = OmniAdapter(root_dir=self.root_dir, config=self.config, event_bus=self.event_bus)
            for tool in omni_adapter.discover():
                self.register_tool(tool)
        except Exception as e:
            logger.debug(f"OmniAdapter custom plugin error: {e}")

        # Apply active feature toggles to mark tools enabled or disabled
        self.apply_feature_toggles(self.config)

        return DiscoveryResult(self.list_tools())

    async def execute_tool(
        self,
        name: str,
        args: Dict[str, Any],
        context: Optional[Dict[str, Any]] = None,
    ) -> str:
        """Execute a registered tool by name with arguments and optional context."""
        tool = self.get_tool(name)
        if tool is None:
            raise KeyError(f"Tool '{name}' not found in registry")

        if not tool.enabled:
            raise ValueError(f"Tool '{name}' is currently disabled")

        # Emit start event if event_bus is present
        if self.event_bus is not None and hasattr(self.event_bus, "emit"):
            try:
                res = self.event_bus.emit("before_tool_exec", tool_name=name, args=args, context=context)
                if asyncio.iscoroutine(res):
                    await res
            except Exception:
                pass

        try:
            result = await tool.execute(args=args, context=context)

            # Emit success event if event_bus is present
            if self.event_bus is not None and hasattr(self.event_bus, "emit"):
                try:
                    res = self.event_bus.emit(
                        "after_tool_exec",
                        tool_name=name,
                        result=result,
                        context=context,
                    )
                    if asyncio.iscoroutine(res):
                        await res
                except Exception:
                    pass

            return result
        except Exception as e:
            if self.event_bus is not None and hasattr(self.event_bus, "emit"):
                try:
                    res = self.event_bus.emit(
                        "on_error",
                        error=str(e),
                        context={"tool": name, "args": args},
                    )
                    if asyncio.iscoroutine(res):
                        await res
                except Exception:
                    pass
            raise
