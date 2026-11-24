"""Hermes platform plugins as gateway channels: run Hermes' chat-platform adapters (ntfy, IRC, ...) unchanged.

A Hermes platform plugin is a folder with `plugin.yaml` (usually `kind: platform`) and an `__init__.py` whose
`register(ctx)` calls `ctx.register_platform(name=..., adapter_factory=..., check_fn=..., ...)`. The adapter is an
asyncio class built on Hermes' `gateway.platforms.base.BasePlatformAdapter`. Omni finds these plugins in

    gateway.json "hermes_platform_dirs": [...]   folders of plugins, or plugin folders themselves
    the gateway profile's "hermes_plugin_dirs"    (the same list the hermes-plugins capability uses)
    ~/.hermes/plugins/<name>/                     the user's Hermes plugins
    <omni>/plugins/hermes/<name>/                 plugins shipped with Omni
    ("hermes_default_dirs": false in gateway.json skips the last two)

and offers each registered platform as the channel type `hermes:<name>`:

    "channels": {
      "ntfy": {"type": "hermes:ntfy", "topic": "my-omni-inbox", "token": "$NTFY_TOKEN", "allow": ["my-omni-inbox"]},
      "irc":  {"type": "hermes:irc", "server": "irc.libera.chat", "channel": "#mine", "nickname": "omni-bot",
               "allow": ["alice"]}
    }

Every key except Omni's own (`type`, `enabled`, `allow`, `name`, `connect_timeout`, `send_timeout`) goes into the
adapter's `PlatformConfig`: `token`, `api_key` and `home_channel` into those fields, everything else into `extra`,
which is where Hermes adapters read their settings (`extra.topic`, `extra.server`, ...). A string value
`"$NAME"` is read from that environment variable. The adapters still read their own environment variables
(`NTFY_TOPIC`, `IRC_SERVER`, ...) too, as they do under Hermes. `allow` lists sender ids as for every channel; the
sender id is what the adapter reports as the user id (an IRC nick, the ntfy topic name). The channel name used for
`allow`, conversations and job delivery (`deliver: "ntfy:my-omni-inbox"`) is the key in "channels".

How it runs: each channel gets its own asyncio event loop on a background thread. `connect()` runs when the gateway
starts (a failure is listed under the gateway's errors), messages the adapter hands to `handle_message` become
gateway messages, replies go out through the adapter's `send(chat_id, text)` (split to the platform's message
length), the typing indicator uses `send_typing` when the adapter has one, and stopping the gateway calls
`disconnect()` and ends the loop.

Hermes itself is not needed: while such a channel is loaded Omni provides the parts of Hermes' `gateway` package
these adapters import (`gateway.config`, `gateway.platforms.base`, `.event`, `._shared`, `.helpers`,
`.send_targets`, `gateway.session`, and `agent.i18n`). If a real `gateway` package is importable, it is used
instead and nothing is replaced. An adapter that imports something outside that set fails to load, and the
channel's error names what it wanted.

What works: plain text in and out, per-chat conversations, typing indicators, adapter-side reconnects. Attachments
an adapter downloads to local files are mentioned in the message text as `[image: <path>]`, so the agent can read
them. What does not: sending media, reactions, message edits and streaming, threads (replies go to the chat, not
the thread), buttons and approval cards, slash-command menus, the Hermes setup wizards, and standalone senders for
cron. The adapters' own dependencies must be installed (`pip install httpx` for ntfy, `aiohttp` for Mattermost,
SMS and LINE); a missing one is reported with the package to install. Bundled adapters that need more of Hermes
(Telegram, Discord, Slack, Matrix, Feishu, WhatsApp, Signal, Email, ...) will report the Hermes module they need;
Omni's own `telegram`, `slack` and `discord` channel types cover those.
"""
from __future__ import annotations

import asyncio
import concurrent.futures
import contextlib
import enum
import importlib
import importlib.util
import inspect
import json
import logging
import os
import queue
import re
import sys
import tempfile
import threading
import time
import types
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

SHIM_FILE = "<omni hermes gateway shim>"
OMNI_KEYS = {"type", "enabled", "allow", "_agent_dir", "name", "connect_timeout", "send_timeout"}
HERMES_INTERNAL = {"gateway", "agent", "hermes_cli", "hermes_constants", "utils", "tools", "plugins", "providers", "pm"}
PIP_NAMES = {"discord": "discord.py", "telegram": "python-telegram-bot", "slack_bolt": "slack-bolt", "slack_sdk": "slack-sdk",
             "dingtalk_stream": "dingtalk-stream", "lark_oapi": "lark-oapi", "yaml": "pyyaml", "nacl": "pynacl",
             "Crypto": "pycryptodome", "socks": "pysocks", "aiohttp_socks": "aiohttp-socks", "mautrix": "mautrix"}
MODULE_NAMES = {v: k for k, v in PIP_NAMES.items()}

# ============================================================================================ the `gateway` shim
# Mirrors the members of Hermes' gateway package that the bundled platform adapters use. Faithful where it is cheap
# (dataclasses, dedup, secrets, target parsers); runner-owned behaviour (sessions, auth, busy queues) is left to Omni.

_TRUTHY = {"1", "true", "yes", "on"}


class Platform(enum.Enum):
    """Hermes' platform enum; any other non-empty name becomes a member on demand (`Platform("ntfy")`)."""
    LOCAL = "local"
    TELEGRAM = "telegram"
    DISCORD = "discord"
    WHATSAPP = "whatsapp"
    WHATSAPP_CLOUD = "whatsapp_cloud"
    SLACK = "slack"
    SIGNAL = "signal"
    MATTERMOST = "mattermost"
    MATRIX = "matrix"
    EMAIL = "email"
    SMS = "sms"
    DINGTALK = "dingtalk"
    API_SERVER = "api_server"
    WEBHOOK = "webhook"
    MSGRAPH_WEBHOOK = "msgraph_webhook"
    FEISHU = "feishu"
    WECOM = "wecom"
    WECOM_CALLBACK = "wecom_callback"
    WEIXIN = "weixin"
    BLUEBUBBLES = "bluebubbles"
    QQBOT = "qqbot"
    RELAY = "relay"

    @classmethod
    def _missing_(cls, value: Any) -> Any:
        if not isinstance(value, str) or not value.strip():
            return None
        value = value.strip().lower()
        if value in cls._value2member_map_:
            return cls._value2member_map_[value]
        pseudo = object.__new__(cls)
        pseudo._value_ = value
        pseudo._name_ = value.upper().replace("-", "_").replace(" ", "_")
        cls._value2member_map_[value] = pseudo
        cls._member_map_[pseudo._name_] = pseudo
        return pseudo


def _bool(value: Any, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, str):
        v = value.strip().lower()
        return True if v in _TRUTHY else False if v in {"0", "false", "no", "off"} else default
    return bool(value)


@dataclass
class HomeChannel:
    platform: Platform
    chat_id: str
    name: str
    thread_id: Optional[str] = None
    user_id: Optional[str] = None
    scope_id: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        optional = {k: v for k in ("thread_id", "user_id", "scope_id") if (v := getattr(self, k))}
        return {"platform": self.platform.value, "chat_id": self.chat_id, "name": self.name, **optional}

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "HomeChannel":
        optional = {k: str(data[k]) if data.get(k) else None for k in ("thread_id", "user_id", "scope_id")}
        return cls(platform=Platform(data.get("platform") or "local"), chat_id=str(data["chat_id"]),
                   name=data.get("name", "Home"), **optional)


