"""Slack and Discord gateway channels (polling), channel plugins, and Hermes image-generation providers. Offline."""
import http.server
import json
import sys
import threading
import unittest
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from test_capabilities import CapCase  # noqa: E402
from src.pi import gateway as gw  # noqa: E402
from src.pi.ai import Faux  # noqa: E402
from src.pi.caps import hermes_plugins, image_gen  # noqa: E402

PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 64


class FakeChat:
    """Answers the few Slack and Discord endpoints the channels use."""

    def __init__(self):
        self.slack, self.discord, self.sent, self.auth = [], [], [], []
        outer = self

        class H(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def reply(self, data):
                body = json.dumps(data).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                u = urlparse(self.path)
                q = {k: v[0] for k, v in parse_qs(u.query).items()}
                outer.auth.append(self.headers.get("Authorization"))
                if u.path == "/slack/auth.test":
                    self.reply({"ok": True, "user_id": "UBOT"})
                elif u.path == "/slack/conversations.history":
                    self.reply({"ok": True, "messages": [m for m in outer.slack if float(m["ts"]) > float(q["oldest"])][::-1]})
                elif u.path == "/discord/users/@me":
                    self.reply({"id": "999"})
                elif u.path.startswith("/discord/channels/"):
                    rows = [m for m in outer.discord if int(m["id"]) > int(q.get("after", 0))]
                    self.reply(rows[-1:] if "after" not in q else rows[::-1])
                else:
                    self.reply({"ok": False, "error": "unknown_method"})

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
                if self.path == "/slack/chat.postMessage":
                    outer.sent.append(("slack", body["channel"], body["text"]))
                    self.reply({"ok": True})
                elif self.path.endswith("/messages"):
                    outer.sent.append(("discord", self.path.split("/")[3], body["content"]))
                    self.reply({"id": "1"})
                else:
                    self.reply({})
        self.srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.srv.server_address[1]}"

    def close(self):
        self.srv.shutdown()
        self.srv.server_close()


class TestChannels(CapCase):
    def gateway(self, **channels):
        gw.save_config(self.agent, {"profile": "tcoder", "model": "faux/faux-1", "cwd": str(self.dir), "channels": channels})
        return gw.Gateway(self.agent)

    def test_slack_reads_new_messages_and_answers(self):
        fake = FakeChat()
        try:
            g = self.gateway(slack={"type": "slack", "token": "xoxb-1", "api_base": fake.url + "/slack", "channels": ["C1"],
                                    "interval": 0.05, "allow": ["U1"]})
            self.assertEqual(g.errors, [])
            ch = g.channels["slack"]
            ch.started = "100.000000"
            fake.slack += [{"ts": "50.0", "user": "U1", "text": "old, before start"},
                           {"ts": "101.0", "user": "U1", "text": "<@UBOT> what is 2+2?"},
                           {"ts": "102.0", "user": "UBOT", "text": "my own message"},
                           {"ts": "103.0", "bot_id": "B1", "text": "another bot"},
                           {"ts": "104.0", "user": "U1", "subtype": "channel_join", "text": "joined"}]
            got = ch.poll(0.05)
            self.assertEqual([(m.chat, m.sender, m.text) for m in got], [("C1", "U1", "what is 2+2?")])
            self.assertEqual(ch.poll(0.05), [])                         # nothing is delivered twice
            self.assertEqual(ch.cursor["C1"], "104.0")
            Faux.queue = [{"text": "4"}]
            ch.send("C1", g.handle(got[0]))
            self.assertEqual(fake.sent, [("slack", "C1", "4")])
            self.assertIn("Bearer xoxb-1", fake.auth)
            ch.mention_only = True
            fake.slack.append({"ts": "105.0", "user": "U1", "text": "not for the bot"})
            self.assertEqual(ch.poll(0.05), [])
        finally:
            fake.close()

    def test_discord_reads_new_messages_and_answers(self):
        fake = FakeChat()
        try:
            fake.discord.append({"id": "10", "content": "history", "author": {"id": "5", "username": "ann"}})
            g = self.gateway(discord={"type": "discord", "token": "tok", "api_base": fake.url + "/discord", "channels": ["77"], "interval": 0.05})
            ch = g.channels["discord"]
            self.assertEqual(ch.poll(0.05), [])                         # the first look only sets the cursor
            fake.discord += [{"id": "11", "content": "<@999> hello there", "author": {"id": "5", "username": "ann"}, "mentions": [{"id": "999"}]},
                             {"id": "12", "content": "from a bot", "author": {"id": "6", "bot": True}},
                             {"id": "13", "content": "mine", "author": {"id": "999"}}]
            got = ch.poll(0.05)
            self.assertEqual([(m.channel, m.chat, m.sender, m.name, m.text) for m in got], [("discord", "77", "5", "ann", "hello there")])
            self.assertEqual(ch.cursor["77"], "13")
            ch.send("77", "x" * 2000)
            self.assertEqual([len(t) for _c, _id, t in fake.sent], [1900, 100])
            self.assertIn("Bot tok", fake.auth)
        finally:
            fake.close()

    def test_bad_config_and_unreachable_service(self):
        g = self.gateway(slack={"type": "slack", "token": "x"}, discord={"type": "discord", "channels": ["1"]})
        self.assertEqual(len(g.errors), 2)
        self.assertIn("list the channel ids", g.errors[0])
        self.assertIn("token is missing", g.errors[1])
        g = self.gateway(slack={"type": "slack", "token": "x", "channels": ["C1"], "api_base": "http://127.0.0.1:1", "interval": 0.05})
        self.assertEqual(g.channels["slack"].poll(0.05), [])
        self.assertIn("C1:", g.channels["slack"].error)

    def test_channel_plugins(self):
        folder = self.agent / "channels"
        folder.mkdir()
        (folder / "echo.py").write_text(
            "class Echo(Channel):\n    name = 'echo'\n    def __init__(self, cfg):\n        self.box = [Inbound('echo', 'c', 'u', cfg['say'])]\n"
            "    def poll(self, timeout):\n        out, self.box = self.box, []\n        return out\n    def send(self, chat, text):\n        pass\n"
            "CHANNELS = {'echo': Echo}\n", encoding="utf-8")
        (folder / "direct.py").write_text("register_channel('direct', lambda cfg: Channel())\n", encoding="utf-8")
        (folder / "broken.py").write_text("raise ValueError('nope')\n", encoding="utf-8")
        try:
            g = self.gateway(echo={"type": "echo", "say": "hi"}, direct={"type": "direct"})
            self.assertEqual([m.text for m in g.channels["echo"].poll(0)], ["hi"])
            self.assertIn("direct", g.channels)
            self.assertEqual(g.errors, ["channel plugin broken.py: ValueError: nope"])
        finally:
            gw.CHANNEL_TYPES.pop("echo", None)
            gw.CHANNEL_TYPES.pop("direct", None)


