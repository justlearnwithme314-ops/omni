const readline = require("readline");
const path = require("path");
const Module = require("module");

// Redirect console.log and console.error to stderr so stdout is strictly for JSON-RPC
const origLog = console.log;
console.log = (...args) => process.stderr.write(args.join(" ") + "\n");
console.error = (...args) => process.stderr.write(args.join(" ") + "\n");

// Intercept Pi core module imports
const origRequire = Module.prototype.require;
const mockExtensionAPI = {
  ExtensionAPI: class MockExtensionAPI {},
};

Module.prototype.require = function(id) {
  if (id === "@oh-my-pi/pi-coding-agent" || id.endsWith("/pi-coding-agent") || id.includes("pi-ai")) {
    return mockExtensionAPI;
  }
  return origRequire.apply(this, arguments);
};

const loadedExtensions = new Map();

async function getOrLoadExtension(filePath) {
  const absPath = path.resolve(filePath);
  if (loadedExtensions.has(absPath)) {
    return loadedExtensions.get(absPath);
  }
  const tools = new Map();
  const commands = new Map();

  const handlers = new Map();
  const noop = () => undefined;
  const base = {
    registerTool(toolDef) { if (toolDef && toolDef.name) tools.set(toolDef.name, toolDef); },
    registerCommand(name, opts) { commands.set(name, opts); },
    on(event, handler) { if (!handlers.has(event)) handlers.set(event, []); handlers.get(event).push(handler); },
    off(event, handler) { handlers.set(event, (handlers.get(event) || []).filter((h) => h !== handler)); },
    emit(event, data) { return fire(event, data); },
    getActiveTools: () => [...tools.keys()],
    getAllTools: () => [...tools.values()].map((t) => ({ name: t.name, description: t.description })),
    getCommands: () => [...commands.keys()],
    getFlag: noop,
    exec: async (cmd, args = []) => {
      const { spawnSync } = require("child_process");
      const r = spawnSync(cmd, args, { encoding: "utf8", shell: true });
      return { stdout: r.stdout || "", stderr: r.stderr || "", code: r.status ?? 1 };
    },
    events: { emit() {}, on() {}, off() {} },
    logger: {
      info(...a) { process.stderr.write("[pi:info] " + a.join(" ") + "\n"); },
      warn(...a) { process.stderr.write("[pi:warn] " + a.join(" ") + "\n"); },
      error(...a) { process.stderr.write("[pi:error] " + a.join(" ") + "\n"); },
      debug() {},
    },
    config: {},
  };
  // Any API method we do not implement (registerShortcut, registerMessageRenderer, sendMessage, ...) is a safe no-op.
  const pi = new Proxy(base, { get: (t, k) => (k in t ? t[k] : (typeof k === "string" ? noop : undefined)) });
  const uiStub = new Proxy({}, { get: () => async () => undefined });
  const ctx = { ui: uiStub, hasUI: false, cwd: process.cwd(), sessionManager: new Proxy({}, { get: () => () => [] }) };
  async function fire(event, data) {
    for (const h of handlers.get(event) || []) {
      try { await h(data || { type: event }, ctx); } catch (e) { process.stderr.write("[pi:handler " + event + "] " + e + "\n"); }
    }
  }

  let mod;
  try {
    mod = require(absPath);
  } catch (err) {
    const { pathToFileURL } = require("url");
    mod = await import(pathToFileURL(absPath).href);
  }

  const initFn = typeof mod === "function" ? mod : (mod.default || mod.register);
  if (typeof initFn === "function") {
    await Promise.resolve(initFn(pi));
  }
  await fire("session_start", { type: "session_start" });

  const extData = { tools, commands, pi, ctx };
  loadedExtensions.set(absPath, extData);
  return extData;
}

const rl = readline.createInterface({
  input: process.stdin,
  output: process.stdout,
  terminal: false,
});

rl.on("line", async (line) => {
  if (!line.trim()) return;
  let msg;
  try {
    msg = JSON.parse(line);
  } catch (e) {
    return;
  }

  const { id, method, params } = msg;

  try {
    if (method === "discover") {
      const ext = await getOrLoadExtension(params.extension_path);
      const toolList = [];
      for (const [name, def] of ext.tools.entries()) {
        toolList.push({
          name,
          description: def.description || "",
          parameters: def.parameters || { type: "object", properties: {} },
        });
      }
      const commandList = Array.from(ext.commands.keys());
      process.stdout.write(JSON.stringify({ jsonrpc: "2.0", id, result: { tools: toolList, commands: commandList } }) + "\n");
    } else if (method === "execute") {
      const ext = await getOrLoadExtension(params.extension_path);
      const { tool_name, args } = params;
      const tool = ext.tools.get(tool_name);
      if (!tool) {
        throw new Error("Tool " + tool_name + " not found in extension");
      }
      let res;
      if (typeof tool.execute === "function") {
        res = await Promise.resolve(tool.execute("call_" + Date.now(), args || {}, undefined, undefined, extData.ctx));
      } else if (typeof tool.handler === "function") {
        res = await Promise.resolve(tool.handler(args || {}));
      } else {
        throw new Error("Tool " + tool_name + " has no execute or handler method");
      }
      const output = typeof res === "string" ? res
        : (res && Array.isArray(res.content)) ? res.content.map((c) => c.text || "").join("\n") : JSON.stringify(res);
      process.stdout.write(JSON.stringify({ jsonrpc: "2.0", id, result: output }) + "\n");
    } else {
      process.stdout.write(JSON.stringify({ jsonrpc: "2.0", id, error: { code: -32601, message: "Method not found" } }) + "\n");
    }
  } catch (err) {
    process.stdout.write(JSON.stringify({
      jsonrpc: "2.0",
      id,
      error: {
        code: -32000,
        message: String(err && err.message ? err.message : err)
      }
    }) + "\n");
  }
});
