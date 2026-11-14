"""Image generation: the provider registry, the OpenAI and Gemini providers against local fakes, saving rules. Offline."""
import base64
import http.server
import json
import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from test_capabilities import CapCase  # noqa: E402
from src.pi import caps  # noqa: E402
from src.pi.caps import image_gen  # noqa: E402

# not in the built-in module list yet: register it here (harmless once it is, the objects are the same)
[caps.register(c) for c in image_gen.CAPABILITIES]

PNG = b"\x89PNG\r\n\x1a\n" + b"fake-png-body" * 10
JPG = b"\xff\xd8\xff\xe0" + b"fake-jpg-body" * 10
ENV = ("OPENAI_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY", "OMNI_OPENAI_IMAGE_BASE", "OMNI_GEMINI_IMAGE_BASE")


class Round15(CapCase):
    def setUp(self):
        super().setUp()
        self._saved = {k: os.environ.pop(k, None) for k in ENV}     # no real keys, no real endpoints
        self._providers = dict(image_gen.PROVIDERS)
        self.seen = []
        seen = self.seen

        class H(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def reply(self, code, body, ctype="application/json"):
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                seen.append({"path": self.path, "method": "GET"})
                if self.path == "/files/out.jpg":
                    return self.reply(200, JPG, "image/jpeg")
                self.reply(404, b"{}")

            def do_POST(self):
                q = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                seen.append({"path": self.path, "body": q, "auth": self.headers.get("Authorization"),
                             "goog": self.headers.get("x-goog-api-key")})
                host = f"http://127.0.0.1:{self.server.server_address[1]}"
                if self.path == "/v1/images/generations":
                    if "forbidden" in q["prompt"]:
                        return self.reply(400, json.dumps({"error": {"message": "prompt rejected by policy"}}).encode())
                    item = {"url": f"{host}/files/out.jpg"} if "by url" in q["prompt"] else {"b64_json": base64.b64encode(PNG).decode()}
                    return self.reply(200, json.dumps({"created": 1, "data": [item]}).encode())
                if self.path.startswith("/v1beta/models/") and self.path.endswith(":generateContent"):
                    parts = [{"text": "Here is your image."}]
                    if "nothing" not in q["contents"][0]["parts"][0]["text"]:
                        parts.append({"inlineData": {"mimeType": "image/png", "data": base64.b64encode(PNG).decode()}})
                    return self.reply(200, json.dumps({"candidates": [{"content": {"role": "model", "parts": parts}}]}).encode())
                self.reply(404, b"{}")

        self.srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.srv.server_address[1]}"

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()
        image_gen.PROVIDERS.clear()
        image_gen.PROVIDERS.update(self._providers)
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        super().tearDown()

    def profile(self, **extra):
        (self.profiles / "r15i.json").write_text(json.dumps({"name": "r15i", "extends": "lean", "capabilities": ["image-gen"], **extra}))
        return self.session(profile="r15i")


