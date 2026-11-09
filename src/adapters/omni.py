"""
Native Omni Plugin Adapter (Maximum Extensibility & Freedom)
Allows users to drop custom Python scripts, multi-file packages, or scripts in any language
directly into plugins/omni/ without foreign harness baggage.
"""

from dataclasses import dataclass
import importlib.util
import inspect
import json
import logging
import os
from pathlib import Path
import sys
from typing import Any, Callable, Dict, List, Optional

from .base import CanonicalTool, DiscoveryResult
from ..paths import PROJECT_ROOT

logger = logging.getLogger(__name__)


def omni_tool(name: Optional[str] = None, description: Optional[str] = None, parameters: Optional[Dict[str, Any]] = None):
    """Decorator to turn any python function into an Omni tool automatically."""
    def decorator(fn: Callable):
        tool_name = name or fn.__name__
        tool_desc = description or (fn.__doc__ or "").strip() or f"Omni tool: {tool_name}"

        # If parameters not provided, infer from signature
        tool_params = parameters
        if tool_params is None:
            sig = inspect.signature(fn)
            props = {}
            required = []
            for param_name, param in sig.parameters.items():
                if param_name in ("self", "context"):
                    continue
                param_type = "string"
                if param.annotation is int:
                    param_type = "integer"
                elif param.annotation is float:
                    param_type = "number"
                elif param.annotation is bool:
                    param_type = "boolean"
                elif param.annotation is list or param.annotation == list:
                    param_type = "array"
                elif param.annotation is dict or param.annotation == dict:
                    param_type = "object"

                props[param_name] = {"type": param_type, "description": f"Parameter {param_name}"}
                if param.default is inspect.Parameter.empty:
                    required.append(param_name)

            tool_params = {
                "type": "object",
                "properties": props,
                "required": required,
            }

        fn._is_omni_tool = True
        fn._omni_tool_name = tool_name
        fn._omni_tool_desc = tool_desc
        fn._omni_tool_params = tool_params
        return fn

    return decorator


class OmniPluginContext:
    """Plugin context passed to custom Omni plugins via def register(omni)."""

    def __init__(self, adapter: "OmniAdapter", plugin_dir: Path):
        self.adapter = adapter
        self.plugin_dir = plugin_dir
        self.tools: List[CanonicalTool] = []
        self.commands: Dict[str, Callable] = {}
        self.logger = logging.getLogger(f"omni.plugin.{plugin_dir.name}")

    def register_tool(
        self,
        name: str,
        description: str,
        parameters: Dict[str, Any],
        handler: Callable,
        metadata: Optional[Dict[str, Any]] = None,
    ):
        """Register a canonical tool callable by the AI."""
        async def async_wrapper(args: dict, context: dict = None) -> str:
            sig = inspect.signature(handler)
            param_count = len(sig.parameters)
            if inspect.iscoroutinefunction(handler):
                if param_count == 0:
                    res = await handler()
                elif param_count == 1:
                    res = await handler(args)
                else:
                    res = await handler(args, context or {})
            else:
                if param_count == 0:
                    res = handler()
                elif param_count == 1:
                    res = handler(args)
                else:
                    res = handler(args, context or {})
            return str(res) if not isinstance(res, str) else res

        tool = CanonicalTool(
            name=name,
            description=description,
            parameters=parameters,
            handler=async_wrapper,
            source="omni",
            metadata=metadata or {"plugin_dir": str(self.plugin_dir)},
        )
        self.tools.append(tool)
        logger.info(f"OmniPlugin: Registered tool '{name}'")

    def register_command(self, name: str, handler: Callable):
        """Register a REPL slash command (e.g. /mycmd)."""
        cmd_name = name.lstrip("/")
        self.commands[cmd_name] = handler
        logger.info(f"OmniPlugin: Registered command '/{cmd_name}'")

    def on(self, event_name: str, callback: Callable):
        """Hook into Omni EventBus lifecycle events."""
        if self.adapter.event_bus and hasattr(self.adapter.event_bus, "on"):
            self.adapter.event_bus.on(event_name, callback)


class OmniAdapter:
    """Discovers and registers custom, native Omni plugins from plugins/omni/."""

    def __init__(self, root_dir: Optional[Path] = None, config: Any = None, event_bus: Any = None):
        self.root_dir = Path(root_dir or PROJECT_ROOT).resolve()
        self.config = config
        self.event_bus = event_bus
        self.tools: List[CanonicalTool] = []
        self.commands: Dict[str, Callable] = {}

    def discover(self) -> DiscoveryResult:
        discovered: List[CanonicalTool] = []
        plugins_dir = self.root_dir / "plugins" / "omni"
        if not plugins_dir.is_dir():
            os.makedirs(plugins_dir, exist_ok=True)
            self.tools = []
            return DiscoveryResult([])

        # Scan for single-file python plugins or subdirectories
        for item in sorted(plugins_dir.iterdir()):
            if item.name.startswith((".", "_")):
                continue

            # 1. Single-file Python plugin (e.g. plugins/omni/webui.py or weather.py)
            if item.is_file() and item.suffix.lower() == ".py":
                tools = self._load_python_plugin_file(item)
                discovered.extend(tools)

            # 2. Directory plugin (e.g. plugins/omni/my_plugin/)
            elif item.is_dir():
                init_file = item / "__init__.py"
                main_file = item / "plugin.py"
                entry = init_file if init_file.is_file() else (main_file if main_file.is_file() else None)
                if entry:
                    tools = self._load_python_plugin_file(entry, plugin_dir=item)
                    discovered.extend(tools)

        self.tools = discovered
        return DiscoveryResult(discovered)

    def _load_python_plugin_file(self, py_path: Path, plugin_dir: Optional[Path] = None) -> List[CanonicalTool]:
        tools: List[CanonicalTool] = []
        mod_name = f"omni_custom_{py_path.stem}"
        pdir = plugin_dir or py_path.parent

        try:
            spec = importlib.util.spec_from_file_location(mod_name, str(py_path))
            if not spec or not spec.loader:
                return []
            module = importlib.util.module_from_spec(spec)
            sys.modules[mod_name] = module
            spec.loader.exec_module(module)

            # A. Check for register(omni) function
            register_fn = getattr(module, "register", None)
            if callable(register_fn):
                ctx = OmniPluginContext(adapter=self, plugin_dir=pdir)
                res = register_fn(ctx)
                if inspect.iscoroutine(res):
                    asyncio.create_task(res)
                tools.extend(ctx.tools)
                self.commands.update(ctx.commands)

            # B. Check for @omni_tool decorated functions
            for attr_name in dir(module):
                obj = getattr(module, attr_name)
                if callable(obj) and getattr(obj, "_is_omni_tool", False):
                    ctx = OmniPluginContext(adapter=self, plugin_dir=pdir)
                    ctx.register_tool(
                        name=obj._omni_tool_name,
                        description=obj._omni_tool_desc,
                        parameters=obj._omni_tool_params,
                        handler=obj,
                    )
                    tools.extend(ctx.tools)

        except Exception as e:
            logger.error(f"Failed to load custom Omni plugin at {py_path}: {e}", exc_info=True)

        return tools
