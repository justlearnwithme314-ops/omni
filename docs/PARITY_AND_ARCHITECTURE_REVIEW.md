# Omni — Parity & Architecture Review

*Audit date: 2026-11-09 · Scope: how Omni implements features from **Pi**, **Oh My Pi (OMP)**, **Hermes** and **OpenClaw**
· Goal: a feature-rich, extensible, modular harness that is compatible with the others.*

This is a **work plan**, not a changelog. Every finding has an ID, the **evidence** behind it, the **fix**, and an
**acceptance test**, so items can be picked up independently. Nothing in this file has been implemented yet.

> **How to read the evidence.** Numbers below were measured against the upstream checkouts in `D:\Harness`
> (`oh-my-pi`, `hermes-agent`, `openclaw`) and against Omni itself on the audit date, either by static analysis
> or by actually running the code. Items marked **(observed)** were reproduced by running Omni;
> items marked **(static)** come from reading/grepping the source; **(unverified)** is my inference and should be
> confirmed before acting on it.

---

## 0. Verdict

The **foundation is sound** (config/paths, event bus, adapter base, a working MCP stdio client, a working CDP relay,
shadow worktrees, isolated test harness). But **most of the "feature parity" I previously reported is surface-level**:
catalog entries and command names exist, while the behaviour behind them is missing, shallow, or semantically wrong.
Three problems matter most:

1. **The command catalog is inflated and partly fake.** ~70 entries are upstream *sub*command names listed as
   top-level commands, and core commands such as `/new`, `/clear`, `/reset`, `/resume`, `/export` answer
   "Unknown slash command" while autocomplete suggests them. (§P0-2)
2. **Compatibility layers are shallow or wrong.** Pi extensions get 7 of 33 API methods and none of the 44 events;
   OpenClaw skills are modelled as shell-executing tools, which is the opposite of how they work upstream;
   Hermes plugins get no hooks. (§P0-1, §P1-5, §P1-6)
3. **The agent loop is not a full harness yet.** No streaming, retries, cancellation, prompt caching or thinking
   parameters; no `edit`/`grep`/`find`/`ls`/web/todo/task tools; no session persistence or resume. 25 of 41 "feature"
   modules are not reachable from a normal chat turn. (§P0-3 … §P0-6)

### Scorecard — earlier claim vs. verified

| Area | What I previously reported | Verified reality |
|---|---|---|
| OMP slash commands | "170+ commands implemented" | OMP has **88** real top-level builtin commands. Omni catalogs 87 of them but **handles 38**; **~70 other catalog entries are upstream subcommand names** (`add`, `all`, `append`, `cancel`, `done`, `drop`…) |
| Pi core commands | "Pi profile complete" | 19 upstream · 18 cataloged · **6 handled**. `/new` `/resume` `/fork` `/export` `/copy` `/settings` `/login` unhandled |
| OpenClaw chat commands | "all 48 wired" | 48 upstream · 47 cataloged · **35 handled** (missing `new reset restart update usage login loop context fork acp commands models reasoning`) |
| Hermes commands | "Hermes profile" | 105 upstream · 58 cataloged · **37 handled** |
| CLI subcommands | "all 69 routable" | 141 cataloged · **12 implemented** in `cli.py` (`commands evolve keys login mode model models plugins profile run stats toggle`) |
| Pi extensions | "14 extensions, compatible" | **5 of 13** packages load; the loaders that "succeed" register ~0 tools; **7 fail** on missing deps / TS features |
| Pi extension API | "Node IPC bridge" | **7 of 33** `ExtensionAPI` methods, `pi.on()` is a no-op, **0 of 44** events fire, no `ctx.ui` (29 methods upstream) |
| OpenClaw skills | "28 skills active as tools" | Wrong model: upstream lists skills in the prompt (`<available_skills>`) and reads them on demand; Omni *executes* the first code line of the markdown |
| Hermes plugins | "plugin ecosystem supported" | `plugin.yaml` + `register(ctx)` shape matches; **0 of 14 hooks** dispatched; thin `ctx` |
| TTSR / speculation / MoA / egress / budget | "implemented" | Modules exist but are **not on the agent path** (slash-command only, or only imported by tests) |
| "Rust native acceleration" | "native speed" | Pure-Python `difflib` + `re` (compiled library is never loaded) |
| MCP | "MCP support" | **Works for stdio** (verified round-trip). No HTTP/SSE, resources, prompts, reconnect |

### What is solid (verified)

