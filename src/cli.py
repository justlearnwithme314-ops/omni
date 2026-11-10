"""
OmniHarness CLI Entrypoint
Unified interface combining Pi-style lean REPL, OpenClaw skill execution, and Hermes self-evolution.
"""

import argparse
import asyncio
import os
import sys
from typing import Optional

if __name__ == "__main__":  # route the Pi profile before importing the rest of Omni (keeps `omni pi` lean)
    from .pi.launcher import maybe_launch_pi
    maybe_launch_pi()

from .kernel.config import HarnessConfig
from .kernel.events import EventBus
from .kernel.state import SessionState
from .kernel.engine import KernelEngine
from .providers.keyring import Keyring
from .providers.router import ProviderRouter
from .adapters.registry import MasterToolRegistry
from .modes.manager import ModeManager, PROFILES
from .telemetry.tracer import TrajectoryTracer
from .evolution.optimizer import EvolutionOptimizer


def render_omni_dashboard(config=None, model="google-antigravity/gemini-2.5-flash", profile="AUTONOMOUS", tools_count=34):
    """Renders the solid blue block logo and sleek Pi/OMP dashboard."""
    try:
        from rich.console import Console, Group
        from rich.panel import Panel
        from rich.table import Table
        from rich.text import Text
        from rich import box
        from .features.themes import ThemeManager

        tm = ThemeManager()
        active_theme = tm.get_theme()
        console = Console(safe_box=False)

        # Solid Block Logo (matching user Pi 'agent' banner)
        logo_text = (
            "\n"
            " ██████╗ ███╗   ███╗███╗   ██╗██╗\n"
            "██╔═══██╗████╗ ████║████╗  ██║██║\n"
            "██║   ██║██╔████╔██║██╔██╗ ██║██║\n"
            "██║   ██║██║╚██╔╝██║██║╚██╗██║██║\n"
            "╚██████╔╝██║ ╚═╝ ██║██║ ╚████║██║\n"
            " ╚═════╝ ╚═╝     ╚═╝╚═╝  ╚═══╝╚═╝\n"
        )
        console.print(Text(logo_text, style="bold #00bfff"))

        title_text = Text()
        title_text.append(" ◆ ", style=f"bold {active_theme.primary}")
        title_text.append("OMNI", style="bold bright_white")
        title_text.append(" • ", style="dim")
        title_text.append("Universal Agentic AI System", style=f"italic {active_theme.secondary}")

        table = Table(show_header=False, box=None, padding=(0, 1), expand=True)
        table.add_column(style=f"bold {active_theme.primary}", width=18)
        table.add_column(style="bright_white")

        table.add_row("⚡ Active Model:", model)
        badge = f"{tools_count} core tools active" if profile.lower() == "lean" else f"{tools_count} tools active • OpenClaw + Hermes + Pi"
        table.add_row("⚙ Profile Mode:", f"{profile.upper()} [dim]({badge})[/dim]")
        table.add_row("🔑 Keyring Auth:", f"[bold {active_theme.success}][✓][/bold {active_theme.success}] Google OAuth (Connected)")
        table.add_row("🎨 Visual Theme:", f"{active_theme.name.title()} [dim](type /skin to change)[/dim]")

        footer_text = Text()
        footer_text.append("Type a prompt to chat • ", style="dim")
        footer_text.append("Tab", style="bold bright_yellow")
        footer_text.append(" for autocomplete • ", style="dim")
        footer_text.append("/help", style=f"bold {active_theme.primary}")
        footer_text.append(" for commands", style="dim")

        content = Group(table, Text(""), footer_text)

        panel = Panel(
            content,
            title=title_text,
            border_style=active_theme.panel_border,
            box=box.ROUNDED,
            padding=(1, 2)
        )

        console.print(panel)
        console.print()
    except Exception:
        print(f"\n=== OMNI [{model}] ({profile}) ===\n")

def print_banner():
    render_omni_dashboard()


