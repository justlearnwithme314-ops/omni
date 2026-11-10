"""Model catalog and resolution (Pi's model-registry + models.json + extension providers).

A model is a plain dict shaped like pi-ai's `Model`:
    {id, name, provider, api, baseUrl, reasoning, input, contextWindow, maxTokens, cost{input,output,cacheRead,cacheWrite}}
`cost` is USD per million tokens.
"""
from __future__ import annotations

import fnmatch
import json
import os
import re
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional

THINKING_LEVELS = ["off", "minimal", "low", "medium", "high", "xhigh", "max"]

# provider -> (api, baseUrl, env vars)
PROVIDERS: Dict[str, Dict[str, Any]] = {
    "anthropic": {"api": "anthropic-messages", "baseUrl": "https://api.anthropic.com", "env": ["ANTHROPIC_API_KEY", "ANTHROPIC_OAUTH_TOKEN"]},
    "openai": {"api": "openai-completions", "baseUrl": "https://api.openai.com/v1", "env": ["OPENAI_API_KEY"]},
    "google": {"api": "google-generative-ai", "baseUrl": "https://generativelanguage.googleapis.com/v1beta", "env": ["GEMINI_API_KEY", "GOOGLE_API_KEY"]},
    "google-antigravity": {"api": "google-antigravity", "baseUrl": "https://daily-cloudcode-pa.googleapis.com", "env": []},
    # subscription sign-ins (`/login openai-codex`, `/login github-copilot`); stream adapters in stream.py
    "openai-codex": {"api": "openai-codex-responses", "baseUrl": "https://chatgpt.com/backend-api", "env": []},
    "github-copilot": {"api": "github-copilot", "baseUrl": "https://api.individual.githubcopilot.com",
                       "env": ["COPILOT_GITHUB_TOKEN"]},
    "openrouter": {"api": "openai-completions", "baseUrl": "https://openrouter.ai/api/v1", "env": ["OPENROUTER_API_KEY"]},
    "groq": {"api": "openai-completions", "baseUrl": "https://api.groq.com/openai/v1", "env": ["GROQ_API_KEY"]},
    "xai": {"api": "openai-completions", "baseUrl": "https://api.x.ai/v1", "env": ["XAI_API_KEY"]},
    "deepseek": {"api": "openai-completions", "baseUrl": "https://api.deepseek.com/v1", "env": ["DEEPSEEK_API_KEY"]},
    "mistral": {"api": "openai-completions", "baseUrl": "https://api.mistral.ai/v1", "env": ["MISTRAL_API_KEY"]},
    "cerebras": {"api": "openai-completions", "baseUrl": "https://api.cerebras.ai/v1", "env": ["CEREBRAS_API_KEY"]},
    "zai": {"api": "openai-completions", "baseUrl": "https://api.z.ai/api/paas/v4", "env": ["ZAI_API_KEY"]},
    "ollama": {"api": "openai-completions", "baseUrl": "http://localhost:11434/v1", "env": [], "noAuth": True},
    "lmstudio": {"api": "openai-completions", "baseUrl": "http://localhost:1234/v1", "env": [], "noAuth": True},
    "llama-cpp": {"api": "openai-completions", "baseUrl": "http://localhost:8080/v1", "env": [], "noAuth": True},
    "faux": {"api": "faux", "baseUrl": "", "env": [], "noAuth": True},
}


def _m(provider: str, mid: str, name: str, ctx: int, out: int, cin: float, cout: float, reasoning: bool = True,
       image: bool = True, cread: Optional[float] = None, cwrite: Optional[float] = None) -> Dict[str, Any]:
    p = PROVIDERS[provider]
    return {
        "id": mid, "name": name, "provider": provider, "api": p["api"], "baseUrl": p["baseUrl"],
        "reasoning": reasoning, "input": ["text", "image"] if image else ["text"],
        "contextWindow": ctx, "maxTokens": out,
        "cost": {"input": cin, "output": cout, "cacheRead": cread if cread is not None else cin / 10,
                 "cacheWrite": cwrite if cwrite is not None else cin * 1.25},
    }


