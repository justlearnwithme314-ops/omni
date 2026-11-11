// Module hooks for the Omni Pi extension host.
// - maps Pi's host-provided packages (@earendil-works/*, @mariozechner/*, @oh-my-pi/*, typebox) to Omni shims
// - resolves TypeScript-style imports ("./x", "./x.js" -> x.ts, directory index)
// - transpiles .ts/.mts/.cts with Node's own TypeScript transform (handles enums + parameter properties,
//   which plain type stripping rejects)
// - rewrites named imports from host packages into destructuring from a Proxy, so an import of a name the shim
//   does not implement yields a recorded stub instead of a link-time SyntaxError
import { createHash } from "node:crypto";
import { existsSync, mkdirSync, readFileSync, renameSync, statSync, writeFileSync } from "node:fs";
import { createRequire, stripTypeScriptTypes } from "node:module";
import os from "node:os";
import path from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";
import { compileTsx } from "./jsx.mjs";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const SHIM = (n) => pathToFileURL(path.join(HERE, "shims", n)).href;

const VIRTUAL = [
  [/^@(?:earendil-works|mariozechner|oh-my-pi)\/pi-coding-agent(?:\/.*)?$/, "coding-agent.mjs"],
  [/^@(?:earendil-works|mariozechner|oh-my-pi)\/pi-ai(?:\/.*)?$/, "ai.mjs"],
  [/^@(?:earendil-works|mariozechner|oh-my-pi)\/pi-tui(?:\/.*)?$/, "tui.mjs"],
  [/^@(?:earendil-works|mariozechner|oh-my-pi)\/pi-agent-core(?:\/.*)?$/, "agent-core.mjs"],
  [/^(?:typebox|@sinclair\/typebox)(?:\/.*)?$/, "typebox.mjs"],
];
export const VIRTUAL_RE = /^(?:@(?:earendil-works|mariozechner|oh-my-pi)\/pi-[a-z-]+(?:\/[^"']*)?|typebox(?:\/[^"']*)?|@sinclair\/typebox(?:\/[^"']*)?)$/;

const FALLBACK_ROOTS = [
  process.env.OMNI_PI_NODE_MODULES,
  path.join(os.homedir(), ".pi", "agent", "npm", "node_modules"),
  path.join(os.homedir(), ".omp", "plugins", "node_modules"),
  path.join(os.homedir(), ".omp", "agent", "node_modules"),
].filter((p) => p && existsSync(p));

const TS_EXT = [".ts", ".tsx", ".mts", ".cts"];  // .jsx is compiled like .tsx

function tryFile(p) {
  try {
    if (statSync(p).isFile()) return p;
  } catch {}
  return null;
}

function resolveRelative(spec, parentURL) {
  if (!parentURL || !parentURL.startsWith("file:")) return null;
  const base = path.resolve(path.dirname(fileURLToPath(parentURL)), spec);
  const cands = [base];
  if (/\.(m|c)?js$/.test(base)) cands.push(base.replace(/\.(m|c)?js$/, ".ts"), base.replace(/\.(m|c)?js$/, ".tsx"));
  for (const e of [...TS_EXT, ".js", ".jsx", ".mjs", ".cjs", ".json"]) cands.push(base + e);
  for (const e of ["index.ts", "index.tsx", "index.js", "index.jsx", "index.mjs"]) cands.push(path.join(base, e));
  for (const c of cands) {
    const f = tryFile(c);
    if (f) return pathToFileURL(f).href;
  }
  return null;
}