async def async_run(args, config: HarnessConfig):
    # Initialize Core Subsystems
    event_bus = EventBus()
    keyring = Keyring()
    router = ProviderRouter(keyring=keyring, config=config)
    router.allow_mock_fallback = False  # real provider errors must be shown, never replaced by fake replies

    mode_val = getattr(args, "mode", None) or config.active_mode
    mode_mgr = ModeManager(default_mode=mode_val)
    mode_mgr.apply_to_config(config)

    tool_registry = MasterToolRegistry(config=config, event_bus=event_bus)
    tool_registry.discover_all()

    tracer = TrajectoryTracer()

    engine = KernelEngine(
        config=config,
        event_bus=event_bus,
        provider_router=router,
        tool_registry=tool_registry,
        tracer=tracer,
    )

    # Determine active model
    raw_model = getattr(args, "model", None) or getattr(config, "default_model", None) or "google/gemini-2.5-flash"
    active_model = router.resolve_model(raw_model)

    profile = mode_mgr.get_profile()
    if tool_registry:
        from .kernel.features import FeatureRegistry
        f_reg = FeatureRegistry(config_data=getattr(config, "config_data", {}), config_path=getattr(config, "config_path", None))
        f_reg.apply_profile(profile.name.lower())
        tool_registry.apply_feature_toggles(f_reg)
    tool_count = len([t for t in tool_registry.list_tools() if t.enabled]) if tool_registry else 0
    render_omni_dashboard(config=config, model=active_model, profile=profile.name, tools_count=tool_count)

    prompt_val = getattr(args, "prompt", None)
    user_prompt = " ".join(prompt_val) if prompt_val else None
    if not user_prompt:
        from .repl import run_rich_repl
        from .kernel.services import services
        try:
            await run_rich_repl(
                engine=engine,
                config=config,
                router=router,
                tool_registry=tool_registry,
                tracer=tracer,
                initial_model=active_model,
                initial_mode=profile.name,
            )
        finally:
            services.shutdown()  # stop collab/canvas servers, gateways, any browser we launched
    else:
        print(f"\n[prompt] > {user_prompt}")
        session_state = await engine.run_session(user_prompt, model=active_model)
        last_msg = session_state.messages[-1] if session_state.messages else None
        from .repl import print_rich_markdown
        print()
        print_rich_markdown(last_msg.content if last_msg else "<no response>")


def cmd_toggle(args, config: HarnessConfig):
    from .repl import print_feature_table
    from .kernel.features import FeatureRegistry
    mgr = FeatureRegistry(config_data=getattr(config, "config_data", {}), config_path=getattr(config, "config_path", None))
    if not args.feature:
        print_feature_table(mgr)
        return
    val = None
    if args.value:
        val = args.value.lower() in ("on", "true", "1", "yes")
        ok = mgr.set_feature(args.feature, val)
    else:
        val = mgr.toggle(args.feature)
        ok = val is not None
    if ok:
        st = "ON" if val else "OFF"
        print(f"[✓] Feature '{args.feature}' is now {st}")
    else:
        print(f"[!] Unknown feature '{args.feature}'. Run 'omni toggle' to list all features.")


def cmd_mode(args, config: HarnessConfig):
    from .kernel.features import PROFILE_PRESETS, FeatureRegistry
    mgr = FeatureRegistry(config_data=getattr(config, "config_data", {}), config_path=getattr(config, "config_path", None))
    if not args.name:
        cur = getattr(config, "active_mode", "autonomous")
        print(f"Current mode: {cur.upper()}")
        print("Available modes:")
        for k, p in PROFILE_PRESETS.items():
            print(f"  • {k.upper():<12} - {p["description"]}")
        return
    mode = args.name.lower()
    if mode in PROFILE_PRESETS:
        mgr.apply_profile(mode)
        config.set_mode(mode)
        print(f"[✓] Switched mode to: {mode.upper()} - {PROFILE_PRESETS[mode]["description"]}")
    else:
        print(f"[!] Unknown mode '{args.name}'. Available: lean, balanced, autonomous, evolve")


