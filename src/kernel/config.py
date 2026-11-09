from __future__ import annotations

FEATURE_ALIASES = {
    "plugins_openclaw": "openclaw_skills",
    "openclaw_skills": "plugins_openclaw",
    "plugins_hermes": "hermes_plugins",
    "hermes_plugins": "plugins_hermes",
    "plugins_pi": "pi_extensions",
    "pi_extensions": "plugins_pi",
    "plugins_mcp": "mcp_servers",
    "mcp_servers": "plugins_mcp",
}

"""OmniHarness Kernel Configuration Module.

Reads and writes harness.config.json with support for feature flags,
execution modes, and model tiers.
"""


import json
import os
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Union

# Config always lives in THIS project's own root (never another checkout).
# Override with the OMNI_CONFIG environment variable (used by the test suite).
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "harness.config.json"
FALLBACK_CONFIG_PATH = DEFAULT_CONFIG_PATH

DEFAULT_FEATURES: Dict[str, bool] = {
    "plugins_pi": True,
    "plugins_hermes": True,
    "plugins_openclaw": True,
    "plugins_mcp": True,
    "self_evolution": True,
    "context_compaction": True,
    "auth_gateway": True,
    "speculative_execution": False,
}

VALID_MODES: Set[str] = {"lean", "autonomous", "evolve"}

DEFAULT_MODEL_TIERS: Dict[str, List[str]] = {
    "fast": [
        "google/gemini-3.8-flash",
        "openai/gpt-4o-mini",
        "anthropic/claude-3-5-haiku",
    ],
    "smart": [
        "anthropic/claude-3-5-sonnet",
        "openai/gpt-4o",
        "google/gemini-3-pro",
    ],
}

DEFAULT_DIRECTORIES: Dict[str, str] = {
    "plugins_pi": "plugins/pi",
    "plugins_hermes": "plugins/hermes",
    "plugins_openclaw": "plugins/openclaw",
    "plugins_mcp": "plugins/mcp",
    "skills": "skills",
}

DEFAULT_EVOLUTION: Dict[str, Any] = {
    "enabled": True,
    "trajectory_db": "telemetry.sqlite",
    "min_failures_before_patch": 3,
}


