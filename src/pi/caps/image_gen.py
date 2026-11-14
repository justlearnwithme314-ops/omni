"""`image-gen`: make an image from a description and save it as a file in the project.

`image_generate(prompt, size?, provider?, path?)` asks an image provider for a picture, saves it under the project
(default `images/<slug>-<n>.png`) and returns the saved path. The image data itself never enters the conversation.

    size       1024x1024 (default) · 1536x1024 (landscape) · 1024x1536 (portrait) · auto
    provider   which backend to use; default: the profile's choice, else the first one that is set up
    path       where to save, relative to the project; an existing file is never overwritten

Built-in providers (set the key in the environment and the provider becomes available):

    openai     OPENAI_API_KEY                       model `gpt-image-1`
    gemini     GEMINI_API_KEY or GOOGLE_API_KEY     model `gemini-2.5-flash-image`

Both are written from the published API descriptions and are untested against the live services.

Profile settings (all optional): `"image_gen": {"provider": "openai", "openai_model": "gpt-image-1",
"gemini_model": "gemini-2.5-flash-image"}`. `/image-providers` lists the providers and whether each can be used.

Adding a provider (this is the slot a Hermes image-generation plugin is plugged into):

    from src.pi.caps import image_gen
    image_gen.register_provider("mine", generate, available=lambda session: True, description="My backend")

`generate(prompt, size, session)` returns the image as `bytes`, or `{"data": bytes}`, `{"url": "https://..."}`
(downloaded, http(s) only, 20 MB at most) or `{"path": "file the provider wrote"}`. Raise `ToolError` with a message
for the user when it fails. `available(session)` returns whether it can run now (omit it for "always").
`unregister_provider("mine")` removes it.
"""
from __future__ import annotations

import base64
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from ..tools.base import ToolDef, ToolError, check_abort, text_result
from . import Capability

SIZES = ("1024x1024", "1536x1024", "1024x1536", "auto")
MAX_BYTES = 20 * 1024 * 1024
TIMEOUT = 180
OPENAI_MODEL = "gpt-image-1"
GEMINI_MODEL = "gemini-2.5-flash-image"
_GEMINI_ASPECT = {"1024x1024": "1:1", "1536x1024": "3:2", "1024x1536": "2:3"}


@dataclass
class ImageProvider:
    name: str
    generate: Callable[[str, str, Any], Any]            # (prompt, size, session) -> bytes | {"data"} | {"url"} | {"path"}
    available: Optional[Callable[[Any], bool]] = None   # session -> can it run now? (None = always)
    description: str = ""

    def is_available(self, session: Any) -> bool:
        if self.available is None:
            return True
        try:
            return bool(self.available(session))
        except Exception:
            return False


PROVIDERS: Dict[str, ImageProvider] = {}


def register_provider(name: str, generate: Callable[[str, str, Any], Any],
                      available: Optional[Callable[[Any], bool]] = None, description: str = "") -> ImageProvider:
    """Add (or replace) an image provider. Providers are tried in registration order when none is asked for."""
    p = ImageProvider(str(name).strip().lower(), generate, available, description)
    if not p.name:
        raise ValueError("a provider needs a name")
    PROVIDERS[p.name] = p
    return p


def unregister_provider(name: str) -> None:
    PROVIDERS.pop(str(name).strip().lower(), None)


# ------------------------------------------------------------------------------------------- http
def _config(session: Any) -> Dict[str, Any]:
    cfg = session.profile.get("image_gen")
    return cfg if isinstance(cfg, dict) else {}


def _model(session: Any, provider: str, default: str) -> str:
    cfg = _config(session)
    return str(cfg.get(f"{provider}_model") or (cfg.get("model") if cfg.get("provider") == provider else "") or default)


def _post_json(url: str, body: Dict[str, Any], headers: Dict[str, str], who: str) -> Dict[str, Any]:
    req = urllib.request.Request(url, data=json.dumps(body).encode("utf-8"), method="POST",
                                 headers={"Content-Type": "application/json", "User-Agent": "omni-image-gen/1.0", **headers})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            raw = r.read(4 * MAX_BYTES)
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
    try:
        data = json.loads(raw.decode("utf-8", "replace"))
    except ValueError:
        raise ToolError(f"{who} did not answer with JSON") from None
    if not isinstance(data, dict):
        raise ToolError(f"{who} answered with something unexpected")
    return data


def _b64(text: Any, who: str) -> bytes:
    try:
        return base64.b64decode(str(text), validate=False)
    except ValueError:
        raise ToolError(f"{who} returned image data that is not valid base64") from None


