"""
Local open-weights engine (llama.cpp ``llama-server`` / vLLM) for Hermes-style models.

Launches a real server process and talks to its OpenAI-compatible HTTP API. Nothing is
simulated: when the binary or a model file is missing, :class:`LocalEngineError` says
exactly what to install. Hermes ``<tool_call>`` XML in replies is parsed with the same
parser the cloud providers use (``src.providers.client.parse_hermes_tool_calls``).
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import logging
import os
import shutil
import subprocess
import time
from typing import Any, Dict, List, Optional
import urllib.error
import urllib.request
from ..paths import user_data_dir

logger = logging.getLogger(__name__)


class LocalEngineError(RuntimeError):
    pass


@dataclass
class LocalModelConfig:
    backend: str = "llama.cpp"  # "llama.cpp" or "vllm"
    model_path: str = ""
    gpu_layers: int = 99
    context_size: int = 8192
    host: str = "127.0.0.1"
    port: int = 8080
    temperature: float = 0.7


def _model_dirs() -> List[str]:
    root = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
    return [
        os.path.join(root, "models"),
        str(user_data_dir() / "models"),
        os.path.expanduser("~/.cache/llama.cpp"),
        os.path.expanduser("~/.cache/lm-studio/models"),
    ]


class LocalEngineRunner:
    def __init__(self, config: Optional[LocalModelConfig] = None):
        self.config = config or LocalModelConfig()
        self.process: Optional[subprocess.Popen] = None

    @property
    def base_url(self) -> str:
        return f"http://{self.config.host}:{self.config.port}"

    def list_available_models(self) -> List[Dict[str, Any]]:
        """GGUF files that actually exist on disk (empty list if none)."""
        found: List[Dict[str, Any]] = []
        for folder in _model_dirs():
            for root, _, files in os.walk(folder) if os.path.isdir(folder) else []:
                for f in files:
                    if f.lower().endswith(".gguf"):
                        path = os.path.join(root, f)
                        found.append({"name": f, "path": path, "size_mb": round(os.path.getsize(path) / 1048576, 1)})
        return found

    def binary(self) -> Optional[str]:
        return shutil.which("llama-server" if self.config.backend == "llama.cpp" else "vllm")

    def is_running(self) -> bool:
        try:
            with urllib.request.urlopen(f"{self.base_url}/health", timeout=1.5) as r:
                return r.status == 200
        except Exception:
            return False

    def start(self, model_path: Optional[str] = None, wait: float = 60.0) -> str:
        if self.is_running():
            return f"Server already running on {self.base_url}"
        exe = self.binary()
        if not exe:
            hint = "winget install llama.cpp  (or download a release from github.com/ggml-org/llama.cpp)" \
                if self.config.backend == "llama.cpp" else "pip install vllm"
            raise LocalEngineError(f"'{self.config.backend}' server binary not found on PATH. Install it: {hint}")
        model = model_path or self.config.model_path
        if not model:
            models = self.list_available_models()
            if not models:
                raise LocalEngineError(
                    "No .gguf model found. Put one in ~/.omni/models (e.g. a Hermes 3 GGUF from huggingface.co/NousResearch)."
                )
            model = models[0]["path"]
        if not os.path.exists(model):
            raise LocalEngineError(f"Model file not found: {model}")

        self.process = subprocess.Popen(
            [exe, "-m", model, "-ngl", str(self.config.gpu_layers), "-c", str(self.config.context_size),
             "--host", self.config.host, "--port", str(self.config.port)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        deadline = time.monotonic() + wait
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                raise LocalEngineError(f"Server exited immediately (code {self.process.returncode}); check the model/flags.")
            if self.is_running():
                return f"Started {os.path.basename(exe)} with {os.path.basename(model)} on {self.base_url}"
            time.sleep(0.5)
        raise LocalEngineError(f"Server did not become ready within {wait:.0f}s")

    def stop(self) -> None:
        if self.process and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
        self.process = None

    def query(self, messages: List[Dict[str, Any]], tools: Optional[List[Dict[str, Any]]] = None,
              timeout: float = 120.0) -> Dict[str, Any]:
        """Chat completion. Returns the assistant message, with ``tool_calls`` filled from Hermes XML if present."""
        if not self.is_running():
            raise LocalEngineError("Local engine is not running. Start it with /local start")
        body: Dict[str, Any] = {"messages": messages, "temperature": self.config.temperature}
        if tools:
            body["tools"] = tools
        req = urllib.request.Request(
            f"{self.base_url}/v1/chat/completions", data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                message = json.loads(resp.read().decode())["choices"][0]["message"]
        except urllib.error.URLError as e:
            raise LocalEngineError(f"Local engine request failed: {e}") from e

        if not message.get("tool_calls") and "<tool_call>" in (message.get("content") or ""):
            from ..providers.client import parse_hermes_tool_calls  # reuse the shared Hermes parser
            cleaned, calls = parse_hermes_tool_calls(message["content"])
            message["content"], message["tool_calls"] = cleaned, calls
        return message
