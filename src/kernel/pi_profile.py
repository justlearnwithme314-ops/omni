"""Pi profile: barebone, token-efficient. Only Pi's tools + a tiny system prompt."""
PI_TOOLS = {"read", "write", "edit", "bash", "grep", "find", "ls"}
MINIMAL_PROMPT = ("You are an expert coding assistant. Use the tools read, write, edit, bash, grep, find, ls to work in the "
                  "user's project. Be concise. Show file paths clearly.")


def apply(profile: str, executor) -> None:
    reg = executor.tool_registry
    if executor.engine is not None:
        executor.engine.minimal_prompt = profile == "pi"
    if reg is None:
        return
    if profile == "pi":
        for t in reg.list_tools():
            t.enabled = t.name in PI_TOOLS
    else:
        from .features import FeatureRegistry
        reg.apply_feature_toggles(FeatureRegistry(config_data=getattr(executor.config, "config_data", {}),
                                                  config_path=getattr(executor.config, "config_path", None)))
