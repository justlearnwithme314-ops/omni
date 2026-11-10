"""Themes. Token names follow Pi's theme schema so extension renderers (theme.fg("accent", ...)) look right."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Dict, List

BASE_TOKENS = [
    "accent", "accentAlt", "border", "borderAccent", "borderMuted", "success", "error", "warning", "muted", "dim", "text",
    "thinkingText", "selectedBg", "userMessageBg", "userMessageText", "customMessageBg", "customMessageText", "customMessageLabel",
    "toolPendingBg", "toolSuccessBg", "toolErrorBg", "toolTitle", "toolOutput", "mdHeading", "mdLink", "mdLinkUrl", "mdCode",
    "mdCodeBlock", "mdCodeBlockBorder", "mdQuote", "mdQuoteBorder", "mdHr", "mdListBullet", "toolDiffAdded", "toolDiffRemoved",
    "toolDiffContext", "syntaxComment", "syntaxKeyword", "syntaxFunction", "syntaxVariable", "syntaxString", "syntaxNumber",
    "syntaxType", "syntaxOperator", "syntaxPunctuation", "thinkingOff", "thinkingMinimal", "thinkingLow", "thinkingMedium",
    "thinkingHigh", "thinkingXhigh", "bashMode", "panelBg", "headerBg", "footerBg", "editorBg",
]

THEMES: Dict[str, Dict[str, str]] = {
    "omni": {  # deep indigo night with an aurora accent
        "accent": "#7aa2ff", "accentAlt": "#c792ea", "border": "#3b4261", "borderAccent": "#7aa2ff", "borderMuted": "#2a2f45",
        "success": "#9ece6a", "error": "#ff6b81", "warning": "#e0af68", "muted": "#8089b3", "dim": "#565f89", "text": "#d5d9f0",
        "thinkingText": "#8a90b8", "selectedBg": "#2d3352", "userMessageBg": "#1c2033", "userMessageText": "#e6e9ff",
        "customMessageBg": "#1f1b2e", "customMessageText": "#d6c8ff", "customMessageLabel": "#c792ea",
        "toolPendingBg": "#181c2b", "toolSuccessBg": "#151c1c", "toolErrorBg": "#221519", "toolTitle": "#a9b8ff",
        "toolOutput": "#a8aecf", "mdHeading": "#89ddff", "mdLink": "#7aa2ff", "mdLinkUrl": "#565f89", "mdCode": "#f7c97e",
        "mdCodeBlock": "#c0caf5", "mdCodeBlockBorder": "#2f3550", "mdQuote": "#9aa5ce", "mdQuoteBorder": "#4a5175",
        "mdHr": "#3b4261", "mdListBullet": "#7dcfff", "toolDiffAdded": "#9ece6a", "toolDiffRemoved": "#ff6b81",
        "toolDiffContext": "#7a80a8", "syntaxComment": "#5f6896", "syntaxKeyword": "#bb9af7", "syntaxFunction": "#7aa2ff",
        "syntaxVariable": "#c0caf5", "syntaxString": "#9ece6a", "syntaxNumber": "#ff9e64", "syntaxType": "#2ac3de",
        "syntaxOperator": "#89ddff", "syntaxPunctuation": "#9aa5ce", "thinkingOff": "#3b4261", "thinkingMinimal": "#4f6a8f",
        "thinkingLow": "#5a8fd0", "thinkingMedium": "#7aa2ff", "thinkingHigh": "#c792ea", "thinkingXhigh": "#ff79c6",
        "bashMode": "#e0af68", "panelBg": "#161a28", "headerBg": "#111421", "footerBg": "#111421", "editorBg": "#13172a",
    },
    "tokyo-night": {
        "accent": "#7aa2f7", "accentAlt": "#bb9af7", "border": "#3b4261", "borderAccent": "#7aa2f7", "borderMuted": "#292e42",
        "success": "#9ece6a", "error": "#f7768e", "warning": "#e0af68", "muted": "#737aa2", "dim": "#565f89", "text": "#c0caf5",
        "thinkingText": "#7982a9", "selectedBg": "#283457", "userMessageBg": "#1f2335", "userMessageText": "#c0caf5",
        "customMessageBg": "#1f2335", "customMessageText": "#bb9af7", "customMessageLabel": "#bb9af7", "toolPendingBg": "#1a1b26",
        "toolSuccessBg": "#1a1b26", "toolErrorBg": "#2d1f27", "toolTitle": "#7dcfff", "toolOutput": "#a9b1d6",
        "mdHeading": "#7dcfff", "mdLink": "#7aa2f7", "mdLinkUrl": "#565f89", "mdCode": "#e0af68", "mdCodeBlock": "#c0caf5",
        "mdCodeBlockBorder": "#3b4261", "mdQuote": "#9aa5ce", "mdQuoteBorder": "#565f89", "mdHr": "#3b4261",
        "mdListBullet": "#7dcfff", "toolDiffAdded": "#9ece6a", "toolDiffRemoved": "#f7768e", "toolDiffContext": "#737aa2",
        "syntaxComment": "#565f89", "syntaxKeyword": "#bb9af7", "syntaxFunction": "#7aa2f7", "syntaxVariable": "#c0caf5",
        "syntaxString": "#9ece6a", "syntaxNumber": "#ff9e64", "syntaxType": "#2ac3de", "syntaxOperator": "#89ddff",
        "syntaxPunctuation": "#a9b1d6", "thinkingOff": "#3b4261", "thinkingMinimal": "#565f89", "thinkingLow": "#7aa2f7",
        "thinkingMedium": "#7dcfff", "thinkingHigh": "#bb9af7", "thinkingXhigh": "#f7768e", "bashMode": "#e0af68",
        "panelBg": "#1a1b26", "headerBg": "#16161e", "footerBg": "#16161e", "editorBg": "#1a1b26",
    },
    "catppuccin": {
        "accent": "#89b4fa", "accentAlt": "#cba6f7", "border": "#45475a", "borderAccent": "#89b4fa", "borderMuted": "#313244",
        "success": "#a6e3a1", "error": "#f38ba8", "warning": "#f9e2af", "muted": "#9399b2", "dim": "#6c7086", "text": "#cdd6f4",
        "thinkingText": "#a6adc8", "selectedBg": "#313244", "userMessageBg": "#24273a", "userMessageText": "#cdd6f4",
        "customMessageBg": "#2a2438", "customMessageText": "#f5c2e7", "customMessageLabel": "#cba6f7", "toolPendingBg": "#1e1e2e",
        "toolSuccessBg": "#1e1e2e", "toolErrorBg": "#2c1f27", "toolTitle": "#89dceb", "toolOutput": "#bac2de",
        "mdHeading": "#89dceb", "mdLink": "#89b4fa", "mdLinkUrl": "#6c7086", "mdCode": "#fab387", "mdCodeBlock": "#cdd6f4",
        "mdCodeBlockBorder": "#45475a", "mdQuote": "#a6adc8", "mdQuoteBorder": "#585b70", "mdHr": "#45475a",
        "mdListBullet": "#94e2d5", "toolDiffAdded": "#a6e3a1", "toolDiffRemoved": "#f38ba8", "toolDiffContext": "#7f849c",
        "syntaxComment": "#6c7086", "syntaxKeyword": "#cba6f7", "syntaxFunction": "#89b4fa", "syntaxVariable": "#cdd6f4",
        "syntaxString": "#a6e3a1", "syntaxNumber": "#fab387", "syntaxType": "#f9e2af", "syntaxOperator": "#89dceb",
        "syntaxPunctuation": "#bac2de", "thinkingOff": "#45475a", "thinkingMinimal": "#6c7086", "thinkingLow": "#74c7ec",
        "thinkingMedium": "#89b4fa", "thinkingHigh": "#cba6f7", "thinkingXhigh": "#f5c2e7", "bashMode": "#f9e2af",
        "panelBg": "#1e1e2e", "headerBg": "#181825", "footerBg": "#181825", "editorBg": "#1e1e2e",
    },
    "light": {
        "accent": "#3b5bdb", "accentAlt": "#9c36b5", "border": "#c5cae0", "borderAccent": "#3b5bdb", "borderMuted": "#dde1ef",
        "success": "#2b8a3e", "error": "#c92a2a", "warning": "#e67700", "muted": "#6b7194", "dim": "#9aa0bd", "text": "#1f2335",
        "thinkingText": "#6b7194", "selectedBg": "#e3e8ff", "userMessageBg": "#eef1fb", "userMessageText": "#1f2335",
        "customMessageBg": "#f6f0fb", "customMessageText": "#5f3dc4", "customMessageLabel": "#9c36b5", "toolPendingBg": "#f5f6fb",
        "toolSuccessBg": "#f2f8f3", "toolErrorBg": "#fbf0f0", "toolTitle": "#364fc7", "toolOutput": "#41466b",
        "mdHeading": "#1864ab", "mdLink": "#3b5bdb", "mdLinkUrl": "#9aa0bd", "mdCode": "#c2255c", "mdCodeBlock": "#1f2335",
        "mdCodeBlockBorder": "#dde1ef", "mdQuote": "#495057", "mdQuoteBorder": "#c5cae0", "mdHr": "#dde1ef",
        "mdListBullet": "#1098ad", "toolDiffAdded": "#2b8a3e", "toolDiffRemoved": "#c92a2a", "toolDiffContext": "#868e96",
        "syntaxComment": "#868e96", "syntaxKeyword": "#9c36b5", "syntaxFunction": "#1864ab", "syntaxVariable": "#1f2335",
        "syntaxString": "#2b8a3e", "syntaxNumber": "#d9480f", "syntaxType": "#0b7285", "syntaxOperator": "#495057",
        "syntaxPunctuation": "#495057", "thinkingOff": "#c5cae0", "thinkingMinimal": "#9aa0bd", "thinkingLow": "#4dabf7",
        "thinkingMedium": "#3b5bdb", "thinkingHigh": "#9c36b5", "thinkingXhigh": "#c2255c", "bashMode": "#e67700",
        "panelBg": "#f8f9fd", "headerBg": "#eef1fb", "footerBg": "#eef1fb", "editorBg": "#ffffff",
    },
}
THEMES["dark"] = THEMES["omni"]
THEMES["system"] = THEMES["omni"]
BRAND_GRADIENT = ["#7aa2ff", "#a58cff", "#e27df5", "#ff9fb2"]


def load_custom_themes(dirs: List[Path]) -> None:
    """Pi theme JSON files ({name, colors: {...}} or flat) dropped into themes/ directories."""
    for d in dirs:
        d = Path(d)
        files = [d] if d.is_file() else sorted(d.glob("*.json")) if d.is_dir() else []
        for p in files:
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
                colors = data.get("colors") or data.get("vars") or data
                base = dict(THEMES["omni"])
                base.update({k: v for k, v in colors.items() if isinstance(v, str) and v.startswith("#")})
                THEMES[data.get("name") or p.stem] = base
            except Exception:
                continue


def palette_for(name: str) -> Dict[str, str]:
    return dict(THEMES.get(name) or THEMES["omni"])


def thinking_color(pal: Dict[str, str], level: str) -> str:
    return pal.get({"off": "thinkingOff", "minimal": "thinkingMinimal", "low": "thinkingLow", "medium": "thinkingMedium",
                    "high": "thinkingHigh", "xhigh": "thinkingXhigh", "max": "thinkingXhigh"}.get(level, "border"), pal["border"])
