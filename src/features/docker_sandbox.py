"""
Ephemeral Docker sandbox (OpenClaw ``sandbox``).

Runs a command in a disposable container with resource limits. **Fails closed**: when
Docker is unavailable the command is *refused* instead of silently running on the host.
Host execution is possible only when the caller passes ``allow_host=True`` (the
``/sandbox --host`` flag), and the result always says whether isolation was applied.
"""

from __future__ import annotations

from dataclasses import dataclass
import logging
import os
import shlex
import shutil
import subprocess
import time
from typing import List, Optional, Sequence, Union

logger = logging.getLogger(__name__)

Command = Union[str, Sequence[str]]


@dataclass
class SandboxConfig:
    memory_limit: str = "512m"
    cpu_limit: str = "1.0"
    timeout_sec: int = 30
    network_mode: str = "none"  # safest default; pass "bridge" to allow network
    read_only_root: bool = True
    image: str = "python:3.12-slim"


@dataclass
class SandboxExecutionResult:
    stdout: str
    stderr: str
    exit_code: int
    duration_s: float
    isolated_in_docker: bool
    refused: bool = False


def _argv(command: Command) -> List[str]:
    if isinstance(command, str):
        return shlex.split(command, posix=True)
    return list(command)


class DockerSandboxRunner:
    REFUSED_CODE = 126

    def __init__(self, config: Optional[SandboxConfig] = None):
        self.config = config or SandboxConfig()
        self._docker_available: Optional[bool] = None

    def is_docker_active(self, refresh: bool = False) -> bool:
        if self._docker_available is None or refresh:
            self._docker_available = self._probe_docker()
        return self._docker_available

    @staticmethod
    def _probe_docker() -> bool:
        if not shutil.which("docker"):
            return False
        try:
            return subprocess.run(["docker", "info"], capture_output=True, timeout=5).returncode == 0
        except Exception:
            return False

    def docker_command(self, argv: List[str], cwd: Optional[str] = None) -> List[str]:
        cfg = self.config
        cmd = [
            "docker", "run", "--rm",
            f"--memory={cfg.memory_limit}", f"--cpus={cfg.cpu_limit}",
            f"--network={cfg.network_mode}", "--pids-limit=256", "--cap-drop=ALL",
            "--security-opt=no-new-privileges",
        ]
        if cfg.read_only_root:
            cmd += ["--read-only", "--tmpfs", "/tmp:rw,size=64m"]
        if cwd:
            cmd += ["-v", f"{os.path.abspath(cwd)}:/workspace:ro", "-w", "/workspace"]
        return cmd + [cfg.image] + argv

    def run(self, command: Command, cwd: Optional[str] = None, allow_host: bool = False) -> SandboxExecutionResult:
        started = time.perf_counter()
        argv = _argv(command)
        if not argv:
            return SandboxExecutionResult("", "empty command", 2, 0.0, False, refused=True)

        if self.is_docker_active():
            return self._exec(self.docker_command(argv, cwd), None, started, isolated=True)

        if not allow_host:
            return SandboxExecutionResult(
                "",
                "Docker is not available, so the command was NOT run. Install/start Docker Desktop, "
                "or re-run with explicit host execution (/sandbox --host <cmd>) if you trust the command.",
                self.REFUSED_CODE, round(time.perf_counter() - started, 3), False, refused=True,
            )
        logger.warning("Sandbox: running %r on the HOST without isolation (allow_host=True)", argv)
        return self._exec(argv, cwd, started, isolated=False)

    def _exec(self, argv: List[str], cwd: Optional[str], started: float, isolated: bool) -> SandboxExecutionResult:
        try:
            proc = subprocess.run(argv, cwd=cwd, capture_output=True, text=True, timeout=self.config.timeout_sec)
            return SandboxExecutionResult(
                proc.stdout or "", proc.stderr or "", proc.returncode,
                round(time.perf_counter() - started, 3), isolated,
            )
        except subprocess.TimeoutExpired:
            return SandboxExecutionResult(
                "", f"Timed out after {self.config.timeout_sec}s", 124,
                round(time.perf_counter() - started, 3), isolated,
            )
        except FileNotFoundError as e:
            return SandboxExecutionResult("", str(e), 127, round(time.perf_counter() - started, 3), isolated)
