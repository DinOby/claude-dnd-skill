"""Tests for image_providers/gemini.py against a local fake of the Gemini API.

No network and no real key: base_url points at a ThreadingHTTPServer that
records requests and replays canned responses.
"""

from __future__ import annotations

import base64
import json
import os
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
SKILL = REPO / "skills" / "dnd" if (REPO / "skills" / "dnd").is_dir() else REPO
DISPLAY = SKILL / "display"
if str(DISPLAY) not in sys.path:
    sys.path.insert(0, str(DISPLAY))

import image_providers as ip                 # noqa: E402
from image_providers import gemini           # noqa: E402

KEY = "test-key-12345"
PNG = b"\x89PNG\r\n\x1a\nfake-image-bytes"
B64 = base64.b64encode(PNG).decode()


class _Fake:
    """Canned responses, consumed in order; every request is recorded."""

    def __init__(self):
        self.requests = []
        self.responses = []

        fake = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                fake.requests.append({"path": self.path, "key": self.headers.get("x-goog-api-key"), "body": body})
                code, payload = fake.responses.pop(0) if fake.responses else (200, {})
                if callable(payload):
                    code, payload = payload(body)
                data = json.dumps(payload).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *a):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}/v1beta"

    def close(self):
        self.server.shutdown()
        self.server.server_close()


