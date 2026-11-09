from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

from .base import CanonicalTool, DiscoveryResult

logger = logging.getLogger("omni.adapters.openclaw")


def parse_frontmatter(content: str) -> Tuple[Dict[str, Any], str]:
    """Parse YAML frontmatter with JSON5 and single-line regex fallbacks.
    
    Returns (metadata_dict, markdown_body).
    """
    pattern = r"^\s*---\s*\n(.*?)\n(?:---|\.\.\.)\s*\n(.*)$"
    match = re.match(pattern, content, re.DOTALL)
    if not match:
        return {}, content

    fm_str, body = match.group(1), match.group(2)
    data: Dict[str, Any] = {}

    # 1. Attempt standard YAML parsing
    try:
        import yaml
        parsed = yaml.safe_load(fm_str)
        if isinstance(parsed, dict):
            return parsed, body
    except Exception:
        pass

    # 2. JSON5 fallback parser
    try:
        # Strip single-line (//...) and multi-line (/*...*/) comments
        cleaned = re.sub(r"//.*", "", fm_str)
        cleaned = re.sub(r"/\*[\s\S]*?\*/", "", cleaned)
        # Quote unquoted property names: { name: "val" } -> { "name": "val" }
        cleaned = re.sub(r"(?<=[{,\n])\s*([a-zA-Z0-9_]+)\s*:", r' "\1":', cleaned)
        # Replace single-quoted strings with double quotes
        cleaned = re.sub(r"'([^']*)'", r'"\1"', cleaned)
        # Strip trailing commas before } or ]
        cleaned = re.sub(r",\s*([\]}])", r"\1", cleaned)
        parsed = json.loads(cleaned)
        if isinstance(parsed, dict):
            return parsed, body
    except Exception:
        pass

    # 3. Single-line fallback parser
    data = {
        "metadata": {
            "openclaw": {
                "requires": {
                    "bins": [],
                    "env": [],
                    "os": [],
                }
            }
        }
    }

    current_section = ""
    for line in fm_str.splitlines():
        line_stripped = line.strip()
        if not line_stripped or line_stripped.startswith("#"):
            continue

        if line_stripped.startswith("name:"):
            data["name"] = line_stripped.split(":", 1)[1].strip().strip("\"'")
        elif line_stripped.startswith("description:"):
            data["description"] = line_stripped.split(":", 1)[1].strip().strip("\"'")
        elif "bins:" in line_stripped:
            m = re.search(r"bins:\s*\[(.*?)\]", line_stripped)
            if m:
                data["metadata"]["openclaw"]["requires"]["bins"] = [
                    x.strip().strip("\"'") for x in m.group(1).split(",") if x.strip()
                ]
        elif "env:" in line_stripped:
            m = re.search(r"env:\s*\[(.*?)\]", line_stripped)
            if m:
                data["metadata"]["openclaw"]["requires"]["env"] = [
                    x.strip().strip("\"'") for x in m.group(1).split(",") if x.strip()
                ]
        elif "os:" in line_stripped:
            m = re.search(r"os:\s*\[(.*?)\]", line_stripped)
            if m:
                data["metadata"]["openclaw"]["requires"]["os"] = [
                    x.strip().strip("\"'") for x in m.group(1).split(",") if x.strip()
                ]

    return data, body


