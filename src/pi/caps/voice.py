"""`voice`: read text aloud into an audio file, and turn an audio file back into text.

`speak(text, voice?, path?, play?)` turns text into speech, saves it under the project (default
`audio/<slug>-<n>.<ext>`) and returns the saved path. The audio data itself never enters the conversation.

    text       what to say, 4,000 characters at most
    voice      a voice name of the provider in use (OpenAI: alloy, ash, coral, echo, ...; system: an installed voice)
    path       where to save, relative to the project; an existing file is never overwritten
    play       true: also start playing it on this machine (best effort, does not wait for the end)

`transcribe(path, language?)` returns the words spoken in an audio file inside the project
(mp3, wav, m4a, ogg, webm, flac; 25 MB at most). `language` is an optional hint such as `en` or `de`.

Built-in providers:

    openai     speech + transcription    OPENAI_API_KEY     models `gpt-4o-mini-tts` / `gpt-4o-mini-transcribe`
    system     speech only, offline      no key             Windows: built-in speech (PowerShell) -> .wav
                                                            macOS: `say` -> .aiff      Linux: `espeak-ng` / `espeak` -> .wav

The OpenAI calls are written from the published API descriptions and are untested against the live service.
The `system` provider is available only when the program it needs is installed.

Profile settings (all optional): `"voice": {"tts": "openai", "stt": "openai", "voice": "alloy",
"tts_model": "gpt-4o-mini-tts", "stt_model": "gpt-4o-mini-transcribe"}`. Without `tts` / `stt` the first provider
that is set up is used. `OMNI_OPENAI_AUDIO_BASE` points the OpenAI calls at another server (a proxy or a compatible
service). `/voices` lists the providers and whether each can be used.

Adding a provider (this is the slot a Hermes or OpenClaw TTS / transcription plugin is plugged into):

    from src.pi.caps import voice
    voice.register_tts("mine", synthesize, available=lambda session: True, description="My voices")
    voice.register_stt("mine", transcribe, available=lambda session: True, description="My recogniser")

`synthesize(text, voice, session)` returns the audio as `bytes`, or `{"data": bytes, "ext": "mp3"}` or
`{"path": "file the provider wrote"}`; `voice` is "" when none was asked for. `transcribe(path, language, session)`
gets the audio file as a `Path` and returns the text. Raise `ToolError` with a message for the user when it fails.
`available(session)` returns whether it can run now (omit it for "always"). `unregister_tts("mine")` and
`unregister_stt("mine")` remove them.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from ..tools.base import ToolDef, ToolError, check_abort, text_result
from . import Capability

MAX_TEXT = 4000
MAX_BYTES = 25 * 1024 * 1024
TIMEOUT = 180
SYSTEM_TIMEOUT = 120
OPENAI_TTS_MODEL = "gpt-4o-mini-tts"
OPENAI_STT_MODEL = "gpt-4o-mini-transcribe"
OPENAI_VOICE = "alloy"
AUDIO_TYPES = {".mp3": "audio/mpeg", ".wav": "audio/wav", ".m4a": "audio/mp4", ".ogg": "audio/ogg",
               ".webm": "audio/webm", ".flac": "audio/flac"}
_EXT = re.compile(r"^[a-z0-9]{1,5}$")
# Windows speech script: the text, the output file and the voice arrive through the environment, never the command line
_POWERSHELL = ("Add-Type -AssemblyName System.Speech; "
               "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer; "
               "try { if ($env:OMNI_TTS_VOICE) { $s.SelectVoice($env:OMNI_TTS_VOICE) }; "
               "$s.SetOutputToWaveFile($env:OMNI_TTS_OUT); "
               "$s.Speak([System.IO.File]::ReadAllText($env:OMNI_TTS_IN, [System.Text.Encoding]::UTF8)) } "
               "finally { $s.Dispose() }")


@dataclass
class TtsProvider:
    name: str
    synthesize: Callable[[str, str, Any], Any]          # (text, voice, session) -> bytes | {"data", "ext"} | {"path"}
    available: Optional[Callable[[Any], bool]] = None   # session -> can it run now? (None = always)
    description: str = ""

    def is_available(self, session: Any) -> bool:
        if self.available is None:
            return True
        try:
            return bool(self.available(session))
        except Exception:
            return False


@dataclass
class SttProvider:
    name: str
    transcribe: Callable[[Path, str, Any], str]         # (audio file, language or "", session) -> text
    available: Optional[Callable[[Any], bool]] = None   # session -> can it run now? (None = always)
    description: str = ""

    def is_available(self, session: Any) -> bool:
        if self.available is None:
            return True
        try:
            return bool(self.available(session))
        except Exception:
            return False


TTS_PROVIDERS: Dict[str, TtsProvider] = {}
STT_PROVIDERS: Dict[str, SttProvider] = {}


def register_tts(name: str, synthesize: Callable[[str, str, Any], Any],
                 available: Optional[Callable[[Any], bool]] = None, description: str = "") -> TtsProvider:
    """Add (or replace) a text-to-speech provider. Providers are tried in registration order when none is chosen."""
    p = TtsProvider(str(name).strip().lower(), synthesize, available, description)
    if not p.name:
        raise ValueError("a provider needs a name")
    TTS_PROVIDERS[p.name] = p
    return p


def unregister_tts(name: str) -> None:
    TTS_PROVIDERS.pop(str(name).strip().lower(), None)


def register_stt(name: str, transcribe: Callable[[Path, str, Any], str],
                 available: Optional[Callable[[Any], bool]] = None, description: str = "") -> SttProvider:
    """Add (or replace) a speech-to-text provider. Providers are tried in registration order when none is chosen."""
    p = SttProvider(str(name).strip().lower(), transcribe, available, description)
    if not p.name:
        raise ValueError("a provider needs a name")
    STT_PROVIDERS[p.name] = p
    return p


def unregister_stt(name: str) -> None:
    STT_PROVIDERS.pop(str(name).strip().lower(), None)


# ------------------------------------------------------------------------------------------- http
def _config(session: Any) -> Dict[str, Any]:
    cfg = session.profile.get("voice")
    return cfg if isinstance(cfg, dict) else {}


def _openai() -> Tuple[str, str]:
    key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not key:
        raise ToolError("OPENAI_API_KEY is not set")
    return key, (os.environ.get("OMNI_OPENAI_AUDIO_BASE") or "https://api.openai.com").rstrip("/")


def _has_openai_key(session: Any) -> bool:
    return bool(os.environ.get("OPENAI_API_KEY", "").strip())


def _post(url: str, data: bytes, headers: Dict[str, str], who: str) -> bytes:
    req = urllib.request.Request(url, data=data, method="POST", headers={"User-Agent": "omni-voice/1.0", **headers})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            raw = r.read(MAX_BYTES + 1)
    except urllib.error.HTTPError as e:
        detail = e.read(4000).decode("utf-8", "replace")
        try:
            err = json.loads(detail).get("error")
            detail = str(err.get("message") if isinstance(err, dict) else err or detail)
        except (ValueError, AttributeError):
            pass
        raise ToolError(f"{who} answered HTTP {e.code}: {detail.strip()[:500]}") from None
    except (urllib.error.URLError, OSError) as e:
        raise ToolError(f"could not reach {who}: {getattr(e, 'reason', e)}") from None
    if len(raw) > MAX_BYTES:
        raise ToolError(f"{who} answered with more than 25 MB; not used")
    return raw


def multipart(fields: Dict[str, str], name: str, filename: str, content: bytes, content_type: str) -> Tuple[bytes, str]:
    """A multipart/form-data body with text fields and one file. Returns (body, Content-Type header value)."""
    boundary = "----omni" + uuid.uuid4().hex
    while boundary.encode("ascii") in content:
        boundary = "----omni" + uuid.uuid4().hex

    def clean(value: str) -> str:   # header values: no line breaks, no quotes
        return re.sub(r'[\r\n"\\]', "_", str(value))

    parts: List[bytes] = []
    for key, value in fields.items():
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{clean(key)}"\r\n\r\n'.encode("utf-8")
                     + str(value).encode("utf-8") + b"\r\n")
    parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{clean(name)}"; filename="{clean(filename)}"\r\n'
                 f"Content-Type: {clean(content_type)}\r\n\r\n".encode("utf-8") + content + b"\r\n")
    parts.append(f"--{boundary}--\r\n".encode("ascii"))
    return b"".join(parts), f"multipart/form-data; boundary={boundary}"


# ------------------------------------------------------------------------------------------- built-in providers
def _openai_synthesize(text: str, voice: str, session: Any) -> Any:
    key, base = _openai()
    body = {"model": str(_config(session).get("tts_model") or OPENAI_TTS_MODEL), "input": text,
            "voice": voice or OPENAI_VOICE, "response_format": "mp3"}
    raw = _post(f"{base}/v1/audio/speech", json.dumps(body).encode("utf-8"),
                {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}, "OpenAI")
    if not raw:
        raise ToolError("OpenAI returned no audio")
    return {"data": raw, "ext": "mp3"}


def _openai_transcribe(path: Path, language: str, session: Any) -> str:
    key, base = _openai()
    fields = {"model": str(_config(session).get("stt_model") or OPENAI_STT_MODEL), "response_format": "json"}
    if language:
        fields["language"] = language
    try:
        content = path.read_bytes()
    except OSError as e:
        raise ToolError(f"could not read {path.name}: {e}") from None
    body, content_type = multipart(fields, "file", "audio" + path.suffix.lower(), content,
                                   AUDIO_TYPES.get(path.suffix.lower(), "application/octet-stream"))
    raw = _post(f"{base}/v1/audio/transcriptions", body,
                {"Authorization": f"Bearer {key}", "Content-Type": content_type}, "OpenAI")
    try:
        data = json.loads(raw.decode("utf-8", "replace"))
    except ValueError:
        raise ToolError("OpenAI did not answer with JSON") from None
    if not isinstance(data, dict) or not isinstance(data.get("text"), str):
        raise ToolError("OpenAI returned no transcript")
    return data["text"]


def _platform() -> str:
    return "windows" if sys.platform.startswith("win") else "mac" if sys.platform == "darwin" else "linux"


def _system_program() -> Optional[str]:
    """The program the offline `system` provider runs on this machine, or None when it is not installed."""
    kind = _platform()
    if kind == "windows":
        return shutil.which("powershell")   # Windows PowerShell: the newer `pwsh` does not ship System.Speech
    if kind == "mac":
        return shutil.which("say")
    return shutil.which("espeak-ng") or shutil.which("espeak")


def _system_synthesize(text: str, voice: str, session: Any) -> Any:
    program = _system_program()
    if not program:
        raise ToolError("this machine has no speech program (Windows PowerShell, macOS `say`, or `espeak-ng` on Linux)")
    kind = _platform()
    tmp = Path(tempfile.mkdtemp(prefix="omni-voice-"))
    try:
        src = tmp / "text.txt"
        src.write_text(text, encoding="utf-8")   # the text travels in a file: nothing of it is on the command line
        env = None
        if kind == "windows":
            out = tmp / "speech.wav"
            argv = [program, "-NoProfile", "-NonInteractive", "-Command", _POWERSHELL]
            env = {**os.environ, "OMNI_TTS_IN": str(src), "OMNI_TTS_OUT": str(out), "OMNI_TTS_VOICE": voice}
        elif kind == "mac":
            out = tmp / "speech.aiff"
            argv = [program, "-o", str(out), "-f", str(src)] + (["-v", voice] if voice else [])
        else:
            out = tmp / "speech.wav"
            argv = [program, "-w", str(out), "-f", str(src)] + (["-v", voice] if voice else [])
        try:
            done = subprocess.run(argv, env=env, stdin=subprocess.DEVNULL, capture_output=True, timeout=SYSTEM_TIMEOUT,
                                  creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) if kind == "windows" else 0)
        except subprocess.TimeoutExpired:
            raise ToolError(f"the system speech program did not finish within {SYSTEM_TIMEOUT} seconds") from None
        except OSError as e:
            raise ToolError(f"could not run the system speech program: {e}") from None
        if done.returncode != 0 or not out.is_file():
            said = (done.stderr or b"")
            said = said.decode("utf-8", "replace") if isinstance(said, bytes) else str(said)
            raise ToolError("the system speech program failed" + (f" (voice '{voice}')" if voice else "")
                            + (f": {said.strip()[-400:]}" if said.strip() else ""))
        return {"data": out.read_bytes(), "ext": out.suffix[1:]}
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


register_tts("openai", _openai_synthesize, _has_openai_key, "OpenAI speech API (set OPENAI_API_KEY)")
register_tts("system", _system_synthesize, lambda s: bool(_system_program()),
             "this machine's own voice, offline (Windows PowerShell, macOS say, Linux espeak-ng)")
register_stt("openai", _openai_transcribe, _has_openai_key, "OpenAI transcription API (set OPENAI_API_KEY)")


# ------------------------------------------------------------------------------------------- choosing and saving
def _pick(providers: Dict[str, Any], session: Any, key: str, what: str) -> Any:
    asked = str(_config(session).get(key) or "").strip().lower()
    if asked:
        p = providers.get(asked)
        if p is None:
            raise ToolError(f"no {what} provider '{asked}' (the profile's voice.{key}). Known: {', '.join(providers) or 'none'}")
        if not p.is_available(session):
            raise ToolError(f"{what} provider '{asked}' is not set up" + (f": {p.description}" if p.description else ""))
        return p
    for p in providers.values():
        if p.is_available(session):
            return p
    raise ToolError(f"No {what} provider is set up. Set one of these up and try again:\n"
                    + (_listing(providers, session, key) or "  (none registered)"))


def pick_tts(session: Any) -> TtsProvider:
    return _pick(TTS_PROVIDERS, session, "tts", "text-to-speech")


def pick_stt(session: Any) -> SttProvider:
    return _pick(STT_PROVIDERS, session, "stt", "transcription")


def _listing(providers: Dict[str, Any], session: Any, key: str) -> str:
    chosen = str(_config(session).get(key) or "").strip().lower()
    return "\n".join(f"  {p.name}  {'available' if p.is_available(session) else 'not set up'}"
                     + ("  (profile default)" if p.name == chosen else "") + (f"  - {p.description}" if p.description else "")
                     for p in providers.values())


def providers_text(session: Any) -> str:
    return ("Text to speech (speak):\n" + (_listing(TTS_PROVIDERS, session, "tts") or "  (none registered)")
            + "\nSpeech to text (transcribe):\n" + (_listing(STT_PROVIDERS, session, "stt") or "  (none registered)"))


def _extension(data: bytes) -> str:
    if data[:4] == b"RIFF":
        return ".wav"
    if data[:4] == b"FORM":
        return ".aiff"
    if data[:4] == b"OggS":
        return ".ogg"
    if data[:4] == b"fLaC":
        return ".flac"
    if data[4:8] == b"ftyp":
        return ".m4a"
    return ".mp3"


def _inside(root: Path, target: Path) -> Path:
    resolved = target.resolve()
    try:
        resolved.relative_to(root)
    except ValueError:
        raise ToolError("path must stay inside the project folder") from None
    return resolved


def _target(session: Any, text: str, path: str, ext: str) -> Path:
    root = Path(session.cwd).resolve()
    if path:
        if "\x00" in path:
            raise ToolError("path is not valid")
        target = _inside(root, root / path)
        if target == root or target.is_dir() or path.endswith(("/", "\\")):
            raise ToolError("path must be a file name, not a folder")
        if not target.suffix:
            target = target.with_name(target.name + ext)
        if target.is_dir():
            raise ToolError("path must be a file name, not a folder")
        if target.exists():
            raise ToolError(f"{target.relative_to(root).as_posix()} already exists; choose another path")
        return target
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:40].strip("-") or "speech"
    folder = _inside(root, root / "audio")
    n = 1
    while any(folder.glob(f"{slug}-{n}.*")):
        n += 1
    return folder / f"{slug}-{n}{ext}"


def _audio(result: Any, session: Any) -> Tuple[bytes, str]:
    """A provider's answer as (audio bytes, extension with the dot)."""
    ext = ""
    if isinstance(result, dict):
        ext = str(result.get("ext") or "").strip().lower().lstrip(".")
        if result.get("data") is not None:
            result = result["data"]
        elif result.get("path"):
            src = Path(str(result["path"]))
            src = src if src.is_absolute() else Path(session.cwd) / src
            ext = ext or src.suffix.lower().lstrip(".")
            try:
                if src.stat().st_size > MAX_BYTES:
                    raise ToolError("the audio is larger than 25 MB; not saved")
                result = src.read_bytes()
            except OSError as e:
                raise ToolError(f"the provider's audio file could not be read: {e}") from None
    if isinstance(result, (bytearray, memoryview)):
        result = bytes(result)
    if not isinstance(result, bytes) or not result:
        raise ToolError("the provider returned no audio")
    if len(result) > MAX_BYTES:
        raise ToolError("the audio is larger than 25 MB; not saved")
    return result, ("." + ext if _EXT.match(ext) else _extension(result))


