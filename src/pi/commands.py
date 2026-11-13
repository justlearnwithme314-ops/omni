"""Pi's built-in slash commands (core/slash-commands.ts BUILTIN_SLASH_COMMANDS) implemented against a UI port.

The port is the interactive app (or a headless stand-in) and provides:
  notify(text, level) · select(title, options, initial=None) -> index|None · confirm(title, message) -> bool
  input(title, placeholder="", prefill="") -> str|None · show(title, lines) · set_editor_text(text) · copy(text) -> bool
  quit() · reset_transcript()
Handlers run on a worker thread so dialogs can block.
"""
from __future__ import annotations

import html
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from .ai import THINKING_LEVELS, model_key
from .session import SessionManager

BUILTIN: List[Tuple[str, str, Optional[str]]] = [
    ("settings", "Open settings menu", None),
    ("model", "Select model (opens selector UI)", "<provider/model>"),
    ("tree", "Navigate session tree (switch branches)", None),
    ("thinking", "Set thinking level", "<level>"),
    ("scoped-models", "Enable/disable models for Ctrl+P cycling", None),
    ("export", "Export session (HTML default, or specify path: .html/.jsonl)", None),
    ("import", "Import a JSONL session, preserving its ID and working directory", None),
    ("share", "Share session (Radius artifact, or a private gist with the pi.dev viewer)", None),
    ("mcp", "Inspect MCP servers: status, tools, sign in, reconnect, exposure, enable/disable", "<login|logout|reconnect> <server>"),
    ("bug", "Report a bug", "<description>"),
    ("copy", "Copy last agent message to clipboard", None),
    ("name", "Set session display name", None),
    ("session", "Show session info and stats", None),
    ("changelog", "Show changelog entries", None),
    ("hotkeys", "Show all keyboard shortcuts", None),
    ("fork", "Create a new fork from a previous user message", None),
    ("clone", "Duplicate the current session at the current position", None),
    ("trust", "Save project trust decision for future sessions", None),
    ("login", "Configure provider authentication", "<provider>"),
    ("logout", "Remove provider authentication", None),
    ("new", "Start a new session", None),
    ("compact", "Manually compact the session context", None),
    ("resume", "Resume another session", None),
    ("reload", "Reload keybindings, extensions, skills, prompts, themes, and context files", None),
    ("quit", "Quit omni", None),
]
OMNI_EXTRAS: List[Tuple[str, str, Optional[str]]] = [
    ("extensions", "Show loaded extensions, their tools/commands and compatibility gaps; `slow` ranks them by time", "[slow]"),
    ("debug", "Write session messages and rendered screen to omni-pi-debug.log", None),
    ("theme", "Switch colour theme", "<name>"),
    ("profile", "Switch profile (core … full), or `new` / `edit <name>` / `show <name>` / `delete <name>` / `save <name>` for your own",
     "<name> | new | edit | show | delete | save"),
    ("doctor", "Health check: sign-ins, modules, extensions, plugins, hooks, MCP, and what is slow", None),
    ("plan", "Plan mode: read-only until you approve the model's plan (needs the `plan` capability)", "[on|off]"),
    ("capabilities", "Turn modules on/off (todo, memory, subagents, hosts…) and see what each costs in context",
     "<id> [on|off]"),
    ("commands", "Command sources: pick which extension/prompt/skill a duplicate /command runs, rename prefixes",
     "[list|conflicts|prefer <name> <prefix:name>|prefix <source> <new>|reset]"),
]

HOTKEYS = [
    ("Enter", "Send message (while working: steer the current run)"), ("Alt+Enter", "Queue a follow-up while working"),
    ("Shift+Enter / Ctrl+J", "New line"), ("Escape", "Interrupt the agent; twice on an empty editor opens /tree"),
    ("Ctrl+C", "Clear the editor; twice to quit"), ("Ctrl+D", "Quit (empty editor)"), ("Ctrl+L", "Model selector"),
    ("Ctrl+P / Ctrl+Shift+P", "Cycle models forward / back"), ("Shift+Tab", "Cycle thinking level"),
    ("Ctrl+O", "Expand / collapse tool output"), ("Ctrl+T", "Show / hide thinking blocks"), ("Ctrl+G", "Edit prompt in $EDITOR"),
    ("Ctrl+X", "Copy the last assistant message"), ("Alt+Up", "Return queued messages to the editor"),
    ("Up / Down", "Prompt history (on first / last line)"), ("PageUp / PageDown, mouse wheel", "Scroll the transcript"),
    ("Tab", "Complete commands, @files and paths"), ("! cmd / !! cmd", "Run a shell command (with / without sending output)"),
]


def builtin_names() -> List[str]:
    return [n for n, _, _ in BUILTIN + OMNI_EXTRAS]


def copy_to_clipboard(text: str) -> bool:
    try:
        if os.name == "nt":
            subprocess.run(["clip.exe"], input=text.encode("utf-16-le"), check=True)
            return True
        if sys.platform == "darwin":
            subprocess.run(["pbcopy"], input=text.encode(), check=True)
            return True
        for cmd in (["wl-copy"], ["xclip", "-selection", "clipboard"], ["xsel", "--clipboard", "--input"]):
            if shutil.which(cmd[0]):
                subprocess.run(cmd, input=text.encode(), check=True)
                return True
    except Exception:
        pass
    return False


def last_assistant_text(session) -> str:
    for e in reversed(session.sessions.get_branch()):
        if e.get("type") == "message" and e["message"].get("role") == "assistant":
            t = "".join(b.get("text", "") for b in e["message"].get("content") or [] if b.get("type") == "text")
            if t.strip():
                return t
    return ""


def export_html(session, path: str) -> None:
    rows = []
    for e in session.sessions.get_branch():
        if e.get("type") != "message":
            continue
        m = e["message"]
        role = m.get("role")
        if role == "user":
            c = m.get("content")
            body = c if isinstance(c, str) else "".join(b.get("text", "") for b in c if b.get("type") == "text")
            rows.append(f'<div class="msg user"><div class="who">you</div><pre>{html.escape(body)}</pre></div>')
        elif role == "assistant":
            parts = []
            for b in m.get("content") or []:
                if b.get("type") == "text":
                    parts.append(f"<pre>{html.escape(b['text'])}</pre>")
                elif b.get("type") == "thinking":
                    parts.append(f'<details><summary>thinking</summary><pre>{html.escape(b["thinking"])}</pre></details>')
                elif b.get("type") == "toolCall":
                    parts.append(f'<div class="tool">⚙ {html.escape(b["name"])} <code>{html.escape(json.dumps(b.get("arguments"))[:400])}</code></div>')
            if m.get("errorMessage"):
                parts.append(f'<div class="err">{html.escape(m["errorMessage"])}</div>')
            rows.append(f'<div class="msg assistant"><div class="who">{html.escape(str(m.get("model")))}</div>{"".join(parts)}</div>')
        elif role == "toolResult":
            t = "".join(b.get("text", "") for b in m.get("content") or [] if b.get("type") == "text")
            rows.append(f'<details class="msg result{" error" if m.get("isError") else ""}"><summary>{html.escape(m.get("toolName", ""))} '
                        f'result</summary><pre>{html.escape(t[:20000])}</pre></details>')
        elif role == "bashExecution":
            rows.append(f'<div class="msg bash"><div class="who">$ {html.escape(m.get("command", ""))}</div><pre>{html.escape(m.get("output", ""))}</pre></div>')
    name = session.sessions.session_name() or session.sessions.session_id
    doc = f"""<!DOCTYPE html><html><head><meta charset="utf-8"><title>{html.escape(name)}</title><style>
body{{background:#0f1220;color:#d5d9f0;font:14px/1.5 ui-sans-serif,system-ui;max-width:960px;margin:40px auto;padding:0 20px}}
h1{{font-size:18px;background:linear-gradient(90deg,#7aa2ff,#e27df5);-webkit-background-clip:text;color:transparent}}
.msg{{border:1px solid #2a2f45;border-radius:10px;padding:10px 14px;margin:12px 0;background:#141828}}
.user{{border-left:3px solid #7aa2ff;background:#1c2033}} .who{{font-size:12px;color:#8089b3;margin-bottom:6px}}
pre{{white-space:pre-wrap;word-break:break-word;margin:0;font:13px/1.45 ui-monospace,Consolas,monospace}}
.tool{{color:#a9b8ff;font:12px ui-monospace,monospace;margin:6px 0}} .err{{color:#ff6b81}} .result{{background:#111421}}
.error{{border-color:#ff6b81}} .bash .who{{color:#e0af68}} summary{{cursor:pointer;color:#8089b3}}
</style></head><body><h1>{html.escape(name)}</h1><div style="color:#565f89">{html.escape(session.cwd)} · exported {time.ctime()}</div>
{''.join(rows)}</body></html>"""
    Path(path).write_text(doc, encoding="utf-8")


