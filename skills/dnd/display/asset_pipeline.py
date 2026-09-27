"""asset_pipeline.py — turn wait-list entries into images, after play.

    queue (pending-assets.json) ──▶ prompt ──▶ ImageProvider ──▶ file + manifest

Driven by scripts/assets.py (`/dm:dnd assets`). Knows nothing about any
specific image service: providers come from image_providers via
config/image-provider.json, which also holds the shared style prompt so all
images look like one set.

Images land in <data-root>/assets/<kind>s/<slug>.<ext> and are registered
in the global manifest, so every campaign can reuse them.
"""

import os
import shutil
import sys
import tempfile
from datetime import datetime, timezone
from typing import Callable, Iterable, Optional

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)
from asset_queue import PendingQueue
from asset_seed import SeedCatalog
from asset_store import CATEGORIES, IMAGE_EXTS, KINDS, AssetStore, make_key, slugify
from config_loader import ConfigFile, check_types
from image_providers import MIME_EXT, ImageRequest, ImageResult, ProviderError, load_provider


def validate_provider_config(cfg: dict) -> "list[str]":
    problems = check_types(cfg, {"version": int, "active": dict, "sizes": dict,
                                 "style": dict, "providers": dict})
    if problems:
        return problems
    for kind, name in cfg["active"].items():
        if kind not in KINDS + ("default",):
            problems.append(f"active: unknown kind '{kind}'")
        if name not in cfg["providers"]:
            problems.append(f"active.{kind}: provider '{name}' is not configured")
    if "default" not in cfg["active"]:
        problems.append("active.default is required")
    for kind, size in cfg["sizes"].items():
        if (not isinstance(size, list) or len(size) != 2
                or not all(isinstance(v, int) and 64 <= v <= 4096 for v in size)):
            problems.append(f"sizes.{kind} should be [width, height] between 64 and 4096")
    return problems


def provider_config() -> ConfigFile:
    return ConfigFile("image-provider.json", validator=validate_provider_config)


def parse_key(text: str) -> str:
    """'item:Flammenschwert der Asche' / 'token:Wirtin Hilde' / 'Heiltrank' → key."""
    kind, sep, name = (text or "").partition(":")
    if sep and kind in KINDS:
        return make_key(kind, name)
    return make_key("item", text)


def build_prompt(entry: dict, cfg: dict) -> str:
    """Subject first (the entry's own prompt, or its name), then the shared style."""
    style = cfg.get("style") or {}
    kind = entry.get("kind", "item")
    subject = (entry.get("prompt") or "").strip()
    if not subject:
        subject = entry.get("name") or entry["key"].partition(":")[2].replace("-", " ")
        if kind == "item" and entry.get("category") in CATEGORIES:
            subject += f" ({entry['category']})"
        if entry.get("hint"):
            subject += f". {entry['hint']}"
    parts = [p.strip().rstrip(".") for p in (subject, style.get(kind, ""), style.get("base", ""))]
    return ". ".join(p[:1].upper() + p[1:] for p in parts if p) + "."


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def postprocess(data: bytes, mime: str, width: int, height: int) -> "tuple[bytes, str]":
    """Downscale to the configured size when Pillow is available; else unchanged.

    Providers often return more pixels than an inventory icon needs (Gemini
    starts at 1K). Pillow is optional — without it images are stored as-is.
    """
    try:
        from PIL import Image
    except ImportError:
        return data, mime
    import io
    try:
        with Image.open(io.BytesIO(data)) as img:
            if img.width <= width and img.height <= height:
                return data, mime
            img.thumbnail((width, height), Image.LANCZOS)
            out = io.BytesIO()
            if mime == "image/jpeg":
                img.convert("RGB").save(out, "JPEG", quality=90)
            else:
                img.save(out, "PNG", optimize=True)
                mime = "image/png"
            return out.getvalue(), mime
    except Exception:
        return data, mime   # unreadable for Pillow → keep what the provider sent


