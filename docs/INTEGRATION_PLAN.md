# Omni integration plan — Pi, Oh My Pi, Hermes and OpenClaw in one modular harness

*Written 2026-11-10. Builds on `PARITY_AND_ARCHITECTURE_REVIEW.md` (the audit) and `PI_PROFILE.md` (what now exists).*

## 0. Goal and design rules

One harness that can be dialled from **barebones Pi** (7 tools, tiny prompt, no daemons) to **everything on**
(OMP's tool suite, Hermes' memory/skills/self-evolution, OpenClaw's gateway, channels and providers), and that runs
each project's own **plugins, skills and extensions unchanged**.

Rules every step below follows:

1. **The Pi runtime is the kernel.** `src/pi/` (agent loop, session tree, tools, prompt, extension events) becomes
   Omni's single core. Heavier behaviour is added *around* it as capabilities and middleware, never by forking the
   loop. The legacy `src/kernel/engine.py` path is retired once its features are ported (phase 6).
2. **One canonical event taxonomy = Pi's event names.** Pi already has the richest lifecycle (≈35 events with
   gate/transform semantics). Hermes hooks and OpenClaw hooks are *mapped onto* it (tables in §4). Every host
   receives the same events; nothing is emitted twice under different names.
3. **Foreign code runs in its native runtime behind a host process** (Node for Pi/OMP/OpenClaw, Python for Hermes)
   and speaks one JSON-lines protocol. Each host reports what it supports; gaps are recorded per plugin
   (`/extensions` already shows "not yet supported" for Pi).
4. **Capabilities are registries with provenance**: tools, commands, shortcuts, providers, skills, prompt
   templates, context sources, channels, renderers, themes, memory backends, execution backends. Each entry says
   who registered it, and each can be enabled per profile.
5. **Profiles are data.** `profiles/<name>.yaml` lists capabilities and defaults; `omni profile <name>` switches.
   Toggles are capability switches, not code paths.
6. **Measured compatibility.** "Compatible" means "passes the conformance fixtures", printed as a percentage per
   harness (§7). No feature is reported as done without a test.

## 1. Baseline

**Update (2026-11-11):** phase 2's capability registry exists (`src/pi/caps`, documented in `CAPABILITIES.md`):
profiles list `capabilities`, the user toggles them with `/capabilities` / `omni toggle` (saved as an overlay),
custom profiles are saved with `/profile save`, and every module reports its context cost. Profiles were renamed to
neutral names (`core`, `lean`, `coder`, `assistant`, `balanced`, `autonomous`, `full`; `pi` / `omp` / `hermes` /
`openclaw` remain as aliases). First native modules: `todo`, `ask`, `fetch`, `memory` (Hermes file format), `task`
(subagents), `skills-universal` (other harnesses' skill folders + OpenClaw/Hermes gating), `skill-authoring`.
Still open from phase 2: `ExecutionBackend` / `Scheduler` / `Channel` registries and `/doctor`.

**Update (2026-11-11, later):** phase 3 started. Capabilities gained a tool `gate` and a `live_prompt` (the
`ToolMiddleware` point in its first form). Oh My Pi ports: `checkpoint` / `rewind`, plan mode (`/plan`,
`exit_plan_mode`), `ast_grep` / `ast_edit` through the ast-grep CLI, and hashline `read` / `edit` (opt-in).
Open in phase 3: hashline block ops, `lsp`, `web_search`, `github`, `eval`, model roles, TTSR, snapcompact,
the command list and the extension API delta.

**Update (2026-11-12):** startup is deferred (window first, hosts in the background), extension TypeScript is
cached, and `/doctor` + `/extensions slow` exist. Phase 4 started: `hermes-plugins` is a native in-process host
with `PluginContext` (tools, commands, prompt sections, config) and eight hooks mapped onto the pipeline; the
legacy registry's Hermes source is no longer used by the shipped profiles. Also new: `hooks` (shell hooks,
Claude Code format), `web-search`, `session-search`, named agents for `task`. Capabilities gained `after_tool`,
`on_prompt`, `commands` and `unload`. Open in phase 4: remaining plugin kinds, cron, platforms, self-evolution.

