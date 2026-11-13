// Omni Pi extension host: loads real Pi / OMP extensions and speaks JSON lines with the Python runtime.
//
//   python -> host  {id, method, params}           host -> python  {id, result} | {id, error}
//   host -> python  {id: "h<n>", method, params}   python -> host  {id, result} | {id, error}
//   notifications   {method, params}  (either direction, no id)
//
// stdout is reserved for the protocol; console output from extensions goes to stderr (the host log).
import { register } from "node:module";
import { spawn } from "node:child_process";
import { existsSync, readFileSync, statSync, writeSync } from "node:fs";
import path from "node:path";
import readline from "node:readline";
import { pathToFileURL } from "node:url";
import { format } from "node:util";

register("./hooks.mjs", import.meta.url);

// ---------------------------------------------------------------------------------------------- stdio hygiene
const toErr = (...a) => process.stderr.write(format(...a) + "\n");
console.log = console.info = console.debug = toErr;
console.warn = (...a) => toErr("[warn]", ...a);
const realOut = (line) => writeSync(1, line + "\n");
process.stdout.write = (chunk, enc, cb) => { process.stderr.write(chunk, enc, cb); return true; };

function send(obj) {
  realOut(JSON.stringify(obj, (_k, v) => (typeof v === "bigint" ? Number(v) : typeof v === "function" ? undefined : v)));
}
let seq = 0;
const pending = new Map();
function rpc(method, params) {
  const id = `h${++seq}`;
  return new Promise((resolve, reject) => {
    pending.set(id, { resolve, reject });
    send({ id, method, params });
  });
}
const notify = (method, params) => send({ method, params });