- Browser relay: real CDP over WebSocket, tested against headless Chromium (eval, DOM snapshot, click, fill, console, screenshot).
- MCP **stdio** client: `initialize → tools/list → tools/call` round-trip against a real server process.
- Shadow worktrees: real `git worktree`, carries uncommitted changes, merges only on passing tests.
- Docker sandbox: fails closed; hardened `docker run` flags (command construction tested, daemon not available here).
- Collab/canvas servers: loopback bind, key/token auth, view-only rooms, CSP-sandboxed HTML.
- Hermes `<tool_call>` XML parser (`parse_hermes_tool_calls`), `plugin.yaml` + `register(ctx)` loading.
- Paths/config layering (`src/paths.py`, `OMNI_*` overrides) and the isolated test harness (`tests/_isolation.py`).
- Router has fallback/cooldown logic **(static; not exercised in this audit)**.

---

## 1. Target architecture (what "extensible, modular, compatible" should mean)

The current structure grew feature-by-feature. To make compatibility a *property* rather than a pile of adapters,
organise around **one canonical model** and keep every foreign format at the edge.

```
 Frontends        REPL/TUI · RPC/JSON · Gateway daemon (chat channels) · CLI
                          │
 Kernel           Session store ─ Agent loop ─ Event bus (superset taxonomy) ─ Middleware pipeline
                          │
 Capability       Tools · Commands · Skills · Providers · Context sources · Channels · Themes/Renderers · Hooks
 registries       (each: register / unregister / list / replace / enable-by-profile, with provenance)
                          │
 Compat adapters  Pi/OMP host · Hermes host · OpenClaw skills+channels · MCP · Claude skills/AGENTS.md importers
                  (translate foreign manifests/APIs → canonical capabilities; run foreign code out-of-process)
```

Design rules to follow in every fix below:

1. **One source of truth per concept.** A command is defined once (handler + metadata via decorator); help, autocomplete,
   profiles and docs are *generated* from the registry. No hand-written catalog beside the handlers.
2. **Features are middleware, not modules.** TTSR, output budgeting, egress firewall, compaction, speculation, checkpoints,
   memory recall are ordered middleware on the loop, enabled by profile/config. A feature that is not on the loop is not a feature.
3. **Everything foreign runs behind an adapter that declares what it supports** (capability negotiation), so "partially
   compatible" is explicit and testable rather than silent.
4. **Profiles are data** (`profiles/*.yaml`: which capabilities, defaults, keybinds, theme), not code. `pi`, `omp`, `hermes`,
   `openclaw`, `full`, plus user-defined.
5. **Layered settings**: built-in defaults < user `~/.omni` < project `.omni/` < env (`OMNI_*`) < CLI flags; importers can
   read `~/.pi/agent/settings.json`, `~/.omp/agent/config.yml`, `.claude/settings.json`, `.mcp.json`.
6. **Conformance tests against upstream fixtures** (pinned versions of real Pi/Hermes/OpenClaw plugins and command lists).
   "Compatible" means "passes the fixture suite", reported as a percentage.

### 1.1 Canonical event taxonomy (superset; map foreign events onto it)

Omni's bus has about nine named hooks (`before_turn`, `before_llm`, `after_llm`, `before_tool_exec`, `after_tool_exec`,
`on_error`, `on_session_start/end`). Upstream needs far more:

| Omni canonical event (proposed) | Pi / OMP event | Hermes hook |
|---|---|---|
| `session.start` / `session.shutdown` / `session.switch` / `session.compact` / `session.tree` | `session_start`, `session_shutdown`, `session_switch`, `session_compact`, `session_tree`, `session_branch` | — |
| `agent.start` / `agent.end` / `turn.start` / `turn.end` | `agent_start`, `agent_end`, `before_agent_start` | — |
| `input.received` (can rewrite/handle) | `input` | `message` |
| `context.build` (can add/trim messages) | `context` | `pre_llm_call` |
| `llm.request` / `llm.response` / `llm.stream.{start,delta,end}` / `llm.retry` | `after_provider_response`, `message_start/update/end`, `auto_retry_*`, `retry_fallback_*` | `pre_llm_call`, `post_llm_call`, `on_stream_start/delta/end`, `transform_llm_output` |
| `tool.call` (can block/modify) / `tool.result` (can transform) | `tool_call`, `tool_execution_start/end`, `tool_result` | `pre_tool_call`, `post_tool_call`, `transform_tool_result`, `transform_terminal_output` |
| `approval.requested` / `approval.resolved` | `tool_approval_requested/resolved` | `action`, `continue` |
| `compaction.start/end` | `auto_compaction_start/end` | — |
| `mode.changed`, `goal.updated`, `todo.reminder`, `plan.review.*` | `mode_changed`, `goal_updated`, `todo_reminder`, `plan_review_*` | — |
| `resources.discover` | `resources_discover` | — |

(Pi/OMP lists above are a subset of the 44 events in `extensibility/extensions/types.ts`; Hermes list is `VALID_HOOKS` in
`hermes_cli/plugins.py`.)

---

## 2. Findings

