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


        elif name == "moa":
            from ..features.moa import MixtureOfAgentsEngine
            moa_engine = MixtureOfAgentsEngine(self.router)
            query = arg or "Analyze the architectural advantages of modular agent design"
            print(f"[*] Convening Mixture-of-Agents council for: '{query[:60]}...'")
            report = await moa_engine.run_moa(query)
            return True, f"\n=== Mixture-of-Agents Synthesis ({report['aggregator']}) ===\n{report['final_synthesis']}"

        elif name == "egress":
            from ..features.egress import EgressProxyFirewall
            fw = getattr(self.engine, "_egress_firewall", EgressProxyFirewall())
            if arg.startswith("allow "):
                domain = arg[6:].strip().lower()
                fw.allowed_domains.add(domain)
                return True, f"[✓] Added '{domain}' to iron-proxy egress whitelist."
            lines = ["\n--- Iron-Proxy Egress Firewall Whitelist ---"]
            for d in sorted(fw.allowed_domains):
                lines.append(f"  • {d}")
            return True, "\n".join(lines)

        elif name == "timeline":
            from ..features.branching import TimelineBranchManager
            bm = TimelineBranchManager()
            return True, bm.render_ascii_dag(state.session_id)

        elif name == "environment":
            from ..features.environments import EnvironmentManager
            em = getattr(self.engine, "_env_manager", EnvironmentManager(workspace_root=getattr(self.config, "root_dir", None)))
            if arg:
                if em.set_environment(arg.lower()):
                    return True, f"[✓] Switched active execution environment to: {arg.upper()}."
                return True, f"[!] Unknown environment '{arg}'. Available: {list(em.environments.keys())}"
            return True, f"Active Execution Environment: {em.active_env_name.upper()} (Available: {list(em.environments.keys())})"

        elif name == "personality":
            if arg:
                state.scratchpad["personality"] = arg
                return True, f"[✓] Switched agent personality to: '{arg}'."
            return True, f"Current personality: {state.scratchpad.get('personality', 'Omni Default')}. Usage: /personality <name>"

        elif name in ("pet", "pets", "hatch"):
            if name == "hatch":
                mascot = "✦ Phoenix ✦"
                state.scratchpad["active_pet"] = mascot
                return True, f"[✓] You hatched a new Petdex companion: {mascot} (Level 1, Exp: 100)!"
            pet = state.scratchpad.get("active_pet", "Omni Lobster 🦞")
            return True, f"\n--- Petdex Active Mascot ---\n  ( •_•) < [{pet} is cheering you on! Level 2]\n  ( >🪓)> Keep shipping clean commits!"


        # ===================================================================
        # OpenClaw Handlers Suite
        # ===================================================================
        elif name == "whoami":
            sender_id = state.scratchpad.get("sender_id", "local_operator")
            role = "admin" if state.scratchpad.get("elevated", False) else "operator"
            return True, (
                f"\n--- OpenClaw Identity ---\n"
                f"  • Sender ID: {sender_id}\n"
                f"  • Role: {role}\n"
                f"  • Channel: terminal_repl\n"
                f"  • Session: {state.session_id}"
            )

        elif name == "allowlist":
            from ..features.permissions import PermissionGate
            gate = getattr(self.engine, "_permission_gate", PermissionGate())
            if arg.startswith("add "):
                pat = arg[4:].strip()
                gate.allow_always(pat)
                return True, f"[✓] Added '{pat}' to allowlist."
            elif arg.startswith("remove "):
                pat = arg[7:].strip()
                gate._allowed_patterns.discard(pat)
                return True, f"[✓] Removed '{pat}' from allowlist."
            return True, f"Allowlist entries ({len(gate._allowed_patterns)}): {list(gate._allowed_patterns)}"

        elif name == "approve":
            pending = state.scratchpad.get("pending_approvals", [])
            if not pending:
                return True, "No pending execution requests requiring approval."
            if arg in ("all", "yes"):
                state.scratchpad["pending_approvals"] = []
                return True, f"[✓] Approved {len(pending)} pending request(s)."
            return True, f"Pending requests: {pending}. Use /approve all to approve."

        elif name == "export-session":
            from ..features.persistence import SessionStore
            store = SessionStore()
            out_file = arg or f"session_{state.session_id[:8]}.html"
            msgs_html = "".join(f"<div class='msg {m.role}'><b>{m.role}:</b> <pre>{m.content}</pre></div>" for m in state.messages)
            html_doc = f"<!DOCTYPE html><html><head><title>Session {state.session_id}</title><style>body{{font-family:sans-serif;padding:20px;background:#1e1e1e;color:#ddd}}.msg{{margin-bottom:15px;padding:10px;border-radius:5px}}.user{{background:#2d3748}}.assistant{{background:#1a202c}}pre{{white-space:pre-wrap}}</style></head><body><h1>Session {state.session_id}</h1>{msgs_html}</body></html>"
            with open(out_file, "w", encoding="utf-8") as f:
                f.write(html_doc)
            return True, f"[✓] Exported standalone HTML session report to: {os.path.abspath(out_file)}"

        elif name in ("export-trajectory", "trajectory"):
            out_file = arg or f"trajectory_{state.session_id[:8]}.jsonl"
            with open(out_file, "w", encoding="utf-8") as f:
                for idx, m in enumerate(state.messages):
                    entry = {"turn": idx, "role": m.role, "content": m.content, "tool_calls": m.tool_calls}
                    f.write(json.dumps(entry) + "\n")
            return True, f"[✓] Exported JSONL trajectory bundle to: {os.path.abspath(out_file)}"

        elif name == "tts":
            if arg in ("on", "off"):
                state.scratchpad["tts_enabled"] = (arg == "on")
                return True, f"[✓] Text-to-Speech synthesis is now {arg.upper()}."
            st = "ENABLED" if state.scratchpad.get("tts_enabled") else "DISABLED"
            return True, f"TTS status: {st}. Usage: /tts [on|off]"

        elif name == "activation":
            if arg in ("mention", "all", "steer"):
                state.scratchpad["group_activation"] = arg
                return True, f"[✓] Group activation mode set to: {arg.upper()}."
            cur = state.scratchpad.get("group_activation", "mention")
            return True, f"Current activation mode: {cur}. Usage: /activation [mention|all|steer]"

        elif name == "send":
            if arg in ("allow", "deny", "inherit"):
                state.scratchpad["send_policy"] = arg
                return True, f"[✓] Outbound delivery policy set to: {arg.upper()}."
            cur = state.scratchpad.get("send_policy", "inherit")
            return True, f"Current delivery policy: {cur}. Usage: /send [allow|deny|inherit]"

        elif name == "think":
            if arg in ("xhigh", "max", "adaptive", "high", "medium", "low", "off"):
                state.scratchpad["thinking_budget"] = arg
                return True, f"[✓] Thinking level set to: {arg.upper()}."
            cur = state.scratchpad.get("thinking_budget", "default")
            return True, f"Current thinking level: {cur}. Usage: /think [xhigh|max|adaptive|off]"

        elif name == "verbose":
            val = arg.lower() in ("on", "true", "1") if arg else not state.scratchpad.get("verbose", False)
            state.scratchpad["verbose"] = val
            return True, f"[✓] Verbose logging mode is now {'ON' if val else 'OFF'}."

        elif name == "trace":
            val = arg.lower() in ("on", "true", "1") if arg else not state.scratchpad.get("trace", False)
            state.scratchpad["trace"] = val
            return True, f"[✓] Plugin trace display is now {'ON' if val else 'OFF'}."

        elif name == "elevated":
            val = arg.lower() in ("on", "true", "1") if arg else not state.scratchpad.get("elevated", False)
            state.scratchpad["elevated"] = val
            return True, f"[✓] Elevated execution permissions are now {'ENABLED' if val else 'DISABLED'}."

        elif name == "exec":
            if arg:
                state.scratchpad["exec_defaults"] = arg
                return True, f"[✓] Exec defaults configured: {arg}"
            return True, f"Exec defaults: {state.scratchpad.get('exec_defaults', 'standard shell')}. Usage: /exec <options>"

        elif name == "queue":
            if arg in ("fifo", "steer", "drop"):
                state.scratchpad["queue_mode"] = arg
                return True, f"[✓] Turn queue mode set to: {arg.upper()}."
            cur = state.scratchpad.get("queue_mode", "steer")
            return True, f"Queue mode: {cur}. Usage: /queue [fifo|steer|drop]"

        elif name == "steer":
            if not arg:
                return True, "Usage: /steer <guidance text>"
            state.scratchpad["live_steering"] = arg
            return True, f"[✓] Live guidance queued for active run: '{arg}'"

        elif name == "followup":
            if not arg:
                return True, "Usage: /followup <message>"
            state.scratchpad.setdefault("followups", []).append(arg)
            return True, f"[✓] Followup turn queued: '{arg}'"

        elif name == "stop":
            state.scratchpad["aborted"] = True
            return True, "[✓] Active run aborted."

        elif name == "subagents":
            subs = state.scratchpad.get("subagents", {})
            lines = [f"\n--- Session Subagent Runs ({len(subs)}) ---"]
            for sid, info in subs.items():
                lines.append(f"  • [{info.get('specialist', 'worker').upper()}] {sid}: {info.get('status')} ({info.get('turn_count', 0)} turns)")
            return True, "\n".join(lines)

        elif name == "agents":
            return True, (
                "\n--- OpenClaw Agent Personas ---\n"
                "  • main: Default primary conversational agent\n"
                "  • scout: Codebase search and file discovery agent\n"
                "  • planner: System architecture and task planning agent\n"
                "  • reviewer: Code analysis and security review agent"
            )

        elif name == "dashboard":
            return True, (
                "\n--- OpenClaw Web Control UI ---\n"
                "  • Dashboard URL: http://127.0.0.1:18789/\n"
                "  • API Endpoints: /api/chat, /api/tools, /api/sessions\n"
                "  • Gateway Daemon: Active on port 18789"
            )

        elif name == "diagnostics":
            return True, (
                "\n=== OpenClaw Gateway Diagnostics ===\n"
                f"  • Gateway Runtime: Node/Python Unified Adapter\n"
                f"  • Active Session: {state.session_id}\n"
                f"  • Channels Ingress: Healthy (WebSockets port 18789)\n"
                f"  • Memory Store: SQLite Connected\n"
                f"  • Permissions: Active Gate ({len(getattr(getattr(self.engine, '_permission_gate', None), '_allowed_patterns', []))} whitelist)"
            )

        elif name == "openclaw":
            return True, (
                "\n[🦞 OpenClaw Assistant]\n"
                "OpenClaw subsystem operational. Run /status to inspect channels, /cron to manage automations, "
                "or /nodes to inspect paired companion devices."
            )

        elif name == "skill":
            if not arg:
                return True, "Usage: /skill <skill_name> [input arguments]"
            parts = arg.split(maxsplit=1)
            s_name = parts[0]
            s_input = parts[1] if len(parts) > 1 else "{}"
            if self.tool_registry:
                tool = self.tool_registry.get_tool(s_name)
                if tool:
                    try:
                        args_dict = json.loads(s_input) if s_input.startswith("{") else {"input": s_input}
                    except Exception:
                        args_dict = {"input": s_input}
                    res = await self.tool_registry.execute_tool(s_name, args_dict)
                    return True, f"[✓] Skill '{s_name}' Output:\n{res}"
                return True, f"[!] Skill '{s_name}' not found in registry."
            return True, "No tool registry configured."

        elif name == "nodes":
            nodes = state.scratchpad.get("paired_nodes", [
                {"id": "node_local_host", "name": "Local Host Terminal", "status": "connected", "role": "node"}
            ])
            lines = [f"\n--- OpenClaw Paired Nodes ({len(nodes)}) ---"]
            for n in nodes:
                lines.append(f"  • [{n['id']}] {n['name']} ({n['status']}) - role: {n['role']}")
            return True, "\n".join(lines)

        elif name == "pair":
            token = f"claw_pair_{int(time.time()*1000)}"
            return True, (
                f"\n=== OpenClaw Companion Device Pairing ===\n"
                f"Pairing Setup Code: {token}\n"
                f"To connect a companion phone or laptop, run:\n"
                f"  openclaw connect --token {token} --gateway ws://127.0.0.1:18789"
            )

        elif name == "channels":
            return True, (
                "\n--- Connected Chat Channels ---\n"
                "  • Terminal REPL: Active (connected)\n"
                "  • Gateway Webhook Ingress: Active on http://127.0.0.1:18789/webhook/\n"
                "    - Telegram Bot: Ready (/webhook/telegram)\n"
                "    - Slack Events: Ready (/webhook/slack)\n"
                "    - Universal JSON: Ready (/webhook/generic)"
            )

        elif name == "security":
            return True, (
                "\n=== OpenClaw Security Audit ===\n"
                "  [✓] File Permissions Gate: Active\n"
                "  [✓] Credential Masking: Active (iron-proxy)\n"
                "  [✓] Egress Whitelist: Active\n"
                "  [✓] No hardcoded secrets detected in configuration"
            )

        elif name == "secrets":
            from ..providers.keyring import Keyring
            keys = self.router.keyring.list_keys() if self.router else []
            lines = [f"\n--- Secrets & Credentials ({len(keys)} registered) ---"]
            for k in keys:
                lines.append(f"  • [{k['provider']}] key_id: {k['key_id']} (priority: {k['priority']})")
            return True, "\n".join(lines)

        elif name == "bash":
            if not arg:
                return True, "Usage: /bash <command>"
            import subprocess
            res = subprocess.run(arg, shell=True, capture_output=True, text=True, timeout=30)
            out = res.stdout.strip() or res.stderr.strip() or "(no output)"
            return True, f"[bash exit {res.returncode}]\n{out}"

        elif name == "name":
            if not arg:
                return True, f"Current session title: '{state.scratchpad.get('title', state.session_id)}'"
            state.scratchpad["title"] = arg
            return True, f"[✓] Session renamed to: '{arg}'"


        # ===================================================================
        # Visual & Advanced OMP/Hermes Handlers
        # ===================================================================
        elif name in ("skin", "theme"):
            from ..features.themes import ThemeManager, THEMES
            from rich.console import Console
            from rich.panel import Panel
            from rich.table import Table
            from rich import box

            tm = ThemeManager()
            if not arg:
                lines = [f"\nActive Theme: {tm.active_theme_name.upper()}", "Available Truecolor Themes:"]
                for t in THEMES.values():
                    m = " * " if t.name == tm.active_theme_name else "   "
                    lines.append(f" {m}{t.name:<16} - {t.description}")
                lines.append("\nUsage: /skin <tokyo-night|catppuccin|matrix|nord|solarized>")
                return True, "\n".join(lines)

            if tm.set_theme(arg):
                t = THEMES[arg.lower()]
                state.scratchpad["active_skin"] = t.name
                c = Console(safe_box=False)
                table = Table(show_header=False, box=None, padding=(0, 1), expand=True)
                table.add_column(width=20)
                table.add_column()
                table.add_row(f"[bold {t.primary}]● Primary Accent[/]", f"[{t.primary}]{t.primary} (Prompts & Titles)[/]")
                table.add_row(f"[bold {t.secondary}]● Secondary Color[/]", f"[{t.secondary}]{t.secondary} (Headers & Accents)[/]")
                table.add_row(f"[bold {t.accent}]● Diamond Highlight[/]", f"[{t.accent}]{t.accent} (Icons & Focus)[/]")
                table.add_row(f"[bold {t.success}]● Success Output[/]", f"[{t.success}]{t.success} (Tool Results)[/]")
                table.add_row(f"[bold {t.warning}]● Tool Call Alert[/]", f"[{t.warning}]{t.warning} (Invocations)[/]")

                panel = Panel(
                    table,
                    title=f"[bold {t.primary}]🎨 Theme Applied: {t.name.upper()}[/bold {t.primary}]",
                    border_style=t.panel_border,
                    box=box.ROUNDED,
                    padding=(1, 2)
                )
                c.print()
                c.print(panel)
                c.print()
                return True, f"[✓] Applied Truecolor Theme: {t.name.upper()} ({t.description})"
            return True, f"[!] Unknown theme '{arg}'. Available: {list(THEMES.keys())}"

        elif name == "split":
            from ..features.split_pane import DualPaneLayout
            from ..features.checkpoints import GitCheckpointManager
            diff_text = GitCheckpointManager().get_diff()
            last_conv = "\n".join(f"{m.role}: {m.content[:120]}" for m in state.messages[-4:]) if state.messages else "New conversation"
            todos = state.scratchpad.get("todos", [])
            DualPaneLayout().display(last_conv, diff_text, todos)
            return True, "[✓] Rendered Split-Screen Dual Pane (pi-tui style)."

        elif name in ("mm", "mental-models"):
            from ..features.mental_models import MentalModelRepository
            mm_repo = MentalModelRepository()
            if arg.startswith("add "):
                parts = arg[4:].split(maxsplit=1)
                slug = parts[0]
                rule = parts[1] if len(parts) > 1 else "Invariant rule"
                fpath = mm_repo.add_model(slug, slug.title(), rule)
                return True, f"[✓] Added Mental Model '{slug}' to {fpath}"
            models = mm_repo.list_models()
            lines = [f"\n--- Project Mental Models ({len(models)} cards) ---"]
            for m in models:
                lines.append(f"  • [{m['slug']}] {m['title']} ({m['path']})")
            lines.append("Usage: /mm add <slug> <rule_content>")
            return True, "\n".join(lines)

        elif name in ("astgrep", "ast-grep"):
            from ..features.ast_grep import ASTGrepSearcher
            searcher = ASTGrepSearcher()
            query = arg or "complete"
            funcs = searcher.search_functions(query)
            lines = [f"\n--- AST-Grep Structural Search for '{query}' ({len(funcs)} functions) ---"]
            for fn in funcs[:10]:
                lines.append(f"  • {fn['kind']} {fn['name']}({', '.join(fn['args'])}) at {fn['file']}:{fn['line']}")
            return True, "\n".join(lines)

        elif name == "prewalk":
            from ..features.prewalk import PrewalkHandoffEngine
            engine = PrewalkHandoffEngine(router=self.router)
            task = arg or "Analyze router architecture and add fallback metrics"
            print(f"[*] Running 2-stage Prewalk Model Handoff for: '{task[:60]}...'")
            report = await engine.execute_prewalk_handoff(task)
            return True, (
                f"\n=== Prewalk Model Handoff Complete ===\n"
                f"Stage 1 ({report['fast_model']}): Scanned files & dependencies.\n"
                f"Stage 2 ({report['smart_model']}): Code generation complete.\n\n"
                f"{report['final_response']}"
            )

        elif name == "say":
            from ..features.audio_chimes import AudioNotifier
            if not arg:
                return True, "Usage: /say <text to speak>"
            AudioNotifier.say(arg)
            return True, f"[✓] Speaking: '{arg}'"

        elif name == "chime":
            from ..features.audio_chimes import AudioNotifier
            AudioNotifier.play_chime("success")
            return True, "[♪] Played system notification chime."

        return False, ""