def cmd_profile(args, config: HarnessConfig):
    from .kernel.commands import CommandRegistry, COMMAND_PROFILES
    reg = CommandRegistry(config=config, active_profile=getattr(config, "command_profile", "full"))
    if not args.name:
        cur = reg.active_profile.upper()
        print(f"\nActive Command Profile: {cur}")
        print("Available Profiles:")
        for k, p in COMMAND_PROFILES.items():
            m = " * " if k == reg.active_profile else "   "
            print(f" {m}{k.upper():<12} - {p["description"]}")
        return
    if reg.apply_profile(args.name):
        print(f"[✓] Switched command profile to: {args.name.upper()}")
    else:
        print(f"[!] Unknown profile '{args.name}'. Available: {list(COMMAND_PROFILES.keys())}")


def cmd_commands(args, config: HarnessConfig):
    from .kernel.commands import CommandRegistry
    reg = CommandRegistry(config=config, active_profile=getattr(config, "command_profile", "full"))
    cmds = reg.get_active_commands()
    print(f"\n=== Active Omni Slash Commands (Profile: {reg.active_profile.upper()}) ===")
    categories = {}
    for c in cmds:
        categories.setdefault(c.category, []).append(c)
    for cat, clist in sorted(categories.items()):
        print(f"\n  [{cat.upper()}]:")
        for c in clist:
            print(f"    /{c.name:<16} - {c.description}")

def cmd_models(args, config: HarnessConfig, keyring: Keyring):
    from .providers.router import MODEL_ALIASES
    active_def = getattr(config, "default_model", None) or "google/gemini-2.5-flash"
    print("\n=== Omni Model Catalog ===")
    print(f"Default Active Model: {active_def}\n")

    print("--- Model Tiers ---")
    for tier, models in config.model_tiers.items():
        print(f"  [{tier.upper()}]:")
        for m in models:
            active_marker = " * " if m == active_def else "   "
            print(f"  {active_marker} {m}")

    print("\n--- Quick Aliases ---")
    for alias, target in sorted(MODEL_ALIASES.items()):
        print(f"   {alias:<12} -> {target}")

    print("\n--- Active Providers in Keyring ---")
    keys = keyring.list_keys()
    providers = sorted({k["provider"] for k in keys if k.get("is_active")})
    for p in providers:
        print(f"   [✓] {p}")
    print("\nUsage: omni model <alias_or_name> (set default)")
    print("       omni run \"prompt\" -m <model> (one-off)")


def cmd_model(args, config: HarnessConfig):
    from .providers.router import ProviderRouter
    if not args.name:
        current = getattr(config, "default_model", None) or "google/gemini-2.5-flash"
        print(f"Current default model: {current}")
        print("To change: omni model <name_or_alias> (e.g. omni model sonnet)")
        return

    resolved = ProviderRouter.resolve_model(args.name)
    config.config_data["default_model"] = resolved
    import json
    with open(config.config_path, "w", encoding="utf-8") as f:
        json.dump(config.config_data, f, indent=2)
    print(f"[✓] Default model updated to: {resolved}")

def cmd_login(args, keyring: Keyring):
    from .providers.oauth import OAuthManager
    oauth_mgr = OAuthManager(keyring)

    if getattr(args, "from_omp", False) or (args.provider and args.provider.lower() in ("omp", "from-omp", "import")):
        imported = oauth_mgr.import_from_omp()
        if not imported:
            print("[!] No active OAuth tokens found in ~/.omp/agent/agent.db")
        else:
            msg = len(imported)
            print(f"\n[+] Successfully imported {msg} OAuth credential(s) from Oh My Pi:")
            for item in imported:
                print(f"  • Provider: {item['provider']} | Email: {item['email']} | Key ID: {item['key_id']}")
        return

    if getattr(args, "oauth", False):
        provider = args.provider.lower() if args.provider else "github-copilot"
        try:
            res = oauth_mgr.login_device_code(provider)
            print(f"[✓] Successfully logged into {provider} via OAuth! (Key ID: {res['key_id']})")
        except Exception as e:
            print(f"[!] OAuth login failed: {e}")
        return

    provider = args.provider.lower() if args.provider else ""
    if not provider:
        print("Usage: omni login <provider> [--key <key>] [--oauth] [--from-omp]")
        print("       omni login --from-omp   (auto-imports your existing Oh My Pi OAuth tokens)")
        return

    key = args.key
    if not key:
        import getpass
        key = getpass.getpass(f"Enter API key for {provider}: ").strip()
    if not key:
        print("[!] Key cannot be empty.")
        return
    key_id = keyring.add_key(provider, key, priority=args.priority)
    print(f"[✓] Key successfully added for provider '{provider}' (id: {key_id})")


