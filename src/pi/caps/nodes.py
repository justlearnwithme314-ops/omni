"""`nodes`: run commands and read/write files on another machine (OpenClaw's paired nodes).

A node is another computer - a build box, a server, a Raspberry Pi - running the small Omni node server. Start it
there, in the folder the agent may work in; it prints a URL and a token:

    omni node serve --root ~/project              (add --read-only to allow reading and listing only)

Register it here, then turn this capability on:

    omni node add buildbox http://127.0.0.1:8765 <token>      (kept in <agent-dir>/nodes.json; `omni node list|remove`)

or put it in a profile, with the token taken from an environment variable:

    "nodes": {"buildbox": {"url": "http://127.0.0.1:8765", "token": "$BUILDBOX_TOKEN"}}

The agent then uses the `node` tool:

    list                              the configured nodes
    info(node)                        hostname, platform, root folder, read-only or not
    exec(node, command, timeout?)     run a shell command there (output is the last 60,000 characters)
    read(node, path, offset?, limit?) read a file (offset is the first line, 1-based)
    write(node, path, content)        create or replace a file
    ls(node, path?)                   list a folder

Paths are relative to the node's root and cannot leave it. `/nodes` shows which nodes answer.

The connection is PLAIN HTTP: the token and all data are unencrypted. Keep the node on 127.0.0.1 and reach it
through an SSH tunnel (`ssh -L 8765:127.0.0.1:8765 host`), a VPN or a tailnet rather than an open network.
Plan mode and the approvals capability treat `exec` like a local shell command.
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Any, Dict, Optional

from ..node_server import DEFAULT_TIMEOUT, MAX_BODY, MAX_TIMEOUT, load_nodes
from ..tools.base import ToolDef, ToolError, text_result
from . import Capability

HTTP_TIMEOUT = 20      # seconds, for everything but exec
PROBE_TIMEOUT = 4      # /nodes


def configured(session: Any) -> Dict[str, Dict[str, Any]]:
    """Nodes from `<agent-dir>/nodes.json`, then the profile's `nodes` (which wins on a name clash)."""
    nodes = load_nodes(session.agent_dir)
    extra = session.profile.get("nodes")
    if isinstance(extra, dict):
        nodes.update({str(k): dict(v) for k, v in extra.items() if isinstance(v, dict)})
    return nodes


def _token(name: str, entry: Dict[str, Any]) -> str:
    token = str(entry.get("token") or "")
    if token.startswith("$"):
        value = os.environ.get(token[1:])
        if not value:
            raise ToolError(f"node '{name}': the environment variable {token[1:]} (its token) is not set")
        return value
    if not token:
        raise ToolError(f"node '{name}' has no token configured")
    return token


