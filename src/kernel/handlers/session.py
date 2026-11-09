"""Basic session commands: /new /clear /reset."""
import os
from . import HandlerContext, command


@command("new", "reset", "fresh")
def new_session(arg: str, ctx: HandlerContext) -> str:
    st = ctx.state
    st.messages.clear()
    for attr in ("turn_index", "tool_calls"):
        if hasattr(st, attr):
            setattr(st, attr, 0 if isinstance(getattr(st, attr), int) else [])
    return "[✓] Started a fresh session (history cleared)."


@command("clear")
def clear(arg: str, ctx: HandlerContext) -> str:
    os.system("cls" if os.name == "nt" else "clear")
    return ""
