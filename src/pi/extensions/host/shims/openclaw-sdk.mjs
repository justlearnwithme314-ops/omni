// Shim for `openclaw/plugin-sdk/*`: the entry helpers and the small parameter/result helpers tool plugins use.
// OpenClaw's SDK has hundreds of subpaths; anything not implemented here resolves to a recorded stub, so a plugin
// that needs more loads, and `/extensions` lists exactly what it asked for that is missing.
import { namespace } from "./_stub.mjs";
import typebox from "./typebox.mjs";

const Type = typebox.Type;

function paramError(key, what) {
  const e = new Error(`${key} ${what}`);
  e.name = "ToolInputError";
  return e;
}

function readStringParam(params, key, opts = {}) {
  const raw = params?.[key];
  if (raw === undefined || raw === null || raw === "") {
    if (opts.required) throw paramError(opts.label || key, "is required");
    return undefined;
  }
  const s = typeof raw === "string" ? raw : String(raw);
  const out = opts.trim === false ? s : s.trim();
  if (!out && opts.required && !opts.allowEmpty) throw paramError(opts.label || key, "is required");
  return out;
}

function readNumberParam(params, key, opts = {}) {
  const raw = params?.[key];
  if (raw === undefined || raw === null || raw === "") {
    if (opts.required) throw paramError(opts.label || key, "is required");
    return undefined;
  }
  const n = typeof raw === "number" ? raw : Number(raw);
  if (!Number.isFinite(n)) throw paramError(opts.label || key, "must be a number");
  return opts.integer ? Math.trunc(n) : n;
}

function readIntegerInRange(params, key, opts, min) {
  const n = readNumberParam(params, key, { ...opts, integer: true });
  if (n === undefined) return undefined;
  if (n < min) throw paramError(opts?.label || key, `must be >= ${min}`);
  return n;
}

function readStringArrayParam(params, key, opts = {}) {
  const raw = params?.[key];
  if (raw === undefined || raw === null) {
    if (opts.required) throw paramError(opts.label || key, "is required");
    return undefined;
  }
  const list = (Array.isArray(raw) ? raw : [raw]).map((v) => String(v).trim()).filter(Boolean);
  if (!list.length && opts.required) throw paramError(opts.label || key, "is required");
  return list;
}

const optional = (schema) => Type.Optional(schema);

const real = {
  definePluginEntry: (entry) => ({ ...entry, __openclawEntry: "plugin" }),
  defineChannelPluginEntry: (entry) => ({ ...entry, __openclawEntry: "channel" }),
  defineBundledChannelEntry: (entry) => ({ ...entry, __openclawEntry: "channel" }),
  emptyPluginConfigSchema: () => ({ type: "object", additionalProperties: false, properties: {} }),
  jsonResult: (payload) => ({ content: [{ type: "text", text: JSON.stringify(payload, null, 2) }], details: payload }),
  textResult: (text, details) => ({ content: [{ type: "text", text: String(text) }], details }),
  readStringParam,
  readNumberParam,
  readStringArrayParam,
  readStringOrNumberParam: (params, key, opts) => readStringParam(params, key, opts),
  readPositiveIntegerParam: (params, key, opts) => readIntegerInRange(params, key, opts, 1),
  readNonNegativeIntegerParam: (params, key, opts) => readIntegerInRange(params, key, opts, 0),
  readBooleanParam: (params, key) => (params?.[key] === undefined ? undefined : params[key] === true || params[key] === "true"),
  optionalStringEnum: (values, opts = {}) => optional(Type.Union(values.map((v) => Type.Literal(v)), opts)),
  stringEnum: (values, opts = {}) => Type.Union(values.map((v) => Type.Literal(v)), opts),
  optionalPositiveIntegerSchema: (opts = {}) => optional(Type.Integer({ minimum: 1, ...opts })),
  optionalNonNegativeIntegerSchema: (opts = {}) => optional(Type.Integer({ minimum: 0, ...opts })),
  ToolInputError: class ToolInputError extends Error { constructor(m) { super(m); this.name = "ToolInputError"; } },
  Type,
};

export default namespace(real, "openclaw-sdk");