BUILTIN_MODELS: List[Dict[str, Any]] = [
    _m("anthropic", "claude-fable-5-2", "Claude Fable 5.2", 400_000, 64_000, 5, 25),  # limits/prices assumed = Opus 5.5
    _m("anthropic", "claude-opus-5-5", "Claude Opus 5.5", 400_000, 64_000, 5, 25),
    _m("anthropic", "claude-sonnet-5-5", "Claude Sonnet 5.5", 400_000, 64_000, 3, 15),
    _m("anthropic", "claude-haiku-5-5", "Claude Haiku 5.5", 200_000, 32_000, 1, 5),
    _m("anthropic", "claude-sonnet-4-5", "Claude Sonnet 4.5", 200_000, 64_000, 3, 15),
    _m("openai", "gpt-5", "GPT-5", 400_000, 128_000, 1.25, 10),
    _m("openai", "gpt-5-mini", "GPT-5 mini", 400_000, 128_000, 0.25, 2),
    _m("openai", "gpt-4o", "GPT-4o", 128_000, 16_384, 2.5, 10, reasoning=False),
    _m("openai", "gpt-4o-mini", "GPT-4o mini", 128_000, 16_384, 0.15, 0.6, reasoning=False),
    _m("google", "gemini-2.5-pro", "Gemini 2.5 Pro", 1_048_576, 65_536, 1.25, 10),
    _m("google", "gemini-2.5-flash", "Gemini 2.5 Flash", 1_048_576, 65_536, 0.3, 2.5),
    _m("google", "gemini-3-pro-preview", "Gemini 3 Pro", 1_048_576, 65_536, 2, 12),
    _m("google-antigravity", "gemini-2.5-flash", "Gemini 2.5 Flash (Antigravity)", 1_048_576, 65_536, 0, 0),
    _m("google-antigravity", "gemini-3-flash", "Gemini 3 Flash (Antigravity)", 1_048_576, 65_536, 0, 0),
    _m("google-antigravity", "gemini-3-pro", "Gemini 3 Pro (Antigravity)", 1_048_576, 65_536, 0, 0),
    _m("google-antigravity", "gemini-3.8-flash", "Gemini 3.8 Flash (Antigravity)", 1_048_576, 65_536, 0, 0),
    _m("google-antigravity", "gemini-4-argon", "Gemini 4 Argon (Antigravity)", 1_048_576, 65_536, 0, 0),
    _m("google-antigravity", "claude-sonnet-5-5", "Claude Sonnet 5.5 (Antigravity)", 400_000, 64_000, 0, 0),
    _m("google-antigravity", "claude-opus-5-5", "Claude Opus 5.5 (Antigravity)", 400_000, 64_000, 0, 0),
    _m("deepseek", "deepseek-chat", "DeepSeek Chat", 128_000, 8_192, 0.27, 1.1, reasoning=False, image=False),
    _m("deepseek", "deepseek-reasoner", "DeepSeek Reasoner", 128_000, 32_768, 0.55, 2.19, image=False),
    _m("groq", "llama-3.3-70b-versatile", "Llama 3.3 70B (Groq)", 131_072, 32_768, 0.59, 0.79, reasoning=False, image=False),
    _m("xai", "grok-4", "Grok 4", 256_000, 64_000, 3, 15),
    _m("mistral", "mistral-large-latest", "Mistral Large", 128_000, 32_768, 2, 6, reasoning=False),
    _m("openrouter", "anthropic/claude-sonnet-4.5", "Claude Sonnet 4.5 (OpenRouter)", 200_000, 64_000, 3, 15),
    # ChatGPT subscription via the Codex backend (Responses API); billed to the plan, so cost 0
    _m("openai-codex", "gpt-6.1-sol", "GPT-6.1 Sol (Codex)", 400_000, 128_000, 0, 0),
    _m("openai-codex", "gpt-6-sol", "GPT-6 Sol (Codex)", 400_000, 128_000, 0, 0),
    _m("openai-codex", "gpt-6-luna", "GPT-6 Luna (Codex)", 400_000, 128_000, 0, 0),
    _m("openai-codex", "gpt-6-astra", "GPT-6 Astra (Codex)", 400_000, 128_000, 0, 0),
    _m("openai-codex", "gpt-5.6-sol", "GPT-5.6 Sol (Codex)", 400_000, 128_000, 0, 0),
    _m("openai-codex", "gpt-5.6-terra", "GPT-5.6 Terra (Codex)", 400_000, 128_000, 0, 0),
    _m("openai-codex", "gpt-5.6-luna", "GPT-5.6 Luna (Codex)", 400_000, 128_000, 0, 0),
    # GitHub Copilot: chat completions, except `wire: responses` models (Copilot serves those on /responses only)
    _m("github-copilot", "claude-sonnet-5.5", "Claude Sonnet 5.5 (Copilot)", 200_000, 32_000, 0, 0, reasoning=False),
    _m("github-copilot", "claude-opus-5.5", "Claude Opus 5.5 (Copilot)", 200_000, 32_000, 0, 0, reasoning=False),
    _m("github-copilot", "claude-haiku-5.5", "Claude Haiku 5.5 (Copilot)", 200_000, 32_000, 0, 0, reasoning=False),
    _m("github-copilot", "claude-sonnet-4.5", "Claude Sonnet 4.5 (Copilot)", 128_000, 16_000, 0, 0, reasoning=False),
    _m("github-copilot", "gpt-4.1", "GPT-4.1 (Copilot)", 128_000, 16_384, 0, 0, reasoning=False),
    _m("github-copilot", "gpt-5-mini", "GPT-5 mini (Copilot)", 128_000, 64_000, 0, 0, reasoning=False),
    _m("github-copilot", "gemini-2.5-pro", "Gemini 2.5 Pro (Copilot)", 128_000, 64_000, 0, 0, reasoning=False),
    dict(_m("github-copilot", "gpt-5.5", "GPT-5.5 (Copilot)", 272_000, 128_000, 0, 0), wire="responses"),
    dict(_m("github-copilot", "gpt-5.3-codex", "GPT-5.3 Codex (Copilot)", 272_000, 128_000, 0, 0), wire="responses"),
    _m("faux", "faux-1", "Faux (offline test model)", 128_000, 8_192, 0, 0, reasoning=True),
]

