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

  const pi = {
    registerTool(toolDef) {
      if (toolDef && toolDef.name) {
        tools.set(toolDef.name, toolDef);
      }
    },
    registerCommand(name, handler) {
      commands.set(name, handler);
    },
    registerProvider(p) {},
    registerFlag(f) {},
    on(event, handler) {},
    emit(event, data) {},
    off(event, handler) {},
    events: { emit() {}, on() {}, off() {} },
    logger: {
      info(...args) { process.stderr.write("[pi:info] " + args.join(" ") + "\n"); },
      warn(...args) { process.stderr.write("[pi:warn] " + args.join(" ") + "\n"); },
      error(...args) { process.stderr.write("[pi:error] " + args.join(" ") + "\n"); },
      debug(...args) {},
    },
    config: {},
  };

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

  const extData = { tools, commands, pi };
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
        res = await Promise.resolve(tool.execute(args || {}));
      } else if (typeof tool.handler === "function") {
        res = await Promise.resolve(tool.handler(args || {}));
      } else {
        throw new Error("Tool " + tool_name + " has no execute or handler method");
      }
      const output = typeof res === "string" ? res : JSON.stringify(res);
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
