from __future__ import annotations

import importlib.util
import json
import logging
import re
import sys
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Union

from .base import CanonicalTool, DiscoveryResult

logger = logging.getLogger("omni.adapters.hermes")


def format_tool_call(name: str, arguments: Optional[Dict[str, Any]] = None) -> str:
    """Format a tool invocation into Hermes XML <tool_call> structure."""
    payload = {
        "name": name,
        "arguments": arguments or {},
    }
    return f"<tool_call>\n{json.dumps(payload, indent=2)}\n</tool_call>"


def parse_tool_calls(text: str) -> List[Dict[str, Any]]:
    """Parse Hermes XML <tool_call> tags from text, extracting tool name and arguments."""
    results: List[Dict[str, Any]] = []
    pattern = r"<tool_call>([\s\S]*?)</tool_call>"
    matches = re.findall(pattern, text, re.IGNORECASE)

    for block in matches:
        block_str = block.strip()
        if not block_str:
            continue

        # 1. Direct JSON inside <tool_call>
        try:
            data = json.loads(block_str)
            if isinstance(data, dict):
                name = data.get("name") or data.get("tool_name") or data.get("tool")
                args = data.get("arguments") or data.get("parameters") or {}
                if isinstance(args, str):
                    try:
                        args = json.loads(args)
                    except Exception:
                        pass
                if name:
                    results.append({
                        "name": str(name),
                        "arguments": args if isinstance(args, dict) else {"raw": args},
                    })
                    continue
        except Exception:
            pass

        # 2. Tag-based XML elements inside <tool_call>
        name_match = re.search(
            r"<(?:tool_name|name)>([\s\S]*?)</(?:tool_name|name)>",
            block_str,
            re.IGNORECASE,
        )
        args_match = re.search(
            r"<(?:parameters|arguments|args)>([\s\S]*?)</(?:parameters|arguments|args)>",
            block_str,
            re.IGNORECASE,
        )

        if name_match:
            name = name_match.group(1).strip()
            args: Dict[str, Any] = {}
            if args_match:
                raw_args = args_match.group(1).strip()
                try:
                    parsed_args = json.loads(raw_args)
                    if isinstance(parsed_args, dict):
                        args = parsed_args
                    else:
                        args = {"raw": parsed_args}
                except Exception:
                    args = {"raw": raw_args}
            results.append({"name": name, "arguments": args})

    return results


class PluginContext:
    """Context passed to Hermes plugin `register(ctx)` entrypoint."""

    def __init__(
        self,
        plugin_name: str,
        config: Optional[Dict[str, Any]] = None,
        event_bus: Optional[Any] = None,
    ):
        self.plugin_name = plugin_name
        self._config = config or {}
        self.event_bus = event_bus
        self.tools: List[CanonicalTool] = []
        self.hooks: Dict[str, List[Callable]] = {}
        self.logger = logging.getLogger(f"hermes.plugin.{plugin_name}")

    def register_tool(
        self,
        *args,
        **kwargs,
    ) -> CanonicalTool:
        """Register a tool from a Hermes plugin.
        
        Supports:
        - ctx.register_tool(CanonicalTool(...))
        - ctx.register_tool("sha256_hash", schema, handler)
        - ctx.register_tool(name="sha256_hash", schema=..., handler=...)
        """
        if len(args) == 1 and isinstance(args[0], CanonicalTool):
            tool = args[0]
            self.tools.append(tool)
            return tool

        name = kwargs.get("name")
        description = kwargs.get("description", "")
        parameters = kwargs.get("parameters", {"type": "object", "properties": {}})
        handler = kwargs.get("handler")
        metadata = kwargs.get("metadata", {})

        if len(args) >= 3:
            name = args[0]
            schema = args[1]
            handler = args[2]
            if isinstance(schema, dict):
                name = schema.get("name", name)
                description = schema.get("description", description)
                parameters = schema.get("parameters", schema)
        elif len(args) == 2:
            if callable(args[1]):
                name = args[0]
                handler = args[1]
            elif isinstance(args[0], dict) and callable(args[1]):
                schema = args[0]
                handler = args[1]
                name = schema.get("name", name or "unnamed_tool")
                description = schema.get("description", description)
                parameters = schema.get("parameters", schema)
            elif isinstance(args[1], dict):
                name = args[0]
                schema = args[1]
                description = schema.get("description", description)
                parameters = schema.get("parameters", schema)
        elif len(args) == 1:
            if isinstance(args[0], dict):
                schema = args[0]
                name = schema.get("name", name or "unnamed_tool")
                description = schema.get("description", description)
                parameters = schema.get("parameters", schema)
                handler = schema.get("handler", handler)

        if not name:
            name = "unnamed_tool"

        tool_metadata = {"plugin": self.plugin_name}
        if isinstance(metadata, dict):
            tool_metadata.update(metadata)

        tool = CanonicalTool(
            name=name,
            description=description,
            parameters=parameters if isinstance(parameters, dict) else {"type": "object", "properties": {}},
            handler=handler,
            source="hermes",
            metadata=tool_metadata,
        )
        self.tools.append(tool)
        return tool

    def register_hook(self, name: str, callback: Callable) -> None:
        """Register an event hook and bridge it to kernel EventBus if available."""
        self.hooks.setdefault(name, []).append(callback)
        if self.event_bus is not None:
            if hasattr(self.event_bus, "subscribe"):
                self.event_bus.subscribe(name, callback)
            elif hasattr(self.event_bus, "on"):
                self.event_bus.on(name, callback)
            elif hasattr(self.event_bus, "register_hook"):
                self.event_bus.register_hook(name, callback)
            elif hasattr(self.event_bus, "add_listener"):
                self.event_bus.add_listener(name, callback)

    def get_config(self, key: Optional[str] = None, default: Any = None) -> Any:
        """Retrieve plugin-specific configuration."""
        if key is None:
            return self._config
        return self._config.get(key, default)


