"""
Omni Complete Command Catalog & Profile System
Contains all 56 OMP CLI subcommands and 175+ interactive REPL slash commands across
Pi, OMP, Hermes, and OpenClaw with dynamic profile filtering.
"""

from dataclasses import dataclass, field
import json
import os
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set, Tuple


@dataclass
class CommandDefinition:
    name: str
    category: str  # "core", "pi", "omp", "hermes", "openclaw"
    description: str
    usage: str
    handler: Optional[Callable] = None
    enabled: bool = True
    aliases: List[str] = field(default_factory=list)


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

# ---------------------------------------------------------------------------
# All 56 OMP Top-Level CLI Subcommands (omp <command>)
# ---------------------------------------------------------------------------
CLI_SUBCOMMANDS: Dict[str, str] = {

    "acp": "CLI subcommand",
    "agents": "Agents action",
    "anonymize": "Session file path or id prefix (default: most recent for cwd)",
    "auth": "export or import",
    "auth-broker": "Sub-command",
    "auth-gateway": "Sub-command",
    "bench": "Model selectors (provider/model or fuzzy id, e.g. opus)",
    "browser-relay": "Port to listen on",
    "cleanse": "What to detect and fix (e.g. ",
    "clip": "Recording to upload (default: the newest /record capture)",
    "collab": "list (default) or link",
    "commit": "Push after committing",
    "complete": "CLI subcommand",
    "completions": "Target shell",
    "compress": "Files or glob patterns to compress",
    "config": "Config action",
    "cron": "Manage autonomous scheduled recurring tasks",
    "diff": "Show recent file modifications diff",
    "doctor": "Run diagnostic health audit across all components",
    "dry-balance": "Model selector (provider/model or fuzzy id). Defaults to the configured default model.",
    "find": "What to find, in plain language",
    "gallery": "Render only tool, composer, or segment surfaces (repeatable; default: all)",
    "gateway": "Manage port 18789 Gateway server",
    "gc": "Apply changes (default is dry-run)",
    "git": "Pin the view to one commit (any revision, e.g. HEAD~2 or a sha)",
    "grep": "Regex pattern to search for",
    "grievances": "list (default), clean, or push",
    "if-bench": "Model selectors (provider/model or fuzzy id, e.g. opus)",
    "images": "status (default), doctor, probe, or purge",
    "install": "Local path, npm spec, or marketplace ref (e.g. ./my-ext, my-pkg@1.2.3, name@marketplace)",
    "join": "Collab link shared by the host (/collab)",
    "launch": "CLI subcommand",
    "launch-help": "AI coding assistant",
    "login": "OAuth provider id (e.g. anthropic, openai-codex); omit to pick interactively",
    "memory": "Inspect Mnemopi semantic knowledge graph assertions",
    "models": "ls (default) | find | refresh | <provider>",
    "native-shell": "CLI subcommand",
    "play": "Recording to play (default: the newest /record capture)",
    "plugin": "Plugin action",
    "predict": "CLI subcommand",
    "ps": "list (default), info, logs, stop, kill, or restart",
    "read": "Path, URL, or internal URI to read (append :sel for line ranges or raw mode, e.g. src/foo.ts:50-100)",
    "render": "Session file path or id prefix (default: most recent for cwd)",
    "say": "Text to speak (or use --file)",
    "session": "pack, unpack, moved, or lease",
    "settings": "Share viewer/upload base used by /share (encrypted blob upload + viewer; links are <base>/<id>#<key>",
    "setup": "Optional component to install",
    "share": "Session id (prefix) or path to a session .jsonl",
    "shell": "Set working directory for commands",
    "skill": "Skill action",
    "ssh": "SSH action",
    "stats": "Port for the dashboard server",
    "stream": "Stream title (defaults to the current directory name)",
    "tiny-models": "Action to perform",
    "token": "Provider ID (e.g. anthropic, openai)",
    "toks": "File path, or literal text when it does not name a file",
    "ttsr": "TTSR action",
    "undo": "Revert recent turn file modifications",
    "update": "Force update",
    "usage": "Optional subcommand to execute",
    "web-search": "Search query text",
    "webhooks": "View status of external app webhooks",
    "worktree": "list (default), clear, or add",
    "wt": "Alias for worktree",
}

