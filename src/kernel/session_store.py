"""JSONL session store (Pi-style: header + one entry per message) with Pi session import."""
import json
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..paths import user_data_dir, workspace_root
from .state import Message, SessionState


def _text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(p.get("text", "") for p in content if isinstance(p, dict) and p.get("type") == "text")
    return ""


class SessionStore:
    def __init__(self, root: Optional[Path] = None):
        self.root = Path(root) if root else user_data_dir() / "sessions"
        self.root.mkdir(parents=True, exist_ok=True)

    def path(self, sid: str) -> Path:
        return self.root / f"{sid}.jsonl"

    def save(self, state: SessionState) -> Path:
        p = self.path(state.session_id)
        head = {"type": "session", "version": 1, "id": state.session_id, "cwd": str(workspace_root()),
                "name": state.scratchpad.get("name", ""), "timestamp": time.time()}
        lines = [json.dumps(head)]
        for i, m in enumerate(state.messages):
            lines.append(json.dumps({"type": "message", "id": i, "parentId": i - 1 if i else None, "message": m.to_dict()}))
        p.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return p

    def _read(self, p: Path) -> List[dict]:
        return [json.loads(l) for l in p.read_text(encoding="utf-8").splitlines() if l.strip()]

    def load(self, ref: str) -> SessionState:
        p = Path(ref) if Path(ref).is_file() else self.path(ref)
        if not p.is_file():
            matches = [f for f in self.root.glob("*.jsonl") if f.stem.startswith(ref)]
            if len(matches) != 1:
                raise FileNotFoundError(f"No unique session matching '{ref}'")
            p = matches[0]
        return self._to_state(self._read(p))

    def _to_state(self, entries: List[dict]) -> SessionState:
        head = next((e for e in entries if e.get("type") == "session"), {})
        st = SessionState(session_id=head.get("id") or uuid.uuid4().hex[:12])
        if head.get("name"):
            st.scratchpad["name"] = head["name"]
        for e in entries:
            if e.get("type") != "message":
                continue
            m = e["message"]
            role = {"toolResult": "tool"}.get(m.get("role"), m.get("role", "user"))
            if role not in ("user", "assistant", "tool", "system"):
                continue
            if "content" in m and not isinstance(m["content"], str):  # Pi format: content parts
                st.messages.append(Message(role=role, content=_text(m["content"]), tool_call_id=m.get("toolCallId")))
            else:
                st.messages.append(Message(**{k: v for k, v in m.items() if k in Message.__dataclass_fields__}))
        return st

    def import_file(self, path: str) -> SessionState:
        st = self._to_state(self._read(Path(path)))
        return st

    def list(self) -> List[Dict[str, Any]]:
        rows = []
        for f in sorted(self.root.glob("*.jsonl"), key=lambda f: f.stat().st_mtime, reverse=True):
            try:
                ents = self._read(f)
            except Exception:
                continue
            msgs = [e["message"] for e in ents if e.get("type") == "message"]
            first = next((_text(m.get("content", "")) for m in msgs if m.get("role") == "user"), "")
            rows.append({"id": f.stem, "name": (ents[0].get("name") if ents else "") or "", "messages": len(msgs),
                         "first": first.strip().replace("\n", " ")[:60], "mtime": f.stat().st_mtime})
        return rows

    @staticmethod
    def export_html(state: SessionState) -> str:
        import html
        rows = "".join(f'<div class="m {m.role}"><b>{m.role}</b><pre>{html.escape(m.content or "")}</pre></div>' for m in state.messages)
        return ("<!doctype html><meta charset=utf-8><title>Omni session</title><style>body{font:14px sans-serif;background:#111;"
                "color:#ddd;max-width:860px;margin:2em auto}.m{margin:1em 0;padding:.6em 1em;border-left:3px solid #555;background:#1b1b1b}"
                ".user{border-color:#7aa2f7}.assistant{border-color:#9ece6a}pre{white-space:pre-wrap}</style>" + rows)
