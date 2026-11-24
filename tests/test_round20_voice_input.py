"""Push-to-talk voice input (src/pi/voice_input.py): recorder detection, recording, the controller the terminal app
drives, the alt+v / escape keys and /dictate. Offline: no recorder program is ever run and no audio is captured."""
import json
import os
import queue
import subprocess
import sys
import threading
import time
import types
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from test_capabilities import CapCase  # noqa: E402
from src.pi import caps  # noqa: E402
from src.pi import voice_input as vi  # noqa: E402
from src.pi.caps import voice  # noqa: E402
from src.pi.tui import keybindings  # noqa: E402

caps.load_builtin()
for _cap in voice.CAPABILITIES:
    caps.register(_cap)

WAV = b"RIFF\x24\x10\x00\x00WAVEfmt " + b"\x00" * 200

# Captured from `ffmpeg -list_devices true -f dshow -i dummy` (ffmpeg 6/7 layout: each device tagged).
DSHOW_NEW = r"""[dshow @ 000001f3c5a0e2c0] "HD Webcam" (video)
[dshow @ 000001f3c5a0e2c0]   Alternative name "@device_pnp_\\?\usb#vid_0bda&pid_58c2&mi_00#6&2bd8e2a3&0&0000#{65e8773d-8f56-11d0-a3b9-00a0c9223196}\global"
[dshow @ 000001f3c5a0e2c0] "OBS Virtual Camera" (none)
[dshow @ 000001f3c5a0e2c0]   Alternative name "@device_sw_{860BB310-5D01-11D0-BD3B-00A0C911CE86}\{A3FCE0F5-3493-419F-958A-ABA1250EC20B}"
[dshow @ 000001f3c5a0e2c0] "Microphone Array (Realtek(R) Audio)" (audio)
[dshow @ 000001f3c5a0e2c0]   Alternative name "@device_cm_{33D9A762-90C8-11D0-BD43-00A0C911CE86}\wave_{8E1F4A07-2C1B-4D4D-9D6B-2B7E0A7F1C11}"
[dshow @ 000001f3c5a0e2c0] "Headset Microphone (Jabra Evolve 65)" (audio)
[dshow @ 000001f3c5a0e2c0]   Alternative name "@device_cm_{33D9A762-90C8-11D0-BD43-00A0C911CE86}\wave_{1D2C3B4A-0000-4C4C-8888-0123456789AB}"
[in#0 @ 000001f3c59f8a40] Error opening input: Immediate exit requested
Error opening input file dummy.
"""
# The older (ffmpeg 4.x) layout: headings, untagged names.
DSHOW_OLD = r"""[dshow @ 0000020b5d0f0c40] DirectShow video devices (some may be both video and audio devices)
[dshow @ 0000020b5d0f0c40]  "Integrated Camera"
[dshow @ 0000020b5d0f0c40]     Alternative name "@device_pnp_\\?\usb#vid_04f2&pid_b6d9&mi_00#6&1f2e3d4c&0&0000#{65e8773d-8f56-11d0-a3b9-00a0c9223196}\global"
[dshow @ 0000020b5d0f0c40] DirectShow audio devices
[dshow @ 0000020b5d0f0c40]  "Mikrofon (USB Audio Device)"
[dshow @ 0000020b5d0f0c40]     Alternative name "@device_cm_{33D9A762-90C8-11D0-BD43-00A0C911CE86}\wave_{AAAA0000-1111-2222-3333-444455556666}"
dummy: Immediate exit requested
"""


class FakeStdin:
    def __init__(self, proc):
        self.proc, self.data, self.closed = proc, b"", False

    def write(self, b):
        self.data += b

    def flush(self):
        pass

    def close(self):
        self.closed = True
        if self.proc.obeys_q and b"q" in self.data:
            self.proc.returncode = 0


class FakeProc:
    """A recorder process: writes the WAV on start; `obeys_q` / `obeys_terminate` decide whether it ends politely."""

    def __init__(self, argv, kw, factory):
        self.argv, self.kw, self.returncode = list(argv), kw, None
        self.obeys_q, self.obeys_terminate = factory.obeys_q, factory.obeys_terminate
        self.events = []
        self.stdin = FakeStdin(self) if kw.get("stdin") == subprocess.PIPE else None
        out = next((a.split("=", 1)[-1] for a in argv if a.endswith("speech.wav")), None)
        if out and factory.audio:
            Path(out).write_bytes(factory.audio)

    def poll(self):
        return self.returncode

    def terminate(self):
        self.events.append("terminate")
        if self.obeys_terminate:
            self.returncode = 1

    def send_signal(self, sig):
        self.events.append(("signal", sig))
        if self.obeys_terminate:
            self.returncode = 1

    def kill(self):
        self.events.append("kill")
        self.returncode = -9

    def wait(self, timeout=None):
        self.events.append(("wait", timeout))
        if self.returncode is None:
            raise subprocess.TimeoutExpired(self.argv, timeout)
        return self.returncode


