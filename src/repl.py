from .kernel.commands import CommandRegistry, COMMAND_PROFILES
from .kernel.commands_executor import CommandExecutor
"""
Omni Interactive REPL
Built with prompt_toolkit and rich: arrow key navigation, auto-suggestions,
command tab-completion, dynamic bottom toolbar, and styled markdown rendering.
"""

import asyncio
import os
import sys
from typing import Any, List, Optional

try:
    from prompt_toolkit import PromptSession
    from prompt_toolkit.auto_suggest import AutoSuggestFromHistory
    from prompt_toolkit.completion import Completer, Completion
    from prompt_toolkit.formatted_text import HTML
    from prompt_toolkit.history import FileHistory
    from prompt_toolkit.styles import Style
    HAVE_PROMPT_TOOLKIT = True
except ImportError:
    HAVE_PROMPT_TOOLKIT = False

try:
    from rich.console import Console
    from rich.markdown import Markdown
    from rich.panel import Panel
    from rich.table import Table
    console = Console()
    HAVE_RICH = True
except ImportError:
    HAVE_RICH = False
    console = None

from .kernel.features import CANONICAL_FEATURES, PROFILE_PRESETS, FeatureRegistry
from .kernel.state import SessionState
from .modes.manager import PROFILES
from .providers.router import MODEL_ALIASES


class OmniCompleter(Completer if HAVE_PROMPT_TOOLKIT else object):
    """Context-aware auto-completer for Omni slash commands, models, modes, and features."""

    def __init__(self, feature_registry: FeatureRegistry):
        self.feature_registry = feature_registry
        self.commands = [
            "/model",
            "/mode",
            "/toggle",
            "/tools",
            "/plugins",
            "/keys",
            "/stats",
            "/evolve",
            "/resume",
            "/clear",
            "/help",
            "/exit",
            "/quit",
        ]
        self.modes = list(PROFILES.keys())
        self.models = list(MODEL_ALIASES.keys())
        self.features = list(CANONICAL_FEATURES.keys())

    def get_completions(self, document, complete_event):
        text = document.text_before_cursor
        stripped = text.strip()

        # Suggest top-level slash commands
        if text.startswith("/") and " " not in text:
            for cmd in self.commands:
                if cmd.startswith(text.lower()):
                    yield Completion(cmd, start_position=-len(text), display_meta="Command")
            return

        parts = stripped.split(maxsplit=2)
        if len(parts) >= 1:
            cmd = parts[0].lower()

            if cmd == "/model":
                prefix = parts[1] if len(parts) > 1 and not text.endswith(" ") else ""
                for m in self.models:
                    if m.startswith(prefix.lower()):
                        yield Completion(m, start_position=-len(prefix), display_meta="Model Alias")

            elif cmd == "/mode":
                prefix = parts[1] if len(parts) > 1 and not text.endswith(" ") else ""
                for mode_name in self.modes:
                    if mode_name.startswith(prefix.lower()):
                        yield Completion(mode_name, start_position=-len(prefix), display_meta="Profile Mode")

            elif cmd == "/profile":
                prefix = parts[1] if len(parts) > 1 and not text.endswith(" ") else ""
                for p_name in self.profiles:
                    if p_name.startswith(prefix.lower()):
                        yield Completion(p_name, start_position=-len(prefix), display_meta="Command Profile")
            elif cmd == "/toggle":" 
                prefix = parts[1] if len(parts) > 1 and not text.endswith(" ") else ""
                for feat in self.features:
                    if feat.startswith(prefix.lower()):
                        is_on = "ON" if self.feature_registry.is_enabled(feat) else "OFF"
                        yield Completion(feat, start_position=-len(prefix), display_meta=f"Feature ({is_on})")


def print_rich_markdown(content: str):
    """Render markdown response beautifully with rich if available."""
    if HAVE_RICH and console:
        console.print(Markdown(content))
    else:
        print(content)