**Update (2026-11-12, later):** five capabilities modelled on other projects: `tracing` (Langfuse),
`memory-archive` (Letta), `durable` (Temporal), `browser` (Browser Use), `sandbox` (Daytona). See
`CAPABILITIES.md`. The reference repos are under `D:\Harness\references`.

**Update (2026-11-13):** `claude-compat` (Claude Code commands, plugins, `.mcp.json`), `scheduler` (the plan's
`Scheduler` point in its first form: cron jobs in fresh sessions, `omni cron run`), labeled memory blocks,
model roles for subagents, and a profile wizard (`/profile new | edit | show | delete`). Capabilities gained
`template_roots`, `agent_roots`, `hook_files` and `mcp_files`. Reference clones now include `letta-code`,
`letta-archive` and `daytona-v0.190.0`.

**Update (2026-11-13, later):** phase 5 started. `openclaw-plugins` loads OpenClaw plugin entries in the Node host
(tools, commands, core hooks; other registrations recorded). `omni gateway` exists (`src/pi/gateway.py`): Telegram and
HTTP channels, allowlist + pairing codes, one persistent session per conversation, a `register_channel` point. Also:
`diagnostics`, `memory-reflect`, `/rate`, browser screenshots, and `requires` between capabilities. Open: hosting
OpenClaw channel / provider plugins, more channels, cron delivery to channels, ACP, Hermes plugin kinds, `lsp`.

**Update (2026-11-13, evening):** user-written capability modules (`<agent-dir>/capabilities/*.py`), `approvals`
(UI, chat and unattended decisions), `code-exec`, Hermes memory-provider plugins, cron delivery to gateway channels,
and `omni profile new | show | delete`. `balanced` now uses `approvals` instead of the legacy permission gate.

**Update (2026-11-10, later):** phase 2's profile part and phase 6's "point every profile at the new kernel" are done
in a first form: `profiles/*.json` (`pi → lean → balanced → autonomous/full → evolve`), bare `omni` runs the core with
the active profile, Omni's MasterToolRegistry (MCP, Hermes, OpenClaw, Omni tools) is bridged in as core tools for
`balanced` and up, and Omni's PermissionGate runs as a tool-call gate. The legacy REPL remains as `omni legacy`. The
table below is the state before that step.


| Layer | State |
|---|---|
| Kernel | `src/pi/agent.py` — Pi loop, queues, retries, compaction, branch summaries, session ops. Legacy `src/kernel/engine.py` still drives non-pi profiles. |
| Sessions | Pi JSONL v3, file-compatible with Pi. Legacy SQLite stores (`sessions.sqlite`, `timeline_branches.sqlite`) still used by the legacy REPL. |
| Tools | Pi's 7 built-ins with Pi semantics. Legacy native tools + 41 feature modules (25 not on any agent path, per the audit). |
| Providers | Streaming adapters for Anthropic, OpenAI-compatible, Gemini, Antigravity, faux; `models.json`; Omni keyring. Legacy non-streaming client still used by the legacy REPL. |
| Pi extensions | Node host with the full `ExtensionAPI` surface listed in `PI_PROFILE.md`; 48/48 tested examples load. |
| Hermes plugins | Legacy adapter loads `plugin.yaml` + `register(ctx)`; 0 hooks dispatched. |
| OpenClaw | Legacy adapter treats `SKILL.md` as executable tools (wrong model); no plugin (`openclaw.plugin.json`) support, no gateway. |
| UI | New full-screen TUI for the Pi profile. Legacy prompt_toolkit REPL for other profiles. |

## 2. Target architecture

```
 Frontends      TUI (src/pi/tui) · print/json · RPC · Gateway daemon (channels) · ACP · Web control UI (later)
                                         │
 Kernel         AgentSession (src/pi/agent.py) ── SessionManager (JSONL tree) ── Event bus (Pi taxonomy)
                Middleware pipeline: input → context sources → prompt build → provider request → tool gate →
                tool exec (ExecutionBackend) → tool result transforms → turn end → settle
                                         │
 Capabilities   tools · commands · shortcuts · providers/models · skills · templates · context sources · memory ·
 (registries)   channels · execution backends · renderers/themes · schedulers · subagent runners
                                         │
 Hosts          Pi/OMP host (Node, done) · OpenClaw host (Node) · Hermes host (Python, in-process or sub-process) ·
                MCP client · Omni native plugins (Python)
                                         │
 Profiles       profiles/*.yaml → which capabilities, defaults, prompt additions, UI chrome
```

