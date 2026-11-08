"""OmniHarness Kernel Events Module.

Implements an asynchronous EventBus supporting Hermes-style hook dispatching
with signature inspection for callbacks.
"""

from __future__ import annotations

import inspect
import logging
from typing import Any, Callable, Dict, List, Optional, Set, Tuple, Union

logger = logging.getLogger(__name__)

# Valid Hermes hook signatures: hook_name -> parameter names
VALID_HOOKS: Dict[str, Tuple[str, ...]] = {
    "on_session_start": ("session_id", "state"),
    "before_turn": ("session_id", "turn_index", "state"),
    "before_llm": ("session_id", "messages", "tools", "model"),
    "after_llm": ("session_id", "response", "state"),
    "before_tool_exec": ("session_id", "tool_name", "args", "context"),
    "after_tool_exec": ("session_id", "tool_name", "result", "context"),
    "on_error": ("session_id", "error", "context"),
    "on_session_end": ("session_id", "trajectory", "state"),
}


def _prepare_callback_call(
    callback: Callable,
    hook_name: str,
    kwargs: Dict[str, Any],
) -> Tuple[List[Any], Dict[str, Any]]:
    """Inspect callback parameter signature to adapt arguments.

    Callbacks can accept:
    - Specific named kwargs (e.g. def fn(session_id, state):)
    - A subset of named kwargs (e.g. def fn(messages, model):)
    - Full **kwargs (e.g. def fn(**kwargs): or def fn(session_id, **kwargs):)
    - Positional-only arguments matching the expected hook parameter order.
    - No arguments (e.g. def fn():).
    """
    try:
        sig = inspect.signature(callback)
    except (TypeError, ValueError):
        # Fallback for objects/built-ins where signature inspection is unavailable
        return [], kwargs

    has_var_keyword = any(
        p.kind == inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values()
    )
    if has_var_keyword:
        return [], kwargs

    matched_kwargs: Dict[str, Any] = {}
    pos_args: List[Any] = []
    expected_order = VALID_HOOKS.get(hook_name, tuple(kwargs.keys()))

    for name, param in sig.parameters.items():
        if param.kind == inspect.Parameter.POSITIONAL_ONLY:
            if name in kwargs:
                pos_args.append(kwargs[name])
            else:
                idx = len(pos_args)
                if idx < len(expected_order) and expected_order[idx] in kwargs:
                    pos_args.append(kwargs[expected_order[idx]])
        elif param.kind in (
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
            inspect.Parameter.KEYWORD_ONLY,
        ):
            if name in kwargs:
                matched_kwargs[name] = kwargs[name]
        elif param.kind == inspect.Parameter.VAR_POSITIONAL:
            # *args present without **kwargs: pass remaining expected args
            for k in expected_order:
                if k in kwargs and k not in matched_kwargs:
                    pos_args.append(kwargs[k])

    return pos_args, matched_kwargs


async def _invoke_callback(
    callback: Callable,
    hook_name: str,
    kwargs: Dict[str, Any],
) -> Any:
    """Execute a single callback (sync or async) with signature-inspected args."""
    pos_args, call_kwargs = _prepare_callback_call(callback, hook_name, kwargs)
    if inspect.iscoroutinefunction(callback):
        return await callback(*pos_args, **call_kwargs)

    res = callback(*pos_args, **call_kwargs)
    if inspect.isawaitable(res):
        return await res
    return res


