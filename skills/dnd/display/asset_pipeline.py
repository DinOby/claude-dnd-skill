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
from asset_store import CATEGORIES, IMAGE_EXTS, KINDS, AssetStore, make_key, slugify
from config_loader import ConfigFile, check_types
from image_providers import MIME_EXT, ImageRequest, ProviderError, load_provider


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


def _write_atomic(path: str, data: bytes) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".img.", suffix=".tmp")
    with os.fdopen(fd, "wb") as f:
        f.write(data)
    os.replace(tmp, path)


class Pipeline:
    def __init__(self, store: AssetStore, queue: PendingQueue,
                 config: Optional[ConfigFile] = None,
                 loader: Callable = load_provider):
        self.store = store
        self.queue = queue
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
    def generate(self, limit: Optional[int] = None, provider: Optional[str] = None,
                 keys: Optional[Iterable[str]] = None, dry_run: bool = False,
                 log: Callable[[str], None] = print) -> dict:
        """Work through pending entries. Returns {"done", "failed", "planned", "blocked"}."""
        cfg = self.config.get()
        sizes = cfg.get("sizes", {})
        max_attempts = int(cfg.get("max_attempts", 3))
        wanted = set(keys) if keys else None
        entries = [e for e in self.queue.entries("pending") if wanted is None or e["key"] in wanted]
        if limit is not None:
            entries = entries[:max(0, limit)]

        summary = {"done": [], "failed": [], "planned": [], "blocked": []}
        unavailable: "dict[str, str]" = {}
        for entry in entries:
            key, kind = entry["key"], entry.get("kind", "item")
            if self.store.find(key):
                self.queue.update(key, status="done", last_error=None)
                log(f"= {key}: already has an image")
                continue
            name = self.provider_name(kind, provider)
            prompt = build_prompt(entry, cfg)
            if dry_run:
                summary["planned"].append({"key": key, "provider": name, "prompt": prompt})
                continue
            if name in unavailable:
                summary["blocked"].append(key)
                continue
            try:
                prov = self.provider(name)
                ok, why = prov.available()
            except ProviderError as e:
                ok, why = False, str(e)
            if not ok:
                unavailable[name] = why
                summary["blocked"].append(key)
                log(f"! provider '{name}' unavailable: {why}")
                continue

            w, h = sizes.get(kind, [512, 512])
            attempts = int(entry.get("attempts") or 0) + 1
            try:
                result = prov.generate(ImageRequest(prompt=prompt, kind=kind, width=w, height=h))
                ext = MIME_EXT.get(result.mime)
                if not ext or not result.data:
                    raise ProviderError(f"unsupported image type {result.mime!r}")
            except Exception as e:   # provider bugs must not abort the whole batch
                retry = attempts < max_attempts
                self.queue.update(key, status="pending" if retry else "failed",
                                  attempts=attempts, last_error=str(e)[:300])
                summary["failed"].append(key)
                log(f"✗ {key}: {e}" + (" (will retry)" if retry else " (giving up)"))
                continue

            rel = f"{kind}s/{key.partition(':')[2]}{ext}"
            _write_atomic(os.path.join(self.store.global_root, rel), result.data)
            self.store.global_manifest.set_entry(key, {
                "file": rel, "name": entry.get("name"), "category": entry.get("category"),
                "source": name, "model": result.meta.get("model"), "prompt": prompt,
                "created": _now(),
            })
            self.queue.update(key, status="done", attempts=attempts, last_error=None, prompt=prompt)
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
