"""Pi built-in slash commands: sessions, export, settings, auth, reload, ..."""
import json
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path

from ..session_store import SessionStore
from . import HandlerContext, UsageError, command
from ...paths import PROJECT_ROOT, user_data_dir, workspace_root

THINKING = ("off", "minimal", "low", "medium", "high", "xhigh")


def _store() -> SessionStore:
    return SessionStore()


def _switch(ctx: HandlerContext, new) -> None:
    st = ctx.state
    st.session_id, st.messages[:] = new.session_id, new.messages
    st.scratchpad.update(new.scratchpad)


def _settings_path() -> Path:
    return user_data_dir() / "settings.json"


def _settings() -> dict:
    p = _settings_path()
    return json.loads(p.read_text(encoding="utf-8")) if p.is_file() else {}


def _save_settings(d: dict) -> None:
    p = _settings_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(d, indent=2), encoding="utf-8")


def _user_msgs(st):
    return [(i, m) for i, m in enumerate(st.messages) if m.role == "user"]


@command("resume")
def resume(arg: str, ctx: HandlerContext) -> str:
    s = _store()
    if not arg:
        rows = s.list()[:15]
        if not rows:
            return "No saved sessions."
        return "\n".join(["--- Sessions (/resume <id or #>) ---"] + [
            f"  {i + 1:>2}. {r['id']}  {r['name'] or r['first'] or '(empty)'}  [{r['messages']} msgs]" for i, r in enumerate(rows)])
    ref = arg
    if arg.isdigit():
        rows = s.list()
        if not 1 <= int(arg) <= len(rows):
            raise UsageError("[!] No such session number")
        ref = rows[int(arg) - 1]["id"]
    s.save(ctx.state)
    _switch(ctx, s.load(ref))
    return f"[✓] Resumed {ctx.state.session_id} ({len(ctx.state.messages)} messages)"


@command("name")
def name(arg: str, ctx: HandlerContext) -> str:
    if arg:
        ctx.state.scratchpad["name"] = arg
        return f"[✓] Session renamed to: '{arg}'"
    return f"Session name: {ctx.state.scratchpad.get('name') or '(unnamed)'}"


@command("session")
def session(arg: str, ctx: HandlerContext) -> str:
    st = ctx.state
    roles = {}
    for m in st.messages:
        roles[m.role] = roles.get(m.role, 0) + 1
    chars = sum(len(m.content or "") for m in st.messages)
    return (f"Session {st.session_id}  name={st.scratchpad.get('name') or '-'}\n  file: {_store().path(st.session_id)}\n"
            f"  messages: {len(st.messages)} {roles}\n  ~tokens: {chars // 4}")


@command("tree")
def tree(arg: str, ctx: HandlerContext) -> str:
    um = _user_msgs(ctx.state)
    if not um:
        return "--- Session Timeline Tree ---\n(empty session)"
    return "\n".join(["--- Session Timeline Tree --- (/fork <n> to branch before a message)"] +
                     [f"  {n + 1:>2}. {(m.content or '').strip().replace(chr(10), ' ')[:80]}" for n, (_, m) in enumerate(um)])


@command("fork")
def fork(arg: str, ctx: HandlerContext) -> str:
    um = _user_msgs(ctx.state)
    if not arg.isdigit() or not 1 <= int(arg) <= len(um):
        return tree("", ctx) + "\nUsage: /fork <n>"
    idx, msg = um[int(arg) - 1]
    s = _store()
    s.save(ctx.state)
    ctx.state.session_id = uuid.uuid4().hex[:12]
    del ctx.state.messages[idx:]
    s.save(ctx.state)
    return f"[✓] Forked into {ctx.state.session_id} before message {arg}. Original text:\n{msg.content}"


@command("clone")
def clone(arg: str, ctx: HandlerContext) -> str:
    s = _store()
    s.save(ctx.state)
    ctx.state.session_id = uuid.uuid4().hex[:12]
    s.save(ctx.state)
    return f"[✓] Cloned into {ctx.state.session_id}"


@command("import")
def import_session(arg: str, ctx: HandlerContext) -> str:
    if not arg or not Path(arg).is_file():
        raise UsageError("Usage: /import <path to .jsonl session>")
    new = _store().import_file(arg)
    _switch(ctx, new)
    return f"[✓] Imported {len(new.messages)} messages from {arg}"