@dataclass
class PlatformConfig:
    """Same fields as Hermes' `PlatformConfig`; adapters read their settings from `extra`."""
    enabled: bool = False
    token: Optional[str] = None
    api_key: Optional[str] = None
    home_channel: Optional[HomeChannel] = None
    reply_to_mode: str = "first"
    gateway_restart_notification: bool = True
    typing_indicator: bool = True
    typing_status_text: Optional[str] = None
    channel_overrides: Dict[str, Any] = field(default_factory=dict)
    extra: Dict[str, Any] = field(default_factory=dict)
    disabled_by_relay: bool = False

    _TYPED_KEYS = frozenset({"enabled", "token", "api_key", "home_channel", "reply_to_mode", "channel_overrides", "extra",
                             "gateway_restart_notification", "typing_indicator", "typing_status_text"})

    def to_dict(self) -> Dict[str, Any]:
        out = {"enabled": self.enabled, "extra": self.extra, "reply_to_mode": self.reply_to_mode,
               "typing_indicator": self.typing_indicator, **{k: v for k in ("token", "api_key") if (v := getattr(self, k))}}
        if self.home_channel:
            out["home_channel"] = self.home_channel.to_dict()
        return out

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "PlatformConfig":
        data = dict(data or {})
        extra = {**{k: v for k, v in data.items() if k not in cls._TYPED_KEYS}, **dict(data.get("extra") or {})}
        home = data.get("home_channel")
        return cls(enabled=_bool(data.get("enabled"), False), token=data.get("token"), api_key=data.get("api_key"),
                   home_channel=HomeChannel.from_dict(home) if isinstance(home, dict) else None,
                   reply_to_mode=data.get("reply_to_mode", "first"),
                   gateway_restart_notification=_bool(data.get("gateway_restart_notification"), True),
                   typing_indicator=_bool(data.get("typing_indicator"), True),
                   typing_status_text=data.get("typing_status_text"),
                   channel_overrides=dict(data.get("channel_overrides") or {}), extra=extra)


@dataclass
class GatewayConfig:
    platforms: Dict[Platform, PlatformConfig] = field(default_factory=dict)


@dataclass
class SessionSource:
    platform: Platform
    chat_id: str
    chat_name: Optional[str] = None
    chat_type: str = "dm"
    user_id: Optional[str] = None
    user_name: Optional[str] = None
    thread_id: Optional[str] = None
    chat_topic: Optional[str] = None
    user_id_alt: Optional[str] = None
    chat_id_alt: Optional[str] = None
    is_bot: bool = False
    scope_id: Optional[str] = None
    guild_id: Optional[str] = None
    parent_chat_id: Optional[str] = None
    message_id: Optional[str] = None
    role_authorized: bool = False
    profile: Optional[str] = None
    profile_route_rejected: bool = field(default=False, repr=False, compare=False)
    auto_thread_created: bool = False
    auto_thread_initial_name: Optional[str] = None
    prospective_thread_id: Optional[str] = None
    delivered_via_upstream_relay: bool = False
    detached_route: Optional[str] = field(default=None, repr=False, compare=False)


class MessageType(enum.Enum):
    TEXT = "text"
    LOCATION = "location"
    PHOTO = "photo"
    VIDEO = "video"
    AUDIO = "audio"
    VOICE = "voice"
    DOCUMENT = "document"
    STICKER = "sticker"
    COMMAND = "command"


class ProcessingOutcome(enum.Enum):
    SUCCESS = "success"
    FAILURE = "failure"
    CANCELLED = "cancelled"


@dataclass
class MessageEvent:
    """Hermes' normalized inbound message (same field order, so positional construction matches)."""
    text: str
    message_type: MessageType = MessageType.TEXT
    user_id: Optional[str] = None
    user_name: Optional[str] = None
    source: Any = None
    raw_message: Any = None
    message_id: Optional[str] = None
    ledger_message_id: Optional[str] = None
    reply_anchor_override: Optional[str] = None
    platform_update_id: Optional[int] = None
    media_urls: List[str] = field(default_factory=list)
    media_types: List[str] = field(default_factory=list)
    media_text_inlined: List[Optional[bool]] = field(default_factory=list)
    reply_to_message_id: Optional[str] = None
    reply_to_text: Optional[str] = None
    reply_to_author_id: Optional[str] = None
    reply_to_author_name: Optional[str] = None
    reply_to_is_own_message: bool = False
    prompt_response: Optional[Dict[str, Any]] = None
    auto_skill: Any = None
    channel_prompt: Optional[str] = None
    channel_context: Optional[str] = None
    internal: bool = False
    metadata: Dict[str, Any] = field(default_factory=dict)
    timestamp: datetime = field(default_factory=datetime.now)
    allow_gateway_control: bool = True
    reply_expected: Optional[bool] = None
    inherit_channel_pin: bool = False
    edit_revision: Optional[str] = None
    media_filenames: List[str] = field(default_factory=list)

    def is_command(self) -> bool:
        return self.allow_gateway_control and (self.text or "").lstrip().startswith("/")

    def get_command(self) -> Optional[str]:
        if not self.is_command():
            return None
        raw = self.text.lstrip().split(maxsplit=1)[0][1:].lower().split("@", 1)[0]
        return None if "/" in raw else raw

    def get_command_args(self) -> str:
        if not self.is_command():
            return self.text
        parts = self.text.lstrip().split(maxsplit=1)
        return parts[1] if len(parts) > 1 else ""


@dataclass
class SendResult:
    success: bool
    message_id: Optional[str] = None
    error: Optional[str] = None
    raw_response: Any = None
    retryable: bool = False
    retry_after: Optional[float] = None
    continuation_message_ids: tuple = ()
    error_kind: Optional[str] = None


def utf16_len(s: str) -> int:
    return len(s.encode("utf-16-le")) // 2


def gateway_trust_env() -> bool:
    return True


def _media_dir(kind: str) -> Path:
    d = Path(tempfile.gettempdir()) / "omni-hermes-media" / kind
    d.mkdir(parents=True, exist_ok=True)
    return d


def _cache_bytes(kind: str, data: bytes, ext: str) -> str:
    ext = ext if ext.startswith(".") else "." + ext
    p = _media_dir(kind) / f"{kind}_{uuid.uuid4().hex[:12]}{ext}"
    p.write_bytes(data)
    return str(p)


def cache_image_from_bytes(data: bytes, ext: str = ".jpg") -> str:
    return _cache_bytes("image", data, ext)


def cache_audio_from_bytes(data: bytes, ext: str = ".ogg") -> str:
    return _cache_bytes("audio", data, ext)


def cache_video_from_bytes(data: bytes, ext: str = ".mp4") -> str:
    return _cache_bytes("video", data, ext)


def cache_document_from_bytes(data: bytes, filename: str) -> str:
    safe = re.sub(r"[^\w.\-]+", "_", os.path.basename(filename or "file")) or "file"
    p = _media_dir("document") / f"doc_{uuid.uuid4().hex[:12]}_{safe}"
    p.write_bytes(data)
    return str(p)