Severity: **P0** wrong/unsafe/blocks usability · **P1** missing core harness capability · **P2** compatibility depth ·
**P3** polish. Effort: **S** < 1 day · **M** 1–4 days · **L** > 4 days.

### P0 — Correctness and integrity

#### P0-1 · OpenClaw skills are modelled as executable tools (and execute documentation)  — Effort M

- **Evidence (observed):** `OpenClawAdapter._extract_command` (`src/adapters/openclaw.py`) returns, in order, a `command:`
  frontmatter key, a `run:`/`command:` line in the body, **the first line of the first code fence**, or any line containing
  `{baseDir}`. Executing the registered tool `autoresearch` produced
  `'ask_user' is not recognized as an internal or external command` — a documentation snippet was run as a shell command.
  `discover()` also scans `plugins/pi` and `skills/` for `SKILL.md`; only `calculator` ships a script (1 of 28 is runnable).
- **Upstream (static):** `openclaw/src/agents/system-prompt-skills.ts` injects an `<available_skills>` list into the system
  prompt ("Scan `<available_skills>` for a matching workflow") and the model reads the full `SKILL.md` on demand
  (`skills.read` / `skills_search`). Skills are instructions, not callable functions.
- **Fix:**
  1. New `Skill` capability: `{name, description, location, frontmatter, body_loader}`; **never** auto-execute markdown.
  2. Context middleware renders a compact `<available_skills>` block (name + description + path) into the system prompt;
     add a `read_skill(name)` tool (and `skills_search`) for progressive disclosure.
  3. Honour frontmatter: `user-invocable`, `disable-model-invocation`, `allowed-tools`, `command-dispatch: tool` +
     `command-tool` (the *only* sanctioned way a skill maps to a tool), `metadata.openclaw.{requires,os,install}` gating.
  4. Search roots: `<project>/.omni/skills`, `~/.omni/skills`, plus importers for `.claude/skills`, `.agents/skills`,
     `~/.openclaw/skills`, `~/.pi/agent/skills`.
  5. Remove `_extract_command` entirely.
- **Acceptance:** a prompt-only skill appears in the prompt and is **not** in the tool list; running it never spawns a
  process; `read_skill` returns the markdown; a skill with `command-dispatch: tool` dispatches to the named tool.

#### P0-2 · Command catalog integrity: fake entries, unhandled entries, two sources of truth — Effort M

- **Evidence (static+observed):** 223 unique catalog commands, **108 with a real handler (48%)**. By category:
  `core 10/15`, `hermes 16/17`, `omp 37/136 (27%)`, `openclaw 42/50`, `pi 3/5`.
  Of OMP's 88 true top-level commands, 38 are handled; **~70 catalog entries are subcommand/argument names** from
  `slash-commands/builtin-*.ts` (e.g. `add all anon append cancel collapse compare configure diagnose disable discover
  disposition done drop edit elide enable expand full headless images import info install installed kill last`).
  Observed in the real REPL: `/new /resume /fork /export /copy /settings /clear /reset /login /changelog /hotkeys /context
  /usage` → `[!] Unknown slash command`, yet they are in the catalog and autocomplete. Omni invents 8 "omp" commands that
  do not exist upstream (`astgrep benchmark diff eval lsp mm split worktree`) — fine as Omni-native, but they are filed as OMP.
  `commands.py` is a 228-entry hand-written literal; handlers live in `commands_executor.py` + `handlers/` (2 sources of truth).
- **Fix:**
  1. Replace the literal catalog with decorator-registered commands:
     `@command("compact", category="omp", origin="omp", usage="/compact [focus]", status="ready")`.
  2. `/help`, autocomplete, profiles and docs are generated from the registry. Unimplemented commands are either absent
     or `status="planned"` and **hidden** from autocomplete/help (shown only with `/help --all`).
  3. Delete the ~70 subcommand entries; model subcommands as `subcommands=[...]` metadata for completion only.
  4. Add `origin` metadata (`pi|omp|hermes|openclaw|omni`) and **aliases** so the same command can exist under several
     harness names (e.g. `/branch` ↔ `/fork`).
  5. Parity manifests (`tests/fixtures/parity/{pi,omp,hermes,openclaw}.json`, generated from upstream) drive a test that
     reports `implemented / total` per harness.
- **Acceptance:** `catalog ⊆ handlers` (test fails otherwise); `/foo` for any advertised command never says "Unknown";
  parity report prints per-harness percentages; OMP top-level coverage ≥ 90%.

#### P0-3 · Provider layer is a thin non-streaming client — Effort L

