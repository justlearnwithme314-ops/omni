"""
Pluggable Execution Environments (Hermes Architecture Standout)
Abstracts execution across Local Host, Docker, Remote SSH, and Cloud Sandbox backends.
"""

from abc import ABC, abstractmethod
import os
from pathlib import Path
import shutil
import subprocess
from typing import Dict, Optional, Tuple


class Environment(ABC):
    """Abstract base class for all execution backends."""

    @abstractmethod
    def execute(self, command: str, cwd: Optional[Path] = None, timeout: float = 60.0) -> Tuple[int, str, str]:
        """Execute a shell command and return (returncode, stdout, stderr)."""
        pass

    @abstractmethod
    def is_available(self) -> bool:
        """Returns True if the execution environment is currently accessible."""
        pass


class LocalEnvironment(Environment):
    """Host execution environment using local subprocess."""

    def __init__(self, workspace_root: Optional[Path] = None):
        self.workspace_root = workspace_root or Path.cwd()

    def is_available(self) -> bool:
        return True

    def execute(self, command: str, cwd: Optional[Path] = None, timeout: float = 60.0) -> Tuple[int, str, str]:
        target_dir = cwd or self.workspace_root
        try:
            res = subprocess.run(
                command,
                shell=True,
                cwd=target_dir,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
            return res.returncode, res.stdout, res.stderr
        except subprocess.TimeoutExpired:
            return -1, "", f"Command timed out after {timeout} seconds."
        except Exception as e:
            return -1, "", str(e)


class DockerEnvironment(Environment):
    """Ephemeral Docker container execution environment."""

    def __init__(self, workspace_root: Optional[Path] = None, image: str = "alpine:latest"):
        self.workspace_root = workspace_root or Path.cwd()
        self.image = image

    def is_available(self) -> bool:
        if not shutil.which("docker"):
            return False
        try:
            res = subprocess.run(["docker", "info"], capture_output=True, timeout=3)
            return res.returncode == 0
        except Exception:
            return False

    def execute(self, command: str, cwd: Optional[Path] = None, timeout: float = 60.0) -> Tuple[int, str, str]:
        if not self.is_available():
            return -1, "", "Docker daemon is not available."

        docker_cmd = [
            "docker", "run", "--rm",
            "-v", f"{self.workspace_root}:/workspace",
            "-w", "/workspace",
            self.image,
            "sh", "-c", command,
        ]
        try:
            res = subprocess.run(docker_cmd, capture_output=True, text=True, timeout=timeout)
            return res.returncode, res.stdout, res.stderr
        except Exception as e:
            return -1, "", f"Docker execution error: {e}"


class SSHEnvironment(Environment):
    """Remote server execution over SSH tunnels."""

    def __init__(self, host: str, user: str = "root", port: int = 22, key_path: Optional[str] = None):
        self.host = host
        self.user = user
        self.port = port
        self.key_path = key_path

    def is_available(self) -> bool:
        return shutil.which("ssh") is not None

    def execute(self, command: str, cwd: Optional[Path] = None, timeout: float = 60.0) -> Tuple[int, str, str]:
        if not self.is_available():
            return -1, "", "SSH client not found on PATH."

        ssh_cmd = ["ssh", "-p", str(self.port), f"{self.user}@{self.host}"]
        if self.key_path:
            ssh_cmd.extend(["-i", self.key_path])
        ssh_cmd.append(command)

        try:
            res = subprocess.run(ssh_cmd, capture_output=True, text=True, timeout=timeout)
            return res.returncode, res.stdout, res.stderr
        except Exception as e:
            return -1, "", f"SSH error: {e}"


class EnvironmentManager:
    """Manages active execution environment selection."""

    def __init__(self, workspace_root: Optional[Path] = None, default_env: str = "local"):
        self.workspace_root = workspace_root or Path.cwd()
        self.environments: Dict[str, Environment] = {
            "local": LocalEnvironment(self.workspace_root),
            "docker": DockerEnvironment(self.workspace_root),
        }
        self.active_env_name = default_env

    def get_active(self) -> Environment:
        env = self.environments.get(self.active_env_name)
        if env and env.is_available():
            return env
        return self.environments["local"]

    def set_environment(self, name: str) -> bool:
        if name in self.environments:
            self.active_env_name = name
            return True
        return False
