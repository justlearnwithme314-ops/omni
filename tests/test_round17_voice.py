"""The voice capability: speak (text to speech) and transcribe (speech to text). Offline."""
import http.server
import json
import os
import subprocess
import sys
import tempfile
import threading
import types
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from test_capabilities import CapCase  # noqa: E402
from src.pi import caps  # noqa: E402
from src.pi.caps import voice  # noqa: E402

caps.load_builtin()
for _cap in voice.CAPABILITIES:   # works whether or not `voice` is in the built-in module list
    caps.register(_cap)

MP3 = b"ID3\x04\x00fake-mp3-audio" + bytes(range(256))
WAV = b"RIFF\x24\x00\x00\x00WAVEfmt fake-wav-audio"
ENV = ("OPENAI_API_KEY", "OMNI_OPENAI_AUDIO_BASE")


class FakeSubprocess(types.SimpleNamespace):
    """Stands in for the `subprocess` module inside voice.py, so no test ever runs a speech program."""

    def __init__(self, returncode=0, stderr=b""):
        super().__init__(DEVNULL=subprocess.DEVNULL, PIPE=subprocess.PIPE, TimeoutExpired=subprocess.TimeoutExpired)
        self.runs, self.popens, self.returncode, self.stderr, self.text_seen = [], [], returncode, stderr, None

    def run(self, argv, env=None, **kw):
        self.runs.append((list(argv), env, kw))
        if env and env.get("OMNI_TTS_OUT"):
            src, out = env["OMNI_TTS_IN"], env["OMNI_TTS_OUT"]
        else:
            src, out = argv[argv.index("-f") + 1], argv[argv.index("-o" if "-o" in argv else "-w") + 1]
        self.text_seen = Path(src).read_text(encoding="utf-8")
        if self.returncode == 0:
            Path(out).write_bytes(WAV)
        return types.SimpleNamespace(returncode=self.returncode, stdout=b"", stderr=self.stderr)

    def Popen(self, argv, **kw):
        self.popens.append(list(argv))
        return types.SimpleNamespace(pid=1)


class Round17Voice(CapCase):
    def setUp(self):
        super().setUp()
        self._saved = {k: os.environ.pop(k, None) for k in ENV}
        self._tts, self._stt = dict(voice.TTS_PROVIDERS), dict(voice.STT_PROVIDERS)
        self.sub = FakeSubprocess()
        self.played = []
        for patcher in (mock.patch.object(voice, "subprocess", self.sub),
                        mock.patch.object(voice, "_system_program", lambda: None),      # no OS speech unless a test says so
                        mock.patch.object(voice, "_play", lambda path: self.played.append(path) or True)):
            patcher.start()
            self.addCleanup(patcher.stop)

    def tearDown(self):
        voice.TTS_PROVIDERS.clear()
        voice.TTS_PROVIDERS.update(self._tts)
        voice.STT_PROVIDERS.clear()
        voice.STT_PROVIDERS.update(self._stt)
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        super().tearDown()

    def profile(self, **extra):
        (self.profiles / "r17v.json").write_text(json.dumps({"name": "r17v", "extends": "lean", "capabilities": ["voice"], **extra}))
        return self.session(profile="r17v")

    def serve(self, status=200):
        """A local stand-in for both OpenAI audio endpoints. Returns the list the requests are recorded in."""
        seen = []

        class H(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                body = self.rfile.read(int(self.headers["Content-Length"]))
                seen.append({"path": self.path, "auth": self.headers.get("Authorization"),
                             "type": self.headers.get("Content-Type", ""), "body": body})
                if status != 200:
                    out, ctype = json.dumps({"error": {"message": "Incorrect API key provided"}}).encode(), "application/json"
                elif self.path == "/v1/audio/speech":
                    out, ctype = MP3, "audio/mpeg"
                elif self.path == "/v1/audio/transcriptions":
                    out, ctype = json.dumps({"text": "hello from the tape"}).encode(), "application/json"
                else:
                    out, ctype = b"{}", "application/json"
                self.send_response(status)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(out)))
                self.end_headers()
                self.wfile.write(out)
        srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        os.environ["OPENAI_API_KEY"] = "sk-test-not-real"
        os.environ["OMNI_OPENAI_AUDIO_BASE"] = f"http://127.0.0.1:{srv.server_address[1]}/"
        return seen