### 2.1 Kernel extension points (to add to `AgentSession`)

| Point | Purpose | Used by |
|---|---|---|
| `ContextSource` | contributes prompt sections / messages each turn (`id`, `priority`, `build(ctx) -> sections`) | AGENTS.md (exists), memory recall, mental models, OMP rules, Hermes context engine |
| `ToolMiddleware` | wraps tool execution (before/after) | approvals, egress firewall, output budget, checkpoints, sandbox routing, TTSR |
| `ExecutionBackend` | runs `bash`-like commands: `local`, `docker`, `ssh`, `worktree` | Hermes terminal backends, OpenClaw nodes, Omni docker sandbox |
| `ProviderAdapter` registry | already in `src/pi/ai/stream.py` (`register_api`) — make it a plugin capability | OpenClaw provider plugins, Hermes model-providers, Pi `registerProvider` |
| `SubagentRunner` | spawns a child `AgentSession` with its own tools/prompt; exposes a `task` tool | OMP `task`, Hermes delegation, Omni subagents |
| `Scheduler` | durable jobs that inject prompts into sessions | OpenClaw cron/heartbeats, Hermes cron providers |
| `Channel` | inbound/outbound messaging bound to sessions | OpenClaw channels, Hermes platforms, Omni gateways |
| `MemoryBackend` | `retain / recall / reflect` + context source | Hermes memory providers, OMP mnemopi, Omni memory graph, OpenClaw memory-core |

All of these become kernel registries in `src/pi/` (renamed to `src/core/` when the legacy kernel is deleted —
see §6), and each is reachable from every host's API.

## 3. The profile dial

| Capability | `pi` | `omp` | `hermes` | `openclaw` | `full` |
|---|---|---|---|---|---|
| Pi core tools (read bash edit write grep find ls) | ✓ (4 default) | ✓ | ✓ | ✓ | ✓ |
| Pi extensions / skills / templates / AGENTS.md | ✓ | ✓ | ✓ | ✓ | ✓ |
| OMP tool suite (hashline edit, ast_grep/ast_edit, lsp, todo, ask, task, fetch, web_search, checkpoint/rewind, github, eval) | – | ✓ | partial (todo, web) | – | ✓ |
| OMP extras (TTSR rules, snapcompact, model roles, plan mode, browser relay, collab) | – | ✓ | – | – | ✓ |
| MCP client (stdio + HTTP) | opt-in | ✓ | ✓ | ✓ | ✓ |
| Persistent memory (retain/recall + recall context source) | – | mnemopi | ✓ | memory-core | ✓ |
| Skill authoring + curator, self-evolution (GEPA) | – | – | ✓ | skill-workshop | ✓ |
| Subagents / delegation | – | ✓ | ✓ | ✓ | ✓ |
| Execution backends (docker/ssh/worktree) | local | local+worktree | local+docker+ssh | local+nodes | all |
| Approvals / permission gates | Pi-style (none by default) | ✓ | ✓ | exec approvals | ✓ |
| Scheduler (cron, heartbeats) | – | – | ✓ | ✓ | ✓ |
| Gateway daemon + channels | – | – | platforms | ✓ | ✓ |
| Provider plugins beyond built-ins | models.json | ✓ | model-providers | ✓ | ✓ |
| UI chrome | minimal header/footer | + todo/status widgets | + memory/skills panels | + channel status | everything |

`profiles/*.yaml` example:

```yaml
name: hermes
extends: pi
tools: [read, bash, edit, write, grep, find, ls, todo, web_search, fetch, memory, skill_manage, task]
context_sources: [agents_md, memory_recall, skills]
middleware: [approvals, output_budget]
execution: { default: local, available: [local, docker, ssh] }
hosts: { pi: true, hermes: true, openclaw: false, mcp: true }
scheduler: true
settings: { compaction.reserveTokens: 24000 }
ui: { header: hermes, widgets: [memory, skills] }
```

Toggles (`omni toggle <cap> on|off`) write a per-user overlay on top of the active profile.

