import { namespace } from "./_stub.mjs";
export const isAgentMessage = (m) => !!m && typeof m.role === "string";
const real = { isAgentMessage };
export default namespace(real, "pi-agent-core");
