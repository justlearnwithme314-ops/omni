"""
Omni Interactive REPL & Visual Engine (OMP & Hermes Style)
Featuring:
- OMP-style 2-line HUD prompt with active theme colors
- Live command autocomplete dropdown menu (complete_while_typing=True)
- Inline ghost-text auto-suggestions from command history
- Live streaming spinner with elapsed metrics during generation
- Persistent command history (~/.omni_history)
- Dynamic bottom status toolbar
- Themed rounded Markdown response cards
"""

import asyncio
import os
import sys
from typing import Any, Dict, List, Optional

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
    from rich import box
    console = Console()
    HAVE_RICH = True
except ImportError:
    HAVE_RICH = False
    console = None

from .features.themes import THEMES, ThemeManager
from .features.spinners import LiveStreamingSpinner
from .features.checkpoints import GitCheckpointManager
from .kernel.commands import CommandRegistry, COMMAND_PROFILES
from .kernel.commands_executor import CommandExecutor
from .kernel.features import CANONICAL_FEATURES, FeatureRegistry
from .kernel.state import SessionState
from .modes.manager import PROFILES
from .providers.router import MODEL_ALIASES
from .paths import history_file as _history_file


class OmniCompleter(Completer if HAVE_PROMPT_TOOLKIT else object):
    """Context-aware auto-completer showing live dropdown suggestions as you type."""

    def __init__(self, feature_registry: FeatureRegistry, command_registry: Optional[CommandRegistry] = None, handled=None):
        self.feature_registry = feature_registry
        self.command_registry = command_registry
        self.handled = handled or set()
        self.modes = list(PROFILES.keys())
        self.models = list(MODEL_ALIASES.keys())
        self.features = list(CANONICAL_FEATURES.keys())
        self.profiles = ["pi", "omp", "hermes", "openclaw", "full"]
        self.skins = list(THEMES.keys())

    def get_completions(self, document, complete_event):
        text = document.text_before_cursor
        stripped = text.strip()

        # 1. Live Slash Command completions (pops up as soon as user types '/')
        if text.startswith("/") and " " not in text:
            if self.command_registry:
                active_cmds = self.command_registry.get_listed_commands(self.handled)
                for cmd in active_cmds:
                    slash_name = f"/{cmd.name}"
                    if slash_name.startswith(text.lower()):
                        yield Completion(
                            slash_name,
                            start_position=-len(text),
                            display_meta=f"[{cmd.category.upper()}] {cmd.description[:35]}",
                        )
            else:
                for c in ["/model", "/mode", "/skin", "/split", "/tools", "/keys", "/stats", "/undo", "/diff", "/memory", "/cron", "/doctor", "/clear", "/help", "/exit"]:
                    if c.startswith(text.lower()):
                        yield Completion(c, start_position=-len(text), display_meta="Command")
            return

        # 2. Argument completions after slash command
        parts = stripped.split(maxsplit=2)
        if len(parts) >= 1:
            cmd = parts[0].lower()
            prefix = parts[1] if len(parts) > 1 and not text.endswith(" ") else ""

            if cmd in ("/model", "/m"):
                for m in self.models:
                    if m.startswith(prefix.lower()):
                        yield Completion(m, start_position=-len(prefix), display_meta="Model Alias")

            elif cmd == "/mode":
                for mode_name in self.modes:
                    if mode_name.startswith(prefix.lower()):
                        yield Completion(mode_name, start_position=-len(prefix), display_meta="Profile Mode")

            elif cmd in ("/skin", "/theme"):
                for sk in self.skins:
                    if sk.startswith(prefix.lower()):
                        yield Completion(sk, start_position=-len(prefix), display_meta="Truecolor Skin")

            elif cmd == "/profile":
                for p_name in self.profiles:
                    if p_name.startswith(prefix.lower()):
                        yield Completion(p_name, start_position=-len(prefix), display_meta="Command Profile")

            elif cmd == "/toggle":
                for feat in self.features:
                    if feat.startswith(prefix.lower()):
                        is_on = "ON" if self.feature_registry.is_enabled(feat) else "OFF"
                        yield Completion(feat, start_position=-len(prefix), display_meta=f"Feature ({is_on})")


def print_rich_markdown(content: str, model_name: str = "Omni"):
    """Render markdown response beautifully with rich panel and active theme."""
    if HAVE_RICH and console:
        tm = ThemeManager()
        theme = tm.get_theme()
        safe_console = Console(safe_box=False)
        short_model = model_name.split("/")[-1] if "/" in model_name else model_name
        panel = Panel(
            Markdown(content),
            title=f"[bold {theme.primary}]● {short_model}[/bold {theme.primary}]",
            border_style=theme.panel_border,
            box=box.ROUNDED,
            padding=(1, 2),
        )
        safe_console.print()
        safe_console.print(panel)
        safe_console.print()
    else:
        print(f"\n[{model_name}] >\n{content}\n")