## 4. Feature inventory and mapping

Source folders to read are in `D:\Harness`. "Approach" says whether we **port** (re-implement in the kernel),
**host** (run the original code), or **map** (translate a format).

### 4.1 Pi (`references/pi-mono`) — status: core done

| Feature | Approach | Remaining work |
|---|---|---|
| Tools, prompt, sessions, commands, modes, extensions, skills, templates, trust | port + host | see `PI_PROFILE.md` gaps |
| MCP (`src/extensions/mcp/*`, `mcp.json`, `/mcp`) | port | **done** (`src/pi/mcp.py`); remaining: `cimd` registration, SSH paste sign-in, per-project overrides from `/mcp` |
| `codemode` + `tool_search` (`src/extensions/codemode`, `tool-search`) | port | **done** (Node `vm` runner, BM25); remaining: classifier / image models for `models.*` |
| Pi packages (`pi install npm:/git:`, `package-manager.ts`) | port | **done** (`omni install ...`, `src/pi/packages.py`); remaining: `pi config` TUI, package changelogs |
| Keybindings (`keybindings.json`), themes JSON | map | themes **done** (incl. package themes); keybindings remain |
| Image resize, inline images, `tuiMode: regular` | port | **done**; remaining: sixel |
| Extension UI hooks, virtual models, TSX | host | **done** (editor component, autocomplete providers, terminal input, markdown transformers, virtual models, `registerMcpServer`, TSX/JSX) |

### 4.2 Oh My Pi (`oh-my-pi/packages/*`) — Pi fork with a larger built-in surface

| OMP package / feature | Omni target | Approach |
|---|---|---|
| `coding-agent` slash commands (88 top-level) | command registry, OMP profile | port the ones with behaviour; aliases where they equal Pi's |
| Tools: `hashline`/edit variants, `ast_grep`, `ast_edit`, `lsp`, `todo`, `task`, `ask`, `fetch`, `web_search`, `checkpoint`, `rewind`, `github`, `eval`, `think`, memory (`recall/reflect/retain`), `manage_skill`, `security_scan` | `src/tools/omp/*` as kernel tools with OMP schemas | port; reuse Omni's `features/ast_grep.py`, `checkpoints.py`, `lsp_bridge.py` after moving them onto the tool API |
| TTSR (time-traveling stream rules) | `ToolMiddleware` + stream watcher | port `features/ttsr.py` onto `message_update` |
| `snapcompact` | compaction strategy option | port; `session_before_compact` hook already exists |
| `mnemopi` | `MemoryBackend` | port, reuse `features/memory_graph.py` |
| `browser-relay` | tool + host | Omni's CDP relay (works) → tool `browser` |
| `collab-web`, `stats`, `metaharness` | optional services | keep as opt-in capabilities |
| Model roles (default/smol/slow/plan), plan mode | settings + commands | port |
| `natives` (Rust) | none | not needed (ripgrep/fd binaries cover the hot paths) |
| OMP extension API delta | Pi host | diff `oh-my-pi/packages/coding-agent/src/extensibility/extensions/types.ts` against Pi's; add the extra methods/events to `host.mjs` (it already accepts `@oh-my-pi/*` imports) |
| `~/.omp/agent/config.yml`, `agent.db` OAuth | importer | `omni import omp` (OAuth import exists in `/login`) |

### 4.3 Hermes (`hermes-agent`, `hermes-self-evolution`)

Hermes plugins are Python packages: `plugin.yaml` + `register(ctx)`, kinds under `plugins/<kind>/` (browser,
computer_use, context_engine, cron_providers, delegation, image_gen, kanban, memory, model-providers, platforms,
video_gen, web, …) plus a plugin catalog (`plugin-catalog/*.yaml`). Rule from `plugins/AGENTS.md`: plugins never
touch core; they use ABCs, hooks and `ctx`.

