# Omni: Universal Agentic AI System

```text
  ___  __  __ _  _ ___ 
 / _ \|  \/  | \| |_ _|
| (_) | |\/| | .` || | 
 \___/|_|  |_|_|\_|___|
 Omni: Universal Agentic AI (Pi + Hermes + OpenClaw Compatible)
```

**Omni** is an open-source, ultra-fast, and modular agentic AI system combining the architectural superpowers of **Oh My Pi (OMP)**, **Hermes Agent**, and **OpenClaw**. 

It can scale effortlessly from an ultra-lightweight, sub-second terminal agent (Pi style) to an autonomous maintenance powerhouse (OpenClaw style) or an evolutionary self-learning system (Hermes style).

---

## Key Architectural Highlights

### 1. Universal Drop-In Plugin Compatibility
Drop any tool, skill, or extension into its designated directory and Omni discovers, validates, and activates it immediately without code modification:
* **`plugins/omni/` (Native Custom Plugins):** Drop single-file Python scripts with `@omni_tool` or `def register(omni)` for maximum developer freedom.
* **`plugins/openclaw/` (OpenClaw Skills):** Full `SKILL.md` parser with YAML frontmatter, JSON5 `metadata.openclaw` platform gating (`requires.bins`, `requires.env`, `requires.os`), and dynamic `{baseDir}` execution.
* **`plugins/hermes/` (Hermes Python Plugins):** Python plugins with `plugin.yaml` and `register(ctx: PluginContext)` exposing 33 signature-inspected lifecycle hooks (`VALID_HOOKS`).
* **`plugins/pi/` (Oh My Pi JS/TS Extensions):** Runs native JavaScript/TypeScript extensions over Node 24 with complete `ExtensionAPI` mocking (`pi.on`, `pi.registerTool`, `pi.registerCommand`).
* **`plugins/mcp/` (Model Context Protocol):** Native JSON-RPC 2.0 stdio client connecting to external MCP servers.

### 2. Multi-Provider Auth & 429/401 Cooldown Key-Pool (Pi-Style)
* **SQLite Credential Store (`agent_auth.db`):** Supports multiple API keys and OAuth sessions per provider with priority weighting.
* **Automatic Rate-Limit & Quota Failover:** When an LLM call encounters HTTP 429 (Rate Limit) or 401 (Quota), Omni places the key in a temporary cooldown block in `auth_credential_blocks` and automatically rotates to the next key or cascades to the next candidate model in the tier (`fast` / `smart`).
* **Google Antigravity CloudCode Proxy:** Native Bearer token authorization and automatic thinking wire model mapping (`gemini-3.8-flash-high`, `claude-sonnet-5-5-medium`, `gemini-4-argon-low`).
* **1-Click OAuth Import:** `omni login --from-omp` auto-syncs existing OAuth sessions directly from `~/.omp/agent/agent.db`.

### 3. Granular Feature Toggles & Runtime Modes
Switch profiles with one command or toggle any of the 16 modular features individually:
* **`lean` Mode (Pi-Style Minimalist Speed):** Disables all heavy plugins, subagents, and gates, running purely on core native tools and context compaction with sub-second latency.
* **`balanced` Mode:** Developer workflow with active plugins, key rotation, and context compaction.
* **`autonomous` Mode (OpenClaw-Style):** Enables all 34 skills, subagents, git checkpoints, and permission gates.
* **`evolve` Mode (Hermes-Style):** Autonomous mode + SQLite trajectory recording + DSPy/GEPA prompt mutation over failure traces.

### 4. Advanced Subsystems
* **Mnemopi Semantic Memory Graph (`src/features/memory_graph.py`):** SQLite Subject-Predicate-Object knowledge graph extracting facts from conversation turns and recalling relevant context assertions.
* **Git Checkpoints & 1-Command `/undo` (`src/features/checkpoints.py`):** Automatically snapshots file modifications per turn, allowing instant `/undo` rollbacks and `/diff` viewing.
* **Intelligent Output Budgeting (`src/features/output_budget.py`):** Automatically clamps runaway tool outputs (e.g. 1,000-line test logs) to head + tail sliding windows with token metrics.
* **Subagent Task Delegation (`src/features/subagents.py`):** Spawns isolated child runs (`planner`, `scout`, `reviewer`) with dedicated prompts and filtered tool scopes.
* **Dangerous Command Permission Gate (`src/features/permissions.py`):** Intercepts destructive bash commands (`rm -rf`, destructive SQL `DROP TABLE`, disk format) with allow/deny lists.
* **Autonomous Cron Task Scheduler (`src/features/cron.py`):** Durable SQLite scheduler executing periodic recurring tasks (daily, hourly, interval) autonomously.
* **Multi-Channel Webhooks (`src/features/webhooks.py`):** Webhook receiver on port 18789 allowing Telegram, Discord, Slack, and Zapier/n8n to hook into Omni.
* **Docker Container Sandbox (`src/features/sandbox.py`):** Runs shell tools inside isolated Docker containers with memory and CPU caps, falling back safely to local environments.
* **Sacred Prefix Cache Guardian (`src/features/prefix_cache.py`):** SHA-256 fingerprinting protecting provider KV prefix caches against mid-session mutations.

---

## Omni core, profiles and capabilities

`omni` runs on a Pi-compatible core with a full-screen terminal window. A **profile** decides how much loads on top
of it, from the barebones `core` (about 1,000 tokens of prompt and tool schemas) to `full`:

| Profile | Adds | Alias |
|---|---|---|
| `core` | read/bash/edit/write, extensions (real Pi / Oh My Pi ones, via a Node host), skills, prompt templates, JSONL sessions | `pi` |
| `lean` | grep, find, ls | |
| `coder` | todo list, subagents (`task`), clarifying questions (`ask`), web `fetch`, context checkpoints, plan mode (`/plan`), AST search | `omp` |
| `assistant` | persistent and archival memory, self-written skills, shared skills, Hermes plugins, search, tracing, resumable runs | `hermes` |
| `balanced` | todo, ask, fetch, MCP registry, Hermes plugins, Omni tools (browser, LSP), permission gate | |
| `autonomous` | + subagents, memory, shared skills, OpenClaw plugins, autonomous-work prompt | `openclaw` |
| `full` | everything, including browser control | |

Each addition is a **capability** you can switch on or off independently, and every one reports what it costs in
context tokens:

```text
omni                          open the window with the active profile
omni profile [name]           list / set the active profile            (/profile inside the window)
omni --profile lean ...       one run with another profile
omni capabilities             list modules and host switches           (/capabilities inside the window)
omni toggle memory on         switch one module, for every profile
omni --capabilities todo,ask  exact module list for one run
omni run "prompt"             one-shot answer
omni install npm:<pkg>        Pi packages (also git:/local; remove / update / list)
omni mcp add <name> ...       MCP servers (remove / list / login / logout; /mcp inside the window)
omni gateway                  answer from chat apps (Telegram, HTTP); `approve <code>` pairs a sender
omni cron list | run          scheduled jobs
omni login <provider>         subscription sign-in: anthropic, google-antigravity, openai-codex, github-copilot
omni legacy                   the previous REPL; subcommands like `omni models` still use the previous CLI
```

`/profile new` walks you through building your own profile (base, modules, name); `/profile save <name>` stores the current setup as one. Both write to `~/.omni/profiles/`. Profiles are JSON
files that `extends` each other; see [docs/CAPABILITIES.md](docs/CAPABILITIES.md) for the format, the module list,
measured context costs, and what is and is not yet covered from Pi, Oh My Pi, Hermes and OpenClaw.

Pi features in every profile: MCP servers (`~/.pi/agent/mcp.json`), `codemode` and `tool_search`, Pi packages,
image resizing and inline images (kitty / iTerm2 protocols), `"tuiMode": "regular"` for a non-fullscreen window,
parallel read-only tools, `/share` via Radius or a gist, and the extension UI hooks (custom editors, autocomplete
providers, terminal input, markdown transformers, virtual models, TSX extensions).
Details: [docs/PI_PROFILE.md](docs/PI_PROFILE.md) · roadmap: [docs/INTEGRATION_PLAN.md](docs/INTEGRATION_PLAN.md).

## Quickstart

### Installation
Clone the repository and install requirements:
```bash
git clone https://github.com/justlearnwithme314-ops/omni.git
cd omni
pip install -r requirements.txt
```

### Launch Interactive REPL
```bash
omni
```
Use arrow keys for command history and Tab for slash command auto-completions (`/model`, `/mode`, `/toggle`, `/tools`, `/keys`, `/undo`, `/diff`, `/memory`, `/cron`, `/clear`, `/help`).

### Command-Line Usage
```bash
# 1. View Model Catalog & Active Providers
omni models