// ---------------------------------------------------------------------------------------------- state
const state = { cwd: process.cwd(), width: 100, height: 30, activeTools: [], allTools: [], models: [], entries: [], header: {} };
let palette = {};
const hex = (h) => { const m = /^#?([0-9a-f]{6})$/i.exec(h || ""); if (!m) return null; const n = parseInt(m[1], 16); return [(n >> 16) & 255, (n >> 8) & 255, n & 255]; };
const theme = {
  fg(color, text) { const c = hex(palette[color]); return c ? `\x1b[38;2;${c[0]};${c[1]};${c[2]}m${text}\x1b[39m` : String(text); },
  bg(color, text) { const c = hex(palette[color]); return c ? `\x1b[48;2;${c[0]};${c[1]};${c[2]}m${text}\x1b[49m` : String(text); },
  bold: (t) => `\x1b[1m${t}\x1b[22m`, italic: (t) => `\x1b[3m${t}\x1b[23m`, underline: (t) => `\x1b[4m${t}\x1b[24m`,
  strikethrough: (t) => `\x1b[9m${t}\x1b[29m`, inverse: (t) => `\x1b[7m${t}\x1b[27m`, dim: (t) => `\x1b[2m${t}\x1b[22m`,
  getFgAnsi(color) { const c = hex(palette[color]); return c ? `\x1b[38;2;${c[0]};${c[1]};${c[2]}m` : ""; },
  getBgAnsi(color) { const c = hex(palette[color]); return c ? `\x1b[48;2;${c[0]};${c[1]};${c[2]}m` : ""; },
  get name() { return state.themeName || "omni"; },
};

const extensions = [];          // {path, name, ok, error, tools, commands, events, missing}
let current = null;             // extension being loaded (for missing-API attribution)
const handlers = new Map();     // event -> [{ext, fn}]
const tools = new Map();        // name -> {def, ext}
const commands = new Map();     // name -> {opts, ext}
const shortcuts = new Map();    // key -> {opts, ext}
const flags = new Map();        // name -> {opts, ext, value}
const messageRenderers = new Map();
const entryRenderers = new Map();
const toolRenderers = [];
const providers = new Map();    // provider name -> config with functions (custom streamSimple)
const widgets = new Map();      // key -> {component|lines}
const customs = new Map();      // id -> {component, done}
const aborts = new Map();       // toolCallId -> AbortController
let loaded = false;
let tuiModule = null;
const terminalListeners = new Set();
let autocompleteTop = null;      // stacked autocomplete provider (built-in provider at the bottom)
let autocompleteTriggers = new Set();
let editorFactory = undefined;   // ctx.ui.setEditorComponent
let editorComp = null;
const markdownTransformers = new Map();  // ext path -> {fn, types}
const virtualModels = new Map();         // "provider/id" -> definition

const bus = new Map();
const events = {
  on(name, fn) { if (!bus.has(name)) bus.set(name, new Set()); bus.get(name).add(fn); return () => bus.get(name)?.delete(fn); },
  off(name, fn) { bus.get(name)?.delete(fn); },
  emit(name, data) { for (const fn of bus.get(name) || []) { try { fn(data); } catch (e) { toErr(e); } } },
};

globalThis.__omniPi = {
  rpc, theme, get state() { return state; },
  onMissing(label) { if (current) current.missing.add(label); else notify("missing", { label }); },
  requestRender() { rerenderWidgets(); for (const id of customs.keys()) renderCustom(id); if (editorComp) notify("ui.editor_render", editorState()); },
  editorAction(key, data, comp) { notify("editor.action", { key, data, text: comp?.getText?.() ?? "" }); },
  registerApi(api, fns) { providers.set(`api:${api}`, fns); },
};

// ---------------------------------------------------------------------------------------------- ui context
function makeUI() {
  const has = () => !!state.hasUI;
  return {
    select: (title, options, opts) => (has() ? rpc("ui.select", { title, options, opts: plain(opts) }) : Promise.resolve(undefined)),
    confirm: (title, message, opts) => (has() ? rpc("ui.confirm", { title, message, opts: plain(opts) }) : Promise.resolve(false)),
    input: (title, placeholder, opts) => (has() ? rpc("ui.input", { title, placeholder, opts: plain(opts) }) : Promise.resolve(undefined)),
    editor: (title, prefill) => (has() ? rpc("ui.editor", { title, prefill }) : Promise.resolve(undefined)),
    notify: (message, type = "info") => notify("ui.notify", { message: String(message), type }),
    onTerminalInput: (fn) => {
      if (typeof fn !== "function") return () => {};
      terminalListeners.add(fn);
      notify("ui.terminalInput", { count: terminalListeners.size });
      return () => { terminalListeners.delete(fn); notify("ui.terminalInput", { count: terminalListeners.size }); };
    },
    setStatus: (key, text) => notify("ui.setStatus", { key, text: text ?? null }),
    setWorkingMessage: (message) => notify("ui.setWorkingMessage", { message: message ?? null }),
    setWorkingVisible: (visible) => notify("ui.setWorkingVisible", { visible }),
    setWorkingIndicator: (options) => notify("ui.setWorkingIndicator", { options: plain(options) }),
    setHiddenThinkingLabel: (label) => notify("ui.setHiddenThinkingLabel", { label: label ?? null }),
    setWidget: (key, content, options) => setWidget(key, content, options),
    setFooter: (factory) => setWidget("__footer", factory, { placement: "footer" }),
    setHeader: (factory) => setWidget("__header", factory, { placement: "header" }),
    setTitle: (title) => notify("ui.setTitle", { title }),
    custom: (factory, options) => openCustom(factory, options),
    pasteToEditor: (text) => notify("ui.pasteToEditor", { text }),
    setEditorText: (text) => { state.editorText = text; notify("ui.setEditorText", { text }); },
    getEditorText: () => state.editorText || "",
    addAutocompleteProvider: (factory) => {
      const below = autocompleteTop || baseAutocomplete;
      const p = typeof factory === "function" ? factory(below) : factory;
      if (!p) return;
      for (const t of p.triggerCharacters || []) autocompleteTriggers.add(t);
      autocompleteTop = { ...p, getSuggestions: p.getSuggestions?.bind(p) ?? below.getSuggestions, applyCompletion: p.applyCompletion?.bind(p) ?? below.applyCompletion,
        shouldTriggerFileCompletion: p.shouldTriggerFileCompletion?.bind(p) ?? below.shouldTriggerFileCompletion };
      notify("ui.autocomplete", { active: true, triggerCharacters: [...autocompleteTriggers] });
    },
    setEditorComponent: (factory) => setEditor(factory),
    getEditorComponent: () => editorFactory,
    getAllThemes: () => [{ name: theme.name, path: undefined }],
    getTheme: () => theme,
    setTheme: (t) => { notify("ui.setTheme", { theme: typeof t === "string" ? t : t?.name }); return { success: true }; },
    getToolsExpanded: () => !!state.toolsExpanded,
    setToolsExpanded: (v) => { state.toolsExpanded = v; notify("ui.setToolsExpanded", { expanded: v }); },
    get theme() { return theme; },
  };
}
const plain = (o) => (o ? JSON.parse(JSON.stringify(o, (_k, v) => (typeof v === "function" || v instanceof AbortSignal ? undefined : v))) : undefined);

function tuiObject() {
  const T = tuiModule?.TUI;
  const t = T ? new T() : {};
  t.requestRender = () => globalThis.__omniPi.requestRender();
  return t;
}
const keybindings = { matches: (data, action) => tuiModule?.matchesKey?.(data, action) ?? false, getKeys: () => [] };

function renderComponent(c, width) {
  try {
    if (!c) return [];
    if (Array.isArray(c)) return c.map(String);
    if (typeof c.render === "function") return c.render(width).map(String);
  } catch (e) { return [`[render error: ${e?.message || e}]`]; }
  return [];
}

function setWidget(key, content, options) {
  const old = widgets.get(key);
  old?.component?.dispose?.();
  if (content === undefined || content === null) { widgets.delete(key); notify("ui.setWidget", { key, lines: null }); return; }
  const entry = { options: plain(options) || {} };
  if (typeof content === "function") {
    entry.component = content(tuiObject(), theme);
  } else entry.lines = Array.isArray(content) ? content.map(String) : [String(content)];
  widgets.set(key, entry);
  notify("ui.setWidget", { key, lines: entry.lines || renderComponent(entry.component, state.width), options: entry.options });
}
function rerenderWidgets() {
  for (const [key, w] of widgets) if (w.component) notify("ui.setWidget", { key, lines: renderComponent(w.component, state.width), options: w.options });
}

let customSeq = 0;
function openCustom(factory, options = {}) {
  const id = `c${++customSeq}`;
  return new Promise((resolve) => {
    const done = (result) => {
      const c = customs.get(id);
      customs.delete(id);
      c?.component?.dispose?.();
      notify("ui.custom_close", { id });
      resolve(result);
    };
    let component;
    try { component = factory(tuiObject(), theme, keybindings, done); }
    catch (e) { notify("ui.notify", { message: `custom UI failed: ${e?.message || e}`, type: "error" }); resolve(undefined); return; }
    Promise.resolve(component).then((comp) => {
      customs.set(id, { component: comp, done });
      if (!state.hasUI) { done(undefined); return; }
      notify("ui.custom_open", { id, overlay: !!options?.overlay, lines: renderComponent(comp, state.width) });
    });
  });
}
function renderCustom(id) {
  const c = customs.get(id);
  if (c) notify("ui.custom_render", { id, lines: renderComponent(c.component, state.width) });
}

// ---------------------------------------------------------------------------------------------- editor / autocomplete
const baseAutocomplete = {
  getSuggestions: (lines, line, col, options) => rpc("autocomplete.base", { lines, line, col, options: plain(options) }),
  applyCompletion(lines, line, col, item, prefix) {
    const cur = lines[line] ?? "";
    const start = Math.max(0, col - String(prefix ?? "").length);
    const value = String(item?.value ?? item?.label ?? "");
    const out = [...lines];
    out[line] = cur.slice(0, start) + value + cur.slice(col);
    return { lines: out, cursorLine: line, cursorCol: start + value.length };
  },
  shouldTriggerFileCompletion: () => true,
};
function editorState() {
  if (!editorComp) return { active: false };
  return { active: true, lines: renderComponent(editorComp, state.width), text: editorComp.getText?.() ?? "" };
}
function setEditor(factory) {
  editorComp?.dispose?.();
  editorFactory = typeof factory === "function" ? factory : undefined;
  editorComp = null;
  if (editorFactory) {
    try {
      editorComp = editorFactory(tuiObject(), theme, keybindings);
      if (editorComp && state.editorText) editorComp.setText?.(state.editorText);
      if (editorComp) {
        const prev = editorComp.onSubmit;
        editorComp.onSubmit = (text) => { try { prev?.(text); } catch {} notify("editor.submit", { text }); };
        editorComp.onChange ??= (text) => { state.editorText = text; };
      }
    } catch (e) {
      notify("ui.notify", { message: `custom editor failed: ${e?.message || e}`, type: "error" });
      editorFactory = undefined; editorComp = null;
    }
  }
  notify("ui.editor_render", editorState());
}

// ---------------------------------------------------------------------------------------------- contexts
function sessionManager() {
  const byId = () => new Map(state.entries.map((e) => [e.id, e]));
  const branch = (fromId) => {
    const m = byId(); const out = []; let cur = m.get(fromId ?? state.leafId);
    while (cur) { out.unshift(cur); cur = cur.parentId ? m.get(cur.parentId) : undefined; }
    return out;
  };
  return {
    getEntries: () => state.entries,
    getBranch: (fromId) => branch(fromId),
    getPath: (fromId) => branch(fromId),
    getLeafId: () => state.leafId ?? null,
    getLeafEntry: () => byId().get(state.leafId),
    getEntry: (id) => byId().get(id),
    getChildren: (id) => state.entries.filter((e) => e.parentId === id),
    getHeader: () => state.header,
    getSessionFile: () => state.sessionFile || undefined,
    getSessionId: () => state.sessionId,
    getSessionDir: () => state.sessionDir,
    getCwd: () => state.cwd,
    getSessionName: () => state.sessionName,
    getLabel: (id) => { let l; for (const e of state.entries) if (e.type === "label" && e.targetId === id) l = e.label; return l; },
    getTree: () => {
      const nodes = new Map(state.entries.map((e) => [e.id, { entry: e, children: [] }]));
      const roots = [];
      for (const n of nodes.values()) (n.entry.parentId && nodes.get(n.entry.parentId) ? nodes.get(n.entry.parentId).children : roots).push(n);
      return roots;
    },
    buildSessionContext: () => ({ messages: branch().filter((e) => e.type === "message").map((e) => e.message), thinkingLevel: state.thinkingLevel, model: state.model }),
    isPersisted: () => !!state.sessionFile,
  };
}
function modelRegistry() {
  return {
    getAll: () => state.models,
    getAvailable: () => state.models.filter((m) => m.available !== false),
    find: (provider, id) => state.models.find((m) => m.provider === provider && m.id === id),
    getApiKey: (model) => rpc("auth.getApiKey", { provider: model?.provider ?? model }),
    getApiKeyForProvider: (provider) => rpc("auth.getApiKey", { provider }),
    refresh: () => rpc("models.refresh", {}),
    authStorage: {
      setRuntimeApiKey: (provider, key) => notify("auth.setRuntimeApiKey", { provider, key }),
      removeRuntimeApiKey: (provider) => notify("auth.setRuntimeApiKey", { provider, key: null }),
      getApiKey: (provider) => rpc("auth.getApiKey", { provider }),
      has: (provider) => rpc("auth.has", { provider }),
      hasAuth: (provider) => rpc("auth.has", { provider }),
    },
  };
}
function makeContext(extra = {}) {
  const ctx = {
    ui: makeUI(),
    get mode() { return state.mode || "interactive"; },
    get hasUI() { return !!state.hasUI; },
    get cwd() { return state.cwd; },
    sessionManager: sessionManager(),
    modelRegistry: modelRegistry(),
    get model() { return state.model; },
    get scopedModels() { return state.scopedModels || []; },
    get thinkingLevel() { return state.thinkingLevel; },
    isIdle: () => !!state.isIdle,
    isProjectTrusted: () => !!state.trusted,
    signal: extra.signal,
    abort: () => notify("ctx.abort", {}),
    hasPendingMessages: () => !!state.pendingMessages,
    shutdown: () => notify("ctx.shutdown", {}),
    getContextUsage: () => state.contextUsage,
    compact: (options) => notify("ctx.compact", { customInstructions: options?.customInstructions }),
    getSystemPrompt: () => state.systemPrompt || "",
    ...extra,
  };
  return ctx;
}
function makeCommandContext() {
  return makeContext({
    getSystemPromptOptions: () => state.systemPromptOptions || {},
    waitForIdle: () => rpc("ctx.waitForIdle", {}),
    newSession: async (options) => {
      const r = await rpc("ctx.newSession", { parentSession: options?.parentSession });
      if (r?.cancelled) return { cancelled: true };
      if (options?.withSession) await options.withSession(makeCommandContext());
      return { cancelled: false };
    },
    fork: async (entryId, options) => {
      const r = await rpc("ctx.fork", { entryId, position: options?.position || "before" });
      if (!r?.cancelled && options?.withSession) await options.withSession(makeCommandContext());
      return r || { cancelled: false };
    },
    navigateTree: (targetId, options) => rpc("ctx.navigateTree", { targetId, ...plain(options) }),
    switchSession: async (sessionPath, options) => {
      const r = await rpc("ctx.switchSession", { sessionPath });
      if (!r?.cancelled && options?.withSession) await options.withSession(makeCommandContext());
      return r || { cancelled: false };
    },
    reload: () => rpc("ctx.reload", {}),
  });
}

// ---------------------------------------------------------------------------------------------- ExtensionAPI
function makeAPI(ext) {
  const api = {
    on(event, fn) {
      if (!handlers.has(event)) handlers.set(event, []);
      const h = { ext, fn };
      handlers.get(event).push(h);
      ext.events.add(event);
      if (loaded) notify("events_changed", { events: [...handlers.keys()] });
      return () => { const l = handlers.get(event); if (l) handlers.set(event, l.filter((x) => x !== h)); };
    },
    registerTool(def) {
      if (!def?.name) return;
      tools.set(def.name, { def, ext });
      ext.tools.add(def.name);
      if (loaded) notify("tools_changed", { tools: toolList() });
    },
    // keyed by extension + name so two extensions can both register `/name`; Omni lets the user pick the source
    registerCommand(name, opts) { commands.set(commandId(ext, name), { name, opts: typeof opts === "function" ? { handler: opts } : opts, ext }); ext.commands.add(name); if (loaded) notify("commands_changed", { commands: commandList() }); },
    registerShortcut(key, opts) { shortcuts.set(String(key).toLowerCase(), { opts, ext }); },
    registerFlag(name, opts) { flags.set(name, { opts, ext, value: state.flags?.[name] ?? opts?.default }); },
    getFlag(name) { return flags.get(name)?.value ?? state.flags?.[name]; },
    registerMessageRenderer(type, r) { messageRenderers.set(type, r); },
    registerEntryRenderer(type, r) { entryRenderers.set(type, r); },
    registerToolRenderer(resolver) { toolRenderers.push(resolver); },
    registerMarkdownTransformer(fn, options) {
      if (typeof fn !== "function") { markdownTransformers.delete(ext.path); }
      else markdownTransformers.set(ext.path, { fn, types: options?.messageTypes ? new Set(options.messageTypes) : null });
      notify("ui.markdownTransformers", { count: markdownTransformers.size });
    },
    sendMessage(message, options) { notify("pi.sendMessage", { message: plain(message), options: plain(options) }); },
    sendUserMessage(content, options) { notify("pi.sendUserMessage", { content: plain(content), options: plain(options) }); },
    appendEntry(customType, data) { notify("pi.appendEntry", { customType, data: plain(data) }); },
    setSessionName(name) { state.sessionName = name; notify("pi.setSessionName", { name }); },
    getSessionName() { return state.sessionName; },
    setLabel(entryId, label) { notify("pi.setLabel", { entryId, label: label ?? null }); },
    exec(command, args = [], options = {}) {
      return new Promise((resolve) => {
        const child = spawn(command, args, { cwd: options.cwd || state.cwd, shell: process.platform === "win32", signal: options.signal, env: { ...process.env, ...(options.env || {}) } });
        let stdout = "", stderr = "", killed = false;
        const timer = options.timeout ? setTimeout(() => { killed = true; child.kill(); }, options.timeout) : null;
        child.stdout?.on("data", (d) => (stdout += d));
        child.stderr?.on("data", (d) => (stderr += d));
        child.on("error", (e) => { stderr += String(e?.message || e); });
        child.on("close", (code) => { if (timer) clearTimeout(timer); resolve({ stdout, stderr, code: code ?? (killed ? 124 : 1), killed }); });
      });
    },
    getActiveTools() { return [...state.activeTools]; },
    getAllTools() { return state.allTools.map((t) => ({ name: t.name, description: t.description, parameters: t.parameters, sourceInfo: { source: t.source } })); },
    getSettings() { return state.settings || {}; },
    setActiveTools(names) { state.activeTools = [...names]; notify("pi.setActiveTools", { names }); },
    getCommands() { return state.commands || []; },
    async setModel(model) { return rpc("pi.setModel", { provider: model?.provider, id: model?.id }); },
    getThinkingLevel() { return state.thinkingLevel; },
    setThinkingLevel(level) { state.thinkingLevel = level; notify("pi.setThinkingLevel", { level }); },
    registerProvider(nameOrProvider, config) {
      const name = typeof nameOrProvider === "string" ? nameOrProvider : nameOrProvider?.name || nameOrProvider?.id;
      const cfg = typeof nameOrProvider === "string" ? config : nameOrProvider;
      if (!name || !cfg) return;
      providers.set(name, cfg);
      const serial = plain(cfg) || {};
      if (typeof cfg.streamSimple === "function" || typeof cfg.stream === "function") serial.api = `ext:${name}`;
      const oa = cfg.oauth;
      if (oa && typeof oa.login === "function") {  // Pi's custom OAuth provider: login / refreshToken / getApiKey run here
        serial.oauth = { name: oa.name || name, isSubscription: !!oa.isSubscription,
          hasRefresh: typeof (oa.refreshToken || oa.refresh) === "function", hasGetApiKey: typeof oa.getApiKey === "function" };
      } else delete serial.oauth;
      notify("pi.registerProvider", { name, config: serial });
    },
    unregisterProvider(name) { providers.delete(name); notify("pi.unregisterProvider", { name }); },
    registerMcpServer(name, config) { notify("pi.registerMcpServer", { name, config: plain(config) }); },
    unregisterMcpServer(name) { notify("pi.unregisterMcpServer", { name }); },
    getMcpServers() { return state.mcpServers || []; },
    registerVirtualModel(def) {
      if (!def?.provider || !def?.id || typeof def.route !== "function") return;
      virtualModels.set(`${def.provider}/${def.id}`, def);
      notify("pi.registerVirtualModel", { model: { provider: def.provider, id: def.id, name: def.name || def.id, thinkingLevels: def.thinkingLevels || ["off"],
        contextWindow: def.contextWindow, maxTokens: def.maxTokens, input: def.input || ["text", "image"] } });
    },
    unregisterVirtualModel(provider, id) { virtualModels.delete(`${provider}/${id}`); notify("pi.unregisterVirtualModel", { provider, id }); },
    events,
    logger: { info: toErr, warn: toErr, error: toErr, debug: () => {} },
  };
  return new Proxy(api, {
    get(t, p) {
      if (p in t) return t[p];
      if (typeof p === "symbol" || p === "then") return undefined;
      ext.missing.add(`pi.${String(p)}`);
      return () => undefined;
    },
  });
}

const toolList = () => [...tools.entries()].map(([name, { def, ext }]) => ({
  name, label: def.label || name, description: def.description || "", parameters: plain(def.parameters) || { type: "object", properties: {} },
  promptSnippet: def.promptSnippet, promptGuidelines: def.promptGuidelines, source: ext.path,
  hasRenderer: !!(def.renderCall || def.renderResult) || toolRenderers.length > 0, defaultActive: def.defaultActive !== false,
}));
const commandId = (ext, name) => `${ext.path}::${name}`;
const commandList = () => [...commands.entries()].map(([id, { name, opts, ext }]) => ({ id, name, extension: ext.name, description: opts?.description, source: ext.path, hasCompletions: !!opts?.getArgumentCompletions }));
// `id` (exact source) wins; a bare name runs the first extension that registered it (Pi's behaviour)
const findCommand = (id, name) => (id && commands.get(id)) || [...commands.values()].find((c) => c.name === name);

// ---------------------------------------------------------------------------------------------- loading
async function loadOne(p) {
  const origPath = p;
  const ext = { path: origPath, name: path.basename(path.dirname(p)) === "extensions" ? path.basename(p) : path.basename(path.dirname(p)), ok: false, error: null,
    tools: new Set(), commands: new Set(), events: new Set(), missing: new Set(), loadMs: 0, handlerMs: {} };
  const t0 = performance.now();
  if (/index\.(ts|js|mjs)$/.test(p) === false) ext.name = path.basename(p).replace(/\.(ts|js|mjs)$/, "");
  if (/^(dist|src|lib|build|bundle|pi|extension|extensions|index)$/.test(ext.name)) {
    // a build folder says nothing: use the name of the package that owns the file
    for (let d = path.dirname(p), i = 0; i < 5; i++, d = path.dirname(d)) {
      try { const n = JSON.parse(readFileSync(path.join(d, "package.json"), "utf8")).name; if (n) { ext.name = n; break; } } catch {}
    }
  }
  extensions.push(ext);
  current = ext;
  try {
    if (statSync(p).isDirectory()) {
      const idx = ["index.ts", "index.js", "index.mjs"].map((n) => path.join(p, n)).find((f) => existsSync(f));
      if (!idx) throw new Error(`extension path is a directory without index.ts/index.js: ${p}`);
      p = idx;
    }
    const mod = await import(pathToFileURL(p).href);
    const factory = mod.default ?? mod.extension ?? mod.activate;
    if (factory && typeof factory === "object" && typeof factory.register === "function") {
      await registerOpenClawPlugin(factory, ext, path.dirname(p));  // an OpenClaw plugin entry (definePluginEntry)
    } else {
      if (typeof factory !== "function") throw new Error("extension has no default export factory");
      await factory(makeAPI(ext));
    }
    ext.ok = true;
  } catch (e) {
    ext.error = e?.stack?.split("\n").slice(0, 3).join(" | ") || String(e);
    const m = /Cannot find (?:package|module) '([^']+)'/.exec(String(e?.message));
    if (m) ext.error = `missing dependency '${m[1]}' (run npm install in ${path.dirname(p)})`;
  } finally {
    current = null;
    ext.loadMs = Math.round(performance.now() - t0);
  }
}
// ---------------------------------------------------------------------------------------------- OpenClaw plugins
// OpenClaw's agent tools have Pi's tool shape, so a plugin's tools, commands and core hooks map straight onto the Pi
// API. Registrations this core has no slot for (channels, providers, services, gateway methods, ...) are recorded as
// "not yet supported" instead of failing the plugin.
const OPENCLAW_HOOKS = {
  before_tool_call(pi, handler) {
    pi.on("tool_call", async (e) => {
      const r = await handler({ toolName: e.toolName, params: e.input, toolCallId: e.toolCallId }, {});
      if (r?.block) return { block: true, reason: r.blockReason || r.reason || `${e.toolName} blocked by a plugin` };
      if (r?.params && typeof r.params === "object") Object.assign(e.input, r.params);
      return undefined;
    });
  },
  after_tool_call(pi, handler) {
    pi.on("tool_result", async (e) => { await handler({ toolName: e.toolName, params: e.input, result: e.content, isError: e.isError, toolCallId: e.toolCallId }, {}); });
  },
  before_prompt_build(pi, handler) {
    pi.on("before_agent_start", async (e) => {
      const r = await handler({ prompt: e.prompt, messages: [] }, {});
      if (!r) return undefined;
      let sp = r.systemPrompt || e.systemPrompt;
      if (r.prependSystemContext) sp = `${r.prependSystemContext}\n\n${sp}`;
      for (const extra of [r.appendSystemContext, r.prependContext]) if (extra) sp = `${sp}\n\n${extra}`;
      return sp !== e.systemPrompt ? { systemPrompt: sp } : undefined;
    });
  },
  agent_end(pi, handler) { pi.on("agent_end", async (e) => { await handler({ messages: e.messages || [], success: e.outcome !== "error" }, {}); }); },
  session_start(pi, handler) { pi.on("session_start", async () => { await handler({ sessionId: state.sessionId }, {}); }); },
  session_end(pi, handler) { pi.on("session_shutdown", async () => { await handler({ sessionId: state.sessionId }, {}); }); },
  message_received(pi, handler) { pi.on("input", async (e) => { await handler({ content: e.text, from: "user" }, { channelId: "cli" }); return { action: "continue" }; }); },
};
OPENCLAW_HOOKS.before_agent_start = OPENCLAW_HOOKS.before_prompt_build;
OPENCLAW_HOOKS.tool_result = OPENCLAW_HOOKS.after_tool_call;

