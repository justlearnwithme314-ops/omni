"""
Mental Models Repository (/mm - OMP mnemopi Standout)
Stores per-project architectural invariant cards in .omni/mental_models/
and injects active engineering rules into prompt context.
"""

from dataclasses import dataclass
import os
from pathlib import Path
import time
from typing import Dict, List, Optional
from ..paths import workspace_root as _workspace_root


@dataclass
class MentalModel:
    slug: str
    title: str
    content: str
    created_at: float


class MentalModelRepository:
    """Manages persistent project mental model rule cards."""

    def __init__(self, workspace_root: Optional[str] = None):
        self.workspace_root = Path(workspace_root or _workspace_root()).resolve()
        self.models_dir = self.workspace_root / ".omni" / "mental_models"
        self._init_repo()

    def _init_repo(self):
        self.models_dir.mkdir(parents=True, exist_ok=True)
        # Create seed model if empty
        seed_file = self.models_dir / "architecture-invariants.md"
        if not seed_file.exists():
            seed_content = (
                "# Architecture Invariants\n\n"
                "- Always separate business logic from I/O and network transports.\n"
                "- Keep public APIs backward compatible.\n"
                "- Use try/except blocks with explicit error recovery.\n"
            )
            seed_file.write_text(seed_content, encoding="utf-8")

    def add_model(self, slug: str, title: str, content: str) -> str:
        """Saves a new mental model card."""
        clean_slug = slug.lower().strip().replace(" ", "-")
        target_file = self.models_dir / f"{clean_slug}.md"
        doc = f"# {title}\n\n{content.strip()}\n"
        target_file.write_text(doc, encoding="utf-8")
        return str(target_file)

    def list_models(self) -> List[Dict[str, str]]:
        """Lists all mental model cards in the workspace."""
        models = []
        if not self.models_dir.exists():
            return models
        for f in sorted(self.models_dir.glob("*.md")):
            try:
                text = f.read_text(encoding="utf-8", errors="replace")
                first_line = text.splitlines()[0] if text.splitlines() else f.stem
                title = first_line.lstrip("#").strip()
                models.append({
                    "slug": f.stem,
                    "title": title,
                    "path": str(f.relative_to(self.workspace_root)),
                    "preview": text[:150],
                })
            except Exception:
                pass
        return models

    def get_rules_for_prompt(self) -> str:
        """Assembles all active mental models into a prompt instruction block."""
        models = self.list_models()
        if not models:
            return ""
        blocks = []
        for m in models:
            card_p = self.workspace_root / m["path"]
            if card_p.exists():
                blocks.append(card_p.read_text(encoding="utf-8", errors="replace").strip())
        return "\n\n--- Active Project Mental Models ---\n" + "\n\n".join(blocks) + "\n--- End Mental Models ---\n"
