"""Push-to-talk voice input: record from the microphone, transcribe with the `voice` capability, put the words in
the editor.

The standard library cannot capture audio, so an external recorder is used. Detection order:

    1. the profile's `"voice": {"record_command": ["prog", "arg", "{out}"]}`   ({out} = the .wav file to write)
    2. ffmpeg     Windows: -f dshow (first audio capture device)   macOS: -f avfoundation ":0"
                  Linux: -f pulse default (when `pactl` is installed), otherwise -f alsa default
    3. rec / sox  (SoX's default audio device)
    4. arecord    (Linux, ALSA)

Audio is recorded as 16 kHz mono 16-bit WAV into a temporary folder that is removed afterwards.

`Recorder` runs one recording. `VoiceController` is the push-to-talk state machine the terminal app drives (one key
starts and stops, Escape cancels); it knows nothing about the terminal, so it can be tested on its own. `dictate`
is the `/dictate [seconds]` command.
"""
from __future__ import annotations

import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

MAX_SECONDS = 120           # a recording stops by itself after this long
STOP_TIMEOUT = 5.0          # seconds a recorder gets to finish its file before it is killed
LIST_TIMEOUT = 15
DICTATE_DEFAULT = 10
SAMPLE_RATE = "16000"
INSTALL_HINT = ("No audio recorder found. Install ffmpeg (https://ffmpeg.org; Windows: `winget install ffmpeg`, "
                "macOS: `brew install ffmpeg`, Linux: your package manager) or SoX, or on Linux alsa-utils (arecord). "
                "Or set the profile's \"voice\": {\"record_command\": [\"program\", \"args\", \"{out}\"]}.")


class RecorderError(Exception):
    """A recording could not be started or produced no audio. The message is meant for the user."""


def _platform() -> str:
    return "windows" if sys.platform.startswith("win") else "mac" if sys.platform == "darwin" else "linux"


def _no_window() -> int:
    return getattr(subprocess, "CREATE_NO_WINDOW", 0) if _platform() == "windows" else 0


# ------------------------------------------------------------------------------------------- finding a recorder
_DSHOW_LINE = re.compile(r'^\[dshow @ [^\]]*\]\s*(.*)$')


def parse_dshow_audio_devices(stderr: str) -> List[str]:
    """The audio capture devices in the stderr of `ffmpeg -list_devices true -f dshow -i dummy`, in listed order.

    Understands both layouts: newer ffmpeg tags each device (`"Name" (audio)`), older ffmpeg lists them under a
    `DirectShow audio devices` heading. `Alternative name` lines are skipped."""
    found: List[str] = []
    section = ""
    for raw in stderr.splitlines():
        m = _DSHOW_LINE.match(raw.strip())
        if not m:
            continue
        body = m.group(1).strip()
        low = body.lower()
        if low.startswith("directshow video devices"):
            section = "video"
            continue
        if low.startswith("directshow audio devices"):
            section = "audio"
            continue
        if low.startswith("alternative name"):
            continue
        dm = re.match(r'^"(.+)"\s*(?:\((audio|video|none|audio, video|video, audio)\))?\s*$', body)
        if not dm:
            continue
        name, tag = dm.group(1), (dm.group(2) or "")
        if ("audio" in tag) if tag else section == "audio":
            if name not in found:
                found.append(name)
    return found


def dshow_default_device(ffmpeg: str) -> str:
    """The first DirectShow audio capture device (Windows lists the default one first)."""
    try:
        done = subprocess.run([ffmpeg, "-hide_banner", "-list_devices", "true", "-f", "dshow", "-i", "dummy"],
                              stdin=subprocess.DEVNULL, capture_output=True, timeout=LIST_TIMEOUT,
                              creationflags=_no_window())
    except (OSError, subprocess.TimeoutExpired) as e:
        raise RecorderError(f"could not ask ffmpeg for the audio devices: {e}") from None
    err = done.stderr or b""
    err = err.decode("utf-8", "replace") if isinstance(err, bytes) else str(err)
    devices = parse_dshow_audio_devices(err)
    if not devices:
        raise RecorderError("ffmpeg found no microphone (no DirectShow audio device). Plug one in or set "
                            "\"voice\": {\"record_command\": [...]} in the profile.")
    return devices[0]