class TestCapability(Round17Voice):
    def test_registered_with_both_tools_and_the_voices_command(self):
        s = self.profile()
        self.assertIn("voice", s.caps.enabled)
        self.assertEqual(sorted(n for n, o in s.caps.owner.items() if o == "voice"), ["speak", "transcribe"])
        cap = caps.CAPABILITIES["voice"]
        self.assertEqual((cap.title, cap.inspired_by), ("Voice", "Hermes / OpenClaw TTS and transcription"))
        text = s.caps.commands()["voices"][1]("")
        self.assertIn("openai  not set up", text)
        self.assertIn("system  not set up", text)
        self.assertIn("Speech to text", text)
        os.environ["OPENAI_API_KEY"] = "sk-test-not-real"
        voice.register_tts("mine", lambda t, v, s: MP3, description="My voices")
        s2 = self.profile(voice={"tts": "mine"})
        text = s2.caps.commands()["voices"][1]("")
        self.assertIn("openai  available", text)
        self.assertIn("mine  available  (profile default)  - My voices", text)


class TestOpenAI(Round17Voice):
    def test_speak_posts_json_and_saves_mp3(self):
        seen = self.serve()
        s = self.profile()
        r = self.call(s, "speak", {"text": "Hello, world! This is Omni."})
        self.assertFalse(r.get("isError"), self.text(r))
        self.assertEqual(r["details"]["path"], "audio/hello-world-this-is-omni-1.mp3")
        self.assertIn("Saved audio/hello-world-this-is-omni-1.mp3", self.text(r))
        self.assertEqual((Path(s.cwd) / r["details"]["path"]).read_bytes(), MP3)
        self.assertEqual((r["details"]["provider"], r["details"]["played"]), ("openai", False))
        self.assertEqual(self.played, [])
        req = seen[0]
        self.assertEqual((req["path"], req["auth"]), ("/v1/audio/speech", "Bearer sk-test-not-real"))
        self.assertEqual(json.loads(req["body"]), {"model": "gpt-4o-mini-tts", "input": "Hello, world! This is Omni.",
                                                   "voice": "alloy", "response_format": "mp3"})
        r2 = self.call(s, "speak", {"text": "Hello, world! This is Omni.", "voice": "coral", "play": True})
        self.assertEqual(r2["details"]["path"], "audio/hello-world-this-is-omni-2.mp3")       # numbered, never replaced
        self.assertEqual(json.loads(seen[1]["body"])["voice"], "coral")
        self.assertTrue(r2["details"]["played"])
        self.assertEqual([p.name for p in self.played], ["hello-world-this-is-omni-2.mp3"])
        self.assertIn("playing", self.text(r2))

    def test_profile_sets_models_and_voice(self):
        seen = self.serve()
        s = self.profile(voice={"tts": "openai", "stt": "openai", "tts_model": "tts-1", "stt_model": "whisper-1", "voice": "sage"})
        self.assertFalse(self.call(s, "speak", {"text": "hi"}).get("isError"))
        body = json.loads(seen[0]["body"])
        self.assertEqual((body["model"], body["voice"]), ("tts-1", "sage"))
        (Path(s.cwd) / "a.wav").write_bytes(WAV)
        self.assertEqual(self.text(self.call(s, "transcribe", {"path": "a.wav"})), "hello from the tape")
        self.assertIn(b'name="model"\r\n\r\nwhisper-1\r\n', seen[1]["body"])

    def test_transcribe_sends_the_file_as_multipart(self):
        seen = self.serve()
        s = self.profile()
        audio = MP3 + b"\r\n--tricky\r\n" + bytes(range(256)) * 4
        (Path(s.cwd) / "rec").mkdir()
        (Path(s.cwd) / "rec" / "Note 1.MP3").write_bytes(audio)
        r = self.call(s, "transcribe", {"path": "rec/Note 1.MP3", "language": "en"})
        self.assertFalse(r.get("isError"), self.text(r))
        self.assertEqual(self.text(r), "hello from the tape")
        self.assertEqual((r["details"]["path"], r["details"]["provider"]), ("rec/Note 1.MP3", "openai"))
        req = seen[0]
        self.assertEqual((req["path"], req["auth"]), ("/v1/audio/transcriptions", "Bearer sk-test-not-real"))
        self.assertTrue(req["type"].startswith("multipart/form-data; boundary="))
        boundary = req["type"].split("boundary=", 1)[1].encode()
        body = req["body"]
        self.assertTrue(body.startswith(b"--" + boundary + b"\r\n"))
        self.assertTrue(body.endswith(b"\r\n--" + boundary + b"--\r\n"))
        self.assertIn(b'Content-Disposition: form-data; name="model"\r\n\r\ngpt-4o-mini-transcribe\r\n', body)
        self.assertIn(b'Content-Disposition: form-data; name="language"\r\n\r\nen\r\n', body)
        self.assertIn(b'name="file"; filename="audio.mp3"\r\nContent-Type: audio/mpeg\r\n\r\n' + audio + b"\r\n--" + boundary, body)
        self.assertEqual(body.count(b"--" + boundary), 5)                # model, response_format, language, file, end
        self.call(s, "transcribe", {"path": "rec/Note 1.MP3"})
        self.assertNotIn(b'name="language"', seen[1]["body"])

    def test_http_errors_are_reported(self):
        self.serve(status=401)
        s = self.profile()
        r = self.call(s, "speak", {"text": "hi"})
        self.assertTrue(r.get("isError"))
        self.assertIn("OpenAI answered HTTP 401: Incorrect API key provided", self.text(r))
        self.assertFalse((Path(s.cwd) / "audio").exists())
        (Path(s.cwd) / "a.ogg").write_bytes(b"OggS-fake")
        r = self.call(s, "transcribe", {"path": "a.ogg"})
        self.assertTrue(r.get("isError"))
        self.assertIn("HTTP 401", self.text(r))
        os.environ["OMNI_OPENAI_AUDIO_BASE"] = "http://127.0.0.1:1"
        self.assertIn("could not reach OpenAI", self.text(self.call(s, "speak", {"text": "hi"})))


