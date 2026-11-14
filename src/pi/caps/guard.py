"""`mcp-audit` and `structured-output`.

`mcp-audit` (Hermes' OSV check): MCP servers are usually started with `npx <package>` or `uvx <package>`, which
downloads and runs whatever is published under that name. `/mcp-audit` (and the `mcp_audit` tool) looks each such
package up in the OSV database (api.osv.dev) and reports known malware (`MAL-…` advisories) and vulnerabilities.
Only the package name, ecosystem and pinned version are sent. Nothing is blocked: it is a report.

`structured-output` (Browser Use's output schema): when a JSON schema is set, the agent must finish by calling
`respond` with a value that matches it; a mismatch is returned to the model as an error to fix. The accepted value
is kept as `session.structured_output` and written to `OMNI_OUTPUT_FILE` when that is set, so scripts get a
machine-readable result instead of prose. The schema comes from the profile (`"output_schema": {...}`) or from
`.omni/output-schema.json` in the project. Supported keywords: type, properties, required, items, enum,
additionalProperties: false.
"""
from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from ..tools.base import ToolDef, ToolError, text_result
from . import Capability

OSV_URL = "https://api.osv.dev/v1/query"
_RUNNERS = {"npx": "npm", "bunx": "npm", "pnpx": "npm", "uvx": "PyPI", "pipx": "PyPI"}


def package_of(cfg: Dict[str, Any]) -> Optional[Tuple[str, str, str]]:
    """(ecosystem, name, version or '') for a server started through a package runner, else None."""
    exe = Path(str(cfg.get("command") or "")).name.lower()
    exe = re.sub(r"\.(cmd|bat|exe)$", "", exe)
    args = [str(a) for a in (cfg.get("args") or [])]
    if exe in ("pnpm", "yarn", "bun") and args[:1] in (["dlx"], ["x"]):
        exe, args = "npx", args[1:]
    if exe == "uv" and args[:2] == ["tool", "run"]:
        exe, args = "uvx", args[2:]
    if exe == "pipx" and args[:1] == ["run"]:
        args = args[1:]
    eco = _RUNNERS.get(exe)
    if not eco:
        return None
    skip = False
    for a in args:
        if skip:
            skip = False
            continue
        if a in ("-p", "--package", "--from", "--spec", "--with", "--python"):
            skip = a in ("--with", "--python")
            if not skip:
                continue
            continue
        if a.startswith("-"):
            continue
        if eco == "npm":
            m = re.match(r"^(@[^/@]+/[^@]+|[^@]+)(?:@(.+))?$", a)
            return (eco, m.group(1), (m.group(2) or "") if m else "") if m else None
        m = re.match(r"^([A-Za-z0-9_.-]+)(?:\[[^\]]*\])?(?:==(.+))?$", a)
        return (eco, m.group(1), m.group(2) or "") if m else None
    return None