- **Evidence (static):** `providers/client.py` (875 lines) has **0** occurrences of `stream`, `text/event-stream`, `retry`,
  `backoff`, `Retry-After`, `cache_control`, `thinking`, `reasoning_effort`, `tool_choice`, `parallel_tool`, `image`.
  Three wire formats (`openai_compatible`, `anthropic`, `google_antigravity`); router references 8 providers
  (anthropic, openai, google, google-antigravity, deepseek, mistral, ollama, local). No way to add a provider without
  editing `client.py`. Consequence: **`/think high` and the "thinking: high" toolbar are cosmetic** — nothing is sent.
- **Fix:** `Provider` plugin interface
  `{id, models(), stream(request) -> AsyncIterator[Event], complete(), supports: {tools,vision,thinking,cache,json}}`;
  SSE streaming for each wire format; retry with exponential backoff + jitter honouring `Retry-After`; cancellation via
  `asyncio.CancelledError` propagating to the HTTP request; prompt-cache breakpoints (Anthropic `cache_control`,
  OpenAI/Google automatic) keyed to the byte-stable prefix Omni already maintains; thinking/effort mapped per provider;
  image input; `tool_choice`; usage + cost accounting per turn; register new providers from extensions
  (Pi `registerProvider` / OMP model catalog). Add OpenRouter, Groq, xAI, Bedrock, Azure, Vertex, Codex, Copilot via the
  OpenAI-compatible path with a catalog (YAML) instead of code.
- **Acceptance:** tokens render incrementally in the REPL; Ctrl-C aborts mid-stream; a forced 429 retries with backoff;
  `/think high` changes the outgoing request; a provider can be added from a plugin file.

#### P0-4 · Native toolset is too small for a coding harness; shell is ungated — Effort L

- **Evidence (observed):** built-in tools: `read_file write_file execute_bash system_diagnostic browser_cdp lsp_query
  canvas_render remote_node_task clear_input_history` (9). OMP ships: `read write edit bash grep find glob ast_grep ast_edit
  lsp todo task ask web_search fetch checkpoint rewind github eval think memory(recall/reflect/retain) manage_skill
  security_scan …`; Pi: `read write edit bash grep find ls`. Hermes has ~298 modules under `tools/`.
  Omni has **no `edit`** (whole-file rewrites only), **no grep/find/glob/ls**, no web fetch/search, no todo, no ask-user,
  no `task`/subagent tool, and `ast_grep`/`checkpoints`/`memory_graph` exist only behind slash commands.
  `permission_gates` is **absent from `DEFAULT_FEATURES`** (off unless a config enables it); `execute_bash` runs on the
  host and does not use the Docker sandbox.
- **Fix:** implement the Pi core set first (`read write edit bash grep find ls`) with the same names/schemas so Pi prompts and
  skills work unchanged; then OMP additions (`glob ast_grep ast_edit todo ask task fetch web_search checkpoint rewind lsp`).
  `edit`: exact-match replace with fuzzy-whitespace fallback and a diff result (see OMP `hashline`/edit tools). Put every
  mutating tool behind the **approval middleware** (default **on**); route `bash` through an `ExecutionBackend`
  interface (`local`, `docker`, later `ssh`) selected by profile.
- **Acceptance:** an agent can read, search, patch and run tests in a repo with only built-in tools; a denied approval
  blocks execution; `bash` under the `docker` backend never touches the host.

#### P0-5 · Agent loop gaps — Effort L

- **Evidence (static):** `engine.py` (782 lines): sequential tool execution (no `asyncio.gather`), no cancellation/abort
  (`CancelledError`/`KeyboardInterrupt` never handled), **no session persistence** from the loop, no retry, no streaming,
  `run_turn` is 250 lines. Default system prompt is built inside the engine; **no `AGENTS.md`/`CLAUDE.md`/`SYSTEM.md`
  loading anywhere** (grep over `src/` and `plugins/omni`).
  Observed: `/new`, `/clear`, `/reset`, `/resume`, `/fork`, `/tree` do not function (see P0-2).
- **Fix:** (a) Session store: append-only JSONL tree with branch/fork/resume/`/tree`, compatible with Pi/OMP session files
  (import/export); (b) parallel execution of independent tool calls (read-only tools concurrent, mutating serialized);
  (c) interrupt/abort that cancels the in-flight provider call and tool subprocesses and leaves a consistent transcript;
  (d) context builder middleware that merges: base prompt + profile prompt + project context files + skills block +
  memory recall; (e) auto-compaction trigger by token budget using the existing SnapCompact; (f) split `run_turn` into
  `build_context → call_model → handle_tool_calls → finalize`.
- **Acceptance:** kill the REPL mid-conversation and `/resume` restores it; two read-only tool calls run concurrently;
  Ctrl-C leaves a valid transcript; an `AGENTS.md` in the project root is reflected in the prompt.

#### P0-6 · Features not connected to the agent loop — Effort M