def _play(path: Path) -> bool:
    """Start the platform's player for this file without waiting for it. True when a player was started."""
    try:
        kind = _platform()
        if kind == "windows":
            os.startfile(str(path))  # type: ignore[attr-defined]
            return True
        if kind == "mac":
            players = [["afplay"], ["open"]]
        else:
            players = [["ffplay", "-nodisp", "-autoexit", "-loglevel", "quiet"], ["mpv", "--no-video", "--really-quiet"],
                       ["paplay"], ["xdg-open"]]
            if path.suffix.lower() == ".wav":
                players.insert(0, ["aplay", "-q"])
        for argv in players:
            program = shutil.which(argv[0])
            if program:
                subprocess.Popen([program, *argv[1:], str(path)], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL)
                return True
    except Exception:
        pass
    return False


# ------------------------------------------------------------------------------------------- the tools
def _tools(session: Any) -> Dict[str, ToolDef]:
    def speak(_id, p, signal=None, on_update=None, ctx=None):
        check_abort(signal)
        text = str(p.get("text") or "").strip()
        if not text:
            raise ToolError("text is required: what should be said?")
        if len(text) > MAX_TEXT:
            raise ToolError(f"text is {len(text):,} characters; speak takes at most {MAX_TEXT:,}. Split it into parts.")
        voice = str(p.get("voice") or _config(session).get("voice") or "").strip()
        if len(voice) > 100 or re.search(r"[\x00-\x1f]", voice):
            raise ToolError("voice is not a valid voice name")
        path = str(p.get("path") or "").strip()
        if path:
            _target(session, text, path, ".mp3")   # a bad path fails before anything is synthesized (and paid for)
        provider = pick_tts(session)
        try:
            result = provider.synthesize(text, voice, session)
        except ToolError:
            raise
        except Exception as e:
            raise ToolError(f"text-to-speech provider '{provider.name}' failed: {type(e).__name__}: {e}") from None
        check_abort(signal)
        data, ext = _audio(result, session)
        target = _target(session, text, path, ext)
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            with open(target, "xb") as f:   # "x": never replaces a file that appeared meanwhile
                f.write(data)
        except FileExistsError:
            raise ToolError(f"{target.name} already exists; choose another path") from None
        except OSError as e:
            raise ToolError(f"could not save the audio: {e}") from None
        rel = target.relative_to(Path(session.cwd).resolve()).as_posix()
        played = _play(target) if p.get("play") is True else None
        note = "" if played is None else "; playing it now" if played else "; could not start a player on this machine"
        return text_result(f"Saved {rel} ({max(1, len(data) // 1024)} KB, {provider.name}){note}",
                           {"path": rel, "provider": provider.name, "bytes": len(data), "played": bool(played)})

    def transcribe(_id, p, signal=None, on_update=None, ctx=None):
        check_abort(signal)
        path = str(p.get("path") or "").strip()
        if not path or "\x00" in path:
            raise ToolError("path is required: the audio file to transcribe, relative to the project")
        root = Path(session.cwd).resolve()
        source = _inside(root, root / path)
        if not source.is_file():
            raise ToolError(f"no audio file at {path}")
        if source.suffix.lower() not in AUDIO_TYPES:
            raise ToolError(f"transcribe reads {', '.join(e[1:] for e in AUDIO_TYPES)} files; {source.name} is not one of them")
        try:
            size = source.stat().st_size
        except OSError as e:
            raise ToolError(f"could not read {path}: {e}") from None
        if size > MAX_BYTES:
            raise ToolError(f"{source.name} is {size / 1048576:.1f} MB; transcribe takes at most 25 MB. Split or compress it.")
        if size == 0:
            raise ToolError(f"{source.name} is empty")
        language = str(p.get("language") or "").strip()
        if language and not re.match(r"^[A-Za-z]{2,3}(-[A-Za-z0-9]{2,8})*$", language):
            raise ToolError("language must be a language code such as en, de or pt-BR")
        provider = pick_stt(session)
        try:
            result = provider.transcribe(source, language, session)
        except ToolError:
            raise
        except Exception as e:
            raise ToolError(f"transcription provider '{provider.name}' failed: {type(e).__name__}: {e}") from None
        if isinstance(result, dict):
            result = result.get("text")
        text = str(result or "").strip()
        rel = source.relative_to(root).as_posix()
        return text_result(text or "(no speech was recognised in the file)",
                           {"path": rel, "provider": provider.name, "characters": len(text)})

    return {
        "speak": ToolDef(
            name="speak", label="Speak",
            description="Turn text into speech and save it as an audio file in the project. Returns the saved path "
                        "(default audio/<slug>-<n>.<ext>), not the audio data. text: at most 4,000 characters. voice: "
                        "optional voice name of the provider in use. path: optional place to save, relative to the "
                        "project (existing files are not overwritten). play: true also starts playing it on this machine.",
            parameters={"type": "object", "properties": {
                "text": {"type": "string", "description": "what to say"},
                "voice": {"type": "string"},
                "path": {"type": "string", "description": "file to save to, relative to the project"},
                "play": {"type": "boolean"}},
                "required": ["text"]},
            execute=speak, prompt_snippet="Turn text into a spoken audio file"),
        "transcribe": ToolDef(
            name="transcribe", label="Transcribe audio",
            description="Return the words spoken in an audio file inside the project (mp3, wav, m4a, ogg, webm, flac; "
                        "at most 25 MB). language: optional hint such as en or de.",
            parameters={"type": "object", "properties": {
                "path": {"type": "string", "description": "audio file, relative to the project"},
                "language": {"type": "string"}},
                "required": ["path"]},
            execute=transcribe, prompt_snippet="Get the text spoken in an audio file"),
    }


CAPABILITIES = [
    Capability("voice", "Voice", "Read text aloud into an audio file and transcribe audio files to text.",
               "Hermes / OpenClaw TTS and transcription", tools=_tools,
               commands=lambda s: {"voices": ("List speech and transcription providers and whether each is set up",
                                              lambda a: providers_text(s))}),
]