# Sprites (map objects) are generated in front of this colour and cut out.
CHROMA_RGB = (255, 0, 255)


def pillow_available() -> bool:
    try:
        import PIL  # noqa: F401
        return True
    except ImportError:
        return False


def chroma_key(data: bytes) -> "tuple[bytes, str]":
    """Make the magenta background transparent; returns (PNG bytes, "image/png").

    "Magenta-ness" is min(R, B) − G: high on the key colour, low on real
    colours (reds and blues have one of R/B low, greens have G high). Pixels
    above the upper bound become transparent, those between the bounds fade
    (antialiased edges), and on those edge pixels the magenta tint is pulled
    out of R and B. Needs Pillow.
    """
    import io
    from PIL import Image, ImageChops
    lo, hi = 60, 150
    with Image.open(io.BytesIO(data)) as src:
        img = src.convert("RGB")
    r, g, b = img.split()
    mag = ImageChops.subtract(ImageChops.darker(r, b), g)
    alpha = mag.point(lambda v: 0 if v >= hi else 255 if v <= lo else int(255 * (hi - v) / (hi - lo)))
    edge = mag.point(lambda v: 255 if lo < v < hi else 0)
    g_up = g.point(lambda v: min(255, v + 40))
    r = Image.composite(ImageChops.darker(r, g_up), r, edge)
    b = Image.composite(ImageChops.darker(b, g_up), b, edge)
    out = Image.merge("RGBA", (r, g, b, alpha))
    bbox = alpha.point(lambda v: 255 if v > 16 else 0).getbbox()
    if bbox:   # trim empty margins so the object fills its squares
        out = out.crop(bbox)
    buf = io.BytesIO()
    out.save(buf, "PNG", optimize=True)
    return buf.getvalue(), "image/png"


def _write_atomic(path: str, data: bytes) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".img.", suffix=".tmp")
    with os.fdopen(fd, "wb") as f:
        f.write(data)
    os.replace(tmp, path)