PLUGIN = '''
class Painter:
    name = "painter"
    def is_available(self):
        return True
    def generate(self, prompt, aspect_ratio="square", **kw):
        if "fail" in prompt:
            return {"success": False, "error": "quota exceeded"}
        from pathlib import Path
        out = Path(__file__).parent / "made.png"
        out.write_bytes(%r)
        return {"success": True, "image": str(out), "aspect_ratio": aspect_ratio}

def register(ctx):
    ctx.register_image_gen_provider(Painter())
''' % PNG


class TestHermesImageProvider(CapCase):
    def test_plugin_provider_feeds_image_generate(self):
        plug = self.dir / "hplugins" / "painter-plugin"
        plug.mkdir(parents=True)
        (plug / "plugin.yaml").write_text("name: painter-plugin\nversion: 1.0.0\n", encoding="utf-8")
        (plug / "__init__.py").write_text(PLUGIN, encoding="utf-8")
        (self.profiles / "hpi.json").write_text(json.dumps({
            "name": "hpi", "extends": "lean", "capabilities": ["hermes-plugins", "image-gen"], "image_gen": {"provider": "painter"},
            "hermes_plugin_dirs": [str(self.dir / "hplugins")], "hermes_default_dirs": False}))
        try:
            s = self.session(profile="hpi")
            rec = next(p for p in hermes_plugins.host_of(s).plugins if p["name"] == "painter-plugin")
            self.assertTrue(rec["ok"], rec)
            self.assertEqual(rec["image_providers"], ["painter"])
            self.assertNotIn("image_gen_provider", rec["unsupported"])
            r = self.call(s, "image_generate", {"prompt": "a red fox", "size": "1536x1024"})
            self.assertFalse(r.get("isError"), self.text(r))
            self.assertEqual(r["details"]["provider"], "painter")
            self.assertEqual((Path(s.cwd) / r["details"]["path"]).read_bytes(), PNG)
            bad = self.call(s, "image_generate", {"prompt": "fail please"})
            self.assertTrue(bad.get("isError"))
            self.assertIn("quota exceeded", self.text(bad))
        finally:
            image_gen.unregister_provider("painter")


if __name__ == "__main__":
    unittest.main()