def cmd_plugins(args, config: HarnessConfig):
    bus = EventBus()
    registry = MasterToolRegistry(config=config, event_bus=bus)
    registry.discover_all()
    tools = registry.list_tools()
    print("\n--- Discovered Drop-In Tools & Skills ---")
    if not tools:
        print("  No tools discovered.")
        return
    for t in tools:
        print(f"  • [{t.source.upper()}] {t.name}: {t.description[:80]}")


def cmd_stats(args):
    tracer = TrajectoryTracer()
    stats = tracer.get_session_stats()
    print("\n--- Omni Telemetry (Metaharness Trace Store) ---")
    for k, v in stats.items():
        print(f"  {k}: {v}")


def cmd_evolve(args):
    tracer = TrajectoryTracer()
    optimizer = EvolutionOptimizer(tracer=tracer)
    failures = optimizer.analyze_failures()
    print(f"\n--- Hermes Self-Evolution Engine ---")
    print(f"Found {len(failures)} trajectory failure events in database.")
    for idx, f in enumerate(failures[:5]):
        print(f"  [{idx+1}] Category: {f['category']} | Detail: {f['detail'][:80]}")
    if args.patch and failures:
        patch = optimizer.generate_prompt_patch(
            failure_summary=failures[0]["category"],
            rule_instruction="Always double-check JSON schema syntax before tool submission."
        )
        print(f"[✓] Generated and saved evolution patch: {patch}")