class TestBuiltinProviders(Round15):
    def test_openai_b64_and_url(self):
        os.environ["OPENAI_API_KEY"] = "sk-test-fake"
        os.environ["OMNI_OPENAI_IMAGE_BASE"] = self.base
        s = self.profile()
        self.assertIn("image_generate", s.all_tools())
        r = self.call(s, "image_generate", {"prompt": "A red fox, in the snow!"})
        self.assertFalse(r.get("isError"), self.text(r))
        self.assertEqual(r["details"]["path"], "images/a-red-fox-in-the-snow-1.png")
        self.assertEqual(r["details"]["provider"], "openai")
        self.assertIn("images/a-red-fox-in-the-snow-1.png", self.text(r))
        self.assertNotIn(base64.b64encode(PNG).decode()[:20], json.dumps(r))        # the data stays out of the conversation
        self.assertEqual((Path(s.cwd) / "images" / "a-red-fox-in-the-snow-1.png").read_bytes(), PNG)
        req = self.seen[0]
        self.assertEqual(req["auth"], "Bearer sk-test-fake")
        self.assertEqual(req["body"], {"model": "gpt-image-1", "prompt": "A red fox, in the snow!", "n": 1, "size": "1024x1024"})

        r = self.call(s, "image_generate", {"prompt": "A red fox, in the snow!", "size": "1536x1024"})   # same slug: next number
        self.assertEqual(r["details"]["path"], "images/a-red-fox-in-the-snow-2.png")
        self.assertEqual(self.seen[-1]["body"]["size"], "1536x1024")

        r = self.call(s, "image_generate", {"prompt": "a harbour by url", "path": "art/harbour"})         # url answer, downloaded
        self.assertFalse(r.get("isError"), self.text(r))
        self.assertEqual(r["details"]["path"], "art/harbour.jpg")                    # extension follows the actual data
        self.assertEqual((Path(s.cwd) / "art" / "harbour.jpg").read_bytes(), JPG)
        self.assertEqual(self.seen[-1], {"path": "/files/out.jpg", "method": "GET"})

        r = self.call(s, "image_generate", {"prompt": "something forbidden"})
        self.assertTrue(r.get("isError"))
        self.assertIn("HTTP 400", self.text(r))
        self.assertIn("prompt rejected by policy", self.text(r))

    def test_openai_model_from_profile(self):
        os.environ["OPENAI_API_KEY"] = "sk-test-fake"
        os.environ["OMNI_OPENAI_IMAGE_BASE"] = self.base + "/"
        s = self.profile(image_gen={"provider": "openai", "model": "dall-e-3"})
        self.assertFalse(self.call(s, "image_generate", {"prompt": "x"}).get("isError"))
        self.assertEqual(self.seen[0]["body"]["model"], "dall-e-3")

    def test_gemini(self):
        os.environ["GOOGLE_API_KEY"] = "g-test-fake"
        os.environ["OMNI_GEMINI_IMAGE_BASE"] = self.base
        s = self.profile()
        r = self.call(s, "image_generate", {"prompt": "a teapot", "size": "1024x1536", "path": "out/teapot.png"})
        self.assertFalse(r.get("isError"), self.text(r))
        self.assertEqual(r["details"], {"path": "out/teapot.png", "provider": "gemini", "size": "1024x1536", "bytes": len(PNG)})
        self.assertEqual((Path(s.cwd) / "out" / "teapot.png").read_bytes(), PNG)
        req = self.seen[0]
        self.assertEqual(req["path"], "/v1beta/models/gemini-2.5-flash-image:generateContent")
        self.assertEqual(req["goog"], "g-test-fake")
        self.assertEqual(req["body"], {"contents": [{"parts": [{"text": "a teapot"}]}],
                                       "generationConfig": {"responseModalities": ["TEXT", "IMAGE"], "imageConfig": {"aspectRatio": "2:3"}}})
        self.call(s, "image_generate", {"prompt": "a cup", "size": "auto"})
        self.assertNotIn("imageConfig", self.seen[-1]["body"]["generationConfig"])
        r = self.call(s, "image_generate", {"prompt": "nothing at all"})
        self.assertTrue(r.get("isError"))
        self.assertIn("Gemini returned no image", self.text(r))
        self.assertIn("Here is your image.", self.text(r))

    def test_provider_choice(self):
        os.environ["OPENAI_API_KEY"] = "sk-test-fake"
        os.environ["GEMINI_API_KEY"] = "g-test-fake"
        os.environ["OMNI_OPENAI_IMAGE_BASE"] = os.environ["OMNI_GEMINI_IMAGE_BASE"] = self.base
        s = self.profile(image_gen={"provider": "gemini", "gemini_model": "gemini-x-image"})
        self.assertEqual(self.call(s, "image_generate", {"prompt": "one"})["details"]["provider"], "gemini")   # the profile's choice
        self.assertEqual(self.seen[-1]["path"], "/v1beta/models/gemini-x-image:generateContent")
        self.assertEqual(self.call(s, "image_generate", {"prompt": "two", "provider": "OpenAI"})["details"]["provider"], "openai")
        r = self.call(s, "image_generate", {"prompt": "three", "provider": "nope"})
        self.assertTrue(r.get("isError"))
        self.assertIn("no image provider 'nope'", self.text(r))
        del os.environ["GEMINI_API_KEY"]
        r = self.call(s, "image_generate", {"prompt": "four"})                       # the profile's provider lost its key
        self.assertTrue(r.get("isError"))
        self.assertIn("'gemini' is not set up", self.text(r))


