"""Omni's Pi runtime: a faithful re-implementation of the Pi coding agent (pi-mono `coding-agent`).

Layout mirrors Pi's own packages so behaviour can be compared file by file:

    ai/          providers, model catalog, credentials        (pi-ai)
    tools/       read write edit bash grep find ls             (coding-agent/src/core/tools)
    session.py   JSONL v3 session tree                         (core/session-manager.ts)
    resources.py settings, trust, context files, skills, templates (core/resource-loader.ts, skills.ts ...)
    prompt.py    system prompt builder                         (core/system-prompt.ts)
    agent.py     agent loop + AgentSession                     (agent-core + core/agent-session.ts)
    extensions/  Node host that runs real Pi extensions        (core/extensions/*)
    tui/         terminal UI                                   (pi-tui + modes/interactive)
    modes/       print / json / rpc                            (modes/*)
    cli.py       `pi`-compatible command line                  (cli/args.ts, main.ts)

Nothing in here imports the rest of Omni except the credential store, so the Pi profile stays lean.
"""

def safe_stdio() -> None:
    """Keep CLI output from crashing on characters the stream can't encode (e.g. `✓` piped on a cp1252 console)."""
    import sys
    for stream in (sys.stdout, sys.stderr):
        try:
            if stream is not None and getattr(stream, "errors", "strict") in ("strict", "surrogateescape"):
                stream.reconfigure(errors="replace")  # type: ignore[attr-defined]
        except Exception:
            pass


PI_COMPAT_VERSION = "0.78"  # Pi release whose behaviour this runtime tracks (see docs/PI_PROFILE.md)
APP_NAME = "omni"