class TestProviders(Round17Voice):
    def test_custom_tts_provider_and_result_shapes(self):
        calls = []

        def synth(text, v, session):
            calls.append((text, v))
            return {"data": b"custom-audio", "ext": "ogg"}
        p = voice.register_tts(" Mine ", synth, available=lambda s: True, description="My voices")
        self.assertEqual((p.name, voice.TTS_PROVIDERS["mine"]), ("mine", p))
        s = self.profile()
        r = self.call(s, "speak", {"text": "Read this", "voice": "nova"})
        self.assertFalse(r.get("isError"), self.text(r))
        self.assertEqual((r["details"]["path"], r["details"]["provider"]), ("audio/read-this-1.ogg", "mine"))
        self.assertEqual(calls, [("Read this", "nova")])
        self.assertEqual((Path(s.cwd) / "audio/read-this-1.ogg").read_bytes(), b"custom-audio")

        voice.register_tts("mine", lambda t, v, s: WAV)                       # plain bytes: the format is recognised
        self.assertEqual(self.call(s, "speak", {"text": "Read this"})["details"]["path"], "audio/read-this-2.wav")
        made = Path(tempfile.mkdtemp(prefix="omni-voice-test-")) / "out.flac"
        made.write_bytes(b"fLaC-fake")
        voice.register_tts("mine", lambda t, v, s: {"path": str(made)})       # a file the provider wrote
        r = self.call(s, "speak", {"text": "Read this", "path": "said/it"})
        self.assertEqual(r["details"]["path"], "said/it.flac")
        self.assertEqual((Path(s.cwd) / "said/it.flac").read_bytes(), b"fLaC-fake")

        voice.register_tts("mine", lambda t, v, s: b"")
        self.assertIn("returned no audio", self.text(self.call(s, "speak", {"text": "x"})))
        voice.register_tts("mine", lambda t, v, s: 1 / 0)
        r = self.call(s, "speak", {"text": "x"})
        self.assertTrue(r.get("isError"))
        self.assertIn("text-to-speech provider 'mine' failed: ZeroDivisionError", self.text(r))
        voice.unregister_tts("MINE")
        self.assertNotIn("mine", voice.TTS_PROVIDERS)
        self.assertRaises(ValueError, voice.register_tts, " ", synth)

    def test_custom_stt_provider(self):
        calls = []

        def hear(path, language, session):
            calls.append((path, language))
            return "  custom words  "
        voice.register_stt("ears", hear, description="My recogniser")
        s = self.profile()
        (Path(s.cwd) / "clip.flac").write_bytes(b"fLaC-fake")
        r = self.call(s, "transcribe", {"path": "clip.flac", "language": "pt-BR"})
        self.assertFalse(r.get("isError"), self.text(r))
        self.assertEqual(self.text(r), "custom words")
        self.assertEqual(calls, [((Path(s.cwd) / "clip.flac").resolve(), "pt-BR")])
        self.assertIsInstance(calls[0][0], Path)
        voice.register_stt("ears", lambda p, l, s: "")
        self.assertIn("no speech", self.text(self.call(s, "transcribe", {"path": "clip.flac"})))
        voice.unregister_stt("ears")
        self.assertRaises(ValueError, voice.register_stt, "", hear)

    def test_profile_choice_and_first_available(self):
        voice.register_tts("off", lambda t, v, s: b"off", available=lambda s: False, description="needs setting up")
        voice.register_tts("broken", lambda t, v, s: b"broken", available=lambda s: 1 / 0)
        voice.register_tts("one", lambda t, v, s: b"one")
        voice.register_tts("two", lambda t, v, s: b"two")
        s = self.profile()
        self.assertEqual(self.call(s, "speak", {"text": "x"})["details"]["provider"], "one")       # first that is set up
        s = self.profile(voice={"tts": "Two"})
        self.assertEqual(self.call(s, "speak", {"text": "x"})["details"]["provider"], "two")
        s = self.profile(voice={"tts": "off"})
        r = self.call(s, "speak", {"text": "x"})
        self.assertTrue(r.get("isError"))
        self.assertIn("'off' is not set up: needs setting up", self.text(r))
        s = self.profile(voice={"tts": "nope", "stt": "nope"})
        self.assertIn("no text-to-speech provider 'nope'", self.text(self.call(s, "speak", {"text": "x"})))
        (Path(s.cwd) / "a.wav").write_bytes(WAV)
        self.assertIn("no transcription provider 'nope'", self.text(self.call(s, "transcribe", {"path": "a.wav"})))

    def test_no_provider_is_a_clear_error(self):
        s = self.profile()                                   # no key, and setUp made the system voice unavailable
        r = self.call(s, "speak", {"text": "hello"})
        self.assertTrue(r.get("isError"))
        self.assertIn("No text-to-speech provider is set up", self.text(r))
        self.assertIn("openai  not set up", self.text(r))
        self.assertIn("system  not set up", self.text(r))
        self.assertEqual(self.sub.runs, [])
        self.assertFalse((Path(s.cwd) / "audio").exists())
        (Path(s.cwd) / "a.wav").write_bytes(WAV)
        r = self.call(s, "transcribe", {"path": "a.wav"})
        self.assertTrue(r.get("isError"))
        self.assertIn("No transcription provider is set up", self.text(r))
        self.assertIn("OPENAI_API_KEY", self.text(r))