class FakePopen:
    def __init__(self, audio=WAV, obeys_q=True, obeys_terminate=True):
        self.audio, self.obeys_q, self.obeys_terminate = audio, obeys_q, obeys_terminate
        self.procs = []

    def __call__(self, argv, **kw):
        p = FakeProc(argv, kw, self)
        self.procs.append(p)
        return p


def fake_which(*present):
    return lambda name: f"/opt/bin/{name}" if name in present else None


class Round20(CapCase):
    def setUp(self):
        super().setUp()
        self._stt = dict(voice.STT_PROVIDERS)
        self._key = os.environ.pop("OPENAI_API_KEY", None)
        self.heard = []
        voice.register_stt("fake", lambda path, lang, s: self.heard.append((Path(path).read_bytes()[:4], lang)) or
                           "  hello from the microphone  ", description="fixed text")
        self.popen = FakePopen()
        self.runs = []
        self.patch(vi.subprocess, "Popen", self.popen)
        self.patch(vi.subprocess, "run", self.fake_run)
        self.patch(vi.shutil, "which", fake_which("ffmpeg"))
        self.patch(vi, "_platform", lambda: "linux")

    def tearDown(self):
        voice.STT_PROVIDERS.clear()
        voice.STT_PROVIDERS.update(self._stt)
        if self._key is not None:
            os.environ["OPENAI_API_KEY"] = self._key
        super().tearDown()

    def patch(self, obj, name, value):
        p = mock.patch.object(obj, name, value)
        p.start()
        self.addCleanup(p.stop)

    def fake_run(self, argv, **kw):
        self.runs.append(list(argv))
        return types.SimpleNamespace(returncode=1, stdout=b"", stderr=DSHOW_NEW.encode("utf-8"))

    def profile(self, caps_list=("voice",), **extra):
        (self.profiles / "r20.json").write_text(json.dumps({"name": "r20", "extends": "lean",
                                                            "capabilities": list(caps_list), **extra}))
        return self.session(profile="r20")


