"""
Terminal Audio Chimes & Notifications (/tan, omp say Standout)
Plays lightweight, non-blocking system chimes or local audio bells upon
turn completions, long-running test suite finishes, or user alerts.
"""

import sys
import threading
from typing import Optional


class AudioNotifier:
    """Non-blocking audio alert and chime engine."""

    @staticmethod
    def play_chime(kind: str = "success"):
        """Plays a non-blocking system notification chime."""
        def _beep():
            try:
                if sys.platform == "win32":
                    import winsound
                    if kind == "success":
                        winsound.MessageBeep(winsound.MB_OK)
                    elif kind == "error":
                        winsound.MessageBeep(winsound.MB_ICONHAND)
                    elif kind == "alert":
                        winsound.MessageBeep(winsound.MB_ICONEXCLAMATION)
                    else:
                        winsound.Beep(880, 150)
                else:
                    # ANSI bell on Unix
                    sys.stdout.write("\a")
                    sys.stdout.flush()
            except Exception:
                pass

        threading.Thread(target=_beep, daemon=True).start()

    @staticmethod
    def say(text: str):
        """Synthesizes text through local OS speech synthesis if available."""
        def _speak():
            try:
                if sys.platform == "win32":
                    import subprocess
                    ps_cmd = f"Add-Type -AssemblyName System.Speech; (New-Object System.Speech.Synthesis.SpeechSynthesizer).Speak('{text[:200]}')"
                    subprocess.run(["powershell", "-Command", ps_cmd], capture_output=True, timeout=5)
            except Exception:
                pass

        threading.Thread(target=_speak, daemon=True).start()