class PortInteraction:
    """Adapts the UI port to `ai.oauth.Interaction`: browser URL, notices, pickers and the paste-the-code prompt."""

    def __init__(self, port):
        self.port = port

    def notify(self, text: str, level: str = "info") -> None:
        self.port.notify(text, level)

    def open_url(self, url: str) -> None:
        try:
            import webbrowser
            webbrowser.open(url)
        except Exception:
            pass

    def select(self, title: str, options: List[str]) -> Optional[int]:
        return self.port.select(title, options)

    def manual(self, message: str, placeholder: str, cancel=None) -> Optional[str]:
        fn = getattr(self.port, "input_cancellable", None)
        if fn is not None:  # closes itself when the browser callback arrives first
            return fn(message, placeholder, cancel)
        if cancel is not None:  # a port without a closable prompt: rely on the browser callback only
            cancel.wait()
            return None
        return self.port.input(message, placeholder=placeholder)


class Commands:
    def __init__(self, session, port):
        self.s = session
        self.port = port

    def sources(self):
        from .command_sources import CommandSources
        return CommandSources(self.s)

    def all_commands(self) -> List[Dict[str, Any]]:
        """Every command with its source; a name several sources provide is listed once per source."""
        return [{"name": e.name, "description": e.description, "hint": e.hint,
                 "source": "builtin" if e.kind in ("builtin", "omni") else e.kind,
                 "prefix": e.prefix, "qualified": e.qualified, "origin": e.origin}
                for e in self.sources().entries()]

    def is_builtin(self, name: str) -> bool:
        return name in builtin_names()

    def run(self, name: str, args: str) -> bool:
        fn: Optional[Callable[[str], None]] = getattr(self, "cmd_" + name.replace("-", "_"), None)
        if fn is None:
            spec = self.s.caps.commands().get(name) if hasattr(self.s, "caps") else None
            if spec is None:
                return False
            try:  # a command registered by a capability (e.g. a Hermes plugin's /command)
                out = spec[1](args.strip())
            except Exception as e:
                self.port.notify(f"/{name} failed: {type(e).__name__}: {e}", "error")
                return True
            if out:
                self.port.notify(str(out), "info")
            return True
        fn(args.strip())
        return True

    # ------------------------------------------------------------------------------------------ models
    def cmd_model(self, args: str) -> None:
        s = self.s
        if args:
            m = s.registry.resolve(args)
            if not m:
                self.port.notify(f"Unknown model: {args}", "error")
                return
            s.set_model(m)
            self.port.notify(f"Model: {model_key(m)}", "success")
            return
        # Two steps, so every provider is visible at once (a flat list buries small providers under big ones):
        # 1) providers - current first, then signed-in, then the rest;  2) that provider's models.  Esc goes back.
        from .ai import oauth
        by_provider: Dict[str, List[Dict[str, Any]]] = {}
        for m in s.registry.all():
            by_provider.setdefault(m["provider"], []).append(m)
        authed = {p: s.auth.has_auth(p) for p in by_provider}  # once per provider, not per row
        cur = s.model["provider"] if s.model else None
        providers = sorted(by_provider, key=lambda p: (p != cur, not authed[p], p == "faux", p))

        def provider_label(p: str) -> str:
            n = len(by_provider[p])
            o = oauth.PROVIDERS.get(p)
            kind = (f"{o.name}" if o else "") + (" · extension" if o is not None and o.source == "extension" else "")
            state = "signed in" if authed[p] else f"not signed in: /login {p}"
            return (("● " if p == cur else "  ") + f"{p}   {n} model{'s' if n != 1 else ''} · {state}" +
                    (f" · {kind}" if kind else ""))

        pick_provider = providers.index(cur) if cur in providers else 0
        while True:
            n_ok = sum(len(by_provider[p]) for p in providers if authed[p])
            pi = self.port.select(f"Select provider ({len(providers)} providers · {n_ok} models available)",
                                  [provider_label(p) for p in providers], initial=pick_provider)
            if pi is None:
                return
            pick_provider = pi
            prov = providers[pi]
            models = sorted(by_provider[prov], key=lambda m: m.get("name") or m["id"])
            labels = [("● " if model_key(m) == model_key(s.model) else "  ") + m["id"] +
                      (f"   {m['name']}" if m.get("name") and m["name"] != m["id"] else "") for m in models]
            labels.append("  ← back to providers")
            title = f"{prov} models" + ("" if authed[prov] else f" (not signed in: /login {prov})")
            mi = self.port.select(title, labels, initial=next((i for i, m in enumerate(models)
                                                               if model_key(m) == model_key(s.model)), 0))
            if mi is None or mi >= len(models):
                continue  # esc / back -> provider list
            chosen = models[mi]
            break
        if not authed[chosen["provider"]]:
            self.port.notify(f"{model_key(chosen)} has no credentials yet; run /login {chosen['provider']}.", "warning")
        s.set_model(chosen)
        self.port.notify(f"Model: {model_key(chosen)}", "success")

    def cmd_thinking(self, args: str) -> None:
        s = self.s
        if not s.model or not s.model.get("reasoning"):
            self.port.notify("Current model does not support thinking levels.", "warning")
            return
        level = args
        if not level:
            idx = self.port.select("Thinking level", THINKING_LEVELS, initial=THINKING_LEVELS.index(s.thinking_level))
            if idx is None:
                return
            level = THINKING_LEVELS[idx]
        if level not in THINKING_LEVELS:
            self.port.notify(f"Unknown level '{level}'. Use one of: {', '.join(THINKING_LEVELS)}", "error")
            return
        s.set_thinking_level(level)
        self.port.notify(f"Thinking: {s.thinking_level}", "success")

    def cmd_scoped_models(self, args: str) -> None:
        s = self.s
        enabled = set(s.settings.get("enabledModels") or [])
        models = [m for m in s.registry.all() if s.auth.has_auth(m["provider"]) and m["provider"] != "faux"]
        while True:
            labels = [("[x] " if model_key(m) in enabled else "[ ] ") + model_key(m) for m in models] + ["✓ done"]
            idx = self.port.select("Models for Ctrl+P cycling (enter toggles)", labels)
            if idx is None or idx == len(models):
                break
            k = model_key(models[idx])
            enabled.symmetric_difference_update({k})
        s.settings.set_global("enabledModels", sorted(enabled))
        s.scoped_models = [x for x in (s.registry.resolve(p) for p in sorted(enabled)) if x]
        self.port.notify(f"{len(enabled)} model(s) in the cycle.", "success")

    API_KEY_PROVIDERS = ["anthropic", "openai", "google", "openrouter", "groq", "xai", "deepseek", "mistral", "cerebras", "zai"]

    def cmd_login(self, args: str) -> None:
        """Pi's /login: subscription sign-in (OAuth) or an API key, plus importing OMP's OAuth sessions."""
        from .ai import oauth
        s = self.s
        prov = args.strip()
        method = None  # "oauth" | "key"
        if not prov:
            rows: List[Tuple[str, str]] = []
            labels: List[str] = []
            for pid, p in oauth.PROVIDERS.items():
                why = p.available()
                rows.append(("oauth", pid))
                labels.append(f"Sign in: {p.name}" + (f"   [{pid}, from an extension]" if p.source == "extension" else "") +
                          (f"   (unavailable: {why})" if why else ""))
            for pid in self.API_KEY_PROVIDERS:
                rows.append(("key", pid))
                labels.append(f"API key: {pid}")
            rows.append(("import", ""))
            labels.append("Import OAuth sessions from Oh My Pi (~/.omp)")
            idx = self.port.select("Login: subscription sign-in (OAuth) or API key", labels)
            if idx is None:
                return
            method, prov = rows[idx]
        if method == "import" or prov.startswith("import"):
            try:
                from ..providers.keyring import Keyring  # type: ignore
                from ..providers.oauth import OAuthManager  # type: ignore
                got = OAuthManager(Keyring()).import_from_omp()
                self.port.notify(f"Imported {len(got)} OAuth credential(s) from OMP into Omni's keyring." if got else
                                 "No OAuth credentials found in ~/.omp/agent/agent.db", "success" if got else "warning")
            except Exception as e:
                self.port.notify(f"Import failed: {e}", "error")
            return
        if method is None:
            has_oauth, has_key = prov in oauth.PROVIDERS, prov in self.API_KEY_PROVIDERS
            if has_oauth and has_key:
                idx = self.port.select(f"Login to {prov}", [f"Sign in with your subscription ({oauth.PROVIDERS[prov].name})",
                                                            "Paste an API key"])
                if idx is None:
                    return
                method = "oauth" if idx == 0 else "key"
            else:
                method = "oauth" if has_oauth else "key"
        if method == "oauth":
            self._oauth_login(prov)
            return
        key = self.port.input(f"API key for {prov}", placeholder="paste key (stored in auth.json)", secret=True)
        if key:
            s.auth.set_api_key(prov, key.strip())
            self.port.notify(f"Saved {prov} key to {s.auth.path}", "success")
            self._pick_model_for(prov)

    def _oauth_login(self, prov: str) -> None:
        from .ai import oauth
        try:
            cred = oauth.login(prov, PortInteraction(self.port))
        except oauth.LoginCancelled:
            self.port.notify("Login cancelled.", "info")
            return
        except Exception as e:
            self.port.notify(f"{prov} sign-in failed: {e}", "error")
            return
        self.s.auth.set_oauth(prov, cred)
        who = cred.get("email") or cred.get("orgName") or ""
        self.port.notify(f"Signed in to {oauth.PROVIDERS[prov].name}" + (f" as {who}" if who else "") +
                         f". Token saved to {self.s.auth.path} (refreshed automatically).", "success")
        self._pick_model_for(prov)

    def _pick_model_for(self, prov: str) -> None:
        s = self.s
        if not s.model or not s.auth.has_auth(s.model["provider"]):
            cands = [m for m in s.registry.all() if m["provider"] == prov]
            if cands:
                s.set_model(cands[0])
                self.port.notify(f"Model: {model_key(cands[0])}", "info")

    def cmd_logout(self, args: str) -> None:
        s = self.s
        stored = s.auth.stored_providers()
        if not stored:
            self.port.notify("No credentials stored in auth.json.", "info")
            return
        prov = args if args in stored else None
        if prov is None:
            kinds = {p: (s.auth.entry(p) or {}).get("type") for p in stored}
            labels = [p + ("   (OAuth sign-in)" if kinds[p] == "oauth" else "   (API key)") for p in stored]
            idx = self.port.select("Remove credentials for", labels)
            if idx is None:
                return
            prov = stored[idx]
        s.auth.remove(prov)
        self.port.notify(f"Removed {prov}.", "success")

    # ---------------------------------------------------------------------------------------- sessions
    def cmd_new(self, args: str) -> None:
        if self.s.new_session():
            self.port.reset_transcript()
            self.port.notify("New session started.", "success")

    def cmd_resume(self, args: str) -> None:
        s = self.s
        sessions = SessionManager.list_sessions(s.sessions.session_dir)
        if args == "all" or not sessions:
            sessions = SessionManager.list_all_sessions(s.agent_dir)
        sessions = [x for x in sessions if x["path"] != str(s.sessions.file)]
        if not sessions:
            self.port.notify("No other sessions found.", "info")
            return
        labels = []
        for x in sessions[:200]:
            when = time.strftime("%b %d %H:%M", time.localtime(x["mtime"]))
            title = x["name"] or x["firstMessage"] or "(empty)"
            labels.append(f"{when}  {title[:70]}  · {x['messageCount']} msgs")
        idx = self.port.select("Resume session", labels)
        if idx is not None and s.resume(sessions[idx]["path"]):
            self.port.reset_transcript()
            self.port.notify("Session resumed.", "success")

    def cmd_name(self, args: str) -> None:
        if not args:
            self.port.notify(f"Session name: {self.s.sessions.session_name() or '(unnamed)'}", "info")
            return
        self.s.set_session_name(args)
        self.port.notify(f"Session named '{args}'.", "success")

    def cmd_session(self, args: str) -> None:
        s = self.s
        t = s.totals()
        u = s.context_usage()
        lines = [f"file      {s.sessions.file or '(not persisted)'}", f"id        {s.sessions.session_id}",
                 f"name      {s.sessions.session_name() or '(unnamed)'}", f"cwd       {s.cwd}",
                 f"model     {model_key(s.model)}  · thinking {s.thinking_level}", f"messages  {t['messages']}  · tool calls {t['toolCalls']}",
                 f"tokens    in {t['input']:,} · out {t['output']:,} · cache read {t['cacheRead']:,} · cache write {t['cacheWrite']:,}",
                 f"cost      ${t['cost']:.4f}",
                 f"context   {u['tokens']:,} / {u['contextWindow']:,}" + (f" ({u['percent']:.1f}%)" if u.get("percent") is not None else "")]
        self.port.show("session", lines)

    def cmd_tree(self, args: str) -> None:
        s = self.s
        rows = [r for r in s.sessions.tree_rows() if r["entry"]["type"] in ("message", "branch_summary", "compaction")
                and (r["entry"]["type"] != "message" or r["entry"]["message"].get("role") in ("user", "assistant"))]
        if not rows:
            self.port.notify("Session tree is empty.", "info")
            return
        labels = []
        for r in rows:
            e = r["entry"]
            if e["type"] == "message":
                m = e["message"]
                c = m.get("content")
                txt = c if isinstance(c, str) else " ".join(b.get("text", "") or (f"⚙ {b.get('name')}" if b.get("type") == "toolCall" else "")
                                                             for b in c or [])
                prefix = "you  " if m["role"] == "user" else "  ↳ "
            else:
                txt, prefix = e.get("summary", "")[:60], "◇ "
            mark = "● " if e["id"] == s.sessions.leaf_id else ("│ " if r["onPath"] else "  ")
            lab = f" [{r['label']}]" if r.get("label") else ""
            labels.append(f"{mark}{'  ' * r['depth']}{prefix}{' '.join(str(txt).split())[:90]}{lab}")
        idx = self.port.select("Session tree (enter to continue from there)", labels,
                               initial=next((i for i, r in enumerate(rows) if r["entry"]["id"] == s.sessions.leaf_id), len(rows) - 1))
        if idx is None:
            return
        target = rows[idx]["entry"]
        summarize = False
        if not s.settings.get("branchSummary.skipPrompt", False) and target["id"] not in {e["id"] for e in s.sessions.get_branch()}:
            summarize = self.port.confirm("Branch summary", "Summarize the branch you are leaving?")
        prefill = ""
        if target["type"] == "message" and target["message"].get("role") == "user":
            c = target["message"].get("content")
            prefill = c if isinstance(c, str) else "".join(b.get("text", "") for b in c or [] if b.get("type") == "text")
        s.navigate_tree(target["id"], summarize=summarize)
        self.port.reset_transcript()
        if prefill:
            self.port.set_editor_text(prefill)

    def cmd_fork(self, args: str) -> None:
        s = self.s
        users = [e for e in s.sessions.get_branch() if e["type"] == "message" and e["message"].get("role") == "user"]
        if not users:
            self.port.notify("No user messages to fork from.", "info")
            return
        def txt(e):
            c = e["message"].get("content")
            return c if isinstance(c, str) else "".join(b.get("text", "") for b in c or [] if b.get("type") == "text")
        idx = self.port.select("Fork from message", [" ".join(txt(e).split())[:100] for e in users], initial=len(users) - 1)
        if idx is None:
            return
        text = txt(users[idx])
        if s.fork(users[idx]["id"], "before"):
            self.port.reset_transcript()
            self.port.set_editor_text(text)
            self.port.notify("Forked into a new session.", "success")

    def cmd_clone(self, args: str) -> None:
        if self.s.clone():
            self.port.reset_transcript()
            self.port.notify("Session cloned.", "success")

    def cmd_compact(self, args: str) -> None:
        self.port.notify("Compacting…", "info")
        e = self.s.compact(args or None)
        if e:
            self.port.reset_transcript()
            self.port.notify(f"Compacted {e['tokensBefore']:,} tokens.", "success")

    def cmd_export(self, args: str) -> None:
        s = self.s
        path = args or os.path.join(s.cwd, f"pi-session-{s.sessions.session_id[:8]}.html")
        if path.endswith(".jsonl"):
            s.sessions.export_jsonl(path)
        else:
            export_html(s, path)
        self.port.notify(f"Exported to {path}", "success")

    def cmd_import(self, args: str) -> None:
        if not args or not os.path.isfile(os.path.expanduser(args)):
            self.port.notify("Usage: /import <path-to-session.jsonl>", "error")
            return
        src = Path(os.path.expanduser(args))
        sm = SessionManager(self.s.cwd, self.s.agent_dir)
        sm.open(str(src))
        dest_dir = SessionManager(sm.cwd, self.s.agent_dir).session_dir
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / src.name
        if not dest.exists():
            shutil.copy2(src, dest)
        if self.s.resume(str(dest)):
            self.port.reset_transcript()
            self.port.notify(f"Imported {src.name}", "success")

    def cmd_share(self, args: str) -> None:
        """Pi's /share: a Radius artifact when Radius auth is configured, else a private GitHub gist shown through the
        pi.dev session viewer. `sessionShareDestination` (auto | gist | radius) forces one destination."""
        from .share import share_session
        if not self.port.confirm("Share session", "Upload this session? It can contain prompts, tool output and file contents."):
            return
        self.port.notify("Uploading session...", "info")
        ok, text = share_session(self.s)
        self.port.notify(text, "success" if ok else "error")

    def cmd_mcp(self, args: str) -> None:
        mgr = self.s.mcp
        if mgr is None:
            self.port.notify("MCP is off for this session (--no-mcp, the profile, or no tools).", "info")
            return
        parts = args.split()
        if parts and parts[0] in ("login", "logout", "reconnect") and len(parts) > 1:
            return self._mcp_action(parts[0], parts[1])
        if not mgr.servers:
            self.port.show("mcp", ["No MCP servers configured.", "", "Add one: omni mcp add <name> -- <command> [args]",
                                   "     or: omni mcp add <name> --url https://host/mcp", "then /reload."] + [f"! {e}" for e in mgr.errors])
            return
        if not getattr(self.port, "interactive", True):
            self.port.show("mcp", mgr.status_lines())
            return
        names = [s.name for s in sorted(mgr.servers.values(), key=lambda x: x.name)]
        labels = [l for l in mgr.status_lines() if not l.startswith("    ")][:len(names)]
        order = [l.split()[0] for l in labels]
        idx = self.port.select("MCP servers", labels + ["✓ done"])
        if idx is None or idx >= len(order):
            return
        srv = mgr.servers[order[idx]]
        actions = ["Tools", "Reconnect", "Sign in", "Sign out", "Disable" if srv.cfg.get("enabled", True) is not False else "Enable",
                   "Exposure: codemode", "Exposure: deferred", "Exposure: direct", "Exposure: hidden", "Details"]
        a = self.port.select(f"{srv.name} · {srv.state}", actions)
        if a is None:
            return
        act = actions[a]
        if act == "Tools":
            self.port.show(f"{srv.name} tools", [f"{t['name']:<36} [{srv.tool_exposure(t['name'])}] "
                                                  f"{(t.get('description') or '').strip().splitlines()[0][:80] if (t.get('description') or '').strip() else ''}"
                                                  for t in srv.tools] or ["(no tools)"])
        elif act == "Details":
            self.port.show(srv.name, [f"state      {srv.state}", f"source     {srv.cfg.get('_source')}", f"exposure   {srv.exposure}",
                                      f"transport  {'stdio: ' + str(srv.cfg.get('command')) if srv.cfg.get('command') else 'http: ' + str(srv.cfg.get('url'))}",
                                      ""] + (srv.error or "").split("\n"))
        elif act in ("Reconnect", "Sign in", "Sign out"):
            self._mcp_action({"Reconnect": "reconnect", "Sign in": "login", "Sign out": "logout"}[act], srv.name)
        else:
            fields = {"enabled": act == "Enable"} if act in ("Enable", "Disable") else {"exposure": act.split(": ")[1]}
            src = srv.cfg.get("_source", "")
            if src and src not in ("extension",) and os.path.isfile(src.split(" + ")[-1]):
                from .mcp import update_server_fields
                update_server_fields(src.split(" + ")[-1], srv.name, fields)
            else:
                srv.cfg.update(fields)
            mgr.load()
            mgr.start()
            self.port.notify(f"{srv.name}: {', '.join(f'{k}={v}' for k, v in fields.items())}", "success")

    def _mcp_action(self, action: str, name: str) -> None:
        mgr = self.s.mcp
        srv = mgr.servers.get(name) if mgr else None
        if srv is None:
            self.port.notify(f"Unknown MCP server: {name}", "error")
            return
        from . import mcp_oauth
        if action == "logout":
            mcp_oauth.logout(self.s.agent_dir, name)
            self.port.notify(f"Signed out of {name}", "success")
            return
        if action == "login":
            if not srv.cfg.get("url"):
                self.port.notify(f"{name} is a stdio server; there is nothing to sign in to.", "warning")
                return
            try:
                mcp_oauth.login(self.s.agent_dir, name, srv.cfg, notify=lambda t: self.port.notify(t, "info"))
            except Exception as e:
                self.port.notify(f"Sign-in failed: {e}", "error")
                return
        srv.close()
        srv.connect()
        self.port.notify(f"{name}: {srv.state}" + (f" — {srv.error.splitlines()[0]}" if srv.error else ""),
                         "success" if srv.state == "connected" else "error")

    def cmd_copy(self, args: str) -> None:
        t = last_assistant_text(self.s)
        if not t:
            self.port.notify("Nothing to copy yet.", "info")
            return
        self.port.notify("Copied last message." if self.port.copy(t) else "No clipboard tool available.", "success")

    def cmd_trust(self, args: str) -> None:
        ok = self.port.confirm("Project trust", f"Trust {self.s.cwd}? Trusted projects load .pi/ settings, extensions, skills and prompts.")
        self.s.trust_store.save(self.s.cwd, ok)
        self.port.notify(f"Saved: {'trusted' if ok else 'not trusted'}. Run /reload to apply.", "success")

    def cmd_reload(self, args: str) -> None:
        self.s.reload()
        self.port.notify(f"Reloaded · {len(self.s.skills)} skills · {len(self.s.templates)} prompts · "
                         f"{len(getattr(self.s.ext, 'extensions', []))} extensions", "success")

    def cmd_settings(self, args: str) -> None:
        s = self.s
        from .tui.theme import THEMES
        options = [
            ("theme", sorted(k for k in THEMES if k not in ("dark", "system"))),
            ("defaultThinkingLevel", THINKING_LEVELS),
            ("hideThinkingBlock", [False, True]), ("quietStartup", [False, True]),
            ("steeringMode", ["one-at-a-time", "all"]), ("followUpMode", ["one-at-a-time", "all"]),
            ("doubleEscapeAction", ["tree", "fork", "none"]), ("defaultProjectTrust", ["ask", "always", "never"]),
            ("compaction.enabled", [True, False]), ("enableSkillCommands", [True, False]),
        ]
        while True:
            labels = [f"{k:<22} {s.settings.get(k)}" for k, _ in options] + ["✓ done"]
            idx = self.port.select("Settings (saved to ~/.pi/agent/settings.json)", labels)
            if idx is None or idx == len(options):
                return
            key, values = options[idx]
            vi = self.port.select(key, [str(v) for v in values])
            if vi is not None:
                s.settings.set_global(key, values[vi])
                if key == "theme":
                    self.port.apply_theme(values[vi])

    def cmd_theme(self, args: str) -> None:
        from .tui.theme import THEMES
        names = sorted(k for k in THEMES if k not in ("dark", "system"))
        name = args
        if not name:
            idx = self.port.select("Theme", names)
            if idx is None:
                return
            name = names[idx]
        if name not in THEMES:
            self.port.notify(f"Unknown theme {name}", "error")
            return
        self.s.settings.set_global("theme", name)
        self.port.apply_theme(name)

    def cmd_hotkeys(self, args: str) -> None:
        lines = [f"{k:<34} {v}" for k, v in HOTKEYS]
        for key, sc in getattr(self.s.ext, "shortcuts", {}).items():
            lines.append(f"{key:<34} {sc.get('description') or 'extension shortcut'}")
        self.port.show("hotkeys", lines)

    def cmd_changelog(self, args: str) -> None:
        self.port.show("changelog", [
            "omni pi profile",
            "• Pi-compatible runtime: read/bash/edit/write/grep/find/ls with Pi's schemas and messages",
            "• JSONL v3 session tree: /tree /fork /clone /resume /import /export, compaction and branch summaries",
            "• AGENTS.md/CLAUDE.md, SYSTEM.md/APPEND_SYSTEM.md, skills (/skill:name), prompt templates",
            "• Node extension host running real Pi/OMP extensions (tools, commands, events, ctx.ui, renderers)",
            "• Full-screen terminal UI with themes",
        ])

    def cmd_bug(self, args: str) -> None:
        path = Path(self.s.agent_dir) / "omni-pi-bug.json"
        path.write_text(json.dumps({"description": args, "session": str(self.s.sessions.file), "model": model_key(self.s.model),
                                    "diagnostics": self.s.diagnostics, "time": time.ctime()}, indent=2), encoding="utf-8")
        self.port.notify(f"Bug details written to {path}. Attach it to an issue in your omni repository.", "success")

    def cmd_debug(self, args: str) -> None:
        path = Path(self.s.agent_dir) / "omni-pi-debug.log"
        data = {"messages": self.s.sessions.build_context()["messages"], "screen": self.port.screen_dump(),
                "diagnostics": self.s.diagnostics, "systemPrompt": self.s.system_prompt()}
        path.write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")
        self.port.notify(f"Debug log written to {path}", "success")

    def cmd_extensions(self, args: str) -> None:
        exts = self.s.ext.report() if hasattr(self.s.ext, "report") else []
        if not exts:
            self.port.show("extensions", ["No extensions loaded.", "", "Discovery: ~/.pi/agent/extensions, .pi/extensions (trusted),",
                                          "settings.json \"extensions\", -e <path>, and Omni's plugins/pi folder."] + self.s.diagnostics[-5:])
            return
        lines = []
        if args.strip() in ("slow", "time", "timing"):
            # where startup and per-turn time goes: load time plus the time spent in each extension's event handlers
            rows = sorted(exts, key=lambda e: -(e.get("loadMs", 0) + sum((e.get("handlerMs") or {}).values())))
            total_load = sum(e.get("loadMs", 0) for e in exts)
            lines.append(f"{len(exts)} extensions · {total_load} ms to load · last background startup "
                         f"{getattr(self.s, 'startup_seconds', 0.0):.1f} s")
            lines.append("")
            for e in rows[:25]:
                hm = e.get("handlerMs") or {}
                worst = ", ".join(f"{k} {v} ms" for k, v in sorted(hm.items(), key=lambda kv: -kv[1])[:3] if v >= 5)
                lines.append(f"{e.get('loadMs', 0):>6} ms load   {e['name']}" + (f"   handlers: {worst}" if worst else ""))
            lines += ["", "Handler time is paid on every matching event (session_start once, before_agent_start per message).",
                      "Disable an extension you do not need in settings.json, or run with -ne for none."]
            self.port.show("extensions", lines)
            return
        for e in exts:
            status = "✓" if e.get("ok") else "✗"
            lines.append(f"{status} {e['name']}  ({e['path']})" + (f"  · {e['loadMs']} ms" if e.get("loadMs", 0) >= 50 else ""))
            if e.get("error"):
                lines.append(f"    error: {e['error']}")
            if e.get("tools"):
                lines.append(f"    tools: {', '.join(e['tools'])}")
            if e.get("commands"):
                lines.append(f"    commands: {', '.join('/' + c for c in e['commands'])}")
            if e.get("events"):
                lines.append(f"    events: {', '.join(e['events'])}")
            if e.get("missing"):
                lines.append(f"    not yet supported: {', '.join(sorted(e['missing']))[:200]}")
        self.port.show("extensions", lines)

    def cmd_profile(self, args: str) -> None:
        from .profiles import canonical, list_profiles, save_profile, set_active_profile
        profiles = list_profiles()
        names = [p["name"] for p in profiles]
        if args.strip().lower().startswith("save"):
            new = args.strip()[4:].strip() or (self.port.input("Name for the new profile", placeholder="my-setup") or "").strip()
            if not new:
                return
            s = self.s
            switches = {k: s.profile[k] for k in ("pi_extensions", "omni_plugins_pi", "permission_gate", "mcp", "omni_registry")
                        if k in s.profile and s.profile.get(k) != s.base_profile.get(k)}
            try:
                path = save_profile(new, s.base_profile["name"], list(s.profile.get("capabilities") or []),
                                    f"Custom: {s.base_profile['name']} with {len(s.caps.enabled)} module(s).", switches)
            except ValueError as e:
                self.port.notify(str(e), "error")
                return
            self.port.notify(f"Saved profile '{new.lower()}' to {path}. Switch with /profile {new.lower()}.", "success")
            return
        word = args.strip().split(" ", 1)[0].lower()
        rest = args.strip().split(" ", 1)[1].strip() if " " in args.strip() else ""
        if word in ("new", "create", "edit"):
            self._profile_wizard(rest, edit=word == "edit")
            return
        if word in ("delete", "remove"):
            from .profiles import delete_profile
            try:
                delete_profile(rest)
            except ValueError as e:
                self.port.notify(str(e), "error")
                return
            self.port.notify(f"Deleted custom profile '{rest.lower()}'.", "success")
            return
        if word == "show":
            from .profiles import describe_profile
            self.port.show("profile", describe_profile(rest or self.s.profile.get("name")))
            return
        name = canonical(args.strip()) if args.strip() else ""
        if not name:
            cur = self.s.profile.get("name")
            labels = [("● " if p["name"] == cur else "  ") + f"{p['name']:<11} {p.get('description', '')}" for p in profiles]
            idx = self.port.select("Profile (saved as the default for `omni`)", labels,
                                   initial=names.index(cur) if cur in names else 0)
            if idx is None:
                return
            name = names[idx]
        if name not in names:
            self.port.notify(f"Unknown profile '{name}'. Available: {', '.join(names)}", "error")
            return
        self.port.notify(f"Switching to {name}…", "info")
        self.s.switch_profile(name)
        set_active_profile(name)
        self.port.reset_transcript()
        self.port.notify(f"Profile: {name} · {len(self.s.active_tool_names)} tools · "
                         f"{len(self.s.caps.enabled)} module(s): {', '.join(self.s.caps.enabled) or 'none'}", "success")

    def _profile_wizard(self, name: str, edit: bool = False) -> None:
        """Build a custom profile step by step: base, modules (with what each adds), name. Nothing changes until saved."""
        from .caps import load_builtin
        from .profiles import BUILTIN, PROJECT_ROOT, canonical, list_profiles, load_profile, save_profile, user_profiles_dir
        profiles = list_profiles()
        caps = load_builtin()
        existing = None
        if edit:
            name = canonical(name) if name else str(self.s.base_profile.get("name"))
            f = user_profiles_dir() / f"{name}.json"
            if not f.is_file():
                self.port.notify(f"'{name}' is not a custom profile (built-in profiles cannot be edited; use /profile new).", "error")
                return
            existing = load_profile(name)
            try:
                base = json.loads(f.read_text(encoding="utf-8")).get("extends") or "core"
            except (OSError, ValueError):
                base = "core"
        else:
            choices = [p for p in profiles if p["name"] in BUILTIN or (PROJECT_ROOT / "profiles" / f"{p['name']}.json").is_file()]
            labels = [f"{p['name']:<11} {len(p.get('capabilities') or [])} module(s) — {p.get('description', '')}" for p in choices]
            idx = self.port.select("New profile — 1/3: start from which profile?", labels)
            if idx is None:
                return
            base = choices[idx]["name"]
        base_prof = load_profile(base)
        chosen = list((existing or base_prof).get("capabilities") or [])
        switches = {c.id: c.is_on(existing or base_prof) for c in caps.values() if c.is_on is not None}
        base_switch = {c.id: c.is_on(base_prof) for c in caps.values() if c.is_on is not None}
        order = sorted(caps.values(), key=lambda c: (c.is_on is not None, c.group != "feature", c.id))
        while True:
            labels = []
            for c in order:
                on = switches[c.id] if c.is_on is not None else c.id in chosen
                labels.append(f"{'[x]' if on else '[ ]'} {c.id:<19} {c.description}" + (f"  ({c.inspired_by})" if c.inspired_by else ""))
            labels.append(f"✓ continue with {len(chosen)} module(s)")
            idx = self.port.select(f"{'Edit' if edit else 'New'} profile — 2/3: modules on top of {base} (enter toggles)", labels)
            if idx is None:
                return
            if idx >= len(order):
                break
            c = order[idx]
            if c.is_on is not None:
                switches[c.id] = not switches[c.id]
            elif c.id in chosen:
                chosen.remove(c.id)
                dependents = [d.id for d in caps.values() if c.id in d.requires and d.id in chosen]
                for d in dependents:  # modules that build on this one go with it
                    chosen.remove(d)
                if dependents:
                    self.port.notify(f"Also removed {', '.join(dependents)} (needs {c.id}).", "info")
            else:
                chosen.append(c.id)
                added = []
                for r in c.requires:  # bring in what it builds on
                    rc = caps.get(r)
                    if rc is None:
                        continue
                    if rc.is_on is not None and not switches[r]:
                        switches[r] = True
                        added.append(r)
                    elif rc.is_on is None and r not in chosen:
                        chosen.append(r)
                        added.append(r)
                if added:
                    self.port.notify(f"Also turned on {', '.join(added)} (needed by {c.id}).", "info")
        if not edit:
            name = (name or self.port.input("New profile — 3/3: name", placeholder="my-setup") or "").strip()
            if not name:
                return
        extra: Dict[str, Any] = {}
        scratch = json.loads(json.dumps(base_prof))
        for c in caps.values():
            if c.is_on is not None and switches[c.id] != base_switch[c.id]:
                before = json.dumps(scratch, sort_keys=True)
                c.apply(scratch, switches[c.id])
                extra.update({k: v for k, v in scratch.items() if k not in ("capabilities", "name")
                              and json.dumps(v, sort_keys=True) != json.dumps(base_prof.get(k), sort_keys=True)})
                del before
        if existing:
            for k in ("description", "skills_budget_chars", "memory", "tracing", "sandbox", "browser", "hooks", "model_roles",
                      "prompt_append", "tools", "accent"):
                if k in existing and existing.get(k) != base_prof.get(k):
                    extra.setdefault(k, existing[k])
        desc = extra.pop("description", "") or f"Custom: {base} with {len(chosen)} module(s)."
        try:
            path = save_profile(name, base, chosen, desc, extra, overwrite=edit)
        except ValueError as e:
            self.port.notify(str(e), "error")
            return
        self.port.notify(f"Saved profile '{name.lower()}' ({len(chosen)} modules, based on {base}) to {path}. "
                         f"Switch with /profile {name.lower()}.", "success")
        if edit and self.s.base_profile.get("name") == name.lower():
            self.s.switch_profile(name.lower())

    def cmd_capabilities(self, args: str) -> None:
        """List / toggle capabilities. Toggles are saved as the user's overlay and apply to every profile."""
        s = self.s
        parts = args.split()
        if parts:
            cid = parts[0].lower()
            want = {"on": True, "off": False}.get(parts[1].lower()) if len(parts) > 1 else None
            if len(parts) > 1 and want is None:
                self.port.notify("Usage: /capabilities <id> [on|off]", "error")
                return
            self._toggle_capability(cid, want)
            return
        while True:
            rows = s.caps.rows()
            total = sum(r["tokens"] for r in rows)
            labels = []
            for r in rows:
                state = "[x]" if r["on"] else "[ ]"
                cost = f"~{r['tokens']} tok" if r["tokens"] else ("" if r["group"] == "host" or not r["on"] else "0 tok")
                note = f" · not loaded: {r['problem']}" if r["problem"] else ""
                mine = " · your override" if r["overridden"] else ""
                labels.append(f"{state} {r['id']:<19} {r['title']} — {r['description']}" +
                              (f"  ({cost})" if cost else "") + mine + note)
            labels.append("✓ done")
            idx = self.port.select(f"Capabilities · profile {s.profile.get('name')} · modules add ~{total} tokens per request "
                                   "(enter toggles)", labels)
            if idx is None or idx >= len(rows):
                return
            self._toggle_capability(rows[idx]["id"], None)

    def doctor_lines(self) -> List[str]:
        """One screen that answers "is everything loaded, and if not, why?"."""
        s = self.s
        ok, bad, warn = "✓", "✗", "!"
        lines = [f"Profile {s.profile.get('name')} · model {model_key(s.model)} · thinking {s.thinking_level}",
                 f"Working directory {s.cwd}" + ("" if s.trusted else "  (project not trusted: its extensions/skills/hooks are off)"), ""]
        # sign-ins
        providers = sorted({m["provider"] for m in s.registry.all() if m["provider"] != "faux"})
        signed = [p for p in providers if s.auth.has_auth(p)]
        lines.append(f"{ok if signed else bad} Sign-ins: {', '.join(signed) or 'none — run /login'}")
        if s.auth.last_refresh_error:
            lines.append(f"  {warn} last token refresh failed: {s.auth.last_refresh_error}")
        if s.model and not s.auth.has_auth(s.model["provider"]):
            lines.append(f"  {bad} the current model's provider ({s.model['provider']}) has no credentials: /login {s.model['provider']}")
        # modules
        rows = s.caps.rows()
        on = [r for r in rows if r["on"] and r["group"] == "feature"]
        total = sum(r["tokens"] for r in rows)
        lines.append(f"{ok} Modules ({len(on)} on, ~{total} tokens per request): {', '.join(r['id'] for r in on) or 'none'}")
        for cid, why in s.caps.problems.items():
            lines.append(f"  {warn} {cid} not loaded: {why}")
        # extension host
        if s.opts.no_extensions:
            lines.append(f"{ok} Extensions: off (-ne or the profile)")
        else:
            from .extensions.bridge import MIN_NODE, node_executable, node_version
            node = node_executable()
            ver = node_version(node) if node else None
            if not node:
                lines.append(f"{bad} Extensions: Node.js not found (install Node >= {'.'.join(map(str, MIN_NODE))})")
            elif ver and ver < MIN_NODE:
                lines.append(f"{bad} Extensions: Node {'.'.join(map(str, ver))} is too old (need >= {'.'.join(map(str, MIN_NODE))})")
            else:
                exts = s.ext.report() if hasattr(s.ext, "report") else []
                failed = [e for e in exts if not e.get("ok")]
                load = sum(e.get("loadMs", 0) for e in exts)
                lines.append(f"{ok if not failed else warn} Extensions: {len(exts)} loaded in {load} ms"
                             + (f", {len(failed)} failed" if failed else "") + f" · Node {'.'.join(map(str, ver or ()))}")
                for e in failed[:6]:
                    lines.append(f"  {bad} {e['name']}: {str(e.get('error'))[:160]}")
                slow = sorted(exts, key=lambda e: -(e.get("loadMs", 0) + sum((e.get("handlerMs") or {}).values())))[:3]
                slow = [e for e in slow if e.get("loadMs", 0) + sum((e.get("handlerMs") or {}).values()) >= 200]
                if slow:
                    lines.append("  slowest: " + ", ".join(
                        f"{e['name']} ({e.get('loadMs', 0) + sum((e.get('handlerMs') or {}).values())} ms)" for e in slow)
                        + "   (/extensions slow for the full list)")
        # Hermes plugins
        from .caps import hermes_plugins, hooks as shell_hooks
        host = hermes_plugins.host_of(s)
        if host is not None:
            good = [p for p in host.plugins if p["ok"]]
            lines.append(f"{ok} Hermes plugins: {len(good)} loaded" + (f" ({', '.join(p['name'] for p in good)})" if good else ""))
            for pl in host.plugins:
                if pl["error"]:
                    lines.append(f"  {bad if pl['error'] != 'disabled' else warn} {pl['name']}: {pl['error']}")
                if pl["unsupported"]:
                    lines.append(f"  {warn} {pl['name']} registers things this core has no slot for yet: {', '.join(sorted(set(pl['unsupported'])))}")
        if s.caps.is_enabled("hooks"):
            counts = {k: v for k, v in shell_hooks.summary(s).items() if v}
            lines.append(f"{ok} Shell hooks: " + (", ".join(f"{k} ×{v}" for k, v in counts.items()) or "none configured (hooks.json)"))
        # MCP
        if s.mcp is not None:
            try:
                rows = [x for x in s.mcp.status_lines() if x.strip()]
            except Exception:
                rows = []
            lines.append(f"{ok} MCP: {len(rows)} line(s) of status (/mcp for details)" if rows else f"{ok} MCP: no servers configured")
            lines += [f"  {x.strip()[:160]}" for x in rows[:6]]
        lines.append(f"{ok} Skills: {len(s.skills)} · prompt templates: {len(s.templates)} · context files: {len(s.context_files)}")
        if getattr(s, "startup_seconds", 0):
            lines.append(f"{ok} Background startup took {s.startup_seconds:.1f} s (the window opens before it finishes)")
        notes = [d for d in s.diagnostics if "capability '" not in d][-6:]
        if notes:
            lines += ["", "Recent warnings:"] + [f"  {warn} {d[:200]}" for d in notes]
        return lines

    def cmd_doctor(self, args: str) -> None:
        self.port.show("doctor", self.doctor_lines())

    def cmd_plan(self, args: str) -> None:
        s = self.s
        if not s.caps.is_enabled("plan"):
            self.port.notify("Plan mode is a capability that is off in this profile. Turn it on with "
                             "/capabilities plan on (or use the coder / full profile).", "warning")
            return
        from .caps import plan
        arg = args.strip().lower()
        if arg not in ("", "on", "off"):
            self.port.notify("Usage: /plan [on|off]", "error")
            return
        on = (not plan.is_on(s)) if not arg else arg == "on"
        plan.set_mode(s, on)
        self.port.notify("Plan mode on: read-only until you approve a plan. Describe the task." if on else
                         "Plan mode off: edits and commands are allowed again.", "success")

    def _toggle_capability(self, cid: str, want: Optional[bool]) -> None:
        s = self.s
        try:
            new = s.caps.toggle(cid, want)
        except KeyError:
            from .caps import load_builtin
            self.port.notify(f"Unknown capability '{cid}'. Available: {', '.join(sorted(load_builtin()))}", "error")
            return
        s.reload()
        why = s.caps.problems.get(cid)
        self.port.notify(f"{cid}: {'on' if new else 'off'}" + (f" (not loaded: {why})" if why and new else "") +
                         f" · {len(s.active_tool_names)} tools active", "warning" if why and new else "success")

    # ------------------------------------------------------------------------------- command sources
    def cmd_commands(self, args: str) -> None:
        """Prefix manager: which source a duplicate `/name` runs, and the prefix each source uses."""
        cs = self.sources()
        parts = args.split()
        sub = parts[0].lower() if parts else ""
        if sub in ("list", "ls", "all"):
            self.port.show("command sources", self._sources_lines(cs, all_commands=True))
            return
        if sub in ("conflicts", "dupes", "duplicates"):
            self.port.show("duplicate commands", self._conflict_lines(cs))
            return
        if sub == "prefer" and len(parts) >= 2:
            name = parts[1].lstrip("/")
            group = cs.by_name().get(name) or []
            if len(group) < 2:
                self.port.notify(f"/{name} has only one source; nothing to choose.", "info")
                return
            if len(parts) < 3:
                self._choose_preference(cs, name, group)
                return
            want = parts[2].lstrip("/")
            hit = next((e for e in group if want in (e.qualified, e.prefix, e.source_key)), None)
            if hit is None:
                self.port.notify(f"No source '{want}' for /{name}. Choices: " +
                                 ", ".join("/" + e.qualified for e in group), "error")
                return
            cs.remember(name, hit.qualified)
            self.port.notify(f"/{name} now runs /{hit.qualified} ({hit.origin})", "success")
            return
        if sub in ("forget", "unprefer") and len(parts) >= 2:
            name = parts[1].lstrip("/")
            cs.remember(name, None)
            self.port.notify(f"/{name} will ask again which source to use.", "success")
            return
        if sub in ("prefix", "rename") and len(parts) >= 2:
            key = self._source_key(cs, parts[1])
            if key is None:
                self.port.notify(f"Unknown source '{parts[1]}'. See /commands list.", "error")
                return
            new = parts[2] if len(parts) > 2 else (self.port.input(f"New prefix for {key} (empty = default)") or "")
            try:
                now = cs.set_prefix(key, new.strip().rstrip(":") or None)
            except ValueError as e:
                self.port.notify(str(e), "error")
                return
            self.port.notify(f"{key} commands now use /{now}:<name>", "success")
            return
        if sub == "reset":
            cs.reset()
            self.port.notify("Forgot all remembered command sources.", "success")
            return
        if sub:
            self.port.notify("Usage: /commands [list|conflicts|prefer <name> [prefix:name]|forget <name>|"
                             "prefix <source> [new]|reset]", "warning")
            return
        conflicts = cs.conflicts()
        menu = [f"Duplicate commands ({len(conflicts)}): choose which source runs", "Rename a source prefix",
                "Show all commands by source", "Forget remembered choices"]
        idx = self.port.select("Command sources", menu)
        if idx == 0:
            if not conflicts:
                self.port.notify("No command is provided by more than one source.", "info")
                return
            names = sorted(conflicts)
            prefs = cs.preferences()
            labels = [f"/{n:<22} {len(conflicts[n])} sources" + (f"   -> /{prefs[n]}" if n in prefs else "   (asks)")
                      for n in names]
            j = self.port.select("Which command?", labels)
            if j is not None:
                self._choose_preference(cs, names[j], conflicts[names[j]])
        elif idx == 1:
            srcs = cs.sources()
            j = self.port.select("Rename which source's prefix?",
                                 [f"{p + ':':<18} {o}  ({n} commands)" for _k, p, o, n in srcs])
            if j is not None:
                self.cmd_commands(f"prefix {srcs[j][0]}")
        elif idx == 2:
            self.port.show("command sources", self._sources_lines(cs, all_commands=True))
        elif idx == 3:
            self.cmd_commands("reset")

    def _choose_preference(self, cs, name: str, group) -> None:
        prefs = cs.preferences()
        labels = [("● " if prefs.get(name) == e.qualified else "  ") + f"/{e.qualified:<28} {e.origin}" for e in group]
        labels.append("  ask every time")
        j = self.port.select(f"/{name} is provided by {len(group)} sources. Which one should it run?", labels)
        if j is None:
            return
        if j == len(group):
            cs.remember(name, None)
            self.port.notify(f"/{name} will ask which source to use.", "success")
        else:
            cs.remember(name, group[j].qualified)
            self.port.notify(f"/{name} now runs /{group[j].qualified}", "success")

    @staticmethod
    def _source_key(cs, text: str) -> Optional[str]:
        t = text.rstrip(":")
        for key, prefix, _o, _n in cs.sources():
            if t in (key, prefix):
                return key
        return None

    @staticmethod
    def _conflict_lines(cs) -> List[str]:
        conflicts = cs.conflicts()
        if not conflicts:
            return ["No command is provided by more than one source."]
        prefs = cs.preferences()
        lines = []
        for name in sorted(conflicts):
            lines.append(f"/{name}" + (f"   -> runs /{prefs[name]}" if name in prefs
                                       else "   -> asks (Pi's order outside the window)"))
            for e in conflicts[name]:
                lines.append(f"    /{e.qualified:<30} {e.origin}  {e.path}")
        lines += ["", "Pick one: /commands prefer <name> <prefix:name>   ·   type /<prefix>:<name> to run a source directly"]
        return lines

    @staticmethod
    def _sources_lines(cs, all_commands: bool = False) -> List[str]:
        lines = []
        by_src: Dict[str, List[Any]] = {}
        for e in cs.entries():
            by_src.setdefault(e.source_key, []).append(e)
        for key, prefix, origin, n in cs.sources():
            lines.append(f"{prefix + ':':<18} {origin}  [{key}]  {n} command(s)")
            if all_commands:
                lines.append("    " + "  ".join("/" + e.name for e in by_src[key]))
        lines += ["", "Rename: /commands prefix <source> <new>   ·   duplicates: /commands conflicts"]
        return lines

    def cmd_quit(self, args: str) -> None:
        self.port.quit()
