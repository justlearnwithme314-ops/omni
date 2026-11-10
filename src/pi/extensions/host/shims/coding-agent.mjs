// @earendil-works/pi-coding-agent runtime exports used by extensions.
import { execFile } from "node:child_process";
import os from "node:os";
import path from "node:path";
import { namespace } from "./_stub.mjs";
import * as tui from "./tui.mjs";
import { Type } from "./typebox.mjs";

const host = () => globalThis.__omniPi;

export const VERSION = "0.78.1";
export const CONFIG_DIR_NAME = ".pi";
export const APP_NAME = "pi";
export const DEFAULT_MAX_LINES = 2000;
export const DEFAULT_MAX_BYTES = 50 * 1024;

export function formatSize(b) {
  return b < 1024 ? `${b}B` : b < 1048576 ? `${(b / 1024).toFixed(1)}KB` : `${(b / 1048576).toFixed(1)}MB`;
}
function lines(s) { const l = String(s ?? "").split("\n"); if (String(s).endsWith("\n")) l.pop(); return s ? l : []; }
export function truncateHead(content, opts = {}) {
  const maxLines = opts.maxLines ?? DEFAULT_MAX_LINES, maxBytes = opts.maxBytes ?? DEFAULT_MAX_BYTES;
  const all = lines(content); const totalBytes = Buffer.byteLength(String(content ?? ""));
  const base = { truncated: false, truncatedBy: null, totalLines: all.length, totalBytes, lastLinePartial: false, firstLineExceedsLimit: false, maxLines, maxBytes };
  if (all.length <= maxLines && totalBytes <= maxBytes) return { ...base, content, outputLines: all.length, outputBytes: totalBytes };
  const out = []; let used = 0, by = "lines";
  for (const [i, l] of all.entries()) {
    if (i >= maxLines) break;
    const b = Buffer.byteLength(l) + (i ? 1 : 0);
    if (used + b > maxBytes) { by = "bytes"; break; }
    out.push(l); used += b;
  }
  const text = out.join("\n");
  return { ...base, content: text, truncated: true, truncatedBy: by, outputLines: out.length, outputBytes: Buffer.byteLength(text), firstLineExceedsLimit: out.length === 0 && all.length > 0 };
}
export function truncateTail(content, opts = {}) {
  const maxLines = opts.maxLines ?? DEFAULT_MAX_LINES, maxBytes = opts.maxBytes ?? DEFAULT_MAX_BYTES;
  const all = lines(content); const totalBytes = Buffer.byteLength(String(content ?? ""));
  const base = { truncated: false, truncatedBy: null, totalLines: all.length, totalBytes, lastLinePartial: false, firstLineExceedsLimit: false, maxLines, maxBytes };
  if (all.length <= maxLines && totalBytes <= maxBytes) return { ...base, content, outputLines: all.length, outputBytes: totalBytes };
  const out = []; let used = 0, by = "lines";
  for (let i = all.length - 1; i >= 0 && out.length < maxLines; i--) {
    const b = Buffer.byteLength(all[i]) + (out.length ? 1 : 0);
    if (used + b > maxBytes) { by = "bytes"; break; }
    out.unshift(all[i]); used += b;
  }
  const text = out.join("\n");
  return { ...base, content: text, truncated: true, truncatedBy: by, outputLines: out.length, outputBytes: Buffer.byteLength(text) };
}
export function truncateLine(line, max = 500) {
  return line.length <= max ? { text: line, wasTruncated: false } : { text: line.slice(0, max) + "... [truncated]", wasTruncated: true };
}

