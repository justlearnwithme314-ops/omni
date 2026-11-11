"""`omni pi ...` / `omni --profile pi ...` command line, mirroring Pi's flags (docs/cli.md)."""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import List, Optional

from . import PI_COMPAT_VERSION
from .agent import SessionOptions


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="omni", description="Omni: a modular coding agent. Profiles go from barebones "
                                "Pi (`--profile pi`) to everything on (`--profile full`).", allow_abbrev=False)
    a = p.add_argument
    a("messages", nargs="*", help="initial prompt(s); @path includes a file")
    a("-p", "--print", action="store_true", help="run the prompts, print the final answer, exit")
    a("--mode", choices=["text", "json", "rpc"], default="text")
    a("--provider")
    a("--model", "-m")
    a("--api-key")
    a("--thinking", choices=["off", "minimal", "low", "medium", "high", "xhigh", "max"])
    a("--models", help="comma-separated model patterns for ctrl+p cycling")
    a("--list-models", nargs="?", const="", default=None, metavar="SEARCH")
    a("-c", "--continue", dest="continue_recent", action="store_true", help="continue the most recent session here")
    a("-r", "--resume", action="store_true", help="pick a session to resume")
    a("--session", help="session file path or id prefix")
    a("--session-dir")
    a("--no-session", action="store_true")
    a("-n", "--name")
    a("-t", "--tools", help="tool list: read,bash,edit,write,grep,find,ls ( +name / -name modify the defaults)")
    a("-xt", "--exclude-tools", default="")
    a("-nbt", "--no-builtin-tools", action="store_true")
    a("-nt", "--no-tools", action="store_true")
    a("-e", "--extension", action="append", default=[])
    a("-ne", "--no-extensions", action="store_true")
    a("--skill", action="append", default=[])
    a("-ns", "--no-skills", action="store_true")
    a("--prompt-template", action="append", default=[])
    a("-np", "--no-prompt-templates", action="store_true")
    a("-nc", "--no-context-files", action="store_true")
    a("--system-prompt")
    a("--append-system-prompt")
    a("--use-theme")
    a("-a", "--approve", action="store_true", help="trust project-local files for this run")
    a("-na", "--no-approve", action="store_true", help="ignore trust-gated project files")
    a("--no-mcp", action="store_true", help="do not connect MCP servers")
    a("--no-omni-plugins", action="store_true", help="do not load extensions from Omni's plugins/pi folder")
    a("--profile", help="profile from profiles/*.json or ~/.omni/profiles (core, lean, coder, assistant, balanced, "
                        "autonomous, full; aliases: pi, omp, hermes, openclaw)")
    a("--capabilities", help="exact module list for this run, e.g. todo,ask,fetch ('' = none); overrides the profile")
    a("--list-profiles", action="store_true")
    a("-v", "--version", action="store_true")
    return p


def options_from_args(args: argparse.Namespace) -> SessionOptions:
    plugin_dir = None
    if not args.no_omni_plugins:
        cand = Path(__file__).resolve().parents[2] / "plugins" / "pi"
        plugin_dir = str(cand) if cand.is_dir() else None
    approve = True if args.approve else False if args.no_approve else None
    o = SessionOptions(
        cwd=os.getcwd(), model=args.model, provider=args.provider, api_key=args.api_key, thinking=args.thinking,
        tools=args.tools, exclude_tools=[t for t in args.exclude_tools.split(",") if t], no_builtin_tools=args.no_builtin_tools,
        no_tools=args.no_tools, no_session=args.no_session, session=args.session, continue_recent=args.continue_recent,
        session_dir=args.session_dir, session_name=args.name, extensions=args.extension, no_extensions=args.no_extensions,
        skills=args.skill, no_skills=args.no_skills, prompt_templates=args.prompt_template,
        no_prompt_templates=args.no_prompt_templates, no_context_files=args.no_context_files, system_prompt=args.system_prompt,
        append_system_prompt=args.append_system_prompt, approve=approve,
        mode="rpc" if args.mode == "rpc" else "json" if args.mode == "json" else "print" if args.print else "interactive",
        omni_plugin_dir=plugin_dir, profile=args.profile, no_mcp=args.no_mcp,
        capabilities=None if args.capabilities is None else [c.strip() for c in args.capabilities.split(",") if c.strip()])
    return o


