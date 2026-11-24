"""Hermes platform plugins (chat-platform adapters) running unchanged as gateway channels `hermes:<name>`. Offline."""
import json
import os
import shutil
import sys
import threading
import time
import types
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from test_capabilities import CapCase  # noqa: E402
from src.pi import gateway as gw  # noqa: E402
from src.pi import hermes_platforms as hp  # noqa: E402
from src.pi.ai import Faux  # noqa: E402

HERMES_NTFY = Path(r"D:\Harness\references\hermes-agent\plugins\platforms\ntfy")

# A stdlib-only platform: inbound arrives on an asyncio.Queue the test feeds, outbound is recorded.
# Its imports are the ntfy adapter's, line for line.
FAKE_ADAPTER = '''
import asyncio
import json
import logging
import time
import uuid
from datetime import datetime, timezone, UTC
from typing import Any, Dict, List, Optional

from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import BasePlatformAdapter, SendResult
from gateway.platforms.event import MessageEvent, MessageType
from gateway.platforms._shared import get_scoped_secret as _get_scoped_secret, send_error
from gateway.platforms.helpers import MessageDeduplicator
from gateway.platforms._shared import (
    extra_or_secret as _extra_or_secret, seed_extra_from_env as _seed_extra_from_env
)
from gateway.platforms.send_targets import parse_nonempty

INSTANCES = []


def check_requirements() -> bool:
    return True


def validate_config(config) -> bool:
    return bool(_extra_or_secret(getattr(config, "extra", {}) or {}, "room", "FAKECHAT_ROOM"))


class FakeChatAdapter(BasePlatformAdapter):
    MAX_MESSAGE_LENGTH = 50

    def __init__(self, config: PlatformConfig):
        super().__init__(config=config, platform=Platform("fakechat"))
        self.room = _extra_or_secret(config.extra or {}, "room", "FAKECHAT_ROOM")
        self.token = config.token
        self.inbox = None
        self.sent, self.typed = [], []
        self.disconnected = False
        self._task = None
        self._dedup = MessageDeduplicator(max_size=10, ttl_seconds=60)
        INSTANCES.append(self)

    async def connect(self, *, is_reconnect: bool = False) -> bool:
        if self.token == "bad":
            self._set_fatal_error("auth", "token rejected", retryable=False)
            return False
        self.inbox = asyncio.Queue()
        self.loop = asyncio.get_running_loop()
        self._task = asyncio.create_task(self._reader())
        self._mark_connected()
        self._wire_plugin_handlers(None)
        return True

    async def _reader(self):
        while self._running:
            ev = await self.inbox.get()
            if ev is None:
                self._set_fatal_error("gone", "server went away", retryable=True)
                await self._notify_fatal_error()
                return
            if self._dedup.is_duplicate(ev["id"]):
                continue
            source = self.build_source(chat_id=ev.get("room", self.room), chat_type="group", user_id=ev["user"],
                                       user_name=ev.get("nick"), message_id=ev["id"])
            await self.handle_message(MessageEvent(text=ev["text"], message_type=MessageType.TEXT, source=source,
                                                   message_id=ev["id"], media_urls=ev.get("media", []),
                                                   media_types=ev.get("media_types", []), timestamp=datetime.now(tz=UTC)))

    def push(self, **ev):
        self.loop.call_soon_threadsafe(self.inbox.put_nowait, ev)

    async def disconnect(self) -> None:
        self._mark_disconnected()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self.disconnected = True

    async def send(self, chat_id: str, content: str, reply_to: Optional[str] = None,
                   metadata: Optional[dict[str, Any]] = None) -> SendResult:
        if content == "explode":
            return SendResult(success=False, error="refused by server")
        self.sent.append((chat_id, content))
        return SendResult(success=True, message_id=uuid.uuid4().hex[:8])

    async def send_typing(self, chat_id: str, metadata=None) -> None:
        self.typed.append(chat_id)

    async def get_chat_info(self, chat_id: str) -> dict[str, Any]:
        return {"name": chat_id, "type": "group"}


async def _standalone_send(pconfig, chat_id, message, **kw):
    return send_error("not here token=abc")


def register(ctx) -> None:
    ctx.register_platform(
        target_ref_syntax_fn=parse_nonempty,
        name="fakechat", label="Fake Chat", adapter_factory=lambda cfg: FakeChatAdapter(cfg),
        check_fn=check_requirements, validate_config=validate_config, required_env=["FAKECHAT_ROOM"],
        install_hint="No extra packages needed", standalone_sender_fn=_standalone_send, max_message_length=50,
        emoji="x", platform_hint="plain text")
    ctx.register_tool("ignored", "x", {}, lambda a: "")
'''


