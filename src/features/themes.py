"""
Truecolor Terminal Themes & Skins (Hermes & OMP Style)
Pre-packaged truecolor themes: Tokyo Night, Catppuccin Mocha, Cyberpunk Matrix, Nord, Solarized.
Persists selection and provides Rich theme integration.
"""

from dataclasses import dataclass
import json
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

try:
    from rich.theme import Theme as RichTheme
    HAVE_RICH = True
except ImportError:
    HAVE_RICH = False


@dataclass
class Theme:
    name: str
    description: str
    primary: str       # Main accent (prompts, titles)
    secondary: str     # Supporting details
    accent: str        # Highlights
    success: str       # Success indicators, tool results
    warning: str       # Warnings, tool calls
    error: str         # Errors, denials
    text: str          # Regular text
    panel_border: str  # Card/Panel borders


THEMES: Dict[str, Theme] = {
    "tokyo-night": Theme(
        name="tokyo-night",
        description="Tokyo Night: Vibrant neon blues and deep purples inspired by nighttime Tokyo.",
        primary="#7dcfff",
        secondary="#bb9af7",
        accent="#7aa2f7",
        success="#9ece6a",
        warning="#e0af68",
        error="#f7768e",
        text="#c0caf5",
        panel_border="#7aa2f7",
    ),
    "catppuccin": Theme(
        name="catppuccin",
        description="Catppuccin Mocha: Soothing, warm pastel palette with rich contrast.",
        primary="#cba6f7",
        secondary="#f5c2e7",
        accent="#89b4fa",
        success="#a6e3a1",
        warning="#fab387",
        error="#f38ba8",
        text="#cdd6f4",
        panel_border="#cba6f7",
    ),
    "matrix": Theme(
        name="matrix",
        description="Cyberpunk Matrix: High-contrast phosphor green and neon cyber terminal aesthetics.",
        primary="#00ff41",
        secondary="#008f11",
        accent="#00ff66",
        success="#00ff41",
        warning="#ffb000",
        error="#ff0033",
        text="#00ff41",
        panel_border="#00ff41",
    ),
    "nord": Theme(
        name="nord",
        description="Nord: Arctic, north-bluish clean and minimal aesthetic.",
        primary="#88c0d0",
        secondary="#81a1c1",
        accent="#5e81ac",
        success="#a3be8c",
        warning="#ebcb8b",
        error="#bf616a",
        text="#d8dee9",
        panel_border="#88c0d0",
    ),
    "solarized": Theme(
        name="solarized",
        description="Solarized Dark: Precision warm cyan, blue, and gold contrast.",
        primary="#2aa198",
        secondary="#268bd2",
        accent="#6c71c4",
        success="#859900",
        warning="#b58900",
        error="#dc322f",
        text="#93a1a1",
        panel_border="#2aa198",
    ),
}


class ThemeManager:
    def __init__(self, config_path: Optional[str] = None):
        self.config_path = config_path or os.path.join(
            os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
            "omni.config.json",
        )
        self.active_theme_name = self._load_saved_theme()

    def _load_saved_theme(self) -> str:
        cfg = Path(self.config_path)
        if cfg.exists():
            try:
                with open(cfg, "r", encoding="utf-8") as f:
                    data = json.load(f)
                theme = data.get("active_skin") or data.get("theme")
                if theme and theme.lower() in THEMES:
                    return theme.lower()
            except Exception:
                pass
        return "tokyo-night"

    def get_theme(self, name: Optional[str] = None) -> Theme:
        key = (name or self.active_theme_name).lower()
        return THEMES.get(key, THEMES["tokyo-night"])

    def set_theme(self, name: str) -> bool:
        key = name.lower().strip()
        if key not in THEMES:
            return False
        self.active_theme_name = key
        # Save to config
        cfg = Path(self.config_path)
        try:
            data = {}
            if cfg.exists():
                with open(cfg, "r", encoding="utf-8") as f:
                    data = json.load(f)
            data["active_skin"] = key
            with open(cfg, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
        except Exception:
            pass
        return True

    def list_themes(self) -> List[Dict[str, Any]]:
        return [
            {
                "name": t.name,
                "description": t.description,
                "active": t.name == self.active_theme_name,
                "primary": t.primary,
            }
            for t in THEMES.values()
        ]

    def get_rich_theme(self, name: Optional[str] = None) -> Any:
        if not HAVE_RICH:
            return None
        t = self.get_theme(name)
        return RichTheme({
            "primary": t.primary,
            "secondary": t.secondary,
            "accent": t.accent,
            "success": t.success,
            "warning": t.warning,
            "error": t.error,
            "text": t.text,
            "panel": t.panel_border,
        })
