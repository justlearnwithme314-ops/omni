"""Shell-level commands that work without a session, like Pi's:

    omni install|remove|uninstall|update|list ...     packages (also `omni pi install ...`)
    omni mcp add|remove|list|login|logout ...         MCP servers
    omni login anthropic|google-antigravity           subscription sign-in (OAuth) into auth.json
                                                      (other `omni login ...` forms stay with the legacy CLI)
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

PACKAGE_COMMANDS = ("install", "remove", "uninstall", "update", "list")
COMMANDS = PACKAGE_COMMANDS + ("mcp",)


def _agent_dir() -> Path:
    from .resources import agent_dir
    d = agent_dir()
    d.mkdir(parents=True, exist_ok=True)
    return d


def _trusted(cwd: str, agent: Path) -> bool:
    from . import resources as res
    if not res.project_has_resources(cwd):
        return True
    return bool(res.TrustStore(agent).decision(cwd))


def run(argv: List[str]) -> Optional[int]:
    """Return an exit code when argv is a shell subcommand, else None."""
    if argv and argv[0] == "login" and _is_core_login(argv[1:]):
        return login_cli(argv[1:], _agent_dir())
    if not argv or argv[0] not in COMMANDS:
        return None
    agent, cwd = _agent_dir(), os.getcwd()
    if argv[0] in PACKAGE_COMMANDS:
        from .packages import cli
        return cli(argv, agent, cwd)
    return mcp_cli(argv[1:], agent, cwd)


def _is_core_login(rest: List[str]) -> bool:
    from .ai.oauth import PROVIDERS
    return len(rest) == 1 and rest[0] in PROVIDERS


def login_cli(rest: List[str], agent: Path) -> int:
    """`omni login <provider>`: OAuth sign-in from a plain terminal (browser + optional pasted code)."""
    from . import safe_stdio
    safe_stdio()
    from .ai import oauth
    from .ai.auth import AuthStorage
    pid = rest[0]
    try:
        cred = oauth.login(pid)
    except oauth.LoginCancelled:
        print("Login cancelled.")
        return 1
    except Exception as e:
        print(f"{pid} sign-in failed: {e}", file=sys.stderr)
        return 1
    store = AuthStorage(agent)
    store.set_oauth(pid, cred)
    who = cred.get("email") or cred.get("orgName")
    print(f"Signed in to {oauth.PROVIDERS[pid].name}" + (f" as {who}" if who else "") + f". Saved to {store.path}")
    return 0


def _kv(items: List[str], flag: str) -> Dict[str, str]:
    out = {}
    for it in items:
        if "=" not in it:
            raise SystemExit(f"{flag} expects KEY=VALUE, got {it!r}")
        k, v = it.split("=", 1)
        out[k] = v
    return out


def mcp_cli(argv: List[str], agent: Path, cwd: str) -> int:
    from . import safe_stdio
    safe_stdio()
    from . import mcp as M
    if not argv or argv[0] in ("-h", "--help", "help"):
        print("usage: omni mcp add <name> [-l] [--exposure M] [--description T] (--url URL [--header K=V] [--bearer-token-env-var NAME]\n"
              "                                   [--oauth-client-id ID] [--oauth-client-secret S] [--oauth-callback-port N]"
              " | [--env K=V] [--cwd DIR] -- <command> [args...])\n"
              "       omni mcp remove <name> [-l]\n       omni mcp list [--json]\n       omni mcp login <name> [-l] [--timeout S]\n"
              "       omni mcp logout <name>")
        return 0
    sub, rest = argv[0], argv[1:]
    local = False
    if sub == "add":
        if "--" in rest:
            i = rest.index("--")
            opts, command = rest[:i], rest[i + 1:]
        else:
            opts, command = rest, []
        name = None
        cfg: Dict[str, Any] = {}
        env, headers = [], []
        j = 0
        while j < len(opts):
            a = opts[j]
            nxt = opts[j + 1] if j + 1 < len(opts) else None
            if a in ("-l", "--local"):
                local = True
            elif a == "--url":
                cfg["url"] = nxt; j += 1
            elif a == "--env":
                env.append(nxt or ""); j += 1
            elif a == "--header":
                headers.append(nxt or ""); j += 1
            elif a == "--cwd":
                cfg["cwd"] = nxt; j += 1
            elif a == "--exposure":
                cfg["exposure"] = nxt; j += 1
            elif a == "--description":
                cfg["description"] = nxt; j += 1
            elif a == "--bearer-token-env-var":
                headers.append(f"Authorization=Bearer ${{{nxt}}}"); j += 1
            elif a.startswith("--oauth-"):
                key = {"--oauth-client-id": "clientId", "--oauth-client-secret": "clientSecret", "--oauth-callback-port": "redirectPort",
                       "--oauth-client-name": "clientName", "--oauth-client-registration": "clientRegistration"}.get(a)
                if not key:
                    print(f"unknown option {a}")
                    return 2
                cfg.setdefault("oauth", {})[key] = int(nxt) if key == "redirectPort" and nxt and nxt.isdigit() else nxt
                j += 1
            elif name is None and not a.startswith("-"):
                name = a
            else:
                print(f"unknown option {a}")
                return 2
            j += 1
        if not name:
            print("omni mcp add: a server name is required")
            return 2
        if command:
            cfg["command"], cfg["args"] = command[0], command[1:]
        if env:
            cfg["env"] = _kv(env, "--env")
        if headers:
            cfg["headers"] = _kv(headers, "--header")
        err = M.validate(name, dict(cfg))
        if err:
            print(f"✗ {name}: {err}")
            return 2
        path = M.save_server(agent, cwd, name, cfg, local=local)
        print(f"✓ added {name} to {path}. Run `omni mcp list` to check it.")
        return 0
    if sub == "remove":
        local = "-l" in rest or "--local" in rest
        names = [a for a in rest if not a.startswith("-")]
        if not names:
            print("omni mcp remove: a server name is required")
            return 2
        path = (Path(cwd) / ".pi" / "mcp.json") if local else (agent / "mcp.json")
        data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        if names[0] not in (data.get("mcpServers") or {}):
            print(f"· {names[0]} is not in {path}")
            return 1
        M.save_server(agent, cwd, names[0], None, local=local)
        print(f"✓ removed {names[0]} from {path} (stored sign-in kept; `omni mcp logout {names[0]}` deletes it)")
        return 0
    if sub == "list":
        mgr = M.McpManager(agent, cwd, _trusted(cwd, agent))
        mgr.load()
        mgr.start()
        mgr.wait_all(30)
        bad = bool(mgr.errors)
        if "--json" in rest:
            out = [{"name": s.name, "state": s.state, "exposure": s.exposure, "source": s.cfg.get("_source"), "error": s.error,
                    "tools": [{"name": t["name"], "exposure": s.tool_exposure(t["name"])} for t in s.tools]} for s in mgr.servers.values()]
            print(json.dumps({"servers": out, "errors": mgr.errors}, indent=2))
        else:
            if not mgr.servers and not mgr.errors:
                print("No MCP servers configured. Add one with: omni mcp add <name> -- <command>")
            for s in mgr.servers.values():
                print(f"{s.name}  {s.state}  exposure={s.exposure}  {s.cfg.get('_source', '')}")
                for t in s.tools:
                    e = s.tool_exposure(t["name"])
                    print(f"    {t['name']}" + (f"  [{e}]" if e != s.exposure else ""))
                if s.error:
                    print("    " + s.error.replace("\n", "\n    "))
            for e in mgr.errors:
                print(f"! {e}")
        bad = bad or any(s.state != "connected" for s in mgr.servers.values() if s.cfg.get("enabled") is not False)
        mgr.close()
        return 1 if bad else 0
    if sub in ("login", "logout"):
        names = [a for a in rest if not a.startswith("-") and not a.isdigit()]
        if not names:
            print(f"omni mcp {sub}: a server name is required")
            return 2
        from . import mcp_oauth
        if sub == "logout":
            print(f"{'✓ signed out of' if mcp_oauth.logout(agent, names[0]) else '· no stored sign-in for'} {names[0]}")
            return 0
        cfgs, _errs, _ = M.load_config(agent, cwd, _trusted(cwd, agent))
        cfg = cfgs.get(names[0])
        if not cfg or not cfg.get("url"):
            print(f"✗ {names[0]} is not a configured streamable-HTTP server")
            return 1
        try:
            mcp_oauth.login(agent, names[0], cfg)
        except Exception as e:
            print(f"✗ sign-in failed: {e}")
            return 1
        print(f"✓ signed in to {names[0]}")
        return 0
    print(f"unknown mcp command {sub}")
    return 2


def main(argv: Optional[List[str]] = None) -> int:
    rc = run(list(sys.argv[1:] if argv is None else argv))
    return 2 if rc is None else rc
