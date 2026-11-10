# Omni core and the Pi profile

**Update:** the runtime described here is now Omni's core for every profile. `omni` opens it with the active profile
(`profiles/*.json`): `pi` is barebones Pi, `lean` adds grep/find/ls, `balanced` adds Omni's tool registry (MCP,
Hermes, Omni tools) and the permission gate, `autonomous`/`full` add OpenClaw skills, `evolve` adds self-evolution
bookkeeping. Switch with `omni profile <name>` or `/profile`. The previous REPL is `omni legacy`.

The Pi profile is a self-contained re-implementation of the Pi coding agent (`pi-mono/packages/coding-agent`, tracked
release 0.78) inside Omni, in `src/pi/`. It aims to behave like Pi down to tool schemas, error strings, session files
and the extension API, so that Pi prompts, skills, sessions and extensions work unchanged.

It does not depend on the rest of Omni at runtime (only the credential store is shared), so it is also the lean
baseline that heavier profiles build on (see `INTEGRATION_PLAN.md`).

## Running it

```text
omni pi                         interactive full-screen UI
omni pi -c                      continue the latest session in this folder
omni pi -p "explain src/"       print mode (final answer to stdout)
omni pi --mode json "..."       JSONL event stream
omni pi --mode rpc              JSONL commands on stdin (Pi's RPC protocol, common subset)
omni pi --model sonnet --thinking high -e ./my-ext.ts
omni --profile pi ...           same as `omni pi ...`
omni profile pi                 make pi the default profile for bare `omni` / `omni run`
omni legacy                     the previous Omni REPL
omni install npm:@x/pi-tools    Pi packages: install / remove / update / list (also `omni pi install ...`, -l = project)
omni mcp add docs --url https://example.com/mcp      MCP servers: add / remove / list / login / logout
python -m src.pi ...            direct entry point (no Omni imports)
```