async function registerOpenClawPlugin(entry, ext, dir) {
  const pi = makeAPI(ext);
  ext.kind = "openclaw";
  if (entry.id || entry.name) ext.name = String(entry.id || entry.name);
  if (entry.__openclawEntry === "channel") ext.missing.add("openclaw.channel (messaging channels need the gateway)");
  const toolContext = { config: {}, workspaceDir: state.cwd, agentDir: state.agentDir, sandboxed: false };
  const base = {
    id: ext.name, name: entry.name || ext.name, source: dir, config: {}, pluginConfig: {},
    logger: { info: toErr, warn: toErr, error: toErr, debug() {} },
    resolvePath: (p) => path.resolve(dir, p),
    registerTool(tool, _opts) {
      const made = typeof tool === "function" ? tool(toolContext) : tool;
      const add = (t) => {
        if (!t || typeof t.execute !== "function" || !t.name) return;
        pi.registerTool({ name: t.name, label: t.label || t.name, description: t.description || "", parameters: t.parameters || { type: "object", properties: {} },
          execute: (id, params, signal, onUpdate) => t.execute(id, params, signal, onUpdate) });
      };
      if (made && typeof made.then === "function") return made.then((v) => [].concat(v || []).forEach(add));
      [].concat(made || []).forEach(add);
      return undefined;
    },
    registerCommand(def) {
      if (!def?.name || typeof def.handler !== "function") return;
      pi.registerCommand(String(def.name).replace(/^\//, ""), { description: def.description || "", handler: async (args, ctx) => {
        const r = await def.handler({ args, commandBody: args, channel: "cli", isAuthorizedSender: true, config: {} });
        const text = typeof r === "string" ? r : r?.text;
        if (text) ctx.ui.notify(String(text), "info");
      } });
    },
    on(name, handler) {
      const map = OPENCLAW_HOOKS[name];
      if (map && typeof handler === "function") map(pi, handler);
      else ext.missing.add(`openclaw.on:${name}`);
    },
    registerHook(names, handler) { for (const n of [].concat(names || [])) base.on(n, handler); },
  };
  const api = new Proxy(base, {
    get(t, prop) {
      if (prop in t || typeof prop === "symbol") return t[prop];
      if (String(prop).startsWith("register")) return () => { ext.missing.add(`openclaw.${String(prop)}`); };
      if (prop === "runtime") return new Proxy({}, { get: (_r, k) => (typeof k === "symbol" ? undefined : () => { ext.missing.add(`openclaw.runtime.${String(k)}`); }) });
      return undefined;
    },
  });
  await entry.register(api);
}

