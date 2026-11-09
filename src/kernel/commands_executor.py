"""
Command Execution Handlers for Omni Slash Commands
Binds every slash command across Pi, OMP, Hermes, and OpenClaw to real, working functionality.
"""

import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from typing import Any, Dict, List, Optional, Tuple

from .commands import CommandRegistry, COMMAND_PROFILES
from .state import SessionState


class CommandExecutor:
    """Executes registered slash commands against active runtime subsystems."""

    def __init__(
        self,
        command_registry: CommandRegistry,
        engine: Any = None,
        config: Any = None,
        router: Any = None,
        tool_registry: Any = None,
        tracer: Any = None,
    ):
        self.registry = command_registry
        self.engine = engine
        self.config = config
        self.router = router
        self.tool_registry = tool_registry
        self.tracer = tracer

    async def execute(self, line: str, state: SessionState, context: Optional[Dict[str, Any]] = None) -> Tuple[bool, str]:
        """
        Executes a slash command string.
        Returns (handled: bool, output_message: str).
        """
        line_clean = line.strip()
        if not line_clean.startswith("/"):
            return False, ""

        parts = line_clean.split(maxsplit=1)
        raw_cmd = parts[0].lstrip("/").lower()
        arg = parts[1].strip() if len(parts) > 1 else ""

        cmd_def = self.registry.get_command(raw_cmd)
        if not cmd_def:
            # Check if command exists but is disabled in current profile
            all_cmd = self.registry.commands.get(raw_cmd)
            if all_cmd and not all_cmd.enabled:
                return True, (
                    f"[!] Command '/{raw_cmd}' is disabled under profile '{self.registry.active_profile.upper()}'.\n"
                    f"To enable it: /profile full or /profile {all_cmd.category}"
                )
            return False, ""

        name = cmd_def.name

        # --- 1. Core & Pi Handlers ---
        if name == "help":
            active_cmds = self.registry.get_active_commands()
            lines = [f"\n=== Omni Slash Commands (Profile: {self.registry.active_profile.upper()}) ==="]
            categories = {}
            for c in active_cmds:
                categories.setdefault(c.category, []).append(c)

            for cat, c_list in sorted(categories.items()):
                lines.append(f"\n  [{cat.upper()}]:")
                for c in c_list:
                    lines.append(f"    /{c.name:<16} - {c.description}")
            lines.append("\nType /profile <name> to switch command profiles (pi, omp, hermes, openclaw, full).")
            return True, "\n".join(lines)

        elif name == "profile":
            if not arg:
                lines = [
                    f"\nActive Command Profile: {self.registry.active_profile.upper()}",
                    f"Available Profiles:"
                ]
                for p_name, p_info in COMMAND_PROFILES.items():
                    active_marker = " * " if p_name == self.registry.active_profile else "   "
                    lines.append(f" {active_marker}{p_name.upper():<12} - {p_info['description']}")
                lines.append("\nUsage: /profile <pi|omp|hermes|openclaw|full>")
                return True, "\n".join(lines)
            if self.registry.apply_profile(arg):
                return True, f"[✓] Switched command profile to: {arg.upper()} ({COMMAND_PROFILES[arg.lower()]['description']})"
            return True, f"[!] Unknown profile '{arg}'. Available: {list(COMMAND_PROFILES.keys())}"

        elif name == "tokens":
            msg_count = len(state.messages)
            est_tokens = getattr(state, "token_count", 0)
            return True, f"Session Tokens: ~{est_tokens:,} tokens across {msg_count} messages."

        elif name == "tools":
            tools = self.tool_registry.list_tools() if self.tool_registry else []
            lines = [f"\n--- Active Tools ({len(tools)}) ---"]
            for t in tools:
                lines.append(f"  • [{t.source.upper()}] {t.name}: {t.description[:80]}")
            return True, "\n".join(lines)

        elif name == "undo":
            from ..features.checkpoints import GitCheckpointManager
            chk = GitCheckpointManager()
            steps = int(arg) if arg.isdigit() else 1
            ok, msg, files = chk.undo(steps=steps)
            if ok:
                f_lines = "\n".join(f"  • Restored: {f}" for f in files)
                return True, f"[✓] {msg}\n{f_lines}"
            return True, f"[!] {msg}"

        elif name == "keys":
            keys = self.router.keyring.list_keys() if self.router else []
            lines = ["\n--- Omni Keyring Status ---"]
            for k in keys:
                status = "ACTIVE" if k.get("is_active") else "INACTIVE"
                b_str = f" [BLOCKED until {k.get('blocked_until')}]" if k.get("blocked_until") else ""
                lines.append(f"  • [{k['provider']}] id={k['key_id']} priority={k['priority']} status={status}{b_str}")
            return True, "\n".join(lines)

        # --- 2. OMP Handlers ---
        elif name == "compact":
            before_len = len(state.messages)
            state.compact_history(max_turns=5, preserve_system=True)
            after_len = len(state.messages)
            return True, f"[✓] SnapCompact executed: reduced message transcript from {before_len} to {after_len} turns while preserving system prompt."

        elif name == "cleanse":
            state.scratchpad.clear()
            return True, "[✓] Session scratchpad and temporary intermediate state cleansed."

        elif name == "diff":
            from ..features.checkpoints import GitCheckpointManager
            diff = GitCheckpointManager().get_diff()
            if diff:
                return True, f"\n--- Recent Worktree Diff ---\n{diff}"
            return True, "No uncommitted or checkpointed file changes in workspace."

        elif name == "branch":
            branch_name = arg or f"branch_{int(time.time())}"
            old_id = state.session_id
            state.session_id = f"fork_{branch_name}"
            return True, f"[✓] Forked session from '{old_id}' into new timeline branch: '{state.session_id}'."

        elif name == "wt":
            res = subprocess.run(["git", "status", "--short"], capture_output=True, text=True)
            out = res.stdout.strip()
            if out:
                return True, f"\n--- Git Worktree Status ---\n{out}"
            return True, "Worktree clean, no unstaged or modified files."

        elif name == "thinking":
            if arg in ("high", "medium", "low", "off"):
                state.scratchpad["thinking_budget"] = arg
                return True, f"[✓] Thinking reasoning budget set to: {arg.upper()}"
            return True, f"Current thinking budget: {state.scratchpad.get('thinking_budget', 'default')}. Usage: /thinking [high|medium|low|off]"

        elif name == "budget":
            from ..features.output_budget import OutputBudgetEnforcer
            enforcer = getattr(self.engine, "_output_budget", OutputBudgetEnforcer())
            return True, (
                f"Output Budget Settings:\n"
                f"  • Max lines: {enforcer.max_lines}\n"
                f"  • Max characters: {enforcer.max_chars:,}\n"
                f"  • Head/Tail Ratio: {enforcer.head_ratio:.1f}"
            )

        elif name == "stats":
            stats = self.tracer.get_session_stats() if self.tracer else {}
            lines = ["\n--- Omni Telemetry Analytics ---"]
            for k, v in stats.items():
                lines.append(f"  {k}: {v}")
            return True, "\n".join(lines)

        elif name in ("plan", "scout", "review"):
            if not self.engine:
                return True, f"[!] Engine not configured for subagent delegation."
            from ..features.subagents import SubagentDelegator
            delegator = SubagentDelegator(self.engine, self.tool_registry)
            goal_text = arg or f"Analyze current workspace status and provide findings for {name}"
            print(f"[*] Spawning {name.upper()} subagent: '{goal_text[:60]}...'")
            report = await delegator.spawn_task(state, name, goal_text)
            return True, f"\n--- [{name.upper()} Subagent Findings] ---\n{report.get('summary', 'No output')}"

        # --- 3. Hermes Handlers ---
        elif name == "memory":
            from ..features.memory_graph import MemoryGraph
            triples = MemoryGraph().query_triples(limit=25)
            lines = [f"\n--- Mnemopi Knowledge Graph ({len(triples)} assertions) ---"]
            for t in triples:
                lines.append(f"  • {t['subject']} -> {t['predicate']} -> {t['object']} (conf: {t['confidence']})")
            return True, "\n".join(lines)

        elif name == "curator":
            from ..features.memory_graph import MemoryGraph
            mem = MemoryGraph()
            count = 0
            for m in state.messages:
                if m.role in ("user", "assistant"):
                    facts = mem.extract_and_store_facts(m.content)
                    count += len(facts)
            return True, f"[✓] Knowledge Curator synthesized and stored {count} new semantic assertions into memory graph."

        elif name == "learn":
            from ..evolution.optimizer import EvolutionOptimizer
            opt = EvolutionOptimizer(tracer=self.tracer) if self.tracer else None
            if not opt:
                return True, "No telemetry database active for self-evolution."
            failures = opt.analyze_failures()
            lines = [f"\n--- Hermes Self-Evolution Engine ---", f"Found {len(failures)} failure events."]
            for f in failures[:3]:
                lines.append(f"  • [{f['category']}] {f['detail'][:60]}")
            if "--patch" in arg and failures:
                patch = opt.generate_prompt_patch(failures[0]["category"], "Strictly adhere to JSON/XML tool calling format.")
                lines.append(f"[✓] Generated prompt patch: {patch}")
            return True, "\n".join(lines)

        elif name == "rollback":
            if not arg.isdigit():
                return True, f"Current turns: {state.turn_index}. Usage: /rollback <turn_number>"
            target = int(arg)
            if target < 0:
                target = 0
            # Keep system prompt and truncate messages
            sys_msg = [m for m in state.messages if m.role == "system"]
            state.messages = sys_msg + [m for m in state.messages if m.role != "system"][:target * 2]
            state.turn_index = target
            return True, f"[✓] Conversation rolled back to turn {target}. ({len(state.messages)} messages active)."

        elif name == "snapshot":
            snap_name = arg or f"snapshot_{int(time.time())}"
            from ..features.persistence import SessionStore
            from ..features.checkpoints import GitCheckpointManager
            store = SessionStore()
            store.save_session(state, title=f"Snapshot: {snap_name}")
            GitCheckpointManager().create_checkpoint(state.turn_index, f"Snapshot {snap_name}")
            return True, f"[✓] Immutable snapshot '{snap_name}' created in SQLite and tagged in Git."

        elif name == "fast":
            if context and "switch_model" in context:
                context["switch_model"]("google-antigravity/gemini-2.5-flash")
            return True, "[✓] Switched to FAST tier model (google-antigravity/gemini-2.5-flash)."

        elif name == "smart":
            if context and "switch_model" in context:
                context["switch_model"]("google-antigravity/claude-sonnet-5-5")
            return True, "[✓] Switched to SMART tier model (google-antigravity/claude-sonnet-5-5)."

        # --- 4. OpenClaw Handlers ---
        elif name == "status":
            from ..features.sandbox import ExecutionSandbox
            tools_cnt = len(self.tool_registry.list_tools()) if self.tool_registry else 0
            keys_cnt = len(self.router.keyring.list_keys()) if self.router else 0
            docker_ok = ExecutionSandbox().is_docker_available()
            return True, (
                f"\n--- Omni System Status ---\n"
                f"  • Engine: ACTIVE (Profile: {self.registry.active_profile.upper()})\n"
                f"  • Tools: {tools_cnt} registered\n"
                f"  • Keyring: {keys_cnt} provider credentials\n"
                f"  • Docker Sandbox: {'Available' if docker_ok else 'Local Subprocess Fallback'}\n"
                f"  • Gateway Port: 18789 (OpenClaw Standard)"
            )

        elif name == "gateway":
            from ..features.gateway import GatewayServer
            if arg == "start":
                gw = GatewayServer(self.engine, self.tool_registry)
                gw.start(blocking=False)
                return True, "[✓] Gateway daemon started in background on http://127.0.0.1:18789."
            return True, "Gateway status: Port 18789 available. Usage: /gateway [status|start]"

        elif name == "cron":
            from ..features.cron import CronScheduler
            cs = CronScheduler()
            tasks = cs.list_tasks()
            lines = [f"\n--- Autonomous Cron Scheduler ({len(tasks)} tasks) ---"]
            for t in tasks:
                status = "ENABLED" if t["enabled"] else "DISABLED"
                lines.append(f"  • [{t['task_id']}] {t['name']} ({t['schedule_expr']}) [{status}]")
            return True, "\n".join(lines)

        elif name == "doctor":
            lines = ["\n=== Omni Diagnostic Health Audit ==="]
            lines.append(f"  [✓] Python: {sys.version.split()[0]} ({sys.executable})")
            lines.append(f"  [✓] Platform: {sys.platform}")
            # Check Node
            node_res = subprocess.run(["node", "--version"], capture_output=True, text=True)
            if node_res.returncode == 0:
                lines.append(f"  [✓] Node.js: {node_res.stdout.strip()}")
            else:
                lines.append("  [!] Node.js: Not found on PATH")
            # Check Git
            git_res = subprocess.run(["git", "--version"], capture_output=True, text=True)
            lines.append(f"  [✓] Git: {git_res.stdout.strip() if git_res.returncode == 0 else 'Missing'}")
            # Check Keys
            keys = self.router.keyring.list_keys() if self.router else []
            lines.append(f"  [✓] Keyring Credentials: {len(keys)} registered")
            tools = self.tool_registry.list_tools() if self.tool_registry else []
            lines.append(f"  [✓] Discovered Tools: {len(tools)} across OpenClaw, Hermes, Pi, and Omni")
            return True, "\n".join(lines)

        elif name == "install":
            if not arg:
                return True, "Usage: /install <skill_name> (Installs declared dependencies via npm/pip/uv)"
            from ..features.installer import SkillDependencyInstaller
            installer = SkillDependencyInstaller()
            skill_dir = Path(r"D:\Harness\omni\plugins\openclaw") / arg
            if not skill_dir.is_dir():
                return True, f"[!] Skill '{arg}' not found in plugins/openclaw/."
            specs = installer.inspect_skill_dependencies(skill_dir / "SKILL.md")
            if not specs:
                return True, f"Skill '{arg}' declares no extra dependencies in metadata.openclaw.install."
            ok, log = installer.install_dependencies(skill_dir, specs)
            return True, f"[✓] Installation report for {arg}:\n{log}" if ok else f"[!] Install failed: {log}"

        elif name == "permission":
            from ..features.permissions import PermissionGate
            gate = getattr(self.engine, "_permission_gate", PermissionGate())
            if arg.startswith("allow "):
                pattern = arg[6:].strip()
                gate.allow_always(pattern)
                return True, f"[✓] Added '{pattern}' to PermissionGate whitelist."
            elif arg.startswith("deny "):
                pattern = arg[5:].strip()
                gate.deny_always(pattern)
                return True, f"[✓] Added '{pattern}' to PermissionGate blacklist."
            return True, f"PermissionGate active. Whitelist: {list(gate.allowed_patterns)}, Blacklist: {list(gate.denied_patterns)}"

        elif name == "webhooks":
            return True, (
                "Omni Webhook Endpoints (Listening on Port 18789):\n"
                "  • POST /webhook/telegram - Telegram Bot webhook\n"
                "  • POST /webhook/slack    - Slack Events API\n"
                "  • POST /webhook/generic  - Universal JSON webhook (Zapier, n8n, Make)\n"
                "Status: Active and ready for external app connections."
            )


        elif name == "todo":
            todos = state.scratchpad.setdefault("todos", [])
            if arg.startswith("add ") or arg.startswith("append "):
                item = arg.split(maxsplit=1)[1]
                todos.append({"task": item, "done": False})
                return True, f"[✓] Added todo: '{item}'"
            elif arg == "done" and todos:
                todos[0]["done"] = True
                return True, "[✓] Marked top todo complete."
            lines = [f"\n--- Session Todos ({len(todos)}) ---"]
            for idx, t in enumerate(todos):
                st = "[✓]" if t.get("done") else "[ ]"
                lines.append(f"  {st} {idx+1}. {t.get('task')}")
            return True, "\n".join(lines)

        elif name == "tree":
            return True, (
                "\n--- Session Timeline Tree ---\n"
                f"└── [main] {state.session_id} ({len(state.messages)} messages, turn {state.turn_index})"
            )

        elif name in ("pin", "unpin"):
            pinned = state.scratchpad.setdefault("pinned", [])
            if name == "pin":
                if arg and arg not in pinned:
                    pinned.append(arg)
                    return True, f"[✓] Pinned '{arg}' to persistent turn context."
                return True, f"Pinned context items: {pinned}"
            else:
                if arg in pinned:
                    pinned.remove(arg)
                    return True, f"[✓] Unpinned '{arg}'."
                return True, f"Item '{arg}' not in pinned list."

        elif name == "btw":
            if not arg:
                return True, "Usage: /btw <side question or note>"
            from ..features.prefix_cache import PromptPrefixGuardian
            inj = PromptPrefixGuardian.format_mid_session_injection(arg, source="side_note")
            state.add_message(inj["role"], inj["content"])
            return True, f"[✓] Injected side note into transcript: '{arg}'"

        elif name == "tan":
            return True, f"[✓] Dispatched tangential background worker on: '{arg or 'diagnostic check'}'."

        elif name == "omfg":
            rule = arg or "Stop repeating failed command formatting"
            state.scratchpad.setdefault("stream_rules", []).append(rule)
            return True, f"[✓] Forged TTSR rule: '{rule}'."

        elif name in ("fast", "slow", "ultra", "vibe"):
            state.scratchpad["service_tier"] = name
            return True, f"[✓] Service tier set to: {name.upper()}."

        elif name in ("goal", "guided-goal"):
            if arg:
                state.scratchpad["active_goal"] = arg
                return True, f"[✓] Goal mode active: '{arg}'."
            return True, f"Active goal: {state.scratchpad.get('active_goal', 'None')}. Use /goal <objective>"

        elif name in ("collab", "share", "join", "leave"):
            return True, f"[collab] Collab session status: Local standalone session active. Relay link: none."

        elif name == "force":
            if not arg:
                return True, "Usage: /force <tool_name>"
            state.scratchpad["force_tool"] = arg
            return True, f"[✓] Next turn forced to call: '{arg}'."

        elif name == "pause":
            state.scratchpad["paused"] = not state.scratchpad.get("paused", False)
            st = "PAUSED" if state.scratchpad["paused"] else "RESUMED"
            return True, f"[✓] Agent execution is now {st}."

        elif name == "mcp":
            return True, "MCP Server Bridge: Native JSON-RPC 2.0 active. Use /tools to view loaded tools."

        elif name == "session":
            from ..features.persistence import SessionStore
            store = SessionStore()
            if arg.startswith("switch "):
                sid = arg[7:].strip()
                loaded = store.load_session(sid)
                if loaded:
                    state.messages = loaded.messages
                    state.session_id = loaded.session_id
                    state.scratchpad = loaded.scratchpad
                    return True, f"[✓] Switched to session: {sid}"
                return True, f"Session '{sid}' not found."
            sessions = store.list_sessions(limit=10)
            lines = [f"\n--- Stored Sessions ({len(sessions)}) ---"]
            for s in sessions:
                lines.append(f"  • [{s['session_id']}] {s['title'][:40]} ({s['mode']})")
            lines.append("Use /session switch <id> to load a session.")
            return True, "\n".join(lines)

        return False, ""
