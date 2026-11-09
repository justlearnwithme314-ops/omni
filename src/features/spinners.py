"""
Live Streaming Spinners & Token Rate Counters (OMP & Hermes Style)
Provides dynamic braille/arc spinners (⠋, ⠙, ⠹) tracking elapsed time and tokens/sec.
"""

import asyncio
import sys
import threading
import time
from typing import Optional


SPINNER_FRAMES = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]


class LiveStreamingSpinner:
    """Non-blocking terminal spinner with live token counter and elapsed metrics."""

    def __init__(self, message: str = "Thinking..."):
        self.message = message
        self.running = False
        self.start_time: float = 0.0
        self.token_count: int = 0
        self._thread: Optional[threading.Thread] = None

    def start(self, message: Optional[str] = None):
        if message:
            self.message = message
        self.running = True
        self.start_time = time.time()
        self.token_count = 0
        self._thread = threading.Thread(target=self._spin, daemon=True)
        self._thread.start()

    def update(self, token_count: Optional[int] = None, note: Optional[str] = None):
        if token_count is not None:
            self.token_count = token_count
        if note is not None:
            self.message = note

    def stop(self, final_message: str = ""):
        self.running = False
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=0.5)

        # Clear spinner line
        elapsed = time.time() - self.start_time if self.start_time else 0.0
        rate_str = f" ({self.token_count / elapsed:.1f} tok/s)" if elapsed > 0 and self.token_count > 0 else ""
        sys.stdout.write(f"\r\033[K")
        sys.stdout.flush()
        if final_message:
            print(f"{final_message} [{elapsed:.2f}s{rate_str}]")

    def _spin(self):
        idx = 0
        while self.running:
            elapsed = time.time() - self.start_time
            frame = SPINNER_FRAMES[idx % len(SPINNER_FRAMES)]
            rate_str = f" | {self.token_count / elapsed:.1f} tok/s" if elapsed > 0.5 and self.token_count > 0 else ""
            line = f"\r\033[93m{frame}\033[0m {self.message} [\033[90m{elapsed:.1f}s{rate_str}\033[0m]"
            sys.stdout.write(line)
            sys.stdout.flush()
            idx += 1
            time.sleep(0.08)