def write_plugin(root: Path, folder: str, adapter: str, yaml: str = "", init: str = "from .adapter import register\n"):
    d = root / folder
    d.mkdir(parents=True)
    (d / "plugin.yaml").write_text(yaml or f"name: {folder}-platform\nlabel: {folder}\nkind: platform\nversion: 1.0.0\n",
                                   encoding="utf-8")
    (d / "__init__.py").write_text(init, encoding="utf-8")
    (d / "adapter.py").write_text(adapter, encoding="utf-8")
    return d


def hermes_threads():
    return [t for t in threading.enumerate() if t.name.startswith("omni-hermes-") and t.is_alive()]


class PlatformCase(CapCase):
    def setUp(self):
        super().setUp()
        self.plugins = self.dir / "hplatforms"
        self.plugins.mkdir()
        self._before = set(gw.CHANNEL_TYPES)
        self._threads_before = set(threading.enumerate())

    def tearDown(self):
        for k in list(gw.CHANNEL_TYPES):
            if k not in self._before:
                gw.CHANNEL_TYPES.pop(k, None)
        hp.REGISTERED.clear()
        super().tearDown()

    def gateway(self, **channels):
        gw.save_config(self.agent, {"profile": "tcoder", "model": "faux/faux-1", "cwd": str(self.dir),
                                    "hermes_platform_dirs": [str(self.plugins)], "hermes_default_dirs": False,
                                    "channels": channels})
        return gw.Gateway(self.agent)

    def assert_clean(self):
        deadline = time.time() + 5
        while hermes_threads() and time.time() < deadline:
            time.sleep(0.05)
        self.assertEqual(hermes_threads(), [])
        self.assertNotIn("gateway", sys.modules)
        self.assertNotIn("agent.i18n", sys.modules)


class TestDiscovery(PlatformCase):
    def test_discovers_platform_plugins_and_captures_register_platform(self):
        write_plugin(self.plugins, "fakechat", FAKE_ADAPTER)
        other = self.plugins / "memory-thing"           # an ordinary Hermes plugin: not a platform
        other.mkdir()
        (other / "plugin.yaml").write_text("name: memory-thing\n", encoding="utf-8")
        (other / "__init__.py").write_text("def register(ctx):\n    ctx.register_hook('x', print)\n", encoding="utf-8")
        cfg = {"hermes_platform_dirs": [str(self.plugins)], "hermes_default_dirs": False}
        found = hp.discover(self.agent, cfg)
        self.assertEqual([d.name for d in found], ["fakechat"])
        res = hp.load_plugin(found[0])
        self.assertIsNone(res["error"], res)
        entry = res["platforms"][0]
        self.assertEqual((entry["name"], entry["label"], entry["max_message_length"], entry["required_env"]),
                         ("fakechat", "Fake Chat", 50, ["FAKECHAT_ROOM"]))
        self.assertTrue(callable(entry["adapter_factory"]) and callable(entry["validate_config"]))
        self.assertEqual(entry["target_ref_syntax_fn"](" room1 "), ("room1", None))
        self.assertEqual(res["ignored"], ["tool"])
        self.assertIn("fakechat", res["names"])
        self.assertNotIn("gateway", sys.modules)                # the shim is gone once loading is done

    def test_no_hermes_channel_means_no_loading(self):
        write_plugin(self.plugins, "fakechat", FAKE_ADAPTER)
        g = self.gateway(echo={"type": "webhook", "url": "http://127.0.0.1:1/x"})
        self.assertNotIn("hermes:fakechat", gw.CHANNEL_TYPES)
        self.assertEqual(g.errors, [])


