// Codemode script runner. One process per script; JSON lines on stdin/stdout with the Python side.
// The script runs as the body of an async function inside a fresh `vm` context that has only the codemode
// globals: no require/process/timers/fetch. Tool calls, searchTools, describeTool and describeNamespace go to Python.
import vm from "node:vm";
import readline from "node:readline";

const rl = readline.createInterface({ input: process.stdin });
const send = (m) => process.stdout.write(JSON.stringify(m) + "\n");
const waiting = new Map();
let nextId = 1;
let pendingCalls = 0;
let finished = false;
let init = null;

const LIMIT_CHARS = 16777216;
const LIMIT_ITEMS = 100000;
const items = [];
const consoleLines = [];
let outChars = 0;
let outCount = 0;
const storeSet = {};
const storeDel = new Set();
class ExitSignal { }

function bump(n) {
  outChars += n;
  outCount += 1;
  if (outChars > LIMIT_CHARS) throw new Error(`Output exceeds ${LIMIT_CHARS} characters; write large data to a file with a tool instead`);
  if (outCount > LIMIT_ITEMS) throw new Error(`Output exceeds ${LIMIT_ITEMS} text(), image(), and console calls`);
}
const fmt = (v) => typeof v === "string" ? v : (v === undefined ? "undefined" : safeJson(v));
function safeJson(v) {
  try { const s = JSON.stringify(v, null, 2); return s === undefined ? String(v) : s; } catch { return String(v); }
}

function request(type, payload) {
  const id = nextId++;
  pendingCalls++;
  return new Promise((resolve, reject) => {
    waiting.set(id, { resolve, reject });
    send({ type, id, ...payload });
  }).finally(() => { pendingCalls--; scheduleStallCheck(); });
}

function toImage(value) {
  if (typeof value === "string") {
    const m = /^data:(image\/(png|jpeg|jpg|gif|webp));base64,(.*)$/s.exec(value);
    if (!m) throw new Error("image() takes a base64 data: URL, { image_url }, or an image block; remote URLs are not supported");
    return { type: "image", mimeType: m[1] === "image/jpg" ? "image/jpeg" : m[1], data: m[3] };
  }
  if (value && typeof value === "object") {
    if (typeof value.image_url === "string") return toImage(value.image_url);
    if (value.image_url && typeof value.image_url.url === "string") return toImage(value.image_url.url);
    if (value.type === "image" && typeof value.data === "string") {
      const mt = value.mimeType || value.mime_type || "image/png";
      if (!/^image\/(png|jpeg|gif|webp)$/.test(mt)) throw new Error(`Unsupported image type ${mt}`);
      return { type: "image", mimeType: mt, data: value.data };
    }
  }
  throw new Error("image() takes a base64 data: URL, { image_url }, or an image block");
}