async def cache_image_from_bytes_async(data: bytes, ext: str = ".jpg") -> str:
    return await asyncio.to_thread(cache_image_from_bytes, data, ext)


async def cache_audio_from_bytes_async(data: bytes, ext: str = ".ogg") -> str:
    return await asyncio.to_thread(cache_audio_from_bytes, data, ext)


async def cache_video_from_bytes_async(data: bytes, ext: str = ".mp4") -> str:
    return await asyncio.to_thread(cache_video_from_bytes, data, ext)


async def cache_document_from_bytes_async(data: bytes, filename: str) -> str:
    return await asyncio.to_thread(cache_document_from_bytes, data, filename)


def _split_text(content: str, max_length: int) -> List[str]:
    """Split on newlines, then spaces, with `(i/n)` markers (Hermes' `truncate_message`, without fence repair)."""
    if len(content) <= max_length:
        return [content]
    budget = max(1, max_length - 10)
    chunks, rest = [], content
    while rest:
        if len(rest) <= budget:
            chunks.append(rest)
            break
        region = rest[:budget]
        cut = region.rfind("\n")
        if cut < budget // 2:
            cut = region.rfind(" ")
        if cut < 1:
            cut = budget
        chunks.append(rest[:cut])
        rest = rest[cut:].lstrip()
    return [f"{c} ({i + 1}/{len(chunks)})" for i, c in enumerate(chunks)] if len(chunks) > 1 else chunks


class BasePlatformAdapter(ABC):
    """The adapter contract: `connect()`, `disconnect()`, `send()`, `get_chat_info()`; inbound goes through
    `handle_message(event)`, which hands it to the handler Omni installs."""
    supports_code_blocks = False
    supports_status_text = False
    supports_async_delivery = True
    splits_long_messages = False
    typed_command_prefix = "/"
    interactive_resume = True
    serves_profile_prefix = False
    REQUIRES_EDIT_FINALIZE = False
    gateway_runner = None

    def __init__(self, config: PlatformConfig, platform: Platform):
        self.config, self.platform = config, platform
        self._message_handler: Optional[Callable[..., Any]] = None
        self._fatal_error_handler: Optional[Callable[..., Any]] = None
        self._running, self._fatal_error_retryable = False, True
        self._fatal_error_code: Optional[str] = None
        self._fatal_error_message: Optional[str] = None
        self._active_sessions: Dict[str, Any] = {}
        self._pending_messages: Dict[str, Any] = {}
        self._background_tasks: set = set()
        self._owner_profile: Optional[str] = None
        self._typing_paused: set = set()
        self._status_text: Dict[str, str] = {}
        self._plugin_handler_native: Any = None
        self._plugin_handlers_wired: Optional[set] = None

    # --- identity and state
    @property
    def name(self) -> str:
        return self.platform.value.title()

    @property
    def is_connected(self) -> bool:
        return self._running

    @property
    def has_fatal_error(self) -> bool:
        return self._fatal_error_code is not None

    @property
    def fatal_error_code(self) -> Optional[str]:
        return self._fatal_error_code

    @property
    def fatal_error_message(self) -> Optional[str]:
        return self._fatal_error_message

    @property
    def fatal_error_retryable(self) -> bool:
        return self._fatal_error_retryable

    @property
    def send_path_degraded(self) -> bool:
        return False

    @property
    def message_len_fn(self) -> Callable[[str], int]:
        return len

    def max_message_length_for_chat(self, chat_id: str) -> int:
        return int(getattr(self, "MAX_MESSAGE_LENGTH", 4096) or 4096)

    def _mark_connected(self, *, listener_base: Optional[str] = None) -> None:
        self._running = True
        self._fatal_error_code = self._fatal_error_message = None
        self._fatal_error_retryable = True

    def _mark_degraded(self) -> None:
        pass

    def _mark_disconnected(self) -> None:
        self._running = False

    def _set_fatal_error(self, code: str, message: str, *, retryable: bool) -> None:
        self._running = False
        self._fatal_error_code, self._fatal_error_message, self._fatal_error_retryable = code, message, retryable

    def _write_runtime_status_safe(self, context: str, **kwargs: Any) -> None:
        pass

    async def _notify_fatal_error(self) -> None:
        handler = self._fatal_error_handler
        if handler:
            result = handler(self)
            if inspect.isawaitable(result):
                await result

    def _acquire_platform_lock(self, scope: str, identity: str, resource_desc: str) -> bool:
        return True   # one gateway per agent dir; Omni does not run several profiles in one process

    def _release_platform_lock(self) -> None:
        pass

    def _wire_plugin_handlers(self, native: Any = None) -> None:
        pass

    def rewire_plugin_handlers(self) -> None:
        pass

    # --- runner wiring (Omni installs the message and fatal-error handlers; the rest is runner policy Omni keeps itself)
    def set_message_handler(self, handler: Callable[..., Any]) -> None:
        self._message_handler = handler

    def set_fatal_error_handler(self, handler: Callable[..., Any]) -> None:
        self._fatal_error_handler = handler

    def set_platform_event_handler(self, handler: Any) -> None:
        pass

    def set_reaction_handler(self, handler: Any) -> None:
        pass

    def set_busy_session_handler(self, handler: Any) -> None:
        pass

    def set_authorization_check(self, check: Any) -> None:
        pass

    def set_session_store(self, store: Any) -> None:
        pass

    def set_owner_profile(self, profile_name: Optional[str]) -> None:
        self._owner_profile = profile_name

    def set_topic_recovery_fn(self, fn: Any) -> None:
        pass

    def set_status_text(self, chat_id: str, text: Optional[str]) -> None:
        if text:
            self._status_text[str(chat_id)] = text
        else:
            self._status_text.pop(str(chat_id), None)

    def _is_sender_authorized(self, user_id: Optional[str], chat_type: Optional[str] = None, *_a: Any, **_k: Any) -> bool:
        return True   # Omni's `allow` list decides

    def has_pending_interrupt(self, session_key: str) -> bool:
        return False

    def get_pending_message(self, session_key: str) -> Any:
        return None

    async def interrupt_session_activity(self, session_key: str, chat_id: str, metadata: Any = None) -> None:
        pass

    async def on_processing_start(self, event: Any) -> None:
        pass

    async def on_processing_complete(self, event: Any, outcome: Any) -> None:
        pass

    async def cancel_background_tasks(self) -> None:
        for t in list(self._background_tasks):
            t.cancel()
        self._background_tasks.clear()

    # --- inbound
    async def handle_message(self, event: MessageEvent) -> None:
        if self._message_handler is None:
            logger.error("[%s] dropping inbound message: no message handler installed", self.name)
            return
        result = self._message_handler(event)
        if inspect.isawaitable(result):
            await result

    def build_source(self, chat_id: str, chat_name: Optional[str] = None, chat_type: str = "dm",
                     user_id: Optional[str] = None, user_name: Optional[str] = None, thread_id: Optional[str] = None,
                     chat_topic: Optional[str] = None, user_id_alt: Optional[str] = None, chat_id_alt: Optional[str] = None,
                     is_bot: bool = False, scope_id: Optional[str] = None, guild_id: Optional[str] = None,
                     parent_chat_id: Optional[str] = None, message_id: Optional[str] = None, role_authorized: bool = False,
                     auto_thread_created: bool = False, auto_thread_initial_name: Optional[str] = None) -> SessionSource:
        def opt(v: Any) -> Optional[str]:
            return str(v) if v else None
        return SessionSource(platform=self.platform, chat_id=str(chat_id), chat_name=chat_name, chat_type=chat_type,
                             user_id=None if user_id in (None, "") else str(user_id), user_name=user_name,
                             thread_id=opt(thread_id), chat_topic=(chat_topic or "").strip() or None, user_id_alt=user_id_alt,
                             chat_id_alt=chat_id_alt, is_bot=is_bot, scope_id=opt(scope_id), guild_id=opt(guild_id),
                             parent_chat_id=opt(parent_chat_id), message_id=opt(message_id), role_authorized=role_authorized,
                             profile=self._owner_profile, auto_thread_created=auto_thread_created,
                             auto_thread_initial_name=auto_thread_initial_name)

    # --- outbound
    @abstractmethod
    async def connect(self, *, is_reconnect: bool = False) -> bool: ...

    @abstractmethod
    async def disconnect(self) -> None: ...

    @abstractmethod
    async def send(self, chat_id: str, content: str, reply_to: Optional[str] = None,
                   metadata: Optional[Dict[str, Any]] = None) -> SendResult: ...

    @abstractmethod
    async def get_chat_info(self, chat_id: str) -> Dict[str, Any]: ...

    async def send_typing(self, chat_id: str, metadata: Any = None) -> None:
        pass

    async def stop_typing(self, chat_id: str) -> None:
        pass

    async def edit_message(self, chat_id: str, message_id: str, content: str, *, finalize: bool = False) -> SendResult:
        return SendResult(success=False, error="Not supported")

    async def delete_message(self, chat_id: str, message_id: str) -> bool:
        return False

    async def send_private_notice(self, chat_id: str, user_id: Optional[str], content: str, reply_to: Optional[str] = None,
                                  metadata: Optional[Dict[str, Any]] = None) -> SendResult:
        return await self.send(chat_id=chat_id, content=content, reply_to=reply_to, metadata=metadata)

    async def send_image(self, chat_id: str, image_url: str, caption: Optional[str] = None, reply_to: Optional[str] = None,
                         metadata: Optional[Dict[str, Any]] = None) -> SendResult:
        return await self.send(chat_id=chat_id, content=f"{caption}\n{image_url}" if caption else image_url,
                               reply_to=reply_to, metadata=metadata)

    def format_message(self, content: str) -> str:
        return content

    def warning_text(self, visible: str, hidden: Optional[str] = "", **_kw: Any) -> Optional[str]:
        return visible

    @staticmethod
    def truncate_message(content: str, max_length: int = 4096, len_fn: Optional[Callable[[str], int]] = None) -> List[str]:
        return _split_text(content, max_length)