def request(name: str, entry: Dict[str, Any], endpoint: str, payload: Dict[str, Any], timeout: float = HTTP_TIMEOUT) -> Dict[str, Any]:
    url = str(entry.get("url") or "").rstrip("/")
    if not url.startswith(("http://", "https://")):
        raise ToolError(f"node '{name}' has no valid url (it must start with http:// or https://)")
    req = urllib.request.Request(url + endpoint, data=json.dumps(payload).encode("utf-8"), method="POST",
                                 headers={"Content-Type": "application/json", "Authorization": f"Bearer {_token(name, entry)}"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        if e.code == 401:
            raise ToolError(f"node '{name}' rejected the token (401). Check the token given to `omni node add` against "
                            "the one `omni node serve` printed.") from None
        try:
            why = json.loads(e.read().decode("utf-8")).get("error") or e.reason
        except Exception:
            why = e.reason
        raise ToolError(f"node '{name}': {why} ({e.code})") from None
    except (urllib.error.URLError, OSError) as e:  # refused, DNS, timeout
        raise ToolError(f"node '{name}' is unreachable at {url}: {getattr(e, 'reason', e)}. Is `omni node serve` running "
                        "there, and the tunnel/VPN up?") from None
    except ValueError:
        raise ToolError(f"node '{name}' at {url} did not answer like an Omni node") from None
    if not isinstance(data, dict):
        raise ToolError(f"node '{name}' at {url} did not answer like an Omni node")
    return data


def _info_line(info: Dict[str, Any]) -> str:
    return f"{info.get('hostname')} ({info.get('platform')}), root {info.get('root')}" + (", read-only" if info.get("read_only") else "")


def status_text(session: Any) -> str:
    nodes = configured(session)
    if not nodes:
        return "No nodes configured. Start one with `omni node serve` on the other machine, then `omni node add NAME URL TOKEN` here."
    rows = []
    for name in sorted(nodes):
        try:
            state = "ok: " + _info_line(request(name, nodes[name], "/v1/info", {}, timeout=PROBE_TIMEOUT))
        except ToolError as e:
            state = "NOT ANSWERING: " + str(e)
        rows.append(f"{name:<16} {nodes[name].get('url', '')}\n    {state}")
    return "\n".join(rows)


def _tools(session: Any) -> Dict[str, ToolDef]:
    def execute(_id, p, signal=None, on_update=None, ctx=None):
        action = str(p.get("action") or "list").lower()
        nodes = configured(session)
        if action == "list":
            if not nodes:
                return text_result("No nodes configured. The user adds one with `omni node add NAME URL TOKEN`.", {"count": 0})
            return text_result("\n".join(f"{n:<16} {nodes[n].get('url', '')}" for n in sorted(nodes)), {"count": len(nodes), "nodes": sorted(nodes)})
        if action not in ("info", "exec", "read", "write", "ls"):
            raise ToolError("action must be list, info, exec, read, write or ls")
        name = str(p.get("node") or "")
        entry = nodes.get(name)
        if entry is None:
            raise ToolError(f"no node '{name}'. " + (f"Configured: {', '.join(sorted(nodes))}" if nodes else "None are configured."))
        if action == "info":
            info = request(name, entry, "/v1/info", {})
            return text_result(f"{name}: {_info_line(info)}", info)
        if action == "exec":
            command = str(p.get("command") or "").strip()
            if not command:
                raise ToolError("command is required")
            try:
                timeout = max(0.1, min(float(p.get("timeout") or DEFAULT_TIMEOUT), MAX_TIMEOUT))
            except (TypeError, ValueError):
                raise ToolError("timeout must be a number of seconds") from None
            r = request(name, entry, "/v1/exec", {"command": command, "timeout": timeout}, timeout=timeout + 30)
            text = str(r.get("output") or "").rstrip("\n")
            if r.get("truncated"):
                text = "[earlier output dropped; this is the end of it]\n" + text
            if r.get("timed_out"):
                text += f"\n[timed out after {timeout:g}s and was killed]"
            elif r.get("exit_code"):
                text += f"\n[exit code {r.get('exit_code')}]"
            return text_result(text.strip("\n") or "(no output)", {"exitCode": r.get("exit_code"), "timedOut": bool(r.get("timed_out")),
                                                                  "truncated": bool(r.get("truncated"))})
        path = str(p.get("path") or "")
        if action == "ls":
            r = request(name, entry, "/v1/list", {"path": path or "."})
            entries = r.get("entries") or []
            rows = [f"{e.get('name')}/" if e.get("type") == "dir" else f"{e.get('name')}  ({e.get('size')} bytes)" for e in entries]
            if r.get("truncated"):
                rows.append("[more entries not shown]")
            return text_result("\n".join(rows) or "(empty folder)", {"count": len(entries)})
        if not path:
            raise ToolError("path is required")
        if action == "read":
            body: Dict[str, Any] = {"path": path}
            for key in ("offset", "limit"):
                if p.get(key) is not None:
                    try:
                        body[key] = int(p[key])
                    except (TypeError, ValueError):
                        raise ToolError(f"{key} must be an integer") from None
            r = request(name, entry, "/v1/read", body)
            content, total = str(r.get("content") or ""), int(r.get("total_lines") or 0)
            first = max(1, int(body.get("offset") or 1))
            shown = len(content.splitlines())
            note = f"\n[lines {first}-{first + shown - 1} of {total}; pass offset to read on]" if shown and first + shown - 1 < total else ""
            return text_result((content or "(empty)") + note, {"totalLines": total})
        if not isinstance(p.get("content"), str):
            raise ToolError("content is required")
        if len(p["content"].encode("utf-8")) > MAX_BODY - 4096:  # the node would drop the connection mid-upload
            raise ToolError(f"content is too large: a node accepts at most {MAX_BODY // (1024 * 1024)} MB per write")
        r = request(name, entry, "/v1/write", {"path": path, "content": p["content"]})
        return text_result(f"Wrote {r.get('bytes')} bytes to {path} on {name}.", {"bytes": r.get("bytes")})

    return {"node": ToolDef(
        name="node", label="Node",
        description="Work on another machine that runs an Omni node server. Actions: list, info(node), "
                    "exec(node, command, timeout?: seconds, max 600), read(node, path, offset?, limit?), "
                    "write(node, path, content), ls(node, path?). Paths are relative to the node's root folder and "
                    "cannot leave it. exec returns the end of the output (60,000 characters at most).",
        parameters={"type": "object", "properties": {
            "action": {"type": "string", "enum": ["list", "info", "exec", "read", "write", "ls"]},
            "node": {"type": "string"}, "command": {"type": "string"}, "timeout": {"type": "number"},
            "path": {"type": "string"}, "content": {"type": "string"}, "offset": {"type": "integer"},
            "limit": {"type": "integer"}}, "required": ["action"]},
        execute=execute, prompt_snippet="Run commands and read/write files on remote nodes")}


def _gate(session: Any, tool_name: str, args: Dict[str, Any]) -> Optional[str]:
    # a command on another machine is still a shell command: let the same guards see it
    if tool_name == "node" and str(args.get("action")).lower() == "exec":
        return session.caps.gate("bash", {"command": str(args.get("command") or "")})
    return None


CAPABILITIES = [Capability("nodes", "Remote nodes", "Run commands and read/write files on other machines that run "
                           "`omni node serve`.", "OpenClaw nodes", tools=_tools, gate=_gate,
                           commands=lambda s: {"nodes": ("List remote nodes and whether each answers", lambda _a: status_text(s))})]
