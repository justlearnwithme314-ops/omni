"""
Declarative Skill Dependency Auto-Installer (OpenClaw Architecture Standout)
"""

import json
from pathlib import Path
import shutil
import subprocess
import sys
from typing import Any, Dict, List, Optional, Tuple
from ..paths import PROJECT_ROOT


class SkillDependencyInstaller:
    def __init__(self, workspace_root: Optional[str] = None):
        self.workspace_root = Path(workspace_root or PROJECT_ROOT).resolve()

    def detect_package_managers(self) -> Dict[str, Optional[str]]:
        return {
            "pip": sys.executable,
            "npm": shutil.which("npm"),
            "uv": shutil.which("uv"),
            "brew": shutil.which("brew"),
        }

    def inspect_skill_dependencies(self, skill_md_path: Path) -> Dict[str, List[str]]:
        if not skill_md_path.is_file():
            return {}

        try:
            content = skill_md_path.read_text(encoding="utf-8", errors="replace")
            if content.startswith("---"):
                parts = content.split("---", 2)
                if len(parts) >= 3:
                    import yaml
                    data = yaml.safe_load(parts[1]) or {}
                    metadata = data.get("metadata", {})
                    openclaw_meta = metadata.get("openclaw", {})
                    return openclaw_meta.get("install", {})
        except Exception:
            pass
        return {}

    def install_dependencies(
        self,
        skill_dir: Path,
        install_specs: Dict[str, List[str]],
        dry_run: bool = False,
    ) -> Tuple[bool, str]:
        pms = self.detect_package_managers()
        logs = []

        for pm, packages in install_specs.items():
            if not packages:
                continue

            pm_lower = pm.lower()
            if pm_lower in ("pip", "python"):
                cmd = [pms["pip"], "-m", "pip", "install"] + list(packages)
            elif pm_lower == "npm":
                if not pms["npm"]:
                    return False, f"npm is required to install {packages} but was not found on PATH."
                cmd = ["npm", "install", "-g"] + list(packages)
            elif pm_lower == "uv":
                if not pms["uv"]:
                    cmd = [pms["pip"], "-m", "pip", "install"] + list(packages)
                else:
                    cmd = ["uv", "pip", "install"] + list(packages)
            elif pm_lower == "brew":
                if not pms["brew"]:
                    return False, f"Homebrew is not installed for {packages}."
                cmd = ["brew", "install"] + list(packages)
            else:
                continue

            if dry_run:
                logs.append(f"[Dry-run] Would execute: {' '.join(cmd)}")
                continue

            try:
                res = subprocess.run(cmd, cwd=skill_dir, capture_output=True, text=True, timeout=120)
                if res.returncode == 0:
                    logs.append(f"[✓] Installed {pm} packages: {', '.join(packages)}")
                else:
                    return False, f"Failed: {res.stderr.strip() or res.stdout.strip()}"
            except Exception as e:
                return False, f"Error executing {cmd[0]}: {e}"

        return True, "\n".join(logs)
