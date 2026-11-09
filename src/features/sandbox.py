"""
Docker Container Execution Sandbox (OpenClaw Architecture Standout)
"""

from pathlib import Path
import shutil
import subprocess
from typing import Optional, Tuple


class ExecutionSandbox:
    def __init__(
        self,
        workspace_dir: Optional[str] = None,
        image: str = "alpine:latest",
        memory_limit: str = "512m",
        cpu_limit: str = "1.0",
        prefer_docker: bool = False,
    ):
        self.workspace_dir = Path(workspace_dir or r"D:\Harness\omni").resolve()
        self.image = image
        self.memory_limit = memory_limit
        self.cpu_limit = cpu_limit
        self.prefer_docker = prefer_docker
        self._docker_available = self._check_docker()

    def _check_docker(self) -> bool:
        if not shutil.which("docker"):
            return False
        try:
            res = subprocess.run(["docker", "info"], capture_output=True, timeout=3)
            return res.returncode == 0
        except Exception:
            return False

    def is_docker_available(self) -> bool:
        return self._docker_available

    def execute(
        self,
        command: str,
        cwd: Optional[Path] = None,
        timeout: float = 60.0,
    ) -> Tuple[int, str, str]:
        work_dir = cwd or self.workspace_dir

        if self.prefer_docker and self._docker_available:
            docker_cmd = [
                "docker", "run", "--rm",
                "-v", f"{self.workspace_dir}:/workspace",
                "-w", "/workspace",
                f"--memory={self.memory_limit}",
                f"--cpus={self.cpu_limit}",
                self.image,
                "sh", "-c", command,
            ]
            try:
                res = subprocess.run(docker_cmd, capture_output=True, text=True, timeout=timeout)
                return res.returncode, res.stdout, res.stderr
            except Exception as e:
                return -1, "", f"Docker execution error: {e}"

        try:
            res = subprocess.run(command, shell=True, cwd=work_dir, capture_output=True, text=True, timeout=timeout)
            return res.returncode, res.stdout, res.stderr
        except subprocess.TimeoutExpired:
            return -1, "", f"Command timed out after {timeout} seconds."
        except Exception as e:
            return -1, "", str(e)
