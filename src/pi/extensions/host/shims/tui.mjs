// pi-tui compatible components. Components render to arrays of ANSI strings; Omni's Python TUI paints them.
import { namespace } from "./_stub.mjs";

const ANSI_RE = /\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b_[^\x1b]*\x1b\\/g;
export const CURSOR_MARKER = "\x1b_pi:c\x07";

function charWidth(cp) {
  if (cp === 0 || cp < 32 || (cp >= 0x7f && cp < 0xa0)) return 0;
  if (cp >= 0x300 && cp <= 0x36f) return 0;
  if (cp === 0x200d || (cp >= 0xfe00 && cp <= 0xfe0f)) return 0;
  if ((cp >= 0x1100 && cp <= 0x115f) || (cp >= 0x2e80 && cp <= 0xa4cf) || (cp >= 0xac00 && cp <= 0xd7a3) ||
      (cp >= 0xf900 && cp <= 0xfaff) || (cp >= 0xfe30 && cp <= 0xfe4f) || (cp >= 0xff00 && cp <= 0xff60) ||
      (cp >= 0xffe0 && cp <= 0xffe6) || (cp >= 0x1f300 && cp <= 0x1faff) || (cp >= 0x20000 && cp <= 0x3fffd)) return 2;
  return 1;
}

export function stripAnsi(s) {
  return String(s ?? "").replace(ANSI_RE, "");
}

export function visibleWidth(s) {
  let w = 0;
  for (const ch of stripAnsi(s)) w += charWidth(ch.codePointAt(0));
  return w;
}

export function truncateToWidth(text, maxWidth, ellipsis = "…", pad = false) {
  text = String(text ?? "");
  if (visibleWidth(text) <= maxWidth) return pad ? text + " ".repeat(Math.max(0, maxWidth - visibleWidth(text))) : text;
  const target = Math.max(0, maxWidth - visibleWidth(ellipsis));
  let out = "", w = 0, i = 0;
  while (i < text.length) {
    ANSI_RE.lastIndex = i;
    const m = ANSI_RE.exec(text);
    if (m && m.index === i) { out += m[0]; i += m[0].length; continue; }
    const cp = text.codePointAt(i);
    const ch = String.fromCodePoint(cp);
    const cw = charWidth(cp);
    if (w + cw > target) break;
    out += ch; w += cw; i += ch.length;
  }
  out += "\x1b[0m" + ellipsis;
  return pad ? out + " ".repeat(Math.max(0, maxWidth - visibleWidth(out))) : out;
}

export function wrapTextWithAnsi(text, width) {
  const out = [];
  for (const para of String(text ?? "").split("\n")) {
    if (visibleWidth(para) <= width) { out.push(para); continue; }
    const words = para.split(/(\s+)/);
    let line = "";
    for (const w of words) {
      if (visibleWidth(line + w) > width && line.trim()) { out.push(line.replace(/\s+$/, "")); line = w.trimStart(); }
      else line += w;
      while (visibleWidth(line) > width) { out.push(truncateToWidth(line, width, "")); line = sliceFrom(line, width); }
    }
    out.push(line);
  }
  return out;
}

function sliceFrom(text, cols) {
  let w = 0, i = 0;
  while (i < text.length && w < cols) {
    ANSI_RE.lastIndex = i;
    const m = ANSI_RE.exec(text);
    if (m && m.index === i) { i += m[0].length; continue; }
    const cp = text.codePointAt(i);
    w += charWidth(cp); i += String.fromCodePoint(cp).length;
  }
  return text.slice(i);
}