class HarnessConfig:
    """Configuration manager for OmniHarness kernel.

    Reads and writes harness.config.json, providing helpers for feature flags,
    execution profiles/modes, and model tiers.
    """

    VALID_MODES = VALID_MODES

    def __init__(
        self,
        config_path: Optional[Union[str, Path]] = None,
        auto_load: bool = True,
    ) -> None:
        env_path = os.environ.get("OMNI_CONFIG")
        if config_path is not None:
            self.config_path = Path(config_path)
        elif env_path:
            self.config_path = Path(env_path)
        else:
            self.config_path = DEFAULT_CONFIG_PATH

        self.version: str = "1.0.0"
        self.active_mode: str = "lean"
        self.features: Dict[str, bool] = dict(DEFAULT_FEATURES)
        self.model_tiers: Dict[str, List[str]] = {
            k: list(v) for k, v in DEFAULT_MODEL_TIERS.items()
        }
        self.directories: Dict[str, str] = dict(DEFAULT_DIRECTORIES)
        self.evolution: Dict[str, Any] = dict(DEFAULT_EVOLUTION)
        self._raw_data: Dict[str, Any] = {}

        if auto_load and self.config_path.exists():
            self.load()

    def load(self, path: Optional[Union[str, Path]] = None) -> None:
        """Load configuration from JSON file."""
        target = Path(path) if path is not None else self.config_path
        with open(target, "r", encoding="utf-8") as f:
            data = json.load(f)

        self._raw_data = data
        self.version = data.get("version", self.version)
        self.active_mode = data.get("active_mode", self.active_mode)

        if "features" in data and isinstance(data["features"], dict):
            self.features.update(data["features"])

        if "model_tiers" in data and isinstance(data["model_tiers"], dict):
            self.model_tiers.update(data["model_tiers"])

        if "directories" in data and isinstance(data["directories"], dict):
            self.directories.update(data["directories"])

        if "evolution" in data and isinstance(data["evolution"], dict):
            self.evolution.update(data["evolution"])

    def save(self, path: Optional[Union[str, Path]] = None) -> None:
        """Save configuration to JSON file."""
        target = Path(path) if path is not None else self.config_path
        target.parent.mkdir(parents=True, exist_ok=True)

        data = dict(self._raw_data)
        data.update({
            "version": self.version,
            "active_mode": self.active_mode,
            "features": self.features,
            "model_tiers": self.model_tiers,
            "directories": self.directories,
            "evolution": self.evolution,
        })

        with open(target, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
            f.write("\n")

    def is_feature_enabled(self, feature: str) -> bool:
        """Check whether a specific feature flag is enabled."""
        if feature in self.features:
            return bool(self.features[feature])
        alt = FEATURE_ALIASES.get(feature)
        if alt and alt in self.features:
            return bool(self.features[alt])
        try:
            from .features import CANONICAL_FEATURES
            if feature in CANONICAL_FEATURES:
                return CANONICAL_FEATURES[feature].default_enabled
        except Exception:
            pass
        return False

    def set_feature(self, feature: str, enabled: bool, auto_save: bool = True) -> None:
        """Set a feature flag value and optionally write to disk."""
        self.features[feature] = bool(enabled)
        if auto_save:
            self.save()

    def set_mode(self, mode: str, auto_save: bool = True) -> None:
        """Set the active mode (lean, autonomous, evolve) and optionally write to disk."""
        normalized = mode.lower().strip()
        if normalized not in self.VALID_MODES:
            raise ValueError(
                f"Invalid mode '{mode}'. Must be one of: {sorted(self.VALID_MODES)}"
            )
        self.active_mode = normalized
        if auto_save:
            self.save()

    # Feature flag properties
    @property
    def plugins_pi(self) -> bool:
        return self.is_feature_enabled("plugins_pi")

    @property
    def plugins_hermes(self) -> bool:
        return self.is_feature_enabled("plugins_hermes")

    @property
    def plugins_openclaw(self) -> bool:
        return self.is_feature_enabled("plugins_openclaw")

    @property
    def plugins_mcp(self) -> bool:
        return self.is_feature_enabled("plugins_mcp")

    @property
    def self_evolution(self) -> bool:
        return self.is_feature_enabled("self_evolution")

    @property
    def context_compaction(self) -> bool:
        return self.is_feature_enabled("context_compaction")

    @property
    def auth_gateway(self) -> bool:
        return self.is_feature_enabled("auth_gateway")

    @property
    def speculative_execution(self) -> bool:
        return self.is_feature_enabled("speculative_execution")

    # Profile mode properties
    @property
    def is_lean(self) -> bool:
        return self.active_mode == "lean"

    @property
    def is_autonomous(self) -> bool:
        return self.active_mode == "autonomous"

    @property
    def is_evolve(self) -> bool:
        return self.active_mode == "evolve"

    # Model tier helpers
    def get_tier_models(self, tier: str) -> List[str]:
        """Return list of models configured for a tier ('fast' or 'smart')."""
        return list(self.model_tiers.get(tier, []))

    @property
    def fast(self) -> List[str]:
        return self.get_tier_models("fast")

    @property
    def smart(self) -> List[str]:
        return self.get_tier_models("smart")

    def to_dict(self) -> Dict[str, Any]:
        """Convert configuration to dictionary."""
        return {
            "version": self.version,
            "active_mode": self.active_mode,
            "features": dict(self.features),
            "model_tiers": {k: list(v) for k, v in self.model_tiers.items()},
            "directories": dict(self.directories),
            "evolution": dict(self.evolution),
        }

    @classmethod
    def from_dict(
        cls,
        data: Dict[str, Any],
        config_path: Optional[Union[str, Path]] = None,
    ) -> "HarnessConfig":
        """Instantiate config from dictionary without requiring disk load."""
        inst = cls(config_path=config_path, auto_load=False)
        inst.version = data.get("version", inst.version)
        inst.active_mode = data.get("active_mode", inst.active_mode)
        if "features" in data and isinstance(data["features"], dict):
            inst.features.update(data["features"])
        if "model_tiers" in data and isinstance(data["model_tiers"], dict):
            inst.model_tiers.update(data["model_tiers"])
        if "directories" in data and isinstance(data["directories"], dict):
            inst.directories.update(data["directories"])
        if "evolution" in data and isinstance(data["evolution"], dict):
            inst.evolution.update(data["evolution"])
        inst._raw_data = dict(data)
        return inst
