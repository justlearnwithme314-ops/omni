"""
Slash-command handler registry.

Replaces the ever-growing ``if/elif`` chain in ``CommandExecutor.execute``. A handler is a
plain function ``(arg: str, ctx: HandlerContext) -> str`` registered with ``@command(...)``.

* Handlers run in a worker thread, so slow network/process work never freezes the REPL.
* Errors are handled centrally: a handler can simply raise (``UsageError`` for bad input);
  the user always gets a clean one-line message instead of a traceback.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import logging
from typing import Any, Callable, Dict, Optional, Tuple

from ..services import Services, services as default_services

logger = logging.getLogger(__name__)


class UsageError(Exception):
    """Raised by a handler when the user supplied bad arguments; the message is shown as-is."""


@dataclass
class HandlerContext:
    executor: Any
    state: Any
    services: Services = field(default_factory=lambda: default_services)
    extra: Dict[str, Any] = field(default_factory=dict)


Handler = Callable[[str, HandlerContext], str]
_REGISTRY: Dict[str, Handler] = {}


def command(*names: str) -> Callable[[Handler], Handler]:
    def register(fn: Handler) -> Handler:
        for n in names:
            _REGISTRY[n] = fn
        return fn
    return register


def registered() -> Dict[str, Handler]:
    return dict(_REGISTRY)


async def dispatch(name: str, arg: str, executor: Any, state: Any,
                   extra: Optional[Dict[str, Any]] = None) -> Optional[Tuple[bool, str]]:
    """Run a registered handler. Returns None when ``name`` has no registry handler."""
    handler = _REGISTRY.get(name)
    if handler is None:
        return None
    ctx = HandlerContext(executor=executor, state=state, extra={**(extra or {}), "name": name})
    try:
        return True, await asyncio.to_thread(handler, arg, ctx)
    except UsageError as e:
        return True, str(e)
    except Exception as e:  # noqa: BLE001 - surfaced to the user, logged for debugging
        logger.debug("handler /%s failed", name, exc_info=True)
        return True, f"[!] /{name} failed: {e}"


from . import session, subsystems  # noqa: E402,F401  (registers handlers on import)