class GeminiTests(unittest.TestCase):
    def setUp(self):
        self.fake = _Fake()
        self.addCleanup(self.fake.close)
        env = {k: v for k, v in os.environ.items() if k not in ("DND_IMAGE_KEY", "GEMINI_API_KEY")}
        env["DND_IMAGE_KEY"] = KEY
        patcher = mock.patch.dict(os.environ, env, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def provider(self, **cfg):
        return gemini.create({"base_url": self.fake.base, "key_files": [], "timeout": 5, **cfg})

    def req(self, w=512, h=512):
        return ip.ImageRequest(prompt="A rusty dagger.", kind="item", width=w, height=h)

    def test_interactions_request_and_steps_response(self):
        self.fake.responses.append((200, {"status": "completed", "steps": [
            {"type": "thought", "content": [{"type": "text", "text": "…"}]},
            {"type": "model_output", "content": [{"type": "image", "mime_type": "image/png", "data": B64}]}]}))
        res = self.provider().generate(self.req())
        self.assertEqual((res.data, res.mime), (PNG, "image/png"))
        self.assertEqual(res.meta["model"], "gemini-3.1-flash-lite-image")
        sent = self.fake.requests[0]
        self.assertEqual(sent["path"], "/v1beta/interactions")
        self.assertEqual(sent["key"], KEY)
        self.assertEqual(sent["body"]["model"], "gemini-3.1-flash-lite-image")
        self.assertEqual(sent["body"]["input"], [{"type": "text", "text": "A rusty dagger."}])
        self.assertEqual(sent["body"]["response_format"],
                         {"type": "image", "mime_type": "image/jpeg", "aspect_ratio": "1:1", "image_size": "1K"})

    def test_other_response_shapes(self):
        shapes = [
            {"interaction": {"output_image": {"data": B64, "mime_type": "image/jpeg"}}},
            {"candidates": [{"content": {"parts": [{"text": "here"},
                                                   {"inlineData": {"mimeType": "image/png", "data": B64}}]}}]},
        ]
        for shape in shapes:
            found = gemini.find_image(shape)
            self.assertIsNotNone(found, shape)
            self.assertEqual(found[0], PNG)

    def test_rejected_response_format_shape_retries_as_list(self):
        def strict(body):
            if isinstance(body["response_format"], dict):
                return 400, {"error": {"message": "Invalid value at 'response_format': expected list"}}
            return 200, {"steps": [{"content": [{"mime_type": "image/png", "data": B64}]}]}
        self.fake.responses += [(0, strict), (0, strict), (0, strict)]
        prov = self.provider()
        self.assertEqual(prov.generate(self.req()).data, PNG)
        self.assertIsInstance(self.fake.requests[1]["body"]["response_format"], list)
        prov.generate(self.req())   # remembers the list form: one request, no retry
        self.assertEqual(len(self.fake.requests), 3)

    def test_generate_content_api(self):
        self.fake.responses.append((200, {"candidates": [{"content": {"parts": [
            {"inlineData": {"mimeType": "image/png", "data": B64}}]}}]}))
        res = self.provider(api="generate_content", model="gemini-3.1-flash-image").generate(self.req(1024, 576))
        self.assertEqual(res.data, PNG)
        sent = self.fake.requests[0]
        self.assertEqual(sent["path"], "/v1beta/models/gemini-3.1-flash-image:generateContent")
        self.assertEqual(sent["body"]["generationConfig"],
                         {"responseModalities": ["IMAGE"], "imageConfig": {"aspectRatio": "16:9", "imageSize": "1K"}})

    def test_errors(self):
        cases = [
            ((429, {"error": {"message": "Resource exhausted"}}), True, "429"),
            ((503, {"error": {"message": "overloaded"}}), True, "503"),
            ((400, {"error": {"message": "prompt blocked by safety"}}), False, "safety"),
            ((403, {"error": {"message": "API key not valid"}}), False, "403"),
            ((200, {"status": "in_progress", "id": "v1_x"}), True, "not completed"),
            ((200, {"status": "completed", "steps": [{"content": [{"type": "text", "text": "no"}]}]}), False, "no image"),
        ]
        for response, retryable, text in cases:
            self.fake.responses.append(response)
            with self.assertRaises(ip.ProviderError) as ctx:
                self.provider().generate(self.req())
            self.assertEqual(ctx.exception.retryable, retryable, text)
            self.assertIn(text, str(ctx.exception))
            self.assertNotIn(KEY, str(ctx.exception))

    def test_unreachable_is_retryable(self):
        prov = gemini.create({"base_url": "http://127.0.0.1:9/v1beta", "key_files": [], "timeout": 2})
        with self.assertRaises(ip.ProviderError) as ctx:
            prov.generate(self.req())
        self.assertTrue(ctx.exception.retryable)

    def test_key_sources_and_availability(self):
        ok, why = self.provider().available()
        self.assertTrue(ok)
        self.assertIn("env:DND_IMAGE_KEY", why)
        self.assertNotIn(KEY, why)
        with mock.patch.dict(os.environ, {"DND_IMAGE_KEY": ""}):
            ok, why = self.provider().available()
            self.assertFalse(ok)
            self.assertIn("no API key", why)
            with mock.patch.object(gemini, "KEY_DIR", Path(__file__).parent / "_nope"):
                self.assertFalse(gemini.create({"base_url": self.fake.base}).available()[0])

    def test_aspect_ratio(self):
        self.assertEqual(gemini.aspect_ratio(512, 512), "1:1")
        self.assertEqual(gemini.aspect_ratio(1920, 1080), "16:9")
        self.assertEqual(gemini.aspect_ratio(1000, 1500), "2:3")

    def test_bundled_config_loads_gemini_by_default(self):
        import asset_pipeline as ap
        from config_loader import ConfigFile
        cfg = ConfigFile("image-provider.json", validator=ap.validate_provider_config,
                         default_dir=str(DISPLAY / "config"), user_dir=str(Path(__file__).parent / "_nope"))
        data = cfg.get()
        self.assertEqual(cfg.warnings, [])
        self.assertEqual(data["active"]["default"], "gemini")
        prov = ip.load_provider("gemini", data, user_dir=str(Path(__file__).parent / "_nope"))
        self.assertEqual(prov.model, "gemini-3.1-flash-lite-image")


@unittest.skipUnless(__import__("importlib").util.find_spec("PIL"), "Pillow not installed")
class PostprocessTests(unittest.TestCase):
    def test_downscales_large_images(self):
        import io
        from PIL import Image
        import asset_pipeline as ap
        buf = io.BytesIO()
        Image.new("RGB", (1024, 1024), (200, 50, 50)).save(buf, "PNG")
        data, mime = ap.postprocess(buf.getvalue(), "image/png", 512, 512)
        self.assertEqual(Image.open(io.BytesIO(data)).size, (512, 512))
        small, _ = ap.postprocess(data, "image/png", 512, 512)
        self.assertEqual(small, data)


class PostprocessFallbackTests(unittest.TestCase):
    def test_garbage_or_no_pillow_is_passthrough(self):
        import asset_pipeline as ap
        self.assertEqual(ap.postprocess(b"not an image", "image/png", 64, 64), (b"not an image", "image/png"))


if __name__ == "__main__":
    unittest.main()