const extReport = () => extensions.map((e) => ({ path: e.path, name: e.name, ok: e.ok, error: e.error, tools: [...e.tools], commands: [...e.commands], events: [...e.events], missing: [...e.missing],
  loadMs: e.loadMs, handlerMs: Object.fromEntries(Object.entries(e.handlerMs).map(([k, v]) => [k, Math.round(v)])) }));

// ---------------------------------------------------------------------------------------------- emit
async function emit(event, payload) {
  const list = handlers.get(event) || [];
  if (!list.length) return null;
  const ev = payload || {};
  const ctx = makeContext();
  const run = async (h, e) => {
    const t = performance.now();
    try { return await h.fn(e, ctx); }
    catch (err) { notify("extension_error", { path: h.ext.path, event, error: String(err?.stack || err) }); return undefined; }
    finally { if (h.ext?.handlerMs) h.ext.handlerMs[event] = (h.ext.handlerMs[event] || 0) + (performance.now() - t); }
  };
  switch (event) {
    case "tool_call": {
      for (const h of list) {
        const r = await run(h, ev);
        if (r?.block) return { block: true, reason: r.reason, input: ev.input, terminate: r.terminate };
      }
      return { input: ev.input };
    }
    case "tool_result": {
      let changed = false;
      for (const h of list) {
        const r = await run(h, ev);
        if (r && typeof r === "object") { for (const k of ["content", "details", "isError", "structuredContent"]) if (k in r) { ev[k] = r[k]; changed = true; } }
      }
      return changed ? { content: ev.content, details: ev.details, isError: ev.isError } : null;
    }
    case "input": {
      let text = ev.text, images = ev.images, transformed = false;
      for (const h of list) {
        const r = await run(h, { ...ev, text, images });
        if (r?.action === "handled") return { action: "handled" };
        if (r?.action === "transform") { text = r.text; images = r.images ?? images; transformed = true; }
      }
      return transformed ? { action: "transform", text, images } : { action: "continue" };
    }
    case "context":
    case "context_with_system": {
      let messages = ev.messages, changed = false;
      for (const h of list) { const r = await run(h, { ...ev, messages }); if (r?.messages) { messages = r.messages; changed = true; } }
      return changed ? { messages } : null;
    }
    case "before_agent_start": {
      let systemPrompt = ev.systemPrompt; const messages = []; let changed = false;
      for (const h of list) {
        const r = await run(h, { ...ev, systemPrompt });
        if (r?.systemPrompt) { systemPrompt = r.systemPrompt; changed = true; }
        if (r?.message) messages.push(r.message);
      }
      return { systemPrompt: changed ? systemPrompt : undefined, messages };
    }
    case "message_end": {
      let message = ev.message, changed = false;
      for (const h of list) { const r = await run(h, { ...ev, message }); if (r?.message) { message = r.message; changed = true; } }
      return changed ? { message } : null;
    }
    case "before_provider_request": {
      let payload2 = ev.payload, changed = false;
      for (const h of list) { const r = await run(h, { ...ev, payload: payload2 }); if (r !== undefined && r !== null) { payload2 = r; changed = true; } }
      return changed ? { payload: payload2 } : null;
    }
    case "session_before_switch": case "session_before_fork": case "session_before_compact": case "session_before_tree": {
      const out = {};
      for (const h of list) { const r = await run(h, ev); if (r?.cancel) return { cancel: true }; if (r && typeof r === "object") Object.assign(out, r); }
      return out;
    }
    case "user_bash": {
      for (const h of list) { const r = await run(h, ev); if (r?.result) return { result: r.result }; if (r?.operations) h.ext.missing.add("user_bash.operations"); }
      return null;
    }
    case "turn_end": case "agent_before_settle": {
      const entries = []; let cont = false;
      for (const h of list) { const r = await run(h, ev); if (r?.entries) entries.push(...r.entries); if (r?.continue) cont = true; }
      return { entries, continue: cont };
    }
    case "resources_discover": {
      const out = { skillPaths: [], promptPaths: [], themePaths: [] };
      for (const h of list) { const r = await run(h, ev); for (const k of Object.keys(out)) if (Array.isArray(r?.[k])) out[k].push(...r[k]); }
      return out;
    }
    case "cache_warming_decision": {
      let last = null;
      for (const h of list) { const r = await run(h, ev); if (r) last = r; }
      return last;
    }
    default:
      for (const h of list) await run(h, ev);
      return null;
  }
}

