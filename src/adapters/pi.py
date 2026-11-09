from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import subprocess
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

from .base import CanonicalTool, DiscoveryResult

logger = logging.getLogger("omni.adapters.pi")

_NODE_RUNNER_SCRIPT = r"""
const readline = require('readline');
const path = require('path');
const Module = require('module');

// Mock @oh-my-pi/pi-coding-agent ExtensionAPI
const origRequire = Module.prototype.require;
const mockExtensionAPI = {
  ExtensionAPI: class MockExtensionAPI {},
};

Module.prototype.require = function(id) {
  if (id === '@oh-my-pi/pi-coding-agent' || id.endsWith('/pi-coding-agent')) {
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
    events: { emit() {}, on() {}, off() {} },
    logger: {
      info() {},
      warn() {},
      error() {},
      debug() {},
    },
    config: {},
  };

  let mod;
  try {
    mod = require(absPath);
  } catch (err) {
    const { pathToFileURL } = require('url');
    mod = await import(pathToFileURL(absPath).href);
  }

  const initFn = typeof mod === 'function' ? mod : (mod.default || mod.register);
  if (typeof initFn === 'function') {
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

rl.on('line', async (line) => {
  if (!line.trim()) return;
  let msg;
  try {
    msg = JSON.parse(line);
  } catch (e) {
    return;
  }

  const { id, method, params } = msg;

  try {
    if (method === 'discover') {
      const extPath = params.extension_path;
      const extData = await getOrLoadExtension(extPath);
      const toolList = [];
      for (const [name, t] of extData.tools.entries()) {
        toolList.push({
          name: t.name,
          description: t.description || '',
          parameters: t.parameters || { type: 'object', properties: {} },
        });
      }
      process.stdout.write(JSON.stringify({ jsonrpc: '2.0', id, result: { tools: toolList } }) + '\n');
    } else if (method === 'execute') {
      const { extension_path, tool_name, args } = params;
      const extData = await getOrLoadExtension(extension_path);
      const tool = extData.tools.get(tool_name);
      if (!tool) {
        throw new Error(`Tool '${tool_name}' not found in extension`);
      }
      let res;
      if (typeof tool.execute === 'function') {
        res = await Promise.resolve(tool.execute(args || {}));
      } else if (typeof tool.handler === 'function') {
        res = await Promise.resolve(tool.handler(args || {}));
      } else {
        throw new Error(`Tool '${tool_name}' has no execute or handler method`);
      }
      const output = typeof res === 'string' ? res : JSON.stringify(res);
      process.stdout.write(JSON.stringify({ jsonrpc: '2.0', id, result: output }) + '\n');
    } else {
      process.stdout.write(JSON.stringify({ jsonrpc: '2.0', id, error: { code: -32601, message: 'Method not found' } }) + '\n');
    }
  } catch (err) {
    process.stdout.write(JSON.stringify({
      jsonrpc: '2.0',
      id,
      error: {
        code: -32000,
        message: String(err && err.message ? err.message : err)
      }
    }) + '\n');
  }
});
"""


class NodeProcessManager:
    """Manages the long-lived Node.js child process communicating via JSON-RPC stdio."""

    def __init__(self):
        self._proc: Optional[subprocess.Popen] = None
        self._lock = threading.Lock()
        self._req_id = 0

    def _ensure_proc(self) -> subprocess.Popen:
        if self._proc is not None and self._proc.poll() is None:
            return self._proc

        node_bin = shutil.which("node")
        if not node_bin:
            raise RuntimeError("Node.js binary not found on PATH. Required for Pi extensions.")

        env = os.environ.copy()
        omp_nm = os.path.expanduser("~/.omp/plugins/node_modules")
        if os.path.exists(omp_nm):
            existing = env.get("NODE_PATH", "")
            env["NODE_PATH"] = f"{omp_nm};{existing}" if existing else omp_nm

        runner_js = os.path.join(os.path.dirname(__file__), "pi_runner.js")
        self._proc = subprocess.Popen(
            [node_bin, "--experimental-transform-types", "--no-warnings", "--import", Path(os.path.join(os.path.dirname(__file__), "pi_register.mjs")).resolve().as_uri(), runner_js],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
            env=env,
        )
        return self._proc

    def send_request(self, method: str, params: Dict[str, Any], timeout: float = 5.0) -> Any:
        with self._lock:
            try:
                proc = self._ensure_proc()
                self._req_id += 1
                req_id = self._req_id

                msg = {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "method": method,
                    "params": params,
                }

                if proc.stdin is None or proc.stdout is None:
                    return None

                proc.stdin.write(json.dumps(msg) + "\n")
                proc.stdin.flush()

                data = None
                while data is None:  # skip stray (non-JSON / other-id) output from extensions
                    raw_line = proc.stdout.readline()
                    if not raw_line:
                        self._proc = None
                        return None
                    try:
                        cand = json.loads(raw_line)
                    except ValueError:
                        continue
                    if isinstance(cand, dict) and cand.get("id") == req_id:
                        data = cand
                if "error" in data:
                    return None
                return data.get("result")
            except Exception:
                self._proc = None
                return None