def print_feature_table(feature_registry: FeatureRegistry):
    """Print feature toggles in a rich tabular format."""
    features = feature_registry.list_features()
    if HAVE_RICH and console:
        table = Table(title="Omni Feature Toggles & Capabilities", header_style="bold magenta", box=box.ROUNDED)
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
    """Runs the rich interactive REPL with live auto-completion, OMP HUD, and themes."""
    tm = ThemeManager()
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
    history_file = str(_history_file())

    # Initialize PromptSession with live auto-completion dropdown
    session = None
    if HAVE_PROMPT_TOOLKIT:
        try:
            completer = OmniCompleter(feature_registry, cmd_registry, handled=cmd_executor.handled_names())
            session = PromptSession(
                history=FileHistory(history_file),
                auto_suggest=AutoSuggestFromHistory(),
                completer=completer,
                complete_while_typing=True,
            )
        except Exception:
            session = None

    def get_bottom_toolbar():
        from pathlib import Path
        clean_m = active_model.split("/")[-1]
        if "gemini-3.8" in clean_m:
            m_display = "gemini flash (antigravity) 3.8"
        elif "gemini-2.5" in clean_m:
            m_display = "gemini flash (antigravity) 2.5"
        elif "claude-sonnet" in clean_m:
            m_display = "claude sonnet (antigravity) 5.5"
        else:
            m_display = clean_m

        cur_tok = getattr(session_state, "token_count", 0)
        max_tok = "1M"
        pct = min(100, int((cur_tok / 1_000_000) * 100)) if cur_tok else 0

        try:
            cwd_parts = Path.cwd().parts
            cwd_disp = f"{cwd_parts[-2]}/{cwd_parts[-1]}" if len(cwd_parts) >= 2 else Path.cwd().name
        except Exception:
            cwd_disp = "Users/Admin"

        think = session_state.scratchpad.get("thinking_budget", "high")
        t = tm.get_theme()

        from prompt_toolkit.formatted_text import FormattedText
        return FormattedText([
            (f"bold {t.primary}", f" {m_display} "),
            ("#555555", "│ "),
            (f"{t.success}", f"{pct}% / {max_tok} "),
            ("#555555", "│ "),
            (f"{t.secondary}", f"{cwd_disp} "),
            ("", "                     "),
            (f"{t.warning}", f"thinking: {think} "),
        ])

    session_state = SessionState()

    while True:
        try:
            t = tm.get_theme()
            model_short = active_model.split("/")[-1]
            tool_count = len([t for t in tool_registry.list_tools() if t.enabled]) if tool_registry else 0

            # OMP-style 2-line HUD prompt with active theme colors (using safe FormattedText)
            from prompt_toolkit.formatted_text import FormattedText
            prompt_fragments = FormattedText([
                (f"bold {t.secondary}", "╭─ "),
                (f"bold {t.primary}", f"[{model_short}] "),
                ("#555555", "• "),
                (f"bold {t.success}", f"{active_mode.upper()} "),
                ("#555555", "• "),
                (f"{t.text}", f"{tool_count} tools "),
                ("#555555", "• "),
                (f"italic {t.accent}", f"{t.name}\n"),
                (f"bold {t.primary}", "╰─❯ "),
            ])

            dynamic_style = Style.from_dict({
                "completion-menu": f"bg:#1f2026 {t.text}",
                "completion-menu.completion": f"bg:#1f2026 {t.text}",
                "completion-menu.completion.current": f"bg:{t.primary} #000000 bold",
                "completion-menu.meta": f"bg:#2a2b36 {t.secondary}",
                "completion-menu.meta.current": f"bg:{t.primary} #000000",
                "auto-suggestion": "#555555 italic",
                "bottom-toolbar": f"{t.text} bg:#18181b",
            })

            fallback_prompt_str = f"╭─ [{model_short}] ({t.name})\n╰─❯ "

            if session:
                line = await session.prompt_async(prompt_fragments, bottom_toolbar=get_bottom_toolbar, style=dynamic_style)
            else:
                line = input(fallback_prompt_str)

            line = line.strip()
            if not line:
                continue

            if line.lower() in ["exit", "quit", ":q", "/exit", "/quit"]:
                print("Exiting Omni REPL. Goodbye!")
                break

            # Handle Slash Commands via CommandExecutor
            # Handle Slash Commands strictly locally (NEVER forward /commands to LLM)
            from .features.prompt_templates import expand as _expand_prompt
            _expanded = _expand_prompt(line) if line.startswith("/") else None
            if _expanded:
                line = _expanded  # Pi prompt template or /skill:name -> sent to the model
            if line.startswith("/") and not _expanded:
                def set_model_cb(new_m):
                    nonlocal active_model
                    active_model = router.resolve_model(new_m)

                def set_mode_cb(new_mode_name):
                    nonlocal active_mode
                    active_mode = new_mode_name

                handled, cmd_msg = await cmd_executor.execute(
                    line, session_state, context={"switch_model": set_model_cb, "switch_mode": set_mode_cb, "clear_prompt_history": lambda: (hasattr(session, 'history') and hasattr(session.history, '_loaded_strings') and session.history._loaded_strings.clear())}
                )
                if handled:
                    if cmd_msg:
                        print(cmd_msg)
                else:
                    print(f"[!] Unknown slash command '{line.split()[0]}'. Type /help for available commands.")
                continue

            spinner = LiveStreamingSpinner(f"[{model_short}] Generating response...")
            spinner.start()
            try:
                response = await engine.run_turn(
                    session_state,
                    line,
                    max_tool_iterations=getattr(PROFILES.get(active_mode), "max_tool_iterations", 10),
                    model=active_model,
                )
            finally:
                spinner.stop()
                try:
                    from .kernel.session_store import SessionStore
                    SessionStore().save(session_state)
                except Exception:
                    pass

            # Render response in themed rounded chat bubble
            print_rich_markdown(response, model_name=active_model)

        except (KeyboardInterrupt, asyncio.CancelledError):
            print("\n[Canceled turn. Type exit to quit.]")
            continue
        except EOFError:
            print("\nExiting Omni REPL.")
            break
        except Exception as e:
            print(f"\n[!] Error during turn: {e}")
