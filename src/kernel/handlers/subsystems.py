"""
Handlers for the specialised subsystems. Each handler talks to the shared service
instances in ``ctx.services`` so state (running servers, rooms, worktrees) persists
between commands. Output states plainly what is and is not available.
"""

from __future__ import annotations

import json
import shlex
from typing import List

from . import HandlerContext, UsageError, command


def _split(arg: str) -> List[str]:
    try:
        return shlex.split(arg, posix=True)
    except ValueError as e:
        raise UsageError(f"[!] Could not parse arguments: {e}")


def _sub(arg: str, default: str):
    head, _, rest = arg.strip().partition(" ")
    return (head.lower() or default), rest.strip()


# --------------------------------------------------------------------------- browser
@command("browser")
def browser(arg: str, ctx: HandlerContext) -> str:
    b = ctx.services.browser
    sub, rest = _sub(arg, "tabs")
    usage = "Usage: /browser [tabs|launch [headless]|open <url>|nav <url>|snapshot|eval <js>|click <css>|fill <css> <text>|screenshot|console|close <tab-id>]"

    if sub == "launch":
        return "[✓] " + b.launch(headless=rest.lower() == "headless")
    if sub in ("tabs", "list"):
        tabs = b.list_tabs()
        lines = [f"--- Browser tabs on {b.host}:{b.port} ({len(tabs)}) ---"]
        lines += [f"  {'*' if t.id == b.active_tab_id else ' '} [{t.id[:8]}] {t.title[:50] or '(untitled)'}  {t.url[:70]}" for t in tabs]
        return "\n".join(lines)
    if sub == "open":
        if not rest:
            raise UsageError(usage)
        t = b.open_tab(rest)
        return f"[✓] Opened [{t.id[:8]}] {t.url}"
    if sub == "nav":
        if not rest:
            raise UsageError(usage)
        b.navigate(rest)
        return f"[✓] Navigated to {rest}"
    if sub in ("snapshot", "dom"):
        s = b.snapshot_dom()
        out = [f"Title: {s.get('title')}", f"URL:   {s.get('url')}", f"Text:  {s.get('text', '')[:400]}"]
        out += [f"  link: {l['text'][:50]} -> {l['href'][:70]}" for l in s.get("links", [])[:10]]
        out += [f"  ctrl: <{c['tag']}> id={c['id'] or '-'} {c['text'][:40]}" for c in s.get("controls", [])[:10]]
        return "\n".join(out)
    if sub == "eval":
        if not rest:
            raise UsageError(usage)
        return f"{b.evaluate_js(rest)!r}"
    if sub == "click":
        if not rest:
            raise UsageError(usage)
        return "[✓] Clicked" if b.click_element(rest) else f"[!] No element matches '{rest}'"
    if sub == "fill":
        parts = rest.split(maxsplit=1)
        if len(parts) != 2:
            raise UsageError(usage)
        return "[✓] Filled" if b.fill_input(parts[0], parts[1]) else f"[!] No element matches '{parts[0]}'"
    if sub == "screenshot":
        return f"[✓] Saved {b.capture_screenshot(rest or None)}"
    if sub == "console":
        errs = b.read_console_errors()
        return "\n".join(errs) if errs else "(no console errors or warnings)"
    if sub == "close":
        if not rest:
            raise UsageError(usage)
        full = next((t.id for t in b.list_tabs() if t.id.startswith(rest)), rest)
        b.close_tab(full)
        return f"[✓] Closed {rest}"
    raise UsageError(usage)


