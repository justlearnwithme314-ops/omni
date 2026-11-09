"""
Interactive Visual Canvas (src/canvas/ in OpenClaw).
Serves a live web whiteboard / diagram renderer on port 18790.
Allows the AI to render interactive HTML bundles, Mermaid flowcharts, SVG diagrams,
and data visualizations alongside code chat.
"""

from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import hmac
import html
import json
import logging
import os
import secrets
import threading
import urllib.parse
import time
from typing import Any, Dict, List, Optional
from ..paths import user_data_dir

logger = logging.getLogger(__name__)


@dataclass
class CanvasDocument:
    id: str
    title: str
    kind: str  # "mermaid", "html_bundle", "svg", "markdown"
    content: str
    created_at: float = field(default_factory=time.time)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "kind": self.kind,
            "content": self.content,
            "created_at": self.created_at,
            
        }


CANVAS_VIEWER_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Omni Canvas - {{TITLE}}</title>
    <script src="https://cdn.jsdelivr.net/npm/mermaid/dist/mermaid.min.js"></script>
    <style>
        body {
            background: #0d1117;
            color: #c9d1d9;
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Helvetica, Arial, sans-serif;
            margin: 0;
            padding: 24px;
        }
        .header {
            border-bottom: 1px solid #30363d;
            padding-bottom: 12px;
            margin-bottom: 24px;
            display: flex;
            justify-content: space-between;
            align-items: center;
        }
        .title { font-size: 1.4rem; font-weight: bold; color: #58a6ff; }
        .meta { color: #8b949e; font-size: 0.85rem; }
        .canvas-container {
            background: #161b22;
            border: 1px solid #30363d;
            border-radius: 8px;
            padding: 24px;
            overflow: auto;
            min-height: 400px;
            display: flex;
            justify-content: center;
            align-items: center;
        }
    </style>
</head>
<body>
    <div class="header">
        <div class="title">◆ {{TITLE}}</div>
        <div class="meta">Rendered by Omni Visual Canvas • {{KIND}}</div>
    </div>
    <div class="canvas-container">
        {{BODY}}
    </div>
    <script>
        if (window.mermaid) {
            mermaid.initialize({ startOnLoad: true, theme: 'dark' });
        }
    </script>
</body>
</html>
"""


class VisualCanvasManager:
    """Local visual canvas server.

    Security model: loopback bind; creating documents over HTTP needs the bearer token;
    titles and diagram source are HTML-escaped; agent-supplied HTML/SVG is served with a
    ``Content-Security-Policy: sandbox allow-scripts`` header so it runs in an opaque origin
    and cannot touch the canvas server's own origin.
    """

    KINDS = ("mermaid", "html_bundle", "svg", "markdown")

    def __init__(self, port: int = 18790, storage_dir: Optional[str] = None, host: str = "127.0.0.1"):
        self.port = port
        self.host = host
        self.token = secrets.token_urlsafe(16)
        self.storage_dir = storage_dir or str(user_data_dir() / "canvas")
        os.makedirs(self.storage_dir, exist_ok=True)
        self.documents: Dict[str, CanvasDocument] = {}
        self._server: Optional[ThreadingHTTPServer] = None
        self._thread: Optional[threading.Thread] = None
        self.is_running = False

    def url_for(self, doc_id: str) -> str:
        return f"http://{self.host}:{self.port}/canvas/{doc_id}"

    def create_document(self, title: str, content: str, kind: str = "mermaid") -> CanvasDocument:
        if kind not in self.KINDS:
            raise ValueError(f"kind must be one of {self.KINDS}")
        doc = CanvasDocument(id=f"doc_{secrets.token_hex(4)}", title=title[:200], content=content, kind=kind)
        self.documents[doc.id] = doc
        self.start()
        return doc

    def get_document(self, doc_id: str) -> Optional[CanvasDocument]:
        return self.documents.get(doc_id)

    def list_documents(self) -> List[CanvasDocument]:
        return list(self.documents.values())

    def render(self, doc: CanvasDocument) -> bytes:
        if doc.kind == "mermaid":
            body = f'<pre class="mermaid">{html.escape(doc.content)}</pre>'
        elif doc.kind in ("html_bundle", "svg"):
            body = doc.content  # untrusted: neutralised by the CSP sandbox header
        else:
            body = f'<pre style="white-space:pre-wrap">{html.escape(doc.content)}</pre>'
        page = (
            CANVAS_VIEWER_HTML.replace("{{TITLE}}", html.escape(doc.title))
            .replace("{{KIND}}", html.escape(doc.kind.upper()))
            .replace("{{BODY}}", body)
        )
        return page.encode("utf-8")

    def start(self) -> None:
        if self.is_running:
            return
        manager = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format, *args):
                pass

            def _send(self, code: int, body: bytes, ctype: str, extra: Optional[Dict[str, str]] = None) -> None:
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("X-Content-Type-Options", "nosniff")
                for k, v in (extra or {}).items():
                    self.send_header(k, v)
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                path = urllib.parse.urlparse(self.path).path
                if path in ("/", "/canvas"):
                    docs = [dict(d.to_dict(), url=manager.url_for(d.id)) for d in manager.documents.values()]
                    for d in docs:
                        d.pop("content", None)
                    return self._send(200, json.dumps({"documents": docs}).encode(), "application/json")
                if path.startswith("/canvas/"):
                    doc = manager.get_document(path[len("/canvas/"):])
                    if doc:
                        return self._send(
                            200, manager.render(doc), "text/html; charset=utf-8",
                            {"Content-Security-Policy": "sandbox allow-scripts"},
                        )
                self._send(404, b'{"error":"not found"}', "application/json")

            def do_POST(self):
                if urllib.parse.urlparse(self.path).path != "/canvas":
                    return self._send(404, b'{"error":"not found"}', "application/json")
                supplied = self.headers.get("Authorization", "").removeprefix("Bearer ").strip()
                if not hmac.compare_digest(supplied, manager.token):
                    return self._send(403, b'{"error":"forbidden"}', "application/json")
                try:
                    length = min(int(self.headers.get("Content-Length", 0)), 1_000_000)
                    data = json.loads(self.rfile.read(length).decode("utf-8"))
                    doc = manager.create_document(
                        str(data.get("title", "Untitled")), str(data.get("content", "")), str(data.get("kind", "mermaid"))
                    )
                except Exception as e:
                    return self._send(400, json.dumps({"error": str(e)}).encode(), "application/json")
                self._send(200, json.dumps(dict(doc.to_dict(), url=manager.url_for(doc.id))).encode(), "application/json")

        try:
            self._server = ThreadingHTTPServer((self.host, self.port), Handler)
        except OSError as e:
            raise RuntimeError(f"Canvas server cannot bind {self.host}:{self.port}: {e}") from e
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        self.is_running = True

    def stop(self) -> None:
        if self._server:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
        self.is_running = False