def download(url: str) -> bytes:
    """Fetch a returned image URL: http(s) only (also after redirects), at most 20 MB."""
    if not str(url).lower().startswith(("http://", "https://")):
        raise ToolError("the provider returned an image URL that is not http(s); refusing to fetch it")
    req = urllib.request.Request(url, headers={"User-Agent": "omni-image-gen/1.0", "Accept": "image/*"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            if not str(r.geturl()).lower().startswith(("http://", "https://")):
                raise ToolError("the image URL redirected away from http(s); refusing to fetch it")
            data = r.read(MAX_BYTES + 1)
    except urllib.error.HTTPError as e:
        raise ToolError(f"HTTP {e.code} downloading the generated image") from None
    except (urllib.error.URLError, OSError) as e:
        raise ToolError(f"could not download the generated image: {getattr(e, 'reason', e)}") from None
    if len(data) > MAX_BYTES:
        raise ToolError("the generated image is larger than 20 MB; not saved")
    return data


# ------------------------------------------------------------------------------------------- built-in providers
def _openai_generate(prompt: str, size: str, session: Any) -> Any:
    key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not key:
        raise ToolError("OPENAI_API_KEY is not set")
    base = (os.environ.get("OMNI_OPENAI_IMAGE_BASE") or "https://api.openai.com").rstrip("/")
    body = {"model": _model(session, "openai", OPENAI_MODEL), "prompt": prompt, "n": 1, "size": size}
    data = _post_json(f"{base}/v1/images/generations", body, {"Authorization": f"Bearer {key}"}, "OpenAI")
    items = data.get("data")
    first = items[0] if isinstance(items, list) and items and isinstance(items[0], dict) else {}
    if first.get("b64_json"):
        return _b64(first["b64_json"], "OpenAI")
    if first.get("url"):
        return {"url": str(first["url"])}
    raise ToolError("OpenAI returned no image")


def _gemini_generate(prompt: str, size: str, session: Any) -> Any:
    key = (os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY") or "").strip()
    if not key:
        raise ToolError("GEMINI_API_KEY (or GOOGLE_API_KEY) is not set")
    base = (os.environ.get("OMNI_GEMINI_IMAGE_BASE") or "https://generativelanguage.googleapis.com").rstrip("/")
    model = urllib.parse.quote(_model(session, "gemini", GEMINI_MODEL), safe="-._")
    config: Dict[str, Any] = {"responseModalities": ["TEXT", "IMAGE"]}
    if size in _GEMINI_ASPECT:   # Gemini takes a shape, not pixels
        config["imageConfig"] = {"aspectRatio": _GEMINI_ASPECT[size]}
    body = {"contents": [{"parts": [{"text": prompt}]}], "generationConfig": config}
    data = _post_json(f"{base}/v1beta/models/{model}:generateContent", body, {"x-goog-api-key": key}, "Gemini")
    said: List[str] = []
    for cand in data.get("candidates") or []:
        parts = ((cand or {}).get("content") or {}).get("parts") or []
        for part in parts:
            if not isinstance(part, dict):
                continue
            inline = part.get("inlineData") or part.get("inline_data")
            if isinstance(inline, dict) and inline.get("data"):
                return _b64(inline["data"], "Gemini")
            if part.get("text"):
                said.append(str(part["text"]))
    blocked = (data.get("promptFeedback") or {}).get("blockReason")
    raise ToolError("Gemini returned no image" + (f" (blocked: {blocked})" if blocked else "")
                    + (f": {' '.join(said).strip()[:300]}" if said else ""))


register_provider("openai", _openai_generate, lambda s: bool(os.environ.get("OPENAI_API_KEY", "").strip()),
                  "OpenAI images API (set OPENAI_API_KEY)")
register_provider("gemini", _gemini_generate,
                  lambda s: bool((os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY") or "").strip()),
                  "Google Gemini image models (set GEMINI_API_KEY or GOOGLE_API_KEY)")


# ------------------------------------------------------------------------------------------- the tool
def pick_provider(session: Any, name: Optional[str] = None) -> ImageProvider:
    asked = str(name or "").strip().lower()
    source = "provider"
    if not asked:
        asked, source = str(_config(session).get("provider") or "").strip().lower(), "the profile's image_gen.provider"
    if asked:
        p = PROVIDERS.get(asked)
        if p is None:
            raise ToolError(f"no image provider '{asked}' ({source}). Known: {', '.join(PROVIDERS) or 'none'}")
        if not p.is_available(session):
            raise ToolError(f"image provider '{asked}' is not set up" + (f": {p.description}" if p.description else ""))
        return p
    for p in PROVIDERS.values():
        if p.is_available(session):
            return p
    raise ToolError("No image provider is set up. Set one of these up and try again:\n" + (providers_text(session) or "  (none registered)"))


def providers_text(session: Any) -> str:
    chosen = str(_config(session).get("provider") or "").strip().lower()
    return "\n".join(f"  {p.name}  {'available' if p.is_available(session) else 'not set up'}"
                     + ("  (profile default)" if p.name == chosen else "") + (f"  - {p.description}" if p.description else "")
                     for p in PROVIDERS.values())


def _extension(data: bytes) -> str:
    if data[:3] == b"\xff\xd8\xff":
        return ".jpg"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return ".webp"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return ".gif"
    return ".png"


def _inside(root: Path, target: Path) -> Path:
    resolved = target.resolve()
    try:
        resolved.relative_to(root)
    except ValueError:
        raise ToolError("path must stay inside the project folder") from None
    return resolved


def _target(session: Any, prompt: str, path: str, ext: str) -> Path:
    root = Path(session.cwd).resolve()
    if path:
        if "\x00" in path:
            raise ToolError("path is not valid")
        target = _inside(root, root / path)
        if not target.suffix:
            target = target.with_name(target.name + ext)
        if target == root or target.is_dir():
            raise ToolError("path must be a file name, not a folder")
        if target.exists():
            raise ToolError(f"{target.relative_to(root).as_posix()} already exists; choose another path")
        return target
    slug = re.sub(r"[^a-z0-9]+", "-", prompt.lower()).strip("-")[:40].strip("-") or "image"
    folder = _inside(root, root / "images")
    n = 1
    while any(folder.glob(f"{slug}-{n}.*")):
        n += 1
    return folder / f"{slug}-{n}{ext}"


def _image_bytes(result: Any, session: Any) -> bytes:
    if isinstance(result, dict):
        if result.get("data") is not None:
            result = result["data"]
        elif result.get("url"):
            result = download(str(result["url"]))
        elif result.get("path"):
            src = Path(str(result["path"]))
            src = src if src.is_absolute() else Path(session.cwd) / src
            try:
                if src.stat().st_size > MAX_BYTES:
                    raise ToolError("the generated image is larger than 20 MB; not saved")
                result = src.read_bytes()
            except OSError as e:
                raise ToolError(f"the provider's image file could not be read: {e}") from None
    if isinstance(result, (bytearray, memoryview)):
        result = bytes(result)
    if not isinstance(result, bytes) or not result:
        raise ToolError("the provider returned no image")
    if len(result) > MAX_BYTES:
        raise ToolError("the generated image is larger than 20 MB; not saved")
    return result


def _tools(session: Any) -> Dict[str, ToolDef]:
    def execute(_id, p, signal=None, on_update=None, ctx=None):
        check_abort(signal)
        prompt = str(p.get("prompt") or "").strip()
        if not prompt:
            raise ToolError("prompt is required: describe the image")
        size = str(p.get("size") or SIZES[0]).strip().lower().replace(" ", "").replace("×", "x")
        if size not in SIZES:
            raise ToolError(f"size must be one of: {', '.join(SIZES)}")
        path = str(p.get("path") or "").strip()
        if path:
            _target(session, prompt, path, ".png")   # a bad path fails before anything is generated (and paid for)
        provider = pick_provider(session, p.get("provider"))
        try:
            result = provider.generate(prompt, size, session)
        except ToolError:
            raise
        except Exception as e:
            raise ToolError(f"image provider '{provider.name}' failed: {type(e).__name__}: {e}") from None
        check_abort(signal)
        data = _image_bytes(result, session)
        target = _target(session, prompt, path, _extension(data))
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
        except OSError as e:
            raise ToolError(f"could not save the image: {e}") from None
        rel = target.relative_to(Path(session.cwd).resolve()).as_posix()
        return text_result(f"Saved {rel} ({max(1, len(data) // 1024)} KB, {provider.name}, {size})",
                           {"path": rel, "provider": provider.name, "size": size, "bytes": len(data)})

    return {"image_generate": ToolDef(
        name="image_generate", label="Generate image",
        description="Create an image from a text description and save it as a file in the project. Returns the saved "
                    "path (default images/<slug>-<n>.png), not the image data. size: 1024x1024 (default), 1536x1024 "
                    "(landscape), 1024x1536 (portrait) or auto. provider: optional backend name; path: optional place "
                    "to save, relative to the project (existing files are not overwritten).",
        parameters={"type": "object", "properties": {
            "prompt": {"type": "string", "description": "what the image should show"},
            "size": {"type": "string", "enum": list(SIZES)},
            "provider": {"type": "string"},
            "path": {"type": "string", "description": "file to save to, relative to the project"}},
            "required": ["prompt"]},
        execute=execute, prompt_snippet="Generate an image and save it to a file")}


CAPABILITIES = [
    Capability("image-gen", "Image generation", "Create images from a description and save them in the project.",
               "Hermes image_gen plugins", tools=_tools,
               commands=lambda s: {"image-providers": ("List image generation providers and whether each is set up",
                                                       lambda a: "Image providers:\n" + (providers_text(s) or "  (none registered)"))}),
]
