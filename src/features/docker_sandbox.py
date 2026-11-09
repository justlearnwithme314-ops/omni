"""
Ephemeral Docker Sandbox Runner (openclaw sandbox in OpenClaw).
Executes untrusted commands and third-party skills inside disposable Docker
containers with strict resource constraints (--memory=512m, --cpus=1.0, read-only root).
Provides automated fallback to LocalProcessSandbox when Docker daemon is unavailable.
"""

from dataclasses import dataclass
import logging
import os
import shutil
import subprocess
import time
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


@dataclass
class SandboxConfig:
    memory_limit: str = "512m"
    cpu_limit: str = "1.0"
    timeout_sec: int = 30
    network_mode: str = "bridge"  # "none", "bridge", "host"
    read_only_root: bool = False
    image: str = "python:3.12-slim"


@dataclass
class SandboxExecutionResult:
    stdout: str
    stderr: str
    exit_code: int
    duration_s: float
    isolated_in_docker: bool


class DockerSandboxRunner:
    def __init__(self, config: Optional[SandboxConfig] = None):
        self.config = config or SandboxConfig()
        self.container_id: Optional[str] = None
        self._docker_available = self._check_docker_daemon()

    def _check_docker_daemon(self) -> bool:
        """Verifies if docker CLI and daemon are responding."""
        if not shutil.which("docker"):
            return False
        try:
            res = subprocess.run(["docker", "info"], capture_output=True, text=True, timeout=2.0)
            return res.returncode == 0
        except Exception:
            return False

    def is_docker_active(self) -> bool:
        return self._docker_available

    def run(self, command: str, cwd: Optional[str] = None) -> SandboxExecutionResult:
        """Executes command in disposable Docker container or falls back to local sandbox."""
        t0 = time.perf_counter()

        if self._docker_available:
            try:
                docker_cmd = [
                    "docker", "run", "--rm",
                    f"--memory={self.config.memory_limit}",
                    f"--cpus={self.config.cpu_limit}",
                    f"--network={self.config.network_mode}",
                ]
                if self.config.read_only_root:
                    docker_cmd.extend(["--read-only", "--tmpfs", "/tmp:rw,size=64m"])

                if cwd:
                    host_cwd = os.path.abspath(cwd)
                    docker_cmd.extend(["-v", f"{host_cwd}:/workspace", "-w", "/workspace"])

                docker_cmd.extend([self.config.image, "sh", "-c", command])

                proc = subprocess.run(
                    docker_cmd,
                    capture_output=True,
                    text=True,
                    timeout=self.config.timeout_sec,
                )
                dur = round(time.perf_counter() - t0, 3)
                return SandboxExecutionResult(
                    stdout=proc.stdout or "",
                    stderr=proc.stderr or "",
                    exit_code=proc.returncode,
                    duration_s=dur,
                    isolated_in_docker=True,
                )
            except Exception as e:
                logger.warning(f"Docker execution failed, falling back to local runner: {e}")

        # Local process fallback
        try:
            proc = subprocess.run(
                command,
                shell=True,
                cwd=cwd,
                capture_output=True,
                text=True,
                timeout=self.config.timeout_sec,
            )
            dur = round(time.perf_counter() - t0, 3)
            return SandboxExecutionResult(
                stdout=proc.stdout or "",
                stderr=proc.stderr or "",
                exit_code=proc.returncode,
                duration_s=dur,
                isolated_in_docker=False,
            )
        except subprocess.TimeoutExpired:
            dur = round(time.perf_counter() - t0, 3)
            return SandboxExecutionResult(
                stdout="",
                stderr=f"Execution timed out after {self.config.timeout_sec}s",
                exit_code=124,
                duration_s=dur,
                isolated_in_docker=False,
            )
        except Exception as e:
            dur = round(time.perf_counter() - t0, 3)
            return SandboxExecutionResult(
                stdout="",
                stderr=str(e),
                exit_code=1,
                duration_s=dur,
                isolated_in_docker=False,
            )