// ---------------------------------------------------------------------------------------------- tools / render
async function executeTool({ name, toolCallId, params }) {
  const t = tools.get(name);
  if (!t) throw new Error(`Tool ${name} not found`);
  const ac = new AbortController();
  aborts.set(toolCallId, ac);
  try {
    let args = params;
    if (typeof t.def.prepareArguments === "function") args = t.def.prepareArguments(args);
    const onUpdate = (partial) => notify("tool_update", { toolCallId, partial: plain(partial) });
    const ctx = makeContext({ signal: ac.signal });
    const r = await t.def.execute(toolCallId, args, ac.signal, onUpdate, ctx);
    if (r === undefined || r === null) return { content: [{ type: "text", text: "" }] };
    if (typeof r === "string") return { content: [{ type: "text", text: r }] };
    return { content: r.content ?? [{ type: "text", text: JSON.stringify(r) }], details: plain(r.details), isError: !!r.isError };
  } finally {
    aborts.delete(toolCallId);
  }
}

function render(p) {
  const width = p.width || state.width;
  const opts = { expanded: !!p.expanded, isPartial: !!p.isPartial };
  if (p.kind === "toolCall" || p.kind === "toolResult") {
    const t = tools.get(p.name);
    let r = null;
    for (const res of toolRenderers) { try { r = res(p.name) || r; } catch {} }
    const def = t?.def || {};
    const ctx = { state: {}, args: p.args, toolCallId: p.toolCallId, cwd: state.cwd, expanded: opts.expanded };
    if (p.kind === "toolCall") {
      const fn = def.renderCall || r?.renderCall;
      return fn ? renderComponent(fn(p.args, theme, ctx), width) : null;
    }
    const fn = def.renderResult || r?.renderResult;
    return fn ? renderComponent(fn(p.result, opts, theme, ctx), width) : null;
  }
  if (p.kind === "message") {
    const fn = messageRenderers.get(p.customType);
    return fn ? renderComponent(fn(p.message, opts, theme), width) : null;
  }
  if (p.kind === "entry") {
    const fn = entryRenderers.get(p.customType);
    return fn ? renderComponent(fn(p.entry, opts, theme), width) : null;
  }
  return null;
}