class TestDetection(Round20):
    def test_dshow_device_parsing_both_layouts(self):
        self.assertEqual(vi.parse_dshow_audio_devices(DSHOW_NEW),
                         ["Microphone Array (Realtek(R) Audio)", "Headset Microphone (Jabra Evolve 65)"])
        self.assertEqual(vi.parse_dshow_audio_devices(DSHOW_OLD), ["Mikrofon (USB Audio Device)"])
        self.assertEqual(vi.parse_dshow_audio_devices("ffmpeg version 7.0\nnothing here"), [])

    def test_windows_ffmpeg_uses_the_first_dshow_microphone(self):
        self.patch(vi, "_platform", lambda: "windows")
        cmd = vi.recorder_command({})
        self.assertEqual(self.runs, [["/opt/bin/ffmpeg", "-hide_banner", "-list_devices", "true", "-f", "dshow", "-i", "dummy"]])
        self.assertEqual(cmd["kind"], "ffmpeg")
        self.assertEqual(cmd["argv"], ["/opt/bin/ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "dshow", "-i",
                                       "audio=Microphone Array (Realtek(R) Audio)", "-t", "120", "-ac", "1", "-ar", "16000",
                                       "-c:a", "pcm_s16le", "{out}"])

    def test_windows_without_microphone(self):
        self.patch(vi, "_platform", lambda: "windows")
        self.patch(vi.subprocess, "run", lambda argv, **kw: types.SimpleNamespace(returncode=1, stdout=b"",
                                                                                  stderr=DSHOW_NEW.split("[dshow @ 000001f3c5a0e2c0] \"Microphone")[0].encode()))
        with self.assertRaises(vi.RecorderError) as e:
            vi.recorder_command({})
        self.assertIn("no microphone", str(e.exception))

    def test_mac_and_linux_ffmpeg(self):
        self.patch(vi, "_platform", lambda: "mac")
        self.assertEqual(vi.recorder_command({})["argv"][5:9], ["-f", "avfoundation", "-i", ":0"])
        self.patch(vi, "_platform", lambda: "linux")
        self.assertEqual(vi.recorder_command({})["argv"][5:9], ["-f", "alsa", "-i", "default"])
        self.patch(vi.shutil, "which", fake_which("ffmpeg", "pactl"))
        self.assertEqual(vi.recorder_command({}, max_seconds=30)["argv"][5:11], ["-f", "pulse", "-i", "default", "-t", "30"])
        self.assertEqual(self.runs, [])   # device listing is a Windows-only step

    def test_sox_rec_and_arecord_order(self):
        self.patch(vi.shutil, "which", fake_which("rec", "sox", "arecord"))
        self.assertEqual(vi.recorder_command({}), {"kind": "sox", "argv": [
            "/opt/bin/rec", "-q", "-r", "16000", "-c", "1", "-b", "16", "{out}", "trim", "0", "120"]})
        self.patch(vi.shutil, "which", fake_which("sox", "arecord"))
        self.assertEqual(vi.recorder_command({})["argv"][:3], ["/opt/bin/sox", "-q", "-d"])
        self.patch(vi, "_platform", lambda: "windows")
        self.assertEqual(vi.recorder_command({})["argv"][:5], ["/opt/bin/sox", "-q", "-t", "waveaudio", "default"])
        self.patch(vi, "_platform", lambda: "linux")
        self.patch(vi.shutil, "which", fake_which("arecord"))
        self.assertEqual(vi.recorder_command({}), {"kind": "arecord", "argv": [
            "/opt/bin/arecord", "-q", "-f", "S16_LE", "-r", "16000", "-c", "1", "-t", "wav", "-d", "120", "{out}"]})

    def test_missing_recorder_names_what_to_install(self):
        self.patch(vi.shutil, "which", fake_which())
        for plat in ("windows", "mac", "linux"):
            self.patch(vi, "_platform", lambda p=plat: p)
            with self.assertRaises(vi.RecorderError) as e:
                vi.recorder_command({})
            self.assertIn("Install ffmpeg", str(e.exception))
            self.assertIn("record_command", str(e.exception))
        self.patch(vi, "_platform", lambda: "mac")
        self.patch(vi.shutil, "which", fake_which("arecord"))   # arecord counts only on Linux
        self.assertRaises(vi.RecorderError, vi.recorder_command, {})

    def test_profile_record_command_wins_and_fills_out(self):
        self.patch(vi.shutil, "which", fake_which())
        r = vi.Recorder({"record_command": ["myrec", "--rate", "16000", "--file={out}"]})
        r.start()
        self.addCleanup(r.cleanup)
        self.assertEqual(r.kind, "custom")
        self.assertEqual(self.popen.procs[-1].argv, ["myrec", "--rate", "16000", f"--file={r.path}"])
        self.assertTrue(r.path.name.endswith(".wav"))
        self.assertEqual(vi.recorder_command({"record_command": ["ffmpeg", "-f", "x"]})["argv"][-1], "{out}")  # appended
        self.assertEqual(vi.recorder_command({"record_command": ["C:/tools/ffmpeg.exe", "{out}"]})["kind"], "ffmpeg")
        self.assertRaises(vi.RecorderError, vi.recorder_command, {"record_command": [1, 2]})