# --- gateway.platforms._shared
_URL_SECRET_RE = re.compile(r"([?&](?:access_token|api[_-]?key|auth[_-]?token|token|signature|sig)=)([^&#\s]+)", re.I)
_ASSIGN_SECRET_RE = re.compile(r"\b(access_token|api[_-]?key|auth[_-]?token|signature|sig)\s*=\s*([^\s,;]+)", re.I)


def get_scoped_secret(name: str, default: Any = None, *, external_fallback: bool = False) -> Any:
    val = os.environ.get(name)
    return val if val is not None else default


def platform_gate_env(name: str, default: str = "") -> str:
    return (os.getenv(name) or default).strip() if name else default


def decode_json_list_literal(raw: Any) -> Any:
    if isinstance(raw, str) and raw.lstrip()[:1] == "[":
        try:
            loaded = json.loads(raw)
        except ValueError:
            return raw
        if isinstance(loaded, list):
            return loaded
    return raw


def extra_or_secret(extra: Optional[dict], key: str, env: str, default: Any = "", *, blank_is_unset: bool = True) -> Any:
    if env:
        v = get_scoped_secret(env, None)
        if v is not None and str(v).strip():
            return v
    value = (extra or {}).get(key)
    if value is None or (blank_is_unset and isinstance(value, str) and not value.strip()):
        return default
    return value


def profile_scoped() -> bool:
    return False


def yaml_env_setter() -> Callable[[str, Any], None]:
    def set_env(name: str, value: Any) -> None:
        if value is None or os.getenv(name):
            return
        os.environ[name] = ",".join(str(v) for v in value) if isinstance(value, list) else str(value)
    return set_env


def send_error(message: Any) -> dict:
    text = _URL_SECRET_RE.sub(lambda m: f"{m.group(1)}***", str(message))
    return {"error": _ASSIGN_SECRET_RE.sub(lambda m: f"{m.group(1)}=***", text)}


_YAML_KINDS = {"lower": (lambda c, k: k in c, lambda v: str(v).lower()), "str": (lambda c, k: c.get(k) not in (None, ""), str),
               "csv": (lambda c, k: c.get(k) is not None, lambda v: v), "json": (lambda c, k: k in c, json.dumps)}


def apply_yaml_bridge(cfg: dict, spec: Any) -> Optional[dict]:
    set_env, seeded = yaml_env_setter(), {}
    for key, env, kind in spec:
        applies, encode = _YAML_KINDS[kind]
        if applies(cfg, key):
            seeded[key] = cfg[key]
            set_env(env, encode(cfg[key]))
    return seeded or None


def seed_extra_from_env(spec: Any, *, home_env: Optional[str] = None, home_default: str = "") -> dict:
    seed: dict = {}
    for env, key, conv in spec:
        raw = str(get_scoped_secret(env, "") or "").strip()
        if raw:
            with contextlib.suppress(ValueError):
                seed[key] = conv(raw) if conv else raw
    if home_env:
        home = str(get_scoped_secret(home_env, "") or "").strip() or home_default
        if home:
            seed["home_channel"] = {"chat_id": home, "name": get_scoped_secret(f"{home_env}_NAME", "Home")}
    return seed


def env_is_connected(*names: str) -> Callable[[Any], bool]:
    return lambda config: all((os.getenv(n) or "").strip() for n in names)


def config_connected_extra(*keys: str) -> Callable[[Any], bool]:
    return lambda config: bool(config.token or config.api_key) or all(config.extra.get(k) for k in keys)