class TestChannel(PlatformCase):
    def test_message_round_trip_typing_and_close(self):
        write_plugin(self.plugins, "fakechat", FAKE_ADAPTER)
        os.environ["OMNI_TEST_FAKE_TOKEN"] = "sekrit"
        try:
            g = self.gateway(chat={"type": "hermes:fakechat", "room": "lobby", "token": "$OMNI_TEST_FAKE_TOKEN",
                                   "allow": ["u1"]})
        finally:
            os.environ.pop("OMNI_TEST_FAKE_TOKEN", None)
        self.assertEqual(g.errors, [])
        ch = g.channels["chat"]
        self.assertEqual(ch.name, "chat")
        a = ch.adapter
        self.assertEqual((a.room, a.token, a.config.extra.get("room")), ("lobby", "sekrit", "lobby"))
        self.assertTrue(a.is_connected)
        self.assertIn("gateway", sys.modules)                   # kept while the adapter lives (lazy imports)

        a.push(id="m1", user="u1", nick="ann", text="what is 2+2?")
        a.push(id="m1", user="u1", nick="ann", text="what is 2+2?")    # duplicate: the adapter drops it
        a.push(id="m2", user="u1", text="look", media=["/tmp/x.png"], media_types=["image/png"])
        got = []
        deadline = time.time() + 5
        while len(got) < 2 and time.time() < deadline:
            got += ch.poll(0.5)
        self.assertEqual([(m.channel, m.chat, m.sender, m.name, m.text) for m in got],
                         [("chat", "lobby", "u1", "ann", "what is 2+2?"), ("chat", "lobby", "u1", "", "look\n[image: /tmp/x.png]")])
        self.assertEqual(ch.poll(0.05), [])

        Faux.queue = [{"text": "4"}]
        g._dispatch(ch, got[0])                                 # typing, Gateway.handle (Faux), then send
        self.assertEqual(a.sent, [("lobby", "4")])
        self.assertIn("lobby", a.typed)

        ch.send("lobby", "word " * 30)                          # 150 chars, platform limit 50: split
        parts = [t for _c, t in a.sent[1:]]
        self.assertGreater(len(parts), 2)
        self.assertTrue(all(len(p) <= 50 for p in parts), parts)
        self.assertTrue(parts[0].endswith(f"(1/{len(parts)})"))
        with self.assertRaises(RuntimeError) as cm:
            ch.send("lobby", "explode")
        self.assertIn("refused by server", str(cm.exception))

        g.stop()
        self.assertTrue(a.disconnected)
        ch.close()                                              # idempotent
        self.assertEqual(ch.poll(0.01), [])
        self.assert_clean()

    def test_stranger_gets_pairing_and_fatal_error_is_reported_once(self):
        write_plugin(self.plugins, "fakechat", FAKE_ADAPTER)
        g = self.gateway(fakechat={"type": "hermes:fakechat", "room": "lobby"})
        ch = g.channels["fakechat"]
        try:
            ch.adapter.push(id="m1", user="stranger", text="hi")
            msgs = []
            deadline = time.time() + 5
            while not msgs and time.time() < deadline:
                msgs = ch.poll(0.5)
            self.assertIn("omni gateway approve", g.handle(msgs[0]))
            ch.adapter.loop.call_soon_threadsafe(ch.adapter.inbox.put_nowait, None)
            deadline = time.time() + 5
            while not ch.fatal and time.time() < deadline:
                time.sleep(0.02)
            with self.assertRaises(RuntimeError) as cm:
                ch.poll(0.01)
            self.assertIn("server went away", str(cm.exception))
            self.assertEqual(ch.poll(0.01), [])
        finally:
            g.stop()
        self.assert_clean()

    def test_config_and_connect_failures_land_in_errors(self):
        write_plugin(self.plugins, "fakechat", FAKE_ADAPTER)
        g = self.gateway(a={"type": "hermes:fakechat"}, b={"type": "hermes:fakechat", "room": "r", "token": "bad"},
                         c={"type": "hermes:nosuch"})
        self.assertEqual(g.channels, {})
        errs = "\n".join(g.errors)
        self.assertIn("channel 'a': hermes:fakechat: configuration is incomplete (needs FAKECHAT_ROOM", errs)
        self.assertIn("channel 'b': hermes:fakechat: could not connect: token rejected", errs)
        self.assertIn("channel 'c': no Hermes platform plugin named 'nosuch' was found", errs)
        self.assert_clean()