// ---- keys -------------------------------------------------------------------------------------------------
const MODS = /^(ctrl|shift|alt|meta|super)+$/i;
export const Key = new Proxy({}, {
  get: (_t, p) => {
    if (typeof p !== "string") return undefined;
    const name = p.replace(/([a-z])([A-Z])/g, "$1+$2").toLowerCase();
    if (name.split("+").every((part) => MODS.test(part))) {
      const fn = (k) => `${name}+${String(k).toLowerCase()}`;
      fn.toString = () => name;
      return fn;
    }
    return name;
  },
});
const SEQ = {
  enter: ["\r", "\n"], return: ["\r"], escape: ["\x1b"], esc: ["\x1b"], tab: ["\t"], backspace: ["\x7f", "\b"],
  up: ["\x1b[A", "\x1bOA"], down: ["\x1b[B", "\x1bOB"], right: ["\x1b[C", "\x1bOC"], left: ["\x1b[D", "\x1bOD"],
  home: ["\x1b[H", "\x1b[1~", "\x1bOH"], end: ["\x1b[F", "\x1b[4~", "\x1bOF"], pageup: ["\x1b[5~"], pagedown: ["\x1b[6~"],
  delete: ["\x1b[3~"], space: [" "], "shift+tab": ["\x1b[Z"], "shift+enter": ["\x1b[13;2u", "\x1b\r"],
};
export function parseKey(data) {
  for (const [k, seqs] of Object.entries(SEQ)) if (seqs.includes(data)) return k;
  if (data.length === 1) {
    const c = data.charCodeAt(0);
    if (c >= 1 && c <= 26) return `ctrl+${String.fromCharCode(c + 96)}`;
    return data;
  }
  if (data.length === 2 && data[0] === "\x1b") return `alt+${data[1]}`;
  return data;
}
export function matchesKey(data, key) {
  const k = String(key).toLowerCase();
  if (SEQ[k]) return SEQ[k].includes(data);
  return parseKey(data) === k;
}
export function isKeyRelease() { return false; }
export function isKeyRepeat() { return false; }

// ---- components --------------------------------------------------------------------------------------------
export class Container {
  constructor() { this.children = []; }
  addChild(c) { this.children.push(c); return c; }
  removeChild(c) { this.children = this.children.filter((x) => x !== c); }
  clear() { this.children = []; }
  invalidate() { for (const c of this.children) c.invalidate?.(); }
  render(width) { return this.children.flatMap((c) => (c && c.render ? c.render(width) : [])); }
}

