// TSX support for the extension host.
//
// 1. A real compiler when one can be found next to the extension or in the fallback roots:
//    typescript (transpileModule), esbuild (transformSync) or sucrase (transform).
// 2. Otherwise a small built-in JSX transform (automatic runtime, `<jsxImportSource>/jsx-runtime`, default "react";
//    a `/** @jsx h */` pragma switches to the classic runtime), after which Node strips the TypeScript types.
import { existsSync, readFileSync } from "node:fs";
import { createRequire, stripTypeScriptTypes } from "node:module";
import path from "node:path";

function findTsconfig(file) {
  let dir = path.dirname(file);
  for (;;) {
    const p = path.join(dir, "tsconfig.json");
    if (existsSync(p)) {
      try { return JSON.parse(readFileSync(p, "utf8").replace(/\/\/.*$/gm, "").replace(/\/\*[\s\S]*?\*\//g, "").replace(/,(\s*[}\]])/g, "$1")).compilerOptions || {}; } catch { return {}; }
    }
    const up = path.dirname(dir);
    if (up === dir) return {};
    dir = up;
  }
}

function tryRequire(name, file, roots) {
  for (const base of [file, ...roots.map((r) => path.join(r, "_.js"))]) {
    try { return createRequire(base)(name); } catch {}
  }
  return null;
}

export function compileTsx(src, file, roots = []) {
  const opts = { ...findTsconfig(file) };
  const pragmaMatch = /\/\*\*?\s*@jsx\s+([\w$.]+)/.exec(src);
  if (pragmaMatch) { opts.jsx = "react"; opts.jsxFactory = pragmaMatch[1]; }
  const fragMatch = /\/\*\*?\s*@jsxFrag\s+([\w$.]+)/.exec(src);
  if (fragMatch) opts.jsxFragmentFactory = fragMatch[1];
  const importSource = opts.jsxImportSource || "react";
  const ts = tryRequire("typescript", file, roots);
  if (ts?.transpileModule) {
    const r = ts.transpileModule(src, {
      fileName: file,
      compilerOptions: opts.jsx === "react"
        ? { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022, jsx: ts.JsxEmit.React, jsxFactory: opts.jsxFactory,
            jsxFragmentFactory: opts.jsxFragmentFactory, esModuleInterop: true }
        : { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022, jsx: ts.JsxEmit.ReactJSX, jsxImportSource: importSource, esModuleInterop: true },
    });
    return r.outputText;
  }
  const esbuild = tryRequire("esbuild", file, roots);
  if (esbuild?.transformSync) {
    return esbuild.transformSync(src, { loader: "tsx", format: "esm", target: "es2022", jsx: opts.jsx === "react" ? "transform" : "automatic",
      jsxImportSource: importSource, jsxFactory: opts.jsxFactory, jsxFragment: opts.jsxFragmentFactory, sourcefile: file }).code;
  }
  const sucrase = tryRequire("sucrase", file, roots);
  if (sucrase?.transform) {
    return sucrase.transform(src, { transforms: ["typescript", "jsx"], jsxRuntime: opts.jsx === "react" ? "classic" : "automatic",
      jsxImportSource: importSource, production: true, filePath: file }).code;
  }
  const pragma = /\/\*\*?\s*@jsx\s+([\w$.]+)/.exec(src)?.[1] || (opts.jsx === "react" ? (opts.jsxFactory || "React.createElement") : null);
  const fragPragma = /\/\*\*?\s*@jsxFrag\s+([\w$.]+)/.exec(src)?.[1] || opts.jsxFragmentFactory || "React.Fragment";
  let out = transformJsx(src, pragma ? { classic: true, factory: pragma, fragment: fragPragma } : { classic: false });
  if (!pragma && out !== src) {
    out = `import { jsx as __omni_jsx, jsxs as __omni_jsxs, Fragment as __omni_Fragment } from "${importSource}/jsx-runtime";\n` + out;
  }
  return stripTypeScriptTypes(out, { mode: "transform" });
}

// ----------------------------------------------------------------------------------------- mini JSX transform
const ID_START = /[A-Za-z_$]/;
const ID_CHAR = /[\w$\-.:]/;
const PREV_OK = new Set(["(", ",", "=", ":", "?", "[", "{", "}", ";", "!", "&", "|", ">", "", "\n", "+", "-", "*", "%", "~", "^"]);
const KEYWORDS_BEFORE = /(?:^|[^\w$])(return|yield|await|default|case|in|of|typeof|void|delete|throw|else|do)\s*$/;

export function transformJsx(src, opts) {
  let out = "";
  let i = 0;
  const n = src.length;
  while (i < n) {
    const c = src[i];
    // comments
    if (c === "/" && src[i + 1] === "/") { const e = src.indexOf("\n", i); const end = e < 0 ? n : e; out += src.slice(i, end); i = end; continue; }
    if (c === "/" && src[i + 1] === "*") { const e = src.indexOf("*/", i + 2); const end = e < 0 ? n : e + 2; out += src.slice(i, end); i = end; continue; }
    if (c === '"' || c === "'" || c === "`") { const end = skipString(src, i); out += src.slice(i, end); i = end; continue; }
    if (c === "<" && looksLikeJsxStart(src, i, out)) {
      try {
        const r = parseElement(src, i, opts);
        out += r.code;
        i = r.end;
        continue;
      } catch { /* not JSX after all: a comparison or a generic */ }
    }
    out += c;
    i++;
  }
  return out;
}

function skipString(src, i) {
  const q = src[i];
  let j = i + 1;
  while (j < src.length) {
    const c = src[j];
    if (c === "\\") { j += 2; continue; }
    if (q === "`" && c === "$" && src[j + 1] === "{") { j = matchBrace(src, j + 1) + 1; continue; }
    if (c === q) return j + 1;
    j++;
  }
  return j;
}

function matchBrace(src, i) {  // src[i] === "{" ; returns index of matching "}"
  let depth = 0;
  let j = i;
  while (j < src.length) {
    const c = src[j];
    if (c === '"' || c === "'" || c === "`") { j = skipString(src, j); continue; }
    if (c === "/" && src[j + 1] === "/") { const e = src.indexOf("\n", j); j = e < 0 ? src.length : e; continue; }
    if (c === "/" && src[j + 1] === "*") { const e = src.indexOf("*/", j + 2); j = e < 0 ? src.length : e + 2; continue; }
    if (c === "{") depth++;
    else if (c === "}") { depth--; if (depth === 0) return j; }
    j++;
  }
  throw new Error("unbalanced braces");
}

function looksLikeJsxStart(src, i, out) {
  const next = src[i + 1];
  if (!(next === ">" || ID_START.test(next || ""))) return false;
  const before = out.replace(/\s+$/, "");
  const prev = before.slice(-1);
  if (KEYWORDS_BEFORE.test(before)) return true;
  if (prev === ">" && !before.endsWith("=>")) return false;
  return PREV_OK.has(prev) || before.endsWith("=>");
}

function skipWs(src, i) { while (i < src.length && /\s/.test(src[i])) i++; return i; }

function readName(src, i) {
  let j = i;
  while (j < src.length && ID_CHAR.test(src[j])) j++;
  if (j === i) throw new Error("expected a name");
  return [src.slice(i, j), j];
}

function tagExpr(name, opts) {
  if (/^[a-z]/.test(name) && !name.includes(".")) return JSON.stringify(name);
  return name;
}

function parseElement(src, i, opts) {
  // at "<"
  let j = i + 1;
  if (src[j] === ">") {  // fragment
    const { children, end } = parseChildren(src, j + 1, null, opts);
    return { code: emit(null, [], children, opts), end };
  }
  const [name, afterName] = readName(src, j);
  j = afterName;
  const props = [];
  for (;;) {
    j = skipWs(src, j);
    if (src[j] === "/" && src[j + 1] === ">") return { code: emit(name, props, [], opts), end: j + 2 };
    if (src[j] === ">") {
      const { children, end } = parseChildren(src, j + 1, name, opts);
      return { code: emit(name, props, children, opts), end };
    }
    if (src[j] === "{") {  // {...spread}
      const close = matchBrace(src, j);
      const inner = src.slice(j + 1, close).trim();
      if (!inner.startsWith("...")) throw new Error("bad attribute");
      props.push({ spread: transformJsx(inner.slice(3), opts) });
      j = close + 1;
      continue;
    }
    const [attr, afterAttr] = readName(src, j);
    j = skipWs(src, afterAttr);
    if (src[j] !== "=") { props.push({ name: attr, value: "true" }); continue; }
    j = skipWs(src, j + 1);
    if (src[j] === '"' || src[j] === "'") {
      const end = skipString(src, j);
      props.push({ name: attr, value: JSON.stringify(src.slice(j + 1, end - 1)) });
      j = end;
    } else if (src[j] === "{") {
      const close = matchBrace(src, j);
      props.push({ name: attr, value: transformJsx(src.slice(j + 1, close), opts) });
      j = close + 1;
    } else if (src[j] === "<") {
      const r = parseElement(src, j, opts);
      props.push({ name: attr, value: r.code });
      j = r.end;
    } else throw new Error("bad attribute value");
  }
}

function parseChildren(src, i, name, opts) {
  const children = [];
  let j = i;
  let text = "";
  const flush = () => {
    const t = jsxText(text);
    if (t !== null) children.push(JSON.stringify(t));
    text = "";
  };
  while (j < src.length) {
    const c = src[j];
    if (c === "<" && src[j + 1] === "/") {
      flush();
      let k = j + 2;
      if (name === null) {
        k = skipWs(src, k);
        if (src[k] !== ">") throw new Error("bad fragment close");
        return { children, end: k + 1 };
      }
      const [closeName, after] = readName(src, skipWs(src, k));
      if (closeName !== name) throw new Error(`mismatched </${closeName}>`);
      k = skipWs(src, after);
      if (src[k] !== ">") throw new Error("bad close tag");
      return { children, end: k + 1 };
    }
    if (c === "<") {
      flush();
      const r = parseElement(src, j, opts);
      children.push(r.code);
      j = r.end;
      continue;
    }
    if (c === "{") {
      flush();
      const close = matchBrace(src, j);
      const inner = src.slice(j + 1, close);
      if (inner.trim() && !/^\s*\/\*[\s\S]*\*\/\s*$/.test(inner)) children.push(transformJsx(inner, opts));
      j = close + 1;
      continue;
    }
    text += c;
    j++;
  }
  throw new Error("unterminated element");
}

function jsxText(t) {
  if (!t) return null;
  const lines = t.split(/\r?\n/);
  const parts = [];
  lines.forEach((line, idx) => {
    let s = line.replace(/\t/g, " ");
    if (idx > 0) s = s.replace(/^\s+/, "");
    if (idx < lines.length - 1) s = s.replace(/\s+$/, "");
    if (s) parts.push(s);
  });
  if (!parts.length) return null;
  return parts.join(" ").replace(/&nbsp;/g, " ").replace(/&lt;/g, "<").replace(/&gt;/g, ">").replace(/&quot;/g, '"').replace(/&amp;/g, "&");
}

function emit(name, props, children, opts) {
  const propParts = props.map((p) => (p.spread !== undefined ? `...(${p.spread})` : `${JSON.stringify(p.name)}: ${p.value}`));
  if (opts.classic) {
    const tag = name === null ? opts.fragment : tagExpr(name, opts);
    const pr = propParts.length ? `{ ${propParts.join(", ")} }` : "null";
    return `${opts.factory}(${[tag, pr, ...children].join(", ")})`;
  }
  const tag = name === null ? "__omni_Fragment" : tagExpr(name, opts);
  const keyIdx = props.findIndex((p) => p.name === "key");
  const key = keyIdx >= 0 ? props[keyIdx].value : null;
  const rest = propParts.filter((_, k) => k !== keyIdx);
  if (children.length === 1) rest.push(`children: ${children[0]}`);
  else if (children.length > 1) rest.push(`children: [${children.join(", ")}]`);
  const fn = children.length > 1 ? "__omni_jsxs" : "__omni_jsx";
  return `${fn}(${tag}, { ${rest.join(", ")} }${key !== null ? `, ${key}` : ""})`;
}