# 2. Set Default Model
omni model flash
omni model sonnet

# 3. Run a One-Shot Prompt
omni run "Say hello in 5 words" -m flash

# 4. View and Toggle Features
omni toggle
omni toggle subagents off
omni toggle subagents on

# 5. Switch Runtime Profile
omni mode lean
omni mode autonomous

# 6. View Discovered Tools
omni plugins

# 7. Check Telemetry & Costs
omni stats
```

---

## Verification & Testing
Omni includes 24 comprehensive unit and integration tests covering all subsystems:
```bash
python -m unittest tests.test_harness tests.test_features_deep tests.test_new_features -v
```

---

## License
MIT License.

## Configuration & environment

Omni locates everything relative to its own install directory or your current project, never a fixed drive path.

| Variable | Meaning | Default |
|---|---|---|
| `OMNI_CONFIG` | settings file | `<install>/harness.config.json` |
| `OMNI_WORKSPACE` | project being worked on (checkpoints, AST search, worktrees) | current directory |
| `OMNI_HOME` | per-user state (benchmarks, paired nodes, browser profile, worktrees) | `~/.omni` |
| `OMNI_HISTORY` | prompt history for ghost suggestions | `~/.omni_history` |
| `OMNI_AUTH_DB` | credentials database | `<install>/agent_auth.db` |
| `OMNI_TELEGRAM_TOKEN`, `OMNI_DISCORD_TOKEN`, `OMNI_SLACK_TOKEN`, `OMNI_WHATSAPP_TOKEN` | chat gateway credentials | unset |

The test suite (`python -m unittest discover tests`) points all of these at a scratch directory (`tests/_isolation.py`).

### What is real and what is not yet implemented

* Working: browser relay (real CDP; Chrome/Edge/Brave), shadow git worktrees, Docker sandbox (refuses to run without Docker), collab and canvas servers (loopback + key/token), Telegram polling, local llama.cpp client, text-to-speech.
* Not implemented yet: speech input/wake word, Discord/Slack/WhatsApp inbound sockets, remote execution on paired nodes, benchmark runner, Firefox automation (WebDriver BiDi).