def main():
    from .pi.launcher import maybe_launch_pi
    maybe_launch_pi()  # `omni pi ...`, `--profile pi`, or saved profile "pi" -> Pi runtime (exits)
    parser = argparse.ArgumentParser(description="Omni: Universal Agentic AI System")
    subparsers = parser.add_subparsers(dest="command")

    # Run command
    run_parser = subparsers.add_parser("run", help="Run the harness on a prompt or start interactive REPL")
    run_parser.add_argument("prompt", nargs="*", help="Optional prompt to execute")
    run_parser.add_argument("--mode", choices=["lean", "autonomous", "evolve"], help="Execution mode")
    run_parser.add_argument("--model", "-m", help="Specific model or alias (e.g. flash, sonnet, gpt-4o)")

    # Keys command
    keys_parser = subparsers.add_parser("keys", help="List registered API credentials and cooldown status")
    keys_parser.add_argument("--provider", help="Filter by provider")

    # Login command
    login_parser = subparsers.add_parser("login", help="Register a provider API key or OAuth session")
    login_parser.add_argument("provider", nargs="?", default="", help="Provider name (e.g. anthropic, openai, google, ollama)")
    login_parser.add_argument("--key", help="API key string (or prompted securely)")
    login_parser.add_argument("--priority", type=int, default=0, help="Priority in key pool (higher tries first)")
    login_parser.add_argument("--oauth", action="store_true", help="Launch interactive device-code OAuth flow")
    login_parser.add_argument("--from-omp", action="store_true", help="Auto-import active OAuth tokens from Oh My Pi (~/.omp)")

    # Models catalog command
    subparsers.add_parser("models", help="List model tiers, aliases, and provider status")

    # Model get/set command
    model_parser = subparsers.add_parser("model", help="Get or set default model")
    model_parser.add_argument("name", nargs="?", help="Model alias or full identifier to set as default")

    # Toggle command
    toggle_parser = subparsers.add_parser("toggle", help="View or toggle modular features")
    toggle_parser.add_argument("feature", nargs="?", help="Feature ID to toggle")
    toggle_parser.add_argument("value", nargs="?", help="on or off")

    # Mode command
    mode_parser = subparsers.add_parser("mode", help="Get or set runtime profile mode")
    mode_parser.add_argument("name", nargs="?", choices=["lean", "balanced", "autonomous", "evolve"], help="Profile mode")

    # Command profile command
    profile_parser = subparsers.add_parser("profile", help="View or set active command profile (pi, omp, hermes, openclaw, full)")
    profile_parser.add_argument("name", nargs="?", choices=["pi", "omp", "hermes", "openclaw", "full"], help="Profile name")

    # List active commands
    subparsers.add_parser("commands", help="List active slash commands under current profile")

    # Plugins command
    subparsers.add_parser("plugins", help="List all discovered drop-in plugins and skills")

    # Stats command
    subparsers.add_parser("stats", help="Display session telemetry and token usage stats")

    # Evolve command
    evolve_parser = subparsers.add_parser("evolve", help="Run Hermes self-evolution optimizer on failed traces")
    evolve_parser.add_argument("--patch", action="store_true", help="Auto-generate prompt patch for top failure")

    # Register all 56 OMP CLI Subcommands dynamically
    from .kernel.commands import CLI_SUBCOMMANDS
    for cmd_name, cmd_desc in CLI_SUBCOMMANDS.items():
        if cmd_name not in subparsers._name_parser_map:
            sp = subparsers.add_parser(cmd_name, help=cmd_desc[:80])
            sp.add_argument("args", nargs="*", help=f"Arguments for {cmd_name}")

    args = parser.parse_args()
    config = HarnessConfig()

    if not args.command or args.command == "run":
        asyncio.run(async_run(args, config))
    elif args.command == "keys":
        keyring = Keyring()
        cmd_keys(args, keyring)
    elif args.command == "login":
        keyring = Keyring()
        cmd_login(args, keyring)
    elif args.command == "models":
        keyring = Keyring()
        cmd_models(args, config, keyring)
    elif args.command == "model":
        cmd_model(args, config)
    elif args.command == "toggle":
        cmd_toggle(args, config)
    elif args.command == "mode":
        cmd_mode(args, config)
    elif args.command == "profile":
        cmd_profile(args, config)
    elif args.command == "commands":
        cmd_commands(args, config)
    elif args.command == "plugins":
        cmd_plugins(args, config)
    elif args.command == "stats":
        cmd_stats(args)
    elif args.command == "evolve":
        cmd_evolve(args)
    elif args.command in CLI_SUBCOMMANDS:
        from .kernel.commands import CommandRegistry
        from .kernel.commands_executor import CommandExecutor
        reg = CommandRegistry(config=config, active_profile=getattr(config, "command_profile", "full"))
        keyring = Keyring()
        router = ProviderRouter(keyring=keyring, config=config)
        tool_registry = MasterToolRegistry(config=config, event_bus=EventBus())
        tool_registry.discover_all()
        tracer = TrajectoryTracer()
        engine = KernelEngine(
            config=config,
            event_bus=EventBus(),
            provider_router=router,
            tool_registry=tool_registry,
            tracer=tracer,
        )
        executor = CommandExecutor(
            command_registry=reg,
            engine=engine,
            config=config,
            router=router,
            tool_registry=tool_registry,
            tracer=tracer,
        )
        state = SessionState()
        extra_args = " ".join(getattr(args, "args", []))
        full_line = f"/{args.command} {extra_args}".strip()
        handled, msg = asyncio.run(executor.execute(full_line, state))
        if msg:
            print(msg)



if __name__ == "__main__":
    main()
