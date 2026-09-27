#!/usr/bin/env python3
"""assets.py — images for items, tokens and maps, generated AFTER play.

During play the display only puts content without an image on a wait-list
(<data-root>/assets/pending-assets.json). This script works through it.

Usage:
    python3 assets.py status                     # counts + pending entries
    python3 assets.py list [--status failed]     # entries (all statuses by default)
    python3 assets.py providers                  # configured image services + readiness
    python3 assets.py prompt KEY "TEXT"          # set the visual description for one entry
    python3 assets.py generate [--limit N] [--provider NAME] [--key KEY ...] [--dry-run]
    python3 assets.py add KEY FILE [--category C] [--name "Display Name"]
    python3 assets.py skip KEY [KEY ...]         # never generate these
    python3 assets.py retry [KEY ...]            # failed/skipped → pending (all failed if no KEY)

KEY is "item:Flammenschwert der Asche", "token:Wirtin Hilde", "map:…" or a
plain item name; it is normalised the same way the display does.

Image services are configured in display/config/image-provider.json
(override: <data-root>/config/image-provider.json).
"""

import argparse
import json
import os
import ssl
import sys
import urllib.request

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

from asset_pipeline import Pipeline, parse_key            # noqa: E402
from asset_queue import STATUSES, PendingQueue            # noqa: E402
from asset_store import CATEGORIES, AssetStore            # noqa: E402


def _pipeline() -> Pipeline:
    store = AssetStore()
    return Pipeline(store, PendingQueue(os.path.join(store.global_root, "pending-assets.json")))


def _notify_display() -> None:
    """Ask a running display to reload images; silent when it is not running."""
    try:
        from runtime_paths import rt
        scheme_file = os.path.join(_DISPLAY, ".scheme")
        scheme = open(scheme_file, encoding="utf-8").read().strip() if os.path.exists(scheme_file) else "http"
        try:
            token = open(rt(".token"), encoding="utf-8").read().strip()
        except OSError:
            token = ""
        req = urllib.request.Request(f"{scheme}://localhost:5001/assets/changed", data=b"", method="POST",
                                     headers={"X-DND-Token": token} if token else {})
        ctx = None
        if scheme == "https":
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
        urllib.request.urlopen(req, timeout=2, context=ctx)
    except Exception:
        pass


def _row(e: dict) -> str:
    extra = f"  prompt: {e['prompt'][:60]}…" if e.get("prompt") else ""
    err = f"  error: {e['last_error'][:80]}" if e.get("last_error") else ""
    camp = f" [{e['campaign']}]" if e.get("campaign") else ""
    return f"  {e['status']:<8} {e.get('category') or '-':<9} {e['key']}{camp}{extra}{err}"


def cmd_status(p: Pipeline, args) -> int:
    entries = p.queue.entries()
    counts = {s: sum(1 for e in entries if e.get("status") == s) for s in STATUSES}
    print("Wait-list: " + ", ".join(f"{n} {s}" for s, n in counts.items()))
    pending = [e for e in entries if e.get("status") == "pending"]
    if pending:
        print("Pending:")
        for e in pending:
            print(_row(e))
    print(f"Provider: {p.provider_name('item')} (items), {p.provider_name('token')} (tokens), "
          f"{p.provider_name('map')} (maps)")
    return 0


def cmd_list(p: Pipeline, args) -> int:
    entries = p.queue.entries(args.status)
    if args.json:
        print(json.dumps(entries, ensure_ascii=False, indent=2))
    else:
        for e in entries:
            print(_row(e))
        if not entries:
            print("(empty)")
    return 0


def cmd_providers(p: Pipeline, args) -> int:
    for st in p.providers_status():
        mark = "ready" if st["available"] else "NOT READY"
        print(f"  {st['name']:<10} {mark:<10} {st['detail']}")
    return 0


def cmd_prompt(p: Pipeline, args) -> int:
    key = parse_key(args.key)
    if not p.queue.update(key, prompt=args.text.strip() or None):
        print(f"not on the wait-list: {key}", file=sys.stderr)
        return 1
    print(f"prompt set for {key}")
    return 0


def cmd_generate(p: Pipeline, args) -> int:
    keys = [parse_key(k) for k in args.key] if args.key else None
    summary = p.generate(limit=args.limit, provider=args.provider, keys=keys, dry_run=args.dry_run)
    if args.dry_run:
        for item in summary["planned"]:
            print(f"- {item['key']} via {item['provider']}\n    {item['prompt']}")
        print(f"{len(summary['planned'])} image(s) would be generated.")
        return 0
    print(f"Done: {len(summary['done'])}, failed: {len(summary['failed'])}, "
          f"blocked (provider not ready): {len(summary['blocked'])}")
    if summary["done"]:
        _notify_display()
    return 1 if summary["blocked"] and not summary["done"] else 0


def cmd_add(p: Pipeline, args) -> int:
    key = parse_key(args.key)
    try:
        rel = p.add_file(key, args.file, name=args.name, category=args.category)
    except (ValueError, OSError) as e:
        print(f"add failed: {e}", file=sys.stderr)
        return 1
    print(f"{key} → {rel}")
    _notify_display()
    return 0


def cmd_skip(p: Pipeline, args) -> int:
    rc = 0
    for k in args.key:
        key = parse_key(k)
        if p.queue.update(key, status="skipped"):
            print(f"skipped {key}")
        else:
            print(f"not on the wait-list: {key}", file=sys.stderr)
            rc = 1
    return rc


def cmd_retry(p: Pipeline, args) -> int:
    keys = [parse_key(k) for k in args.key] if args.key else \
        [e["key"] for e in p.queue.entries("failed")]
    for key in keys:
        if p.queue.update(key, status="pending", attempts=0, last_error=None):
            print(f"pending again: {key}")
    if not keys:
        print("nothing to retry")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="assets", description="Images for items, tokens and maps.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status", help="counts and pending entries")
    ls = sub.add_parser("list", help="list wait-list entries")
    ls.add_argument("--status", choices=STATUSES)
    ls.add_argument("--json", action="store_true")
    sub.add_parser("providers", help="configured image services and whether they are ready")
    pr = sub.add_parser("prompt", help="set the visual description for one entry")
    pr.add_argument("key")
    pr.add_argument("text")
    gen = sub.add_parser("generate", help="generate images for pending entries")
    gen.add_argument("--limit", type=int)
    gen.add_argument("--provider")
    gen.add_argument("--key", action="append")
    gen.add_argument("--dry-run", action="store_true")
    add = sub.add_parser("add", help="use an existing image file")
    add.add_argument("key")
    add.add_argument("file")
    add.add_argument("--category", choices=CATEGORIES)
    add.add_argument("--name")
    sk = sub.add_parser("skip", help="never generate these")
    sk.add_argument("key", nargs="+")
    rt_ = sub.add_parser("retry", help="put failed/skipped entries back to pending")
    rt_.add_argument("key", nargs="*")
    args = ap.parse_args(argv)

    handlers = {"status": cmd_status, "list": cmd_list, "providers": cmd_providers,
                "prompt": cmd_prompt, "generate": cmd_generate, "add": cmd_add,
                "skip": cmd_skip, "retry": cmd_retry}
    return handlers[args.cmd](_pipeline(), args)


if __name__ == "__main__":
    sys.exit(main())
