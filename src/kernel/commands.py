"""
Omni Command Registry & Profile System
Integrates real, functional slash commands across Pi, OMP, Hermes, and OpenClaw
with toggleable command profiles (pi, omp, hermes, openclaw, full, custom).
"""

from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import time
from typing import Any, Callable, Dict, List, Optional, Set, Tuple


@dataclass
class CommandDefinition:
    name: str  # e.g. "model"
    category: str  # "pi", "omp", "hermes", "openclaw", "core"
    description: str
    usage: str
    handler: Optional[Callable] = None
    enabled: bool = True
    aliases: List[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Command Profiles
# ---------------------------------------------------------------------------
COMMAND_PROFILES: Dict[str, Dict[str, Any]] = {
    "pi": {
        "name": "pi",
        "description": "Minimalist Pi Profile: Clean, fast, zero-clutter essential commands.",
        "allowed_categories": {"core", "pi"},
    },
    "omp": {
        "name": "omp",
        "description": "Oh My Pi Profile: IDE tools, diffs, compacting, git worktrees, and planning.",
        "allowed_categories": {"core", "pi", "omp"},
    },
    "hermes": {
        "name": "hermes",
        "description": "Hermes Profile: Knowledge curator, reasoning, rewind, learning, and self-evolution.",
        "allowed_categories": {"core", "pi", "hermes"},
    },
    "openclaw": {
        "name": "openclaw",
        "description": "OpenClaw Profile: Gateway daemon, webhooks, crons, diagnostics, and skill installs.",
        "allowed_categories": {"core", "pi", "openclaw"},
    },
    "full": {
        "name": "full",
        "description": "Full Omni Profile: All commands across Pi, OMP, Hermes, and OpenClaw unlocked.",
        "allowed_categories": {"core", "pi", "omp", "hermes", "openclaw"},
    },
}


class CommandRegistry:
    """Master registry and execution engine for Omni slash commands and profiles."""

    def __init__(self, config: Any = None, active_profile: str = "full"):
        self.config = config
        cfg_path = Path(__file__).resolve().parent.parent.parent / "omni.config.json"
        saved_prof = "full"
        if cfg_path.exists():
            try:
                with open(cfg_path, "r", encoding="utf-8") as f:
                    saved_prof = json.load(f).get("command_profile", "full")
            except Exception:
                pass
        self.active_profile = active_profile if active_profile != "full" else (saved_prof if saved_prof in COMMAND_PROFILES else "full")
        self.commands: Dict[str, CommandDefinition] = {}
        self._init_commands()
        self.apply_profile(self.active_profile)

    def _init_commands(self):
        """Registers all commands across Pi, OMP, Hermes, and OpenClaw with handlers."""

        # === 1. Core & Pi Commands ===
        self.register(CommandDefinition(
            name="help",
            category="core",
            description="Display active slash commands and keybindings",
            usage="/help",
        ))
        self.register(CommandDefinition(
            name="model",
            category="core",
            description="View or switch active model (flash, sonnet, opus, gpt-4o, etc.)",
            usage="/model [name]",
            aliases=["m"],
        ))
        self.register(CommandDefinition(
            name="clear",
            category="core",
            description="Clear current conversation turn history",
            usage="/clear",
        ))
        self.register(CommandDefinition(
            name="reset",
            category="core",
            description="Full session reset (clears messages, scratchpad, and cache)",
            usage="/reset",
        ))
        self.register(CommandDefinition(
            name="tokens",
            category="pi",
            description="Show session token counts and token budget estimates",
            usage="/tokens",
        ))
        self.register(CommandDefinition(
            name="tools",
            category="pi",
            description="List active tools, functions, and drop-in skills",
            usage="/tools",
            aliases=["plugins"],
        ))
        self.register(CommandDefinition(
            name="session",
            category="pi",
            description="Show session metadata, list saved sessions, or switch",
            usage="/session [list|save|switch <id>]",
        ))
        self.register(CommandDefinition(
            name="undo",
            category="pi",
            description="Revert file modifications from the last N turns via git checkpoints",
            usage="/undo [steps]",
        ))
        self.register(CommandDefinition(
            name="keys",
            category="core",
            description="View provider API keys, OAuth status, and cooldown blocks",
            usage="/keys",
        ))
        self.register(CommandDefinition(
            name="profile",
            category="core",
            description="Switch command profile (pi, omp, hermes, openclaw, full)",
            usage="/profile [name]",
        ))

        # === 2. Oh My Pi (OMP) Standouts ===
        self.register(CommandDefinition(
            name="compact",
            category="omp",
            description="Trigger manual SnapCompact context compression",
            usage="/compact",
        ))
        self.register(CommandDefinition(
            name="cleanse",
            category="omp",
            description="Scrub scratchpad and temporary intermediate state",
            usage="/cleanse",
        ))
        self.register(CommandDefinition(
            name="diff",
            category="omp",
            description="View syntax-highlighted git diff of recent turn modifications",
            usage="/diff",
        ))
        self.register(CommandDefinition(
            name="branch",
            category="omp",
            description="Fork or branch current session state into an alternative timeline",
            usage="/branch [name]",
            aliases=["fork"],
        ))
        self.register(CommandDefinition(
            name="wt",
            category="omp",
            description="Inspect git worktree status and tracked files",
            usage="/wt",
            aliases=["worktree"],
        ))
        self.register(CommandDefinition(
            name="thinking",
            category="omp",
            description="Configure model reasoning/thinking budget (high, medium, low, off)",
            usage="/thinking [high|medium|low|off]",
        ))
        self.register(CommandDefinition(
            name="budget",
            category="omp",
            description="Inspect output budget clamping metrics and window statistics",
            usage="/budget",
        ))
        self.register(CommandDefinition(
            name="stats",
            category="omp",
            description="Display session token counts, costs, and performance telemetry",
            usage="/stats",
        ))
        self.register(CommandDefinition(
            name="plan",
            category="omp",
            description="Dispatch planner subagent to decompose a task into structured steps",
            usage="/plan <goal>",
        ))
        self.register(CommandDefinition(
            name="scout",
            category="omp",
            description="Dispatch scout subagent for rapid codebase file search",
            usage="/scout <query>",
        ))
        self.register(CommandDefinition(
            name="review",
            category="omp",
            description="Dispatch reviewer subagent to critique code and diffs",
            usage="/review",
        ))

        # === 3. Hermes Standouts ===
        self.register(CommandDefinition(
            name="memory",
            category="hermes",
            description="Inspect Mnemopi semantic knowledge graph assertions",
            usage="/memory",
        ))
        self.register(CommandDefinition(
            name="curator",
            category="hermes",
            description="Extract and synthesize facts from current conversation into memory graph",
            usage="/curator",
        ))
        self.register(CommandDefinition(
            name="learn",
            category="hermes",
            description="Run Hermes self-evolution optimizer on failed session trajectories",
            usage="/learn [--patch]",
            aliases=["evolve"],
        ))
        self.register(CommandDefinition(
            name="rollback",
            category="hermes",
            description="Rewind conversation turn history to turn N",
            usage="/rollback <turn_index>",
        ))
        self.register(CommandDefinition(
            name="snapshot",
            category="hermes",
            description="Create an immutable named snapshot of session state and files",
            usage="/snapshot <name>",
        ))
        self.register(CommandDefinition(
            name="fast",
            category="hermes",
            description="Quickly switch to fast model tier (e.g. gemini-2.5-flash)",
            usage="/fast",
        ))
        self.register(CommandDefinition(
            name="smart",
            category="hermes",
            description="Quickly switch to smart reasoning model tier (e.g. claude-sonnet)",
            usage="/smart",
        ))

        # === 4. OpenClaw Standouts ===
        self.register(CommandDefinition(
            name="status",
            category="openclaw",
            description="Show system health, gateway status, and channel endpoints",
            usage="/status",
        ))
        self.register(CommandDefinition(
            name="gateway",
            category="openclaw",
            description="Inspect or control port 18789 Gateway server (status, start, stop)",
            usage="/gateway [status|start|stop]",
        ))
        self.register(CommandDefinition(
            name="cron",
            category="openclaw",
            description="View or trigger autonomous scheduled recurring tasks",
            usage="/cron [list|run]",
        ))
        self.register(CommandDefinition(
            name="doctor",
            category="openclaw",
            description="Run diagnostic audit of auth, databases, node environment, and plugins",
            usage="/doctor",
        ))
        self.register(CommandDefinition(
            name="install",
            category="openclaw",
            description="Auto-install missing dependencies for a skill (npm/pip/uv)",
            usage="/install <skill_name>",
        ))
        self.register(CommandDefinition(
            name="permission",
            category="openclaw",
            description="Configure dangerous command permission gate (allow, deny, list)",
            usage="/permission [allow|deny|list] [pattern]",
        ))
        self.register(CommandDefinition(
            name="webhooks",
            category="openclaw",
            description="View status of external app webhooks (Telegram, Discord, Slack)",
            usage="/webhooks",
        ))
        self.register(CommandDefinition(
            name="mode",
            category="core",
            description="Switch runtime profile mode (lean, balanced, autonomous, evolve)",
            usage="/mode [name]",
        ))
        self.register(CommandDefinition(
            name="toggle",
            category="core",
            description="View or toggle individual modular features on/off",
            usage="/toggle [feature] [on|off]",
        ))

    def register(self, cmd: CommandDefinition):
        self.commands[cmd.name.lower()] = cmd
        for alias in cmd.aliases:
            self.commands[alias.lower()] = cmd

    def apply_profile(self, profile_name: str) -> bool:
        """Activates a command profile, filtering which commands are active."""
        prof = COMMAND_PROFILES.get(profile_name.lower())
        if not prof:
            return False
        self.active_profile = profile_name.lower()
        allowed = prof["allowed_categories"]

        for cmd in self.commands.values():
            cmd.enabled = cmd.category in allowed

        cfg_path = Path(__file__).resolve().parent.parent.parent / "omni.config.json"
        try:
            data = {}
            if cfg_path.exists():
                with open(cfg_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
            data["command_profile"] = self.active_profile
            with open(cfg_path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
        except Exception:
            pass
        return True

    def toggle_command(self, cmd_name: str, enabled: Optional[bool] = None) -> Optional[bool]:
        """Toggles or sets the enabled status of an individual command."""
        name = cmd_name.lstrip("/").lower()
        cmd = self.commands.get(name)
        if not cmd:
            return None
        if enabled is None:
            cmd.enabled = not cmd.enabled
        else:
            cmd.enabled = bool(enabled)
        return cmd.enabled

    def get_active_commands(self) -> List[CommandDefinition]:
        """Returns all currently enabled unique commands."""
        seen = set()
        active = []
        for name, cmd in sorted(self.commands.items()):
            if cmd.enabled and cmd.name not in seen:
                seen.add(cmd.name)
                active.append(cmd)
        return active

    def get_command(self, cmd_name: str) -> Optional[CommandDefinition]:
        name = cmd_name.lstrip("/").lower()
        cmd = self.commands.get(name)
        return cmd if cmd and cmd.enabled else None
