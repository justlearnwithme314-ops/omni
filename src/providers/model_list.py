"""Fetch the real model ids a logged-in provider offers, and validate/auto-correct a user-typed model."""
import difflib
import json
import urllib.request
from typing import List, Optional, Tuple

from .auth_plugin import load_providers
from .keyring import Keyring

_BASES = {"openai": "https://api.openai.com/v1", "anthropic": "https://api.anthropic.com/v1",
          "google": "https://generativelanguage.googleapis.com/v1beta"}


def fetch_model_ids(provider: str, kr: Optional[Keyring] = None) -> List[str]:
    kr = kr or Keyring()
    keys = kr.get_active_unblocked_keys(provider)
    if not keys:
        raise LookupError(f"No key for '{provider}'. Use /login first.")
    key = keys[0].get("api_key") or keys[0].get("key")
    base = load_providers().get(provider, {}).get("base_url") or _BASES.get(provider)
    if not base:
        raise LookupError(f"Don't know the models endpoint for '{provider}'.")
    h, url = {"User-Agent": "omni"}, base + "/models"
    if provider == "anthropic":
        h["anthropic-version"] = "2023-06-01"
        h.update({"Authorization": f"Bearer {key}", "anthropic-beta": "oauth-2025-04-20"} if key.startswith("sk-ant-oat") else {"x-api-key": key})
    elif provider == "google":
        url += f"?key={key}"
    else:
        h["Authorization"] = f"Bearer {key}"
    with urllib.request.urlopen(urllib.request.Request(url, headers=h), timeout=20) as r:
        data = json.loads(r.read().decode())
    rows = data.get("data") or data.get("models") or []
    return sorted((m.get("id") or m.get("name", "")).replace("models/", "") for m in rows)


def validate(provider: str, name: str, kr: Optional[Keyring] = None) -> Tuple[Optional[str], str]:
    """Return (model_name_to_use or None, message). Auto-corrects '5.5' -> '5-5' style typos."""
    try:
        ids = fetch_model_ids(provider, kr)
    except Exception as e:  # cannot verify (offline / no endpoint): let it through
        return name, f"(could not verify against {provider}: {e})"
    if name in ids:
        return name, ""
    for cand in (name.replace(".", "-"), name.replace("_", "-")):
        if cand in ids:
            return cand, f"(corrected '{name}' to '{cand}')"
    close = difflib.get_close_matches(name, ids, n=4, cutoff=0.5)
    return None, f"[!] {provider} has no model '{name}'." + (f" Did you mean: {', '.join(close)}?" if close else " Use /models " + provider)
