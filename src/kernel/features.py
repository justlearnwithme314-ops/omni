"""
Omni Feature Registry & Profile Presets
Defines all modular features from Pi, Hermes, and OpenClaw, with individual on/off toggles
and execution profile presets (Lean, Balanced, Autonomous, Evolve).
"""

from dataclasses import dataclass, field
import json
import os
from pathlib import Path
from typing import Any, Dict, List, Optional


@dataclass
class FeatureDefinition:
    id: str
    name: str
    category: str  # "pi", "hermes", "openclaw", "core"
    description: str
    default_enabled: bool = True
    requires: List[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Canonical Feature Inventory across Pi, Hermes, and OpenClaw
# ---------------------------------------------------------------------------
CANONICAL_FEATURES: Dict[str, FeatureDefinition] = {
    # Pi Architecture Standouts
    "context_compaction": FeatureDefinition(
        id="context_compaction",
        name="SnapCompact Context Compression",
        category="pi",
        description="Preserves sacred prompt cache while condensing older conversation turns into compact summaries.",
        default_enabled=True,
    ),
    "speculative_execution": FeatureDefinition(
        id="speculative_execution",
        name="Speculative Tool Execution",
        category="pi",
        description="Pre-warms and speculatively prepares tools while LLM tokens are streaming.",
        default_enabled=False,
    ),
    "internal_urls": FeatureDefinition(
        id="internal_urls",
        name="Internal URL Protocol Handlers",
        category="pi",
        description="Resolves internal protocols (skill://, artifact://, agent://, xd://) into workspace content.",
        default_enabled=True,
    ),
    "subagents": FeatureDefinition(
        id="subagents",
        name="Subagent Task Delegation",
        category="pi",
        description="Allows the primary agent to spawn scoped, isolated child agents for sub-tasks (planner, scout, reviewer).",
        default_enabled=True,
    ),
    "pi_extensions": FeatureDefinition(
        id="pi_extensions",
        name="Pi JS/TS Extensions",
        category="pi",
        description="Enables running native JavaScript and TypeScript extension modules via the Node runner bridge.",
        default_enabled=True,
    ),

    # Hermes Architecture Standouts
    "hermes_tool_calling": FeatureDefinition(
        id="hermes_tool_calling",
        name="Hermes XML & JSON Dual Tool Parser",
        category="hermes",
        description="Parses both standard OpenAI JSON tool calls and open-weight Hermes <tool_call> XML tags.",
        default_enabled=True,
    ),
    "prefix_caching": FeatureDefinition(
        id="prefix_caching",
        name="Sacred Prompt Prefix Cache Guardian",
        category="hermes",
        description="Enforces strict byte-stability on system prompt and tool definitions to protect KV prefix caches.",
        default_enabled=True,
    ),
    "lifecycle_hooks": FeatureDefinition(
        id="lifecycle_hooks",
        name="Hermes 33 Lifecycle Hooks",
        category="hermes",
        description="Signature-inspected event bus dispatching to plugin callbacks before/after turns and tool calls.",
        default_enabled=True,
    ),
    "auxiliary_client": FeatureDefinition(
        id="auxiliary_client",
        name="Auxiliary Side-LLM Delegation",
        category="hermes",
        description="Routes background tasks (context compression, session titling, judging) to lightweight side models.",
        default_enabled=True,
    ),
    "self_evolution": FeatureDefinition(
        id="self_evolution",
        name="Hermes DSPy/GEPA Self-Evolution",
        category="hermes",
        description="Analyzes failed session trajectories in SQLite and synthesizes prompt patches or new skills.",
        default_enabled=True,
    ),
    "hermes_plugins": FeatureDefinition(
        id="hermes_plugins",
        name="Hermes Drop-In Python Plugins",
        category="hermes",
        description="Loads and registers Python plugins via plugin.yaml and def register(ctx: PluginContext).",
        default_enabled=True,
    ),

    # OpenClaw Architecture Standouts
    "openclaw_skills": FeatureDefinition(
        id="openclaw_skills",
        name="OpenClaw Drop-In Skills & Registry",
        category="openclaw",
        description="Loads SKILL.md bundles with YAML frontmatter, platform gating (requires.bins/env), and dynamic {baseDir}.",
        default_enabled=True,
    ),
    "permission_gates": FeatureDefinition(
        id="permission_gates",
        name="Dangerous Command Permission Gate",
        category="openclaw",
        description="Intercepts potentially destructive bash and filesystem operations, requiring explicit user approval.",
        default_enabled=True,
    ),
    "session_persistence": FeatureDefinition(
        id="session_persistence",
        name="Durable SQLite Session Persistence",
        category="openclaw",
        description="Persists conversation state and tool outcomes to SQLite, allowing resumption via --resume <id>.",
        default_enabled=True,
    ),
    "multi_channel_gateway": FeatureDefinition(
        id="multi_channel_gateway",
        name="Multi-Channel Gateway Server",
        category="openclaw",
        description="Runs local HTTP/WebSocket gateway server for external UI and multi-channel client dispatching.",
        default_enabled=False,
    ),

    # Core Standards
    "mcp_servers": FeatureDefinition(
        id="mcp_servers",
        name="Model Context Protocol (MCP) Client",
        category="core",
        description="Standard JSON-RPC 2.0 stdio/SSE client connecting to external MCP tool and resource servers.",
        default_enabled=True,
    ),
    "auth_gateway": FeatureDefinition(
        id="auth_gateway",
        name="Provider Key-Pool & Auto-Fallback",
        category="core",
        description="Pi-style SQLite credential pool with automatic 429/401 rate-limit cooldown blocking and model cascades.",
        default_enabled=True,
    ),
}


# ---------------------------------------------------------------------------
# Profile Presets (From ultra-lightweight Pi to full Autonomous & Evolving)
# ---------------------------------------------------------------------------
PROFILE_PRESETS: Dict[str, Dict[str, Any]] = {
    "lean": {
        "name": "lean",
        "description": "Pi-style minimalist speed: raw CLI, sub-second latency, core tools only, zero plugin overhead.",
        "max_tool_iterations": 3,
        "features": {
            "context_compaction": True,
            "speculative_execution": False,
            "internal_urls": False,
            "subagents": False,
            "pi_extensions": False,
            "hermes_tool_calling": True,
            "prefix_caching": True,
            "lifecycle_hooks": False,
            "auxiliary_client": False,
            "self_evolution": False,
            "hermes_plugins": False,
            "openclaw_skills": False,
            "permission_gates": False,
            "session_persistence": False,
            "multi_channel_gateway": False,
            "mcp_servers": False,
            "auth_gateway": True,
        },
    },
    "balanced": {
        "name": "balanced",
        "description": "Everyday developer workflow: tools enabled, key-rotation, context compaction, and prefix caching.",
        "max_tool_iterations": 10,
        "features": {
            "context_compaction": True,
            "speculative_execution": False,
            "internal_urls": True,
            "subagents": False,
            "pi_extensions": True,
            "hermes_tool_calling": True,
            "prefix_caching": True,
            "lifecycle_hooks": True,
            "auxiliary_client": True,
            "self_evolution": False,
            "hermes_plugins": True,
            "openclaw_skills": True,
            "permission_gates": True,
            "session_persistence": True,
            "multi_channel_gateway": False,
            "mcp_servers": True,
            "auth_gateway": True,
        },
    },
    "autonomous": {
        "name": "autonomous",
        "description": "Full OpenClaw + Pi power: all skills, task decomposition, subagent delegation, permission gates.",
        "max_tool_iterations": 20,
        "features": {
            "context_compaction": True,
            "speculative_execution": True,
            "internal_urls": True,
            "subagents": True,
            "pi_extensions": True,
            "hermes_tool_calling": True,
            "prefix_caching": True,
            "lifecycle_hooks": True,
            "auxiliary_client": True,
            "self_evolution": False,
            "hermes_plugins": True,
            "openclaw_skills": True,
            "permission_gates": True,
            "session_persistence": True,
            "multi_channel_gateway": False,
            "mcp_servers": True,
            "auth_gateway": True,
        },
    },
    "evolve": {
        "name": "evolve",
        "description": "Full Hermes self-evolution: autonomous execution + SQLite trajectory recording + DSPy/GEPA prompt mutation.",
        "max_tool_iterations": 20,
        "features": {
            "context_compaction": True,
            "speculative_execution": True,
            "internal_urls": True,
            "subagents": True,
            "pi_extensions": True,
            "hermes_tool_calling": True,
            "prefix_caching": True,
            "lifecycle_hooks": True,
            "auxiliary_client": True,
            "self_evolution": True,
            "hermes_plugins": True,
            "openclaw_skills": True,
            "permission_gates": True,
            "session_persistence": True,
            "multi_channel_gateway": False,
            "mcp_servers": True,
            "auth_gateway": True,
        },
    },
}


class FeatureRegistry:
    """Manages active features, toggles, and persistence in omni.config.json."""

    def __init__(self, config_data: Optional[Dict[str, Any]] = None, config_path: Optional[str] = None):
        self.config_path = config_path or os.path.join(
            os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
            "omni.config.json",
        )
        self.active_features: Dict[str, bool] = {
            f_id: f_def.default_enabled for f_id, f_def in CANONICAL_FEATURES.items()
        }

        # Load from config data if provided
        if config_data and "features" in config_data:
            self.active_features.update(config_data["features"])

    def is_enabled(self, feature_id: str) -> bool:
        return self.active_features.get(feature_id, False)

    def set_feature(self, feature_id: str, enabled: bool) -> bool:
        if feature_id not in CANONICAL_FEATURES:
            return False
        self.active_features[feature_id] = enabled
        self._save_to_config()
        return True

    def toggle(self, feature_id: str) -> Optional[bool]:
        if feature_id not in CANONICAL_FEATURES:
            return None
        self.active_features[feature_id] = not self.active_features[feature_id]
        self._save_to_config()
        return self.active_features[feature_id]

    def apply_profile(self, profile_name: str) -> bool:
        profile = PROFILE_PRESETS.get(profile_name.lower())
        if not profile:
            return False
        self.active_features.update(profile["features"])
        self._save_to_config()
        return True

    def list_features(self) -> List[Dict[str, Any]]:
        result = []
        for f_id, f_def in CANONICAL_FEATURES.items():
            result.append({
                "id": f_id,
                "name": f_def.name,
                "category": f_def.category,
                "description": f_def.description,
                "enabled": self.active_features.get(f_id, False),
            })
        return result

    def _save_to_config(self):
        try:
            cfg_path = Path(self.config_path)
            data = {}
            if cfg_path.exists():
                with open(cfg_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
            data["features"] = self.active_features
            with open(cfg_path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
        except Exception:
            pass