# ---------------------------------------------------------------------------
# All Slash Commands Catalog
# ---------------------------------------------------------------------------
ALL_SLASH_COMMANDS: List[Dict[str, Any]] = [

    {"name": "clear", "category": "core", "description": "Core command", "usage": "/clear"},
    {"name": "exit", "category": "core", "description": "Core command", "usage": "/exit"},
    {"name": "help", "category": "core", "description": "Core command", "usage": "/help"},
    {"name": "keys", "category": "core", "description": "Core command", "usage": "/keys"},
    {"name": "mode", "category": "core", "description": "Core command", "usage": "/mode"},
    {"name": "model", "category": "core", "description": "Core command", "usage": "/model"},
    {"name": "profile", "category": "core", "description": "Core command", "usage": "/profile"},
    {"name": "quit", "category": "core", "description": "Core command", "usage": "/quit"},
    {"name": "reset", "category": "core", "description": "Core command", "usage": "/reset"},
    {"name": "toggle", "category": "core", "description": "Core command", "usage": "/toggle"},
    {"name": "fresh", "category": "pi", "description": "Pi coding command", "usage": "/fresh"},
    {"name": "m", "category": "pi", "description": "Pi coding command", "usage": "/m"},
    {"name": "new", "category": "pi", "description": "Pi coding command", "usage": "/new"},
    {"name": "plugins", "category": "pi", "description": "Pi coding command", "usage": "/plugins"},
    {"name": "session", "category": "pi", "description": "Pi coding command", "usage": "/session"},
    {"name": "tokens", "category": "pi", "description": "Pi coding command", "usage": "/tokens"},
    {"name": "tools", "category": "pi", "description": "Pi coding command", "usage": "/tools"},
    {"name": "undo", "category": "pi", "description": "Pi coding command", "usage": "/undo"},
    {"name": "moa", "category": "hermes", "description": "Convene Mixture-of-Agents multi-model reasoning council", "usage": "/moa <query>"},
    {"name": "egress", "category": "hermes", "description": "Configure iron-proxy egress credential firewall whitelist", "usage": "/egress [status|allow <domain>]"},
    {"name": "timeline", "category": "hermes", "description": "Render visual ASCII conversation timeline DAG and branches", "usage": "/timeline"},
    {"name": "environment", "category": "hermes", "description": "Select active execution backend (local, docker, ssh)", "usage": "/environment [local|docker|ssh]"},
    {"name": "personality", "category": "hermes", "description": "Switch agent persona/soul profile", "usage": "/personality <name>"},
    {"name": "pet", "category": "hermes", "description": "Interact with Petdex terminal companion mascot", "usage": "/pet"},
    {"name": "hatch", "category": "hermes", "description": "Hatch a new Petdex companion mascot", "usage": "/hatch"},
    {"name": "curator", "category": "hermes", "description": "Extract and synthesize facts from current conversation into memory graph", "usage": "/curator"},
    {"name": "fast", "category": "hermes", "description": "Quickly switch to fast model tier (e.g. gemini-2.5-flash)", "usage": "/fast"},
    {"name": "learn", "category": "hermes", "description": "Run Hermes self-evolution optimizer on failed session trajectories", "usage": "/learn"},
    {"name": "memory", "category": "hermes", "description": "Inspect Mnemopi semantic knowledge graph assertions", "usage": "/memory"},
    {"name": "reasoning", "category": "hermes", "description": "Configure chain-of-thought visibility and model reasoning parameters", "usage": "/reasoning"},
    {"name": "rollback", "category": "hermes", "description": "Rewind conversation turn history to turn N", "usage": "/rollback"},
    {"name": "smart", "category": "hermes", "description": "Quickly switch to smart reasoning model tier (e.g. claude-sonnet)", "usage": "/smart"},
    {"name": "snapshot", "category": "hermes", "description": "Create an immutable named snapshot of session state and files", "usage": "/snapshot"},
    {"name": "cron", "category": "openclaw", "description": "View or trigger autonomous scheduled recurring tasks", "usage": "/cron"},
    {"name": "doctor", "category": "openclaw", "description": "Run diagnostic audit of auth, databases, node environment, and plugins", "usage": "/doctor"},
    {"name": "gateway", "category": "openclaw", "description": "Inspect or control port 18789 Gateway server (status, start, stop)", "usage": "/gateway"},
    {"name": "install", "category": "openclaw", "description": "Auto-install missing dependencies for a skill (npm/pip/uv)", "usage": "/install"},
    {"name": "permission", "category": "openclaw", "description": "Configure dangerous command permission gate (allow, deny, list)", "usage": "/permission"},
    {"name": "status", "category": "openclaw", "description": "Show system health, gateway status, and channel endpoints", "usage": "/status"},
    {"name": "webhooks", "category": "openclaw", "description": "View status of external app webhooks (Telegram, Discord, Slack)", "usage": "/webhooks"},
    {"name": "add", "category": "omp", "description": "Add an SSH host", "usage": "/add"},
    {"name": "add-dir", "category": "omp", "description": "Add a workspace directory to this session (multi-root)", "usage": "/add-dir"},
    {"name": "advisor", "category": "omp", "description": "Toggle the advisor (a second model that reviews each turn and injects notes)", "usage": "/advisor"},
    {"name": "agents", "category": "omp", "description": "Open the agents hub (per-agent model, prewalk, and advisor)", "usage": "/agents"},
    {"name": "all", "category": "omp", "description": "Write a zip with the main transcript, LLM request JSON, and one file per subagent", "usage": "/all"},
    {"name": "anon", "category": "omp", "description": "Write a zip of anonymized session JSONL (contents redacted, metadata kept) plus subagents", "usage": "/anon"},
    {"name": "append", "category": "omp", "description": "Append a task; phase fuzzy-matched or auto-created", "usage": "/append"},
    {"name": "branch", "category": "omp", "description": "Rewind to a previous message, keeping the old path as a branch", "usage": "/branch"},
    {"name": "browser", "category": "omp", "description": "Toggle browser eval-prelude headless vs visible mode", "usage": "/browser"},
    {"name": "btw", "category": "omp", "description": "Ask a side question, or browse this session", "usage": "/btw"},
    {"name": "budget", "category": "omp", "description": "Adjust the token budget", "usage": "/budget"},
    {"name": "cancel", "category": "omp", "description": "Cancel a running native scan", "usage": "/cancel"},
    {"name": "changelog", "category": "omp", "description": "Show changelog entries", "usage": "/changelog"},
    {"name": "cleanse", "category": "omp", "description": "Detect and fix project diagnostics with weighted parallel subagents", "usage": "/cleanse"},
    {"name": "collab", "category": "omp", "description": "Share this session live via a relay", "usage": "/collab"},
    {"name": "collapse", "category": "omp", "description": "Restore the bounded HUD preview", "usage": "/collapse"},
    {"name": "compact", "category": "omp", "description": "Manually compact the session context", "usage": "/compact"},
    {"name": "compare", "category": "omp", "description": "Compare finding lineage across two scans", "usage": "/compare"},
    {"name": "computer", "category": "omp", "description": "Toggle the native computer-use eval prelude for this session", "usage": "/computer"},
    {"name": "configure", "category": "omp", "description": "Open the advisor configuration editor (TUI)", "usage": "/configure"},
    {"name": "context", "category": "omp", "description": "Browse context usage and detailed content", "usage": "/context"},
    {"name": "copy", "category": "omp", "description": "Pick text or code from the conversation to copy", "usage": "/copy"},
    {"name": "debug", "category": "omp", "description": "Open debug tools selector", "usage": "/debug"},
    {"name": "delete", "category": "omp", "description": "Delete the current session and start a new one", "usage": "/delete"},
    {"name": "diagnose", "category": "omp", "description": "Run memory backend diagnostics", "usage": "/diagnose"},
    {"name": "dirs", "category": "omp", "description": "List this session", "usage": "/dirs"},
    {"name": "disable", "category": "omp", "description": "Disable an installed plugin", "usage": "/disable"},
    {"name": "discover", "category": "omp", "description": "Browse available plugins", "usage": "/discover"},
    {"name": "disposition", "category": "omp", "description": "Set a finding disposition with rationale", "usage": "/disposition"},
    {"name": "done", "category": "omp", "description": "Mark task/phase/all completed (fuzzy-matched)", "usage": "/done"},
    {"name": "drop", "category": "omp", "description": "Drop the current goal", "usage": "/drop"},
    {"name": "dump", "category": "omp", "description": "Copy the advisor", "usage": "/dump"},
    {"name": "edit", "category": "omp", "description": "Open todos in $EDITOR (Markdown round-trip)", "usage": "/edit"},
    {"name": "elide", "category": "omp", "description": "Strip tool results + large blocks (default)", "usage": "/elide"},
    {"name": "enable", "category": "omp", "description": "Enable an installed plugin", "usage": "/enable"},
    {"name": "enqueue", "category": "omp", "description": "Enqueue memory consolidation maintenance", "usage": "/enqueue"},
    {"name": "expand", "category": "omp", "description": "Show every phase and task in the HUD", "usage": "/expand"},
    {"name": "export", "category": "omp", "description": "Export session to HTML file", "usage": "/export"},
    {"name": "extended-context", "category": "omp", "description": "Toggle extended context windows", "usage": "/extended-context"},
    {"name": "extensions", "category": "omp", "description": "Open Extension Control Center dashboard", "usage": "/extensions"},
    {"name": "force", "category": "omp", "description": "Force next turn to use a specific tool", "usage": "/force"},
    {"name": "forget-approvals", "category": "omp", "description": "Forget every file and folder approval Ask and remember has recorded", "usage": "/forget-approvals"},
    {"name": "fork", "category": "omp", "description": "Fork this session, or open it in a multiplexer pane or window", "usage": "/fork"},
    {"name": "full", "category": "omp", "description": "Show full, untruncated command lines", "usage": "/full"},
    {"name": "git", "category": "omp", "description": "Open the git UI (split diff viewer, staging, commit composer)", "usage": "/git"},
    {"name": "goal", "category": "omp", "description": "Toggle goal mode (persistent autonomous objective for this session)", "usage": "/goal"},
    {"name": "guided-goal", "category": "omp", "description": "Have the agent interview you in chat, then set up goal mode", "usage": "/guided-goal"},
    {"name": "handoff", "category": "omp", "description": "Summarize the session into a handoff document and compact in place", "usage": "/handoff"},
    {"name": "headless", "category": "omp", "description": "Switch to headless mode", "usage": "/headless"},
    {"name": "hotkeys", "category": "omp", "description": "Show all keyboard shortcuts", "usage": "/hotkeys"},
    {"name": "hub", "category": "omp", "description": "Open the live Agent Hub (subagents, including finished ones)", "usage": "/hub"},
    {"name": "images", "category": "omp", "description": "Strip image blocks", "usage": "/images"},
    {"name": "import", "category": "omp", "description": "Import SARIF or a Codex Security bundle", "usage": "/import"},
    {"name": "info", "category": "omp", "description": "Show session info and stats", "usage": "/info"},
    {"name": "installed", "category": "omp", "description": "List installed marketplace plugins", "usage": "/installed"},
    {"name": "jobs", "category": "omp", "description": "Show async background jobs status", "usage": "/jobs"},
    {"name": "join", "category": "omp", "description": "Join a shared collab session", "usage": "/join"},
    {"name": "kill", "category": "omp", "description": "Cancel a running background job", "usage": "/kill"},
    {"name": "last", "category": "omp", "description": "Show the last N releases (default 1)", "usage": "/last"},
    {"name": "leave", "category": "omp", "description": "Leave the collab session", "usage": "/leave"},
    {"name": "list", "category": "omp", "description": "List active local Collab hosts (no links; use `omp collab link`)", "usage": "/list"},
    {"name": "live", "category": "omp", "description": "Start Codex-backed realtime voice mode", "usage": "/live"},
    {"name": "local", "category": "omp", "description": "Open a session local:// file in $EDITOR (picker when no name given)", "usage": "/local"},
    {"name": "login", "category": "omp", "description": "Login with OAuth provider", "usage": "/login"},
    {"name": "logout", "category": "omp", "description": "Logout from OAuth provider", "usage": "/logout"},
    {"name": "loop", "category": "omp", "description": "Queue a message for after the agent yields", "usage": "/loop"},
    {"name": "marketplace", "category": "omp", "description": "Manage marketplace plugin sources and installed plugins", "usage": "/marketplace"},
    {"name": "mcp", "category": "omp", "description": "Manage MCP servers (add, list, remove, test)", "usage": "/mcp"},
    {"name": "modelpreset", "category": "omp", "description": "Save and switch model presets (role models + thinking level)", "usage": "/modelpreset"},
    {"name": "move", "category": "omp", "description": "Move the current session to a different directory", "usage": "/move"},
    {"name": "notifications", "category": "omp", "description": "Show notification capabilities and subscriptions", "usage": "/notifications"},
    {"name": "off", "category": "omp", "description": "Disable the advisor", "usage": "/off"},
    {"name": "omfg", "category": "omp", "description": "Forge a TTSR rule from a complaint to stop a recurring behavior", "usage": "/omfg"},
    {"name": "on", "category": "omp", "description": "Enable the advisor", "usage": "/on"},
    {"name": "open", "category": "omp", "description": "Open the last link from the conversation in your browser (or pick one with /copy)", "usage": "/open"},
    {"name": "pane", "category": "omp", "description": "Open the fork in a new terminal pane", "usage": "/pane"},
    {"name": "pause", "category": "omp", "description": "Freeze all agents (main, subagents, advisor) until resumed", "usage": "/pause"},
    {"name": "pin", "category": "omp", "description": "Pin or unpin a session at the top of the resume list", "usage": "/pin"},
    {"name": "plan", "category": "omp", "description": "Create an immutable security scan plan", "usage": "/plan"},
    {"name": "plan-review", "category": "omp", "description": "Re-open the plan review for the latest plan (plan mode only)", "usage": "/plan-review"},
    {"name": "prewalk", "category": "omp", "description": "Arm, restart, or cancel a one-shot model handoff", "usage": "/prewalk"},
    {"name": "prompts", "category": "omp", "description": "List available prompts from connected servers", "usage": "/prompts"},
    {"name": "providers", "category": "omp", "description": "Configure sign-in and web search providers", "usage": "/providers"},
    {"name": "queue", "category": "omp", "description": "Show pending memory deltas awaiting consolidation", "usage": "/queue"},
    {"name": "ratchet", "category": "omp", "description": "Build (or reuse) an eval for an LLM flow, then hillclimb it unattended", "usage": "/ratchet"},
    {"name": "reauth", "category": "omp", "description": "Reauthorize OAuth for a server", "usage": "/reauth"},
    {"name": "rebuild", "category": "omp", "description": "Alias for enqueue", "usage": "/rebuild"},
    {"name": "reconnect", "category": "omp", "description": "Reconnect to a specific MCP server", "usage": "/reconnect"},
    {"name": "record", "category": "omp", "description": "Start or stop recording this screen to a replayable file (omp play)", "usage": "/record"},
    {"name": "reload", "category": "omp", "description": "Force reload MCP runtime tools", "usage": "/reload"},
    {"name": "reload-plugins", "category": "omp", "description": "Reload all plugins (skills, commands, hooks, tools, agents, MCP)", "usage": "/reload-plugins"},
    {"name": "remove", "category": "omp", "description": "Remove an SSH host", "usage": "/remove"},
    {"name": "remove-dir", "category": "omp", "description": "Remove a workspace directory from this session", "usage": "/remove-dir"},
    {"name": "rename", "category": "omp", "description": "Rename the current session (omit title to generate)", "usage": "/rename"},
    {"name": "resources", "category": "omp", "description": "List available resources from connected servers", "usage": "/resources"},
    {"name": "restart", "category": "omp", "description": "Restart omp with the same launch flags, resuming this session and its running subagents", "usage": "/restart"},
    {"name": "resume", "category": "omp", "description": "Resume a different session", "usage": "/resume"},
    {"name": "retry", "category": "omp", "description": "Retry the last failed agent turn", "usage": "/retry"},
    {"name": "rm", "category": "omp", "description": "Remove task/phase/all (fuzzy-matched)", "usage": "/rm"},
    {"name": "save", "category": "omp", "description": "Save the current role models and thinking level", "usage": "/save"},
    {"name": "scan", "category": "omp", "description": "Start a planned or newly planned native scan", "usage": "/scan"},
    {"name": "scans", "category": "omp", "description": "List stored project security scans", "usage": "/scans"},
    {"name": "search", "category": "omp", "description": "Search the skill registry", "usage": "/search"},
    {"name": "security", "category": "omp", "description": "Plan, run, inspect, import, and compare OMP-native security scans", "usage": "/security"},
    {"name": "set", "category": "omp", "description": "Set or replace the goal", "usage": "/set"},
    {"name": "settings", "category": "omp", "description": "Open settings menu", "usage": "/settings"},
    {"name": "setup", "category": "omp", "description": "Open provider setup", "usage": "/setup"},
    {"name": "shake", "category": "omp", "description": "Drop heavy content from context (tool results, large blocks)", "usage": "/shake"},
    {"name": "share", "category": "omp", "description": "Share session via an encrypted link (share server or secret gist)", "usage": "/share"},
    {"name": "show", "category": "omp", "description": "Render a scan or security:// resource", "usage": "/show"},
    {"name": "skillful", "category": "omp", "description": "Toggle listing available skills in the system prompt (session only)", "usage": "/skillful"},
    {"name": "skills", "category": "omp", "description": "Search, install, and update skills from the skills.omp.sh registry", "usage": "/skills"},
    {"name": "slow", "category": "omp", "description": "Toggle slow mode: flex tier on OpenAI/Google; on Anthropic, continue at low priority after", "usage": "/slow"},
    {"name": "smithery-login", "category": "omp", "description": "Login to Smithery and cache API key", "usage": "/smithery-login"},
    {"name": "smithery-logout", "category": "omp", "description": "Remove cached Smithery API key", "usage": "/smithery-logout"},
    {"name": "smithery-search", "category": "omp", "description": "Search Smithery registry and deploy an MCP server", "usage": "/smithery-search"},
    {"name": "ssh", "category": "omp", "description": "Manage SSH hosts (add, list, remove)", "usage": "/ssh"},
    {"name": "start", "category": "omp", "description": "Mark task in_progress (fuzzy-matched)", "usage": "/start"},
    {"name": "stats", "category": "omp", "description": "Show memory backend statistics", "usage": "/stats"},
    {"name": "stop", "category": "omp", "description": "Stop sharing", "usage": "/stop"},
    {"name": "switch", "category": "omp", "description": "Toggle fast service (OpenAI service_tier=priority or ultrafast, Anthropic speed=fast, Goog", "usage": "/switch"},
    {"name": "sync", "category": "omp", "description": "Run memory consolidation now", "usage": "/sync"},
    {"name": "tab", "category": "omp", "description": "Alias for window", "usage": "/tab"},
    {"name": "tan", "category": "omp", "description": "Run a full background agent on tangential work", "usage": "/tan"},
    {"name": "test", "category": "omp", "description": "Test connection to a server", "usage": "/test"},
    {"name": "thinking", "category": "omp", "description": "Drop all thinking blocks", "usage": "/thinking"},
    {"name": "todo", "category": "omp", "description": "View or modify the agent", "usage": "/todo"},
    {"name": "trace", "category": "omp", "description": "Open this session", "usage": "/trace"},
    {"name": "tree", "category": "omp", "description": "Navigate session tree (switch branches)", "usage": "/tree"},
    {"name": "ultra", "category": "omp", "description": "Enable Ultrafast (OpenAI API, or Codex and relay models that offer it)", "usage": "/ultra"},
    {"name": "unauth", "category": "omp", "description": "Remove OAuth auth from a server", "usage": "/unauth"},
    {"name": "uninstall", "category": "omp", "description": "Uninstall a plugin (selector if no args)", "usage": "/uninstall"},
    {"name": "unpin", "category": "omp", "description": "Unpin a session from the top of the resume list", "usage": "/unpin"},
    {"name": "update", "category": "omp", "description": "Update marketplace catalog(s)", "usage": "/update"},
    {"name": "upgrade", "category": "omp", "description": "Upgrade outdated plugins", "usage": "/upgrade"},
    {"name": "usage", "category": "omp", "description": "Show provider usage and limits", "usage": "/usage"},
    {"name": "validate", "category": "omp", "description": "Validate one finding with OMP-native tools", "usage": "/validate"},
    {"name": "vibe", "category": "omp", "description": "Toggle vibe mode (direct persistent fast/good worker sessions; read-only toolset)", "usage": "/vibe"},
    {"name": "view", "category": "omp", "description": "Share a read-only link (guests can watch, not prompt)", "usage": "/view"},
    {"name": "visible", "category": "omp", "description": "Switch to visible mode", "usage": "/visible"},
    {"name": "window", "category": "omp", "description": "Open the fork in a new multiplexer window", "usage": "/window"},
    {"name": "wt", "category": "omp", "description": "Move this session into a new worktree, changes included", "usage": "/wt"},
]


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
        for item in ALL_SLASH_COMMANDS:
            self.register(CommandDefinition(
                name=item["name"],
                category=item["category"],
                description=item["description"],
                usage=item["usage"],
            ))

    def register(self, cmd: CommandDefinition):
        self.commands[cmd.name.lower()] = cmd
        for alias in cmd.aliases:
            self.commands[alias.lower()] = cmd

    def apply_profile(self, profile_name: str) -> bool:
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