def coerce_port(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return default


# --- gateway.platforms.helpers
class MessageDeduplicator:
    def __init__(self, max_size: int = 2000, ttl_seconds: float = 300):
        self._seen: Dict[str, float] = {}
        self._max_size, self._ttl = max_size, ttl_seconds

    def is_duplicate(self, msg_id: str) -> bool:
        if not msg_id:
            return False
        now = time.time()
        if msg_id in self._seen:
            if now - self._seen[msg_id] < self._ttl:
                return True
            del self._seen[msg_id]
        self._seen[msg_id] = now
        if len(self._seen) > self._max_size:
            cutoff = now - self._ttl
            self._seen = {k: v for k, v in self._seen.items() if v > cutoff}
            if len(self._seen) > self._max_size:
                self._seen = dict(sorted(self._seen.items(), key=lambda kv: kv[1])[-self._max_size:])
        return False

    def contains(self, msg_id: str) -> bool:
        at = self._seen.get(msg_id) if msg_id else None
        if at is None:
            return False
        if time.time() - at < self._ttl:
            return True
        del self._seen[msg_id]
        return False

    def discard(self, msg_id: str) -> None:
        self._seen.pop(msg_id, None)

    def clear(self) -> None:
        self._seen.clear()

    def absorb(self, other: "MessageDeduplicator") -> None:
        cutoff = time.time() - self._ttl
        self._seen.update({k: v for k, v in other._seen.items() if v > cutoff and k not in self._seen})


async def cancel_task(task: Optional[asyncio.Task]) -> None:
    if task is None or task.done():
        return
    task.cancel()
    if task is not asyncio.current_task():
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task


def bounded_put(store: Any, key: str, value: Any, cap: int) -> None:
    store.pop(key, None)
    store[key] = value
    while len(store) > cap:
        del store[next(iter(store))]


_STRIP_RULES = ((re.compile(r"\*\*(.+?)\*\*", re.DOTALL), r"\1"), (re.compile(r"\*(.+?)\*", re.DOTALL), r"\1"),
                (re.compile(r"\b__(?![\s_])(.+?)(?<![\s_])__\b", re.DOTALL), r"\1"),
                (re.compile(r"\b_(?![\s_])(.+?)(?<![\s_])_\b", re.DOTALL), r"\1"),
                (re.compile(r"```[a-zA-Z0-9_+-]*\n?"), ""), (re.compile(r"`(.+?)`"), r"\1"),
                (re.compile(r"^#{1,6}\s+", re.MULTILINE), ""))
_MD_LINK_RE = re.compile(r"\[([^\]]+)\]\(([^\)]+)\)")


def strip_markdown(text: str, *, keep_link_targets: bool = False) -> str:
    for pattern, repl in _STRIP_RULES:
        text = pattern.sub(repl, text)

    def keep(m: "re.Match[str]") -> str:
        label, target = m.group(1).strip(), m.group(2).strip()
        if not re.match(r"https?://", target, re.I):
            return m.group(1)
        return target if label == target else f"{label}\n{target}"
    text = _MD_LINK_RE.sub(keep if keep_link_targets else r"\1", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


async def send_chunks(chunks: list, send_one: Callable[[Any], Any]) -> Any:
    result = SendResult(success=False, error="nothing to send")
    for delivered, chunk in enumerate(chunks):
        result = await send_one(chunk)
        if not result.success:
            if delivered:
                raw = dict(result.raw_response) if isinstance(result.raw_response, dict) else {}
                raw.update(partial_overflow=True, delivered_chunks=delivered, total_chunks=len(chunks))
                result.raw_response = raw
            break
    return result


def redact_phone(phone: str) -> str:
    if not phone:
        return "<none>"
    if len(phone) <= 8:
        return phone[:2] + "****" + phone[-2:] if len(phone) > 4 else "****"
    return phone[:4] + "****" + phone[-4:]


def fence_state_after(text: str, in_code: bool = False, lang: str = "") -> Tuple[bool, str]:
    for line in text.split("\n"):
        s = line.strip()
        if s.startswith("```"):
            tag = s[3:].split()
            in_code, lang = (False, "") if in_code else (True, tag[0] if tag else "")
    return in_code, lang


# --- gateway.platforms.send_targets
NOT_EXPLICIT = object()


def regex_groups_parser(regex: Any, *, thread_group: bool = True) -> Callable[[str], Any]:
    return lambda ref: ((m.group(1), m.group(2) if thread_group else None) if (m := regex.fullmatch(ref)) else None)


def regex_stripped_parser(regex: Any) -> Callable[[str], Any]:
    return lambda ref: (ref.strip(), None) if regex.fullmatch(ref) else None


def parse_nonempty(ref: str) -> Any:
    s = ref.strip()
    return (s, None) if s else None


@dataclass(frozen=True)
class StandaloneSendPolicy:
    label: str = ""
    media: bool = False
    all_sends: bool = False
    captionable: bool = False
    empty_list_between_chunks: bool = False
    forward_force_document: bool = False
    mentions: bool = False
    text_direct: bool = False


# --- agent.i18n (the only piece of Hermes' agent package the simple adapters import at top level)
_I18N = {"platform.irc.quit_message": "Goodbye", "platform.irc.standalone_quit": "Goodbye"}


def t(key: str, *args: Any, **kwargs: Any) -> str:
    text = _I18N.get(key) or key.rsplit(".", 1)[-1].replace("_", " ").capitalize()
    with contextlib.suppress(Exception):
        text = text.format(*args, **kwargs)
    return text


_SHIM_SPEC: Dict[str, List[str]] = {
    "gateway": [],
    "gateway.config": ["Platform", "PlatformConfig", "HomeChannel", "GatewayConfig"],
    "gateway.session": ["SessionSource"],
    "gateway.platforms": [],
    "gateway.platforms.event": ["MessageEvent", "MessageType", "ProcessingOutcome"],
    "gateway.platforms.base": ["BasePlatformAdapter", "SendResult", "utf16_len", "gateway_trust_env",
                               "cache_image_from_bytes", "cache_audio_from_bytes", "cache_video_from_bytes",
                               "cache_document_from_bytes", "cache_image_from_bytes_async", "cache_audio_from_bytes_async",
                               "cache_video_from_bytes_async", "cache_document_from_bytes_async"],
    "gateway.platforms._shared": ["get_scoped_secret", "platform_gate_env", "decode_json_list_literal", "extra_or_secret",
                                  "profile_scoped", "yaml_env_setter", "send_error", "apply_yaml_bridge",
                                  "seed_extra_from_env", "env_is_connected", "config_connected_extra", "coerce_port"],
    "gateway.platforms.helpers": ["MessageDeduplicator", "cancel_task", "bounded_put", "strip_markdown", "send_chunks",
                                  "redact_phone", "fence_state_after"],
    "gateway.platforms.send_targets": ["NOT_EXPLICIT", "regex_groups_parser", "regex_stripped_parser", "parse_nonempty",
                                       "StandaloneSendPolicy"],
}

_shim_lock = threading.RLock()
_shim_users = 0
_shim_modules: Dict[str, types.ModuleType] = {}
_shim_inserted: List[str] = []
_agent_attr_added: List[types.ModuleType] = []


def _build_shim() -> Dict[str, types.ModuleType]:
    if _shim_modules:
        return _shim_modules
    here = sys.modules[__name__]
    for name, members in _SHIM_SPEC.items():
        m = types.ModuleType(name, f"Omni's stand-in for Hermes' {name} (see {__name__})")
        m.__file__ = SHIM_FILE
        m._omni_hermes_shim = True  # type: ignore[attr-defined]
        if name in ("gateway", "gateway.platforms"):
            m.__path__ = []  # type: ignore[attr-defined]
        for attr in members:
            setattr(m, attr, getattr(here, attr))
        _shim_modules[name] = m
    for name, m in _shim_modules.items():
        parent, _, leaf = name.rpartition(".")
        if parent:
            setattr(_shim_modules[parent], leaf, m)
    i18n = types.ModuleType("agent.i18n")
    i18n.__file__ = SHIM_FILE
    i18n._omni_hermes_shim = True  # type: ignore[attr-defined]
    i18n.t = t  # type: ignore[attr-defined]
    _shim_modules["agent.i18n"] = i18n
    return _shim_modules


def _spec_exists(name: str) -> bool:
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):
        return True


def real_gateway_present() -> bool:
    """True when a `gateway` package other than this shim is loaded or importable (then the shim stays out)."""
    m = sys.modules.get("gateway")
    if m is not None:
        return not getattr(m, "_omni_hermes_shim", False)
    return _spec_exists("gateway")


def _agent_is_real() -> bool:
    m = sys.modules.get("agent")
    if m is not None:
        return bool(getattr(m, "__file__", None)) and not getattr(m, "_omni_hermes_shim", False)
    return _spec_exists("agent")


def _shim_acquire() -> None:
    global _shim_users
    with _shim_lock:
        _shim_users += 1
        if _shim_users > 1:
            return
        mods = _build_shim()
        if not real_gateway_present():
            for name, m in mods.items():
                if name.startswith("gateway") and name not in sys.modules:
                    sys.modules[name] = m
                    _shim_inserted.append(name)
        if "agent.i18n" not in sys.modules and not _agent_is_real():
            pkg = sys.modules.get("agent")
            if pkg is None:
                pkg = types.ModuleType("agent")
                pkg.__path__ = []  # type: ignore[attr-defined]
                pkg._omni_hermes_shim = True  # type: ignore[attr-defined]
                sys.modules["agent"] = pkg
                _shim_inserted.append("agent")
            if not hasattr(pkg, "i18n"):
                pkg.i18n = mods["agent.i18n"]  # type: ignore[attr-defined]
                _agent_attr_added.append(pkg)
            sys.modules["agent.i18n"] = mods["agent.i18n"]
            _shim_inserted.append("agent.i18n")


def _shim_release() -> None:
    global _shim_users
    with _shim_lock:
        _shim_users = max(0, _shim_users - 1)
        if _shim_users:
            return
        for name in reversed(_shim_inserted):
            if getattr(sys.modules.get(name), "_omni_hermes_shim", False):
                del sys.modules[name]
        _shim_inserted.clear()
        for pkg in _agent_attr_added:
            with contextlib.suppress(AttributeError):
                delattr(pkg, "i18n")
        _agent_attr_added.clear()


@contextlib.contextmanager
def shim_installed() -> Any:
    _shim_acquire()
    try:
        yield
    finally:
        _shim_release()


# ============================================================================================ errors
def _pip_name(module: str) -> str:
    return PIP_NAMES.get(module, module.replace("_", "-"))


def explain_import_error(e: BaseException) -> str:
    """One line saying why an adapter could not be imported, naming the package to install when that is the fix."""
    if isinstance(e, ModuleNotFoundError) and e.name:
        top = e.name.split(".")[0]
        if top == "gateway":
            if real_gateway_present():
                return f"could not import {e.name} from the installed `gateway` package ({e})"
            return f"uses {e.name}, which Omni's Hermes compatibility layer does not provide (it needs the full Hermes gateway)"
        if top in HERMES_INTERNAL:
            return f"imports {e.name} from Hermes itself, which is not installed (only Hermes' gateway basics are provided)"
        return f"needs the Python package '{_pip_name(top)}', which is not installed. Run: pip install {_pip_name(top)}"
    m = re.search(r"cannot import name '([^']+)' from '([^']+)'", str(e))
    if m and m.group(2).split(".")[0] in HERMES_INTERNAL:
        if m.group(2).startswith("gateway") and real_gateway_present():
            return f"the installed `gateway` package has no {m.group(2)}.{m.group(1)}"
        return (f"uses {m.group(2)}.{m.group(1)}, which Omni's Hermes compatibility layer does not provide "
                "(it needs the full Hermes gateway)")
    return f"{type(e).__name__}: {e}"


def _hint_packages(hint: str) -> List[str]:
    m = re.search(r"pip install\s+([^#\n]+)", hint or "")
    if not m:
        return []
    out = []
    for tok in m.group(1).split():
        tok = tok.strip("'\"`")
        if tok and not tok.startswith("-"):
            out.append(tok)
    return out


def missing_packages(entry: Dict[str, Any]) -> List[str]:
    """Packages a registered platform needs that are not importable. Only asked when its `check_fn` says no."""
    check = entry.get("check_fn")
    try:
        ok = bool(check()) if callable(check) else True
    except Exception:
        ok = False
    if ok:
        return []
    missing: List[str] = []
    hinted = _hint_packages(str(entry.get("install_hint") or ""))
    for pkg in hinted:
        base = re.split(r"[<>=!~\[;]", pkg, 1)[0]
        module = MODULE_NAMES.get(base, base.replace("-", "_"))
        if not _spec_exists(module):
            missing.append(base)
    if not hinted:
        for mod in entry.get("modules") or []:
            for k, v in list(vars(mod).items()):
                if k.endswith("_AVAILABLE") and v is False:
                    name = k[: -len("_AVAILABLE")].lower()
                    if not _spec_exists(name):
                        missing.append(_pip_name(name))
    return sorted(set(missing))


# ============================================================================================ discovery and loading
def _manifest(path: Path) -> Dict[str, Any]:
    try:
        from .caps.hermes_plugins import _parse_manifest
        return _parse_manifest(path)
    except Exception:
        return {}


def is_platform_plugin(d: Path) -> bool:
    if not ((d / "plugin.yaml").is_file() and (d / "__init__.py").is_file()):
        return False
    if str(_manifest(d / "plugin.yaml").get("kind") or "").strip().lower() == "platform":
        return True
    for p in d.glob("*.py"):
        with contextlib.suppress(OSError):
            if "register_platform" in p.read_text(encoding="utf-8", errors="replace"):
                return True
    return False


def search_roots(agent_dir: Any, config: Dict[str, Any]) -> List[Path]:
    roots: List[Path] = []
    for e in config.get("hermes_platform_dirs") or []:
        roots.append(Path(os.path.expanduser(str(e))))
    with contextlib.suppress(Exception):
        from .profiles import load_profile
        for e in load_profile(config.get("profile") or None).get("hermes_plugin_dirs") or []:
            roots.append(Path(os.path.expanduser(str(e))))
    if config.get("hermes_default_dirs", True) is not False:
        from .profiles import PROJECT_ROOT
        roots += [Path.home() / ".hermes" / "plugins", PROJECT_ROOT / "plugins" / "hermes"]
    return roots


def discover(agent_dir: Any, config: Dict[str, Any]) -> List[Path]:
    out: List[Path] = []
    seen = set()
    for r in search_roots(agent_dir, config):
        if not r.is_dir():
            continue
        dirs = [r] if (r / "plugin.yaml").is_file() else sorted(d for d in r.iterdir() if d.is_dir())
        for d in dirs:
            key = str(d.resolve())
            if key not in seen and is_platform_plugin(d):
                seen.add(key)
                out.append(d)
    return out


class PlatformPluginContext:
    """The `ctx` a platform plugin's `register(ctx)` sees: `register_platform` is captured, other kinds are noted."""

    def __init__(self, manifest: Dict[str, Any], path: Path):
        self.manifest = types.SimpleNamespace(**{"name": path.name, "version": "0", "description": "", **manifest})
        self.plugin_id = str(manifest.get("name") or path.name)
        self.platforms: List[Dict[str, Any]] = []
        self.ignored: List[str] = []

    def register_platform(self, name: str, label: str = "", adapter_factory: Optional[Callable] = None,
                          check_fn: Optional[Callable] = None, validate_config: Optional[Callable] = None,
                          required_env: Optional[list] = None, install_hint: str = "", **entry_kwargs: Any) -> None:
        if not callable(adapter_factory):
            raise TypeError(f"register_platform({name!r}): adapter_factory is not callable")
        self.platforms.append({"name": str(name).strip().lower(), "label": label or name, "adapter_factory": adapter_factory,
                               "check_fn": check_fn, "validate_config": validate_config, "required_env": list(required_env or []),
                               "install_hint": install_hint, "plugin": self.plugin_id, **entry_kwargs})

    def get_config(self, key: str, default: Any = None) -> Any:
        return default

    def __getattr__(self, name: str) -> Any:
        if name.startswith("register_"):
            def stub(*_a: Any, **_k: Any) -> None:
                self.ignored.append(name[len("register_"):])
            return stub
        raise AttributeError(name)


_load_cache: Dict[Tuple[str, float], Dict[str, Any]] = {}
REGISTERED: Dict[str, Dict[str, Any]] = {}     # platform name -> entry (what register_platform received, plus modules)


def _stamp(d: Path) -> float:
    return max([p.stat().st_mtime for p in d.glob("*.py")] + [0.0])


def load_plugin(d: Path) -> Dict[str, Any]:
    """Import one platform plugin folder and run its `register(ctx)`.
    Returns {"names": [...candidate names], "platforms": [entries], "error": str|None}."""
    key = (str(d.resolve()), _stamp(d))
    if key in _load_cache:
        return _load_cache[key]
    manifest = _manifest(d / "plugin.yaml")
    ctx = PlatformPluginContext(manifest, d)
    names = {d.name.lower(), re.sub(r"[-_]platform$", "", ctx.plugin_id.lower())}
    if manifest.get("label"):
        names.add(str(manifest["label"]).strip().lower().replace(" ", "_"))
    result: Dict[str, Any] = {"names": sorted(names), "platforms": [], "error": None, "path": str(d), "ignored": ctx.ignored}
    mod_name = f"omni_hermes_platform_{re.sub(r'[^0-9a-zA-Z_]', '_', d.name)}_{abs(hash(key[0])) % 10**8}"
    with shim_installed():
        try:
            spec = importlib.util.spec_from_file_location(mod_name, d / "__init__.py", submodule_search_locations=[str(d)])
            mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
            sys.modules[mod_name] = mod
            spec.loader.exec_module(mod)  # type: ignore[union-attr]
            register = getattr(mod, "register", None)
            if not callable(register):
                raise RuntimeError("no register(ctx) function")
            value = register(ctx)
            if inspect.isawaitable(value):
                asyncio.run(value)  # type: ignore[arg-type]
            if not ctx.platforms:
                raise RuntimeError("register(ctx) did not call ctx.register_platform")
            modules = [m for n, m in list(sys.modules.items()) if n == mod_name or n.startswith(mod_name + ".")]
            cfg_mod = sys.modules.get("gateway.config")
            for entry in ctx.platforms:
                entry["modules"] = modules
                entry["config_cls"] = getattr(cfg_mod, "PlatformConfig", PlatformConfig)
                entry["path"] = str(d)
            result["platforms"] = ctx.platforms
        except BaseException as e:  # noqa: BLE001 - a plugin may raise anything, even SystemExit
            if isinstance(e, KeyboardInterrupt):
                raise
            for n in [n for n in sys.modules if n == mod_name or n.startswith(mod_name + ".")]:
                del sys.modules[n]
            result["error"] = explain_import_error(e)
    _load_cache[key] = result
    return result


# ============================================================================================ the channel
def _resolve(value: Any, resolve_secret: Callable[[Any], str]) -> Any:
    if isinstance(value, str):
        return resolve_secret(value) if value.startswith("$") else value
    if isinstance(value, dict):
        return {k: _resolve(v, resolve_secret) for k, v in value.items()}
    if isinstance(value, list):
        return [_resolve(v, resolve_secret) for v in value]
    return value


def platform_config(entry: Dict[str, Any], cfg: Dict[str, Any], resolve_secret: Callable[[Any], str]) -> Any:
    data = {k: _resolve(v, resolve_secret) for k, v in cfg.items() if k not in OMNI_KEYS}
    home = data.get("home_channel")
    if isinstance(home, (str, int)) and str(home):
        data["home_channel"] = {"platform": entry["name"], "chat_id": str(home), "name": "Home"}
    elif isinstance(home, dict):
        data["home_channel"] = {"platform": entry["name"], "name": "Home", **home}
    data["enabled"] = True
    cls = entry.get("config_cls") or PlatformConfig
    pcfg = cls.from_dict(data)
    seed_fn = entry.get("env_enablement_fn")
    if callable(seed_fn):
        with contextlib.suppress(Exception):
            seed = seed_fn()
            for k, v in (seed or {}).items():
                if k != "home_channel" and k not in pcfg.extra and k not in ("token", "api_key"):
                    pcfg.extra[k] = v
    return pcfg


class _Loop:
    """An asyncio event loop on its own thread, for one adapter."""

    def __init__(self, name: str):
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self._main, daemon=True, name=f"omni-hermes-{name}")
        self.thread.start()

    def _main(self) -> None:
        asyncio.set_event_loop(self.loop)
        self.loop.run_forever()

    def run(self, coro: Any, timeout: float) -> Any:
        fut = asyncio.run_coroutine_threadsafe(coro, self.loop)
        try:
            return fut.result(timeout)
        except concurrent.futures.TimeoutError:
            fut.cancel()
            raise TimeoutError(f"no answer within {timeout:g}s") from None

    def stop(self) -> None:
        if self.loop.is_closed():
            return

        async def drain() -> None:
            tasks = [x for x in asyncio.all_tasks() if x is not asyncio.current_task()]
            for x in tasks:
                x.cancel()
            if tasks:
                await asyncio.wait(tasks, timeout=5)
            with contextlib.suppress(Exception):
                await asyncio.wait_for(self.loop.shutdown_asyncgens(), 5)
            with contextlib.suppress(Exception):
                await asyncio.wait_for(self.loop.shutdown_default_executor(), 5)
        if self.thread.is_alive():
            with contextlib.suppress(Exception):
                self.run(drain(), 15)
            self.loop.call_soon_threadsafe(self.loop.stop)
            self.thread.join(10)
        if not self.thread.is_alive():
            self.loop.close()