function buildContext() {
  const ids = new Map(init.tools.map((t) => [t.id, t.name]));
  const tools = new Proxy({}, {
    get(_t, prop) {
      if (typeof prop !== "string") return undefined;
      const name = ids.get(prop) ?? (init.tools.find((t) => t.name === prop)?.name);
      if (!name) return undefined;
      return (args = {}) => request("call", { name, args }).then((r) => r);
    },
    has(_t, prop) { return ids.has(prop); },
    ownKeys() { return [...ids.keys()]; },
    getOwnPropertyDescriptor(_t, prop) { return ids.has(prop) ? { enumerable: true, configurable: true } : undefined; },
  });
  const out = {
    tools,
    ALL_TOOLS: init.allTools,
    text(v) { const s = fmt(v); bump(s.length); items.push({ type: "text", text: s }); },
    image(v) { const img = toImage(v); bump(img.data.length); items.push(img); },
    exit() { throw new ExitSignal(); },
    store(key, value) {
      key = String(key);
      if (value === undefined) { delete storeSet[key]; storeDel.add(key); delete init.store[key]; return; }
      const json = JSON.stringify(value);
      if (json === undefined) throw new Error("store() takes JSON values");
      if (json.length > 262144) throw new Error("A stored value may have at most 262144 characters of JSON");
      const total = Object.entries({ ...init.store, [key]: value }).reduce((n, [, v]) => n + JSON.stringify(v).length, 0);
      if (total > 1048576) throw new Error("Stored values may have at most 1048576 characters of JSON together");
      storeSet[key] = JSON.parse(json);
      storeDel.delete(key);
      init.store[key] = JSON.parse(json);
    },
    load(key) { const v = init.store[String(key)]; return v === undefined ? undefined : JSON.parse(JSON.stringify(v)); },
    searchTools: (query, opts = {}) => request("search", { query: String(query), limit: opts.limit ?? 8, namespace: opts.namespace }),
    describeTool: (name) => request("describe", { name: String(name) }).then((r) => r ?? undefined),
    describeNamespace: (name) => request("namespace", { name: String(name) }).then((r) => r ?? undefined),
    models: {
      getModelsOfType: (type, provider) => request("models", { op: "getModelsOfType", args: [type, provider] }),
      getAvailableOfType: (type, provider) => request("models", { op: "getAvailableOfType", args: [type, provider] }),
      getModelOfType: (type, provider, id) => request("models", { op: "getModelOfType", args: [type, provider, id] }).then((r) => r ?? undefined),
      classify: (model, context) => request("models", { op: "classify", args: [model, context] }),
      generateImages: (model, context) => request("models", { op: "generateImages", args: [model, context] }),
    },
  };
  const log = (...a) => { const s = a.map((x) => typeof x === "string" ? x : fmt(x)).join(" "); bump(s.length); consoleLines.push(s); };
  out.console = { log, info: log, warn: log, error: log, debug: log };
  return vm.createContext(out, { codeGeneration: { strings: false, wasm: false } });
}

let stallTimer = null;
function scheduleStallCheck() {
  if (finished || stallTimer) return;
  stallTimer = setImmediate(() => {
    stallTimer = null;
    setImmediate(() => {
      if (!finished && pendingCalls === 0 && scriptStarted) finish(false, new Error("Script is waiting on a promise that can never settle (no tool call is pending, and scripts have no timers)"));
    });
  });
}

let scriptStarted = false;
function finish(ok, error) {
  if (finished) return;
  finished = true;
  let err = null;
  if (error && !(error instanceof ExitSignal)) {
    err = error && error.stack && error.name ? `${error.name}: ${error.message}` : String(error && error.message || error);
  } else if (error instanceof ExitSignal) ok = true;
  send({ type: "done", ok: ok && !err, items, console: consoleLines, error: err, store: { set: storeSet, delete: [...storeDel] } });
  setImmediate(() => process.exit(0));
}

rl.on("line", (line) => {
  let m;
  try { m = JSON.parse(line); } catch { return; }
  if (m.type === "init") {
    init = m;
    start();
    return;
  }
  const w = waiting.get(m.id);
  if (!w) return;
  waiting.delete(m.id);
  if (m.ok) w.resolve(m.value); else w.reject(new Error(m.error ?? "Tool call failed"));
});
rl.on("close", () => { if (!finished) finish(false, new Error("Script cancelled")); });

function start() {
  const ctx = buildContext();
  let fn;
  try {
    fn = vm.runInContext(`(async () => {\n${init.code}\n})`, ctx, { filename: "codemode.js", timeout: 10000 });
  } catch (e) {
    finish(false, e);
    return;
  }
  scriptStarted = true;
  let p;
  try { p = fn(); } catch (e) { finish(false, e); return; }
  scheduleStallCheck();
  Promise.resolve(p).then((v) => {
    if (v !== undefined) { const s = fmt(v); items.push({ type: "text", text: s }); }
    finish(true);
  }, (e) => finish(false, e));
}