class TestRegistryAndRules(Round15):
    def test_no_provider(self):
        s = self.profile()
        r = self.call(s, "image_generate", {"prompt": "a cat"})
        self.assertTrue(r.get("isError"))
        text = self.text(r)
        self.assertIn("No image provider is set up", text)
        self.assertIn("OPENAI_API_KEY", text)
        self.assertIn("GEMINI_API_KEY", text)
        self.assertFalse((Path(s.cwd) / "images").exists())
        listing = s.caps.commands()["image-providers"][1]("")
        self.assertIn("openai  not set up", listing)
        self.assertIn("gemini  not set up", listing)

    def test_custom_provider_shapes(self):
        calls = []
        src = Path(tempfile.mkdtemp(prefix="omni-img-src-")) / "made.png"
        src.write_bytes(PNG)

        def generate(prompt, size, session):
            calls.append((prompt, size, session.cwd))
            if "dict" in prompt:
                return {"data": bytearray(JPG)}
            if "file-url" in prompt:
                return {"url": "file:///etc/passwd"}
            if "url" in prompt:
                return {"url": f"{self.base}/files/out.jpg"}
            if "path" in prompt:
                return {"path": str(src)}
            if "boom" in prompt:
                raise RuntimeError("backend down")
            if "empty" in prompt:
                return b""
            return PNG

        p = image_gen.register_provider("Fake", generate, description="test backend")
        self.assertIs(image_gen.PROVIDERS["fake"], p)
        s = self.profile()
        listing = s.caps.commands()["image-providers"][1]("")
        self.assertIn("fake  available  - test backend", listing)

        r = self.call(s, "image_generate", {"prompt": "plain"})                      # first available provider
        self.assertEqual(r["details"]["provider"], "fake")
        self.assertEqual(calls[0], ("plain", "1024x1024", s.cwd))
        self.assertEqual((Path(s.cwd) / r["details"]["path"]).read_bytes(), PNG)
        self.assertEqual(self.call(s, "image_generate", {"prompt": "dict"})["details"]["path"], "images/dict-1.jpg")
        self.assertEqual(self.call(s, "image_generate", {"prompt": "url"})["details"]["path"], "images/url-1.jpg")
        r = self.call(s, "image_generate", {"prompt": "path", "path": "copied.png"})
        self.assertEqual((Path(s.cwd) / "copied.png").read_bytes(), PNG)
        self.assertEqual(self.call(s, "image_generate", {"prompt": "!!!"})["details"]["path"], "images/image-1.png")

        for prompt, expect in (("file-url", "not http(s)"), ("boom", "RuntimeError: backend down"), ("empty", "returned no image")):
            r = self.call(s, "image_generate", {"prompt": prompt})
            self.assertTrue(r.get("isError"), prompt)
            self.assertIn(expect, self.text(r))
        r = self.call(s, "image_generate", {"prompt": "path", "path": "copied.png"})  # never overwrites
        self.assertTrue(r.get("isError"))
        self.assertIn("already exists", self.text(r))

        s2 = self.profile(image_gen={"provider": "fake"})
        self.assertIn("(profile default)", s2.caps.commands()["image-providers"][1](""))
        image_gen.register_provider("off", generate, available=lambda session: False)
        r = self.call(s, "image_generate", {"prompt": "x", "provider": "off"})
        self.assertTrue(r.get("isError"))
        self.assertIn("not set up", self.text(r))
        image_gen.unregister_provider("FAKE")
        image_gen.unregister_provider("fake")                                        # twice is fine
        self.assertNotIn("fake", image_gen.PROVIDERS)
        self.assertTrue(self.call(s, "image_generate", {"prompt": "plain"}).get("isError"))

    def test_path_escape_and_size(self):
        calls = []
        image_gen.register_provider("fake", lambda prompt, size, session: calls.append(prompt) or PNG)
        s = self.profile()
        outside = Path(tempfile.mkdtemp(prefix="omni-img-outside-")) / "x.png"
        for bad in ("../escape.png", str(outside), "images/../../escape.png"):
            r = self.call(s, "image_generate", {"prompt": "a cat", "path": bad})
            self.assertTrue(r.get("isError"), bad)
            self.assertIn("inside the project", self.text(r))
        self.assertFalse(outside.exists())
        self.assertFalse((Path(s.cwd).parent / "escape.png").exists())
        for bad in ("512x512", "huge", "1024"):
            r = self.call(s, "image_generate", {"prompt": "a cat", "size": bad})
            self.assertTrue(r.get("isError"), bad)
            self.assertIn("size must be one of", self.text(r))
        self.assertTrue(self.call(s, "image_generate", {"prompt": "  "}).get("isError"))
        self.assertEqual(calls, [])                                                  # nothing was generated for bad requests
        r = self.call(s, "image_generate", {"prompt": "a cat", "size": "1024 X 1536", "path": "sub/dir/cat.png"})
        self.assertFalse(r.get("isError"), self.text(r))
        self.assertEqual(r["details"]["size"], "1024x1536")
        self.assertTrue((Path(s.cwd) / "sub" / "dir" / "cat.png").is_file())

    def test_registered_once(self):
        self.assertIs(caps.load_builtin()["image-gen"], image_gen.CAPABILITIES[0])
        self.assertEqual(image_gen.CAPABILITIES[0].inspired_by, "Hermes image_gen plugins")


if __name__ == "__main__":
    unittest.main()