_CLOSED = object()


def _defines_own(adapter: Any, method: str) -> bool:
    for klass in type(adapter).__mro__:
        if method in vars(klass):
            return klass.__name__ != "BasePlatformAdapter"
    return False


def _channel_key(gw: Any, cfg: Dict[str, Any], type_name: str) -> str:
    if cfg.get("name"):
        return str(cfg["name"])
    agent_dir = cfg.get("_agent_dir")
    if agent_dir:
        mine = {k: v for k, v in cfg.items() if k != "_agent_dir"}
        hits = [k for k, c in (gw.load_config(Path(agent_dir)).get("channels") or {}).items()
                if isinstance(c, dict) and {kk: vv for kk, vv in c.items() if kk != "_agent_dir"} == mine]
        if len(hits) == 1:
            return str(hits[0])
    return type_name.split(":", 1)[-1]


class HermesPlatformChannel:
    """An Omni gateway channel (`poll`, `send`, `typing`, `close`) driving one Hermes adapter."""

    def __init__(self, gw: Any, entry: Dict[str, Any], cfg: Dict[str, Any]):
        self.gw, self.entry = gw, entry
        self.type_name = f"hermes:{entry['name']}"
        self.name = _channel_key(gw, cfg, self.type_name)
        self.inbox: "queue.Queue[Any]" = queue.Queue()
        self.adapter: Any = None
        self.fatal: Optional[str] = None
        self._fatal_reported = False
        self._closed = False
        self._loop: Optional[_Loop] = None
        self.send_timeout = float(cfg.get("send_timeout") or 60)
        _shim_acquire()          # adapters import lazily too, so the shim stays while the channel lives
        self._shim = True
        try:
            missing = missing_packages(entry)
            if missing:
                raise RuntimeError(f"{self.type_name} needs Python package(s) that are not installed: {', '.join(missing)}. "
                                   f"Run: pip install {' '.join(missing)}")
            pcfg = platform_config(entry, cfg, gw.resolve_secret)
            validate = entry.get("validate_config")
            if callable(validate) and not validate(pcfg):
                need = ", ".join(entry.get("required_env") or []) or "its settings"
                raise ValueError(f"{self.type_name}: configuration is incomplete (needs {need}, from the environment or "
                                 "the matching keys in gateway.json)")
            self._loop = _Loop(self.name)

            async def build() -> Any:
                return entry["adapter_factory"](pcfg)
            self.adapter = self._loop.run(build(), 30)
            self.adapter.set_message_handler(self._on_event)
            with contextlib.suppress(Exception):
                self.adapter.set_fatal_error_handler(self._on_fatal)
            ok = self._loop.run(self.adapter.connect(), float(cfg.get("connect_timeout") or 60))
            if not ok:
                why = getattr(self.adapter, "_fatal_error_message", None) or "connect() returned False (see the log)"
                raise RuntimeError(f"{self.type_name}: could not connect: {why}")
        except BaseException as e:
            self.close()
            if isinstance(e, ImportError):
                raise RuntimeError(f"{self.type_name}: {explain_import_error(e)}") from e
            if isinstance(e, (RuntimeError, ValueError)) and str(e).startswith(self.type_name):
                raise
            raise RuntimeError(f"{self.type_name}: {type(e).__name__}: {e}") from e

    # --- adapter -> Omni (runs on the adapter's loop)
    async def _on_event(self, event: Any) -> None:
        src = getattr(event, "source", None)
        chat = str(getattr(src, "chat_id", "") or "")
        if not chat:
            return
        text = str(getattr(event, "text", "") or "")
        types_ = list(getattr(event, "media_types", None) or [])
        for i, url in enumerate(getattr(event, "media_urls", None) or []):
            kind = str(types_[i] if i < len(types_) else "attachment").split("/")[0] or "attachment"
            text += f"\n[{kind}: {url}]"
        if not text.strip():
            return
        sender = str(getattr(src, "user_id", None) or chat)
        self.inbox.put(self.gw.Inbound(self.name, chat, sender, text.strip(), name=str(getattr(src, "user_name", None) or "")))
        return None

    def _on_fatal(self, adapter: Any) -> None:
        self.fatal = str(getattr(adapter, "_fatal_error_message", None) or getattr(adapter, "_fatal_error_code", None) or "stopped")
        self._fatal_reported = False

    # --- Omni's channel interface
    def poll(self, timeout: float) -> List[Any]:
        if self.fatal and not self._fatal_reported:
            self._fatal_reported = True
            raise RuntimeError(f"{self.type_name} stopped: {self.fatal}")
        try:
            first = self.inbox.get(timeout=max(0.0, timeout))
        except queue.Empty:
            return []
        out = [] if first is _CLOSED else [first]
        while True:
            try:
                item = self.inbox.get_nowait()
            except queue.Empty:
                break
            if item is not _CLOSED:
                out.append(item)
        return out

    def send(self, chat: str, text: str) -> None:
        if self._closed or self.adapter is None or self._loop is None:
            raise RuntimeError(f"{self.name}: channel is closed")
        a = self.adapter
        with contextlib.suppress(Exception):
            text = a.format_message(text)
        limit = int(self.entry.get("max_message_length") or getattr(a, "MAX_MESSAGE_LENGTH", 0) or 0)
        parts = [text or "(empty)"]
        if limit and len(text) > limit and not getattr(a, "splits_long_messages", False):
            parts = a.truncate_message(text, limit)
        for part in parts:
            res = self._loop.run(a.send(chat_id=chat, content=part), self.send_timeout)
            if res is not None and getattr(res, "success", True) is False:
                raise RuntimeError(f"{self.type_name}: {getattr(res, 'error', None) or 'send failed'}")

    def typing(self, chat: str) -> None:
        if self._closed or self.adapter is None or self._loop is None or not _defines_own(self.adapter, "send_typing"):
            return
        with contextlib.suppress(Exception):
            self._loop.run(self.adapter.send_typing(chat), 10)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self.inbox.put(_CLOSED)
        try:
            if self.adapter is not None and self._loop is not None:
                with contextlib.suppress(Exception):
                    self._loop.run(self.adapter.disconnect(), 15)
            if self._loop is not None:
                self._loop.stop()
        finally:
            if getattr(self, "_shim", False):
                self._shim = False
                _shim_release()


