"""`task`: hand a self-contained piece of work to a subagent (Oh My Pi `task`, Hermes `delegate_task`).

The subagent is a fresh `AgentSession` with its own, empty context: it gets only the prompt you write, works with
the core file/shell tools, and returns one final report. Its intermediate tool output never enters the parent's
context, which is the main reason to use it - a wide search or a noisy build costs the parent a few hundred tokens
instead of tens of thousands.

Subagents cannot spawn subagents, run without extensions or MCP, keep no session file, and inherit the parent's
model, credentials, project trust and permission gate.

Named agents: a markdown file with frontmatter (`name`, `description`, optional `tools`, `model`) whose body is the
agent's system prompt - the format Claude Code and Oh My Pi use. They are read from `<agent-dir>/agents`, the
project's `.omni/agents` and `.pi/agents` (trusted), and, with `skills-universal` on, `~/.claude/agents` and the
project's `.claude/agents`. `task(agent="reviewer", prompt=...)` runs one.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from ..tools.base import ToolDef, ToolError, text_result
from . import Capability

READ_ONLY_TOOLS = ["read", "grep", "find", "ls"]
WORK_TOOLS = ["read", "bash", "edit", "write", "grep", "find", "ls"]
MAX_REPORT_CHARS = 12_000
MAX_PARALLEL_TASKS = 6
SUBAGENT_PROMPT = ("You are a subagent working on one delegated task. You have no memory of the parent conversation: "
                   "everything you need is in the task below. Do the work, then end with a concise final report "
                   "(findings, files changed with paths, anything left undone). The report is the only thing the "
                   "parent agent sees.")


TOOL_ALIASES = {"glob": "find", "ls": "ls", "list": "ls", "shell": "bash", "terminal": "bash", "multiedit": "edit",
                "notebookedit": "edit", "str_replace": "edit"}


def discover_agents(session: Any) -> Dict[str, Dict[str, Any]]:
    """name -> {description, tools, model, prompt, path}; earlier folders win."""
    from pathlib import Path
    from .. import resources as res
    roots = [Path(session.agent_dir) / "agents"]
    if session.trusted:
        roots += [Path(session.cwd) / ".omni" / "agents", Path(session.cwd) / ".pi" / "agents"]
    if "skills-universal" in (session.profile.get("capabilities") or []):
        roots.append(Path.home() / ".claude" / "agents")
        if session.trusted:
            roots.append(Path(session.cwd) / ".claude" / "agents")
    roots += [Path(p) for p in session.caps.collect("agent_roots")]
    out: Dict[str, Dict[str, Any]] = {}
    for r in roots:
        if not r.is_dir():
            continue
        for f in sorted(r.glob("*.md")):
            try:
                fm, body = res.parse_frontmatter(f.read_text(encoding="utf-8"))
            except OSError:
                continue
            name = str(fm.get("name") or f.stem).strip().lower()
            if not name or name in out or not body.strip():
                continue
            tools = fm.get("tools")
            if isinstance(tools, str):
                tools = [t.strip() for t in tools.split(",")]
            mapped = []
            for t in tools or []:
                t = str(t).strip().lower()
                t = TOOL_ALIASES.get(t, t)
                if t in WORK_TOOLS and t not in mapped:
                    mapped.append(t)
            out[name] = {"description": str(fm.get("description") or "").strip(), "tools": mapped or None,
                         "model": str(fm.get("model") or "").strip() or None, "prompt": body.strip(), "path": str(f)}
    return out


def model_roles(session: Any) -> Dict[str, str]:
    """Named model roles (Oh My Pi's smol / slow / plan): settings.json `modelRoles`, overridden by the profile's."""
    roles: Dict[str, str] = {}
    for src in (session.settings.get("modelRoles"), session.profile.get("model_roles")):
        if isinstance(src, dict):
            roles.update({str(k).lower(): str(v) for k, v in src.items() if v})
    return roles


def resolve_model_choice(session: Any, choice: str) -> Optional[Dict[str, Any]]:
    pattern = model_roles(session).get(choice.strip().lower(), choice.strip())
    m = session.registry.resolve(pattern)
    return m if m and session.auth.has_auth(m["provider"]) else None


def shared_memory(parent: Any) -> str:
    """The parent's core memory blocks, for the subagent to read (it cannot edit them)."""
    if not parent.caps.is_enabled("memory"):
        return ""
    try:
        from . import memory as mem
        text = mem._prompt(parent)
    except Exception:
        return ""
    return ("\n\nShared memory (read-only; what the main agent knows about the user and the project):\n" + text[:4000]) if text else ""


def run_subagent(parent: Any, prompt: str, read_only: bool, signal: Any = None, on_update: Any = None,
                 agent: Optional[Dict[str, Any]] = None, model_choice: str = "") -> Dict[str, Any]:
    from ..agent import AgentSession, SessionOptions
    from ..ai.models import model_key
    from ..modes import HeadlessUI
    if parent.model is None:
        raise ToolError("no model selected")
    tools = READ_ONLY_TOOLS if read_only else WORK_TOOLS
    model, system = model_key(parent.model), SUBAGENT_PROMPT
    if agent:
        if agent.get("tools"):
            tools = [t for t in agent["tools"] if not read_only or t in READ_ONLY_TOOLS] or READ_ONLY_TOOLS
        wanted = agent.get("model")
        if wanted and wanted != "inherit":
            m = parent.registry.resolve(wanted)
            if m and parent.auth.has_auth(m["provider"]):
                model = model_key(m)
        system = SUBAGENT_PROMPT + "\n\n" + agent["prompt"]
    if model_choice:  # an explicit choice for this call beats the agent file
        picked = resolve_model_choice(parent, model_choice)
        if picked is None:
            raise ToolError(f"model '{model_choice}' is not a configured role or an available model. Roles: "
                            f"{', '.join(sorted(model_roles(parent))) or '(none configured)'}")
        model = model_key(picked)
    opts = SessionOptions(
        cwd=parent.cwd, model=model, thinking=parent.thinking_level, mode="print",
        tools=",".join(tools), no_session=True, no_extensions=True, no_mcp=True,
        no_prompt_templates=True, approve=parent.trusted, agent_dir=str(parent.agent_dir), profile="core",
        capabilities=[], append_system_prompt=system + shared_memory(parent))
    child = AgentSession(opts, ui=HeadlessUI())
    child.start()
    child.gate = parent.gate  # the parent's permission gate also guards the subagent
    steps: List[str] = []

    def watch(ev: Dict[str, Any]) -> None:
        if ev.get("type") == "tool_execution_start" and on_update:
            args = ev.get("args") or {}
            hint = args.get("path") or args.get("command") or args.get("pattern") or ""
            steps.append(f"{ev.get('toolName')} {str(hint)[:60]}".strip())
            on_update({"content": [{"type": "text", "text": "\n".join(steps[-8:])}], "details": {"steps": len(steps)}})
    child.subscribe(watch)
    remove = signal.add_listener(child.abort) if signal is not None and hasattr(signal, "add_listener") else (lambda: None)
    try:
        child.prompt(prompt, source="subagent")
    finally:
        remove()
    messages = [e["message"] for e in child.sessions.entries if e.get("type") == "message"]
    last = next((m for m in reversed(messages) if m.get("role") == "assistant"), None)
    usage = {"input": 0, "output": 0, "cost": 0.0}
    for m in messages:
        u = m.get("usage") or {}
        usage["input"] += (u.get("input") or 0) + (u.get("cacheRead") or 0)
        usage["output"] += u.get("output") or 0
        usage["cost"] += (u.get("cost") or {}).get("total") or 0.0
    if last is None:
        raise ToolError("the subagent produced no response")
    text = "".join(b.get("text", "") for b in last.get("content") or [] if b.get("type") == "text").strip()
    if last.get("stopReason") in ("error", "aborted"):
        raise ToolError(f"subagent {last.get('stopReason')}: {last.get('errorMessage') or text or 'no details'}")
    return {"text": text, "usage": usage, "toolCalls": len(steps), "turns": sum(1 for m in messages if m.get("role") == "assistant")}


def _tools(session: Any) -> Dict[str, ToolDef]:
    def one(spec: Dict[str, Any], signal: Any, on_update: Any) -> Dict[str, Any]:
        prompt = str(spec.get("prompt") or "").strip()
        if not prompt:
            raise ToolError("prompt is required: a complete, self-contained description of the task")
        agent = None
        if spec.get("agent"):
            agents = discover_agents(session)
            agent = agents.get(str(spec["agent"]).strip().lower())
            if agent is None:
                raise ToolError(f"unknown agent '{spec['agent']}'. Available: {', '.join(sorted(agents)) or '(none defined)'}")
        return run_subagent(session, prompt, bool(spec.get("read_only")), signal, on_update, agent, str(spec.get("model") or ""))

    def execute(_id, p, signal=None, on_update=None, ctx=None):
        batch = p.get("tasks")
        if isinstance(batch, list) and batch:  # independent subtasks side by side (Oh My Pi task batches, Hermes delegation)
            if len(batch) > MAX_PARALLEL_TASKS:
                raise ToolError(f"at most {MAX_PARALLEL_TASKS} tasks per call")
            from concurrent.futures import ThreadPoolExecutor
            done: List[str] = []

            def run_one(item: Any) -> Dict[str, Any]:
                i, spec = item
                try:
                    r = one(spec if isinstance(spec, dict) else {}, signal, None)
                except ToolError as e:
                    r = {"text": f"FAILED: {e}", "usage": {"input": 0, "output": 0, "cost": 0.0}, "toolCalls": 0, "turns": 0, "failed": True}
                done.append(str(i + 1))
                if on_update:
                    on_update({"content": [{"type": "text", "text": f"finished {len(done)}/{len(batch)}: task {', '.join(sorted(done))}"}]})
                return r
            with ThreadPoolExecutor(max_workers=min(4, len(batch)), thread_name_prefix="omni-subagent") as pool:
                results = list(pool.map(run_one, enumerate(batch)))
            share = max(1500, MAX_REPORT_CHARS // len(batch))
            parts = []
            for i, (spec, r) in enumerate(zip(batch, results), 1):
                label = str((spec or {}).get("description") or "").strip() if isinstance(spec, dict) else ""
                text = r["text"] or "(no written report)"
                if len(text) > share:
                    text = text[:share] + f"\n[truncated: {len(r['text']) - share} more characters]"
                parts.append(f"## Task {i}" + (f": {label}" if label else "") + f"\n{text}")
            failed = sum(1 for r in results if r.get("failed"))
            return text_result("\n\n".join(parts), {"tasks": len(batch), "failed": failed,
                                                    "toolCalls": sum(r["toolCalls"] for r in results),
                                                    "cost": round(sum(r["usage"]["cost"] for r in results), 6)}, failed == len(batch))
        r = one(p, signal, on_update)
        text = r["text"] or "(the subagent finished without a written report)"
        if len(text) > MAX_REPORT_CHARS:
            text = text[:MAX_REPORT_CHARS] + f"\n\n[report truncated: {len(r['text']) - MAX_REPORT_CHARS} more characters]"
        return text_result(text, {"description": p.get("description"), "usage": r["usage"], "toolCalls": r["toolCalls"],
                                  "turns": r["turns"]})

    agents = discover_agents(session)
    listing = ""
    if agents:
        rows = [f"{n}: {a['description'][:90]}" if a["description"] else n for n, a in sorted(agents.items())]
        listing = " Named agents (pass as `agent`): " + "; ".join(rows)[:700] + "."
    configured = model_roles(session)
    roles = f" Model roles (pass as `model`): {', '.join(sorted(configured))}." if configured else ""
    return {"task": ToolDef(
        name="task", label="Task",
        description="Delegate a self-contained task to a subagent with a fresh context and get back only its final "
                    "report. Use it for wide searches, investigations and independent subtasks whose intermediate "
                    "output you do not need. The subagent knows nothing about this conversation: put all context, "
                    "paths and the expected output in `prompt`. Set read_only=true for pure research. For several independent "
                    "subtasks, pass `tasks` to run them at the same time." + listing + roles,
        parameters={"type": "object", "properties": {
            "description": {"type": "string", "description": "3-6 word label"},
            "prompt": {"type": "string"}, "read_only": {"type": "boolean"},
            "agent": {"type": "string", "description": "name of a defined agent (optional)"},
            "model": {"type": "string", "description": "model role or id for this subagent (optional)"},
            "tasks": {"type": "array", "description": "instead of one prompt: up to 6 independent tasks run in parallel, "
                      "each {prompt, description?, read_only?, agent?, model?}",
                      "items": {"type": "object", "properties": {"prompt": {"type": "string"}, "description": {"type": "string"},
                                                                 "read_only": {"type": "boolean"}, "agent": {"type": "string"},
                                                                 "model": {"type": "string"}}, "required": ["prompt"]}}}},
        execute=execute, prompt_snippet="Delegate a self-contained task to a subagent")}


def _available(session: Any):
    return "subagents cannot spawn subagents" if getattr(session.opts, "capabilities", None) == [] and \
        str(session.opts.append_system_prompt or "").startswith(SUBAGENT_PROMPT) else None


CAPABILITIES = [Capability("task", "Subagents", "Delegate work to a fresh-context subagent and receive only its report.",
                           "Oh My Pi task, Hermes delegate_task", tools=_tools, available=_available)]