async function providerStream({ name, model, context, options }, id) {
  const cfg = providers.get(name);
  const fn = cfg?.streamSimple || cfg?.stream;
  if (!fn) throw new Error(`provider ${name} has no stream function`);
  const s = await fn(model, context, options || {});
  let final = null;
  if (s && typeof s[Symbol.asyncIterator] === "function") {
    for await (const ev of s) {
      notify("provider_event", { streamId: id, event: plain(ev) });
      if (ev?.type === "done") final = ev.message;
      if (ev?.type === "error") final = ev.error || ev.message;
    }
  }
  if (!final && typeof s?.result === "function") final = await s.result();
  return plain(final);
}

// ---------------------------------------------------------------------------------------------- dispatch
const methods = {
  async init(p) {
    Object.assign(state, p.state || {});
    palette = p.palette || {};
    tuiModule = await import("./shims/tui.mjs");
    return { node: process.version };
  },
  async load({ paths }) {
    for (const p of paths) await loadOne(p);
    loaded = true;
    return { extensions: extReport(), tools: toolList(), commands: commandList(), events: [...handlers.keys()],
      shortcuts: [...shortcuts.entries()].map(([k, s]) => ({ key: k, description: s.opts?.description, source: s.ext.path })),
      flags: [...flags.entries()].map(([k, f]) => ({ name: k, description: f.opts?.description, type: f.opts?.type, default: f.opts?.default })) };
  },
  async emit({ event, payload }) { return emit(event, payload); },
  async tool_execute(p) { return executeTool(p); },
  async tool_abort({ toolCallId }) { aborts.get(toolCallId)?.abort(); return true; },
  async command({ name, args, id }) {
    const c = findCommand(id, name);
    if (!c) throw new Error(`Unknown command /${name}`);
    await c.opts.handler(args ?? "", makeCommandContext());
    return true;
  },
  async command_complete({ name, prefix, id }) {
    const c = findCommand(id, name);
    return c?.opts?.getArgumentCompletions ? (await c.opts.getArgumentCompletions(prefix)) ?? null : null;
  },
  async shortcut({ key }) {
    const s = shortcuts.get(String(key).toLowerCase());
    if (!s) return false;
    await s.opts.handler(makeContext());
    return true;
  },
  async render(p) { return render(p); },
  async custom_input({ id, data }) {
    const c = customs.get(id);
    if (!c) return false;
    try { c.component?.handleInput?.(data); } catch (e) { toErr(e); }
    renderCustom(id);
    return true;
  },
  async custom_cancel({ id }) { customs.get(id)?.done(undefined); return true; },
  async provider_stream(p, id) { return providerStream(p, id); },
  async provider_oauth_login({ name, loginId }) {
    const oa = providers.get(name)?.oauth;
    if (typeof oa?.login !== "function") throw new Error(`provider ${name} has no OAuth login`);
    const ac = new AbortController();
    const ask = async (message, placeholder) => {
      try {
        return await rpc("oauth.prompt", { loginId, message: String(message ?? ""), placeholder: String(placeholder ?? "") });
      } catch {
        ac.abort();
        throw new Error("Login cancelled");
      }
    };
    const callbacks = {
      onAuth: (info) => notify("oauth.auth", { loginId, url: typeof info === "string" ? info : info?.url, instructions: info?.instructions }),
      onPrompt: (p) => ask(p?.message, p?.placeholder),
      onManualCodeInput: () => ask("Paste the authorization code or the redirect URL", ""),
      onProgress: (m) => notify("oauth.progress", { loginId, message: String(m?.message ?? m ?? "") }),
      onDeviceCode: (d) => notify("oauth.auth", { loginId, url: d?.verificationUri || d?.verification_uri, instructions: d?.userCode ? `Enter code: ${d.userCode}` : undefined }),
      signal: ac.signal,
    };
    return plain(await oa.login(callbacks));
  },
  async provider_oauth_refresh({ name, credentials }) {
    const oa = providers.get(name)?.oauth;
    const fn = oa?.refreshToken || oa?.refresh;
    if (typeof fn !== "function") throw new Error(`provider ${name} cannot refresh its OAuth token`);
    return plain(await fn.call(oa, credentials));
  },
  async provider_oauth_api_key({ name, credentials }) {
    const oa = providers.get(name)?.oauth;
    if (typeof oa?.getApiKey !== "function") return credentials?.access ?? null;
    const key = await oa.getApiKey(credentials);
    return typeof key === "string" ? key : key == null ? null : JSON.stringify(key);
  },
  async report() { return { extensions: extReport() }; },
  async terminal_input({ data }) {
    let d = data, consume = false;
    for (const fn of [...terminalListeners]) {
      try {
        const r = fn(d);
        if (r?.consume) { consume = true; break; }
        if (typeof r?.data === "string") d = r.data;
      } catch (e) { toErr(e); }
    }
    return { consume, data: d };
  },
  async autocomplete({ lines, line, col, options }) {
    if (!autocompleteTop) return null;
    return plain(await autocompleteTop.getSuggestions(lines, line, col, options || {})) ?? null;
  },
  async autocomplete_apply({ lines, line, col, item, prefix }) {
    const top = autocompleteTop || baseAutocomplete;
    return plain(await top.applyCompletion(lines, line, col, item, prefix));
  },
  async editor_input({ data }) {
    if (!editorComp) return { active: false };
    try { editorComp.handleInput?.(data); } catch (e) { toErr(e); }
    state.editorText = editorComp.getText?.() ?? "";
    return editorState();
  },
  async editor_set_text({ text }) {
    state.editorText = text;
    editorComp?.setText?.(text);
    return editorState();
  },
  async editor_render() { return editorState(); },
  async markdown_transform({ markdown, ...ctx }) {
    let md = markdown;
    for (const { fn, types } of markdownTransformers.values()) {
      const type = ctx.messageType;
      if (types ? !types.has(type) : !["user", "assistant", "assistant-thinking"].includes(type)) continue;
      try { const r = fn(md, ctx); if (typeof r === "string") md = r; } catch (e) { toErr(e); }
    }
    return md;
  },
  async vm_route({ provider, id, request }) {
    const def = virtualModels.get(`${provider}/${id}`);
    if (!def) throw new Error(`virtual model ${provider}/${id} is not registered`);
    const ac = new AbortController();
    const ctx = makeContext({ signal: ac.signal });
    const r = await def.route({ ...request, signal: ac.signal }, ctx);
    if (!r?.model) throw new Error(`virtual model ${provider}/${id} returned no model`);
    return { model: { provider: r.model.provider, id: r.model.id }, thinkingLevel: r.thinkingLevel, state: r.state === undefined ? undefined : plain(r.state),
      hasState: r.state !== undefined && r.state !== request.state };
  },
  async shutdown() { setTimeout(() => process.exit(0), 50); return true; },
};
const notifications = {
  state({ patch }) {
    const prevWidth = state.width;
    Object.assign(state, patch || {});
    if (patch?.width && patch.width !== prevWidth) globalThis.__omniPi.requestRender();
  },
  "session.reset"({ entries, header, leafId }) { state.entries = entries || []; state.header = header || {}; state.leafId = leafId ?? null; },
  "session.append"({ entry }) { state.entries.push(entry); state.leafId = entry.id; },
  "session.leaf"({ leafId }) { state.leafId = leafId; },
};

const rl = readline.createInterface({ input: process.stdin });
rl.on("line", async (line) => {
  if (!line.trim()) return;
  let msg;
  try { msg = JSON.parse(line); } catch { return; }
  if (msg.id !== undefined && !msg.method) {
    const p = pending.get(msg.id);
    if (p) { pending.delete(msg.id); msg.error ? p.reject(new Error(msg.error)) : p.resolve(msg.result); }
    return;
  }
  if (msg.id === undefined) { try { notifications[msg.method]?.(msg.params || {}); } catch (e) { toErr(e); } return; }
  const fn = methods[msg.method];
  if (!fn) { send({ id: msg.id, error: `unknown method ${msg.method}` }); return; }
  try {
    const result = await fn(msg.params || {}, msg.id);
    send({ id: msg.id, result: result === undefined ? null : result });
  } catch (e) {
    send({ id: msg.id, error: String(e?.message || e) });
  }
});
rl.on("close", () => process.exit(0));
process.on("uncaughtException", (e) => { toErr("[uncaught]", e); notify("extension_error", { path: "host", event: "uncaught", error: String(e?.stack || e) }); });
process.on("unhandledRejection", (e) => { toErr("[unhandled]", e); });
