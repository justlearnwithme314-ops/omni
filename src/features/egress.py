"""
Iron-Proxy Egress Firewall & Credential Masking (Hermes Architecture Standout)
Intercepts outbound HTTP requests to prevent credential exfiltration,
injecting credentials at the network boundary based on domain whitelists.
"""

import os
import re
from typing import Dict, List, Optional, Set, Tuple
import urllib.parse


class EgressProxyFirewall:
    def __init__(self, allowed_domains: Optional[Set[str]] = None):
        self.allowed_domains = allowed_domains or {
            "googleapis.com",
            "anthropic.com",
            "openai.com",
            "github.com",
            "api.github.com",
            "raw.githubusercontent.com",
        }
        self.secret_placeholders: Dict[str, str] = {
            "__IRON_PROXY_GITHUB_TOKEN__": os.environ.get("GITHUB_TOKEN", ""),
            "__IRON_PROXY_ANTHROPIC_KEY__": os.environ.get("ANTHROPIC_API_KEY", ""),
            "__IRON_PROXY_OPENAI_KEY__": os.environ.get("OPENAI_API_KEY", ""),
        }

    def is_domain_allowed(self, url: str) -> bool:
        """Checks if target URL host is on the egress whitelist."""
        try:
            parsed = urllib.parse.urlparse(url)
            host = parsed.netloc.split(":")[0].lower()
            return any(host == d or host.endswith(f".{d}") for d in self.allowed_domains)
        except Exception:
            return False

    def filter_and_inject(self, url: str, headers: Dict[str, str]) -> Tuple[bool, str, Dict[str, str]]:
        """
        Inspects outbound request:
        1. Blocks domains not in whitelist.
        2. Injects real credentials replacing placeholders.
        Returns (is_allowed, reason, modified_headers).
        """
        if not self.is_domain_allowed(url):
            return False, f"Egress Firewall: Outbound request to '{url}' blocked by domain policy.", headers

        modified = dict(headers)
        # Scan header values and replace placeholders with real injected secrets
        for k, v in list(modified.items()):
            for placeholder, real_val in self.secret_placeholders.items():
                if placeholder in v:
                    modified[k] = v.replace(placeholder, real_val)

        return True, "Allowed", modified

    def sanitize_log_data(self, text: str) -> str:
        """Redacts sensitive tokens from logs and outputs."""
        if not text:
            return ""
        # Redact Bearer tokens, sk- keys, ya29. tokens
        sanitized = re.sub(r"Bearer\s+ya29\.[A-Za-z0-9_\-]+", "Bearer ya29.[REDACTED]", text)
        sanitized = re.sub(r"sk-[a-zA-Z0-9_\-]{20,}", "sk-[REDACTED]", sanitized)
        sanitized = re.sub(r"gh[op]_[a-zA-Z0-9]{20,}", "gh_[REDACTED]", sanitized)
        return sanitized