# Omni aliases (kept compatible with src/providers/router.py MODEL_ALIASES)
ALIASES = {
    "flash": "google-antigravity/gemini-2.5-flash", "gemini": "google-antigravity/gemini-2.5-flash",
    "3.8": "google-antigravity/gemini-3.8-flash", "pro": "google-antigravity/gemini-3-pro",
    "argon": "google-antigravity/gemini-4-argon", "sonnet": "anthropic/claude-sonnet-5-5",
    "opus": "anthropic/claude-opus-5-5", "fable": "anthropic/claude-fable-5-2", "haiku": "anthropic/claude-haiku-5-5", "4o": "openai/gpt-4o",
    "mini": "openai/gpt-4o-mini", "gpt5": "openai/gpt-5", "deepseek": "deepseek/deepseek-chat",
    "codex": "openai-codex/gpt-6.1-sol", "copilot": "github-copilot/claude-sonnet-5.5",
}


def resolve_config_value(value: Optional[str]) -> Optional[str]:
    """Pi's `$ENV`, `${ENV}`, `!command`, literal semantics for models.json apiKey/header values."""
    if value is None:
        return None
    if value.startswith("!") and not value.startswith("$!"):
        try:
            return subprocess.run(value[1:], shell=True, capture_output=True, text=True, timeout=30).stdout.strip() or None
        except Exception:
            return None
    def sub(m: re.Match) -> str:
        if m.group(0) == "$$":
            return "$"
        if m.group(0) == "$!":
            return "!"
        return os.environ.get(m.group(1) or m.group(2) or "", "")
    out = re.sub(r"\$\$|\$!|\$\{([A-Za-z_][A-Za-z0-9_]*)\}|\$([A-Za-z_][A-Za-z0-9_]*)", sub, value)
    return out