# --------------------------------------------------------------------------- collab
@command("collab", "share")
def collab(arg: str, ctx: HandlerContext) -> str:
    c = ctx.services.collab
    sub, rest = _sub(arg, "")
    if sub == "stop":
        for rid in list(c.rooms):
            c.close_room(rid)
        c.stop()
        return "[✓] Collaboration server stopped and all rooms closed."
    if sub == "status" or (not sub and c.rooms):
        if not c.rooms:
            return "No collaboration rooms. Use /collab (pair) or /collab view."
        lines = [f"--- Collaboration rooms (server {c.host}:{c.port}) ---"]
        lines += [f"  [{r.id}] {r.mode:<4} viewers={len(r.clients)} events={len(r.events)}  {c.get_share_url(r.id)}" for r in c.rooms.values()]
        return "\n".join(lines)
    mode = "view" if sub == "view" else "pair"
    room = c.create_room(mode=mode)
    note = "guests can watch AND send prompts" if mode == "pair" else "guests can only watch"
    return (f"[✓] Room {room.id} ({mode}: {note})\n  {c.get_share_url(room.id)}\n"
            "  Reachable on this machine only (127.0.0.1). Expose it deliberately (e.g. a tunnel) to share it.")


# --------------------------------------------------------------------------- benchmarks
@command("eval", "benchmark")
def evaluate(arg: str, ctx: HandlerContext) -> str:
    m = ctx.services.benchmarks
    sub, rest = _sub(arg, "list")
    if sub == "new":
        if not rest:
            raise UsageError("Usage: /eval new <name>")
        return f"[✓] Created experiment {m.create_experiment(rest)}"
    if sub == "run":
        raise UsageError("[!] Benchmark execution is not implemented yet: it needs a task dataset and Docker. "
                         "The SQLite store (/eval list) is ready to record runs.")
    if sub in ("list", "show"):
        exps = m.list_experiments()
        if not exps:
            return "No experiments yet. Create one with /eval new <name>."
        lines = [f"--- Benchmark experiments ({len(exps)}) ---"]
        for e in exps:
            s = m.get_experiment_summary(e["id"])
            lines.append(f"  [{e['id']}] {e['name']} ({e['benchmark']}): {s['total_runs']} runs, {s['resolve_rate']}% resolved, ${s['total_cost_usd']}")
        return "\n".join(lines)
    raise UsageError("Usage: /eval [list|new <name>|run]")


# --------------------------------------------------------------------------- gateways
@command("gateway")
def gateway(arg: str, ctx: HandlerContext) -> str:
    g = ctx.services.gateways
    sub, rest = _sub(arg, "status")
    targets = list(g.gateways) if rest.lower() in ("", "all") else [rest.lower()]
    for t in targets:
        if t not in g.gateways:
            raise UsageError(f"[!] Unknown platform '{t}'. Choose from: {', '.join(g.gateways)}")
    if sub == "start":
        return "\n".join(g.start_gateway(t) for t in targets)
    if sub == "stop":
        for t in targets:
            g.stop_gateway(t)
        return f"[✓] Stopped: {', '.join(targets)}"
    lines = ["--- Chat gateways ---"]
    for name, st in g.get_status().items():
        state = "connected" if st["connected"] else ("ready (token set)" if st["configured"] else f"needs {st['token_env']}")
        lines.append(f"  {name:<9} {state:<28} {'receive+send' if st['inbound'] else 'send only'}")
    lines.append("Start with /gateway start [platform]. Only Telegram receives messages by itself today.")
    return "\n".join(lines)


# --------------------------------------------------------------------------- sandbox
@command("sandbox")
def sandbox(arg: str, ctx: HandlerContext) -> str:
    s = ctx.services.sandbox
    argv = _split(arg)
    allow_host = False
    if argv and argv[0] == "--host":
        allow_host, argv = True, argv[1:]
    if not argv:
        return (f"Docker sandbox: {'available' if s.is_docker_active(refresh=True) else 'NOT available'}  "
                f"(image {s.config.image}, mem {s.config.memory_limit}, cpus {s.config.cpu_limit}, network {s.config.network_mode})\n"
                "Usage: /sandbox <command>   |   /sandbox --host <command>  (runs on this machine, NO isolation)")
    r = s.run(argv, allow_host=allow_host)
    head = ("[refused] " if r.refused else "") + f"exit={r.exit_code} isolated={r.isolated_in_docker} {r.duration_s}s"
    return head + "\n" + (r.stdout or r.stderr).rstrip()


