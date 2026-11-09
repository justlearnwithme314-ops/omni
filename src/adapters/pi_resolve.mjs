// Fallback module resolution: extensions often rely on packages installed for Pi/OMP, not next to the extension.
import { pathToFileURL } from "node:url";
import { existsSync } from "node:fs";
import os from "node:os";
import path from "node:path";

const ROOTS = [
  path.join(os.homedir(), ".pi", "agent", "npm", "node_modules"),
  path.join(os.homedir(), ".omp", "plugins", "node_modules"),
  path.join(os.homedir(), ".omp", "agent", "node_modules"),
].filter(existsSync);

export async function resolve(specifier, context, nextResolve) {
  try {
    return await nextResolve(specifier, context);
  } catch (err) {
    if (/^\.\.?\/.*\.js$/.test(specifier)) {  // TS-style import: "./x.js" actually lives in x.ts
      try { return await nextResolve(specifier.replace(/\.js$/, ".ts"), context); } catch {}
    }
    if (/^[.\/]|^[a-zA-Z]:|^node:|^file:/.test(specifier)) throw err;
    for (const root of ROOTS) {
      try {
        return await nextResolve(specifier, { ...context, parentURL: pathToFileURL(path.join(root, "_.js")).href });
      } catch {}
    }
    throw err;
  }
}

import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { stripTypeScriptTypes } from "node:module";

// Node refuses to strip types under node_modules; transform them ourselves.
export async function load(url, context, nextLoad) {
  if (url.endsWith(".ts") && url.includes("node_modules")) {
    const source = stripTypeScriptTypes(readFileSync(fileURLToPath(url), "utf8"), { mode: "transform" });
    return { format: "module", source, shortCircuit: true };
  }
  return nextLoad(url, context);
}