export class Text {
  constructor(text = "", paddingX = 1, paddingY = 0, bg) { this.text = text; this.paddingX = paddingX; this.paddingY = paddingY; this.bg = bg; }
  setText(t) { this.text = t; }
  setCustomBgFn(fn) { this.bg = fn; }
  invalidate() {}
  render(width) {
    const inner = Math.max(1, width - this.paddingX * 2);
    const pad = " ".repeat(this.paddingX);
    let lines = String(this.text ?? "") === "" ? [] : wrapTextWithAnsi(this.text, inner).map((l) => pad + l);
    const blank = Array(this.paddingY).fill("");
    lines = [...blank, ...lines, ...blank];
    return this.bg ? lines.map((l) => this.bg(l + " ".repeat(Math.max(0, width - visibleWidth(l))))) : lines;
  }
}
export class TruncatedText extends Text {
  render(width) { return String(this.text ?? "").split("\n").map((l) => truncateToWidth(" ".repeat(this.paddingX) + l, width)); }
}
export class Spacer {
  constructor(lines = 1) { this.lines = lines; }
  setLines(n) { this.lines = n; }
  invalidate() {}
  render() { return Array(this.lines).fill(""); }
}
export class Box extends Container {
  constructor(paddingX = 1, paddingY = 1, bg) { super(); this.paddingX = paddingX; this.paddingY = paddingY; this.bg = bg; }
  setBgFn(fn) { this.bg = fn; }
  render(width) {
    const inner = Math.max(1, width - this.paddingX * 2);
    const pad = " ".repeat(this.paddingX);
    const body = super.render(inner).map((l) => pad + l);
    const blank = Array(this.paddingY).fill("");
    const lines = [...blank, ...body, ...blank];
    return this.bg ? lines.map((l) => this.bg(l + " ".repeat(Math.max(0, width - visibleWidth(l))))) : lines;
  }
}
export class Markdown extends Text {
  constructor(text = "", paddingX = 1, paddingY = 0, _theme, _style) { super(text, paddingX, paddingY); }
}
export class Image {
  constructor(_data, _mime, _theme, opts = {}) { this.alt = opts?.filename || "image"; }
  invalidate() {}
  render() { return [`[image: ${this.alt}]`]; }
}
export class Loader extends Text {
  constructor(_tui, spinColor = (s) => s, msgColor = (s) => s, message = "Loading...") {
    super(message, 1, 0); this.spin = spinColor; this.msg = msgColor; this.frames = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"; this.i = 0;
  }
  setMessage(m) { this.text = m; }
  start() {} stop() {} dispose() {}
  render(width) { this.i = (this.i + 1) % this.frames.length; return [truncateToWidth(` ${this.spin(this.frames[this.i])} ${this.msg(this.text)}`, width)]; }
}
export class CancellableLoader extends Loader {
  constructor(...a) { super(...a); this.onAbort = undefined; this.signal = new AbortController().signal; }
  handleInput(data) { if (matchesKey(data, "escape")) this.onAbort?.(); }
}
export class Input {
  constructor() { this.value = ""; this.cursor = 0; this.focused = true; this.onSubmit = undefined; this.onEscape = undefined; }
  getValue() { return this.value; }
  setValue(v) { this.value = v; this.cursor = v.length; }
  invalidate() {}
  handleInput(data) {
    if (matchesKey(data, "enter")) return this.onSubmit?.(this.value);
    if (matchesKey(data, "escape")) return this.onEscape?.();
    if (matchesKey(data, "backspace")) { if (this.cursor > 0) { this.value = this.value.slice(0, this.cursor - 1) + this.value.slice(this.cursor); this.cursor--; } return; }
    if (matchesKey(data, "left")) { this.cursor = Math.max(0, this.cursor - 1); return; }
    if (matchesKey(data, "right")) { this.cursor = Math.min(this.value.length, this.cursor + 1); return; }
    if (data.length >= 1 && !data.startsWith("\x1b") && data >= " ") { this.value = this.value.slice(0, this.cursor) + data + this.value.slice(this.cursor); this.cursor += data.length; }
  }
  render(width) { return [truncateToWidth("> " + this.value.slice(0, this.cursor) + "\x1b[7m" + (this.value[this.cursor] || " ") + "\x1b[27m" + this.value.slice(this.cursor + 1), width)]; }
}
export class SelectList {
  constructor(items = [], maxVisible = 10, theme = {}) {
    this.items = items; this.filtered = items; this.maxVisible = maxVisible; this.theme = theme; this.selected = 0;
    this.onSelect = undefined; this.onCancel = undefined; this.onSelectionChange = undefined;
  }
  setFilter(f) { const q = String(f || "").toLowerCase(); this.filtered = this.items.filter((i) => `${i.label ?? i.value} ${i.description ?? ""}`.toLowerCase().includes(q)); this.selected = 0; }
  setSelectedIndex(i) { this.selected = Math.max(0, Math.min(i, this.filtered.length - 1)); }
  getSelectedItem() { return this.filtered[this.selected]; }
  invalidate() {}
  handleInput(data) {
    if (matchesKey(data, "up") || data === "k") this.selected = (this.selected - 1 + this.filtered.length) % Math.max(1, this.filtered.length);
    else if (matchesKey(data, "down") || data === "j") this.selected = (this.selected + 1) % Math.max(1, this.filtered.length);
    else if (matchesKey(data, "enter")) { const it = this.filtered[this.selected]; if (it) this.onSelect?.(it); return; }
    else if (matchesKey(data, "escape")) { this.onCancel?.(); return; }
    else return;
    this.onSelectionChange?.(this.filtered[this.selected]);
  }
  render(width) {
    const start = Math.max(0, Math.min(this.selected - Math.floor(this.maxVisible / 2), this.filtered.length - this.maxVisible));
    return this.filtered.slice(start, start + this.maxVisible).map((it, i) => {
      const sel = start + i === this.selected;
      const label = `${sel ? "› " : "  "}${it.label ?? it.value}${it.description ? "  \x1b[2m" + it.description + "\x1b[22m" : ""}`;
      const line = truncateToWidth(label, width);
      return sel && this.theme.selectedText ? this.theme.selectedText(line) : sel ? `\x1b[1m${line}\x1b[22m` : line;
    });
  }
}
export class SettingsList extends SelectList {}
export class Editor {
  // Multi-line editor with the usual emacs-style keys. CustomEditor (coding-agent shim) adds the app keybindings.
  constructor(tui, theme, keybindings, options = {}) {
    this.tui = tui; this.theme = theme; this.keybindings = keybindings; this.options = options || {};
    this.lines = [""]; this.row = 0; this.col = 0; this.focused = true; this.disableSubmit = false;
    this.onSubmit = undefined; this.onChange = undefined; this.borderColor = undefined; this.history = []; this.historyIndex = -1;
  }
  getText() { return this.lines.join("\n"); }
  getExpandedText() { return this.getText(); }
  setText(t) { this.lines = String(t ?? "").split("\n"); this.row = this.lines.length - 1; this.col = this.lines[this.row].length; this._changed(); }
  getValue() { return this.getText(); }
  setValue(v) { this.setText(v); }
  getLines() { return [...this.lines]; }
  getCursor() { return { line: this.row, col: this.col }; }
  insertTextAtCursor(t) { this._insert(String(t)); }
  addToHistory(t) { if (t && this.history[0] !== t) this.history.unshift(t); this.historyIndex = -1; }
  isShowingAutocomplete() { return false; }
  invalidate() {}
  _changed() { try { this.onChange?.(this.getText()); } catch {} }
  _insert(text) {
    const parts = text.replace(/\r\n?/g, "\n").split("\n");
    const line = this.lines[this.row];
    const before = line.slice(0, this.col), after = line.slice(this.col);
    if (parts.length === 1) { this.lines[this.row] = before + parts[0] + after; this.col += parts[0].length; }
    else {
      const mid = parts.slice(1, -1);
      const last = parts[parts.length - 1];
      this.lines.splice(this.row, 1, before + parts[0], ...mid, last + after);
      this.row += parts.length - 1; this.col = last.length;
    }
    this._changed();
  }
  handleInput(data) {
    const k = (name) => matchesKey(data, name);
    if (data.startsWith("\x1b[200~")) { this._insert(data.replace(/^\x1b\[200~/, "").replace(/\x1b\[201~$/, "")); return; }
    if (k("enter")) { if (!this.disableSubmit) { const t = this.getText(); this.onSubmit?.(t); } return; }
    if (k("shift+enter") || data === "\x1b\r" || data === "\n") { this._insert("\n"); return; }
    if (k("backspace")) {
      if (this.col > 0) { const l = this.lines[this.row]; this.lines[this.row] = l.slice(0, this.col - 1) + l.slice(this.col); this.col--; }
      else if (this.row > 0) { const prev = this.lines[this.row - 1]; this.col = prev.length; this.lines.splice(this.row - 1, 2, prev + this.lines[this.row]); this.row--; }
      this._changed(); return;
    }
    if (k("delete") || (data === "\x04" && this.getText())) {
      const l = this.lines[this.row];
      if (this.col < l.length) this.lines[this.row] = l.slice(0, this.col) + l.slice(this.col + 1);
      else if (this.row < this.lines.length - 1) this.lines.splice(this.row, 2, l + this.lines[this.row + 1]);
      this._changed(); return;
    }
    if (k("left")) { if (this.col > 0) this.col--; else if (this.row > 0) { this.row--; this.col = this.lines[this.row].length; } return; }
    if (k("right")) { if (this.col < this.lines[this.row].length) this.col++; else if (this.row < this.lines.length - 1) { this.row++; this.col = 0; } return; }
    if (k("up")) {
      if (this.row > 0) { this.row--; this.col = Math.min(this.col, this.lines[this.row].length); }
      else if (this.history.length && this.historyIndex < this.history.length - 1) { this.historyIndex++; this.setText(this.history[this.historyIndex]); }
      return;
    }
    if (k("down")) {
      if (this.row < this.lines.length - 1) { this.row++; this.col = Math.min(this.col, this.lines[this.row].length); }
      else if (this.historyIndex >= 0) { this.historyIndex--; this.setText(this.historyIndex >= 0 ? this.history[this.historyIndex] : ""); }
      return;
    }
    if (k("home") || data === "\x01") { this.col = 0; return; }
    if (k("end") || data === "\x05") { this.col = this.lines[this.row].length; return; }
    if (data === "\x0b") { this.lines[this.row] = this.lines[this.row].slice(0, this.col); this._changed(); return; }  // ctrl+k
    if (data === "\x15") { this.lines[this.row] = this.lines[this.row].slice(this.col); this.col = 0; this._changed(); return; }  // ctrl+u
    if (data === "\x17") {  // ctrl+w
      const l = this.lines[this.row]; const m = /\S*\s*$/.exec(l.slice(0, this.col)); const n = m ? m[0].length : 0;
      this.lines[this.row] = l.slice(0, this.col - n) + l.slice(this.col); this.col -= n; this._changed(); return;
    }
    if (data === "\x1bb") { const m = /\S+\s*$/.exec(this.lines[this.row].slice(0, this.col)); this.col -= m ? m[0].length : 0; return; }
    if (data === "\x1bf") { const m = /^\s*\S+/.exec(this.lines[this.row].slice(this.col)); this.col += m ? m[0].length : 0; return; }
    if (data.length >= 1 && !data.startsWith("\x1b") && !/[\x00-\x08\x0e-\x1f\x7f]/.test(data)) this._insert(data.replace(/\t/g, "  "));
  }
  render(width) {
    const color = (s) => (typeof this.borderColor === "function" ? this.borderColor(s) : s);
    const inner = Math.max(4, width - 2);
    const out = [color("─".repeat(Math.max(1, width)))];
    this.lines.forEach((line, r) => {
      let shown = line;
      if (r === this.row && this.focused) shown = line.slice(0, this.col) + CURSOR_MARKER + "\x1b[7m" + (line[this.col] || " ") + "\x1b[27m" + line.slice(this.col + 1);
      for (const seg of wrapTextWithAnsi(shown, inner)) out.push(" " + seg);
    });
    out.push(color("─".repeat(Math.max(1, width))));
    return out;
  }
}
export class TUI {
  constructor() { this.children = []; }
  requestRender() { globalThis.__omniPi?.requestRender?.(); }
  addChild(c) { this.children.push(c); }
  removeChild(c) { this.children = this.children.filter((x) => x !== c); }
  get terminal() { return { columns: globalThis.__omniPi?.state?.width || 100, rows: globalThis.__omniPi?.state?.height || 30 }; }
}
export function getEditorKeybindings() { return { matches: (data, action) => matchesKey(data, action) }; }
export function getKeybindings() { return getEditorKeybindings(); }
export const fuzzyFilter = (items, q, key = (x) => x) => items.filter((i) => String(key(i)).toLowerCase().includes(String(q).toLowerCase()));
export const fuzzyMatch = (q, s) => String(s).toLowerCase().includes(String(q).toLowerCase());

const real = {
  CURSOR_MARKER, stripAnsi, visibleWidth, truncateToWidth, wrapTextWithAnsi, Key, parseKey, matchesKey, isKeyRelease, isKeyRepeat,
  Container, Text, TruncatedText, Spacer, Box, Markdown, Image, Loader, CancellableLoader, Input, SelectList, SettingsList, Editor,
  TUI, getEditorKeybindings, getKeybindings, fuzzyFilter, fuzzyMatch,
};
export default namespace(real, "pi-tui");