class TestLoadProblems(PlatformCase):
    def test_missing_dependency_names_the_package(self):
        write_plugin(self.plugins, "needsdep", "import omni_surely_missing_pkg\n" + FAKE_ADAPTER.replace('"fakechat"', '"needsdep"'))
        write_plugin(self.plugins, "flagdep", FAKE_ADAPTER.replace('"fakechat"', '"flagdep"').replace(
            "def check_requirements() -> bool:\n    return True",
            "try:\n    import omni_other_missing\n    LIB_AVAILABLE = True\nexcept ImportError:\n    LIB_AVAILABLE = False\n\n"
            "def check_requirements() -> bool:\n    return LIB_AVAILABLE").replace(
            'install_hint="No extra packages needed"', 'install_hint="pip install omni-other-missing   # comment"'))
        g = self.gateway(n={"type": "hermes:needsdep", "room": "r"}, f={"type": "hermes:flagdep", "room": "r"})
        self.assertEqual(g.channels, {})
        errs = "\n".join(g.errors)
        self.assertIn("channel 'n': hermes:needsdep (plugin needsdep) could not be loaded: needs the Python package "
                      "'omni-surely-missing-pkg', which is not installed. Run: pip install omni-surely-missing-pkg", errs)
        self.assertIn("channel 'f': hermes:flagdep needs Python package(s) that are not installed: omni-other-missing. "
                      "Run: pip install omni-other-missing", errs)
        self.assert_clean()

    def test_unknown_gateway_name_and_hermes_internals_are_reported(self):
        write_plugin(self.plugins, "toonew", FAKE_ADAPTER.replace(
            "from gateway.platforms.base import BasePlatformAdapter, SendResult",
            "from gateway.platforms.base import BasePlatformAdapter, SendResult, ExecApprovalPrompt"))
        write_plugin(self.plugins, "deep", "from gateway.status import acquire_scoped_lock\n" + FAKE_ADAPTER)
        write_plugin(self.plugins, "core", "from hermes_cli.setup import prompt\n" + FAKE_ADAPTER)
        g = self.gateway(x={"type": "hermes:toonew"}, y={"type": "hermes:deep"}, z={"type": "hermes:core"})
        errs = "\n".join(g.errors)
        self.assertIn("uses gateway.platforms.base.ExecApprovalPrompt, which Omni's Hermes compatibility layer does not provide", errs)
        self.assertIn("uses gateway.status, which Omni's Hermes compatibility layer does not provide", errs)
        self.assertIn("imports hermes_cli from Hermes itself, which is not installed", errs)
        self.assert_clean()

    def test_a_real_gateway_package_is_not_shadowed(self):
        write_plugin(self.plugins, "fakechat", FAKE_ADAPTER)
        fake = types.ModuleType("gateway")
        fake.__file__ = str(self.dir / "gateway" / "__init__.py")
        sys.modules["gateway"] = fake
        try:
            self.assertTrue(hp.real_gateway_present())
            g = self.gateway(chat={"type": "hermes:fakechat", "room": "r"})
            self.assertIs(sys.modules["gateway"], fake)
            self.assertNotIn("gateway.config", sys.modules)
            self.assertEqual(g.channels, {})
            self.assertIn("could not import gateway.config from the installed `gateway` package", "\n".join(g.errors))
        finally:
            sys.modules.pop("gateway", None)
        self.assertFalse(hp.real_gateway_present())


class TestRealNtfyAdapter(PlatformCase):
    def test_bundled_ntfy_imports_and_registers_through_the_shim(self):
        if not (HERMES_NTFY / "adapter.py").is_file():
            self.skipTest(f"Hermes reference checkout not found at {HERMES_NTFY}")
        shutil.copytree(HERMES_NTFY, self.plugins / "ntfy")
        res = hp.load_plugin(self.plugins / "ntfy")
        self.assertIsNone(res["error"], res)
        entry = res["platforms"][0]
        self.assertEqual((entry["name"], entry["max_message_length"], entry["required_env"]), ("ntfy", 4096, ["NTFY_TOPIC"]))
        g = self.gateway(ntfy={"type": "hermes:ntfy", "topic": "omni-test", "server": "http://127.0.0.1:1"})
        try:
            if hp._spec_exists("httpx"):
                self.assertEqual(g.errors, [])
                self.assertIn("ntfy", g.channels)
            else:  # ntfy guards its httpx import, so the gap shows up as a missing package, with the fix
                self.assertEqual(g.channels, {})
                self.assertIn("hermes:ntfy needs Python package(s) that are not installed: httpx. Run: pip install httpx",
                              "\n".join(g.errors))
        finally:
            g.stop()
        self.assert_clean()


if __name__ == "__main__":
    unittest.main()
