"""
Split-Screen Dual Pane Layout (OMP pi-tui Style)
Renders a split view:
Left pane: Live conversation transcript and tool execution logs.
Right pane: Real-time file diff viewer (rich syntax + difflib) and active todo checklist.
"""

from typing import Any, Dict, List, Optional

try:
    from rich.box import ROUNDED
    from rich.console import Console
    from rich.layout import Layout
    from rich.panel import Panel
    from rich.syntax import Syntax
    from rich.table import Table
    from rich.text import Text
    HAVE_RICH = True
except ImportError:
    HAVE_RICH = False


class DualPaneLayout:
    """Manages full-terminal split dual-pane layout rendering."""

    def __init__(self, theme_name: str = "tokyo-night"):
        self.theme_name = theme_name
        self.console = Console() if HAVE_RICH else None

    def render(
        self,
        conversation_text: str,
        diff_text: str = "",
        todos: Optional[List[Dict[str, Any]]] = None,
        title: str = "Omni Split View (pi-tui style)",
        subagents: Optional[Dict[str, Any]] = None,
    ) -> Optional[Any]:
        """Constructs and returns a Rich Layout object."""
        if not HAVE_RICH:
            print(f"=== {title} ===\n{conversation_text}\n--- Diff ---\n{diff_text}")
            return None

        layout = Layout(name="root")
        layout.split_row(
            Layout(name="left", ratio=3),
            Layout(name="right", ratio=2),
        )

        # Left Pane: Live conversation & tool streaming
        left_panel = Panel(
            Text(conversation_text, style="white"),
            title="[bold cyan]● Live Conversation & Tool Logs[/bold cyan]",
            border_style="cyan",
            box=ROUNDED,
        )
        layout["left"].update(left_panel)

        # Right Pane: Split vertically into Diff Viewer (top) and Todos/Status (bottom)
        layout["right"].split_column(
            Layout(name="diff_viewer", ratio=3),
            Layout(name="sidebar", ratio=2),
        )

        # Right Top: File Diff Viewer
        if diff_text.strip():
            diff_syntax = Syntax(diff_text, "diff", theme="monokai", line_numbers=True)
            diff_panel = Panel(
                diff_syntax,
                title="[bold green]● Real-Time File Diff[/bold green]",
                border_style="green",
                box=ROUNDED,
            )
        else:
            diff_panel = Panel(
                Text("No modified files in current turn.\nWorkspace clean.", style="dim"),
                title="[bold green]● Real-Time File Diff[/bold green]",
                border_style="green",
                box=ROUNDED,
            )
        layout["right"]["diff_viewer"].update(diff_panel)

        # Right Bottom: Active Todos & Subagents
        todo_table = Table(show_header=True, header_style="bold magenta", expand=True)
        todo_table.add_column("Status", width=8)
        todo_table.add_column("Task")

        active_todos = todos or [
            {"done": True, "task": "Load model & tool definitions"},
            {"done": True, "task": "Check provider credentials"},
            {"done": False, "task": "Execute requested prompt"},
        ]
        for t in active_todos:
            st = "[bold green][✓][/bold green]" if t.get("done") else "[bold yellow][ ][/bold yellow]"
            todo_table.add_row(st, t.get("task", ""))

        sidebar_panel = Panel(
            todo_table,
            title="[bold magenta]● Active Todos & HUD[/bold magenta]",
            border_style="magenta",
            box=ROUNDED,
        )
        layout["right"]["sidebar"].update(sidebar_panel)

        return layout

    def display(
        self,
        conversation_text: str,
        diff_text: str = "",
        todos: Optional[List[Dict[str, Any]]] = None,
        title: str = "Omni Split View",
    ):
        """Prints the dual-pane layout directly to terminal."""
        layout = self.render(conversation_text, diff_text, todos, title)
        if layout and self.console:
            self.console.print(layout)
