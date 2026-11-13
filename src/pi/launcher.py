"""Decides whether an `omni ...` invocation runs on the core (the Pi-based runtime and window) or on the legacy CLI.
Imports nothing heavy.

    omni                          core, active profile (omni.config.json `command_profile`, default pi)
    omni -c / -p "..." / --model  core, active profile (any invocation starting with a flag)
    omni "fix the tests"          core, active profile, with that first message
    omni run "..." [-m model]     core, print mode
    omni pi [...]                 core, pi profile
    omni --profile <name> [...]   core, that profile
    omni profile [name]           list / set the active profile
    omni install|remove|update|list   Pi packages (npm:, git:, local paths); also `omni pi install ...`
    omni mcp add|remove|list|login|logout
    omni legacy [...]             the previous Omni CLI and REPL (subcommands such as `models`, `toggle` go there too)
"""
from __future__ import annotations

import sys
from typing import List, Optional


def _run_args(rest: List[str]) -> List[str]:
    out: List[str] = []
    has_prompt, j = False, 0
    while j < len(rest):  # `omni run "prompt" -m x` meant one-shot; keep that meaning (-p)
        a = rest[j]
        if a in ("--model", "-m") and j + 1 < len(rest):
            out += ["--model", rest[j + 1]]
            j += 2
            continue
        if a == "--mode" and j + 1 < len(rest):
            out += ["--profile", rest[j + 1]]
            j += 2
            continue
        has_prompt = has_prompt or not a.startswith("-")
        out.append(a)
        j += 1
    return (["-p"] if has_prompt else []) + out


def core_argv(argv: List[str]) -> Optional[List[str]]:
    """argv for the core CLI, or None when the legacy CLI should handle the invocation."""
    if argv and argv[0] in ("legacy", "--legacy"):
        return None
    if argv and argv[0] == "pi":
        return ["--profile", "pi"] + argv[1:]
    if "--profile" in argv:
        return argv
    from .profiles import active_profile_name
    prof = ["--profile", active_profile_name()]
    if not argv:
        return prof
    if argv[0] == "run":
        return prof + _run_args(argv[1:])
    if argv[0].startswith("-") or argv[0].startswith("@") or " " in argv[0]:
        return prof + argv
    return None


# backwards compatible name used by tests
pi_argv = core_argv


def profile_command(argv: List[str]) -> bool:
    """`omni profile` / `omni profiles` / `omni profile <name>` for core profiles. Returns True when handled."""
    if not argv or argv[0] not in ("profile", "profiles"):
        return False
    from .profiles import active_profile_name, list_profiles, set_active_profile
    profiles = {p["name"]: p for p in list_profiles()}
    if len(argv) == 1 or argv[0] == "profiles":
        cur = active_profile_name()
        print("\n  Omni profiles (lightest first)\n")
        for name, p in profiles.items():
            mark = "●" if name == cur else " "
            print(f"  {mark} {name:<11} {p.get('description', '')}")
        print("\n  omni profile <name>   switch      ·   omni --profile <name>   one run only\n")
        return True
    from .profiles import canonical
    name = canonical(argv[1])
    if name not in profiles:
        return False  # maybe a legacy command profile; let the old CLI handle it
    set_active_profile(name)
    print(f"  ✓ active profile: {name} — {profiles[name].get('description', '')}")
    return True


def capability_command(argv: List[str]) -> bool:
    """`omni capabilities` (list) and `omni toggle <id> [on|off]`, without starting a session."""
    if not argv or argv[0] not in ("capabilities", "caps", "toggle"):
        return False
    from . import resources as res
    from .caps import effective_profile, load_builtin, read_overlay, write_overlay
    from .profiles import active_profile_name, load_profile
    agent = res.agent_dir()
    caps = load_builtin()
    base = load_profile(active_profile_name())
    if argv[0] == "toggle":
        if len(argv) < 2 or argv[1] not in caps or (len(argv) > 2 and argv[2] not in ("on", "off")):
            print("usage: omni toggle <capability> [on|off]\n  capabilities: " + ", ".join(sorted(caps)))
            return True
        cap, eff = caps[argv[1]], effective_profile(base, agent)
        plain = effective_profile(base, agent, use_overlay=False)
        state = (lambda prof: cap.is_on(prof) if cap.is_on else cap.id in prof["capabilities"])
        new = (argv[2] == "on") if len(argv) > 2 else not state(eff)
        overlay = read_overlay(agent)
        if new == state(plain):
            overlay.pop(cap.id, None)
        else:
            overlay[cap.id] = new
        write_overlay(agent, overlay)
        print(f"  ✓ {cap.id}: {'on' if new else 'off'}  (applies to every profile; `omni toggle {cap.id}` flips it back)")
        return True
    eff = effective_profile(base, agent)
    print(f"\n  Capabilities · profile {eff['name']}\n")
    for cid, cap in sorted(caps.items(), key=lambda kv: (kv[1].group != "feature", kv[0])):
        on = cap.is_on(eff) if cap.is_on else cid in eff["capabilities"]
        mine = "  (your override)" if cid in eff["_overlay"] else ""
        print(f"  {'●' if on else '○'} {cid:<19} {cap.description}{mine}")
    print("\n  omni toggle <id> [on|off]   ·   in a session: /capabilities\n")
    return True


def maybe_launch_pi(argv: Optional[List[str]] = None) -> None:
    import os
    from . import safe_stdio
    safe_stdio()
    if os.environ.get("OMNI_LEGACY") == "1":
        return
    raw = sys.argv[1:] if argv is None else argv
    if profile_command(raw) or capability_command(raw):
        sys.exit(0)
    if raw[:1] == ["gateway"] and (len(raw) == 1 or raw[1] in ("run", "status", "approve")):
        from . import resources as res
        from .gateway import cli as gateway_cli
        hard_exit(gateway_cli(raw[1:], res.agent_dir()))
    if raw[:1] == ["cron"]:
        from . import resources as res
        from .caps.scheduler import cli as cron_cli
        hard_exit(cron_cli(raw[1:], res.agent_dir()))
    from .subcommands import run as run_sub
    sub = raw[1:] if raw[:1] == ["pi"] else raw
    rc = run_sub(sub)
    if rc is not None:
        sys.exit(rc)
    if raw and raw[0] in ("legacy", "--legacy"):
        sys.argv = [sys.argv[0]] + raw[1:]  # hand the rest to the legacy CLI
        os.environ["OMNI_LEGACY"] = "1"
        return
    args = core_argv(raw)
    if args is None:
        return
    from .cli import main
    hard_exit(main(args))


def hard_exit(code: int) -> None:
    """Leave now. A normal interpreter exit joins every worker thread first (the extension host's dispatch pool,
    plugin event loops), which added ~5 s after the answer was already printed. Session files are written
    synchronously, so nothing is lost by skipping that."""
    import os
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.flush()
        except Exception:
            pass
    os._exit(int(code or 0))
