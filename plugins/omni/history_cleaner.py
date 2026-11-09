"""
History Cleaner Plugin for Omni.
Provides tools and interactive commands to clear user prompt input history and manage auto-suggestions.
"""

import os
from pathlib import Path
from src.adapters.omni import omni_tool


@omni_tool(
    name="clear_input_history",
    description="Clears the user's terminal input history file (~/.omni_history), wiping past prompts and ghost auto-suggestions.",
    parameters={
        "type": "object",
        "properties": {
            "keep_last": {
                "type": "integer",
                "description": "Number of recent history lines to preserve, or 0 to wipe all history completely.",
                "default": 0,
            }
        },
    },
)
def clear_input_history(keep_last: int = 0) -> str:
    """Clears ~/.omni_history and resets prompt suggestions."""
    hist_path = Path.home() / ".omni_history"
    if not hist_path.exists():
        return "[i] Input history is already empty (no ~/.omni_history file found)."

    try:
        with open(hist_path, "r", encoding="utf-8", errors="ignore") as f:
            lines = f.readlines()

        total = len(lines)
        if keep_last <= 0:
            with open(hist_path, "w", encoding="utf-8") as f:
                f.truncate(0)
            return (
                f"[✓] Successfully wiped all {total} entries from input history (~/.omni_history).\n"
                "[✓] Ghost auto-suggestions have been completely reset."
            )
        else:
            preserved = lines[-keep_last:]
            with open(hist_path, "w", encoding="utf-8") as f:
                f.writelines(preserved)
            return (
                f"[✓] Preserved last {len(preserved)} entries; removed {total - len(preserved)} older entries from ~/.omni_history.\n"
                "[✓] Ghost auto-suggestions updated."
            )
    except Exception as e:
        return f"[!] Failed to clear history file: {e}"