@command("export")
def export(arg: str, ctx: HandlerContext) -> str:
    s = _store()
    out = Path(arg) if arg else workspace_root() / f"omni-session-{ctx.state.session_id}.html"
    if out.suffix == ".jsonl":
        out.write_text(s.save(ctx.state).read_text(encoding="utf-8"), encoding="utf-8")
    else:
        out.write_text(s.export_html(ctx.state), encoding="utf-8")
    return f"[✓] Exported to {out}"


@command("copy")
def copy(arg: str, ctx: HandlerContext) -> str:
    last = next((m.content for m in reversed(ctx.state.messages) if m.role == "assistant" and m.content), None)
    if not last:
        raise UsageError("[!] No assistant message to copy")
    if sys.platform == "win32":
        cmd, data = ["clip"], last.encode("utf-16le")
    elif sys.platform == "darwin":
        cmd, data = ["pbcopy"], last.encode()
    else:
        cmd, data = ["xclip", "-selection", "clipboard"], last.encode()
    subprocess.run(cmd, input=data, check=True)
    return f"[✓] Copied {len(last)} characters"


@command("share")
def share(arg: str, ctx: HandlerContext) -> str:
    return "[!] /share needs an upload target and is not implemented. Use /export to write HTML you can share yourself."


@command("bug")
def bug(arg: str, ctx: HandlerContext) -> str:
    return (f"Omni bug report (review before sending; sessions may contain secrets)\n  version: see pyproject\n"
            f"  python: {sys.version.split()[0]}  os: {sys.platform}\n  session: {ctx.state.session_id}\n  note: {arg or '-'}")


@command("thinking")
def thinking(arg: str, ctx: HandlerContext) -> str:
    if arg and arg.lower() not in THINKING:
        raise UsageError(f"Usage: /thinking [{'|'.join(THINKING)}]")
    if arg:
        ctx.state.scratchpad["thinking_budget"] = arg.lower()
    return f"Thinking level set to: {ctx.state.scratchpad.get('thinking_budget', 'off').upper()} (stored; provider-side effect depends on the model)"


@command("settings")
def settings(arg: str, ctx: HandlerContext) -> str:
    s = _settings()
    sub, _, rest = arg.partition(" ")
    if sub == "set" and "=" in rest:
        k, v = rest.split("=", 1)
        s[k.strip()] = v.strip()
        _save_settings(s)
        return f"[✓] {k.strip()} = {v.strip()}"
    if sub == "import-pi":
        p = Path.home() / ".pi" / "agent" / "settings.json"
        if not p.is_file():
            raise UsageError(f"[!] {p} not found")
        pi = json.loads(p.read_text(encoding="utf-8"))
        for k in ("defaultProvider", "defaultModel", "defaultThinkingLevel", "theme", "enabledModels"):
            if k in pi:
                s[k] = pi[k]
        _save_settings(s)
        return f"[✓] Imported {', '.join(k for k in s)} from {p}"
    return (f"--- Settings ({_settings_path()}) ---\n" + ("\n".join(f"  {k} = {v}" for k, v in s.items()) or "  (none)") +
            "\nUsage: /settings set key=value | /settings import-pi")


@command("scoped-models")
def scoped_models(arg: str, ctx: HandlerContext) -> str:
    s = _settings()
    if arg:
        s["enabledModels"] = [x.strip() for x in arg.replace(",", " ").split()]
        _save_settings(s)
    return "Models used for cycling: " + (", ".join(s.get("enabledModels", [])) or "(all)") + "\nUsage: /scoped-models <model> <model> ..."


@command("login")
def login(arg: str, ctx: HandlerContext) -> str:
    """/login  (list)  |  /login <provider>  |  /login <provider> <api-key>   (providers: plugins/auth/providers.json)"""
    import getpass
    from ...providers.auth_plugin import AuthPlugin, LoginError
    ap = AuthPlugin(ask=lambda q: getpass.getpass(q), say=print)
    parts = arg.split()
    if not parts:
        return "--- Providers (/login <provider> [api-key]) ---\n" + "\n".join(ap.status())
    if parts and parts[0] == "import":
        from ...providers.oauth import OAuthManager
        got = OAuthManager(ap.kr).import_from_omp()
        return f"[✓] Imported {len(got)} login(s) from Oh My Pi: " + ", ".join(sorted({str(k.get('provider')) for k in got}))
    try:
        return "[✓] " + ap.login(parts[0].lower(), parts[1] if len(parts) > 1 else None)
    except LoginError as e:
        return f"[!] {e}"