class TestSystemVoice(Round17Voice):
    TEXT = 'Say "hi"; & $(whoami) `calc` | æøå'

    def system(self, kind, program):
        for patcher in (mock.patch.object(voice, "_platform", lambda: kind),
                        mock.patch.object(voice, "_system_program", lambda: program)):
            patcher.start()
            self.addCleanup(patcher.stop)
        return self.profile()

    def check(self, r, s, ext):
        self.assertFalse(r.get("isError"), self.text(r))
        self.assertEqual(r["details"]["provider"], "system")
        self.assertTrue(r["details"]["path"].endswith(ext), r["details"]["path"])
        self.assertEqual((Path(s.cwd) / r["details"]["path"]).read_bytes(), WAV)
        self.assertEqual(len(self.sub.runs), 1)
        argv, env, kw = self.sub.runs[0]
        self.assertEqual(self.sub.text_seen, self.TEXT)                      # the text arrived, through the temp file
        for arg in argv:
            self.assertNotIn(self.TEXT, arg)
            self.assertNotIn("whoami", arg)
        self.assertNotIn("shell", kw)
        return argv, env

    def test_windows_passes_text_through_a_file(self):
        s = self.system("windows", "C:/Windows/powershell.exe")
        argv, env = self.check(self.call(s, "speak", {"text": self.TEXT, "voice": "Microsoft Zira Desktop"}), s, ".wav")
        self.assertEqual(argv[:4], ["C:/Windows/powershell.exe", "-NoProfile", "-NonInteractive", "-Command"])
        self.assertEqual(argv[4], voice._POWERSHELL)                         # a fixed script: nothing interpolated
        self.assertIn("System.Speech.Synthesis.SpeechSynthesizer", argv[4])
        self.assertNotIn("Zira", argv[4])
        self.assertEqual(env["OMNI_TTS_VOICE"], "Microsoft Zira Desktop")
        self.assertNotIn("whoami", "".join(v for k, v in env.items() if k.startswith("OMNI_TTS_")))
        self.assertFalse(Path(env["OMNI_TTS_IN"]).exists())                  # the temp folder is cleaned up

    def test_macos_say(self):
        s = self.system("mac", "/usr/bin/say")
        argv, env = self.check(self.call(s, "speak", {"text": self.TEXT, "voice": "Samantha"}), s, ".aiff")
        self.assertEqual((argv[0], argv[1], argv[3], argv[5:]), ("/usr/bin/say", "-o", "-f", ["-v", "Samantha"]))
        self.assertTrue(argv[2].endswith(".aiff"))
        self.assertIsNone(env)

    def test_linux_espeak(self):
        s = self.system("linux", "/usr/bin/espeak-ng")
        argv, _env = self.check(self.call(s, "speak", {"text": self.TEXT}), s, ".wav")
        self.assertEqual((argv[0], argv[1], argv[3], len(argv)), ("/usr/bin/espeak-ng", "-w", "-f", 5))

    def test_failure_is_reported(self):
        self.sub.returncode, self.sub.stderr = 1, b"Cannot set voice. No matching voice is installed"
        s = self.system("windows", "powershell")
        r = self.call(s, "speak", {"text": "hello", "voice": "Nobody"})
        self.assertTrue(r.get("isError"))
        self.assertIn("the system speech program failed (voice 'Nobody'): Cannot set voice", self.text(r))
        self.assertFalse((Path(s.cwd) / "audio").exists())

    def test_availability_follows_the_installed_program(self):
        self.addCleanup(mock.patch.stopall)
        mock.patch.stopall()                                                 # the real _system_program, a fake `which`
        mock.patch.object(voice, "subprocess", self.sub).start()
        mock.patch.object(voice, "_platform", lambda: "linux").start()
        which = mock.patch.object(voice.shutil, "which", lambda name: None).start()
        s = self.profile()
        self.assertFalse(voice.TTS_PROVIDERS["system"].is_available(s))
        self.assertIn("No text-to-speech provider", self.text(self.call(s, "speak", {"text": "x"})))
        del which
        mock.patch.object(voice.shutil, "which", lambda name: "/usr/bin/espeak" if name == "espeak" else None).start()
        self.assertTrue(voice.TTS_PROVIDERS["system"].is_available(s))
        self.assertEqual(voice._system_program(), "/usr/bin/espeak")
        mock.patch.object(voice, "_play", lambda path: True).start()
        self.assertEqual(self.call(s, "speak", {"text": "x"})["details"]["provider"], "system")
        self.assertEqual(self.sub.runs[0][0][0], "/usr/bin/espeak")

    def test_play_starts_a_player_without_waiting(self):
        self.addCleanup(mock.patch.stopall)
        mock.patch.stopall()
        mock.patch.object(voice, "subprocess", self.sub).start()
        mock.patch.object(voice, "_platform", lambda: "mac").start()
        mock.patch.object(voice.shutil, "which", lambda name: "/usr/bin/afplay" if name == "afplay" else None).start()
        self.assertTrue(voice._play(Path("x.mp3")))
        self.assertEqual(self.sub.popens, [["/usr/bin/afplay", "x.mp3"]])
        self.assertEqual(self.sub.runs, [])                                  # started, not waited for
        mock.patch.object(voice.shutil, "which", lambda name: None).start()
        self.assertFalse(voice._play(Path("x.mp3")))                         # no player: reported, not an error


