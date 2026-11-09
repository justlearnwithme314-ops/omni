"""
Interactive Visual Canvas (src/canvas/ in OpenClaw).
Serves a live web whiteboard / diagram renderer on port 18790.
Allows the AI to render interactive HTML bundles, Mermaid flowcharts, SVG diagrams,
and data visualizations alongside code chat.
"""

from dataclasses import dataclass, field
from http.server import HTTPServer, BaseHTTPRequestHandler
import json
import logging
import os
import secrets
import threading
import time
from typing import Any, Dict, List, Optional

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
            "url": f"http://localhost:18790/canvas/{self.id}",
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
    def __init__(self, port: int = 18790, storage_dir: Optional[str] = None):
        self.port = port
        self.storage_dir = storage_dir or os.path.join(
            os.path.dirname(os.path.dirname(os.path.dirname(__file__))), ".omni", "canvas"
        )
        os.makedirs(self.storage_dir, exist_ok=True)
        self.documents: Dict[str, CanvasDocument] = {}
        self._server: Optional[HTTPServer] = None
        self._thread: Optional[threading.Thread] = None
        self.is_running = False
        self._load_documents()

    def _load_documents(self):
        # Create default sample architecture canvas
        default_doc = CanvasDocument(
            id="architecture_overview",
            title="Omni Universal System Architecture",
            kind="mermaid",
            content="graph TD\n  Client[REPL / CLI / Gateways] --> Kernel[Omni Microkernel]\n  Kernel --> Adapters[5-Ecosystem Adapters]\n  Adapters --> Pi[Oh My Pi / Pi]\n  Adapters --> Claw[OpenClaw Skills]\n  Adapters --> Hermes[Hermes Plugins]\n  Adapters --> MCP[Model Context Protocol]\n  Adapters --> Native[Omni Native Plugins]",
        )
        self.documents[default_doc.id] = default_doc

    def create_document(self, title: str, content: str, kind: str = "mermaid") -> CanvasDocument:
        """Creates and stores a visual document, returning entry URL."""
        doc_id = f"doc_{secrets.token_hex(4)}"
        doc = CanvasDocument(id=doc_id, title=title, content=content, kind=kind)
        self.documents[doc_id] = doc

        # Ensure server is running
        self.start()
        return doc

    def get_document(self, doc_id: str) -> Optional[CanvasDocument]:
        return self.documents.get(doc_id)

    def list_documents(self) -> List[CanvasDocument]:
        return list(self.documents.values())

    def start(self):
        """Starts local Canvas HTTP server on 0.0.0.0:port."""
        if self.is_running:
            return

        manager = self

        class CanvasHTTPHandler(BaseHTTPRequestHandler):
            def log_message(self, format, *args):
                pass  # Quiet logs

            def do_GET(self):
                if self.path in ("/", "/canvas"):
                    # Gallery view
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    docs = [d.to_dict() for d in manager.documents.values()]
                    self.wfile.write(json.dumps({"documents": docs}).encode("utf-8"))
                    return

                if self.path.startswith("/canvas/"):
                    doc_id = self.path.split("?")[0].replace("/canvas/", "")
                    doc = manager.get_document(doc_id)
                    if doc:
                        self.send_response(200)
                        self.send_header("Content-Type", "text/html; charset=utf-8")
                        self.end_headers()

                        if doc.kind == "mermaid":
                            body = f'<pre class="mermaid">\n{doc.content}\n</pre>'
                        elif doc.kind in ("html_bundle", "svg"):
                            body = doc.content
                        else:
                            body = f'<pre style="white-space: pre-wrap;">{doc.content}</pre>'

                        html = CANVAS_VIEWER_HTML.replace("{{TITLE}}", doc.title).replace("{{KIND}}", doc.kind.upper()).replace("{{BODY}}", body)
                        self.wfile.write(html.encode("utf-8"))
                        return

                self.send_response(404)
                self.end_headers()

            def do_POST(self):
                if self.path == "/canvas":
                    length = int(self.headers.get("Content-Length", 0))
                    body = self.rfile.read(length).decode("utf-8")
                    try:
                        data = json.loads(body)
                        doc = manager.create_document(
                            title=data.get("title", "Untitled Canvas"),
                            content=data.get("content", ""),
                            kind=data.get("kind", "mermaid"),
                        )
                        self.send_response(200)
                        self.send_header("Content-Type", "application/json")
                        self.end_headers()
                        self.wfile.write(json.dumps(doc.to_dict()).encode("utf-8"))
                        return
                    except Exception:
                        pass
                self.send_response(400)
                self.end_headers()

        try:
            self._server = HTTPServer(("0.0.0.0", self.port), CanvasHTTPHandler)
            self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
            self._thread.start()
            self.is_running = True
            logger.info(f"VisualCanvasManager listening on port {self.port}")
        except Exception as e:
            logger.warning(f"Failed to bind VisualCanvas on port {self.port}: {e}")
            self.is_running = False

    def stop(self):
        if self._server:
            try:
                self._server.shutdown()
                self._server.server_close()
            except Exception:
                pass
        self.is_running = False
