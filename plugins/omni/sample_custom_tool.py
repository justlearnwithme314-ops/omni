"""
Sample native Omni plugin showing custom tool creation with @omni_tool and def register(omni).
Drop any Python file directly into plugins/omni/ and it immediately becomes an active tool!
"""

import platform
import sys
from src.adapters.omni import omni_tool


@omni_tool(
    name="system_diagnostic",
    description="Returns current operating system, Python version, and engine status diagnostic.",
)
def system_diagnostic() -> str:
    return (
        f"OS: {platform.system()} {platform.release()} | "
        f"Python: {sys.version.split()[0]} | "
        f"Omni Engine: ACTIVE"
    )


def register(omni):
    omni.register_command(
        "ping",
        lambda args: print("[pong] Omni custom plugin is alive and responding!"),
    )
