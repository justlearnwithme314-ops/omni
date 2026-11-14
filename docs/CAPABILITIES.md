# Capabilities and profiles

Omni is built from **capabilities**: self-contained modules that load only when a profile or you switch them on.
The barebones `core` profile loads none of them; `full` loads all. Everything in between is a list in a JSON file.

## Profiles

| Profile | Extends | Adds | Alias (inspiration) |
|---|---|---|---|
| `core` | – | 4 tools (`read bash edit write`), extensions, skills, prompt templates | `pi`, `minimal` |
| `lean` | core | `grep find ls` | |
| `coder` | lean | `todo`, `task`, `ask`, `fetch`, `checkpoint`, `plan`, `ast` | `omp` |
| `assistant` | lean | `todo`, `memory`, `task`, `ask`, `fetch`, `skills-universal`, `skill-authoring` | `hermes` |
| `balanced` | lean | `todo`, `ask`, `fetch` + MCP registry, Hermes plugins, Omni tools, permission gate | |
| `autonomous` | balanced | `task`, `memory`, `skills-universal`, `checkpoint` + OpenClaw plugins | `openclaw` |
| `full` | autonomous | `skill-authoring`, `plan`, `ast` | |

The old names keep working everywhere (`omni pi`, `--profile hermes`, `/profile omp`).

Measured context cost per request (system prompt + tool schemas, estimated at 4 characters per token, no
extensions or MCP servers loaded):

| Profile | Tools | Prompt | Tool schemas | Total |
|---|---|---|---|---|
| `core` | 4 | ~260 | ~700 | **~1,000** |
| `lean` | 7 | ~290 | ~1,240 | ~1,500 |
| `coder` | 14 | ~450 | ~1,930 | ~2,400 |
| `full` | 25 | ~1,500 | ~3,100 | ~4,600 |

`/capabilities` shows the live number for your setup, per module.

## Capabilities

Features (native modules, `src/pi/caps/`):