| Hermes feature | Omni target | Approach |
|---|---|---|
| `PluginContext` (`register_tool`, `register_hook`, `register_command`, `register_skill`, storage) | **Hermes host** | host: run plugins in-process (Python) behind a `HermesHost` that implements `ctx` against kernel registries; switch to a sub-process if isolation is needed |
| `VALID_HOOKS` (`hermes_cli/plugins.py`) | Pi event taxonomy | map (table below) |
| Kind ABCs: `MemoryProvider`, model provider, context engine, image/video gen, platforms, terminal backends | `MemoryBackend`, `ProviderAdapter`, `ContextSource`, tools, `Channel`, `ExecutionBackend` | adapter classes per kind, so catalog plugins load unchanged |
| Toolsets (`toolsets.py`, `toolset_distributions.py`) | profile tool lists | map: a Hermes toolset name → set of kernel tools |
| Skills (`skills/`, `optional-skills/`) + curator | skills registry + `skill_manage` tool + curator command | port curator (propose → approve → write SKILL.md) |
| Persistent state (`hermes_state_*`: sessions, FTS search, compaction, usage) | session store + search index | import Hermes sessions into JSONL; add an FTS index (SQLite) over JSONL for `/search` |
| Gateway / platforms (Telegram, Discord, Slack…) | `Channel` capability | shared with OpenClaw (§4.4) |
| Cron | `Scheduler` | port |
| Delegation / subagents | `SubagentRunner` + `task` tool | port |
| Self-evolution (DSPy + GEPA, `hermes-self-evolution/evolution/*`) | offline job over session traces | host: run the upstream package against Omni's JSONL traces; output = PR-style patch to a skill / prompt, applied only after approval |
| `SOUL.md`, `AGENTS.md` | context sources | map |

Hook mapping (verify names against `hermes_cli/plugins.py::VALID_HOOKS` before implementing):

| Hermes hook | Pi event | Semantics to preserve |
|---|---|---|
| `pre_llm_call` | `context` / `before_provider_request` | may edit messages |
| `post_llm_call`, `transform_llm_output` | `message_end` | may replace assistant message |
| `on_stream_start/delta/end` | `message_start/update/end` | observe |
| `pre_tool_call` | `tool_call` | may block / modify input |
| `post_tool_call`, `transform_tool_result` | `tool_result` | may transform result |
| `transform_terminal_output` | `tool_result` where tool = bash | transform text |
| `message`, `on_interim_message` | `input`, `message_update` | |
| `action`, `continue` | `turn_end` boundary result | `continue: true` |

### 4.4 OpenClaw (`openclaw/`)

OpenClaw is a gateway-centred assistant: a daemon (`src/gateway`, `src/daemon`) owns sessions per conversation,
~180 extension packages provide **channels** (telegram, discord, slack, whatsapp, signal, imessage, matrix, msteams,
…), **providers** (anthropic, openai, google, bedrock, ollama, openrouter, …), memory (`memory-core`,
`memory-lancedb`, `memory-wiki`), tools (browser, canvas, deep-research, firecrawl, …) and policy. Plugins declare
metadata in `openclaw.plugin.json` (id, categories, channels, configSchema, activation, doctor contract) and an
entry built with `openclaw/plugin-sdk/*` helpers (e.g. `defineBundledChannelEntry`). Skills are `SKILL.md`
instructions listed in the prompt, never executed.

| OpenClaw feature | Omni target | Approach |
|---|---|---|
| Skills (`skills/`, `custodian-skills/`, `metadata.openclaw.requires/os/install` gating, `command-dispatch: tool`) | Pi skills registry | map: reuse `resources.py` discovery + add OpenClaw gating fields; delete `_extract_command` from the legacy adapter |
| Plugin manifest + SDK (`openclaw.plugin.json`, `src/plugin-sdk/*`) | **OpenClaw host** (Node) | host: provide `openclaw/plugin-sdk/*` shims the same way `host.mjs` shims Pi packages; start with provider + tool + channel contracts (`docs/plugins/sdk-*.md` are the spec) |
| Gateway daemon (durable ingress queue, per-channel sessions, routing/bindings, allowlists, DM pairing) | `omni gateway` frontend | port the daemon shape; each inbound conversation = one `AgentSession` with its own JSONL file |
| Channels | `Channel` capability | host OpenClaw channel plugins first (they already implement each platform); fall back to Omni's own Telegram |
| Providers (~60 plugins) | `ProviderAdapter` | host; many are OpenAI-compatible and can be mapped to `models.json` entries without running code |
| Exec approvals, allowlists, policy | `ToolMiddleware` | port |
| Cron / heartbeats | `Scheduler` | port |
| Nodes / device pairing (`linux-node`, `device-pair`) | `ExecutionBackend: node` | port transport (companion connects back over WebSocket) |
| ACP (`acpx`) | frontend | expose the kernel over ACP (also used by editors) |
| Control UI (`ui/`) | optional web frontend | later |
| `~/.openclaw` config, `migrate-hermes`, `migrate-claude` | importers | read-only import with preview |