export async function resolve(specifier, context, nextResolve) {
  for (const [re, file] of VIRTUAL) {
    if (re.test(specifier)) return { url: SHIM(file), shortCircuit: true };
  }
  try {
    return await nextResolve(specifier, context);
  } catch (err) {
    if (/^\.\.?(\/|$)|^\//.test(specifier) || /^[a-zA-Z]:[\\/]/.test(specifier)) {
      const url = resolveRelative(specifier, context.parentURL);
      if (url) return { url, shortCircuit: true };
      throw err;
    }
    if (/^(node:|file:|data:)/.test(specifier)) throw err;
    for (const root of FALLBACK_ROOTS) {
      try {
        return await nextResolve(specifier, { ...context, parentURL: pathToFileURL(path.join(root, "_.js")).href });
      } catch {}
    }
    if (/^@oh-my-pi\//.test(specifier)) return { url: SHIM("generic.mjs"), shortCircuit: true };
    throw err;
  }
}

let counter = 0;
// import { a, b as c, type T } from "@earendil-works/pi-coding-agent"
const NAMED_IMPORT = /import\s+(?:([A-Za-z_$][\w$]*)\s*,\s*)?\{([^}]*)\}\s*from\s*(["'])([^"']+)\3\s*;?/g;

export function rewriteVirtualImports(src) {
  return src.replace(NAMED_IMPORT, (all, def, names, _q, spec) => {
    if (!VIRTUAL_RE.test(spec)) return all;
    const parts = names.split(",").map((s) => s.trim()).filter(Boolean).filter((s) => !s.startsWith("type "));
    const ns = `__omni_pi_ns${counter++}`;
    const bindings = parts.map((p) => {
      const m = p.match(/^([\w$]+)\s+as\s+([\w$]+)$/);
      return m ? `${m[1]}: ${m[2]}` : p;
    });
    let out = `import ${ns} from "${spec}";`;
    if (def) out += ` const ${def} = ${ns};`;
    if (bindings.length) out += ` const { ${bindings.join(", ")} } = ${ns};`;
    return out;
  });
}

// Compiled TypeScript is cached on disk, keyed by path + mtime + size + Node version: a start with many extensions
// then reads ready-made JavaScript instead of transpiling every file again. OMNI_TS_CACHE=0 turns it off.
const CACHE_DIR = process.env.OMNI_TS_CACHE === "0" ? null
  : path.join(process.env.OMNI_TS_CACHE_DIR || path.join(os.tmpdir(), "omni-ts-cache"), process.version);
let cacheReady = false;
function cached(file, compile) {
  if (!CACHE_DIR) return compile();
  let key;
  try {
    const st = statSync(file);
    key = createHash("sha1").update(`${file}\0${st.mtimeMs}\0${st.size}\0v3`).digest("hex");
  } catch { return compile(); }
  const hit = path.join(CACHE_DIR, key + ".js");
  try { return readFileSync(hit, "utf8"); } catch {}
  const out = compile();
  try {
    if (!cacheReady) { mkdirSync(CACHE_DIR, { recursive: true }); cacheReady = true; }
    const tmp = `${hit}.${process.pid}.tmp`;
    writeFileSync(tmp, out);
    renameSync(tmp, hit);
  } catch {}
  return out;
}

// Extensions written as ES modules sometimes still call require() (Pi runs them through a bundler that allows
// it). Give such a module a real `require` bound to its own location.
const REQUIRE_PRELUDE = 'import { createRequire as __omniCreateRequire } from "node:module"; const require = __omniCreateRequire(import.meta.url);\n';
function withRequire(src) {
  if (!/(^|[^.\w$])require\s*\(/.test(src)) return src;
  if (/\b(?:const|let|var|function)\s+require\b|\bcreateRequire\b|import\s+require\b/.test(src)) return src;
  return REQUIRE_PRELUDE + src;
}
const VIRTUAL_MARK = /@(?:earendil-works|mariozechner|oh-my-pi)\/pi-|["'](?:@sinclair\/typebox|typebox)/;

export async function load(url, context, nextLoad) {
  if (!url.startsWith("file:")) return nextLoad(url, context);
  const file = fileURLToPath(url);
  const ext = path.extname(file);
  const isShim = file.startsWith(path.join(HERE, "shims"));
  if (TS_EXT.includes(ext)) {
    const src = cached(file, () => {
      const raw = readFileSync(file, "utf8");
      const js = ext === ".tsx" ? compileTsx(raw, file, FALLBACK_ROOTS) : stripTypeScriptTypes(raw, { mode: "transform", sourceUrl: url });
      return ext === ".cts" ? rewriteVirtualImports(js) : withRequire(rewriteVirtualImports(js));
    });
    const format = ext === ".cts" ? "commonjs" : "module";
    return { format, source: src, shortCircuit: true };
  }
  if (ext === ".jsx") {
    const src = compileTsx(readFileSync(file, "utf8"), file, FALLBACK_ROOTS);
    return { format: "module", source: rewriteVirtualImports(src), shortCircuit: true };
  }
  if (!isShim && (ext === ".js" || ext === ".mjs") && !file.includes(`${path.sep}node_modules${path.sep}`)) {
    const r = await nextLoad(url, context);
    if (r.format === "module" && r.source) {
      const src = typeof r.source === "string" ? r.source : Buffer.from(r.source).toString("utf8");
      return { ...r, source: withRequire(rewriteVirtualImports(src)) };
    }
    return r;
  }
  if (!isShim && (ext === ".js" || ext === ".mjs")) {
    // A published extension bundle (node_modules/<pkg>/dist/index.js) imports Pi's packages by name too. Without the
    // rewrite, one name the shim lacks is a link-time SyntaxError for the whole extension.
    const r = await nextLoad(url, context);
    if (r.format === "module" && r.source) {
      const src = typeof r.source === "string" ? r.source : Buffer.from(r.source).toString("utf8");
      if (VIRTUAL_MARK.test(src)) return { ...r, source: rewriteVirtualImports(src) };
    }
    return r;
  }
  return nextLoad(url, context);
}
