"""
Process-wide service container.

Slash commands and AI tools must share ONE instance of each long-lived subsystem
(browser relay, collab server, gateways, worktrees, ...). Previously every command built
a fresh manager, so state such as "gateway started" or "room created" vanished
immediately. Services are created lazily on first use and live until ``shutdown()``.
"""

from __future__ import annotations

import logging
import threading
from typing import Any, Callable, Dict, Optional, TypeVar

logger = logging.getLogger(__name__)
T = TypeVar("T")


class Services:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._items: Dict[str, Any] = {}
        self.agent_router: Optional[Callable[[Any], str]] = None  # set by the REPL when an agent is attached

    def _get(self, key: str, factory: Callable[[], T]) -> T:
        with self._lock:
            if key not in self._items:
                self._items[key] = factory()
            return self._items[key]

    # ---- accessors (imports are lazy so unused subsystems cost nothing) ----
    @property
    def browser(self):
        from ..features.browser_relay import ChromeDevToolsRelay
        return self._get("browser", ChromeDevToolsRelay)

    @property
    def collab(self):
        from ..features.collab_web import CollabServerManager
        return self._get("collab", CollabServerManager)

    @property
    def canvas(self):
        from ..features.visual_canvas import VisualCanvasManager
        return self._get("canvas", VisualCanvasManager)

    @property
    def gateways(self):
        from ..features.gateways.manager import ChatGatewayManager

        def router(msg):
            if self.agent_router is None:
                return "Omni: no agent is attached to this gateway yet."
            return self.agent_router(msg)

        return self._get("gateways", lambda: ChatGatewayManager(message_router=router))

    @property
    def sandbox(self):
        from ..features.docker_sandbox import DockerSandboxRunner
        return self._get("sandbox", DockerSandboxRunner)

    @property
    def nodes(self):
        from ..features.node_pairing import NodePairingManager
        return self._get("nodes", NodePairingManager)

    @property
    def benchmarks(self):
        from ..features.metaharness import MetaHarnessManager
        return self._get("benchmarks", MetaHarnessManager)

    @property
    def local_engine(self):
        from ..features.local_engine import LocalEngineRunner
        return self._get("local_engine", LocalEngineRunner)

    @property
    def voice(self):
        from ..features.voice_loop import VoiceLoopManager
        return self._get("voice", VoiceLoopManager)

    @property
    def tickets(self):
        from ..features.paperclip_adapter import PaperclipAdapter
        return self._get("tickets", PaperclipAdapter)

    @property
    def lsp(self):
        from ..features.lsp_bridge import LspClient
        return self._get("lsp", LspClient)

    @property
    def worktrees(self):
        from ..features.shadow_worktree import ShadowWorktreeManager
        return self._get("worktrees", ShadowWorktreeManager)

    # ---- lifecycle ----
    def running(self) -> Dict[str, bool]:
        with self._lock:
            return {k: True for k in self._items}

    def shutdown(self) -> None:
        """Stop every service that was started. Safe to call repeatedly."""
        with self._lock:
            items, self._items = self._items, {}
        for name, svc in items.items():
            for stop in ("stop_all", "stop", "close"):
                fn = getattr(svc, stop, None)
                if callable(fn):
                    try:
                        fn()
                    except Exception as e:  # never let one service block shutdown
                        logger.warning("service %s failed to stop: %s", name, e)
                    break


services = Services()