class TestRecorder(Round20):
    def test_graceful_stop_sends_q_to_ffmpeg(self):
        r = vi.Recorder({})
        r.start()
        proc = self.popen.procs[-1]
        self.assertEqual(proc.kw["stdin"], subprocess.PIPE)
        self.assertEqual(proc.argv[-1], str(r.path))
        path = r.stop()
        self.assertEqual(path.read_bytes(), WAV)
        self.assertEqual((proc.stdin.data, proc.stdin.closed), (b"q", True))
        self.assertNotIn("kill", proc.events)
        r.cleanup()
        self.assertFalse(path.exists())
        self.assertFalse(r.folder.exists())

    def test_kill_when_the_recorder_ignores_stop(self):
        self.popen.obeys_q = self.popen.obeys_terminate = False
        r = vi.Recorder({}, stop_timeout=0.01)
        r.start()
        proc = self.popen.procs[-1]
        r.stop()
        self.assertEqual([e for e in proc.events if not isinstance(e, tuple)], ["kill"])
        self.assertEqual(proc.events[0], ("wait", 0.01))
        r.cleanup()

    def test_non_ffmpeg_is_signalled_then_killed(self):
        self.patch(vi.shutil, "which", fake_which("arecord"))
        self.popen.obeys_terminate = False
        r = vi.Recorder({}, stop_timeout=0.01)
        r.start()
        proc = self.popen.procs[-1]
        self.assertEqual(proc.kw["stdin"], subprocess.DEVNULL)
        r.stop()
        self.assertEqual(proc.events[0][0], "signal")
        self.assertIn("kill", proc.events)
        r.cleanup()
        self.patch(vi, "_platform", lambda: "windows")
        self.patch(vi.shutil, "which", fake_which("sox"))
        self.popen.obeys_terminate = True
        r = vi.Recorder({})
        r.start()
        r.stop()
        self.assertEqual(self.popen.procs[-1].events[0], "terminate")
        r.cleanup()

    def test_max_duration_stops_by_itself(self):
        r = vi.Recorder({}, max_seconds=0.05)
        r.start()
        self.assertIn("0.1", self.popen.procs[-1].argv)   # ffmpeg is also told the limit (-t)
        deadline = time.time() + 3
        while r.recording and time.time() < deadline:
            time.sleep(0.01)
        self.assertFalse(r.recording)
        self.assertTrue(r.timed_out)
        self.assertEqual(self.popen.procs[-1].stdin.data, b"q")
        self.assertEqual(r.stop().read_bytes(), WAV)       # the file is still there to be transcribed
        r.cleanup()
        hit = threading.Event()
        r2 = vi.Recorder({}, max_seconds=0.05, on_limit=hit.set)
        r2.start()
        self.assertTrue(hit.wait(3))
        r2.cancel()

    def test_cancel_deletes_the_file(self):
        r = vi.Recorder({})
        r.start()
        self.assertTrue(r.path.exists())
        r.cancel()
        self.assertFalse(r.path.exists())
        self.assertFalse(r.folder.exists())
        self.assertEqual(self.popen.procs[-1].stdin.data, b"q")

    def test_no_audio_is_an_error(self):
        self.popen.audio = b""
        r = vi.Recorder({})
        r.start()
        with self.assertRaises(vi.RecorderError) as e:
            r.stop()
        self.assertIn("produced no audio", str(e.exception))
        r.cleanup()


class Harness:
    """The callables the terminal app hands the controller, run synchronously."""

    def __init__(self):
        self.editor_text, self.notes, self.status = "", [], None

    def insert(self, t):
        self.editor_text += t

    def notify(self, text, level="info"):
        self.notes.append((level, text))

    def set_status(self, text):
        self.status = text


class TestController(Round20):
    def controller(self, s):
        h = Harness()
        c = vi.VoiceController(s, h.insert, h.notify, h.set_status, worker=lambda fn: fn(), post=lambda fn: fn())
        return c, h

    def test_toggle_records_transcribes_and_inserts(self):
        s = self.profile(voice={"stt": "fake", "language": "de"})
        c, h = self.controller(s)
        h.editor_text = "note: "
        c.toggle()
        self.assertEqual(c.state, "recording")
        self.assertEqual(h.status, "● recording… (alt+v to stop, esc to cancel)")
        folder = c.recorder.folder
        c.toggle()
        self.assertEqual(c.state, "idle")
        self.assertEqual(h.editor_text, "note: hello from the microphone")      # inserted, not sent
        self.assertEqual(self.heard, [(b"RIFF", "de")])
        self.assertIsNone(h.status)
        self.assertFalse(folder.exists())                                      # temp file deleted
        self.assertIn("review and press enter", h.notes[-1][1])

    def test_escape_cancels(self):
        s = self.profile(voice={"stt": "fake"})
        c, h = self.controller(s)
        self.assertFalse(c.cancel())       # nothing to cancel: escape keeps its usual meaning
        c.toggle()
        folder = c.recorder.folder
        self.assertTrue(c.cancel())
        self.assertEqual((c.state, h.status, h.editor_text, self.heard), ("idle", None, "", []))
        self.assertFalse(folder.exists())

    def test_capability_off_or_no_stt(self):
        s = self.profile(caps_list=())
        c, h = self.controller(s)
        c.toggle()
        self.assertEqual(c.state, "idle")
        self.assertIn("/capabilities voice on", h.notes[-1][1])
        self.assertEqual(self.popen.procs, [])
        voice.STT_PROVIDERS.pop("fake")
        s2 = self.profile()
        c2, h2 = self.controller(s2)
        c2.toggle()
        self.assertIn("No transcription provider is set up", h2.notes[-1][1])
        self.assertEqual(self.popen.procs, [])

    def test_no_recorder_and_limit(self):
        s = self.profile(voice={"stt": "fake", "max_record_seconds": 0.05})
        self.patch(vi.shutil, "which", fake_which())
        c, h = self.controller(s)
        c.toggle()
        self.assertEqual((c.state, h.notes[-1][0]), ("idle", "error"))
        self.assertIn("Install ffmpeg", h.notes[-1][1])
        self.patch(vi.shutil, "which", fake_which("ffmpeg"))
        done = threading.Event()
        c2 = vi.VoiceController(s, h.insert, h.notify, h.set_status, worker=lambda fn: fn(),
                                post=lambda fn: (fn(), done.set()))
        c2.toggle()
        self.assertTrue(done.wait(3))
        deadline = time.time() + 3
        while c2.state != "idle" and time.time() < deadline:
            time.sleep(0.01)
        self.assertEqual(h.editor_text, "hello from the microphone")
        self.assertTrue(any("stopped" in t for _l, t in h.notes))