def expand_file_args(messages: List[str]) -> List[str]:
    """`@path` arguments are inlined into the first message (Pi's CLI file processor)."""
    files, text = [], []
    for m in messages:
        if m.startswith("@") and os.path.isfile(m[1:]):
            try:
                files.append(f'<file name="{m[1:]}">\n{Path(m[1:]).read_text(encoding="utf-8")}\n</file>')
                continue
            except UnicodeDecodeError:
                pass
        text.append(m)
    if not files:
        return text
    first = "\n".join(files) + ("\n\n" + text[0] if text else "")
    return [first] + text[1:]


def _stdin_is_piped() -> bool:
    import stat
    try:
        if sys.stdin is None or sys.stdin.isatty():
            return False
        mode = os.fstat(sys.stdin.fileno()).st_mode
        return stat.S_ISFIFO(mode) or stat.S_ISREG(mode)
    except (OSError, ValueError):
        return False


def main(argv: Optional[List[str]] = None) -> int:
    from .subcommands import run as run_sub
    raw = list(sys.argv[1:] if argv is None else argv)
    if "--profile" in raw[:2]:
        i = raw.index("--profile")
        rest = raw[:i] + raw[i + 2:]
    else:
        rest = raw
    rc = run_sub(rest)
    if rc is not None:
        return rc
    args = build_parser().parse_args(argv)
    if args.version:
        print(f"omni core (Pi-compatible, tracks pi {PI_COMPAT_VERSION})")
        return 0
    if args.list_profiles:
        from .launcher import profile_command
        profile_command(["profiles"])
        return 0
    if args.use_theme:
        os.environ["OMNI_PI_THEME"] = args.use_theme
    opts = options_from_args(args)
    if args.list_models is not None:
        from .agent import AgentSession
        s = AgentSession(opts)
        q = args.list_models.lower()
        for m in s.registry.all():
            key = f"{m['provider']}/{m['id']}"
            if q in key.lower():
                print(f"{'✓' if s.auth.has_auth(m['provider']) else ' '} {key:<50} ctx {m['contextWindow']:>9,}  "
                      f"{'reasoning' if m.get('reasoning') else ''}")
        return 0
    if args.resume and not args.session:
        from .agent import AgentSession
        from .session import SessionManager
        s = AgentSession(opts)
        items = SessionManager.list_sessions(s.sessions.session_dir)
        for i, it in enumerate(items[:20]):
            print(f"  [{i}] {it['name'] or it['firstMessage'][:70] or '(empty)'}  · {it['messageCount']} msgs")
        if not items:
            print("No sessions for this folder.")
            return 1
        choice = input("resume #: ").strip()
        if not choice.isdigit() or int(choice) >= len(items):
            return 1
        opts.session = items[int(choice)]["path"]
    messages = expand_file_args(args.messages)
    if args.mode != "rpc" and _stdin_is_piped():
        piped = sys.stdin.read()
        if piped.strip():
            messages = [piped + ("\n\n" + messages[0] if messages else "")] + messages[1:]
            if opts.mode == "interactive":
                opts.mode = "print"
    if opts.mode == "rpc":
        from .modes import run_rpc
        return run_rpc(opts)
    if opts.mode in ("print", "json"):
        from .modes import run_print
        if not messages:
            print("No prompt given.", file=sys.stderr)
            return 2
        return run_print(opts, messages, json_mode=opts.mode == "json")
    from .tui.app import run_interactive
    if args.use_theme:
        from .resources import agent_dir  # noqa: F401
    return run_interactive(opts, messages)


if __name__ == "__main__":
    sys.exit(main())