def osv_query(eco: str, name: str, version: str = "", url: Optional[str] = None) -> List[Dict[str, Any]]:
    body: Dict[str, Any] = {"package": {"ecosystem": eco, "name": name}}
    if version and re.match(r"^\d", version):
        body["version"] = version
    req = urllib.request.Request(url or os.environ.get("OMNI_OSV_URL") or OSV_URL, data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json", "User-Agent": "omni-mcp-audit/1.0"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return list(json.loads(r.read(2_000_000).decode("utf-8", "replace")).get("vulns") or [])


def audit(session: Any) -> str:
    servers = getattr(getattr(session, "mcp", None), "servers", None) or {}
    if not servers:
        return "No MCP servers are configured."
    rows, bad = [], 0
    for name, srv in servers.items():
        pkg = package_of(srv.cfg)
        if pkg is None:
            rows.append(f"  {name}: not started through a package runner (nothing to look up)")
            continue
        eco, pname, ver = pkg
        label = f"{pname}{'@' + ver if ver else ''} ({eco})"
        try:
            vulns = osv_query(eco, pname, ver)
        except (urllib.error.URLError, OSError, ValueError) as e:
            rows.append(f"  {name}: {label} - could not reach OSV: {getattr(e, 'reason', e)}")
            continue
        mal = [v for v in vulns if str(v.get("id", "")).startswith("MAL-")]
        if mal:
            bad += 1
            rows.append(f"! {name}: {label} is reported as MALWARE: " + ", ".join(v["id"] for v in mal[:3])
                        + f" - {str(mal[0].get('summary') or '')[:120]}")
        elif vulns and ver:
            rows.append(f"~ {name}: {label} has {len(vulns)} known vulnerabilit{'y' if len(vulns) == 1 else 'ies'}: "
                        + ", ".join(str(v.get('id')) for v in vulns[:4]))
        else:
            rows.append(f"  {name}: {label} - no malware reports" + ("" if ver else " (version not pinned)"))
    head = f"{bad} server(s) run a package reported as malware. Disable them." if bad else "No malware reports."
    return head + "\n" + "\n".join(rows)


def _audit_tools(session: Any) -> Dict[str, ToolDef]:
    return {"mcp_audit": ToolDef(
        name="mcp_audit", label="MCP audit",
        description="Check the packages behind the configured MCP servers (npx / uvx) against the OSV database for known "
                    "malware and vulnerabilities. Run it before enabling a server you do not know.",
        parameters={"type": "object", "properties": {}},
        execute=lambda _id, p, signal=None, on_update=None, ctx=None: text_result(audit(session)),
        prompt_snippet="Check MCP server packages for known malware")}


# ------------------------------------------------------------------------------------------- structured output
_TYPES = {"string": str, "number": (int, float), "integer": int, "boolean": bool, "array": list, "object": dict}


def check(value: Any, schema: Any, path: str = "$") -> List[str]:
    if not isinstance(schema, dict):
        return []
    errs: List[str] = []
    t = schema.get("type")
    if isinstance(t, str):
        t = [t]
    if t:
        ok = False
        for name in t:
            if name == "null":
                ok = ok or value is None
            elif name in _TYPES:
                ok = ok or (isinstance(value, _TYPES[name]) and not (name in ("number", "integer") and isinstance(value, bool)))
        if not ok:
            return [f"{path}: expected {' or '.join(t)}, got {type(value).__name__}"]
    if "enum" in schema and value not in schema["enum"]:
        errs.append(f"{path}: must be one of {schema['enum']}")
    if isinstance(value, dict):
        props = schema.get("properties") or {}
        for k in schema.get("required") or []:
            if k not in value:
                errs.append(f"{path}.{k}: missing")
        for k, v in value.items():
            if k in props:
                errs += check(v, props[k], f"{path}.{k}")
            elif schema.get("additionalProperties") is False:
                errs.append(f"{path}.{k}: not allowed")
    if isinstance(value, list) and "items" in schema:
        for i, v in enumerate(value):
            errs += check(v, schema["items"], f"{path}[{i}]")
    return errs


def output_schema(session: Any) -> Optional[Dict[str, Any]]:
    sch = session.profile.get("output_schema")
    if isinstance(sch, dict):
        return sch
    try:
        sch = json.loads((Path(session.cwd) / ".omni" / "output-schema.json").read_text(encoding="utf-8"))
        return sch if isinstance(sch, dict) else None
    except (OSError, ValueError):
        return None


def _respond_tools(session: Any) -> Dict[str, ToolDef]:
    schema = output_schema(session)
    session.structured_output = None
    if schema is None:
        return {}
    wrapped = schema.get("type") != "object"   # tool arguments must be an object

    def execute(_id, p, signal=None, on_update=None, ctx=None):
        value = p.get("value") if wrapped else p
        errs = check(value, schema)
        if errs:
            raise ToolError("The result does not match the required schema:\n" + "\n".join(errs[:12]) + "\nCall respond again with a corrected value.")
        session.structured_output = value
        out = os.environ.get("OMNI_OUTPUT_FILE")
        if out:
            Path(out).write_text(json.dumps(value, indent=2, ensure_ascii=False), encoding="utf-8")
        return text_result("Result accepted. You are done; do not add anything else.", {"accepted": True})

    return {"respond": ToolDef(
        name="respond", label="Respond",
        description="Deliver the final result of this task in the required structure. Call it exactly once, as the last step.",
        parameters=schema if not wrapped else {"type": "object", "properties": {"value": schema}, "required": ["value"]},
        execute=execute, prompt_snippet="Deliver the final structured result")}


def _respond_prompt(session: Any) -> str:
    if output_schema(session) is None:
        return ""
    return ("This task has a required output structure. When the work is finished, call the `respond` tool with the result "
            "instead of writing it as text.")


CAPABILITIES = [
    Capability("mcp-audit", "MCP package audit", "Check the packages behind MCP servers against OSV for known malware.",
               "Hermes OSV malware check", tools=_audit_tools,
               commands=lambda s: {"mcp-audit": ("Check MCP server packages against the OSV database", lambda _a: audit(s))}),
    Capability("structured-output", "Structured output", "Finish with a result that matches a JSON schema (profile "
               "`output_schema` or .omni/output-schema.json).", "Browser Use output schema", tools=_respond_tools, prompt=_respond_prompt),
]
