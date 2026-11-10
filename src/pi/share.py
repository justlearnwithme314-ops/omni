"""Session sharing like Pi's session-share.ts: Radius artifacts (https://radius.pi.dev/v1/artifacts) with a trailing
`pi.share` entry, falling back to a private gist viewed through https://pi.dev/session/#<gist id>."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import urllib.error
import urllib.parse
import urllib.request
import uuid
from typing import Any, Optional, Tuple

RADIUS_GATEWAY = os.environ.get("PI_RADIUS_GATEWAY", "https://radius.pi.dev")
VIEWER = "https://pi.dev/session/"


def radius_token(session: Any) -> Optional[str]:
    tok = None
    try:
        tok = session.auth.get_api_key("radius")
    except Exception:
        tok = None
    return tok or os.environ.get("RADIUS_API_KEY") or None


def export_for_share(session: Any, path: str) -> None:
    """Current branch as JSONL plus the trailing `pi.share` entry (system prompt + tool schemas)."""
    sm = session.sessions
    branch = sm.get_branch()
    with open(path, "w", encoding="utf-8") as f:
        f.write(json.dumps(sm.header, ensure_ascii=False) + "\n")
        for e in branch:
            f.write(json.dumps(e, ensure_ascii=False) + "\n")
        from .session import now_iso
        trailing = {"type": "custom", "customType": "pi.share", "id": uuid.uuid4().hex[:8],
                    "parentId": branch[-1]["id"] if branch else None, "timestamp": now_iso(),
                    "data": {"systemPrompt": session.last_system_prompt or session.system_prompt(),
                             "tools": [{"name": t.name, "description": t.description, "parameters": t.parameters}
                                       for t in session.active_tools()]}}
        f.write(json.dumps(trailing, ensure_ascii=False) + "\n")


def share_via_radius(path: str, token: str) -> Tuple[bool, str]:
    body = open(path, "rb").read()
    url = RADIUS_GATEWAY.rstrip("/") + "/v1/artifacts?" + urllib.parse.urlencode({"visibility": "organization", "title": "Pi session"})
    req = urllib.request.Request(url, data=body, method="POST", headers={
        "Authorization": f"Bearer {token}", "Content-Type": "application/x-ndjson", "Content-Length": str(len(body))})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            data = json.loads(r.read().decode() or "null")
    except urllib.error.HTTPError as e:
        try:
            err = json.loads(e.read().decode()).get("error")
        except Exception:
            err = None
        return False, f"Failed to upload Radius artifact: {err or e.reason or e.code}"
    except Exception as e:
        return False, f"Failed to upload Radius artifact: {e}"
    art = (data or {}).get("artifact") or {}
    if not art.get("canonical_url"):
        return False, f"Failed to upload Radius artifact: {(data or {}).get('error') or 'no artifact returned'}"
    return True, f"Share URL: {art['canonical_url']}"


def share_via_gist(session: Any, tmpdir: str) -> Tuple[bool, str]:
    if not shutil.which("gh"):
        return False, "GitHub CLI (gh) is not installed. Install it from https://cli.github.com/"
    if subprocess.run(["gh", "auth", "status"], capture_output=True, text=True).returncode != 0:
        return False, "GitHub CLI is not logged in. Run 'gh auth login' first."
    from .commands import export_html
    html = os.path.join(tmpdir, "session.html")
    try:
        export_html(session, html)
    except Exception as e:
        return False, f"Failed to export session: {e}"
    r = subprocess.run(["gh", "gist", "create", "--public=false", html], capture_output=True, text=True)
    if r.returncode != 0:
        return False, f"Failed to create gist: {(r.stderr or '').strip() or 'Unknown error'}"
    gist_url = (r.stdout or "").strip()
    gist_id = gist_url.rstrip("/").split("/")[-1] if gist_url else ""
    if not gist_id:
        return False, "Failed to parse gist ID from gh output"
    viewer = os.environ.get("PI_SHARE_VIEWER_URL") or VIEWER
    return True, f"Share URL: {viewer}#{gist_id}\nGist: {gist_url}"


def share_session(session: Any) -> Tuple[bool, str]:
    tmpdir = tempfile.mkdtemp(prefix="pi-share-")
    try:
        jsonl = os.path.join(tmpdir, "session.jsonl")
        try:
            export_for_share(session, jsonl)
        except Exception as e:
            return False, f"Failed to export session: {e}"
        dest = session.settings.get("sessionShareDestination", "auto")
        if dest != "gist":
            tok = radius_token(session)
            if tok:
                return share_via_radius(jsonl, tok)
            if dest == "radius":
                return False, "Radius authentication is not configured. Run '/login radius' first (or set RADIUS_API_KEY)."
        return share_via_gist(session, tmpdir)
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