class PiAdapter:
    """Adapter for Pi extensions written in JavaScript or TypeScript."""

    def __init__(
        self,
        root_dir: Optional[Union[str, Path]] = None,
        config: Optional[Any] = None,
        event_bus: Optional[Any] = None,
    ):
        if root_dir is not None:
            self.root_dir = Path(root_dir).resolve()
        else:
            self.root_dir = Path(__file__).resolve().parent.parent.parent

        self.config = config
        self.event_bus = event_bus
        self.tools: List[CanonicalTool] = []
        self._runner = NodeProcessManager()

    def __del__(self):
        try:
            self._runner.close()
        except Exception:
            pass

    def _make_tool_handler(self, ext_path: Path, tool_name: str) -> Any:
        runner = self._runner
        ext_str = str(ext_path).replace("\\", "/")

        async def handler(args: dict, context: dict = None) -> str:
            def _call():
                return runner.send_request("execute", {
                    "extension_path": ext_str,
                    "tool_name": tool_name,
                    "args": args if args is not None else {},
                })

            res = await asyncio.to_thread(_call)
            return res if isinstance(res, str) else json.dumps(res)

        return handler

    def discover(self) -> DiscoveryResult:
        """Scans plugins/pi/ for .ts and .js extension files, registering discovered tools."""
        discovered: List[CanonicalTool] = []
        plugins_dir = self.root_dir / "plugins" / "pi"

        if not plugins_dir.is_dir():
            self.tools = []
            return DiscoveryResult([])

        if not shutil.which("node"):
            logger.warning("PiAdapter: Node.js is not installed; skipping Pi extension discovery")
            self.tools = []
            return DiscoveryResult([])

        # Targeted discovery: check package.json or direct entrypoint files (never blind rglob)
        target_files = []
        for item in sorted(plugins_dir.iterdir()):
            if any(skip in item.name for skip in ["node_modules", ".git", ".cache"]):
                continue
            if item.is_file() and item.suffix.lower() in (".js", ".ts", ".mjs", ".cjs"):
                if not item.name.endswith((".test.js", ".spec.js", ".d.ts")):
                    target_files.append(item)
                continue

            if not item.is_dir():
                continue

            pkg_json = item / "package.json"
            if pkg_json.is_file():
                try:
                    with open(pkg_json, "r", encoding="utf-8") as f:
                        pkg_data = json.load(f)
                    pi_cfg = pkg_data.get("pi", {})
                    ext_entries = pi_cfg.get("extensions", [])
                    if isinstance(ext_entries, str):
                        ext_entries = [ext_entries]
                    for entry in ext_entries:
                        target = (item / entry).resolve()
                        if target.is_file() and target.suffix.lower() in (".js", ".ts", ".mjs", ".cjs"):
                            target_files.append(target)
                        elif target.is_dir():
                            for sub in target.iterdir():
                                if sub.is_file() and sub.suffix.lower() in (".js", ".ts", ".mjs", ".cjs"):
                                    if not sub.name.endswith((".d.ts", ".test.ts", ".test.js", ".spec.ts", ".spec.js")):
                                        target_files.append(sub)
                    if not ext_entries and pkg_data.get("main"):
                        main_target = (item / pkg_data["main"]).resolve()
                        if main_target.is_file():
                            target_files.append(main_target)
                except Exception as err:
                    logger.debug(f"PiAdapter: Error parsing package.json in {item.name}: {err}")
            else:
                for cand in ["extension.js", "extension.ts", "index.js", "index.ts"]:
                    cand_file = item / cand
                    if cand_file.is_file():
                        target_files.append(cand_file)
                        break

        seen_paths = set()
        for ext_file in target_files:
            ext_str = str(ext_file).replace("\\", "/")
            if ext_str in seen_paths:
                continue
            seen_paths.add(ext_str)

            try:
                result = self._runner.send_request("discover", {"extension_path": ext_str}, timeout=5.0)
                tool_list = (result or {}).get("tools", [])

                for tool_def in tool_list:
                    tool_name = tool_def.get("name")
                    if not tool_name:
                        continue

                    tool = CanonicalTool(
                        name=tool_name,
                        description=tool_def.get("description", ""),
                        parameters=tool_def.get("parameters", {"type": "object", "properties": {}}),
                        handler=self._make_tool_handler(ext_file, tool_name),
                        source="pi",
                        metadata={
                            "extension_path": ext_str,
                            "file_name": ext_file.name,
                        },
                    )
                    discovered.append(tool)
                    logger.info(f"PiAdapter: Registered tool '{tool_name}' from {ext_file.name}")
            except Exception as e:
                logger.debug(f"PiAdapter: Skipping {ext_file.name}: {e}")

        self.tools = discovered
        return DiscoveryResult(discovered)
