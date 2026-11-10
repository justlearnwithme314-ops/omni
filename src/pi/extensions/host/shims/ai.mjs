// pi-ai surface used by extensions: StringEnum, TypeBox re-exports, a few transcript helpers, and model calls
// routed through Omni's provider layer.
import { namespace } from "./_stub.mjs";
import { Type } from "./typebox.mjs";

export { Type };
export const StringEnum = (values, opts = {}) => ({ type: "string", enum: [...values], ...opts });
export const getSystemMessageText = (m) =>
  m?.content || Object.values(m?.sections || {}).filter(Boolean).join("\n\n");
export const getTextContent = (content) =>
  typeof content === "string" ? content : (content || []).filter((b) => b.type === "text").map((b) => b.text).join("");

const host = () => globalThis.__omniPi;
export async function complete(model, context, options = {}) {
  return host().rpc("ai.complete", { model, context, options: { reasoning: options.reasoning, maxTokens: options.maxTokens } });
}
export const completeSimple = complete;
export function stream(model, context, options = {}) {
  const p = complete(model, context, options);
  return {
    async *[Symbol.asyncIterator]() {
      const msg = await p;
      yield { type: "done", message: msg };
    },
    result: () => p,
  };
}
export const streamSimple = stream;
export const getModels = () => host()?.state?.models || [];
export const getModel = (provider, id) => getModels().find((m) => m.provider === provider && m.id === id);
export const getProviders = () => [...new Set(getModels().map((m) => m.provider))];
export const calculateCost = (model, usage) => {
  const c = model?.cost || {};
  const part = (n, k) => ((usage?.[n] || 0) * (c[k] || 0)) / 1e6;
  const cost = { input: part("input", "input"), output: part("output", "output"), cacheRead: part("cacheRead", "cacheRead"), cacheWrite: part("cacheWrite", "cacheWrite") };
  cost.total = cost.input + cost.output + cost.cacheRead + cost.cacheWrite;
  return cost;
};
export const registerApiProvider = (api, fns) => host().registerApi(api, fns);

const real = { Type, StringEnum, getSystemMessageText, getTextContent, complete, completeSimple, stream, streamSimple, getModels, getModel, getProviders, calculateCost, registerApiProvider };
export default namespace(real, "pi-ai");
