"""Brings Omni's existing capabilities into the core for heavier profiles.

- `load_registry_tools`: runs Omni's MasterToolRegistry (MCP servers, Hermes plugins, OpenClaw skills, Omni tools
  such as browser_cdp / lsp_query / canvas_render / remote_node_task, plugins/omni) and wraps each tool as a core
  ToolDef. Pi extensions are NOT loaded through it (the Node host does that), and tools that duplicate the core's
  read/bash/edit/write/grep/find/ls are skipped.
- `PermissionGateMiddleware`: Omni's PermissionGate in front of every tool call; asks the user (or blocks when
  there is no UI).
"""
from __future__ import annotations

import asyncio
import copy
import json
import logging
import threading
from typing import Any, Callable, Dict, List, Optional, Tuple

from .tools.base import ToolDef, ToolError

DUPLICATES = {"read", "write", "edit", "bash", "grep", "find", "ls", "read_file", "write_file", "execute_bash"}
SOURCE_FEATURE = {"mcp": "mcp_servers", "hermes": "hermes_plugins", "openclaw": "openclaw_skills"}

log = logging.getLogger("omni.core.bridge")


def _run(coro_fn: Callable[[], Any]) -> Any:
    """Run an async callable from a worker thread (the agent loop is synchronous)."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro_fn())
    box: Dict[str, Any] = {}

    def worker() -> None:
        try:
            box["v"] = asyncio.run(coro_fn())
        except BaseException as e:  # noqa: BLE001
            box["e"] = e
    t = threading.Thread(target=worker, daemon=True)
    t.start()
    t.join()
    if "e" in box:
        raise box["e"]
    return box.get("v")


def load_registry_tools(cwd: str, sources: List[str], diagnostics: List[str]) -> Dict[str, ToolDef]:
    try:
        from ..adapters.registry import MasterToolRegistry  # type: ignore
    except Exception as e:
        diagnostics.append(f"Omni tool registry unavailable: {e}")
        return {}
    cfg: Dict[str, Any] = {}
    try:
        from .profiles import PROJECT_ROOT
        cfg = json.loads((PROJECT_ROOT / "harness.config.json").read_text(encoding="utf-8"))
    except Exception:
        cfg = {}
    cfg = copy.deepcopy(cfg)
    feats = cfg.setdefault("features", {})
    feats["pi_extensions"] = False  # Pi extensions run in the core's Node host
    for src, feat in SOURCE_FEATURE.items():
        feats[feat] = src in sources
    try:
        reg = MasterToolRegistry(config=cfg)
        reg.pi_adapter.discover = lambda: []  # type: ignore[attr-defined]
        for src, adapter in (("openclaw", reg.openclaw_adapter), ("hermes", reg.hermes_adapter), ("mcp", reg.mcp_adapter)):
            if src not in sources:
                adapter.discover = lambda: []  # type: ignore[attr-defined]
        tools = list(reg.discover_all())
    except Exception as e:
        diagnostics.append(f"Omni tool registry failed: {type(e).__name__}: {e}")
        return {}
    out: Dict[str, ToolDef] = {}
    for t in tools:
        src = "omni" if t.source in ("native", "omni") else t.source
        if not t.enabled or t.name in DUPLICATES or src not in sources:
            continue
        out[t.name] = _wrap(t, cwd)
    return out


def _wrap(t: Any, cwd: str) -> ToolDef:
    def execute(tool_call_id, params, signal=None, on_update=None, ctx=None):
        if getattr(signal, "aborted", False):
            raise ToolError("Operation aborted")
        context = {"cwd": getattr(ctx, "cwd", cwd), "tool_call_id": tool_call_id}
        try:
            available = t.is_available()
        except Exception:
            available = True
        if not available:
            raise ToolError(f"{t.name} is not available right now")
        try:
            res = _run(lambda: t.execute(args=params or {}, context=context))
        except ToolError:
            raise
        except Exception as e:
            raise ToolError(f"{type(e).__name__}: {e}")
        text = res if isinstance(res, str) else json.dumps(res, ensure_ascii=False, default=str)
        return {"content": [{"type": "text", "text": text}], "details": {"source": t.source}}

    params = t.parameters if isinstance(t.parameters, dict) else {"type": "object", "properties": {}}
    desc = (t.description or t.name).strip()
    return ToolDef(name=t.name, label=t.name, description=desc, parameters=params, execute=execute,
                   prompt_snippet=desc.split("\n")[0][:120], source=f"omni:{t.source}")


class PermissionGateMiddleware:
    """Wraps Omni's PermissionGate; returns (allowed, reason). `ask` is the UI confirm, or None when headless."""

    def __init__(self, cwd: str, ask: Optional[Callable[[str, str], Optional[str]]]):
        self.gate = None
        try:
            from ..features.permissions import PermissionGate  # type: ignore
            self.gate = PermissionGate(workspace_dir=cwd)
        except Exception as e:
            log.debug("permission gate unavailable: %s", e)
        self.ask = ask
        self.session_allow: set = set()

    def check(self, tool_name: str, args: Dict[str, Any]) -> Tuple[bool, str]:
        if self.gate is None:
            return True, ""
        try:
            allowed, reason = self.gate.check_permission(tool_name, args or {})
        except Exception:
            return True, ""
        if allowed:
            return True, ""
        key = (tool_name, json.dumps(args, sort_keys=True, default=str))
        if key in self.session_allow:
            return True, ""
        if self.ask is None:
            return False, f"Blocked by permission gate: {reason}"
        answer = self.ask(f"Allow {tool_name}?", reason)
        if answer == "always":
            self.session_allow.add(key)
            return True, ""
        if answer == "once":
            return True, ""
        return False, f"User denied: {reason}"