- **Evidence (static, import-graph reachability from `engine` + `router` + `registry`):** **25 of 41** feature modules are
  not on the agent path: `ast_grep audio_chimes branching checkpoints cron egress environments gateway gateways installer
  memory_graph mental_models moa native_accel output_budget persistence petdex prewalk sandbox speculation spinners
  split_pane themes ttsr webhooks`. Never imported anywhere: `evolution.constraints`, `evolution.tournament`,
  `features/gateway.py` (531 lines, superseded?). Imported only by tests: `native_accel petdex ttsr webhooks`.
  So "TTSR", "egress firewall", "output budgeting", "speculative execution", "MoA", the "webhook receiver on 18789" and
  the "genetic tournament" do not affect a chat turn.
- **Fix:** decide per module — **wire as middleware** (`ttsr`, `output_budget`, `egress`, `speculation`, `checkpoints`,
  `memory_graph` recall, `prewalk`, `mental_models` context source, `persistence`, `moa` as an optional "advisor" tool) or
  **delete**. Remove true orphans. Add a startup `/doctor` that lists each feature as `active | available | inactive` and why.
- **Acceptance:** an import-graph test fails if a module under `features/` is neither middleware, a registered
  capability, nor explicitly marked `experimental`; `/doctor` output matches reality.

### P1 — Missing core harness capabilities

#### P1-1 · Gateway / channels (OpenClaw) — Effort L
- **Evidence:** only Telegram long-polling receives messages; Discord/Slack/WhatsApp are send-only; **no agent router is
  attached** (inbound messages get a placeholder reply); remote node dispatch is explicitly `not_implemented`.
  OpenClaw's gateway is a daemon with durable ingress queues, per-channel sessions, routing/bindings, allowlists, DM pairing,
  cron, and a WebSocket control API (`src/gateway`, `src/channels`, `src/daemon`, `src/cron`).
- **Fix:** a `Channel` capability (`start/stop/send/receive`, `capabilities`, `account` config) + a `GatewayDaemon` that owns
  sessions-per-conversation, a durable inbound queue, allowlist/pairing policy, and the agent router
  (`services.agent_router` already exists as a hook). Implement Discord (gateway WS), Slack (Socket Mode), WhatsApp
  (Cloud API webhooks) as separate channel plugins. Node pairing needs a real transport (companion connects back over WS).
- **Acceptance:** a Telegram message reaches the agent and the reply returns; allowlist blocks unknown senders;
  sessions survive daemon restart.

#### P1-2 · MCP depth — Effort M
- **Evidence:** stdio works (**observed**). Static: no HTTP/SSE/streamable-HTTP transport, no `resources/*`, `prompts/*`,
  no `notifications/tools/list_changed`, no reconnect, no `.mcp.json` discovery (only servers listed under `plugins/mcp`),
  tool names are not namespaced.
- **Fix:** transports (stdio, streamable-HTTP, SSE), resources and prompts surfaced as `read_resource` tool and prompt
  commands, `list_changed` refresh, reconnect/backoff, `mcp__<server>__<tool>` naming (matches OMP/Claude), OAuth for
  remote servers, per-server enable/disable and timeouts; discover `.mcp.json` (project), `~/.omni/mcp.json`, and import
  Claude Desktop / OMP / Pi `mcp` configs.
- **Acceptance:** an HTTP MCP server and a stdio server both load; a server's tool list updates live.

#### P1-3 · Context & instruction files — Effort S/M
- **Evidence (static):** no support for `AGENTS.md`, `CLAUDE.md`, `SYSTEM.md`/`APPEND_SYSTEM.md`, prompt-template directories,
  `.claude/skills`, `.agents/skills`, `settings.json`, `.mcp.json`, themes dir.
- **Fix:** `ContextSource` capability; defaults load `AGENTS.md` (walk up from cwd + `~/.omni/AGENTS.md`), fall back to
  `CLAUDE.md`; support `@include`; prompt templates from `prompts/` as slash commands (`/name args`); user theme files.
- **Acceptance:** drop an `AGENTS.md` in a repo and the agent follows it; `/context` shows each source and its token cost.

#### P1-4 · Remaining CLI surface — Effort M
- **Evidence:** 141 subcommands cataloged, **12** implemented; `src/cli.py` and `src/cli/` (package) both exist; two REPL
  implementations (`src/repl.py` 167-line `run_rich_repl`, `src/cli/repl.py` 174-line).
- **Fix:** keep one entry point and one REPL; generate CLI subcommands from the same command registry
  (`omni <command>` ≡ `/command` where meaningful), add `--print/-p` non-interactive and `--json`/RPC modes (Pi/OMP parity),
  and delete the catalog-only subcommands.
- **Acceptance:** `omni -p "prompt"` prints the answer and exits; `omni rpc` speaks a documented JSON protocol.

