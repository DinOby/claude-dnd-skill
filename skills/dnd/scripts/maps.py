#!/usr/bin/env python3
"""maps.py — the battle-map library: list, reset, restore.

Location layouts live in <data-root>/maps/library/<id>.json and are reused by
every campaign; which tokens stand on a map is stored per campaign in
<campaign>/maps/<id>.json. Resetting a layout makes the next
`push_stats.py --map-new TEMPLATE --map-id ID` build it fresh from the
template.

Nothing is ever deleted: reset moves the layout — and the location's own
image `map:<id>`, unless --keep-image — into
<data-root>/maps/archive/<id>/<timestamp>/. Generic seed images
(`map:taverne`, …) are shared and never touched. Token placement in the
campaigns stays; tokens that still fit reappear on the new layout.

Usage:
    python3 maps.py list [--tag tavern] [--archived] [--json]
    python3 maps.py reset ID [ID ...] [--keep-image] [--dry-run]
    python3 maps.py reset --tag tavern | --all [--keep-image] [--dry-run]
    python3 maps.py restore ID [--version TIMESTAMP]
"""

import argparse
import json
import os
import shutil
import sys
from datetime import datetime, timezone

try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

_HERE = os.path.dirname(os.path.abspath(__file__))
_DISPLAY = os.path.join(_HERE, os.pardir, "display")
for _p in (_HERE, _DISPLAY):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from asset_store import AssetStore                      # noqa: E402
from grid_map import slug                               # noqa: E402
from paths import campaigns_dir, maps_library_dir       # noqa: E402


class Library:
    """Library, archive and images of the battle maps (paths injectable for tests)."""

    def __init__(self, library_dir=None, campaigns=None, store=None):
        self.library_dir = str(library_dir or maps_library_dir())
        self.archive_dir = os.path.join(os.path.dirname(self.library_dir), "archive")
        self.campaigns = str(campaigns or campaigns_dir())
        self.store = store or AssetStore()

    # ── reading ──
    def _read(self, path):
        try:
            with open(path, encoding="utf-8-sig") as f:
                data = json.load(f)
            return data if isinstance(data, dict) else None
        except (OSError, ValueError):
            return None

    def ids(self):
        try:
            return sorted(f[:-5] for f in os.listdir(self.library_dir) if f.endswith(".json"))
        except OSError:
            return []

    def layout(self, map_id):
        return self._read(os.path.join(self.library_dir, f"{map_id}.json"))

    def campaigns_using(self, map_id):
        try:
            names = sorted(os.listdir(self.campaigns))
        except OSError:
            return []
        return [c for c in names if os.path.isfile(os.path.join(self.campaigns, c, "maps", f"{map_id}.json"))]

    def image(self, map_id, layout):
        """('own', entry) for the location's own image, ('generic'|'none', …) otherwise."""
        own = self.store.global_manifest.entry(f"map:{map_id}")
        if own and not own.get("generic"):
            return "own", own
        bg = ((layout or {}).get("background") or {}).get("asset")
        if bg and self.store.find(bg):
            return "generic", None
        return "none", None

    def rows(self, tag=None):
        out = []
        for mid in self.ids():
            lay = self.layout(mid) or {}
            if tag and tag not in (lay.get("tags") or []):
                continue
            kind, _ = self.image(mid, lay)
            out.append({"id": mid, "name": lay.get("name") or mid, "template": lay.get("template"),
                        "tags": lay.get("tags") or [], "size": f"{lay.get('cols')}×{lay.get('rows')}",
                        "image": kind, "campaigns": self.campaigns_using(mid)})
        return out

    def archived(self, map_id=None):
        out = []
        try:
            ids = [map_id] if map_id else sorted(os.listdir(self.archive_dir))
        except OSError:
            return out
        for mid in ids:
            d = os.path.join(self.archive_dir, mid)
            try:
                versions = sorted(os.listdir(d))
            except OSError:
                continue
            for v in versions:
                meta = self._read(os.path.join(d, v, "meta.json")) or {}
                out.append({"id": mid, "version": v, "image": bool(meta.get("image")),
                            "archived": meta.get("archived")})
        return out

    # ── changing ──
    def reset(self, map_id, keep_image=False):
        """Archive one layout (and its own image). Returns the archive folder."""
        lay_path = os.path.join(self.library_dir, f"{map_id}.json")
        if not os.path.isfile(lay_path):
            raise FileNotFoundError(f"no map '{map_id}' in the library")
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        dest = os.path.join(self.archive_dir, map_id, stamp)
        os.makedirs(dest)
        meta = {"id": map_id, "archived": stamp, "image": None}
        kind, entry = self.image(map_id, self.layout(map_id))
        if kind == "own" and not keep_image:
            found = self.store.find(f"map:{map_id}")
            if found and found["scope"] == "global":
                ext = os.path.splitext(found["path"])[1]
                shutil.move(found["path"], os.path.join(dest, "image" + ext))
                meta["image"] = {"key": f"map:{map_id}", "entry": entry, "file": "image" + ext}
            self.store.global_manifest.remove_entry(f"map:{map_id}")
        shutil.move(lay_path, os.path.join(dest, "layout.json"))
        with open(os.path.join(dest, "meta.json"), "w", encoding="utf-8") as f:
            json.dump(meta, f, ensure_ascii=False, indent=2)
        return dest

    def restore(self, map_id, version=None):
        """Put an archived version back (the current layout, if any, is archived first)."""
        versions = [a["version"] for a in self.archived(map_id)]
        if not versions:
            raise FileNotFoundError(f"nothing archived for '{map_id}'")
        version = version or versions[-1]
        if version not in versions:
            raise FileNotFoundError(f"no archived version {version} of '{map_id}' (have: {', '.join(versions)})")
        src = os.path.join(self.archive_dir, map_id, version)
        meta = self._read(os.path.join(src, "meta.json")) or {}
        if os.path.isfile(os.path.join(self.library_dir, f"{map_id}.json")):
            self.reset(map_id, keep_image=not meta.get("image"))
        os.makedirs(self.library_dir, exist_ok=True)
        shutil.copyfile(os.path.join(src, "layout.json"), os.path.join(self.library_dir, f"{map_id}.json"))
        img = meta.get("image")
        if img and os.path.isfile(os.path.join(src, img["file"])):
            rel = (img["entry"] or {}).get("file") or f"maps/{map_id}{os.path.splitext(img['file'])[1]}"
            target = os.path.join(self.store.global_root, rel)
            os.makedirs(os.path.dirname(target), exist_ok=True)
            shutil.copyfile(os.path.join(src, img["file"]), target)
            self.store.global_manifest.set_entry(img["key"], img["entry"])
        return version