class ModelRegistry:
    """Built-in catalog + `<agent-dir>/models.json` + extension-registered providers."""

    def __init__(self, agent_dir: Path):
        self.agent_dir = Path(agent_dir)
        self.extension_providers: Dict[str, Dict[str, Any]] = {}
        self.virtual: Dict[str, Dict[str, Any]] = {}   # "provider/id" -> virtual model (extension router)
        self.models: List[Dict[str, Any]] = []
        self.provider_overrides: Dict[str, Dict[str, Any]] = {}
        self.errors: List[str] = []
        self.reload()

    # -- loading -----------------------------------------------------------------------------------------
    def reload(self) -> None:
        models = [dict(m) for m in BUILTIN_MODELS]
        self.provider_overrides = {}
        self.errors = []
        path = self.agent_dir / "models.json"
        if path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                models = self._merge_provider_config(models, data.get("providers", {}), source="models.json")
            except Exception as e:  # malformed file must not stop startup (Pi warns too)
                self.errors.append(f"models.json: {e}")
        for name, cfg in self.extension_providers.items():
            models = self._merge_provider_config(models, {name: cfg}, source="extension")
        for key, vm in self.virtual.items():  # a virtual model hides a physical one with the same provider and id
            models = [m for m in models if f"{m['provider']}/{m['id']}" != key] + [vm]
        self.models = models

    def register_virtual(self, d: Dict[str, Any]) -> None:
        if not d.get("provider") or not d.get("id"):
            return
        levels = list(d.get("thinkingLevels") or ["off"])
        vm = {"id": d["id"], "name": d.get("name") or d["id"], "provider": d["provider"], "api": "virtual", "virtual": True,
              "reasoning": levels != ["off"], "thinkingLevels": levels, "input": d.get("input") or ["text", "image"],
              "contextWindow": int(d.get("contextWindow") or 0) or 200_000, "maxTokens": int(d.get("maxTokens") or 0) or 32_000,
              "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0}, "source": "extension"}
        self.virtual[f"{d['provider']}/{d['id']}"] = vm
        self.reload()

    def unregister_virtual(self, provider: str, model_id: str) -> None:
        self.virtual.pop(f"{provider}/{model_id}", None)
        self.reload()

    def _merge_provider_config(self, models, providers: Dict[str, Any], source: str):
        for pname, cfg in providers.items():
            base = PROVIDERS.get(pname, {})
            api = cfg.get("api") or base.get("api") or "openai-completions"
            base_url = cfg.get("baseUrl") or base.get("baseUrl") or ""
            self.provider_overrides[pname] = {**self.provider_overrides.get(pname, {}), **{k: v for k, v in cfg.items() if k != "models"}}
            if cfg.get("baseUrl") and not cfg.get("models"):
                for m in models:
                    if m["provider"] == pname:
                        m["baseUrl"] = base_url
            for raw in cfg.get("models", []) or []:
                mid = raw.get("id")
                if not mid:
                    continue
                m = {
                    "id": mid, "name": raw.get("name", mid), "provider": pname, "api": raw.get("api", api),
                    "baseUrl": raw.get("baseUrl", base_url), "reasoning": bool(raw.get("reasoning", False)),
                    "input": raw.get("input", ["text"]), "contextWindow": int(raw.get("contextWindow", 128_000)),
                    "maxTokens": int(raw.get("maxTokens", 16_384)),
                    "cost": {**{"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0}, **(raw.get("cost") or {})},
                    "headers": raw.get("headers") or cfg.get("headers"), "source": source,
                    "compat": raw.get("compat") or cfg.get("compat"),
                }
                models = [x for x in models if not (x["provider"] == pname and x["id"] == mid)] + [m]
            for mid, ov in (cfg.get("modelOverrides") or {}).items():
                for m in models:
                    if m["provider"] == pname and m["id"] == mid:
                        m.update(ov)
        return models

    def register_provider(self, name: str, config: Dict[str, Any]) -> None:
        self.extension_providers[name] = config
        self.reload()

    def unregister_provider(self, name: str) -> None:
        self.extension_providers.pop(name, None)
        self.reload()

    # -- lookup ------------------------------------------------------------------------------------------
    def all(self) -> List[Dict[str, Any]]:
        return list(self.models)

    def find(self, provider: str, model_id: str) -> Optional[Dict[str, Any]]:
        for m in self.models:
            if m["provider"] == provider and m["id"] == model_id:
                return m
        return None

    def provider_info(self, provider: str) -> Dict[str, Any]:
        info = dict(PROVIDERS.get(provider, {}))
        info.update(self.provider_overrides.get(provider, {}))
        return info

    def resolve(self, pattern: Optional[str], provider: Optional[str] = None) -> Optional[Dict[str, Any]]:
        """Resolve `provider/model`, a bare id, an alias, or a glob (Pi's `--model <pattern>`).

        Unknown `provider/id` pairs for a known provider are accepted with default metadata, so new models work
        before the catalog knows them.
        """
        if not pattern:
            return None
        pat = ALIASES.get(pattern.strip().lower(), pattern.strip())
        level = None
        if ":" in pat and pat.rsplit(":", 1)[1] in THINKING_LEVELS:  # pi accepts model:thinking
            pat, level = pat.rsplit(":", 1)
        if provider and "/" not in pat:
            pat = f"{provider}/{pat}"
        found: Optional[Dict[str, Any]] = None
        if "/" in pat:
            p, mid = pat.split("/", 1)
            found = self.find(p, mid)
            if not found and (p in PROVIDERS or p in self.provider_overrides):
                info = self.provider_info(p)
                found = {"id": mid, "name": mid, "provider": p, "api": info.get("api", "openai-completions"),
                         "baseUrl": info.get("baseUrl", ""), "reasoning": True, "input": ["text", "image"],
                         "contextWindow": 200_000, "maxTokens": 32_000,
                         "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0}}
        if not found:
            exact = [m for m in self.models if m["id"] == pat]
            found = exact[0] if exact else None
        if not found:
            low = pat.lower()
            cands = [m for m in self.models if fnmatch.fnmatch(f"{m['provider']}/{m['id']}".lower(), low)
                     or fnmatch.fnmatch(m["id"].lower(), low) or low in m["id"].lower()]
            found = cands[0] if cands else None
        if found and level:
            found = dict(found, _thinking=level)
        return found


def model_key(model: Optional[Dict[str, Any]]) -> str:
    return f"{model['provider']}/{model['id']}" if model else "(no model)"


def clamp_thinking(model: Optional[Dict[str, Any]], level: str) -> str:
    if model and model.get("virtual"):
        levels = model.get("thinkingLevels") or ["off"]
        return level if level in levels else levels[0]
    if not model or not model.get("reasoning"):
        return "off"
    return level if level in THINKING_LEVELS else "medium"


def calculate_cost(model: Dict[str, Any], usage: Dict[str, Any]) -> Dict[str, float]:
    c = model.get("cost") or {}
    def part(k_usage: str, k_cost: str) -> float:
        return (usage.get(k_usage, 0) or 0) * float(c.get(k_cost, 0) or 0) / 1_000_000
    cost = {"input": part("input", "input"), "output": part("output", "output"),
            "cacheRead": part("cacheRead", "cacheRead"), "cacheWrite": part("cacheWrite", "cacheWrite")}
    cost["total"] = sum(cost.values())
    return cost


def empty_usage() -> Dict[str, Any]:
    return {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0, "totalTokens": 0,
            "cost": {"input": 0.0, "output": 0.0, "cacheRead": 0.0, "cacheWrite": 0.0, "total": 0.0}}
