"""Pi's `<agent-dir>/keybindings.json`: reassign keys to named actions.

    {
      "app.model.select": "ctrl+k",                 one key
      "app.tools.expand": ["ctrl+o", "f2"],         or several
      "app.thinking.toggle": [],                    unbound
      "app.session.new": "ctrl+n",                  actions Pi leaves unassigned run the matching command
      "/board": "f3"                                Omni extra: any slash command
    }

Key names are lower-case with modifiers in the order `ctrl+alt+shift+key` (Pi's `shift+ctrl+p` is accepted too).
An action you list loses its default keys; actions you do not list keep theirs. `/reload` re-reads the file, and
`/hotkeys` shows the result. Actions for parts of Pi's interface this one does not have are ignored.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# action -> (the key the app's handler understands, default keys). "/x" means: run that slash command.
ACTIONS: Dict[str, Tuple[str, List[str]]] = {
    "tui.input.submit": ("enter", ["enter"]),
    "tui.input.newLine": ("shift+enter", ["shift+enter", "ctrl+j", "ctrl+enter"]),
    "tui.editor.cursorWordLeft": ("alt+left", ["alt+left", "ctrl+left", "alt+b"]),
    "tui.editor.cursorWordRight": ("alt+right", ["alt+right", "ctrl+right", "alt+f"]),
    "tui.editor.cursorLineStart": ("home", ["home", "ctrl+a"]),
    "tui.editor.cursorLineEnd": ("end", ["end", "ctrl+e"]),
    "tui.editor.deleteWordBackward": ("ctrl+w", ["ctrl+w", "alt+backspace", "ctrl+backspace"]),
    "tui.editor.deleteToLineStart": ("ctrl+u", ["ctrl+u"]),
    "tui.editor.deleteToLineEnd": ("ctrl+k", ["ctrl+k"]),
    "tui.editor.pageUp": ("pageup", ["pageup"]),
    "tui.editor.pageDown": ("pagedown", ["pagedown"]),
    "app.interrupt": ("escape", ["escape"]),
    "app.clear": ("ctrl+c", ["ctrl+c"]),
    "app.exit": ("ctrl+d", ["ctrl+d"]),
    "app.editor.external": ("ctrl+g", ["ctrl+g"]),
    "app.model.select": ("ctrl+l", ["ctrl+l"]),
    "app.model.cycleForward": ("ctrl+p", ["ctrl+p"]),
    "app.model.cycleBackward": ("alt+p", ["ctrl+shift+p", "alt+p"]),
    "app.thinking.cycle": ("shift+tab", ["shift+tab"]),
    "app.thinking.toggle": ("ctrl+t", ["ctrl+t"]),
    "app.tools.expand": ("ctrl+o", ["ctrl+o"]),
    "app.message.copy": ("ctrl+x", ["ctrl+x"]),
    "app.message.followUp": ("alt+enter", ["alt+enter"]),
    "app.message.dequeue": ("alt+up", ["alt+up"]),
    "app.session.new": ("/new", []),
    "app.session.tree": ("/tree", []),
    "app.session.fork": ("/fork", []),
    "app.session.resume": ("/resume", []),
}
_MODS = ("ctrl", "alt", "shift")


def normalize(key: str) -> str:
    parts = [p.strip().lower() for p in str(key).split("+") if p.strip()]
    if not parts:
        return ""
    if str(key).rstrip().endswith("+") and len(str(key).strip()) > 1:   # "ctrl++"
        parts.append("+")
    mods = [m for m in _MODS if m in parts[:-1]]
    return "+".join(mods + [parts[-1]])


class Keymap:
    def __init__(self, table: Dict[str, str], bindings: Dict[str, List[str]], problems: List[str]):
        self.table = table              # pressed key -> handler key, "/command", or "" (unbound)
        self.bindings = bindings        # action or /command -> keys, as shown by /hotkeys
        self.problems = problems

    def resolve(self, key: str) -> Optional[str]:
        """None: not remapped, handle as usual. '': swallowed. Otherwise the key or '/command' to act on."""
        return self.table.get(key)

    def __bool__(self) -> bool:
        return bool(self.table)


def load(agent_dir: Any) -> Keymap:
    path = Path(agent_dir) / "keybindings.json"
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except OSError:
        return Keymap({}, {}, [])
    except ValueError as e:
        return Keymap({}, {}, [f"keybindings.json: {e}"])
    if not isinstance(raw, dict):
        return Keymap({}, {}, ["keybindings.json: expected an object of action -> key(s)"])
    table: Dict[str, str] = {}
    bindings: Dict[str, List[str]] = {}
    problems: List[str] = []
    custom: Dict[str, Tuple[str, List[str]]] = {}
    for action, keys in raw.items():
        keys = [keys] if isinstance(keys, str) else keys if isinstance(keys, list) else []
        keys = [k for k in (normalize(k) for k in keys) if k]
        if str(action).startswith("/"):
            custom[str(action)] = (str(action), keys)
        elif action in ACTIONS:
            custom[action] = (ACTIONS[action][0], keys)
        elif not str(action).startswith(("tui.", "app.")):
            problems.append(f"keybindings.json: unknown action '{action}'")
    # default keys of a reassigned action stop working, unless another action still owns them
    for action, (_target, keys) in custom.items():
        for k in ACTIONS.get(action, ("", []))[1]:
            if k not in keys:
                table[k] = ""
    for action, (target, defaults) in ACTIONS.items():
        if action not in custom:
            for k in defaults:
                if table.get(k) == "":
                    del table[k]         # still the default of an untouched action
    for action, (target, keys) in custom.items():
        bindings[action] = keys
        for k in keys:
            if table.get(k) not in (None, "") and table[k] != target:
                problems.append(f"keybindings.json: {k} is assigned twice; '{action}' wins")
            table[k] = target
    # a key mapped to itself needs no entry
    return Keymap({k: v for k, v in table.items() if k != v}, bindings, problems)


def describe(km: Keymap) -> str:
    rows = []
    for action, (_target, defaults) in ACTIONS.items():
        keys = km.bindings.get(action, defaults)
        mark = "*" if action in km.bindings else " "
        rows.append(f"{mark} {action:<34} {', '.join(keys) or '(none)'}")
    rows += [f"* {a:<34} {', '.join(k) or '(none)'}" for a, k in km.bindings.items() if a.startswith("/")]
    return "\n".join(rows) + ("\n* = from keybindings.json" if km.bindings else "") + "".join(f"\n! {p}" for p in km.problems)
