"""gemini — Google Gemini image generation ("Nano Banana"), stdlib only.

Config (config/image-provider.json → providers.gemini):
    model        default "gemini-3.1-flash-lite-image" (cheapest; 1K only).
                 Alternatives: "gemini-3.1-flash-image" (512/1K/2K/4K),
                 "gemini-3-pro-image"; "gemini-2.5-flash-image" is deprecated.
    api          "interactions" (default, POST /v1beta/interactions) or
                 "generate_content" (POST /v1beta/models/<model>:generateContent)
    image_size   "1K" (default) — "512", "1K", "2K", "4K" where the model allows
    mime_type    "image/png" (default) or "image/jpeg"
    api_key_env  env vars to read the key from, in order
    key_files    files under ~/.config/claude-dnd/ to try after the env vars
    timeout      seconds (default 120)
    base_url     API root (tests point this at a local fake)

The key is never logged or put into error messages.

Response parsing is deliberately shape-tolerant: the first object anywhere in
the JSON that carries base64 `data` plus an image mime type (`mime_type` or
`mimeType`) is taken — this covers steps[].content[], output_image and the
generateContent candidates[].content.parts[].inlineData layouts.
"""

import base64
import json
import os
import urllib.error
import urllib.request
from pathlib import Path
from typing import Optional

from . import ImageRequest, ImageResult, ProviderError

DEFAULT_MODEL = "gemini-3.1-flash-lite-image"
DEFAULT_BASE = "https://generativelanguage.googleapis.com/v1beta"
ASPECT_RATIOS = {"1:1": 1.0, "3:2": 1.5, "2:3": 2 / 3, "3:4": 0.75, "4:3": 4 / 3,
                 "4:5": 0.8, "5:4": 1.25, "9:16": 9 / 16, "16:9": 16 / 9, "21:9": 21 / 9}
KEY_DIR = Path.home() / ".config" / "claude-dnd"


def aspect_ratio(width: int, height: int) -> str:
    """Nearest supported aspect ratio for a requested size."""
    want = width / max(1, height)
    return min(ASPECT_RATIOS, key=lambda r: abs(ASPECT_RATIOS[r] - want))


def find_image(obj) -> "Optional[tuple[bytes, str]]":
    """(bytes, mime) of the first inline image anywhere in a response."""
    stack = [obj]
    while stack:
        cur = stack.pop(0)
        if isinstance(cur, dict):
            mime = cur.get("mime_type") or cur.get("mimeType") or ""
            data = cur.get("data")
            if isinstance(data, str) and data and str(mime).startswith("image/"):
                try:
                    return base64.b64decode(data, validate=False), mime
                except (ValueError, TypeError):
                    pass
            stack.extend(cur.values())
        elif isinstance(cur, list):
            stack.extend(cur)
    return None


def _error_message(body: bytes) -> str:
    try:
        err = json.loads(body.decode("utf-8", "replace")).get("error", {})
        return str(err.get("message") or err.get("status") or "")[:300]
    except (ValueError, AttributeError):
        return body[:200].decode("utf-8", "replace")


class GeminiProvider:
    name = "gemini"

    def __init__(self, config: dict):
        self.model = config.get("model", DEFAULT_MODEL)
        self.api = config.get("api", "interactions")
        self.image_size = config.get("image_size", "1K")
        self.mime_type = config.get("mime_type", "image/png")
        self.env_names = config.get("api_key_env", ["DND_IMAGE_KEY", "GEMINI_API_KEY"])
        self.key_files = config.get("key_files", ["image.key", "tts.key"])
        self.timeout = float(config.get("timeout", 120))
        self.base_url = config.get("base_url", DEFAULT_BASE).rstrip("/")
        self._format_as_list = bool(config.get("response_format_as_list", False))

    # ── Key ──
    def _key(self) -> "tuple[Optional[str], str]":
        for env in self.env_names:
            v = os.environ.get(env, "").strip()
            if v:
                return v, f"env:{env}"
        for name in self.key_files:
            path = KEY_DIR / name
            try:
                v = path.read_text(encoding="utf-8").strip()
            except OSError:
                continue
            if v:
                return v, f"file:{path}"
        return None, "unset"

    def available(self) -> "tuple[bool, str]":
        key, source = self._key()
        if not key:
            return False, (f"no API key — set {self.env_names[0] if self.env_names else 'GEMINI_API_KEY'} "
                           f"or {' / '.join(self.env_names[1:])} (key from aistudio.google.com)")
        if self.api not in ("interactions", "generate_content"):
            return False, f"unknown api '{self.api}'"
        return True, f"{self.model} via {self.api} (key {source})"

    # ── Requests ──
    def _body(self, req: ImageRequest) -> "tuple[str, dict]":
        ratio = aspect_ratio(req.width, req.height)
        if self.api == "generate_content":
            image_cfg = {"aspectRatio": ratio}
            if self.image_size:
                image_cfg["imageSize"] = self.image_size
            return (f"{self.base_url}/models/{self.model}:generateContent", {
                "contents": [{"parts": [{"text": req.prompt}]}],
                "generationConfig": {"responseModalities": ["IMAGE"], "imageConfig": image_cfg},
            })
        fmt = {"type": "image", "mime_type": self.mime_type, "aspect_ratio": ratio}
        if self.image_size:
            fmt["image_size"] = self.image_size
        return (f"{self.base_url}/interactions", {
            "model": self.model,
            "input": [{"type": "text", "text": req.prompt}],
            "response_format": [fmt] if self._format_as_list else fmt,
        })

    def _post(self, url: str, body: dict, key: str) -> dict:
        data = json.dumps(body).encode("utf-8")
        request = urllib.request.Request(url, data=data, method="POST", headers={
            "Content-Type": "application/json", "x-goog-api-key": key})
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            msg = _error_message(e.read())
            retryable = e.code == 429 or e.code >= 500
            raise ProviderError(f"Gemini HTTP {e.code}: {msg}", retryable=retryable) from None
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            raise ProviderError(f"Gemini unreachable: {getattr(e, 'reason', e)}", retryable=True) from None
        except ValueError:
            raise ProviderError("Gemini returned invalid JSON", retryable=True) from None

    def generate(self, req: ImageRequest) -> ImageResult:
        key, _ = self._key()
        if not key:
            raise ProviderError("no API key configured")
        url, body = self._body(req)
        try:
            response = self._post(url, body, key)
        except ProviderError as e:
            # The interactions docs show response_format both as an object and
            # as a list; if the API rejects our shape, try the other one once.
            if (self.api == "interactions" and "HTTP 400" in str(e)
                    and "response_format" in str(e)):
                self._format_as_list = not self._format_as_list
                url, body = self._body(req)
                response = self._post(url, body, key)
            else:
                raise
        found = find_image(response)
        if not found:
            status = response.get("status") if isinstance(response, dict) else None
            if status and status != "completed":
                raise ProviderError(f"Gemini interaction not completed (status {status})", retryable=True)
            raise ProviderError("Gemini returned no image (prompt may have been blocked)")
        data, mime = found
        return ImageResult(data=data, mime=mime,
                           meta={"model": self.model, "api": self.api, "aspect_ratio": aspect_ratio(req.width, req.height)})


def create(config: dict) -> GeminiProvider:
    return GeminiProvider(config)