def print_feature_table(feature_registry: FeatureRegistry):
    """Print feature toggles in a rich tabular format."""
    features = feature_registry.list_features()
    if HAVE_RICH and console:
        table = Table(title="Omni Feature Toggles & Capabilities", header_style="bold magenta")
        table.add_column("Category", style="cyan", width=12)
        table.add_column("Feature ID", style="bold white", width=24)
        table.add_column("Status", width=8)
        table.add_column("Description", style="dim")

        for f in features:
            status_str = "[bold green]ON[/bold green]" if f["enabled"] else "[bold red]OFF[/bold red]"
            table.add_row(f["category"].upper(), f["id"], status_str, f["description"])
        console.print(table)
    else:
        print("\n=== Omni Feature Toggles ===")
        for f in features:
            status = "ON" if f["enabled"] else "OFF"
            print(f"  [{f['category'].upper():<8}] {f['id']:<24} [{status}] - {f['description']}")


async def run_rich_repl(
    engine: Any,
    config: Any,
    router: Any,
    tool_registry: Any,
    tracer: Any,
    initial_model: Optional[str] = None,
    initial_mode: Optional[str] = None,
):
    """Runs the rich interactive REPL with prompt_toolkit and rich."""
    feature_registry = FeatureRegistry(config_data=getattr(config, "config_data", {}), config_path=getattr(config, "config_path", None))
    active_mode = initial_mode or getattr(config, "active_mode", "autonomous")
    raw_model = initial_model or getattr(config, "default_model", None) or "google/gemini-2.5-flash"
    active_model = router.resolve_model(raw_model)

    cmd_registry = CommandRegistry(config=config, active_profile=getattr(config, "command_profile", "full"))
    cmd_executor = CommandExecutor(
        command_registry=cmd_registry,
        engine=engine,
        config=config,
        router=router,
        tool_registry=tool_registry,
        tracer=tracer,
    )
    history_file = os.path.expanduser("~/.omni_history")

    # Prompt session initialization
    session = None
    if HAVE_PROMPT_TOOLKIT:
        try:
            completer = OmniCompleter(feature_registry, cmd_registry)
            session = PromptSession(
                history=FileHistory(history_file),
                auto_suggest=AutoSuggestFromHistory(),
                completer=completer,
                style=Style.from_dict({
                    "prompt": "#00d7ff bold",
                    "bottom-toolbar": "#ffffff bg:#333333",
                }),
            )
        except Exception:
            session = None

    def get_bottom_toolbar():
        tool_count = len(tool_registry.list_tools()) if tool_registry else 0
        keys_count = len(router.keyring.get_active_unblocked_keys("google-antigravity")) + len(router.keyring.get_active_unblocked_keys("anthropic"))
        model_short = active_model.split("/")[-1]
        return HTML(
            f" <b>Model:</b> <style color='#00ffff'>{model_short}</style> │ "
            f"<b>Mode:</b> <style color='#55ff55'>{active_mode.upper()}</style> │ "
            f"<b>Tools:</b> {tool_count} │ "
            f"<b>Keys:</b> {keys_count} active │ "
            f"<i>Type /help for commands</i> "
        )

    print("\n[*] Interactive REPL active. Use Up/Down arrows for history, Tab for suggestions.")
    print("[*] Available slash commands: /model <name>, /mode <mode>, /toggle <feat>, /tools, /clear, exit\n")

    session_state = SessionState()

    while True:
        try:
            model_short = active_model.split("/")[-1]
            prompt_str = f"[{model_short}] > "

            if session:
                line = await session.prompt_async(prompt_str, bottom_toolbar=get_bottom_toolbar)
            else:
                line = input(prompt_str)

            line = line.strip()
            if not line:
                continue

            if line.lower() in ["exit", "quit", ":q"]:
                print("Goodbye!")
                break

            # Handle Slash Commands
            if line.startswith("/"):
                def set_model_cb(new_m):
                    nonlocal active_model
                    active_model = router.resolve_model(new_m)

                handled, cmd_msg = await cmd_executor.execute(
                    line, session_state, context={"switch_model": set_model_cb}
                )
                if handled:
                    if cmd_msg:
                        print(cmd_msg)
                    continue
                parts = line.split(maxsplit=2)
                cmd = parts[0].lower()
                arg1 = parts[1].strip() if len(parts) > 1 else ""
                arg2 = parts[2].strip() if len(parts) > 2 else ""

                if cmd == "/model":
                    if arg1:
                        active_model = router.resolve_model(arg1)
                        print(f"[✓] Switched active model to: {active_model}")
                    else:
                        print(f"\nActive Model: {active_model}")
                        print("Aliases: flash, flash-3.8, sonnet, opus, pro, gpt-4o, mini, deepseek, haiku, argon")
                        print("Usage: /model <name_or_alias>")
                    continue

                elif cmd == "/mode":
                    if arg1:
                        if arg1.lower() in PROFILES:
                            active_mode = arg1.lower()
                            feature_registry.apply_profile(active_mode)
                            config.set_mode(active_mode)
                            print(f"[✓] Switched profile mode to: {active_mode.upper()}")
                        else:
                            print(f"Unknown mode. Available: {list(PROFILES.keys())}")
                    else:
                        print(f"Active mode: {active_mode.upper()}")
                        print(f"Available modes: {list(PROFILES.keys())}")
                    continue

                elif cmd == "/toggle":
                    if not arg1:
                        print_feature_table(feature_registry)
                    else:
                        if arg2:
                            val = arg2.lower() in ("on", "true", "1", "yes")
                            ok = feature_registry.set_feature(arg1, val)
                        else:
                            val = feature_registry.toggle(arg1)
                            ok = val is not None
                        if ok:
                            status_str = "ENABLED (ON)" if val else "DISABLED (OFF)"
                            print(f"[✓] Feature '{arg1}' is now {status_str}")
                        else:
                            print(f"Unknown feature ID: '{arg1}'. Type /toggle without arguments to list features.")
                    continue

                elif cmd in ("/tools", "/plugins"):
                    tools = tool_registry.list_tools() if tool_registry else []
                    print(f"\n--- Loaded Tools ({len(tools)}) ---")
                    for t in tools:
                        print(f"  • [{t.source.upper()}] {t.name}: {t.description[:80]}")
                    continue

                elif cmd == "/keys":
                    keys = router.keyring.list_keys()
                    print("\n--- Keyring Status ---")
                    for k in keys:
                        status = "ACTIVE" if k.get("is_active") else "INACTIVE"
                        blocked = k.get("blocked_until")
                        b_str = f" [BLOCKED until {blocked}]" if blocked else ""
                        print(f"  • [{k['provider']}] id={k['key_id']} priority={k['priority']} status={status}{b_str}")
                    continue

                elif cmd == "/clear":
                    session_state = SessionState()
                    print("[✓] Conversation history reset.")
                    continue

                elif cmd == "/stats":
                    stats = tracer.get_session_stats() if tracer else {}
                    print("\n--- Telemetry Stats ---")
                    for k, v in stats.items():
                        print(f"  {k}: {v}")
                    continue

                elif cmd == "/help":
                    print("\n=== Omni Slash Commands ===")
                    print("  /model <name>      - Switch active model (flash, sonnet, opus, gpt-4o, etc.)")
                    print("  /mode <profile>    - Switch profile mode (lean, balanced, autonomous, evolve)")
                    print("  /toggle [feature]  - List or toggle individual features on/off")
                    print("  /tools             - List all active drop-in tools and skills")
                    print("  /keys              - View API key pool and cooldown status")
                    print("  /stats             - View session token counts and costs")
                    print("  /clear             - Clear conversation history")
                    print("  exit / quit        - Exit REPL\n")
                    continue

            # Run conversational turn
            response = await engine.run_turn(
                session_state,
                line,
                max_tool_iterations=getattr(PROFILES.get(active_mode), "max_tool_iterations", 10),
                model=active_model,
            )

            print()
            print_rich_markdown(response)

        except (KeyboardInterrupt, asyncio.CancelledError):
            print("\n[Canceled turn. Type exit to quit.]")
            continue
        except EOFError:
            print("\nExiting Omni REPL.")
            break
        except Exception as e:
            print(f"\n[!] Error during turn: {e}")