### 4.5 Cross-cutting compatibility inputs

- `AGENTS.md` / `CLAUDE.md` / `SOUL.md` / `SYSTEM.md` → context sources (done for the first four).
- `.claude/skills`, `.agents/skills`, `~/.openclaw/skills`, Hermes skill dirs → one skills registry with source tags.
- `.mcp.json`, `~/.pi/agent/mcp.json`, Claude Desktop config → MCP registry.
- Settings importers: `~/.pi/agent/settings.json` (native), `~/.omp/agent/config.yml`, `~/.hermes/config.yaml`,
  `~/.openclaw/*` → preview + apply (`omni import <harness>`).
- Session importers: Pi/OMP JSONL (native), Hermes DB → JSONL, OpenClaw transcripts → JSONL.

## 5. Roadmap

Effort: S < 1 day, M 1–4 days, L > 4 days. Each phase ends green on its acceptance tests.

### Phase 1 — Pi parity polish (M)
1. MCP client in the kernel (stdio + streamable HTTP, `mcp.json`, `/mcp`, `mcp__server__tool`, `registerMcpServer` from
   extensions). *Accept:* a stdio and an HTTP server both load; tool list refreshes on `list_changed`.
2. `omni pi install npm:/git:/path` (Pi packages) with per-package `node_modules`. *Accept:* the 13 packages in
   `plugins/pi` load or report a precise reason; ≥ 12/13 load.
3. Remaining extension API gaps (`setEditorComponent`, autocomplete providers, `onTerminalInput`, markdown
   transformers, `user_bash` operations). *Accept:* Pi's `modal-editor`, `rainbow-editor`, `github-issue-autocomplete`
   examples work.
4. Parallel execution for read-only tool batches; `tuiMode: regular`; image resize. *Accept:* two `read` calls overlap.
5. Live-provider smoke tests (opt-in, real keys): Anthropic, OpenAI, Gemini, Antigravity. *Accept:* stream, tool call,
   thinking, 429 retry recorded in CI-optional suite.

### Phase 2 — Kernel extension points (L)
1. Add the registries in §2.1 to `AgentSession` with provenance and per-profile enablement.
2. `profiles/*.yaml` loader + `omni profile` / `omni toggle` rewritten on top; `pi` profile = current behaviour.
3. Move Omni's working features onto the new points: approvals (`permissions.py`), docker sandbox
   (`docker_sandbox.py`) as `ExecutionBackend`, shadow worktrees, checkpoints (`/undo`), output budget, egress,
   memory graph (recall source + tools), browser relay tool. Delete the rest of the orphans the audit lists.
4. `/doctor`: every capability reports `active | available | inactive (reason)`.
*Accept:* every kept module in `src/features/` is reachable from the kernel (import-graph test); `pi` profile prompt
and tool list are byte-identical to before.

### Phase 3 — OMP profile (L)
1. Port OMP tools (table §4.2) with OMP schemas; register under the `omp` profile.
2. OMP slash commands that carry behaviour; aliases; command registry generated from handlers.
3. OMP extension API delta in `host.mjs`; `@oh-my-pi/*` shims beyond the Pi ones.
4. TTSR, snapcompact, model roles, plan mode.
*Accept:* OMP command coverage ≥ 90% of its 88 top-level commands; OMP-only extensions from `~/.omp` load.

### Phase 4 — Hermes profile (L)
1. `HermesHost` with full `PluginContext`; hook mapping table §4.3; kind adapters (memory, model provider, context
   engine, platforms, terminal backends).
2. Toolsets → profile tool lists; Hermes skills + curator; `task` delegation; scheduler.
3. Hermes session import; FTS search over JSONL.
4. Self-evolution job (`omni evolve`) running the upstream optimizer on JSONL traces, producing reviewable patches.
*Accept:* `plugins/hermes/crypto_helper` and three bundled Hermes plugins (one memory, one tool, one platform) run;
a `pre_tool_call` hook blocks a tool; recalled memory appears in the prompt.