# ============================================================================================ gateway hook
def _failing(message: str) -> Callable[[Dict[str, Any]], Any]:
    def factory(cfg: Dict[str, Any]) -> Any:
        raise RuntimeError(message)
    return factory


def install(gw: Any, agent_dir: Any, config: Optional[Dict[str, Any]] = None) -> List[str]:
    """Register `hermes:<name>` channel types for the platform plugins found. Only runs when gateway.json uses
    such a type. Problems with a plugin surface through its channel (the factory raises), so they land in the
    gateway's errors only for platforms that are actually configured. Returns problems not tied to a channel."""
    config = config if config is not None else gw.load_config(Path(agent_dir))
    wanted = {str(c.get("type"))[len("hermes:"):].lower() for c in (config.get("channels") or {}).values()
              if isinstance(c, dict) and str(c.get("type") or "").startswith("hermes:")}
    if not wanted:
        return []
    problems: List[str] = []
    failed: Dict[str, str] = {}
    dirs = discover(agent_dir, config)
    for d in dirs:
        try:
            res = load_plugin(d)
        except Exception as e:  # noqa: BLE001
            problems.append(f"hermes platform plugin {d.name}: {type(e).__name__}: {e}")
            continue
        if res["error"]:
            for n in res["names"]:
                failed.setdefault(n, f"hermes:{n} (plugin {d.name}) could not be loaded: {res['error']}")
            continue
        for entry in res["platforms"]:
            REGISTERED[entry["name"]] = entry
            gw.register_channel(f"hermes:{entry['name']}", lambda cfg, e=entry: HermesPlatformChannel(gw, e, cfg))
    for n in wanted:
        if n in REGISTERED and f"hermes:{n}" in gw.CHANNEL_TYPES:
            continue
        if n in failed:
            gw.register_channel(f"hermes:{n}", _failing(failed[n]))
        else:
            where = ", ".join(str(r) for r in search_roots(agent_dir, config)) or "(no folders)"
            gw.register_channel(f"hermes:{n}", _failing(f"no Hermes platform plugin named '{n}' was found (looked in {where})"))
    return problems
