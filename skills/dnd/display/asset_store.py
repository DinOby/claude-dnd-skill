"""asset_store.py — images for game content (items, tokens, maps).

Layout under the data root (update-safe, see scripts/paths.py user_assets_dir):

    assets/
      manifest.json          content key → image file
      tokens/ items/ maps/   the image files (any sub-layout; manifest decides)

A campaign may carry its own `<campaign>/assets/` with the same layout; its
entries win over the global ones, so one campaign can re-skin a shared item.

manifest.json:
    {"version": 1,
     "entries": {
       "item:flammenschwert-der-asche": {"file": "items/flammenschwert-der-asche.png",
            "name": "Flammenschwert der Asche", "category": "weapon",
            "source": "gemini", "prompt": "…", "created": "2026-09-27T12:00:00Z"}},
     "aliases": {"flame sword of ash": "item:flammenschwert-der-asche"}}

Keys are "<kind>:<slug>" — see make_key(). Anything without an image
resolves to a category placeholder, never to an error.
"""

import json
import os
import re
import sys
import tempfile
import threading
import unicodedata
from typing import Callable, Iterable, Optional

_HERE = os.path.dirname(os.path.abspath(__file__))
PLACEHOLDER_DIR = os.path.join(_HERE, "assets", "placeholders")

KINDS = ("item", "token", "map")
ITEM_CATEGORIES = ("weapon", "armor", "potion", "scroll", "ring", "wand",
                   "wondrous", "tool", "gear")
TOKEN_CATEGORIES = ("pc", "npc", "enemy")
CATEGORIES = ITEM_CATEGORIES + TOKEN_CATEGORIES + ("map",)
DEFAULT_CATEGORY = {"item": "gear", "token": "npc", "map": "map"}
IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".webp", ".gif", ".svg")

_UMLAUTS = str.maketrans({"ä": "ae", "ö": "oe", "ü": "ue", "ß": "ss",
                          "Ä": "ae", "Ö": "oe", "Ü": "ue", "ẞ": "ss"})
# "Heiltrank (2)", "Rations x5", "Pfeile ×20", "3x Fackel", "2 Dolche" → quantity dropped
_QTY_TAIL = re.compile(r"\s*(?:[(\[]\s*[x×]?\s*\d+\s*[)\]]|[x×]\s*\d+)\s*$", re.IGNORECASE)
_QTY_HEAD = re.compile(r"^\s*\d+\s*[x×]?\s+", re.IGNORECASE)


# ── Keys ──────────────────────────────────────────────────────────────────────

def slugify(text: str, max_len: int = 80) -> str:
    """'Flammenschwert der Asche' → 'flammenschwert-der-asche' (ASCII, stable)."""
    s = unicodedata.normalize("NFKC", text or "").translate(_UMLAUTS)
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode("ascii")
    s = re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")
    return s[:max_len].rstrip("-")


def base_name(kind: str, name: str) -> str:
    """Display name without inventory quantities (items only)."""
    name = (name or "").strip()
    if kind == "item":
        name = _QTY_HEAD.sub("", _QTY_TAIL.sub("", name)).strip()
    return name


def make_key(kind: str, name: str) -> str:
    """Content key, e.g. make_key('item', 'Heiltrank (2)') → 'item:heiltrank'."""
    if kind not in KINDS:
        raise ValueError(f"unknown asset kind: {kind!r}")
    slug = slugify(base_name(kind, name))
    return f"{kind}:{slug}" if slug else ""


def _safe_rel(rel: str) -> Optional[str]:
    """Normalised relative image path inside an assets root, or None."""
    if not isinstance(rel, str) or not rel:
        return None
    norm = os.path.normpath(rel.replace("\\", "/"))
    if (os.path.isabs(norm) or norm.startswith(os.pardir) or os.sep + os.pardir in norm
            or not norm.lower().endswith(IMAGE_EXTS)):
        return None
    return norm


# ── Manifest ──────────────────────────────────────────────────────────────────

