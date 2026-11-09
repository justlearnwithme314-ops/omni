"""
Voice output (and the future wake-word loop).

Text-to-speech is real: it uses Windows SAPI through PowerShell (text is passed via an
environment variable, never interpolated into the script, so it cannot inject commands).
Speech *input* / wake-word listening is NOT implemented: ``start_listening`` raises with
the packages that would be required, rather than pretending to listen.
"""

from __future__ import annotations

from dataclasses import dataclass
import logging
import os
import re
import shutil
import subprocess
import sys
from typing import Optional

logger = logging.getLogger(__name__)


class VoiceUnavailable(RuntimeError):
    pass


@dataclass
class VoiceConfig:
    wake_word: str = "hey omni"
    rate: int = 0  # SAPI rate, -10..10


_PS_SCRIPT = (
    "Add-Type -AssemblyName System.Speech; "
    "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer; "
    "$s.Rate = [int]$env:OMNI_TTS_RATE; "
    "$s.Speak($env:OMNI_TTS_TEXT)"
)


class VoiceLoopManager:
    def __init__(self, config: Optional[VoiceConfig] = None):
        self.config = config or VoiceConfig()
        self.is_listening = False  # always False until STT is implemented

    @staticmethod
    def clean_for_speech(text: str) -> str:
        text = re.sub(r"```.*?```", " code block ", text, flags=re.S)
        return re.sub(r"[`*#_>]", "", text).strip()[:1000]

    def tts_available(self) -> bool:
        return sys.platform == "win32" and shutil.which("powershell") is not None

    def speak(self, text: str, block: bool = False) -> bool:
        """Speak text aloud. Returns False if no TTS engine is available."""
        if not self.tts_available():
            return False
        spoken = self.clean_for_speech(text)
        if not spoken:
            return False
        env = dict(os.environ, OMNI_TTS_TEXT=spoken, OMNI_TTS_RATE=str(self.config.rate))
        proc = subprocess.Popen(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", _PS_SCRIPT],
            env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        if block:
            proc.wait(timeout=120)
        return True

    def start_listening(self) -> None:
        raise VoiceUnavailable(
            "Wake-word / speech-to-text is not implemented yet. It needs a microphone + STT stack, "
            "e.g. `pip install faster-whisper sounddevice`. Text-to-speech works: /voice say <text>"
        )

    def stop(self) -> None:
        self.is_listening = False