@command("logout")
def logout(arg: str, ctx: HandlerContext) -> str:
    from ...providers.keyring import Keyring
    kr = Keyring()
    keys = kr.list_keys(arg or None)
    if not keys:
        return "No stored keys."
    for k in keys:
        kr.remove_key(k["provider"], k["key_id"])
    return f"[✓] Removed {len(keys)} key(s)" + (f" for {arg}" if arg else "")


@command("trust")
def trust(arg: str, ctx: HandlerContext) -> str:
    s = _settings()
    t = set(s.get("trustedProjects", []))
    t.add(str(workspace_root()))
    s["trustedProjects"] = sorted(t)
    _save_settings(s)
    return f"[✓] Trusted {workspace_root()}"


@command("mcp")
def mcp(arg: str, ctx: HandlerContext) -> str:
    reg = ctx.executor.tool_registry
    tools = [t for t in (reg.list_tools() if reg else []) if t.source == "mcp"]
    return "\n".join([f"--- MCP tools ({len(tools)}) ---"] + [f"  {t.name}" for t in tools] +
                     ["Configure servers in plugins/mcp/*.json (mcpServers format); /reload to re-read."])


@command("reload")
def reload(arg: str, ctx: HandlerContext) -> str:
    from ...features.prompt_templates import load_templates
    reg = ctx.executor.tool_registry
    n = 0
    if reg:
        reg.discover_all()
        n = len(reg.list_tools())
    return f"[✓] Reloaded: {n} tools, {len(load_templates())} prompt templates"


@command("hotkeys")
def hotkeys(arg: str, ctx: HandlerContext) -> str:
    return ("Enter send · Alt/Esc+Enter newline · ↑/↓ history · Tab complete · Ctrl+R history search\n"
            "Ctrl+C cancel · Ctrl+D exit · Ctrl+L clear screen · Ctrl+A/E line start/end")


@command("changelog")
def changelog(arg: str, ctx: HandlerContext) -> str:
    f = PROJECT_ROOT / "CHANGELOG.md"
    if f.is_file():
        return f.read_text(encoding="utf-8")[:3000]
    r = subprocess.run(["git", "log", "--oneline", "-15"], cwd=PROJECT_ROOT, capture_output=True, text=True)
    return "Recent changes:\n" + r.stdout


@command("llama")
def llama(arg: str, ctx: HandlerContext) -> str:
    from .subsystems import local
    return local(arg, ctx)


@command("models")
def models(arg: str, ctx: HandlerContext) -> str:
    """List real models from a provider's API: /models [anthropic|openai|google|openrouter|groq|xai|deepseek|mistral]"""
    import json as _json
    import urllib.request
    from ...providers.auth_plugin import load_providers
    from ...providers.keyring import Keyring
    kr, provs = Keyring(), load_providers()
    want = arg.strip().lower()
    if not want:
        names = sorted({k["provider"] for k in kr.list_keys()})
        return "Logged-in providers: " + (", ".join(names) or "none (use /login)") + "\nUsage: /models <provider>  then  /model <provider>/<id>"
    keys = kr.get_active_unblocked_keys(want)
    if not keys:
        return f"[!] No key for '{want}'. Use /login first."
    key = keys[0].get("api_key") or keys[0].get("key")
    base = (provs.get(want, {}).get("base_url") or {"openai": "https://api.openai.com/v1", "anthropic": "https://api.anthropic.com/v1",
            "google": "https://generativelanguage.googleapis.com/v1beta"}.get(want))
    if not base:
        return f"[!] Don't know the models endpoint for '{want}'."
    h = {"User-Agent": "omni"}
    url = base + "/models"
    if want == "anthropic":
        h["anthropic-version"] = "2023-06-01"
        h.update({"Authorization": f"Bearer {key}", "anthropic-beta": "oauth-2025-04-20"} if key.startswith("sk-ant-oat") else {"x-api-key": key})
    elif want == "google":
        url += f"?key={key}"
    else:
        h["Authorization"] = f"Bearer {key}"
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=h), timeout=20) as r:
            data = _json.loads(r.read().decode())
    except Exception as e:
        return f"[!] {want} models request failed: {e}"
    rows = data.get("data") or data.get("models") or []
    ids = sorted((m.get("id") or m.get("name", "")).replace("models/", "") for m in rows)
    return f"--- {want} models ({len(ids)}) ---\n" + "\n".join(f"  {want}/{i}" for i in ids[:60]) + "\nUse: /model <provider>/<id>"