class Pipeline:
    def __init__(self, store: AssetStore, queue: PendingQueue,
                 config: Optional[ConfigFile] = None,
                 loader: Callable = load_provider,
                 catalog: Optional[SeedCatalog] = None):
        self.store = store
        self.queue = queue
        self.catalog = catalog
        self.config = config or provider_config()
        self._loader = loader
        self._providers: dict = {}

    # ── Providers ──
    def provider_name(self, kind: str, override: Optional[str] = None) -> str:
        active = self.config.get().get("active", {})
        return override or active.get(kind) or active.get("default", "dummy")

    def provider(self, name: str):
        if name not in self._providers:
            self._providers[name] = self._loader(name, self.config.get())
        return self._providers[name]

    def providers_status(self) -> "list[dict]":
        out = []
        for name in sorted(self.config.get().get("providers", {})):
            try:
                ok, why = self.provider(name).available()
            except ProviderError as e:
                ok, why = False, str(e)
            out.append({"name": name, "available": ok, "detail": why})
        return out

    # ── Generation ──
    def _ready_provider(self, name: str, unavailable: "dict[str, str]", log: Callable[[str], None],
                        kind: str = "item"):
        """The provider if it can generate now; else None (remembered in `unavailable`)."""
        if kind == "sprite" and not pillow_available():
            if "sprite:pillow" not in unavailable:
                unavailable["sprite:pillow"] = "Pillow missing"
                log("! sprites need Pillow to cut out the background: pip install pillow")
            return None
        if name in unavailable:
            return None
        try:
            prov = self.provider(name)
            ok, why = prov.available()
        except ProviderError as e:
            ok, why = False, str(e)
        if not ok:
            unavailable[name] = why
            log(f"! provider '{name}' unavailable: {why}")
            return None
        return prov

    @staticmethod
    def _request(prov, prompt: str, kind: str, size: "list[int]"):
        """One provider call; raises on any failure, including an unusable image."""
        w, h = size
        result = prov.generate(ImageRequest(prompt=prompt, kind=kind, width=w, height=h))
        if not MIME_EXT.get(result.mime) or not result.data:
            raise ProviderError(f"unsupported image type {result.mime!r}")
        if kind == "sprite":   # cut out here, so a bad image counts as this entry's failure
            try:
                data, mime = chroma_key(result.data)
            except Exception as e:
                raise ProviderError(f"could not cut out the background: {e}")
            result = ImageResult(data=data, mime=mime, meta=result.meta)
        return result

    def _store_image(self, key: str, kind: str, result, size: "list[int]") -> str:
        """Post-process and write one image; returns its path relative to the assets root."""
        data, mime = postprocess(result.data, result.mime, *size)
        rel = f"{kind}s/{key.partition(':')[2]}{MIME_EXT.get(mime, MIME_EXT[result.mime])}"
        _write_atomic(os.path.join(self.store.global_root, rel), data)
        return rel

    def _has_specific_image(self, key: str) -> bool:
        """An image exists that is not a generic seed (a generic one may be replaced)."""
        found = self.store.find(key)
        return bool(found and not found["entry"].get("generic"))

    def generate(self, limit: Optional[int] = None, provider: Optional[str] = None,
                 keys: Optional[Iterable[str]] = None, dry_run: bool = False,
                 log: Callable[[str], None] = print) -> dict:
        """Work through pending entries. Returns {"done", "failed", "planned", "blocked"}.

        A key whose only image is a generic seed is generated anyway: the
        wait-list entry is the specific version and replaces the generic one.
        """
        cfg = self.config.get()
        sizes = cfg.get("sizes", {})
        max_attempts = int(cfg.get("max_attempts", 3))
        wanted = set(keys) if keys else None
        entries = [e for e in self.queue.entries("pending") if wanted is None or e["key"] in wanted]
        if limit is not None:
            entries = entries[:max(0, limit)]

        summary = {"done": [], "failed": [], "planned": [], "blocked": []}
        unavailable: "dict[str, str]" = {}
        seed_prompts = self._seed_prompts()
        for entry in entries:
            key, kind = entry["key"], entry.get("kind", "item")
            if not entry.get("prompt") and not entry.get("hint") and key in seed_prompts:
                entry = dict(entry, prompt=seed_prompts[key])
            if self._has_specific_image(key):
                self.queue.update(key, status="done", last_error=None)
                log(f"= {key}: already has an image")
                continue
            name = self.provider_name(kind, provider)
            prompt = build_prompt(entry, cfg)
            if dry_run:
                summary["planned"].append({"key": key, "provider": name, "prompt": prompt})
                continue
            prov = self._ready_provider(name, unavailable, log, kind)
            if prov is None:
                summary["blocked"].append(key)
                continue

            size = sizes.get(kind, [512, 512])
            attempts = int(entry.get("attempts") or 0) + 1
            try:
                result = self._request(prov, prompt, kind, size)
            except Exception as e:   # provider bugs must not abort the whole batch
                retry = attempts < max_attempts
                self.queue.update(key, status="pending" if retry else "failed",
                                  attempts=attempts, last_error=str(e)[:300])
                summary["failed"].append(key)
                log(f"✗ {key}: {e}" + (" (will retry)" if retry else " (giving up)"))
                continue

            rel = self._store_image(key, kind, result, size)
            self.store.global_manifest.set_entry(key, {
                "file": rel, "name": entry.get("name"), "category": entry.get("category"),
                "source": name, "model": result.meta.get("model"), "prompt": prompt,
                "created": _now(),
            })
            self.queue.update(key, status="done", attempts=attempts, last_error=None, prompt=prompt)
            summary["done"].append(key)
            log(f"✓ {key} → {rel}")
        return summary

    def _seed_prompts(self) -> "dict[str, str]":
        """key → prompt of the seed catalogue (e.g. a wait-listed sprite:table)."""
        try:
            return {e["key"]: e["prompt"] for e in (self.catalog or SeedCatalog()).entries()}
        except Exception:
            return {}

    # ── Seeding (campaign-independent standard content) ──
    def seed(self, catalog: SeedCatalog, sets: Optional[Iterable[str]] = None,
             limit: Optional[int] = None, provider: Optional[str] = None,
             dry_run: bool = False, log: Callable[[str], None] = print) -> dict:
        """Generate catalogue entries that have no image yet.

        Never replaces an existing image (generic or specific) and leaves
        keys alone that are pending on the wait-list — those get their
        specific image from generate(). Failures are not recorded: the next
        seed run simply tries again. `limit` counts images to make; skipped
        entries do not use it up.

        Returns {"done", "failed", "planned", "blocked", "existing", "queued"}.
        """
        cfg = self.config.get()
        sizes = cfg.get("sizes", {})
        waiting = {e["key"] for e in self.queue.entries("pending")}
        summary = {"done": [], "failed": [], "planned": [], "blocked": [],
                   "existing": [], "queued": []}
        unavailable: "dict[str, str]" = {}
        budget = None if limit is None else max(0, limit)
        for entry in catalog.entries(sets):
            key, kind = entry["key"], entry["kind"]
            if self.store.find(key):
                summary["existing"].append(key)
                continue
            if key in waiting:
                summary["queued"].append(key)
                continue
            if budget is not None:
                if budget == 0:
                    break
                budget -= 1
            name = self.provider_name(kind, provider)
            prompt = build_prompt(entry, cfg)
            if dry_run:
                summary["planned"].append({"key": key, "provider": name, "prompt": prompt})
                continue
            prov = self._ready_provider(name, unavailable, log, kind)
            if prov is None:
                summary["blocked"].append(key)
                continue

            size = sizes.get(kind, [512, 512])
            try:
                result = self._request(prov, prompt, kind, size)
            except Exception as e:   # one bad image must not stop the batch
                summary["failed"].append(key)
                log(f"✗ {key}: {e}")
                continue

            rel = self._store_image(key, kind, result, size)
            record = {"file": rel, "name": entry["name"], "category": entry["category"],
                      "source": name, "model": result.meta.get("model"), "prompt": prompt,
                      "generic": True, "seed": entry["set"], "created": _now()}
            for field in ("srd", "template"):
                if entry.get(field):
                    record[field] = entry[field]
            self.store.global_manifest.set_entry(key, record, aliases=entry["aliases"])
            summary["done"].append(key)
            log(f"✓ {key} → {rel}")
        return summary

    # ── Manual import ──
    def add_file(self, key: str, source: str, name: Optional[str] = None,
                 category: Optional[str] = None) -> str:
        """Register an existing image for `key` (copied into the assets tree)."""
        ext = os.path.splitext(source)[1].lower()
        if ext not in IMAGE_EXTS:
            raise ValueError(f"not an image file: {source}")
        if not os.path.isfile(source):
            raise FileNotFoundError(source)
        kind, _, slug = key.partition(":")
        if kind not in KINDS or not slug:
            raise ValueError(f"invalid key: {key}")
        if category is not None and category not in CATEGORIES:
            raise ValueError(f"unknown category: {category}")
        rel = f"{kind}s/{slug}{ext}"
        dest = os.path.join(self.store.global_root, rel)
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        if os.path.abspath(source) != os.path.abspath(dest):
            shutil.copyfile(source, dest)
        queued = next((e for e in self.queue.entries() if e["key"] == key), {})
        self.store.global_manifest.set_entry(key, {
            "file": rel, "name": name or queued.get("name") or slug.replace("-", " "),
            "category": category or queued.get("category"), "source": "manual",
            "created": _now(),
        })
        if queued:
            self.queue.update(key, status="done", last_error=None)
        return rel
