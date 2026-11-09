"""
Provider Router managing model tiers, provider mappings, key rotation on
rate limit / quota exhaustion, and tier cascades.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

from .client import (
    LLMError,
    QuotaExceededError,
    RateLimitError,
    UnifiedLLMClient,
)
from .keyring import Keyring
from ..paths import PROJECT_ROOT

logger = logging.getLogger(__name__)

DEFAULT_CONFIG_PATH = str(PROJECT_ROOT / "harness.config.json")

DEFAULT_TIERS: dict[str, list[str]] = {
    "fast": [
        "google/gemini-3.8-flash",
        "openai/gpt-4o-mini",
        "anthropic/claude-3-5-haiku",
    ],
    "smart": [
        "anthropic/claude-3-5-sonnet",
        "openai/gpt-4o",
        "google/gemini-3-pro",
    ],
}



MODEL_ALIASES: dict[str, str] = {
    "flash": "google-antigravity/gemini-2.5-flash",
    "gemini": "google-antigravity/gemini-2.5-flash",
    "gemini-2.5-flash": "google-antigravity/gemini-2.5-flash",
    "gemini-3.8-flash": "google-antigravity/gemini-3.8-flash",
    "flash-3.8": "google-antigravity/gemini-3.8-flash",
    "3.8": "google-antigravity/gemini-3.8-flash",
    "flash-3": "google-antigravity/gemini-3-flash",
    "gemini-3-flash": "google-antigravity/gemini-3-flash",
    "pro": "google-antigravity/gemini-3-pro",
    "gemini-3-pro": "google-antigravity/gemini-3-pro",
    "argon": "google-antigravity/gemini-4-argon",
    "gemini-4-argon": "google-antigravity/gemini-4-argon",
    "sonnet": "google-antigravity/claude-sonnet-5-5",
    "claude": "google-antigravity/claude-sonnet-5-5",
    "claude-sonnet": "google-antigravity/claude-sonnet-5-5",
    "claude-sonnet-5-5": "google-antigravity/claude-sonnet-5-5",
    "opus": "google-antigravity/claude-opus-5-5",
    "claude-opus": "google-antigravity/claude-opus-5-5",
    "claude-opus-5-5": "google-antigravity/claude-opus-5-5",
    "gpt-4o": "openai/gpt-4o",
    "4o": "openai/gpt-4o",
    "mini": "openai/gpt-4o-mini",
    "gpt-4o-mini": "openai/gpt-4o-mini",
    "deepseek": "deepseek/deepseek-chat",
    "haiku": "anthropic/claude-3-5-haiku",
}

class ProviderRouter:
    """
    Intelligent routing and key management layer:
    - Resolves model tiers ('fast', 'smart') to candidate model cascades.
    - Maps model names to providers (e.g. google, openai, anthropic, local).
    - Retrieves active unblocked keys from Keyring.
    - Transparently rotates keys on 429 RateLimit (60s cooldown) or Quota exhaustion (3600s cooldown).
    - Cascades to next candidate model if all keys for a model fail.
    - Normalizes response dict: {"content": str, "tool_calls": list[dict], "model": str, "provider": str, "usage": dict}.
    """

    def __init__(
        self,
        keyring: Keyring | None = None,
        client: UnifiedLLMClient | None = None,
        tiers: dict[str, list[str]] | None = None,
        config_path: str | Path | None = None,
        allow_mock_fallback: bool = True,
        config: Any = None,
    ) -> None:
        self.keyring = keyring or Keyring()
        self.client = client or UnifiedLLMClient()
        self.allow_mock_fallback = allow_mock_fallback
        self.config = config

        if tiers is not None:
            self.tiers = tiers
        elif config is not None and hasattr(config, "model_tiers"):
            self.tiers = config.model_tiers
        else:
            self.tiers = self._load_tiers_from_config(config_path or DEFAULT_CONFIG_PATH)

    def _load_tiers_from_config(self, config_path: str | Path) -> dict[str, list[str]]:
        """Load model tiers from harness.config.json if available, or use defaults."""
        path = Path(config_path)
        if path.exists():
            try:
                with open(path, "r", encoding="utf-8") as f:
                    cfg = json.load(f)
                    tiers = cfg.get("model_tiers")
                    if isinstance(tiers, dict) and tiers:
                        logger.info("Loaded model tiers from config: %s", path)
                        return tiers
            except Exception as e:
                logger.warning("Failed loading tiers from %s (%s). Using defaults.", path, e)
        return DEFAULT_TIERS

    @classmethod
    def resolve_model(cls, model_str: str) -> str:
        clean = (model_str or "").strip().lower()
        return MODEL_ALIASES.get(clean, model_str)

    @staticmethod
    def parse_model(model_str: str) -> tuple[str, str]:
        """
        Map model identifier to (provider, model_name).
        Examples:
            'google/gemini-3.8-flash' -> ('google', 'gemini-3.8-flash')
            'openai/gpt-4o'           -> ('openai', 'gpt-4o')
            'anthropic/claude-3-5-sonnet' -> ('anthropic', 'claude-3-5-sonnet')
            'local/model'             -> ('local', 'model')
            'gpt-4o'                  -> ('openai', 'gpt-4o')
        """
        model_str = model_str.strip()
        if "/" in model_str:
            provider, model_name = model_str.split("/", 1)
            return provider.strip().lower(), model_name.strip()

        # Heuristic inference if no provider prefix is supplied
        lower = model_str.lower()
        if "claude" in lower:
            return "anthropic", model_str
        elif "gemini" in lower:
            return "google", model_str
        elif any(k in lower for k in ("llama", "ollama", "local", "mistral")):
            return "local", model_str
        else:
            return "openai", model_str

    async def complete(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        tier: str = "fast",
        model_override: str | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """
        Execute completion with automatic key rotation and model fallback.

        Args:
            messages: List of message dictionaries.
            tools: Optional list of tool specifications.
            tier: Model tier to resolve ('fast', 'smart', etc.).
            model_override: Specific candidate model string to override tier resolution.

        Returns:
            Normalized response dict:
            {"content": str, "tool_calls": list[dict], "model": str, "provider": str, "usage": dict}
        """
        model_kwarg = kwargs.pop("model", None)
        try:
            from .oauth import OAuthManager
            OAuthManager(self.keyring).sync_if_expired()
        except Exception:
            pass
        if model_kwarg and not model_override:
            model_override = model_kwarg
        if model_override:
            model_override = self.resolve_model(model_override)
        # Step 1: Resolve candidate models
        if model_override:
            candidates = [model_override]
        else:
            candidates = self.tiers.get(tier, self.tiers.get("fast", []))

        if not candidates:
            candidates = ["openai/gpt-4o-mini"]

        tier_errors: list[str] = []

        # Step 2: Iterate through candidate models
        for candidate_model in candidates:
            provider, model_name = self.parse_model(candidate_model)

            endpoint_info = self.keyring.get_endpoint(provider)
            base_url = endpoint_info.get("base_url") if endpoint_info else None

            # Retrieve active unblocked keys for candidate's provider
            keys = self.keyring.get_active_unblocked_keys(provider)
            if not keys and provider == "google":
                alt_keys = self.keyring.get_active_unblocked_keys("google-antigravity")
                if alt_keys:
                    provider = "google-antigravity"
                    keys = alt_keys
                    base_url = "https://daily-cloudcode-pa.googleapis.com"


            if not keys:
                if self.allow_mock_fallback:
                    keys = [
                        {
                            "key_id": f"mock_{provider}",
                            "api_key": "mock",
                            "provider": provider,
                        }
                    ]
                else:
                    msg = f"No active unblocked keys found for provider '{provider}' ({candidate_model})"
                    logger.warning("%s. Cascading to next candidate...", msg)
                    tier_errors.append(msg)
                    continue

            # Step 3: Iterate through keys pool for this candidate model
            for key_data in keys:
                key_id = str(key_data.get("key_id", "unknown"))
                api_key = str(key_data.get("api_key", ""))

                try:
                    logger.debug(
                        "Attempting completion on %s with provider '%s' key '%s'",
                        candidate_model,
                        provider,
                        key_id,
                    )
                    resp = await self.client.complete(
                        messages=messages,
                        tools=tools,
                        provider=provider,
                        model=model_name,
                        api_key=api_key,
                        base_url=base_url,
                        **kwargs,
                    )

                    # Ensure standard output normalization
                    return {
                        "content": resp.get("content", ""),
                        "tool_calls": resp.get("tool_calls", []),
                        "model": candidate_model,
                        "provider": provider,
                        "usage": resp.get(
                            "usage",
                            {
                                "prompt_tokens": 0,
                                "completion_tokens": 0,
                                "total_tokens": 0,
                            },
                        ),
                    }

                except RateLimitError as e:
                    cooldown = 60.0
                    logger.warning(
                        "Rate limit (HTTP 429) on %s for provider '%s' (key_id: %s). "
                        "Blocking key for %.0fs and rotating to next key. Error: %s",
                        candidate_model,
                        provider,
                        key_id,
                        cooldown,
                        e,
                    )
                    self.keyring.block_key(
                        provider,
                        key_id,
                        cooldown_seconds=cooldown,
                        reason="rate_limit",
                    )
                    tier_errors.append(f"{candidate_model}[{key_id}]: RateLimit - {e}")
                    continue

                except QuotaExceededError as e:
                    cooldown = 3600.0
                    logger.warning(
                        "Quota exhausted on %s for provider '%s' (key_id: %s). "
                        "Blocking key for %.0fs and rotating to next key. Error: %s",
                        candidate_model,
                        provider,
                        key_id,
                        cooldown,
                        e,
                    )
                    self.keyring.block_key(
                        provider,
                        key_id,
                        cooldown_seconds=cooldown,
                        reason="quota_exceeded",
                    )
                    tier_errors.append(f"{candidate_model}[{key_id}]: QuotaExceeded - {e}")
                    continue

                except Exception as e:
                    err_msg = str(e).lower()
                    if "429" in err_msg or ("rate" in err_msg and "limit" in err_msg):
                        cooldown = 60.0
                        logger.warning(
                            "Detected 429/RateLimit in error for provider '%s' key '%s'. "
                            "Blocking key for %.0fs and rotating. Error: %s",
                            provider,
                            key_id,
                            cooldown,
                            e,
                        )
                        self.keyring.block_key(
                            provider,
                            key_id,
                            cooldown_seconds=cooldown,
                            reason="rate_limit",
                        )
                    elif "quota" in err_msg or "credit" in err_msg or "402" in err_msg:
                        cooldown = 3600.0
                        logger.warning(
                            "Detected quota exhaustion in error for provider '%s' key '%s'. "
                            "Blocking key for %.0fs and rotating. Error: %s",
                            provider,
                            key_id,
                            cooldown,
                            e,
                        )
                        self.keyring.block_key(
                            provider,
                            key_id,
                            cooldown_seconds=cooldown,
                            reason="quota_exceeded",
                        )
                    else:
                        logger.warning(
                            "LLM call failure on %s with provider '%s' key '%s': %s. Rotating key...",
                            candidate_model,
                            provider,
                            key_id,
                            e,
                        )

                    tier_errors.append(f"{candidate_model}[{key_id}]: {e}")
                    continue

            logger.warning(
                "All keys for candidate model '%s' exhausted or failed. Cascading to next candidate in tier '%s'...",
                candidate_model,
                tier,
            )

        # Step 4: Cascade exhausted across all candidates in tier
        if self.allow_mock_fallback:
            fallback_model = candidates[0] if candidates else "mock/fast"
            fb_provider, fb_name = self.parse_model(fallback_model)
            logger.warning(
                "All candidates in tier '%s' failed. Generating deterministic mock response with '%s'.",
                tier,
                fallback_model,
            )
            mock_res = await self.client.complete(
                messages=messages,
                tools=tools,
                provider=fb_provider,
                model=fb_name,
                api_key="mock",
            )
            mock_res["model"] = fallback_model
            mock_res["provider"] = fb_provider
            return mock_res

        raise RuntimeError(
            f"All candidate models in tier '{tier}' failed. "
            f"Candidates attempted: {candidates}. Errors: {'; '.join(tier_errors)}"
        )