def _custom(command: Any) -> List[str]:
    if isinstance(command, str):
        argv = shlex.split(command, posix=_platform() != "windows")
    elif isinstance(command, list) and all(isinstance(a, str) for a in command):
        argv = list(command)
    else:
        raise RecorderError("voice.record_command must be a list of strings, e.g. [\"rec\", \"-q\", \"{out}\"]")
    if not argv or not argv[0].strip():
        raise RecorderError("voice.record_command is empty")
    if not any("{out}" in a for a in argv):
        argv.append("{out}")
    return argv


def recorder_command(config: Optional[Dict[str, Any]] = None, max_seconds: float = MAX_SECONDS) -> Dict[str, Any]:
    """{"argv": [... "{out}" ...], "kind": "custom" | "ffmpeg" | "sox" | "arecord"} for this machine.
    Raises RecorderError naming what to install when there is none."""
    config = config or {}
    if config.get("record_command"):
        argv = _custom(config["record_command"])
        kind = "ffmpeg" if Path(argv[0]).stem.lower() == "ffmpeg" else "custom"
        return {"argv": argv, "kind": kind}
    secs = str(int(max_seconds)) if float(max_seconds) == int(max_seconds) else f"{float(max_seconds):.1f}"
    plat = _platform()
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg:
        if plat == "windows":
            source = ["-f", "dshow", "-i", "audio=" + dshow_default_device(ffmpeg)]
        elif plat == "mac":
            source = ["-f", "avfoundation", "-i", ":0"]
        elif shutil.which("pactl"):
            source = ["-f", "pulse", "-i", "default"]
        else:
            source = ["-f", "alsa", "-i", "default"]
        return {"kind": "ffmpeg",
                "argv": [ffmpeg, "-hide_banner", "-loglevel", "error", "-y", *source, "-t", secs,
                         "-ac", "1", "-ar", SAMPLE_RATE, "-c:a", "pcm_s16le", "{out}"]}
    rec = shutil.which("rec")
    if rec:
        return {"kind": "sox", "argv": [rec, "-q", "-r", SAMPLE_RATE, "-c", "1", "-b", "16", "{out}", "trim", "0", secs]}
    sox = shutil.which("sox")
    if sox:
        device = ["-t", "waveaudio", "default"] if plat == "windows" else ["-d"]
        return {"kind": "sox",
                "argv": [sox, "-q", *device, "-r", SAMPLE_RATE, "-c", "1", "-b", "16", "{out}", "trim", "0", secs]}
    if plat == "linux":
        arecord = shutil.which("arecord")
        if arecord:
            return {"kind": "arecord", "argv": [arecord, "-q", "-f", "S16_LE", "-r", SAMPLE_RATE, "-c", "1", "-t", "wav",
                                                "-d", str(max(1, int(float(max_seconds)))), "{out}"]}
    raise RecorderError(INSTALL_HINT)


