"""
Companion Device Node Pairing Protocol (/pair, /nodes in OpenClaw).
Pairs secondary devices (phones, tablets, laptops) via QR code, Tailscale, or local network.
Allows the agent to dispatch tasks like screen.capture, system.run, or mobile notifications to paired nodes.
"""

from dataclasses import dataclass, field
import hashlib
import json
import logging
import os
import secrets
import time
from typing import Any, Dict, List, Optional
from ..paths import user_data_dir

logger = logging.getLogger(__name__)


@dataclass
class PairedNode:
    id: str
    name: str
    platform: str  # "ios", "android", "macos", "linux", "windows"
    ip_address: str
    capabilities: List[str]
    token: str = ""
    status: str = "online"  # "online", "idle", "offline"
    paired_at: float = field(default_factory=time.time)
    last_ping: float = field(default_factory=time.time)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "platform": self.platform,
            "ip_address": self.ip_address,
            "capabilities": self.capabilities,
            "status": self.status,
            "paired_at": self.paired_at,
        }


@dataclass
class PairingRequest:
    request_id: str
    token: str
    expires_at: float
    device_name: Optional[str] = None
    platform: Optional[str] = None


class NodePairingManager:
    def __init__(self, storage_path: Optional[str] = None):
        self.storage_path = storage_path or str(user_data_dir() / "paired_nodes.json")
        os.makedirs(os.path.dirname(self.storage_path), exist_ok=True)
        self.nodes: Dict[str, PairedNode] = {}
        self.pending_requests: Dict[str, PairingRequest] = {}
        self._load_nodes()

    def _load_nodes(self):
        if os.path.exists(self.storage_path):
            try:
                with open(self.storage_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                for item in data:
                    node = PairedNode(**item)
                    self.nodes[node.id] = node
            except Exception as e:
                logger.error(f"Failed to load paired nodes: {e}")

        # Ensure default local companion node exists
        if not self.nodes:
            default_node = PairedNode(
                id="node_local_host",
                name="Local Host Terminal",
                platform="windows" if os.name == "nt" else "linux",
                ip_address="127.0.0.1",
                token=secrets.token_hex(16),
                capabilities=["system.run", "screen.capture", "notification"],
                status="online",
            )
            self.nodes[default_node.id] = default_node
            self._save_nodes()

    def _save_nodes(self):
        try:
            with open(self.storage_path, "w", encoding="utf-8") as f:
                json.dump([n.to_dict() for n in self.nodes.values()], f, indent=2)
        except Exception as e:
            logger.error(f"Failed to save paired nodes: {e}")

    def create_pairing_request(self, ttl_sec: int = 300) -> PairingRequest:
        """Generates an ephemeral pairing token and request ID for mobile QR scanning."""
        req_id = f"pair_{secrets.token_hex(4)}"
        token = secrets.token_urlsafe(24)
        req = PairingRequest(
            request_id=req_id,
            token=token,
            expires_at=time.time() + ttl_sec,
        )
        self.pending_requests[req_id] = req
        return req

    def get_pairing_url(self, req: PairingRequest, host: str = "127.0.0.1", port: int = 18789) -> str:
        return f"omni://pair?id={req.request_id}&token={req.token}&gateway={host}:{port}"

    def render_qr(self, pairing_url: str) -> str:
        """Terminal QR for the pairing URL, or '' when the optional 'qrcode' package is missing."""
        try:
            import io
            import qrcode  # optional: pip install qrcode
        except ImportError:
            return ""
        qr = qrcode.QRCode(border=1)
        qr.add_data(pairing_url)
        buf = io.StringIO()
        qr.print_ascii(out=buf, invert=True)
        return buf.getvalue()

    def approve_pairing(self, request_id: str, device_name: str, platform: str, ip: str = "127.0.0.1") -> Optional[PairedNode]:
        """Approves a pairing request and registers the new companion node."""
        req = self.pending_requests.pop(request_id, None)
        if not req or time.time() > req.expires_at:
            return None

        node_id = f"node_{secrets.token_hex(4)}"
        node = PairedNode(
            id=node_id,
            name=device_name,
            platform=platform,
            ip_address=ip,
            token=req.token,
            capabilities=["screen.capture", "system.run", "notification"],
            status="online",
        )
        self.nodes[node_id] = node
        self._save_nodes()
        return node

    def list_nodes(self) -> List[PairedNode]:
        return list(self.nodes.values())

    def dispatch_remote_task(self, node_id: str, capability: str, params: Dict[str, Any]) -> Dict[str, Any]:
        """Remote execution is NOT implemented: pairing only records devices.

        A live transport (a companion app connecting back to the gateway) is required before
        tasks can run on another device. This returns an explicit error instead of fake data.
        """
        node = self.nodes.get(node_id)
        if not node:
            return {"success": False, "error": f"Node '{node_id}' not found"}
        if capability not in node.capabilities:
            return {"success": False, "error": f"Node '{node.name}' does not advertise '{capability}'"}
        return {
            "success": False,
            "error": "not_implemented: no live transport to this node (companion app not connected)",
            "node": node.name,
            "capability": capability,
        }
