"""image_providers — swappable image generation back ends.

The rest of the system only sees this interface. A provider is one module
that defines `create(config: dict) -> ImageProvider`; config/image-provider.json
names the module per provider and carries its settings:

    "providers": {"gemini": {"module": "gemini", "model": "…"},
                  "mine":   {"module": "my_backend", "url": "http://…"}}

Modules are looked up in <data-root>/providers/ first (your own back ends,
safe from plugin updates), then in this package. Module names are plain
identifiers — no paths — so config can only pick from those two places.
"""

import importlib.util
import os
import re
import sys
from dataclasses import dataclass, field
from typing import Optional, Protocol

_HERE = os.path.dirname(os.path.abspath(__file__))
_MODULE_RE = re.compile(r"^[a-z_][a-z0-9_]{0,40}$")


@dataclass
class ImageRequest:
    prompt: str
    kind: str                      # item | token | map
    width: int = 512
    height: int = 512
    negative_prompt: str = ""
    seed: Optional[int] = None
    transparent: bool = False      # a wish, not a guarantee — providers may ignore it


@dataclass
class ImageResult:
    data: bytes
    mime: str                      # image/png | image/jpeg | image/webp
    meta: dict = field(default_factory=dict)   # model, revised prompt, cost hints, …


class ProviderError(Exception):
    """A generation failed. `retryable` marks transient failures (rate limit, timeout)."""

    def __init__(self, message: str, retryable: bool = False):
        super().__init__(message)
        self.retryable = retryable


class ImageProvider(Protocol):
    name: str

    def available(self) -> "tuple[bool, str]":
        """(ready, reason) — e.g. (False, "GEMINI_API_KEY not set")."""

    def generate(self, request: ImageRequest) -> ImageResult:
        """Produce one image or raise ProviderError."""


MIME_EXT = {"image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp"}


def _user_provider_dir() -> str:
    scripts = os.path.join(_HERE, os.pardir, os.pardir, "scripts")
    if scripts not in sys.path:
        sys.path.insert(0, scripts)
    try:
        from paths import user_assets_dir
        return str(user_assets_dir().parent / "providers")
    except Exception:
        root = os.environ.get("DND_CAMPAIGN_ROOT", "").strip() or "~/.claude/dnd"
        return os.path.join(os.path.expanduser(root), "providers")


def load_provider(name: str, config: dict, user_dir: Optional[str] = None) -> ImageProvider:
    """Instantiate provider `name` from the "providers" table of `config`."""
    spec_cfg = (config.get("providers") or {}).get(name)
    if not isinstance(spec_cfg, dict):
        raise ProviderError(f"unknown image provider '{name}' (not in image-provider.json)")
    module = spec_cfg.get("module", name)
    if not isinstance(module, str) or not _MODULE_RE.match(module):
        raise ProviderError(f"provider '{name}': invalid module name {module!r}")
    for directory in (user_dir or _user_provider_dir(), _HERE):
        path = os.path.join(directory, module + ".py")
        if os.path.isfile(path):
            spec = importlib.util.spec_from_file_location(f"image_providers._{module}", path)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            if not callable(getattr(mod, "create", None)):
                raise ProviderError(f"provider module '{module}' has no create(config)")
            return mod.create(dict(spec_cfg))
    raise ProviderError(f"provider module '{module}' not found")