class EventBus:
    """Asynchronous event bus supporting Hermes-style hooks with signature inspection."""

    VALID_HOOKS = VALID_HOOKS

    def __init__(self, fail_fast: bool = False) -> None:
        self._listeners: Dict[str, List[Callable]] = {
            hook: [] for hook in self.VALID_HOOKS
        }
        self.fail_fast = fail_fast

    def subscribe(self, hook_name: str, callback: Callable) -> Callable:
        """Register a callback for an event hook."""
        if hook_name not in self._listeners:
            self._listeners[hook_name] = []
        if callback not in self._listeners[hook_name]:
            self._listeners[hook_name].append(callback)
        return callback

    def register(self, hook_name: str, callback: Callable) -> Callable:
        """Alias for subscribe."""
        return self.subscribe(hook_name, callback)

    def add_listener(self, hook_name: str, callback: Callable) -> Callable:
        """Alias for subscribe."""
        return self.subscribe(hook_name, callback)

    def unsubscribe(self, hook_name: str, callback: Callable) -> bool:
        """Remove a callback from an event hook."""
        if hook_name in self._listeners and callback in self._listeners[hook_name]:
            self._listeners[hook_name].remove(callback)
            return True
        return False

    def remove_listener(self, hook_name: str, callback: Callable) -> bool:
        """Alias for unsubscribe."""
        return self.unsubscribe(hook_name, callback)

    def on(
        self, hook_name: str, callback: Optional[Callable] = None
    ) -> Union[Callable, Callable[[Callable], Callable]]:
        """Decorator or direct method to register an event listener."""
        if callback is not None:
            return self.subscribe(hook_name, callback)

        def decorator(fn: Callable) -> Callable:
            self.subscribe(hook_name, fn)
            return fn

        return decorator

    def get_listeners(self, hook_name: str) -> List[Callable]:
        """Return a copy of registered listeners for a hook."""
        return list(self._listeners.get(hook_name, []))

    def clear(self, hook_name: Optional[str] = None) -> None:
        """Clear listeners for a specific hook, or all hooks if None."""
        if hook_name is not None:
            if hook_name in self._listeners:
                self._listeners[hook_name].clear()
        else:
            for listeners in self._listeners.values():
                listeners.clear()

    async def emit(self, hook_name: str, *args, **kwargs) -> List[Any]:
        """Emit an event to all registered listeners with signature inspection."""
        if args:
            expected_params = self.VALID_HOOKS.get(hook_name, ())
            for i, arg in enumerate(args):
                if i < len(expected_params) and expected_params[i] not in kwargs:
                    kwargs[expected_params[i]] = arg

        listeners = list(self._listeners.get(hook_name, []))
        results: List[Any] = []

        for cb in listeners:
            try:
                res = await _invoke_callback(cb, hook_name, kwargs)
                results.append(res)
            except Exception as err:
                logger.error(
                    "Error executing event hook '%s' in callback %r: %s",
                    hook_name,
                    cb,
                    err,
                    exc_info=True,
                )
                if self.fail_fast and hook_name != "on_error":
                    raise
        return results

    # Typed convenience async event hook methods
    async def on_session_start(self, session_id: str, state: Any) -> List[Any]:
        return await self.emit("on_session_start", session_id=session_id, state=state)

    async def before_turn(
        self, session_id: str, turn_index: int, state: Any
    ) -> List[Any]:
        return await self.emit(
            "before_turn",
            session_id=session_id,
            turn_index=turn_index,
            state=state,
        )

    async def before_llm(
        self,
        session_id: str,
        messages: List[Dict[str, Any]],
        tools: Optional[List[Dict[str, Any]]],
        model: str,
    ) -> List[Any]:
        return await self.emit(
            "before_llm",
            session_id=session_id,
            messages=messages,
            tools=tools or [],
            model=model,
        )

    async def after_llm(
        self, session_id: str, response: Any, state: Any
    ) -> List[Any]:
        return await self.emit(
            "after_llm", session_id=session_id, response=response, state=state
        )

    async def before_tool_exec(
        self, session_id: str, tool_name: str, args: Dict[str, Any], context: Any
    ) -> List[Any]:
        return await self.emit(
            "before_tool_exec",
            session_id=session_id,
            tool_name=tool_name,
            args=args,
            context=context,
        )

    async def after_tool_exec(
        self, session_id: str, tool_name: str, result: Any, context: Any
    ) -> List[Any]:
        return await self.emit(
            "after_tool_exec",
            session_id=session_id,
            tool_name=tool_name,
            result=result,
            context=context,
        )

    async def on_error(
        self, session_id: str, error: Any, context: Any
    ) -> List[Any]:
        return await self.emit(
            "on_error", session_id=session_id, error=error, context=context
        )

    async def on_session_end(
        self, session_id: str, trajectory: Any, state: Any
    ) -> List[Any]:
        return await self.emit(
            "on_session_end",
            session_id=session_id,
            trajectory=trajectory,
            state=state,
        )