### P2 — Compatibility depth

#### P1-5 / P2-1 · Pi / OMP extension host is a stub — Effort L
- **Evidence (observed + static):** real `ExtensionAPI` (`packages/coding-agent/src/extensibility/extensions/types.ts`)
  has **33 methods** (`registerTool registerCommand registerShortcut registerFlag registerProvider registerMessageRenderer
  sendMessage sendUserMessage setActiveTools setModel exec appendEntry getThinkingLevel …`) and **44 events**; the UI
  context has **29 methods** (`select confirm input notify setWidget setStatus setHeader …`). Omni's `pi_runner.js` implements
  `registerTool registerCommand registerProvider registerFlag on emit off` — and `on(event, handler) {}` **stores nothing**.
  Load test on the 13 packages in `plugins/pi`: **5 OK, 7 error, 1 n/a**; the OK ones expose 0–1 commands and 0 tools
  (likely because tools are registered inside `session_start` handlers that never fire — *unverified*). Errors:
  `Cannot find package '@earendil-works/pi-tui'`, `'@earendil-works/pi-coding-agent'`, `'@juicesharp/rpiv-config'`, `'p-limit'`,
  a missing native module, and `TypeScript parameter property is not supported in strip-only mode`.
- **Fix:**
  1. Real module resolution: per-extension dependency install (or a shared `node_modules`), and **module aliasing** for
     `@mariozechner/pi-*`, `@earendil-works/pi-*`, `@oh-my-pi/pi-*` → Omni-provided shims.
  2. TypeScript via a real transpiler (esbuild/jiti/tsx), not Node's strip-only mode.
  3. Implement the host: store handlers per event, fire from the canonical event bus (§1.1), implement the full
     `ExtensionAPI` surface (start with `registerTool/Command/Shortcut/Flag/Provider`, `sendMessage`, `exec`, `getActiveTools`,
     `setActiveTools`, `appendEntry`) and a `ctx.ui` shim that maps to the REPL (select/confirm/input/notify/status/widget).
  4. Tool `execute` must receive `(toolCallId, params, signal, onUpdate, ctx)` like Pi, not just `args`.
  5. Report per-extension compatibility (`full | partial: missing X | unsupported`) in `/plugins`.
- **Acceptance:** ≥ 90% of the packages in `plugins/pi` load; `pi-keyrouter`, `pi-web-ui`, `rpiv-todo`, `rpiv-ask-user-question`
  run end-to-end; unsupported APIs are listed instead of silently ignored.

#### P1-6 / P2-2 · Hermes plugin & feature depth — Effort L
- **Evidence:** `plugin.yaml` + `register(ctx)` shape matches (**observed**, `plugins/hermes/crypto_helper`), but the adapter
  has no hook dispatch; Hermes defines **14 hooks** (`pre_tool_call post_tool_call transform_terminal_output
  transform_tool_result transform_llm_output pre_llm_call post_llm_call on_stream_* on_interim_message action continue
  message`), plugin `kind:` values (e.g. `backend`) and `provides_*` keys, plus bundled plugin families
  (`browser memory context_engine cron_providers platforms delegation kanban image_gen web …`).
  Omni's Hermes-flavoured slash commands are shallow: `/subagents`, `/agents`, `/security` return **static text** with no
  subsystem call; `/curator` regex-extracts "facts" into the memory graph; `/evolve`, `/insights`, `/skills`, `/compress`
  have **no handler**. Hermes' defining capabilities — persistent memory used on every turn, skill creation/refinement from
  experience, scheduled jobs, multi-platform gateway, delegated subagents, pluggable terminal backends — are not on the loop.
- **Fix:** implement `PluginContext` (`register_tool`, `register_hook`, `register_command`, `register_skill`, plugin storage)
  and dispatch the 14 hooks from the canonical bus; support `kind` / `provides_*`; load a small set of bundled Hermes plugins
  as conformance fixtures; make memory a context source (recall) + tools (`retain/recall`); implement skill authoring
  (`manage_skill`) and a curator that proposes skill edits for approval; wire `subagents` as a `task` tool; add terminal backends
  (`local`, `docker`, `ssh`) behind `ExecutionBackend`.
- **Acceptance:** a Hermes plugin using `pre_tool_call` can block a tool; memory recalled facts appear in the prompt;
  `/agents` lists real subagent runs.

#### P2-3 · Session and settings compatibility — Effort M
- Import/export of Pi/OMP session JSONL (tree entries) and Hermes session DB; read `~/.pi/agent/settings.json`,
  `~/.omp/agent/config.yml`, `~/.hermes/config.yaml`, `~/.openclaw/…` for model/theme/keys (read-only importer with a
  preview), so a user can switch harness without re-configuring.