### Phase 5 — OpenClaw profile (L)
1. OpenClaw skills gating on the shared registry; remove the executing adapter.
2. `OpenClawHost` with `openclaw/plugin-sdk/*` shims: providers → tools → channels, in that order.
3. Gateway daemon (`omni gateway`): ingress queue, per-conversation sessions, allowlists, pairing, routing; channels
   via hosted plugins; exec approvals; cron/heartbeats on the scheduler.
4. ACP frontend.
*Accept:* a Telegram message reaches an agent session and the reply returns; allowlist blocks strangers; sessions
survive a daemon restart; five provider plugins and two tool plugins load unchanged.

### Phase 6 — Convergence (M)
1. Point every profile (and `omni` with no args) at the new kernel + TUI; delete `src/kernel/engine.py`,
   `src/repl.py`, `src/cli/repl.py`, the hand-written command catalog and legacy adapters.
2. Rename `src/pi` → `src/core` (keep `src/pi` as a thin alias for one release).
3. README regenerated from registries (implemented vs planned table).
*Accept:* one kernel, one REPL, one command registry; full suite + conformance green.

## 6. Legacy code disposition

| Keep (move onto kernel) | Rewrite | Delete |
|---|---|---|
| `paths.py`, keyring + OAuth, CDP browser relay, MCP stdio client, shadow worktrees, docker sandbox, permissions, checkpoints, memory graph, telemetry tracer | provider router (merge into `src/pi/ai`), gateways (onto `Channel`), cron (onto `Scheduler`), subagents (onto `SubagentRunner`) | hand-written command catalog, `_extract_command`, `pi_runner.js` (superseded by `src/pi/extensions/host`), orphan modules listed in the audit, the stale `D:\Harness\omni-harness` copy, committed runtime state files |

## 7. Testing and conformance

- **Unit / behaviour** (offline, `faux` provider): `tests/test_pi_runtime.py` is the template — loop, tools, sessions,
  modes, extension host.
- **Conformance suites** `tests/conformance/{pi,omp,hermes,openclaw,mcp}/`: pinned upstream plugins/extensions/skills
  copied from `D:\Harness\references` and `D:\Harness\*\extensions`; each test loads one, drives one scripted turn,
  asserts its effect. Report `passed/total` per harness.
- **Parity report** `python -m src.pi.parity`: per harness, implemented/total for tools, commands, events, extension
  API methods, ctx.ui methods — generated from the upstream sources (Pi `types.ts`, OMP `types.ts`,
  Hermes `VALID_HOOKS` / `CommandDef`, OpenClaw `defineBuiltinCommand`), so numbers can't drift from reality.
- **Live smoke** (opt-in): provider streaming per adapter.
- **UI snapshot tests**: render frames with a fake terminal (as done while building the TUI) and compare stripped text.

Targets: Pi conformance ≥ 95%; OMP ≥ 85%; Hermes plugin load ≥ 90% of bundled; OpenClaw provider/tool plugins
≥ 80% by phase 5.

## 8. Risks and open questions

- **API drift:** all four upstreams move fast. Mitigation: shims are generated/checked against upstream type files
  in the parity report; pin tested versions in `docs/PI_PROFILE.md`-style tables per harness.
- **OpenClaw plugin SDK size:** the SDK surface is large and some plugins deep-import helpers. Start with
  manifest-only mapping (providers that are OpenAI-compatible need no code), then host the SDK incrementally.
- **Hermes in-process isolation:** a misbehaving plugin can crash the kernel; offer `hosts.hermes.isolate: true`
  (sub-process) once the protocol is shared with the Node hosts.
- **Windows:** keep every path stdlib-only and test on Windows Terminal + conhost; Git Bash discovery and VT input
  are already handled in the Pi profile.
- **Verify before implementing:** exact OMP `ExtensionAPI` delta, Hermes `VALID_HOOKS` list and `PluginContext`
  methods, OpenClaw `plugin-sdk` entry contracts — read the files named in §4 and record them in the parity report
  rather than relying on this document.
