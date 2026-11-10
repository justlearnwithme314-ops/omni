// Proxy namespaces: any export a shim does not implement resolves to a recorded, harmless stub.
export const missing = new Map(); // "module:name" -> count

export function stub(label) {
  const fn = function (...args) {
    record(label);
    if (new.target) return this;
    return proxyStub(label);
  };
  return new Proxy(fn, {
    get(t, p) {
      if (p === Symbol.toPrimitive) return () => "";
      if (p === "then") return undefined; // never look like a promise
      if (p === "__omniMissing") return label;
      if (p in t) return t[p];
      return stub(`${label}.${String(p)}`);
    },
  });
}

function proxyStub(label) {
  return stub(label);
}

export function record(label) {
  missing.set(label, (missing.get(label) || 0) + 1);
  const h = globalThis.__omniPi;
  if (h && h.onMissing) h.onMissing(label);
}

export function namespace(real, moduleName) {
  return new Proxy(real, {
    get(t, p) {
      if (p in t) return t[p];
      if (typeof p === "symbol" || p === "then" || p === "default") return undefined;
      record(`${moduleName}.${p}`);
      return stub(`${moduleName}.${p}`);
    },
    has(t, p) {
      return typeof p === "string" ? true : p in t;
    },
  });
}
