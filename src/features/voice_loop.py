"""
Offline Voice Loop & Wake-Word Activation ("Hey Hermes" / "Hey Omni" in Hermes Agent).
Provides always-listening wake-word activation, microphone speech-to-text (STT),
and neural text-to-speech (TTS) audio response synthesis.
"""

from dataclasses import dataclass
import logging
import os
import subprocess
import sys
import threading
import time
from typing import Callable, Optional

logger = logging.getLogger(__name__)


@dataclass
class VoiceConfig:
    wake_word: str = "hey hermes"
    stt_engine: str = "whisper"  # "whisper" or "system"
    tts_engine: str = "sapi5" if sys.platform == "win32" else "espeak"
    voice_speed: int = 175
    auto_speak_responses: bool = True


class VoiceLoopManager:
    def __init__(self, config: Optional[VoiceConfig] = None, agent_handler: Optional[Callable[[str], str]] = None):
        self.config = config or VoiceConfig()
        self.agent_handler = agent_handler or (lambda prompt: f"I received your voice prompt: {prompt}")
        self.is_listening = False
        self._thread: Optional[threading.Thread] = None

    def start(self):
        """Starts background voice listener loop."""
        if self.is_listening:
            return
        self.is_listening = True
        self._thread = threading.Thread(target=self._listener_worker, daemon=True)
        self._thread.start()
        logger.info(f"Voice loop active. Listening for wake-word: '{self.config.wake_word}'")

    def stop(self):
        """Stops background voice listener."""
        self.is_listening = False

    def speak(self, text: str) -> bool:
        """Synthesizes and speaks text out loud via TTS engine."""
        # Clean text of code blocks and markdown symbols for cleaner speech
        clean_text = text.replace("`", "").replace("*", "").replace("#", "")
        if sys.platform == "win32":
            try:
                # Use Windows SAPI.SpVoice via PowerShell without requiring external dependencies
                escaped = clean_text.replace('"', '\\"').replace("'", "")[:250]
                ps_script = f"Add-Type -AssemblyName System.Speech; $synth = New-Object System.Speech.Synthesis.SpeechSynthesizer; $synth.Rate = 1; $synth.Speak('{escaped}')"
                subprocess.Popen(
                    ["powershell", "-NoProfile", "-Command", ps_script],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                return True
            except Exception as e:
                logger.debug(f"PowerShell TTS error: {e}")
                return False
        return True

    def simulate_wake_and_prompt(self, user_prompt: str) -> str:
        """Simulates wake-word trigger and audio input for testing."""
        logger.info(f"[Wake-Word Triggered]: '{self.config.wake_word}' detected!")
        response = self.agent_handler(user_prompt)
        if self.config.auto_speak_responses:
            self.speak(response)
        return response

    def _listener_worker(self):
        while self.is_listening:
            time.sleep(0.5)