class OpenClawAdapter:
    """Adapter for OpenClaw skills conforming to SKILL.md specification."""

    def __init__(
        self,
        root_dir: Optional[Union[str, Path]] = None,
        config: Optional[Any] = None,
        event_bus: Optional[Any] = None,
    ):
        if root_dir is not None:
            self.root_dir = Path(root_dir).resolve()
        else:
            # Default to repo root (three levels up from src/adapters/openclaw.py)
            self.root_dir = Path(__file__).resolve().parent.parent.parent

        self.config = config
        self.event_bus = event_bus
        self.tools: List[CanonicalTool] = []

    def _check_gates(self, metadata: dict, skill_name: str) -> bool:
        """Enforces environment, binary, and OS requirements."""
        openclaw_meta = metadata.get("openclaw", {}) if isinstance(metadata, dict) else {}
        requires = openclaw_meta.get("requires", {}) if isinstance(openclaw_meta, dict) else {}

        # 1. Binary requirements
        bins = requires.get("bins") or []
        if isinstance(bins, str):
            bins = [bins]
        for b in bins:
            if not b:
                continue
            if shutil.which(b) is None:
                logger.info(
                    f"OpenClaw: Skipping skill '{skill_name}': required binary '{b}' not found on PATH"
                )
                return False

        # 2. Environment requirements
        envs = requires.get("env") or []
        if isinstance(envs, str):
            envs = [envs]
        for e in envs:
            if not e:
                continue
            if e not in os.environ or not os.environ[e].strip():
                logger.info(
                    f"OpenClaw: Skipping skill '{skill_name}': required env variable '{e}' not set"
                )
                return False

        # 3. OS requirements
        allowed_os = requires.get("os") or []
        if isinstance(allowed_os, str):
            allowed_os = [allowed_os]
        if allowed_os:
            current_os = sys.platform.lower()
            aliases = [current_os]
            if current_os.startswith("win"):
                aliases.extend(["win", "win32", "windows"])
            elif current_os.startswith("linux"):
                aliases.extend(["linux", "unix"])
            elif current_os.startswith("darwin"):
                aliases.extend(["darwin", "macos", "mac", "osx"])

            allowed_set = {str(o).lower() for o in allowed_os}
            if not any(alias in allowed_set for alias in aliases):
                logger.info(
                    f"OpenClaw: Skipping skill '{skill_name}': OS '{sys.platform}' not in allowed list {allowed_os}"
                )
                return False

        return True

    def _extract_command(self, body: str, skill_dir: Path, frontmatter: dict) -> str:
        """Extracts command from frontmatter, markdown body, or skill directory contents."""
        # 1. Direct frontmatter command/entrypoint
        if frontmatter.get("command"):
            return str(frontmatter["command"])
        if frontmatter.get("entrypoint"):
            return str(frontmatter["entrypoint"])

        # 2. Labeled command lines in markdown body
        for line in body.splitlines():
            line_str = line.strip()
            match = re.match(
                r"^(?:execute\s+command|command|run|exec)\s*:\s*(.+)$",
                line_str,
                re.IGNORECASE,
            )
            if match:
                return match.group(1).strip()

        # 3. Code fence blocks
        fences = re.findall(
            r"```(?:bash|sh|shell|python|node|powershell|cmd)?\s*\n(.*?)\n```",
            body,
            re.DOTALL,
        )
        for fence in fences:
            for fence_line in fence.splitlines():
                fl = fence_line.strip()
                if fl and not fl.startswith("#"):
                    return fl

        # 4. Any line referencing {baseDir}
        for line in body.splitlines():
            if "{baseDir}" in line or "{base_dir}" in line:
                return line.strip()

        # 5. Fallback: discover standard entry scripts in folder
        candidates = [
            ("calc.py", f"python {{baseDir}}/calc.py"),
            ("main.py", f"python {{baseDir}}/main.py"),
            ("run.py", f"python {{baseDir}}/run.py"),
            ("index.js", f"node {{baseDir}}/index.js"),
            ("main.js", f"node {{baseDir}}/main.js"),
            ("skill.sh", f"bash {{baseDir}}/skill.sh"),
            ("run.sh", f"bash {{baseDir}}/run.sh"),
        ]
        for filename, cmd in candidates:
            if (skill_dir / filename).is_file():
                return cmd

        return ""

    def _create_skill_tool(
        self,
        skill_dir: Path,
        frontmatter: dict,
        body: str,
    ) -> Optional[CanonicalTool]:
        """Creates a CanonicalTool from a skill directory and parsed SKILL.md."""
        name = frontmatter.get("name") or skill_dir.name
        description = frontmatter.get("description") or ""
        metadata = frontmatter.get("metadata") or {}

        # Enforce gates
        if not self._check_gates(metadata, name):
            return None

        # Only an explicit entrypoint makes a skill callable. Markdown is never executed.
        command_template = frontmatter.get("command") or frontmatter.get("entrypoint")
        if not command_template:  # explicit "Execute command: ..." declaration only
            for line in body.splitlines():
                m = re.match(r"^(?:execute\s+command|command)\s*:\s*(.+)$", line.strip(), re.I)
                if m:
                    command_template = m.group(1).strip()
                    break
                if "{baseDir}" in line and not line.lstrip().startswith(("#", "-", "*", ">")):
                    command_template = line.strip()
                    break
        if not command_template:
            logger.debug(f"OpenClaw: No executable command found for skill '{name}' in {skill_dir}")
            return None

        # Replace {baseDir} placeholder with normalized skill directory
        skill_dir_str = str(skill_dir).replace("\\", "/")
        command = (
            command_template.replace("{baseDir}", skill_dir_str)
            .replace("{base_dir}", skill_dir_str)
        )

        parameters = frontmatter.get("parameters") or frontmatter.get("schema") or {
            "type": "object",
            "properties": {
                "expression": {
                    "type": "string",
                    "description": "Expression or arguments for calculation or execution",
                },
                "input": {
                    "type": "string",
                    "description": "Input data or instructions for the skill",
                },
            },
            "additionalProperties": True,
        }

        async def handler(args: dict, context: dict = None) -> str:
            input_json = json.dumps(args if args is not None else {})
            env = os.environ.copy()
            env["SKILL_DIR"] = str(skill_dir)
            env["BASE_DIR"] = str(skill_dir)

            try:
                proc = await asyncio.create_subprocess_shell(
                    command,
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    cwd=str(skill_dir),
                    env=env,
                )
                stdout_data, stderr_data = await proc.communicate(input=input_json.encode("utf-8"))
                out_str = stdout_data.decode("utf-8", errors="replace").strip()
                err_str = stderr_data.decode("utf-8", errors="replace").strip()

                if proc.returncode != 0 and not out_str:
                    return f"Error (exit code {proc.returncode}): {err_str}"
                return out_str
            except Exception as e:
                logger.error(f"Error executing OpenClaw skill '{name}': {e}", exc_info=True)
                return f"Execution error: {e}"

        tool = CanonicalTool(
            name=name,
            description=description,
            parameters=parameters,
            handler=handler,
            source="openclaw",
            metadata={
                "skill_dir": str(skill_dir),
                "command": command,
                "frontmatter": frontmatter,
            },
        )
        return tool

    def discover(self) -> DiscoveryResult:
        """Scans plugins/openclaw/ and skills/ for folders containing SKILL.md."""
        discovered: List[CanonicalTool] = []
        scan_dirs: List[Path] = []

        # Configured or default directories
        openclaw_dir = self.root_dir / "plugins" / "openclaw"
        skills_dir = self.root_dir / "skills"

        if openclaw_dir.exists():
            scan_dirs.append(openclaw_dir)
        if skills_dir.exists():
            scan_dirs.append(skills_dir)
        pi_dir = self.root_dir / "plugins" / "pi"
        if pi_dir.exists():
            scan_dirs.append(pi_dir)

        for base_dir in scan_dirs:
            for skill_file in base_dir.rglob("*"):
                if skill_file.is_file() and skill_file.name.lower() == "skill.md":
                    skill_folder = skill_file.parent
                    try:
                        content = skill_file.read_text(encoding="utf-8", errors="replace")
                        frontmatter, body = parse_frontmatter(content)
                        tool = self._create_skill_tool(skill_folder, frontmatter, body)
                        if tool is not None:
                            discovered.append(tool)
                    except Exception as e:
                        logger.error(f"OpenClaw: Failed to parse skill at {skill_file}: {e}", exc_info=True)

        self.tools = discovered
        return DiscoveryResult(discovered)