| id | Tools | What it does | Follows | Cost |
|---|---|---|---|---|
| `todo` | `todo` | Task list for multi-step work; stored in the session file, so it survives resume, fork and compaction | Hermes `todo`, Oh My Pi `todo_write` | ~135 |
| `ask` | `ask` | The model asks you a question with selectable options; headless runs get "decide yourself" | Oh My Pi `ask`, Hermes `clarify` | ~90 |
| `fetch` | `fetch` | Web page / API response as text, paged with `max_chars` + `offset` (default 12,000 characters) | Oh My Pi `fetch`, Hermes `web_extract` | ~115 |
| `memory` | `memory` | Two bounded stores injected into the prompt: `MEMORY.md` (2,200 chars) and `USER.md` (1,375 chars), in Hermes' file format | Hermes memory | ~160 + store |
| `task` | `task` | Subagent with a fresh context; only its final report returns to the parent | Oh My Pi `task`, Hermes `delegate_task` | ~150 |
| `skills-universal` | – | Also reads skills from `~/.claude`, `~/.hermes`, `~/.openclaw`, `~/.omp/agent`, `~/.codex` (and the project's `.claude` / `.openclaw` / `.hermes` when trusted); applies OpenClaw and Hermes gating | OpenClaw, Hermes, Agent Skills | listing only |
| `skill-authoring` | `skill_manage` | The agent writes and patches its own skills in `<agent-dir>/skills/` | Hermes `skill_manage`, OpenClaw skill-workshop | ~200 |
| `checkpoint` | `checkpoint`, `rewind` | Explore, then rewind: the exploration leaves the model's context and only the report stays (it remains in the session file and transcript) | Oh My Pi | ~200 |
| `plan` | `exit_plan_mode` | `/plan` makes the session read-only (tool gate) until you approve the model's plan | Oh My Pi plan mode | ~80 |
| `ast` | `ast_grep`, `ast_edit` | Structural search and rewrite through the `ast-grep` CLI; off and free when the program is not installed | Oh My Pi | ~300 |
| `web-search` | `web_search` | Titles, URLs and snippets through Tavily, Brave or Exa (with a key) or DuckDuckGo (no key) | Oh My Pi, Hermes | ~70 |
| `session-search` | `session_search`, `/search` | Find earlier conversations by text, across all projects | Hermes `session_search` | ~80 |
| `hashline` | replaces `read`, `edit` | `read` shows `[path#TAG]` + numbered lines; `edit` takes line-addressed patches (`PUT 3.=5:` + `+rows`). Opt-in: in no built-in profile | Oh My Pi hashline mode | ~480 (replaces ~390) |

Hosts for other people's code (also capabilities):

| id | What it runs |
|---|---|
| `hermes-plugins` | Hermes plugins (`plugin.yaml` + `register(ctx)`) from `~/.hermes/plugins`, the project's `.hermes/plugins` and Omni's `plugins/hermes`, in-process, with their tools, commands, prompt sections and hooks: `pre_tool_call` (veto), `post_tool_call`, `transform_tool_result`, `transform_terminal_output`, `pre_llm_call` (adds context), `post_llm_call`, `on_session_start` / `on_session_end`. Other registrations are accepted and listed by `/doctor` as not supported yet |
| `hooks` | Shell-command hooks in Claude Code's format (`PreToolUse`, `PostToolUse`, `UserPromptSubmit`, `SessionStart`, `Stop`) from `<agent-dir>/hooks.json` and the project's `.omni/hooks.json`. Exit code 2 blocks. Claude Code's own settings files are read only when the profile sets `"hooks": {"claude": true}` |

`memory` takes extra labeled blocks (Letta's core-memory blocks) per profile: `"memory": {"blocks": {"persona": 800, "project": {"limit": 1500, "description": "..."}}}`.

`task` accepts a `model`: a role from `modelRoles` in settings.json or `model_roles` in the profile (Oh My Pi's smol / slow / plan), or any model id.

`task` also runs **named agents**: markdown files with `name` / `description` / `tools` / `model` frontmatter in `<agent-dir>/agents`, the project's `.omni/agents` or `.pi/agents`, and (with `skills-universal`) `.claude/agents`.

| `claude-compat` | Claude Code's slash commands (`~/.claude/commands`, the project's `.claude/commands`), the project's `.mcp.json`, and Claude Code plugins (a folder with `.claude-plugin/plugin.json`: its `commands/`, `agents/`, `skills/`, `hooks/hooks.json`, `.mcp.json`). `/plugins` lists them. Your own prompts, skills and `mcp.json` win on a name clash |
| `scheduler` | `cronjob` tool, `/cron`, `omni cron list / run / remove`. Jobs (`every 2h`, cron expressions, `in 20m`, ISO times) run a prompt in a fresh unattended session and save the answer under `<agent-dir>/cron/output`. An open session checks every 30 s; `omni cron run` is for Task Scheduler / crontab |

| `openclaw-plugins` | OpenClaw plugins (`openclaw.plugin.json` + `definePluginEntry`) in the Node extension host: their tools, commands and the hooks `before_tool_call`, `after_tool_call`, `before_prompt_build`, `agent_end`, `session_start` / `session_end`. Channels, providers, services and gateway methods are accepted and listed as not supported yet. `/openclaw` shows what loaded. Tested with a plugin written for the test, not with OpenClaw's bundled plugins |

More feature modules:

| id | What it does | Follows |
|---|---|---|
| `diagnostics` | After each `write` / `edit`, a syntax check of the file (Python, JSON, TOML, YAML, JavaScript via `node --check`); problems are appended to the same tool result | Oh My Pi LSP feedback |
| `memory-reflect` | `/reflect`: one extra model call distils the conversation into core memory and the archive. `"memory": {"reflect_on_exit": true}` runs it when a longer session ends. Needs `memory` | Letta sleep-time agent, Oh My Pi `reflect`, Hermes nudges |

`tracing` also has `/rate up|down|<number> [comment]` (a Langfuse score on the last run), and `browser` has a `screenshot` action that returns an image.

A module can declare `requires`. One whose requirement is off is reported in `/capabilities` and `/doctor` with the command that fixes it; the profile wizard adds requirements, and removes dependents, for you.

| id | What it does | Follows |
|---|---|---|
| `approvals` | Asks before destructive shell commands (recursive deletes, force pushes, `sudo`, piping downloads into a shell, …) and edits outside the project. Answer in the UI, or from chat through the gateway (`yes` / `always` / `no`); with nobody to ask it is denied and the model is told. `/approvals` shows and resets the always-allow list. A guard against mistakes, not a security boundary | Hermes command approval, OpenClaw exec approvals |
| `code-exec` | `execute_code`: run a Python or JavaScript snippet in the project folder, with a timeout and capped output | Hermes `execute_code`, Oh My Pi `eval` |

**Your own modules.** Put a Python file that defines `CAPABILITIES = [Capability(...)]` in `<agent-dir>/capabilities/` (or a trusted project's `.omni/capabilities/`). It shows up in `/capabilities`, the profile wizard and `omni toggle` like a built-in one; built-in ids cannot be replaced.

**Hermes memory providers.** A Hermes plugin that calls `ctx.register_memory_provider(...)` gets its prompt block, per-message recall, turn sync and tools wired in (one external provider at a time, as in Hermes).

**Scheduled jobs and chat.** A job created in a gateway conversation reports back to that chat; `deliver: "telegram:<chat id>"` sets it explicitly.

**Profiles from the shell.** `omni profile new <name> --from lean --add todo,fetch --remove mcp`, `omni profile show <name>`, `omni profile delete <name>`.

Host switches (same commands, they flip profile keys):

| id | Controls |
|---|---|
| `extensions` | Node host for Pi / Oh My Pi extensions |
| `bundled-extensions` | extensions shipped in `plugins/pi` |
| `mcp` | MCP servers from `mcp.json` |
| `permission-gate` | confirmation before destructive tool calls |
| `plugins-hermes`, `plugins-openclaw`, `plugins-omni`, `plugins-mcp-legacy` | sources of Omni's plugin registry |

## Switching things on and off

```
/capabilities                 picker: [x]/[ ] per module, with its token cost; Enter toggles
/capabilities memory on       direct
omni capabilities             list, outside a session
omni toggle memory off
omni --capabilities todo,ask  exact list for one run ('' = none); ignores profile and overrides
```

A toggle is saved to `<agent-dir>/omni-capabilities.json` and applies on top of **every** profile. Setting a
capability back to what the profile already says removes the override.

## Custom profiles

```
/profile new                  wizard: pick a base profile, tick modules and hosts, name it
/profile edit my-setup        same picker on an existing custom profile
/profile show my-setup        what a profile is made of
/profile delete my-setup      custom profiles only
```

The wizard writes a JSON file to `~/.omni/profiles/`; nothing changes until the last step.

```
/profile save my-setup        writes ~/.omni/profiles/my-setup.json from the current state
/profile my-setup             switch to it
```

Or write the file yourself. `capabilities` either replaces the inherited list or edits it:

```json
{
  "name": "research",
  "extends": "lean",
  "description": "Read-mostly research setup",
  "capabilities": ["fetch", "task", "memory"],
  "skills_budget_chars": 4000,
  "mcp": false
}
```

```json
{ "name": "coder-quiet", "extends": "coder", "capabilities": ["-ask", "+memory"] }
```

Profile keys: `tools`, `capabilities`, `pi_extensions`, `omni_plugins_pi`, `omni_registry`, `permission_gate`,
`mcp`, `skills_budget_chars`, `skill_dirs` (extra folders for `skills-universal`), `memory.dir`, `prompt_append`,
`accent`, `description`.

## Ideas taken from other open-source projects

Each of these is a capability, built on the Python standard library (the browser one also needs `websockets`).
The projects themselves are cloned under `D:\Harness\references` for reading; none of their code runs here.

| id | Project it follows | What it does here | In profiles |
|---|---|---|---|
| `tracing` | Langfuse | Every run is a trace of generations (model, tokens, cost, latency) and tool spans. Written to `<agent-dir>/traces/<session>.jsonl`; sent to a Langfuse server when `LANGFUSE_PUBLIC_KEY` / `LANGFUSE_SECRET_KEY` are set. `/trace` shows the last run. `"tracing": {"content": false}` keeps text out | assistant, autonomous, full |
| `memory-archive` | Letta (MemGPT) | Archival memory: `archival_memory_insert` / `archival_memory_search` over an unbounded SQLite full-text store. Only a one-line count is in the prompt. Together with `memory` (core) and `session-search` (recall) this gives Letta's three tiers | assistant, autonomous, full |
| `durable` | Temporal | Run markers in the session file detect a run that was cut off; `/resume-run` continues it without repeating finished tool calls, and unfinished calls get an "interrupted, effect unknown" result. Transient failures of read-only tools are retried with backoff | assistant, autonomous, full |
| `browser` | Browser Use | One `browser` tool. Pages are shown as numbered interactive elements (`[3]<button>Sign in</button>`) and the model acts on the number; every action returns the new page state | full |
| `sandbox` | Daytona | `bash` runs in a per-session Docker / Podman container with the project mounted at `/workspace` and the network off; removed when the session ends. `/sandbox` shows it. Off and free without a container runtime | none (opt-in) |

Not taken over: Temporal's server, workers and multi-language SDKs; Letta's agent server, sleep-time agents and
vector search; Browser Use's vision mode and cloud browsers; Daytona's snapshots, remote workspaces and API;
Langfuse's prompt management, evaluations and dashboards.

## Gateway: Omni from chat apps

`omni gateway` is a long-running frontend (OpenClaw's gateway, Hermes' messaging platforms). Each conversation gets
its own session and session file, so context survives restarts and chats never mix.

```json
{ "profile": "assistant", "cwd": "D:/work/notes",
  "channels": { "telegram": {"token": "$TELEGRAM_BOT_TOKEN", "allow": [123456789]},
                "http":     {"port": 18790, "token": "choose-a-secret"} } }
```

That is `<agent-dir>/gateway.json`. Only senders in `allow` are answered; anyone else gets a one-time pairing code
and `omni gateway approve <code>` lets them in. The HTTP channel (`POST /message {"chat", "text"}` with a bearer
token) listens on 127.0.0.1 only. In a chat: `/new`, `/status`, `/stop`. More channels can be added from Python
with `gateway.register_channel(name, factory)`. `omni gateway status` shows the configuration.

## Proactive and self-improving

- **Heartbeats** (OpenClaw): a scheduled job created with `heartbeat: true` reads `HEARTBEAT.md` in its folder and stays silent (no file, no chat message) when the model answers `HEARTBEAT_OK`.
- **Signals** (Temporal): a job with the schedule `on <name>` waits until `omni signal <name> [text]`, the `cronjob` tool (`action: signal`) or `POST /signal` on the gateway's HTTP channel sends that signal, then runs with the text added to its prompt. `on <name> repeat` runs on every signal.
- **Self-evolution** (Hermes, `self-evolve`): `/evolve run` or `omni evolve run` studies recent sessions and proposes new skills, skill corrections and memory entries. Nothing is applied until `/evolve apply <id>`; a corrected skill keeps its previous text as `SKILL.md.bak`.
- **Evaluations** (Langfuse): `omni eval cases.jsonl --profiles core,coder --models a,b` runs the same prompts under each variant and reports pass rate, tokens, cost and time. Cases are JSONL with `prompt` and an optional `expect` (text, `contains`, `not_contains`, `regex`, `tools`).

## Navigation, usage and small extras

- **`code-nav`** (Oh My Pi `lsp`, navigation part): `code_nav` with `outline(path)`, `definition(name)` and `references(name)`. Exact for Python (parsed with `ast`); pattern-based for other languages. Not a language server: no types, no rename.
- **Usage report** (Langfuse dashboards): `omni usage [--days N] [--html report.html]` and `/usage` add up the local traces: runs, tokens and cost per day, by model, slowest tools, your ratings.
- **Shared memory for subagents** (Letta shared blocks): with `memory` on, a `task` subagent reads the parent's core memory; it cannot change it.
- **Browser**: `wait(text?, seconds?)` until text appears. **Sandbox**: `/sandbox snapshot [name]` saves the container as an image you can start from.

## Rules

`rules` (Oh My Pi's rules and time-travelling stream rules) reads markdown files from `<agent-dir>/rules/` and, in trusted projects, `.omni/rules/`, `.pi/rules/`, `.omp/rules/`. A rule with `alwaysApply: true` is in the prompt; a rule with a `condition` (a regular expression) is not in the prompt at all and is sent to the model as a correction when its reply or a tool call's arguments match, once per session; any other rule is listed by name and read on demand. `/rules` shows them and what fired. The check runs when the model's message is complete, not mid-stream as in Oh My Pi.

The gateway also has a send-only **webhook** channel (`{"type": "webhook", "url": "$SLACK_WEBHOOK_URL", "format": "slack" | "discord" | "json"}`) for delivering scheduled-job results, and `omni doctor` prints the `/doctor` report without opening the window.

## Guard rails and conveniences

- **`budget`**: `"budget": {"session_usd": 2, "run_tool_calls": 60, "run_minutes": 20}`. At a limit the next tool call is refused and the model reports where it is; `/budget` shows the counters, `/budget extend` continues. Defaults: 80 tool calls and 30 minutes per run.
- **`github`** (Oh My Pi): a read-only `github` tool over the `gh` CLI (PRs, diffs, checks, issues, CI runs, search).
- **`workflows`**: `/review`, `/commit`, `/explain`, `/test`, `/fix`, `/catchup` as bundled prompts; your own prompt with the same name replaces one.
- **`notify`**: terminal bell and desktop notification when a run of 20 s or more finishes in the window.
- **`task`** accepts `tasks: [...]` to run up to 6 independent subagents at the same time. **`durable`** takes per-tool retry settings (`"durable": {"tools": {"fetch": {"retries": 4}}}`). **`browser`** has a `key` action.

## Startup time and health

- The window opens as soon as the session, model and built-in tools are ready (about half a second); extensions, plugin hosts and MCP load in the background. A message sent meanwhile runs as soon as they are in.
- Compiled TypeScript for extensions is cached on disk (`OMNI_TS_CACHE=0` turns it off).
- `/extensions slow` ranks extensions by load time and by time spent in their event handlers.
- `/doctor` shows sign-ins, modules, extension failures, Hermes plugins, hooks, MCP and recent warnings on one screen.

## Keeping context small

- `core` adds nothing beyond Pi's prompt and four tool schemas.
- Each module states its cost; `/capabilities` sums them.
- `memory` is hard-capped (3,575 characters total) and snapshotted per session, so the provider's prompt cache stays valid.
- `fetch` pages its output; `task` returns at most 12,000 characters of report and keeps the subagent's tool output out of the parent.
- `skills_budget_chars` shortens skill descriptions in the listing when a shared library gets large; skill bodies are read on demand.
- MCP tools can be deferred behind `tool_search` (existing behaviour, see `PI_PROFILE.md`).

## Writing a capability

```python
# src/pi/caps/hello.py
from ..tools.base import ToolDef, text_result
from . import Capability

def _tools(session):
    return {"hello": ToolDef(name="hello", label="Hello", description="Say hello.",
                             parameters={"type": "object", "properties": {}},
                             execute=lambda _id, p, signal=None, on_update=None, ctx=None: text_result("hello"))}

CAPABILITIES = [Capability("hello", "Hello", "Example module.", tools=_tools)]
```

Add `"hello"` to `_BUILTIN_MODULES` in `src/pi/caps/__init__.py` (or call `caps.register(...)` from your own
Python). A capability may provide `tools`, a `prompt` section, `skill_roots`, a `skill_filter`, and an `available`
check, `commands`, an `after_tool` and `on_prompt` hook, an `unload`, a `gate` (block a tool call with a reason; plan mode uses it) and a `live_prompt` (re-read every request); a module that raises while loading is reported in `/capabilities` and skipped.

## Status against the four harnesses

Done means covered by an offline test in `tests/`.

| Area | Done | Not done yet |
|---|---|---|
| **Pi** | core loop, 7 tools, sessions, TUI, extensions host, MCP, packages, codemode, OAuth sign-ins (see `PI_PROFILE.md`) | keybindings.json, sixel images |
| **Oh My Pi** | extensions load through the same host; `todo`, `task`, `ask`, `fetch`, `checkpoint` / `rewind`, plan mode, `ast_grep` / `ast_edit` (via the CLI), hashline edits (ranges, inserts, cut/paste registers, create / delete / move), `web_search`, named agents for `task`; OAuth flows ported | hashline syntax-block ops (`N*`), `lsp`, `github`, `eval`, model roles, TTSR, snapcompact, the remaining slash commands, its extra extension API |
| **Hermes** | `memory` (file-compatible), `todo`, `delegate_task` equivalent, `skill_manage`, `session_search`, `web_search`, skill gating by `platforms`; plugin host with tools, commands, prompt sections and the eight core hooks | the other plugin kinds (memory providers, context engines, platforms, image / video generation), streaming and API-request hooks, messaging platforms, cron delivery to chat channels, self-evolution job |
| **OpenClaw** | skills with `metadata.openclaw` gating; plugins through the legacy adapter | the rest of the plugin SDK (channel, provider and service plugins), more gateway channels, exec approvals, nodes, ACP |

The order for the remaining work is in `INTEGRATION_PLAN.md` (phases 3-5).

Beyond the four: shell hooks and named agents follow Claude Code's formats, so its hook scripts and `.claude/agents` files can be reused.
