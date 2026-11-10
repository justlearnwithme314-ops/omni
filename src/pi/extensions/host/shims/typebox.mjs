// Minimal TypeBox: builders return plain JSON Schema (what Pi validates against anyway).
import { namespace } from "./_stub.mjs";

export const Kind = Symbol.for("TypeBox.Kind");
export const OptionalKind = Symbol.for("TypeBox.Optional");
export const ReadonlyKind = Symbol.for("TypeBox.Readonly");
export const Hint = Symbol.for("TypeBox.Hint");

const mk = (kind, schema, opts) => {
  const s = { ...schema, ...(opts || {}) };
  Object.defineProperty(s, Kind, { value: kind, enumerable: false });
  return s;
};

export const Type = {
  String: (o) => mk("String", { type: "string" }, o),
  Number: (o) => mk("Number", { type: "number" }, o),
  Integer: (o) => mk("Integer", { type: "integer" }, o),
  Boolean: (o) => mk("Boolean", { type: "boolean" }, o),
  Null: (o) => mk("Null", { type: "null" }, o),
  Any: (o) => mk("Any", {}, o),
  Unknown: (o) => mk("Unknown", {}, o),
  Never: (o) => mk("Never", { not: {} }, o),
  Undefined: (o) => mk("Undefined", { not: {} }, o),
  Void: (o) => mk("Void", { not: {} }, o),
  Literal: (v, o) => mk("Literal", { const: v, type: typeof v === "number" ? "number" : typeof v === "boolean" ? "boolean" : "string" }, o),
  Array: (items, o) => mk("Array", { type: "array", items }, o),
  Tuple: (items, o) => mk("Tuple", { type: "array", items, prefixItems: items, minItems: items.length, maxItems: items.length }, o),
  Union: (anyOf, o) => mk("Union", { anyOf }, o),
  Intersect: (allOf, o) => mk("Intersect", { allOf }, o),
  Enum: (e, o) => mk("Enum", { enum: Object.values(e).filter((v) => typeof e[v] !== "number") }, o),
  Record: (_k, v, o) => mk("Record", { type: "object", additionalProperties: v }, o),
  Optional: (s) => {
    const c = { ...s };
    for (const sym of Object.getOwnPropertySymbols(s)) Object.defineProperty(c, sym, { value: s[sym], enumerable: false });
    Object.defineProperty(c, OptionalKind, { value: "Optional", enumerable: false });
    return c;
  },
  Readonly: (s) => s,
  Unsafe: (s) => s,
  Ref: (ref, o) => mk("Ref", { $ref: typeof ref === "string" ? ref : ref.$id }, o),
  Object: (properties, o) => {
    const required = Object.keys(properties || {}).filter((k) => !properties[k][OptionalKind]);
    const s = { type: "object", properties: properties || {} };
    if (required.length) s.required = required;
    return mk("Object", s, o);
  },
  Partial: (obj) => {
    const props = {};
    for (const [k, v] of Object.entries(obj.properties || {})) props[k] = Type.Optional(v);
    return Type.Object(props);
  },
  Required: (obj) => ({ ...obj, required: Object.keys(obj.properties || {}) }),
  Pick: (obj, keys) => Type.Object(Object.fromEntries(Object.entries(obj.properties || {}).filter(([k]) => keys.includes(k)))),
  Omit: (obj, keys) => Type.Object(Object.fromEntries(Object.entries(obj.properties || {}).filter(([k]) => !keys.includes(k)))),
  KeyOf: (obj) => Type.Union(Object.keys(obj.properties || {}).map((k) => Type.Literal(k))),
  Composite: (objs) => Type.Object(Object.assign({}, ...objs.map((o) => o.properties || {}))),
  Recursive: (fn) => fn(Type.Any()),
  Date: (o) => mk("Date", { type: "string", format: "date-time" }, o),
  Function: () => mk("Function", {}),
  Refine: (s) => s,
  Codec: (s) => s,
};
export const T = Type;

function check(schema, value) {
  if (!schema || typeof schema !== "object") return true;
  if (schema.const !== undefined) return value === schema.const;
  if (schema.enum) return schema.enum.includes(value);
  if (schema.anyOf) return schema.anyOf.some((s) => check(s, value));
  switch (schema.type) {
    case "string": return typeof value === "string";
    case "number": return typeof value === "number";
    case "integer": return Number.isInteger(value);
    case "boolean": return typeof value === "boolean";
    case "null": return value === null;
    case "array": return Array.isArray(value) && (!schema.items || value.every((v) => check(schema.items, v)));
    case "object":
      if (typeof value !== "object" || value === null) return false;
      return (schema.required || []).every((k) => k in value) &&
        Object.entries(schema.properties || {}).every(([k, s]) => !(k in value) || check(s, value[k]));
    default: return true;
  }
}

export const Value = {
  Check: (s, v) => check(s, v),
  Errors: (s, v) => (check(s, v) ? [] : [{ path: "", message: "Invalid value" }]),
  Default: (_s, v) => v,
  Clean: (_s, v) => v,
  Convert: (_s, v) => v,
  Cast: (_s, v) => v,
  Clone: (v) => structuredClone(v),
  Parse: (_s, v) => v,
  Decode: (_s, v) => v,
  Encode: (_s, v) => v,
};
export const TypeCompiler = { Compile: (s) => ({ Check: (v) => check(s, v), Errors: (v) => Value.Errors(s, v) }) };
export const Compile = TypeCompiler.Compile;
export const FormatRegistry = { Set() {}, Has: () => false, Get() {}, Delete() {} };
export const Format = FormatRegistry;
export const TypeGuard = { IsObject: (s) => s?.type === "object", IsString: (s) => s?.type === "string" };
export const KindGuard = TypeGuard;

const real = { Type, T, Kind, OptionalKind, ReadonlyKind, Hint, Value, TypeCompiler, Compile, FormatRegistry, Format, TypeGuard, KindGuard };
export default namespace(real, "typebox");