# ------------------------------------------------------------------------------------------- one recording
class Recorder:
    """One recording into a temporary .wav. `start()`, then `stop()` (returns the file) or `cancel()`.

    After `max_seconds` the recording ends by itself: `on_limit()` is called (from a timer thread) when given,
    otherwise the recorder is stopped and `stop()` later just returns the file. `cleanup()` removes the file."""

    def __init__(self, config: Optional[Dict[str, Any]] = None, max_seconds: Optional[float] = None,
                 on_limit: Optional[Callable[[], None]] = None, stop_timeout: float = STOP_TIMEOUT):
        self.config = config or {}
        limit = max_seconds if max_seconds is not None else self.config.get("max_record_seconds", MAX_SECONDS)
        try:
            self.max_seconds = max(0.01, float(limit))
        except (TypeError, ValueError):
            self.max_seconds = float(MAX_SECONDS)
        self.on_limit = on_limit
        self.stop_timeout = stop_timeout
        self.proc: Optional[Any] = None
        self.kind = ""
        self.argv: List[str] = []
        self.folder: Optional[Path] = None
        self.path: Optional[Path] = None
        self._log: Optional[Any] = None
        self._timer: Optional[threading.Timer] = None
        self._lock = threading.Lock()
        self._stopped = False
        self.timed_out = False

    @property
    def recording(self) -> bool:
        return self.proc is not None and not self._stopped

    def start(self) -> None:
        if self.proc is not None:
            raise RecorderError("already recording")
        found = recorder_command(self.config, self.max_seconds)
        self.kind = found["kind"]
        self.folder = Path(tempfile.mkdtemp(prefix="omni-voice-rec-"))
        self.path = self.folder / "speech.wav"
        self.argv = [a.replace("{out}", str(self.path)) for a in found["argv"]]
        self._log = open(self.folder / "recorder.log", "wb")
        try:
            self.proc = subprocess.Popen(self.argv, stdin=subprocess.PIPE if self.kind == "ffmpeg" else subprocess.DEVNULL,
                                         stdout=subprocess.DEVNULL, stderr=self._log, creationflags=_no_window())
        except OSError as e:
            self.cleanup()
            raise RecorderError(f"could not start the recorder {Path(self.argv[0]).name}: {e}") from None
        self._timer = threading.Timer(self.max_seconds, self._time_up)
        self._timer.daemon = True
        self._timer.start()

    def _time_up(self) -> None:
        if not self.recording:
            return
        self.timed_out = True
        if self.on_limit is not None:
            self.on_limit()
        else:
            self._halt()

    def _halt(self) -> None:
        """End the recorder process: gracefully first, then terminate, then kill."""
        with self._lock:
            if self._stopped or self.proc is None:
                return
            self._stopped = True
            if self._timer is not None:
                self._timer.cancel()
            proc = self.proc
            if proc.poll() is None:
                graceful = False
                if self.kind == "ffmpeg" and proc.stdin is not None:
                    try:
                        proc.stdin.write(b"q")
                        proc.stdin.flush()
                        proc.stdin.close()
                        graceful = True
                    except (OSError, ValueError):
                        pass
                if not graceful:
                    try:
                        if _platform() == "windows":
                            proc.terminate()
                        else:
                            proc.send_signal(signal.SIGINT)   # sox and arecord finish the WAV header on SIGINT
                    except OSError:
                        pass
                try:
                    proc.wait(timeout=self.stop_timeout)
                except subprocess.TimeoutExpired:
                    try:
                        proc.kill()
                    except OSError:
                        pass
                    try:
                        proc.wait(timeout=self.stop_timeout)
                    except subprocess.TimeoutExpired:
                        pass
            if self._log is not None:
                try:
                    self._log.close()
                except OSError:
                    pass

    def stop(self) -> Path:
        """Finish the recording and return the .wav. RecorderError when nothing usable was recorded."""
        if self.proc is None or self.path is None:
            raise RecorderError("not recording")
        self._halt()
        try:
            size = self.path.stat().st_size
        except OSError:
            size = 0
        if size <= 44:   # a bare WAV header holds no sound
            said = ""
            try:
                said = (self.folder / "recorder.log").read_text("utf-8", "replace").strip()[-400:]  # type: ignore[union-attr]
            except OSError:
                pass
            raise RecorderError(f"the recorder ({Path(self.argv[0]).name}) produced no audio"
                                + (f": {said}" if said else " - is a microphone connected and allowed?"))
        return self.path

    def cancel(self) -> None:
        """Stop without keeping anything; the temporary file is deleted."""
        self._halt()
        self.cleanup()

    def cleanup(self) -> None:
        if self._timer is not None:
            self._timer.cancel()
        if self._log is not None:
            try:
                self._log.close()
            except OSError:
                pass
        if self.folder is not None:
            shutil.rmtree(self.folder, ignore_errors=True)


# ------------------------------------------------------------------------------------------- push-to-talk
def _voice_config(session: Any) -> Dict[str, Any]:
    cfg = (getattr(session, "profile", None) or {}).get("voice")
    return cfg if isinstance(cfg, dict) else {}