class TestTerminalKeys(Round20):
    def app(self, s):
        from src.pi.tui.app import InteractiveApp
        from src.pi.tui.editor import Editor
        a = InteractiveApp.__new__(InteractiveApp)
        a.session, a.editor, a.inbox = s, Editor(), queue.Queue()
        a.ac_items, a.statuses, a.blocks, a.scroll, a.toast, a.dirty = [], {}, [], 0, None, False
        a.working, a.last_esc = False, 0.0
        a.in_worker = lambda fn, name="w": fn()
        a.update_completions = lambda *x, **k: None
        return a

    def drain(self, a):
        while not a.inbox.empty():
            a.inbox.get_nowait()[1]()

    def test_alt_v_and_escape_in_the_app(self):
        from src.pi.tui.term import Key
        self.assertEqual(keybindings.ACTIONS["app.voice.toggle"], ("alt+v", ["alt+v"]))
        s = self.profile(voice={"stt": "fake"})
        a = self.app(s)
        a.editor.set_text("see ")
        a._app_key(Key("alt+v"))
        self.assertTrue(a.statuses["voice"].startswith("● recording"))
        a._app_key(Key("alt+v"))
        self.drain(a)
        self.assertEqual(a.editor.text, "see hello from the microphone")
        self.assertNotIn("voice", a.statuses)
        a._app_key(Key("alt+v"))
        a._app_key(Key("escape"))
        self.assertEqual(a._voice.state, "idle")
        self.assertEqual(a.editor.text, "see hello from the microphone")

    def test_rebinding_moves_the_key_and_the_hint(self):
        from src.pi.tui.term import Key
        (self.agent / "keybindings.json").write_text(json.dumps({"app.voice.toggle": "f5"}))
        km = keybindings.load(self.agent)
        self.assertEqual((km.resolve("f5"), km.resolve("alt+v")), ("alt+v", ""))
        s = self.profile(voice={"stt": "fake"})
        a = self.app(s)
        a._app_key(Key("alt+v"))
        self.assertNotIn("voice", a.statuses)          # the old key no longer records
        a._app_key(Key("f5"))
        self.assertIn("(f5 to stop", a.statuses["voice"])
        a._voice.shutdown()


class TestDictate(Round20):
    def test_dictate_puts_the_transcript_in_the_editor(self):
        s = self.profile(voice={"stt": "fake"})
        port = Harness()
        port.set_editor_text = lambda t: setattr(port, "editor_text", t)
        s.ui = port
        out = s.caps.commands()["dictate"][1]("0.05")
        self.assertEqual(port.editor_text, "hello from the microphone")
        self.assertIn("transcript is in the editor", out)
        self.assertIn("recording for 0.05 s", port.notes[0][1])
        self.assertFalse(Path(self.popen.procs[-1].argv[-1]).parent.exists())
        waited = []
        self.assertEqual(vi.dictate(s, "", wait=waited.append).startswith("transcript"), True)
        self.assertEqual(waited, [10])
        self.assertIn("usage", vi.dictate(s, "abc"))
        self.assertIn("between 1 and 120", vi.dictate(s, "500"))

    def test_dictate_without_recorder_or_capability(self):
        s = self.profile(voice={"stt": "fake"})
        self.patch(vi.shutil, "which", fake_which())
        self.assertIn("Install ffmpeg", vi.dictate(s, "1", wait=lambda x: None))
        off = self.profile(caps_list=())
        self.assertIn("voice", vi.dictate(off, "1", wait=lambda x: None))


if __name__ == "__main__":
    unittest.main()