class Manifest:
    """One manifest.json, re-read when it changes on disk."""

    def __init__(self, root: str):
        self.root = root
        self.path = os.path.join(root, "manifest.json")
        self._sig: Optional[tuple] = ("unloaded",)
        self._data: dict = {"version": 1, "entries": {}, "aliases": {}}
        self.warning: Optional[str] = None

    def _signature(self) -> Optional[tuple]:
        try:
            st = os.stat(self.path)
            return (st.st_mtime_ns, st.st_size)
        except OSError:
            return None

    def data(self) -> dict:
        sig = self._signature()
        if sig != self._sig:
            self._sig = sig
            self._data, self.warning = self._read()
            if self.warning:
                print(f"asset_store: {self.warning}", file=sys.stderr)
        return self._data

    def _read(self) -> "tuple[dict, Optional[str]]":
        empty = {"version": 1, "entries": {}, "aliases": {}}
        try:
            with open(self.path, encoding="utf-8-sig") as f:
                raw = json.load(f)
        except FileNotFoundError:
            return empty, None
        except (OSError, ValueError) as e:
            return empty, f"{self.path}: {e} — manifest ignored"
        if not isinstance(raw, dict) or not isinstance(raw.get("entries", {}), dict):
            return empty, f"{self.path}: 'entries' must be an object — manifest ignored"
        entries = {k: v for k, v in raw.get("entries", {}).items()
                   if isinstance(v, dict) and isinstance(v.get("file"), str)}
        aliases = raw.get("aliases") if isinstance(raw.get("aliases"), dict) else {}
        return {"version": raw.get("version", 1), "entries": entries,
                "aliases": {slugify(a): k for a, k in aliases.items() if isinstance(k, str)}}, None

    def entry(self, key: str) -> Optional[dict]:
        return self.data()["entries"].get(key)

    def alias_target(self, kind: str, name: str) -> Optional[str]:
        target = self.data()["aliases"].get(slugify(base_name(kind, name)))
        return target if target and target.startswith(kind + ":") else None

    def set_entry(self, key: str, entry: dict, aliases: "Iterable[str]" = ()) -> None:
        """Add/replace one entry and write atomically (keeps unknown fields).

        `aliases` are other names for the same content; an alias that already
        points elsewhere is left alone.
        """
        try:
            with open(self.path, encoding="utf-8-sig") as f:
                raw = json.load(f)
            if not isinstance(raw, dict):
                raise ValueError("not an object")
        except FileNotFoundError:
            raw = {"version": 1, "entries": {}, "aliases": {}}
        raw.setdefault("entries", {})[key] = entry
        kind = key.partition(":")[0]
        for alias in aliases:
            slug = slugify(base_name(kind, alias))
            if slug and slug != key.partition(":")[2]:
                raw.setdefault("aliases", {}).setdefault(slug, key)
        os.makedirs(self.root, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.root, prefix=".manifest.", suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(raw, f, ensure_ascii=False, indent=2, sort_keys=True)
            f.write("\n")
        os.replace(tmp, self.path)


# ── Store ─────────────────────────────────────────────────────────────────────

def _default_global_root() -> str:
    scripts = os.path.join(_HERE, os.pardir, "scripts")
    if scripts not in sys.path:
        sys.path.insert(0, scripts)
    try:
        from paths import user_assets_dir
        return str(user_assets_dir())
    except Exception:
        root = os.environ.get("DND_CAMPAIGN_ROOT", "").strip() or "~/.claude/dnd"
        return os.path.join(os.path.expanduser(root), "assets")


Categorizer = Callable[[str, str], Optional[str]]


class AssetStore:
    """Resolves content names to image URLs or category placeholders.

    `categorize(kind, name)` is pluggable (set by the caller) so category
    detection can evolve without touching lookup; it only runs for names that
    have no manifest entry and no explicit category.
    """

    SCOPES = ("campaign", "global")

    def __init__(self, global_root: Optional[str] = None,
                 campaign_root: Optional[str] = None,
                 categorize: Optional[Categorizer] = None):
        self._lock = threading.Lock()
        self._manifests = {"global": Manifest(global_root or _default_global_root()),
                           "campaign": Manifest(campaign_root) if campaign_root else None}
        self.categorize = categorize

    @property
    def global_root(self) -> str:
        return self._manifests["global"].root

    @property
    def global_manifest(self) -> Manifest:
        """Where generated and imported images are registered."""
        return self._manifests["global"]

    def set_campaign_root(self, root: Optional[str]) -> None:
        with self._lock:
            self._manifests["campaign"] = Manifest(root) if root else None

    def _scopes(self):
        for scope in self.SCOPES:
            m = self._manifests[scope]
            if m is not None:
                yield scope, m

    def file_for(self, scope: str, rel: str) -> Optional[str]:
        """Absolute path of an image inside a scope's root, or None."""
        m = self._manifests.get(scope)
        rel = _safe_rel(rel)
        if m is None or rel is None:
            return None
        path = os.path.join(m.root, rel)
        return path if os.path.isfile(path) else None

    def find(self, key: str) -> Optional[dict]:
        """{scope, entry, path} for the first scope holding `key` with an existing file."""
        for scope, m in self._scopes():
            entry = m.entry(key)
            if entry and self.file_for(scope, entry["file"]):
                return {"scope": scope, "entry": entry,
                        "path": self.file_for(scope, entry["file"])}
        return None

    def _key_for(self, kind: str, name: str) -> str:
        key = make_key(kind, name)
        if key and self.find(key):
            return key
        for _, m in self._scopes():
            target = m.alias_target(kind, name)
            if target:
                return target
        return key

    def resolve(self, kind: str, name: str, category: Optional[str] = None) -> dict:
        """Everything the UI needs for one name:

            {"name", "key", "url" (None if no image), "category", "placeholder"}
        """
        key = self._key_for(kind, name)
        found = self.find(key) if key else None
        url = None
        if found:
            rel = _safe_rel(found["entry"]["file"]).replace(os.sep, "/")
            version = int(os.path.getmtime(found["path"]))
            url = f"/assets/file/{found['scope']}/{rel}?v={version}"
        cat = (found["entry"].get("category") if found else None) or category
        if cat not in CATEGORIES and self.categorize and not found:
            try:
                cat = self.categorize(kind, name)
            except Exception:
                cat = None
        if cat not in CATEGORIES:
            cat = DEFAULT_CATEGORY[kind]
        return {"name": name, "key": key, "url": url, "category": cat,
                "placeholder": placeholder_url(cat)}

    def has_image(self, kind: str, name: str) -> bool:
        key = self._key_for(kind, name)
        return bool(key and self.find(key))


def placeholder_url(category: str) -> str:
    return f"/assets/placeholder/{category if category in CATEGORIES else 'gear'}.svg"


def placeholder_file(category: str) -> "tuple[str, str]":
    """(directory, filename) of a bundled placeholder; unknown → gear."""
    cat = category if category in CATEGORIES else "gear"
    return PLACEHOLDER_DIR, f"{cat}.svg"
