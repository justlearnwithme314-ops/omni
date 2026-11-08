"""
Runtime Mode Manager
Configures the execution profile between Minimalist Pi (lean), Autonomous OpenClaw (heavy), and Evolving Hermes.
"""

from dataclasses import dataclass
from typing import Any, Dict


@dataclass
class ModeProfile:
    name: str
    max_tool_iterations: int
    enable_plugins: bool
    enable_evolution: bool
    enable_context_compaction: bool
    description: str


PROFILES: Dict[str, ModeProfile] = {
    "lean": ModeProfile(
        name="lean",
        max_tool_iterations=3,
        enable_plugins=False,
        enable_evolution=False,
        enable_context_compaction=True,
        description="Pi-style minimalist speed: fast single/dual turns, core tools only, zero plugin overhead.",
    ),
    "autonomous": ModeProfile(
        name="autonomous",
        max_tool_iterations=15,
        enable_plugins=True,
        enable_evolution=False,
        enable_context_compaction=True,
        description="OpenClaw-style heavy autonomous execution: full drop-in skills, multi-turn task loops, sandboxed commands.",
    ),
    "evolve": ModeProfile(
        name="evolve",
        max_tool_iterations=15,
        enable_plugins=True,
        enable_evolution=True,
        enable_context_compaction=True,
        description="Hermes-style self-evolving mode: full skills + SQLite trajectory recording + DSPy/GEPA prompt mutation.",
    ),
}


class ModeManager:
    def __init__(self, default_mode: str = "lean"):
        self.active_mode = default_mode if default_mode in PROFILES else "lean"

    def get_profile(self, mode: str = None) -> ModeProfile:
        m = mode or self.active_mode
        return PROFILES.get(m, PROFILES["lean"])

    def set_mode(self, mode: str):
        if mode in PROFILES:
            self.active_mode = mode
        else:
            raise ValueError(f"Unknown mode '{mode}'. Choose from: {list(PROFILES.keys())}")

    def apply_to_config(self, config: Any, mode: str = None):
        profile = self.get_profile(mode)
        config.set_mode(profile.name)
        config.set_feature("self_evolution", profile.enable_evolution)
        if not profile.enable_plugins:
            config.set_feature("plugins_openclaw", False)
            config.set_feature("plugins_hermes", False)
            config.set_feature("plugins_pi", False)
            config.set_feature("plugins_mcp", False)
        else:
            config.set_feature("plugins_openclaw", True)
            config.set_feature("plugins_hermes", True)
            config.set_feature("plugins_pi", True)
            config.set_feature("plugins_mcp", True)