class VoiceController:
    """Push-to-talk for an interactive front end. Not thread-bound: the app passes callables.

        insert(text)        put text at the editor's cursor (called through `post`)
        notify(text, level) a toast
        set_status(text)    a persistent status line, None to clear
        worker(fn)          run fn on a background thread
        post(fn)            run fn on the UI thread
    """

    def __init__(self, session: Any, insert: Callable[[str], None], notify: Callable[[str, str], None],
                 set_status: Callable[[Optional[str]], None], worker: Callable[[Callable[[], None]], None],
                 post: Callable[[Callable[[], None]], None], key_label: Callable[[], str] = lambda: "alt+v"):
        self.session = session
        self.insert, self.notify, self.set_status = insert, notify, set_status
        self.worker, self.post, self.key_label = worker, post, key_label
        self.state = "idle"            # idle | recording | transcribing
        self.recorder: Optional[Recorder] = None

    @property
    def recording(self) -> bool:
        return self.state == "recording"

    def toggle(self) -> None:
        if self.state == "recording":
            self.finish()
        elif self.state == "transcribing":
            self.notify("still transcribing the last recording…", "info")
        else:
            self.begin()

    def begin(self) -> None:
        from .caps import voice
        problem = voice.stt_problem(self.session)
        if problem:
            self.notify(problem, "warning")
            return
        rec = Recorder(_voice_config(self.session), on_limit=lambda: self.post(self._limit_reached))
        try:
            rec.start()
        except RecorderError as e:
            self.notify(str(e), "error")
            return
        self.recorder = rec
        self.state = "recording"
        self.set_status(f"● recording… ({self.key_label()} to stop, esc to cancel)")

    def _limit_reached(self) -> None:
        if self.state == "recording":
            self.notify(f"recording reached {int(self.recorder.max_seconds) if self.recorder else MAX_SECONDS} s; "
                        "stopped", "info")
            self.finish()

    def finish(self) -> None:
        rec = self.recorder
        if self.state != "recording" or rec is None:
            return
        self.recorder = None
        self.state = "transcribing"
        self.set_status("transcribing…")
        self.worker(lambda: self._transcribe(rec))

    def _transcribe(self, rec: Recorder) -> None:
        from .caps import voice
        from .tools.base import ToolError
        text, error = "", ""
        try:
            path = rec.stop()
            text = voice.transcribe_audio(self.session, path, str(_voice_config(self.session).get("language") or ""))[0]
        except (RecorderError, ToolError) as e:
            error = str(e)
        except Exception as e:
            error = f"transcription failed: {type(e).__name__}: {e}"
        finally:
            rec.cleanup()
        self.post(lambda: self._done(text, error))

    def _done(self, text: str, error: str) -> None:
        self.state = "idle"
        self.set_status(None)
        if error:
            self.notify(error, "error")
        elif not text:
            self.notify("no speech was recognised", "warning")
        else:
            self.insert(text)
            self.notify("transcribed - review and press enter to send", "info")

    def cancel(self) -> bool:
        """Escape: True when a recording was cancelled (the key is used up)."""
        if self.state != "recording":
            return False
        rec, self.recorder = self.recorder, None
        self.state = "idle"
        if rec is not None:
            rec.cancel()
        self.set_status(None)
        self.notify("recording cancelled", "info")
        return True

    def shutdown(self) -> None:
        if self.recorder is not None:
            self.recorder.cancel()
            self.recorder = None
        self.state = "idle"


# ------------------------------------------------------------------------------------------- /dictate
def dictate(session: Any, args: str, wait: Optional[Callable[[float], None]] = None) -> str:
    """`/dictate [seconds]`: record for that long (default 10, at most 120), put the transcript in the editor."""
    from .caps import voice
    from .tools.base import ToolError
    arg = (args or "").strip()
    seconds: float = DICTATE_DEFAULT
    if arg:
        try:
            seconds = float(arg)
        except ValueError:
            return "usage: /dictate [seconds]   (default 10, at most 120)"
        if not 0 < seconds <= MAX_SECONDS:
            return f"seconds must be between 1 and {MAX_SECONDS}"
    problem = voice.stt_problem(session)
    if problem:
        return problem
    port = getattr(session, "ui", None)
    cfg = _voice_config(session)
    rec = Recorder(cfg, max_seconds=seconds + 5)   # the timer here is only a safety net
    try:
        rec.start()
    except RecorderError as e:
        return str(e)
    try:
        if port is not None and hasattr(port, "notify"):
            port.notify(f"● recording for {seconds:g} s…", "info")
        (wait or threading.Event().wait)(seconds)
        path = rec.stop()
        text = voice.transcribe_audio(session, path, str(cfg.get("language") or ""))[0]
    except (RecorderError, ToolError) as e:
        return str(e)
    finally:
        rec.cleanup()
    if not text:
        return "no speech was recognised"
    if port is not None and hasattr(port, "set_editor_text"):
        port.set_editor_text(text)
        return f"transcript is in the editor ({len(text)} characters) - review and press enter to send"
    return text