- **Acceptance:** `omni import pi` shows what will be imported and applies it; a Pi session opens in Omni and continues.

#### P2-4 · OpenClaw features beyond skills — Effort L
- Heartbeats/cron with durable store (`cron` exists but is off the loop), hooks/webhooks (`webhooks.py` is test-only),
  exec approvals + allowlists, `steer`/`followup` queue modes, `trajectory export`, ACP bridge (`acp` unhandled),
  and the control-UI. Prioritise: approvals, queue modes, cron on the loop, trajectory export.

### P3 — Polish

- **P3-1 Naming honesty:** `paperclip_adapter` is a local ticket file (no Paperclip server) → rename `tickets` or implement the
  API; `native_accel` is pure Python → rename `fast_text` or add a real optional Rust/`ripgrep` backend; `collab_web` is HTTP
  polling → rename or implement WebSocket streaming; `metaharness` records runs but cannot run benchmarks.
- **P3-2 Dead/duplicate code:** `features/gateway.py` (531 lines), `evolution/{tournament,constraints}.py`, `src/cli/repl.py`
  vs `src/repl.py`; tracked state files (`tests/test_auth.db`, `tests/test_telemetry.sqlite`, `.omni/*.json`) and tracked
  `*.pyc`; stale `D:\Harness\omni-harness` copy; runtime-written `harness.config.json`/`omni.config.json` committed to git
  (move runtime state out of the repo; ship `harness.config.example.json`).
- **P3-3 Big functions:** `CommandExecutor.execute` still ~995 lines (≈100 legacy `elif` branches; only 12 commands moved to the
  handler registry), `router.complete` 227, `registry.discover_all` 181, `engine.run_turn` 250 — **11 functions in total exceed
  120 lines** (also `main`, both `run_rich_repl`, `_register_native_tools`, `check_permission`, `compact_history`,
  `_complete_google_antigravity`). Finish migrating to the registry; target ≤ 60 lines per function.
- **P3-4 Feature flags:** overlapping names (`plugins_openclaw` ≡ `openclaw_skills`, …) papered over by `FEATURE_ALIASES`;
  collapse to one name per capability and migrate configs.
- **P3-5 UI:** themes are built-in only (add user theme files), pets/split-pane are decorative modules not connected to the
  real REPL; decide to integrate (a real TUI, e.g. Textual) or remove.
- **P3-6 Docs:** README must state what is implemented vs planned (a generated table from the registry); document the
  extension manifest, event taxonomy, and each `OMNI_*` variable.

### Testing gaps (cross-cutting)

- **9 of 71** `src` modules are never referenced by any test: `branching cli constraints environments oauth prewalk repl
  slack_whatsapp tournament`.
- Many tests assert substrings of output; add **behavioural** tests (loop with a fake provider, tool-call round-trips,
  permission denial, resume).
- Add **conformance suites**: `tests/conformance/{pi,hermes,openclaw,mcp}/` that load real upstream plugins/skills/commands
  (pinned) and report pass rate; run in CI.
- Add a **parity report** (`python -m omni.parity`) printing per-harness `implemented/total` for commands, tools, events,
  extension API methods — this is the metric to watch while fixing the items above.

---

## 3. Suggested order of work

| Phase | Goal | Items | Effort |
|---|---|---|---|
| **0 — Integrity** (do first) | Stop advertising things that don't work; fix the basics | P0-2 (catalog from registry, hide/remove fakes), implement `/new /clear /reset /resume /fork /export /copy /settings /login /quit`, P0-6 triage (delete orphans, `/doctor`), P3-2 hygiene, README honesty | M |
| **1 — Core loop** | A harness you can actually code with | P0-3 (streaming, retry, cancel, thinking, caching), P0-5 (session store, parallel tools, abort, context builder), P0-4 (Pi core tools + approvals default-on + execution backends) | L |
| **2 — Extension kernel** | Make everything pluggable and measurable | §1 capability registries, §1.1 event taxonomy, middleware pipeline, extension manifest, provider plugins, finish handler-registry migration, parity report | L |
| **3 — Compatibility** | Real interoperability | P0-1 skills model, P1-3 context files, P1-2 MCP depth, P1-5 Pi host, P1-6 Hermes ctx + hooks, P2-3 importers, conformance suites | L |
| **4 — Channels & ops** | OpenClaw-style reach | P1-1 gateway daemon + Discord/Slack/WhatsApp, node transport, cron/webhooks/approvals on the loop (P2-4) | L |
| **5 — Polish** | Feel and docs | P3-1, P3-4, P3-5, P3-6, remaining testing gaps | M |

### Quick wins (≤ 1 day each, high value)

