from __future__ import annotations

import asyncio
import inspect
import json
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Optional, Union


class DiscoveryResult(list):
    """A list of CanonicalTool items that can also be awaited seamlessly.
    
    This allows both sync `registry.discover_all()` and async `await registry.discover_all()`
    calling conventions to succeed without errors or warnings.
    """

    def __await__(self):
        async def _wrap():
            return self
        return _wrap().__await__()


@dataclass
class CanonicalTool:
    name: str
    description: str
    parameters: dict
    handler: Callable[..., Any]
    source: str  # 'openclaw' | 'hermes' | 'pi' | 'mcp' | 'native'
    enabled: bool = True
    metadata: dict = field(default_factory=dict)
    check_fn: Optional[Callable[[], bool]] = None
    check_ttl: float = 60.0
    _last_check_time: float = field(default=0.0, init=False, repr=False)
    _last_check_result: bool = field(default=True, init=False, repr=False)

    def is_available(self) -> bool:
        if self.check_fn is None:
            return True
        import time
        now = time.time()
        if now - self._last_check_time < self.check_ttl:
            return self._last_check_result
        try:
            self._last_check_result = bool(self.check_fn())
        except Exception:
            self._last_check_result = False
        self._last_check_time = now
        return self._last_check_result

    def to_openai_schema(self) -> dict:
        """Convert tool to OpenAI Function Calling schema."""
        params = self.parameters
        if not isinstance(params, dict):
            params = {
                "type": "object",
                "properties": {},
            }
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description or "",
                "parameters": params,
            },
        }

    async def execute(self, args: Optional[dict] = None, context: Optional[dict] = None) -> str:
        """Execute the tool handler, supporting both sync and async handlers with flexible signatures."""
        if args is None:
            args = {}
        if context is None:
            context = {}

        handler = self.handler
        if handler is None:
            raise ValueError(f"No handler defined for tool '{self.name}'")

        # Try calling with (args, context), fallback to (args), then ()
        try:
            result = handler(args, context)
        except TypeError:
            try:
                result = handler(args)
            except TypeError:
                result = handler()

        # Handle coroutines / awaitables
        if inspect.isawaitable(result):
            result = await result

        # Ensure return value is string
        if isinstance(result, str):
            return result
        elif result is None:
            return ""
        elif isinstance(result, (dict, list, int, float, bool)):
            return json.dumps(result, ensure_ascii=False)
        return str(result)