# --------------------------------------------------------------------------- node pairing
@command("pair", "nodes")
def pair(arg: str, ctx: HandlerContext) -> str:
    n = ctx.services.nodes
    sub, rest = _sub(arg, "list")
    if sub in ("qr", "new", "request"):
        req = n.create_pairing_request()
        url = n.get_pairing_url(req)
        qr = n.render_qr(url)
        return (qr + "\n" if qr else "") + f"Pairing URL: {url}\nApprove with: /pair approve {req.request_id}" + \
            ("" if qr else "\n(Install 'qrcode' for a scannable QR: pip install qrcode)")
    if sub == "approve":
        if not rest:
            raise UsageError("Usage: /pair approve <request-id>")
        node = n.approve_pairing(rest, "Companion Device", "unknown")
        return f"[✓] Paired {node.name} ({node.id})" if node else f"[!] Request '{rest}' is invalid or expired"
    nodes = n.list_nodes()
    title = "=== OpenClaw Companion Device Pairing ===\n" if ctx_name(ctx) == "pair" else ""
    lines = [title + f"--- OpenClaw Paired Nodes ({len(nodes)}) ---"]
    lines += [f"  [{x.id}] {x.name} ({x.platform}, {x.ip_address}) {x.status}" for x in nodes]
    lines.append("Pairing records devices only; remote task execution on nodes is not implemented yet.")
    lines.append("New device: /pair qr")
    return "\n".join(lines)


def ctx_name(ctx: HandlerContext) -> str:
    return ctx.extra.get("name", "pair")


# --------------------------------------------------------------------------- canvas
@command("canvas")
def canvas(arg: str, ctx: HandlerContext) -> str:
    c = ctx.services.canvas
    sub, rest = _sub(arg, "list")
    if sub == "create":
        title, sep, source = rest.partition("::")
        if not sep or not source.strip():
            raise UsageError("Usage: /canvas create <title> :: <mermaid source>   e.g.  /canvas create Flow :: graph TD; A-->B")
        d = c.create_document(title.strip() or "Untitled", source.strip(), "mermaid")
        return f"[✓] {c.url_for(d.id)}"
    if sub == "open":
        import webbrowser
        if not c.get_document(rest):
            raise UsageError(f"[!] No canvas document '{rest}'")
        webbrowser.open(c.url_for(rest))
        return f"[✓] Opened {c.url_for(rest)}"
    docs = c.list_documents()
    if not docs:
        return "No canvas documents. Create one: /canvas create <title> :: <mermaid source>"
    return "\n".join([f"--- Canvas documents ({len(docs)}) ---"] + [f"  [{d.id}] {d.title} ({d.kind})  {c.url_for(d.id)}" for d in docs])


# --------------------------------------------------------------------------- local engine
@command("local")
def local(arg: str, ctx: HandlerContext) -> str:
    e = ctx.services.local_engine
    sub, rest = _sub(arg, "status")
    if sub == "start":
        return "[✓] " + e.start(rest or None)
    if sub == "stop":
        e.stop()
        return "[✓] Local engine stopped"
    if sub in ("ask", "query"):
        if not rest:
            raise UsageError("Usage: /local ask <prompt>")
        msg = e.query([{"role": "user", "content": rest}])
        calls = msg.get("tool_calls")
        return (msg.get("content") or "") + (f"\n[tool calls] {json.dumps(calls)}" if calls else "")
    models = e.list_available_models()
    lines = [f"--- Local engine ({e.config.backend}) ---",
             f"  server binary : {e.binary() or 'NOT FOUND'}",
             f"  server running: {e.is_running()} ({e.base_url})",
             f"  models        : {len(models)}"]
    lines += [f"    {m['name']} ({m['size_mb']} MB)" for m in models]
    if not models:
        lines.append("    none - put a .gguf file in ~/.omni/models")
    return "\n".join(lines)


