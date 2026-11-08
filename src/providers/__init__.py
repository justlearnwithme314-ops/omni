"""
Providers module for omni-harness.
Exposes Keyring, UnifiedLLMClient, and ProviderRouter.
"""

from .client import (
    AuthenticationError,
    LLMError,
    ProviderUnavailableError,
    QuotaExceededError,
    RateLimitError,
    UnifiedLLMClient,
    parse_hermes_tool_calls,
)
from .keyring import AuthKeyring, Keyring
from .router import ProviderRouter

__all__ = [
    "Keyring",
    "AuthKeyring",
    "UnifiedLLMClient",
    "ProviderRouter",
    "LLMError",
    "RateLimitError",
    "QuotaExceededError",
    "AuthenticationError",
    "ProviderUnavailableError",
    "parse_hermes_tool_calls",
]