// ---- built-in tool factories: definitions whose execute() runs Omni's built-in tool -------------------------
const TOOL_META = {
  read: { label: "read", params: { path: Type.String(), offset: Type.Optional(Type.Number()), limit: Type.Optional(Type.Number()) } },
  bash: { label: "bash", params: { command: Type.String(), timeout: Type.Optional(Type.Number()) } },
  edit: { label: "edit", params: { path: Type.String(), edits: Type.Array(Type.Object({ oldText: Type.String(), newText: Type.String() })) } },
  write: { label: "write", params: { path: Type.String(), content: Type.String() } },
  grep: { label: "grep", params: { pattern: Type.String(), path: Type.Optional(Type.String()), glob: Type.Optional(Type.String()) } },
  find: { label: "find", params: { pattern: Type.String(), path: Type.Optional(Type.String()) } },
  ls: { label: "ls", params: { path: Type.Optional(Type.String()), limit: Type.Optional(Type.Number()) } },
};
function builtinDefinition(name, cwd, options = {}) {
  const fromState = (host()?.state?.allTools || []).find((t) => t.name === name && t.source === "builtin");
  return {
    name,
    label: TOOL_META[name]?.label || name,
    description: fromState?.description || `Built-in ${name} tool`,
    parameters: fromState?.parameters || Type.Object(TOOL_META[name]?.params || {}),
    promptSnippet: fromState?.promptSnippet,
    promptGuidelines: fromState?.promptGuidelines,
    async execute(toolCallId, params, signal, onUpdate, ctx) {
      if (options.spawnHook && name === "bash") {
        const r = options.spawnHook({ command: params.command, cwd: ctx?.cwd || cwd, env: { ...process.env } });
        if (r?.command) params = { ...params, command: r.command };
      }
      return host().rpc("builtin.execute", { name, toolCallId, params, cwd: ctx?.cwd || cwd });
    },
  };
}
export const createReadToolDefinition = (cwd, o) => builtinDefinition("read", cwd, o);
export const createBashToolDefinition = (cwd, o) => builtinDefinition("bash", cwd, o);
export const createEditToolDefinition = (cwd, o) => builtinDefinition("edit", cwd, o);
export const createWriteToolDefinition = (cwd, o) => builtinDefinition("write", cwd, o);
export const createGrepToolDefinition = (cwd, o) => builtinDefinition("grep", cwd, o);
export const createFindToolDefinition = (cwd, o) => builtinDefinition("find", cwd, o);
export const createLsToolDefinition = (cwd, o) => builtinDefinition("ls", cwd, o);
export const createReadTool = createReadToolDefinition;
export const createBashTool = createBashToolDefinition;
export const createEditTool = createEditToolDefinition;
export const createWriteTool = createWriteToolDefinition;
export const createGrepTool = createGrepToolDefinition;
export const createFindTool = createFindToolDefinition;
export const createLsTool = createLsToolDefinition;
export const createCodingToolDefinitions = (cwd, o = {}) => ["read", "bash", "edit", "write"].map((n) => builtinDefinition(n, cwd, o[n]));
export const createReadOnlyToolDefinitions = (cwd, o = {}) => ["read", "grep", "find", "ls"].map((n) => builtinDefinition(n, cwd, o[n]));
export const createCodingTools = createCodingToolDefinitions;
export const createReadOnlyTools = createReadOnlyToolDefinitions;
export const createAllToolDefinitions = (cwd, o = {}) => Object.fromEntries(Object.keys(TOOL_META).map((n) => [n, builtinDefinition(n, cwd, o[n])]));
export const createAllTools = createAllToolDefinitions;
export const createLocalBashOperations = () => ({
  exec: (command, cwd, { onData, signal, timeout } = {}) => new Promise((resolve) => {
    const child = execFile(process.platform === "win32" ? "cmd.exe" : "/bin/bash", process.platform === "win32" ? ["/d", "/s", "/c", command] : ["-c", command],
      { cwd, timeout: timeout ? timeout * 1000 : undefined, signal, maxBuffer: 64 * 1024 * 1024 }, (err) => resolve({ exitCode: err ? (err.code ?? 1) : 0 }));
    child.stdout?.on("data", (d) => onData?.(Buffer.from(d)));
    child.stderr?.on("data", (d) => onData?.(Buffer.from(d)));
  }),
});
export const withFileMutationQueue = async (_p, fn) => fn();

// ---- event type guards ------------------------------------------------------------------------------------
export const isToolCallEventType = (name, ev) => ev?.toolName === name;
export const isToolResultEventType = (name, ev) => ev?.toolName === name;
export const isBashToolResult = (ev) => ev?.toolName === "bash";
export const isReadToolResult = (ev) => ev?.toolName === "read";
export const isEditToolResult = (ev) => ev?.toolName === "edit";
export const isWriteToolResult = (ev) => ev?.toolName === "write";
export const isGrepToolResult = (ev) => ev?.toolName === "grep";
export const isFindToolResult = (ev) => ev?.toolName === "find";
export const isLsToolResult = (ev) => ev?.toolName === "ls";