class HermesAdapter:
    """Adapter for Hermes plugins with YAML/JSON manifest and Python module hooks."""

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
        self.contexts: Dict[str, PluginContext] = {}

    @staticmethod
    def format_tool_call(name: str, arguments: Optional[Dict[str, Any]] = None) -> str:
        return format_tool_call(name, arguments)

    @staticmethod
    def parse_tool_calls(text: str) -> List[Dict[str, Any]]:
        return parse_tool_calls(text)

    def _read_manifest(self, folder: Path) -> Optional[Dict[str, Any]]:
        """Read plugin.yaml or plugin.json from plugin folder."""
        for filename in ["plugin.yaml", "plugin.yml", "plugin.json"]:
            manifest_file = folder / filename
            if manifest_file.is_file():
                try:
                    text = manifest_file.read_text(encoding="utf-8")
                    if filename.endswith(".json"):
                        return json.loads(text)
                    else:
                        try:
                            import yaml
                            return yaml.safe_load(text)
                        except Exception:
                            data = {}
                            for line in text.splitlines():
                                line_s = line.strip()
                                if line_s and ":" in line_s and not line_s.startswith("#"):
                                    k, v = line_s.split(":", 1)
                                    data[k.strip()] = v.strip().strip("\"'")
                            return data
                except Exception as e:
                    logger.warning(f"Hermes: Error reading manifest {manifest_file}: {e}")
        return None

    def discover(self) -> DiscoveryResult:
        """Scans plugins/hermes/ for folders containing plugin.yaml/json and python modules."""
        discovered: List[CanonicalTool] = []
        plugins_dir = self.root_dir / "plugins" / "hermes"

        if not plugins_dir.is_dir():
            self.tools = []
            return DiscoveryResult([])

        for folder in plugins_dir.iterdir():
            if not folder.is_dir():
                continue

            manifest = self._read_manifest(folder)
            init_file = folder / "__init__.py"

            # Must have manifest and python module
            if manifest is None and not init_file.is_file():
                continue

            plugin_name = (manifest.get("name") if manifest else None) or folder.name
            plugin_config = (manifest.get("config") if manifest else None) or {}

            ctx = PluginContext(
                plugin_name=plugin_name,
                config=plugin_config,
                event_bus=self.event_bus,
            )
            self.contexts[plugin_name] = ctx

            if init_file.is_file():
                module_name = f"omni_hermes_plugin_{plugin_name.replace('-', '_')}"
                try:
                    spec = importlib.util.spec_from_file_location(module_name, str(init_file))
                    if spec and spec.loader:
                        mod = importlib.util.module_from_spec(spec)
                        sys.modules[module_name] = mod
                        spec.loader.exec_module(mod)

                        if hasattr(mod, "register") and callable(mod.register):
                            mod.register(ctx)
                            logger.info(
                                f"Hermes: Loaded plugin '{plugin_name}' with {len(ctx.tools)} tool(s)"
                            )
                        else:
                            logger.warning(f"Hermes: Plugin '{plugin_name}' has no register(ctx) function")
                except Exception as e:
                    logger.error(f"Hermes: Failed to load plugin module {init_file}: {e}", exc_info=True)

            discovered.extend(ctx.tools)

        self.tools = discovered
        return DiscoveryResult(discovered)