# --------------------------------------------------------------------------- voice
@command("voice")
def voice(arg: str, ctx: HandlerContext) -> str:
    v = ctx.services.voice
    sub, rest = _sub(arg, "status")
    if sub == "say":
        if not rest:
            raise UsageError("Usage: /voice say <text>")
        return "[✓] Speaking" if v.speak(rest) else "[!] No text-to-speech engine available on this system"
    if sub in ("start", "listen"):
        v.start_listening()  # raises VoiceUnavailable with install guidance
    return f"Voice: text-to-speech {'available' if v.tts_available() else 'unavailable'}; speech input/wake-word: not implemented.\nTry: /voice say hello"


# --------------------------------------------------------------------------- tickets (Paperclip-style)
@command("paperclip")
def paperclip(arg: str, ctx: HandlerContext) -> str:
    t = ctx.services.tickets
    sub, rest = _sub(arg, "status")
    if sub == "tickets":
        items = t.list_tickets()
        return "\n".join([f"--- Tickets ({len(items)}) ---"] + [f"  [{x.id}] {x.title} ({x.priority}, {x.status})" for x in items])
    if sub in ("submit", "done"):
        tid, _, summary = rest.partition(" ")
        if not tid:
            raise UsageError("Usage: /paperclip done <ticket-id> <summary>")
        r = t.submit_deliverable(tid, summary or "completed")
        return f"[✓] {tid} submitted" if r["success"] else f"[!] {r['error']}"
    hb = t.heartbeat()
    return (f"Local ticket queue for employee '{t.profile.employee_id}' ({hb['active_tickets']} in progress). "
            "NOTE: not connected to a Paperclip server; this is a local file store.\nUsage: /paperclip [tickets|done <id> <summary>]")


# --------------------------------------------------------------------------- lsp
@command("lsp")
def lsp(arg: str, ctx: HandlerContext) -> str:
    argv = _split(arg)
    if len(argv) < 2 or argv[0] not in ("diagnostics", "symbols", "definition"):
        raise UsageError("Usage: /lsp diagnostics <file> | /lsp symbols <file> [filter] | /lsp definition <file> <symbol>")
    op, path = argv[0], argv[1]
    extra = argv[2] if len(argv) > 2 else ""
    res = ctx.services.lsp.execute_lsp_operation(op, path, query=extra, symbol=extra)
    return json.dumps(res, indent=2)


# --------------------------------------------------------------------------- worktrees
@command("worktree")
def worktree(arg: str, ctx: HandlerContext) -> str:
    w = ctx.services.worktrees
    sub, rest = _sub(arg, "list")
    if sub == "create":
        return "[✓] " + w.create_worktree()[1]
    if sub == "list":
        items = w.list_worktrees()
        return "\n".join([f"--- Shadow worktrees ({len(items)}) ---"] + [f"  [{i['id']}] {i['branch']}  {i['path']}" for i in items])
    wid, _, cmd = rest.partition(" ")
    if sub in ("run", "verify") and (not wid or not cmd.strip()):
        raise UsageError(f"Usage: /worktree {sub} <id> <command>")
    if sub == "run":
        code, out, err = w.execute_in_worktree(wid, cmd)
        return f"exit={code}\n{(out + err).rstrip()}"
    if sub == "verify":
        o = w.verify_and_merge(wid, cmd)
        return f"{'PASS' if o.passed else 'FAIL'} (exit {o.exit_code}): {o.message}\n{o.output[-800:]}"
    if sub == "merge":
        return ("[✓] " if (r := w.merge_worktree(wid))[0] else "[!] ") + r[1]
    if sub == "discard":
        return "[✓] " + w.discard_worktree(wid)[1]
    raise UsageError("Usage: /worktree [create|list|run <id> <cmd>|verify <id> <test cmd>|merge <id>|discard <id>]")