// ---- paths / misc ------------------------------------------------------------------------------------------
export const getAgentDir = () => host()?.state?.agentDir || path.join(os.homedir(), ".pi", "agent");
export const getSettingsPath = () => path.join(getAgentDir(), "settings.json");
export const getSessionsDir = () => path.join(getAgentDir(), "sessions");
export const getDocsPath = () => path.join(getAgentDir(), "docs");
export const getReadmePath = () => path.join(getAgentDir(), "README.md");
export const getExamplesPath = () => path.join(getAgentDir(), "examples");
export function parseFrontmatter(text) {
  const m = /^---\r?\n([\s\S]*?)\r?\n---\r?\n?([\s\S]*)$/.exec(String(text ?? ""));
  if (!m) return { frontmatter: {}, body: text };
  const fm = {};
  for (const line of m[1].split(/\r?\n/)) { const i = line.indexOf(":"); if (i > 0 && !/^\s/.test(line)) fm[line.slice(0, i).trim()] = line.slice(i + 1).trim().replace(/^["']|["']$/g, ""); }
  return { frontmatter: fm, body: m[2] };
}
export const stripFrontmatter = (t) => parseFrontmatter(t).body;
export async function copyToClipboard(text) {
  return host().rpc("ui.copy", { text });
}
export const convertToLlm = (messages) => messages.filter((m) => ["user", "assistant", "toolResult"].includes(m.role) || m.role === "custom")
  .map((m) => (m.role === "custom" ? { role: "user", content: typeof m.content === "string" ? [{ type: "text", text: m.content }] : m.content, timestamp: m.timestamp } : m));
export const estimateTokens = (m) => Math.ceil(JSON.stringify(m ?? "").length / 4);

// ---- theme / rendering helpers ----------------------------------------------------------------------------
export function getMarkdownTheme() {
  const t = host()?.theme;
  const f = (c) => (s) => (t ? t.fg(c, s) : s);
  return { heading: f("mdHeading"), link: f("mdLink"), code: f("mdCode"), codeBlock: f("mdCodeBlock"), quote: f("mdQuote"), bold: (s) => `\x1b[1m${s}\x1b[22m`, italic: (s) => `\x1b[3m${s}\x1b[23m` };
}
export const getSelectListTheme = () => ({ selectedText: (s) => host()?.theme?.fg("accent", s) ?? s, description: (s) => host()?.theme?.fg("muted", s) ?? s });
export const keyHint = (key, label) => `${host()?.theme?.fg("dim", key) ?? key} ${label ?? ""}`.trim();
export const rawKeyHint = keyHint;
export const keyText = (k) => k;
export class DynamicBorder {
  constructor(color = (s) => s) { this.color = color; }
  invalidate() {}
  render(width) { return [this.color("─".repeat(Math.max(1, width)))]; }
}
export class BorderedLoader extends tui.CancellableLoader {
  constructor(t, theme, message) { super(t, (s) => theme?.fg?.("accent", s) ?? s, (s) => s, message); }
}
// App keys a custom editor forwards to Omni (escape aborts, ctrl+d quits, model / thinking cycling, ...).
const APP_KEYS = ["escape", "ctrl+c", "ctrl+d", "ctrl+l", "ctrl+p", "ctrl+o", "ctrl+t", "ctrl+g", "ctrl+x", "shift+tab", "alt+enter", "alt+up", "pageup", "pagedown", "tab"];
export class CustomEditor extends tui.Editor {
  constructor(t, theme, keybindings, options = {}) {
    super(t, theme, keybindings, options);
    this.actionHandlers = new Map();
    this.onEscape = undefined; this.onCtrlD = undefined;
  }
  onAction(action, handler) { this.actionHandlers.set(action, handler); }
  handleInput(data) {
    for (const key of APP_KEYS) {
      if (tui.matchesKey(data, key) || (key === "alt+enter" && data === "\x1b\r")) {
        if (key === "ctrl+d" && this.getText()) break;
        if (key === "tab" && !this.getText().trim()) break;
        const h = this.actionHandlers.get(key);
        if (h) { h(); return; }
        host()?.editorAction?.(key, data, this);
        return;
      }
    }
    super.handleInput(data);
  }
}
export class Theme {
  fg(c, s) { return host()?.theme?.fg(c, s) ?? s; }
  bg(c, s) { return host()?.theme?.bg(c, s) ?? s; }
  bold(s) { return `\x1b[1m${s}\x1b[22m`; }
  italic(s) { return `\x1b[3m${s}\x1b[23m`; }
}
export const defineTool = (t) => t;
export const defineExtension = (f) => f;

const real = {
  VERSION, CONFIG_DIR_NAME, APP_NAME, DEFAULT_MAX_LINES, DEFAULT_MAX_BYTES, formatSize, truncateHead, truncateTail, truncateLine,
  createReadToolDefinition, createBashToolDefinition, createEditToolDefinition, createWriteToolDefinition, createGrepToolDefinition,
  createFindToolDefinition, createLsToolDefinition, createReadTool, createBashTool, createEditTool, createWriteTool, createGrepTool,
  createFindTool, createLsTool, createCodingToolDefinitions, createReadOnlyToolDefinitions, createCodingTools, createReadOnlyTools,
  createAllToolDefinitions, createAllTools, createLocalBashOperations, withFileMutationQueue, isToolCallEventType, isToolResultEventType,
  isBashToolResult, isReadToolResult, isEditToolResult, isWriteToolResult, isGrepToolResult, isFindToolResult, isLsToolResult,
  getAgentDir, getSettingsPath, getSessionsDir, getDocsPath, getReadmePath, getExamplesPath, parseFrontmatter, stripFrontmatter,
  copyToClipboard, convertToLlm, estimateTokens, getMarkdownTheme, getSelectListTheme, keyHint, rawKeyHint, keyText, DynamicBorder,
  BorderedLoader, CustomEditor, Theme, defineTool, defineExtension,
  ...tui,
};
export default namespace(real, "pi-coding-agent");
