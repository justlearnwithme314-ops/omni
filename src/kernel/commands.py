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
    "skin": "View or switch truecolor terminal themes (tokyo-night, catppuccin, matrix, nord, solarized)",
    "split": "Render split-screen dual pane layout (pi-tui style)",
    "prewalk": "Run 2-stage fast-model prewalk and smart-model code handoff",
    "mm": "Manage project mental models cards (.omni/mental_models/)",
    "astgrep": "Search code using AST structural pattern matching",
    "whoami": "Show your sender id, permission tier, and role",
    "openclaw": "Run the OpenClaw setup and repair assistant",
    "allowlist": "List, add, or remove security allowlist entries",
    "approve": "Review, approve, or deny pending tool execution requests",
    "export-session": "Export current session to an HTML file",
    "export-trajectory": "Export a JSONL trajectory bundle",
    "tts": "Control text-to-speech voice synthesis",
    "steer": "Send live guidance to the active run",
    "followup": "Queue a followup turn",
    "think": "Set thinking level (xhigh, max, adaptive, off)",
    "verbose": "Toggle verbose execution logging mode",
    "trace": "Toggle plugin trace lines",
    "elevated": "Toggle elevated execution permissions",
    "subagents": "Inspect active and completed subagent runs",
    "pair": "Generate companion pairing setup code or QR code",
    "completion": "Generate shell completion script",
    "skills": "List and inspect available skills",
    "secrets": "Secrets runtime controls",
    "security": "Audit local config and state for common security foot-guns",
    "directory": "Lookup contact and group IDs (self, peers, groups) for supported chat channels",
    "channels": "Manage connected chat channels and accounts",
    "plugins": "Manage OpenClaw plugins and extensions",
    "pairing": "Secure DM pairing (approve inbound requests)",
    "qr": "Generate a mobile pairing QR code and setup code",
    "hooks": "Manage internal agent hooks",
    "proxy": "Run the OpenClaw debug proxy and inspect captured traffic",
    "qa": "Run QA scenarios and launch the private QA debugger UI",
    "docs": "Search the live OpenClaw docs",
    "dns": "DNS helpers for wide-area discovery (Tailscale + CoreDNS)",
    "automations": "Manage automations (alias for cron)",
    "chat": "Open a local terminal UI (alias for tui --local)",
    "terminal": "Open a local terminal UI (alias for tui --local)",
    "resume": "Resume a recent Gateway session in the TUI",
    "tui": "Open a terminal UI connected to the Gateway",
    "attach": "Attach Claude Code to a gateway session with scoped MCP tools",
    "worktrees": "Create, inspect, restore, and clean up managed worktrees",
    "sandbox": "Manage sandbox containers (Docker-based agent isolation)",
    "worker": "Run the restricted cloud worker runtime",
    "connect": "Connect this machine to an OpenClaw Gateway as a companion node",
    "node": "Run and manage the headless node host service",
    "users": "Manage durable user profiles and email aliases",
    "devices": "Device pairing and auth tokens",
    "nodes": "Manage gateway-owned nodes (pairing, status, invoke, and media)",
    "exec-policy": "Show or synchronize requested exec policy with host approvals",
    "exec-approvals": "Manage exec approvals (alias for approvals)",
    "approvals": "Manage approval policy and pending requests",
    "capability": "Run provider capability commands (fallback alias: infer)",
    "infer": "Run provider-backed inference commands through a stable CLI surface",
    "telemetry": "Inspect and manage anonymous usage telemetry",
    "promos": "Discover and claim promotional model offers from ClawHub",
    "system": "System tools (events, heartbeat, presence)",
    "logs": "Tail gateway file logs via RPC",
    "daemon": "Manage the Gateway service (launchd/systemd/schtasks)",
    "sessions": "List stored conversation sessions",
    "audit": "Inspect activity records and exact-run identity context",
    "health": "Fetch health from the running gateway",
    "status": "Show channel health and recent session recipients",
    "agent": "Run an agent turn via the Gateway (use --local for embedded)",
    "transcripts": "Inspect stored transcripts",
    "mcp": "Manage OpenClaw mcp.servers config and channel bridge",
    "message": "Send, read, and manage messages and channel actions",
    "uninstall": "Uninstall the gateway service + local data",
    "reset": "Reset local config/state (keeps the CLI installed)",
    "dashboard": "Open the Control UI with your current token",
    "triage": "Collect sanitized diagnostics and open a local coding agent for repair",
    "storage": "List, initialize, and test configured storage locations",
    "migrate": "Import state from another agent system",
    "database": "Inspect database compatibility, manage ownership, and port database stores",
    "backup": "Create, verify, and restore backup archives and SQLite snapshots",
    "templates": "Author and add OpenClaw Templates",
    "configure": "Interactive configuration for credentials, channels, gateway, and agent defaults",
    "onboard": "Guided setup for auth, models, Gateway, workspace, channels, and skills",

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
    {"name": "diff", "category": "omp", "description": "View syntax-highlighted git diff of recent turn modifications", "usage": "/diff"},
    {"name": "skin", "category": "core", "description": "View or switch truecolor themes (tokyo-night, catppuccin, matrix, nord, solarized)", "usage": "/skin [name]"},
    {"name": "split", "category": "omp", "description": "Render split-screen dual pane layout (pi-tui style)", "usage": "/split"},
    {"name": "prewalk", "category": "omp", "description": "Run 2-stage fast-model prewalk and smart-model code handoff", "usage": "/prewalk <task>"},
    {"name": "mm", "category": "omp", "description": "Manage project mental models cards (.omni/mental_models/)", "usage": "/mm [list|add <slug>]"},
    {"name": "astgrep", "category": "omp", "description": "Search code using AST structural pattern matching", "usage": "/astgrep <pattern>"},
    {"name": "secrets", "category": "openclaw", "description": "Manage secrets runtime and environment variables", "usage": "/secrets [list|get|set]"},
    {"name": "security", "category": "openclaw", "description": "Audit local configuration and state for security risks", "usage": "/security"},
    {"name": "channels", "category": "openclaw", "description": "Manage connected chat channels (Discord, Telegram, WhatsApp, Slack)", "usage": "/channels [list|test]"},
    {"name": "pair", "category": "openclaw", "description": "Generate companion pairing setup code or QR code", "usage": "/pair [qr|code]"},
    {"name": "nodes", "category": "openclaw", "description": "Manage gateway-owned paired companion nodes", "usage": "/nodes [list|pair]"},
    {"name": "bash", "category": "openclaw", "description": "Run shell commands on the host", "usage": "/bash <command>"},
    {"name": "queue", "category": "openclaw", "description": "Adjust turn queue settings (fifo, steer, drop)", "usage": "/queue [mode]"},
    {"name": "exec", "category": "openclaw", "description": "Configure execution environment defaults (cwd, env)", "usage": "/exec [defaults]"},
    {"name": "elevated", "category": "openclaw", "description": "Toggle elevated execution permissions (sudo/admin)", "usage": "/elevated [on|off]"},
    {"name": "reasoning", "category": "hermes", "description": "Toggle reasoning/thinking visibility in output", "usage": "/reasoning [on|off]"},
    {"name": "fast", "category": "hermes", "description": "Toggle fast priority inference mode", "usage": "/fast [on|off]"},
    {"name": "trace", "category": "openclaw", "description": "Toggle plugin trace lines in the transcript", "usage": "/trace [on|off]"},
    {"name": "verbose", "category": "openclaw", "description": "Toggle verbose execution logging mode", "usage": "/verbose [on|off]"},
    {"name": "think", "category": "openclaw", "description": "Set model thinking level (xhigh, max, adaptive, off)", "usage": "/think [level]"},
    {"name": "compact", "category": "omp", "description": "Trigger manual context compaction", "usage": "/compact"},
    {"name": "name", "category": "openclaw", "description": "Name or rename the active session", "usage": "/name <title>"},
    {"name": "send", "category": "openclaw", "description": "Control outbound message delivery policy (allow/deny)", "usage": "/send [allow|deny|inherit]"},
    {"name": "activation", "category": "openclaw", "description": "Set group chat activation mode (mention, all, steer)", "usage": "/activation [mention|all|steer]"},
    {"name": "update", "category": "openclaw", "description": "Check for updates, apply, and restart", "usage": "/update"},
    {"name": "restart", "category": "openclaw", "description": "Restart the OpenClaw service cleanly", "usage": "/restart"},
    {"name": "stop", "category": "openclaw", "description": "Stop the currently running agent turn immediately", "usage": "/stop"},
    {"name": "usage", "category": "openclaw", "description": "Show provider token usage, limits, and cost summaries", "usage": "/usage"},
    {"name": "followup", "category": "openclaw", "description": "Queue message as an immediate follow-up turn", "usage": "/followup <message>"},
    {"name": "steer", "category": "openclaw", "description": "Send live guidance to the active run in this session", "usage": "/steer <guidance>"},
    {"name": "agents", "category": "openclaw", "description": "List agent personas and conversation bindings", "usage": "/agents"},
    {"name": "acp", "category": "openclaw", "description": "Manage ACP sessions and IDE runtime options", "usage": "/acp"},
    {"name": "subagents", "category": "openclaw", "description": "Inspect active and completed subagent runs for session", "usage": "/subagents"},
    {"name": "session", "category": "openclaw", "description": "Manage conversation bindings and session lifecycle", "usage": "/session [list|switch|settings]"},
    {"name": "whoami", "category": "openclaw", "description": "Show your channel sender id, permission tier, and role", "usage": "/whoami"},
    {"name": "tts", "category": "openclaw", "description": "Control text-to-speech voice synthesis (on/off/status)", "usage": "/tts [on|off|status]"},
    {"name": "export-trajectory", "category": "openclaw", "description": "Export a JSONL trajectory bundle for the active session", "usage": "/export-trajectory [file]"},
    {"name": "export-session", "category": "openclaw", "description": "Export current session to a standalone HTML file in the workspace", "usage": "/export-session [path]"},
    {"name": "fork", "category": "openclaw", "description": "Fork this conversation into a separate thread branch", "usage": "/fork [name]"},
    {"name": "btw", "category": "openclaw", "description": "Ask a side question without changing future session context", "usage": "/btw <question>"},
    {"name": "context", "category": "openclaw", "description": "Explain how context is built and token allocation", "usage": "/context"},
    {"name": "approve", "category": "openclaw", "description": "Review, approve, or deny pending tool execution requests", "usage": "/approve [id|all|deny]"},
    {"name": "allowlist", "category": "openclaw", "description": "List, add, or remove security allowlist entries", "usage": "/allowlist [list|add|remove] [pattern]"},
    {"name": "openclaw", "category": "openclaw", "description": "Run the OpenClaw setup and repair assistant", "usage": "/openclaw"},
    {"name": "login", "category": "openclaw", "description": "Connect a model provider or OAuth session", "usage": "/login [provider]"},
    {"name": "diagnostics", "category": "openclaw", "description": "Request Gateway diagnostics and feedback bundle export", "usage": "/diagnostics [note]"},
    {"name": "goal", "category": "openclaw", "description": "Show or control the active autonomous goal", "usage": "/goal [status|start|edit|pause|resume|complete|block|clear]"},
    {"name": "loop", "category": "openclaw", "description": "Run and manage recurring autonomous prompts", "usage": "/loop [interval] <prompt>"},
    {"name": "learn", "category": "hermes", "description": "Save lessons from recent work to skills or memory", "usage": "/learn [request]"},
    {"name": "dashboard", "category": "openclaw", "description": "Create or update this session's web dashboard view", "usage": "/dashboard [request]"},
    {"name": "skill", "category": "openclaw", "description": "Run an OpenClaw skill directly by name", "usage": "/skill <name> [input]"},
    {"name": "commands", "category": "core", "description": "List all OpenClaw slash commands", "usage": "/commands"},
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
    {"name": "tokens", "category": "core", "description": "Pi coding command", "usage": "/tokens"},
    {"name": "tools", "category": "core", "description": "Pi coding command", "usage": "/tools"},
    {"name": "undo", "category": "pi", "description": "Pi coding command", "usage": "/undo"},
    {"name": "moa", "category": "hermes", "description": "Convene Mixture-of-Agents multi-model reasoning council", "usage": "/moa <query>"},
    {"name": "egress", "category": "hermes", "description": "Configure iron-proxy egress credential firewall whitelist", "usage": "/egress [status|allow <domain>]"},
    {"name": "timeline", "category": "hermes", "description": "Render visual ASCII conversation timeline DAG and branches", "usage": "/timeline"},
    {"name": "environment", "category": "hermes", "description": "Select active execution backend (local, docker, ssh)", "usage": "/environment [local|docker|ssh]"},
    {"name": "personality", "category": "hermes", "description": "Switch agent persona/soul profile", "usage": "/personality <name>"},
    {"name": "pet", "category": "hermes", "description": "Interact with Petdex terminal companion mascot", "usage": "/pet"},
    {"name": "hatch", "category": "hermes", "description": "Hatch a new Petdex companion mascot", "usage": "/hatch"},
    {"name": "curator", "category": "hermes", "description": "Extract and synthesize facts from current conversation into memory graph", "usage": "/curator"},
    {"name": "memory", "category": "hermes", "description": "Inspect Mnemopi semantic knowledge graph assertions", "usage": "/memory"},
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
    {"name": "all", "category": "omp", "description": "Write a zip with the main transcript, LLM request JSON, and one file per subagent", "usage": "/all"},
    {"name": "anon", "category": "omp", "description": "Write a zip of anonymized session JSONL (contents redacted, metadata kept) plus subagents", "usage": "/anon"},
    {"name": "append", "category": "omp", "description": "Append a task; phase fuzzy-matched or auto-created", "usage": "/append"},
    {"name": "branch", "category": "omp", "description": "Rewind to a previous message, keeping the old path as a branch", "usage": "/branch"},
    {"name": "browser", "category": "omp", "description": "Toggle browser eval-prelude headless vs visible mode", "usage": "/browser"},
    {"name": "budget", "category": "omp", "description": "Adjust the token budget", "usage": "/budget"},
    {"name": "cancel", "category": "omp", "description": "Cancel a running native scan", "usage": "/cancel"},
    {"name": "changelog", "category": "omp", "description": "Show changelog entries", "usage": "/changelog"},
    {"name": "cleanse", "category": "omp", "description": "Detect and fix project diagnostics with weighted parallel subagents", "usage": "/cleanse"},
    {"name": "collab", "category": "omp", "description": "Share this session live via a relay", "usage": "/collab"},
    {"name": "collapse", "category": "omp", "description": "Restore the bounded HUD preview", "usage": "/collapse"},
    {"name": "compare", "category": "omp", "description": "Compare finding lineage across two scans", "usage": "/compare"},
    {"name": "computer", "category": "omp", "description": "Toggle the native computer-use eval prelude for this session", "usage": "/computer"},
    {"name": "configure", "category": "omp", "description": "Open the advisor configuration editor (TUI)", "usage": "/configure"},
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
    {"name": "full", "category": "omp", "description": "Show full, untruncated command lines", "usage": "/full"},
    {"name": "git", "category": "omp", "description": "Open the git UI (split diff viewer, staging, commit composer)", "usage": "/git"},
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
    {"name": "logout", "category": "omp", "description": "Logout from OAuth provider", "usage": "/logout"},
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
    {"name": "prompts", "category": "omp", "description": "List available prompts from connected servers", "usage": "/prompts"},
    {"name": "providers", "category": "omp", "description": "Configure sign-in and web search providers", "usage": "/providers"},
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
    {"name": "resume", "category": "omp", "description": "Resume a different session", "usage": "/resume"},
    {"name": "retry", "category": "omp", "description": "Retry the last failed agent turn", "usage": "/retry"},
    {"name": "rm", "category": "omp", "description": "Remove task/phase/all (fuzzy-matched)", "usage": "/rm"},
    {"name": "save", "category": "omp", "description": "Save the current role models and thinking level", "usage": "/save"},
    {"name": "scan", "category": "omp", "description": "Start a planned or newly planned native scan", "usage": "/scan"},
    {"name": "scans", "category": "omp", "description": "List stored project security scans", "usage": "/scans"},
    {"name": "search", "category": "omp", "description": "Search the skill registry", "usage": "/search"},
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
    {"name": "switch", "category": "omp", "description": "Toggle fast service (OpenAI service_tier=priority or ultrafast, Anthropic speed=fast, Goog", "usage": "/switch"},
    {"name": "sync", "category": "omp", "description": "Run memory consolidation now", "usage": "/sync"},
    {"name": "tab", "category": "omp", "description": "Alias for window", "usage": "/tab"},
    {"name": "tan", "category": "omp", "description": "Run a full background agent on tangential work", "usage": "/tan"},
    {"name": "test", "category": "omp", "description": "Test connection to a server", "usage": "/test"},
    {"name": "thinking", "category": "omp", "description": "Drop all thinking blocks", "usage": "/thinking"},
    {"name": "todo", "category": "omp", "description": "View or modify the agent", "usage": "/todo"},
    {"name": "tree", "category": "omp", "description": "Navigate session tree (switch branches)", "usage": "/tree"},
    {"name": "ultra", "category": "omp", "description": "Enable Ultrafast (OpenAI API, or Codex and relay models that offer it)", "usage": "/ultra"},
    {"name": "unauth", "category": "omp", "description": "Remove OAuth auth from a server", "usage": "/unauth"},
    {"name": "uninstall", "category": "omp", "description": "Uninstall a plugin (selector if no args)", "usage": "/uninstall"},
    {"name": "unpin", "category": "omp", "description": "Unpin a session from the top of the resume list", "usage": "/unpin"},
    {"name": "upgrade", "category": "omp", "description": "Upgrade outdated plugins", "usage": "/upgrade"},
    {"name": "validate", "category": "omp", "description": "Validate one finding with OMP-native tools", "usage": "/validate"},
    {"name": "vibe", "category": "omp", "description": "Toggle vibe mode (direct persistent fast/good worker sessions; read-only toolset)", "usage": "/vibe"},
    {"name": "view", "category": "omp", "description": "Share a read-only link (guests can watch, not prompt)", "usage": "/view"},
    {"name": "visible", "category": "omp", "description": "Switch to visible mode", "usage": "/visible"},
    {"name": "window", "category": "omp", "description": "Open the fork in a new multiplexer window", "usage": "/window"},
    {"name": "wt", "category": "omp", "description": "Move this session into a new worktree, changes included", "usage": "/wt"},
]

OPENCLAW_COMMAND_NAMES: Set[str] = {
    "commands", "skill", "dashboard", "learn", "loop", "status", "goal", "diagnostics", "login",
    "openclaw", "allowlist", "approve", "context", "btw", "fork", "export-session", "export-trajectory",
    "tts", "whoami", "session", "subagents", "acp", "agents", "steer", "followup", "usage", "stop",
    "restart", "update", "activation", "send", "name", "compact", "think", "verbose", "trace", "fast",
    "reasoning", "elevated", "exec", "queue", "bash", "gateway", "cron", "doctor", "install", "permission",
    "webhooks", "nodes", "pair", "channels", "security", "secrets", "help", "clear", "reset", "exit", "quit"
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
        self.active_profile = active_profile or saved_prof or "full"
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