class TestLimits(Round17Voice):
    def setUp(self):
        super().setUp()
        self.made = []
        voice.register_tts("mine", lambda t, v, s: self.made.append(t) or MP3)
        voice.register_stt("ears", lambda p, l, s: self.made.append(p) or "words")

    def test_text_length_cap(self):
        s = self.profile()
        self.assertFalse(self.call(s, "speak", {"text": "a" * 4000}).get("isError"))
        r = self.call(s, "speak", {"text": "a" * 4001})
        self.assertTrue(r.get("isError"))
        self.assertIn("4,001 characters", self.text(r))
        self.assertIn("at most 4,000", self.text(r))
        self.assertTrue(self.call(s, "speak", {"text": "   "}).get("isError"))
        self.assertTrue(self.call(s, "speak", {"text": "hi", "voice": "a\nb"}).get("isError"))
        self.assertEqual(len(self.made), 1)

    def test_speak_path_must_stay_inside_and_never_overwrites(self):
        s = self.profile()
        outside = Path(tempfile.mkdtemp(prefix="omni-voice-outside-")) / "x.mp3"
        for bad in ("../escape.mp3", "audio/../../escape.mp3", str(outside)):
            r = self.call(s, "speak", {"text": "hi", "path": bad})
            self.assertTrue(r.get("isError"), bad)
            self.assertIn("inside the project", self.text(r))
        self.assertFalse(outside.exists())
        self.assertFalse((Path(s.cwd).parent / "escape.mp3").exists())
        self.assertEqual(self.made, [])                                      # refused before anything was synthesized

        r = self.call(s, "speak", {"text": "hi", "path": "out/greeting.mp3"})
        self.assertEqual(r["details"]["path"], "out/greeting.mp3")
        (Path(s.cwd) / "keep.mp3").write_bytes(b"precious")
        for taken in ("out/greeting.mp3", "keep.mp3", "keep"):               # "keep" would become keep.mp3
            r = self.call(s, "speak", {"text": "hi again", "path": taken})
            self.assertTrue(r.get("isError"), taken)
            self.assertIn("already exists", self.text(r))
        self.assertEqual((Path(s.cwd) / "keep.mp3").read_bytes(), b"precious")
        self.assertEqual((Path(s.cwd) / "out/greeting.mp3").read_bytes(), MP3)
        self.assertTrue(self.call(s, "speak", {"text": "hi", "path": "out"}).get("isError"))     # a folder
        self.assertEqual(self.call(s, "speak", {"text": "hi", "path": "out/second"})["details"]["path"], "out/second.mp3")

    def test_oversized_audio_is_not_saved(self):
        s = self.profile()
        with mock.patch.object(voice, "MAX_BYTES", 10):
            r = self.call(s, "speak", {"text": "hi"})
        self.assertTrue(r.get("isError"))
        self.assertIn("larger than 25 MB", self.text(r))
        self.assertFalse((Path(s.cwd) / "audio").exists())

    def test_transcribe_checks_the_file(self):
        s = self.profile()
        outside = Path(tempfile.mkdtemp(prefix="omni-voice-outside-")) / "x.wav"
        outside.write_bytes(WAV)
        for bad in (str(outside), "../x.wav"):
            r = self.call(s, "transcribe", {"path": bad})
            self.assertTrue(r.get("isError"), bad)
            self.assertIn("inside the project", self.text(r))
        self.assertIn("no audio file", self.text(self.call(s, "transcribe", {"path": "missing.wav"})))
        self.assertTrue(self.call(s, "transcribe", {}).get("isError"))
        (Path(s.cwd) / "notes.txt").write_text("not audio")
        self.assertIn("mp3, wav, m4a, ogg, webm, flac", self.text(self.call(s, "transcribe", {"path": "notes.txt"})))
        (Path(s.cwd) / "empty.wav").write_bytes(b"")
        self.assertIn("is empty", self.text(self.call(s, "transcribe", {"path": "empty.wav"})))
        (Path(s.cwd) / "ok.webm").write_bytes(b"webm-fake")
        self.assertTrue(self.call(s, "transcribe", {"path": "ok.webm", "language": "en; rm -rf"}).get("isError"))
        self.assertEqual(self.made, [])
        with (Path(s.cwd) / "big.m4a").open("wb") as f:                      # sparse: 25 MB + 1 byte, nothing read
            f.seek(voice.MAX_BYTES)
            f.write(b"\0")
        r = self.call(s, "transcribe", {"path": "big.m4a"})
        self.assertTrue(r.get("isError"))
        self.assertIn("at most 25 MB", self.text(r))
        self.assertEqual(self.made, [])
        self.assertEqual(self.text(self.call(s, "transcribe", {"path": "ok.webm"})), "words")


if __name__ == "__main__":
    unittest.main()
