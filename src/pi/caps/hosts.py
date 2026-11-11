"""Switch-type capabilities: the hosts and gates a profile already controls through its own keys.

Listing them as capabilities lets one command (`/capabilities`, `omni toggle`) turn any part of the harness on or
off, whether it is a native module or a plugin host.
"""
from __future__ import annotations

from typing import Any, Dict

from . import Capability


def _flag(key: str, default: bool):
    def apply(profile: Dict[str, Any], on: bool) -> None:
        profile[key] = on

    def is_on(profile: Dict[str, Any]) -> bool:
        return bool(profile.get(key, default))
    return apply, is_on


def _registry_source(source: str):
    def apply(profile: Dict[str, Any], on: bool) -> None:
        reg = dict(profile.get("omni_registry") or {})
        sources = [s for s in reg.get("sources") or [] if s != source]
        if on:
            sources.append(source)
        reg["sources"], reg["enabled"] = sources, bool(sources)
        profile["omni_registry"] = reg

    def is_on(profile: Dict[str, Any]) -> bool:
        reg = profile.get("omni_registry") or {}
        return bool(reg.get("enabled")) and source in (reg.get("sources") or [])
    return apply, is_on


def _cap(cid: str, title: str, desc: str, inspired: str, pair) -> Capability:
    return Capability(cid, title, desc, inspired, apply=pair[0], is_on=pair[1], group="host")


CAPABILITIES = [
    _cap("extensions", "Extensions (Node host)", "Run Pi / Oh My Pi extensions (TypeScript) through the Node host.",
         "Pi, Oh My Pi", _flag("pi_extensions", True)),
    _cap("bundled-extensions", "Bundled extensions", "Also load the extensions shipped in Omni's plugins/pi folder.",
         "Omni", _flag("omni_plugins_pi", True)),
    _cap("mcp", "MCP servers", "Connect the MCP servers from mcp.json and expose their tools.", "Pi, MCP", _flag("mcp", True)),
    _cap("permission-gate", "Permission gate", "Ask before destructive tool calls (rm -rf, force pushes, writes outside the project).",
         "Hermes approvals, OpenClaw exec approvals", _flag("permission_gate", False)),
    _cap("plugins-hermes", "Hermes plugins", "Load Hermes plugins (plugin.yaml + register(ctx)) as tools.", "Hermes",
         _registry_source("hermes")),
    _cap("plugins-openclaw", "OpenClaw plugins", "Load OpenClaw skills/plugins through the legacy adapter.", "OpenClaw",
         _registry_source("openclaw")),
    _cap("plugins-omni", "Omni tools", "Omni's own tool modules (browser relay, LSP bridge, ...).", "Omni",
         _registry_source("omni")),
    _cap("plugins-mcp-legacy", "Legacy MCP registry", "MCP servers from Omni's older registry (prefer `mcp`).", "Omni",
         _registry_source("mcp")),
]
