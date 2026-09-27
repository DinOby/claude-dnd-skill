"""dummy — offline test provider: a coloured PNG derived from the prompt.

No network, no key, no dependencies. Lets the whole pipeline (wait-list →
generate → manifest → display) be exercised for free. Same prompt → same
image; the colour is the only hint of what was asked for.
"""

import hashlib
import struct
import zlib

from . import ImageRequest, ImageResult


def _png(width: int, height: int, rgb: "tuple[int, int, int]", ring: "tuple[int, int, int]") -> bytes:
    """Solid square with a darker border — enough to see it in the UI."""
    border = max(2, min(width, height) // 16)
    rows = []
    for y in range(height):
        row = bytearray([0])   # filter: none
        for x in range(width):
            edge = x < border or y < border or x >= width - border or y >= height - border
            row += bytes(ring if edge else rgb)
        rows.append(bytes(row))

    def chunk(tag: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data))

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr)
            + chunk(b"IDAT", zlib.compress(b"".join(rows), 9)) + chunk(b"IEND", b""))


class DummyProvider:
    name = "dummy"

    def __init__(self, config: dict):
        self.size = int(config.get("size", 128))

    def available(self):
        return True, "offline test images"

    def generate(self, request: ImageRequest) -> ImageResult:
        h = hashlib.sha256(request.prompt.encode("utf-8")).digest()
        rgb = (60 + h[0] % 160, 60 + h[1] % 160, 60 + h[2] % 160)
        ring = tuple(c // 2 for c in rgb)
        side = max(8, min(self.size, 512))
        return ImageResult(data=_png(side, side, rgb, ring), mime="image/png",
                           meta={"provider": "dummy"})


def create(config: dict) -> DummyProvider:
    return DummyProvider(config)