All of Pi's CLI flags are accepted: `--provider --model --api-key --thinking --models --list-models -c -r --session
--session-dir --no-session -n -t -xt -nbt -nt -e -ne --skill -ns --prompt-template -np -nc --system-prompt
--append-system-prompt --use-theme -a -na`, plus `@file` arguments and piped stdin, `--no-mcp`, and `-e npm:<pkg>` / `-e git:<repo>` for a package that is used for one run
only (cached under `<agent-dir>/tmp/extensions`). Omni adds `--no-omni-plugins`.

## What is implemented

| Area | Behaviour | Source |
|---|---|---|
| Tools | `read bash edit write grep find ls` with Pi's names, JSON schemas, descriptions, prompt snippets, guidelines, truncation limits (2000 lines / 50 KB) and the exact result / error texts. `edit` uses multi-edit `edits[]`, fuzzy matching (NFKC, smart quotes, dashes, trailing spaces), uniqueness/overlap checks, CRLF + BOM preservation, unified diff. `grep`/`find` use ripgrep / fd when installed and a `.gitignore`-aware fallback otherwise. `bash` finds Git Bash on Windows, streams output, tail-truncates and spills to a temp file. Defaults: `read bash edit write`; `defaultTools` / `--tools` support `+name` / `-name`. | `src/pi/tools/` |
| System prompt | Ported section by section (`preamble tools rules addendum project_context skills cwd`), including tool-dependent rules and `<available_skills>`. `SYSTEM.md` / `APPEND_SYSTEM.md` (project overrides global), `--system-prompt`, `--append-system-prompt`. | `src/pi/prompt.py` |
| Context files | `AGENTS.override.md`, `AGENTS.md`, `CLAUDE.md` from the agent dir and every ancestor of cwd. `-nc` disables. | `resources.py` |
| Skills | Agent Skills spec: `<agent-dir>/skills`, `~/.agents/skills`, `.pi/skills`, `.agents/skills` up to the repo root, settings + `--skill`. Validation warnings, collisions, `disable-model-invocation`, `/skill:name args` expansion with Pi's `<skill>` block. | `resources.py` |
| Prompt templates | `prompts/*.md` as `/name`, frontmatter `description` / `argument-hint`, `$1 $@ $ARGUMENTS ${1:-default} ${@:N} ${@:N:L}`, Pi's quoting rules. | `resources.py` |
| Sessions | JSONL v3 tree, file-compatible with Pi: `~/.pi/agent/sessions/--<cwd>--/<ts>_<uuid>.jsonl`, entry types `message model_change thinking_level_change compaction branch_summary custom custom_message label session_info context_edit`, v1/v2 migration, branch navigation, fork / clone into new files with `parentSession`, `--continue`, `--session <path|id-prefix>`, import / export (JSONL + HTML). | `session.py` |
| Agent loop | Pi's event order; steering (`Enter` while working) and follow-up (`Alt+Enter`) queues with `one-at-a-time` / `all` modes; abort; `!cmd` / `!!cmd` with `bashExecution` messages; auto-retry with exponential backoff and `Retry-After`; auto-compaction at `contextWindow - reserveTokens`, manual `/compact [focus]`, Pi's summary prompts; branch summaries on `/tree`. | `agent.py` |
| Providers | Streaming SSE, stdlib only: Anthropic Messages (prompt caching, thinking budgets, OAuth tokens), OpenAI-compatible (OpenAI, OpenRouter, Groq, xAI, DeepSeek, Mistral, Cerebras, Z.ai, Ollama, LM Studio, llama.cpp), Google Gemini, Google Antigravity (thinking level → wire-model suffix, unary fallback), `faux` offline test model. `models.json` (`$ENV`, `!command` values, `modelOverrides`), extension `registerProvider` incl. JS `streamSimple`. | `ai/` |
| Credentials | Runtime overrides → `~/.pi/agent/auth.json` → Omni keyring (`agent_auth.db`, OAuth refresh, 429/401 cooldown blocks) → env vars. `/login` (and `omni login <provider>` in a terminal) signs in with a subscription over OAuth, saves an API key, or imports OMP's OAuth sessions. OAuth (ported from Pi/OMP): **Anthropic** Claude Pro/Max (PKCE, loopback `:54545/callback` with free-port fallback, pasted redirect URL / `code#state`, copy-code mode for SSH) and **Google Antigravity** (Google PKCE on `:51121/oauth-callback`, Cloud Code project discovery and free-tier onboarding; the `projectId` is sent with requests). Tokens are stored in Pi's `auth.json` OAuth format and refreshed a minute before expiry. The Antigravity OAuth client comes from `OMNI_ANTIGRAVITY_CLIENT_ID`/`_SECRET` or an Oh My Pi checkout (`OMNI_OMP_DIR`, or `oh-my-pi` next to Omni). | `ai/auth.py`, `ai/oauth.py` |
| Commands | All Pi built-ins: `/settings /model /tree /thinking /scoped-models /export /import /share /mcp /bug /copy /name /session /changelog /hotkeys /fork /clone /trust /login /logout /new /compact /resume /reload /quit`, plus `/extensions /debug /theme /profile /commands`, template and skill commands, extension commands. | `commands.py` |
| Command sources | Every command has a source prefix: `pi:` built-ins, `omni:` additions, `<extension-name>:` per extension, `prompt:` / `project:` / `<package>-prompts:` templates, `skill:` (a shadowed duplicate skill: `skill-<source>:`). `/<prefix>:<name>` always runs that source. When several sources provide `/name` (two extensions, a project template overriding a user one, colliding skills), all of them are kept: the `/` menu lists one row per source, `/name` asks which to run (just this time, or always), and print/RPC modes use the remembered choice or Pi's order (built-in > extension > template > skill). `/commands` manages it: duplicates, preferred source per name, renaming a source's prefix (`commandSources` / `commandPrefixes` in settings). | `command_sources.py` |
| Project trust | `.pi/` resources load only for trusted projects; prompt at startup, `/trust`, `-a` / `-na`, `defaultProjectTrust`. | `resources.py` |
| MCP | `~/.pi/agent/mcp.json` + trusted `.pi/mcp.json` (project replaces user entries; `enabled` / `exposure` / `toolExposure`-only entries override), `${ENV}` / `!command` values, stdio and streamable HTTP (JSON or SSE responses, `Mcp-Session-Id`, retries on 408/429/5xx), OAuth sign-in (discovery, dynamic client registration, PKCE, refresh; `mcp-auth.json`). Tools are `mcp__<server>__<tool>` (sanitized, hash suffix on collisions); exposure `codemode` (default) / `deferred` / `direct` / `hidden` with per-tool glob patterns; codemode or `tool_search` is switched on as servers connect; servers connect in the background and the first prompt waits up to 10 s only for `direct` ones. `list_mcp_resources`, `list_mcp_resource_templates`, `read_mcp_resource`; 20 KB middle truncation with the full text in a temp file; the "MCP servers whose tools are not declared directly" prompt section; `mcp.log` with 5 MB rotation; `/mcp` (status, tools, reconnect, sign in/out, exposure, enable/disable) and `omni mcp ...`. | `mcp.py`, `mcp_oauth.py` |
| codemode | `codemode` runs a JavaScript script (async function body) in a Node `vm` context with only the codemode globals: `tools.<name>()` (nested calls go through the normal tool pipeline — validation, permission gate, `tool_call` / `tool_result` hooks — and run in parallel when awaited together), `text() image() console.* return exit()`, `store()/load()` persisted as `codemode-store` entries per branch, `ALL_TOOLS`, `searchTools()` (BM25), `describeTool()`, `describeNamespace()`, `models.*` catalog lookups. `// @options:` line (`max_output_tokens`, `timeout_ms`), Pi's result format (`Script completed`/`failed`, wall time, `==> text N/M <==`, `<console_output>`, `Script error:`), unsettled-promise detection. `codemode.mode` `on` / `only` and `codemode.inlineBudget` control the description. Enable with `"defaultTools": ["+codemode"]` or `--tools ...,codemode`. Classifier / image models are listed but cannot run yet. | `codemode.py`, `codemode_runner.mjs` |
| tool_search | Pi's BM25 ranker (name / description / namespace fields, exact-name and full-local-name bonuses, stop words, singular forms); loads matches for the next call and records them on the branch. | `toolsearch.py` |
| Packages | `install / remove / update / list` for `npm:` (`<agent-dir>/npm`), `git:` / URLs (`<agent-dir>/git/<host>/<path>`, ref pinning) and local paths, `-l` for project scope, settings `packages` with per-type filters (`[]`, globs, `!pattern`, `+path`, `-path`), `autoload: false` project deltas, identity de-duplication, `pi` manifest or conventional `extensions/ skills/ prompts/ themes/`; missing packages are installed at startup and skipped with a warning when that fails. | `packages.py` |
| Images | `read` and tool / MCP results resize images over 2000×2000 or ~4.5 MB (Pillow when installed — PNG and JPEG candidates, lower JPEG qualities, 75 % steps — else a pure-Python PNG path) with Pi's dimension note; `images.autoResize`, `images.blockImages`. Inline display with the kitty graphics protocol (kitty, Ghostty, WezTerm) or iTerm2 images (iTerm2, WezTerm, VS Code); `terminal.showImages`, `terminal.images` (`auto` / `kitty` / `iterm2` / `none`). Other terminals show a placeholder. | `images.py`, `tui/inline_images.py` |
| Parallel tools | Consecutive read-only tools of one assistant message (`read grep find ls`, read-only MCP tools) run concurrently; results keep the call order. `parallelReadOnlyTools: false` turns it off. | `agent.py` |
| /share | Radius artifact (`https://radius.pi.dev/v1/artifacts`, `pi.share` trailing entry with system prompt + tool schemas) when a Radius token is configured (`/login radius` or `RADIUS_API_KEY`), else a private gist shown through `https://pi.dev/session/#<id>`; `sessionShareDestination` `auto` / `gist` / `radius`. | `share.py` |
| Modes | interactive (full-screen, or `"tuiMode": "regular"` / `OMNI_TUI_MODE=regular` to keep the terminal's scrollback: finished output is printed once, only the live area is redrawn), print, json, rpc (prompt, steer, follow_up, abort, clear_queue, new_session, get_state, get_messages, set_model, cycle_model, get_available_models, thinking level commands, steering / follow-up modes, compact, auto compaction / retry toggles, bash, get_session_stats, export_html, switch_session, fork, get_commands, set_session_name, get_last_assistant_text). | `modes.py` |

### Extension host

Real Pi (and OMP) extensions run in a Node process (`src/pi/extensions/host/host.mjs`, Node ≥ 22.13) that talks JSON
lines to Python:

- **Loading** is the same as Pi's: `~/.pi/agent/extensions`, `.pi/extensions` (trusted), `settings.json` `extensions`,
  `-e`, and Omni's `plugins/pi/` — single files, `dir/index.ts`, or `package.json` with `pi.extensions`.
- **TypeScript** is compiled with Node's own transform (enums and parameter properties work). **TSX / JSX** use
  `typescript`, `esbuild` or `sucrase` when one can be resolved, else a built-in JSX transform (automatic runtime
  from `jsxImportSource`, default `react`; `/** @jsx h */` or `"jsx": "react"` for the classic runtime).
  Extension-less and `.js`→`.ts` imports resolve like jiti.
- **Host packages** `@earendil-works/*`, `@mariozechner/*`, `@oh-my-pi/*`, `typebox`, `@sinclair/typebox` map to
  Omni shims (`host/shims/`). A named import the shims don't implement becomes a recorded stub instead of a load
  failure, and `/extensions` lists it under "not yet supported".
- **API surface:** all `pi.on` events (the agent emits Pi's full set, with Pi's composition rules: tool_call
  block/mutate, tool_result / context / input / message_end / before_provider_request chains,
  before_agent_start prompt + messages, session_before_* cancel, user_bash result, turn_end entries/continue), plus
  `registerTool` (incl. dynamic and `prepareArguments`, `onUpdate` streaming, abort signal), `registerCommand` (+
  argument completions), `registerShortcut`, `registerFlag`/`getFlag`, message / entry / tool renderers,
  `sendMessage` (`steer` / `followUp` / `nextTurn`, `triggerTurn`), `sendUserMessage`, `appendEntry`,
  session name and labels, `exec`, active tools, `getAllTools`, `getCommands`, `setModel`, thinking level,
  `registerProvider` / `unregisterProvider`, `registerVirtualModel` / `unregisterVirtualModel` (routing per request
  with `reason` / `previous` / `failed` / branch `state`; the footer shows the routed model),
  `registerMarkdownTransformer` (with `messageTypes`), `registerMcpServer` / `unregisterMcpServer` / `getMcpServers`,
  `events` bus.
- **ctx:** `ui` (select, confirm, input, editor, notify, setStatus, setWidget with string arrays or components and
  above/below placement, setFooter, setHeader, setTitle, working message, custom components with keyboard input,
  editor text, tools-expanded, theme, `setEditorComponent` / `getEditorComponent` with a real multi-line `Editor` and
  `CustomEditor` that forwards app keys, `addAutocompleteProvider` stacked on the built-in provider, `onTerminalInput`
  with `consume`), `mode` / `hasUI`, `cwd`, a read-only `sessionManager` mirror, `modelRegistry`
  with `authStorage.setRuntimeApiKey`, `isIdle`, `abort`, `compact`, `getContextUsage`, `getSystemPrompt`, and for
  commands `newSession`, `fork`, `navigateTree`, `switchSession`, `reload`, `waitForIdle`.
- **Built-in tool factories** (`createBashTool`, `createReadToolDefinition`, ...) run Omni's built-in tools, so tool
  overrides and wrappers work.

Verified: all 48 example extensions in `pi-mono/packages/coding-agent/examples/extensions` that were tested load, and
the games (snake), gates (permission / protected paths), input transforms, todo with custom renderers,
system-prompt rewriting, commands and status widgets run end to end. `plugins/pi/pi-keyrouter` loads and subscribes
its four events.

### Terminal UI

Full-screen, stdlib-only (Windows Terminal / conhost with VT mode, macOS, Linux): branded header, scrolling
transcript with cards for every tool call (live spinners, durations, coloured edit diffs, collapsible output,
extension renderers), Markdown with code highlighting and tables, thinking blocks, an editor whose border colour shows
the thinking level (amber in `!` shell mode), autocomplete for `/commands` and `@files`, modal pickers, extension
widgets and statuses, and a two-line footer with cwd, git branch, tokens, cost and a context gauge. Themes: `omni`
(default), `tokyo-night`, `catppuccin`, `light`, plus Pi theme JSON files.

Keys follow Pi: Enter send/steer · Alt+Enter follow-up · Shift+Enter / Ctrl+J newline · Esc interrupt (double Esc →
`/tree`) · Ctrl+C clear / twice quit · Ctrl+L model · Ctrl+P cycle models · Shift+Tab thinking · Ctrl+O expand tools ·
Ctrl+T thinking blocks · Ctrl+G external editor · Ctrl+X copy · Alt+↑ un-queue · PgUp/PgDn / wheel scroll.

## Known gaps (honest list)

- OAuth sign-in covers Anthropic and Google Antigravity. ChatGPT/Codex and GitHub Copilot sign-in are not offered yet: their chat APIs (Codex Responses, Copilot token exchange) have no streaming adapter here. The flows are tested against a local fake server, not yet against the live providers.

- Pi's llama.cpp manager and cache warming are not implemented.
- Codemode scripts run in Node's `vm` (not QuickJS): globals are restricted and string `eval` is disabled, but it is
  not a hard security boundary, there is no 256 MB memory cap, and `models.classify()` / `generateImages()` return an
  error result (no classifier or image-model providers yet).
- MCP: `cimd` client registration, the pasted-redirect sign-in over SSH, and per-project "enable in this project"
  overrides from `/mcp` are not implemented; `/mcp` writes changes to the file that defines the server.
- Inline images: no sixel (Windows Terminal shows a placeholder). In full-screen mode an image that is partly
  scrolled out of view is hidden until it is fully visible.
- `user_bash` returning custom `operations` is not supported. Extensions that `import` packages they don't ship need
  `npm install` in their folder (`/extensions` names the missing package).
- Not yet verified against live provider APIs or real MCP servers from this environment; the wire formats follow
  the providers' and the MCP specification's documented APIs.

## Tests

`python -m unittest tests.test_pi_runtime tests.test_pi_features tests.test_command_sources tests.test_oauth -v` (offline; MCP uses a small stdio server
written by the test; codemode and extension tests need Node ≥ 22.13 and skip otherwise).