def _select(lib, args):
    if args.all:
        return lib.ids()
    if args.tag:
        return [r["id"] for r in lib.rows(args.tag)]
    return [slug(i) for i in args.ids]


def cmd_list(lib, args):
    if args.archived:
        rows = lib.archived()
        if args.json:
            print(json.dumps(rows, ensure_ascii=False, indent=2))
            return 0
        for r in rows:
            print(f"  {r['id']:<28} {r['version']}" + ("  (+ image)" if r["image"] else ""))
        if not rows:
            print("(archive empty)")
        return 0
    rows = lib.rows(args.tag)
    if args.json:
        print(json.dumps(rows, ensure_ascii=False, indent=2))
        return 0
    for r in rows:
        camps = ", ".join(r["campaigns"]) or "-"
        print(f"  {r['id']:<28} {r['size']:<6} {r['template'] or '-':<17} image {r['image']:<8}"
              f" tags {','.join(r['tags']) or '-'}  campaigns: {camps}")
    if not rows:
        print("(library empty)" if not args.tag else f"(no map tagged '{args.tag}')")
    return 0


def cmd_reset(lib, args):
    ids = _select(lib, args)
    if not ids:
        print("nothing to reset (give ids, --tag or --all)", file=sys.stderr)
        return 1
    rc = 0
    for mid in ids:
        if args.dry_run:
            kind, _ = lib.image(mid, lib.layout(mid))
            if lib.layout(mid) is None:
                print(f"  {mid}: not in the library", file=sys.stderr)
                rc = 1
                continue
            what = "layout" + (" + own image" if kind == "own" and not args.keep_image else "")
            print(f"  would archive {mid}: {what}")
            continue
        try:
            dest = lib.reset(mid, keep_image=args.keep_image)
        except FileNotFoundError as e:
            print(f"  {mid}: {e}", file=sys.stderr)
            rc = 1
            continue
        print(f"  {mid} → archived ({os.path.basename(dest)})")
    if not args.dry_run and rc == 0:
        print("Next `--map-new TEMPLATE --map-id ID` builds these fresh; `maps.py restore ID` undoes it.")
    return rc


def cmd_restore(lib, args):
    try:
        version = lib.restore(slug(args.id), args.version)
    except FileNotFoundError as e:
        print(f"restore failed: {e}", file=sys.stderr)
        return 1
    print(f"{slug(args.id)} restored from {version}. Show it with `push_stats.py --map-show {slug(args.id)}`.")
    return 0


def main(argv=None, lib=None):
    ap = argparse.ArgumentParser(prog="maps", description="Battle-map library: list, reset, restore.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    ls = sub.add_parser("list", help="maps in the library (or the archive)")
    ls.add_argument("--tag")
    ls.add_argument("--archived", action="store_true")
    ls.add_argument("--json", action="store_true")
    rs = sub.add_parser("reset", help="archive layouts so they are rebuilt from their template")
    rs.add_argument("ids", nargs="*")
    rs.add_argument("--tag")
    rs.add_argument("--all", action="store_true")
    rs.add_argument("--keep-image", action="store_true", help="leave the location's own image in place")
    rs.add_argument("--dry-run", action="store_true")
    rt_ = sub.add_parser("restore", help="bring an archived layout back")
    rt_.add_argument("id")
    rt_.add_argument("--version", help="archive timestamp (default: the latest)")
    args = ap.parse_args(argv)
    lib = lib or Library()
    return {"list": cmd_list, "reset": cmd_reset, "restore": cmd_restore}[args.cmd](lib, args)


if __name__ == "__main__":
    sys.exit(main())