1. Make `/help` and autocomplete show only commands that have handlers; print "not implemented" for the rest.
2. Implement `/new`, `/clear`, `/reset`, `/quit` (trivial) and `/export` (dump the current session to JSONL/HTML).
3. Remove `_extract_command` from the OpenClaw adapter (stop executing doc snippets); list skills in the prompt instead.
4. Turn `permission_gates` **on** by default and add it to `DEFAULT_FEATURES`.
5. Load `AGENTS.md`/`CLAUDE.md` into the system prompt.
6. Add the `edit` and `grep` tools (the two biggest practical gaps).
7. Delete the ~70 subcommand-name entries and the three orphan modules.
8. Make `/plugins` print per-extension status with the failure reason (missing package, TS error) so users can fix them.

---

## 4. Definition of done (measurable)

| Metric | Today | Target |
|---|---|---|
| Catalog entries without a handler (advertised) | ~115 of 223 | **0** |
| OMP top-level slash command coverage (handled/88) | 38 (43%) | ≥ 90% |
| Pi core command coverage (handled/19, list unverified — see §6) | 6 | 19 |
| OpenClaw chat command coverage (handled/48) | 35 | ≥ 45 |
| CLI subcommands implemented / advertised | 12 / 141 | = |
| Pi `ExtensionAPI` methods / events supported | 7/33 · 0/44 | ≥ 25/33 · ≥ 35/44 |
| Pi packages loading in `plugins/pi` | 5/13 | ≥ 12/13 |
| Hermes hooks dispatched | 0/14 | 14/14 |
| MCP transports | stdio | stdio + streamable-HTTP + SSE |
| Providers (via catalog) / streaming | 8 / none | ≥ 15 / all |
| Built-in tools usable without extras | 9 (no edit/grep/find) | Pi core 7 + OMP core ≥ 15 |
| Feature modules on the agent path | 16 of 41 | all kept modules; none orphaned |
| Modules without tests | 9 of 71 | 0 |
| Functions > 120 lines | 11 | 0 |

---

## 5. Corrections to things I said earlier

For the record, these earlier statements were overstated or wrong and should not be relied on:

- "170+ / 175 slash commands" and "all 69 (later 141) CLI subcommands routable" — see P0-2, P1-4.
- "Pi: 14 extensions detected / compatible" — 5 of 13 load, API coverage is shallow (P1-5).
- "OpenClaw: 28 skills active as tools" — conceptually wrong (P0-1).
- "TTSR / speculative execution / MoA / egress firewall / output budgeting implemented" — not on the agent path (P0-6).
- "Rust native acceleration", "WebRTC collaboration", "Paperclip adapter", "Harbor/SWE-bench harness" — names outran
  the implementation (P3-1).
- "Firefox works seamlessly" — detection only; Firefox speaks WebDriver BiDi and is not driven.
- During earlier test runs some tests wrote into the real repository/home (checkpoint commits, a stray worktree branch,
  history file). Those were cleaned up and the suite is now isolated (`tests/_isolation.py`); keep it that way.

## 6. Method (to reproduce the numbers)

- **Module reachability:** AST import graph over `src/**`; reachable set from `src.kernel.engine`, `src.providers.router`,
  `src.adapters.registry` (lazy imports included); "never imported" = no importer in `src`, `plugins`, or `tests`.
- **Command coverage:** catalog = `ALL_SLASH_COMMANDS` unique names; handled = names in the handler registry ∪ names matched
  by `elif name ==/in …` in `commands_executor.py`. Upstream lists: OpenClaw `defineBuiltinCommand("…")` in
  `src/auto-reply/commands-registry.shared.ts`; Hermes `CommandDef("…")` in `hermes_cli/commands.py`; OMP top-level
  `name:` entries (excluding nested subcommands) in `packages/coding-agent/src/slash-commands/builtin-*.ts`; Pi core list (19 commands, and the 7-tool set `read write edit bash grep find ls`) is **from my knowledge of Pi's published
  README, not from a local checkout** — Pi core is not cloned in `D:\Harness` (OMP is a fork of it). **Verify those two lists
  against a Pi checkout before treating the Pi percentages as exact.** The Pi *extension API* numbers are verified (read from
  OMP's `extensions/types.ts`, including the tool `execute(toolCallId, params, signal, onUpdate, ctx)` signature).
- **Pi load test:** for each package in `plugins/pi`, send `discover` for its first `pi.extensions` entry to `pi_runner.js`.
- **Pi API surface:** `export interface ExtensionAPI` / `ExtensionUIContext` in `extensions/types.ts`.
- **MCP:** a 15-line stdio server (`initialize`, `tools/list`, `tools/call`) driven through `MCPAdapter.discover()`.
- **Provider/engine capabilities:** keyword counts in `client.py`/`engine.py` plus reading `run_turn`/`_execute_tool`.
- A reusable audit script would make these numbers a regression metric (recommended as the first Phase-2 task).
