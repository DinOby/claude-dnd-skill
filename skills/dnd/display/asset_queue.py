"""asset_queue.py — wait-list of content that needs an image.

The display never generates images during play. When content without an
image shows up (an inventory item, later tokens and maps), the server adds
it here; `scripts/assets.py generate` works through the list afterwards.

<data-root>/assets/pending-assets.json:
    {"version": 1,
     "queue": [{"key": "item:flammenschwert-der-asche", "kind": "item",
                "name": "Flammenschwert der Asche", "category": "weapon",
                "campaign": "ashveil", "hint": null, "prompt": null,
                "status": "pending", "attempts": 0, "last_error": null,
                "first_seen": "2026-09-27T12:00:00Z", "updated": "…"}]}

status: pending | done | failed | skipped. Keys are unique; adding a key that
is already listed is a no-op, whatever its status (a skipped item stays
skipped).

Both the server and the CLI write this file, so every read-modify-write runs
under a lock file next to it.
"""

import json
import os
import tempfile
import threading
import time
from datetime import datetime, timezone
from typing import Iterable, Optional

STATUSES = ("pending", "done", "failed", "skipped")
_LOCK_STALE_S = 30.0
_LOCK_TIMEOUT_S = 10.0


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class _FileLock:
    """Cross-process lock via O_EXCL; a lock older than _LOCK_STALE_S is broken."""

    def __init__(self, path: str):
        self.path = path

    def __enter__(self):
        deadline = time.monotonic() + _LOCK_TIMEOUT_S
        while True:
            try:
                fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.write(fd, str(os.getpid()).encode())
                os.close(fd)
                return self
            except FileExistsError:
                try:
                    if time.time() - os.path.getmtime(self.path) > _LOCK_STALE_S:
                        os.remove(self.path)
                        continue
                except OSError:
                    continue
                if time.monotonic() > deadline:
                    raise TimeoutError(f"could not lock {self.path}")
                time.sleep(0.05)

    def __exit__(self, *exc):
        try:
            os.remove(self.path)
        except OSError:
            pass


class PendingQueue:
    def __init__(self, path: str):
        self.path = path
        self._thread_lock = threading.Lock()
        self._known: "Optional[set[str]]" = None   # keys on file (cheap dedupe for the hot path)

    # ── I/O ──
    def _read(self) -> dict:
        try:
            with open(self.path, encoding="utf-8-sig") as f:
                data = json.load(f)
            if isinstance(data, dict) and isinstance(data.get("queue"), list):
                data["queue"] = [e for e in data["queue"] if isinstance(e, dict) and e.get("key")]
                return data
        except (OSError, ValueError):
            pass
        return {"version": 1, "queue": []}

    def _write(self, data: dict) -> None:
        directory = os.path.dirname(self.path) or "."
        os.makedirs(directory, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=directory, prefix=".pending.", suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.write("\n")
        os.replace(tmp, self.path)

    def _locked(self):
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        return _FileLock(self.path + ".lock")

    # ── API ──
    def entries(self, status: Optional[str] = None) -> "list[dict]":
        queue = self._read()["queue"]
        return [e for e in queue if status is None or e.get("status") == status]

    def add_many(self, items: Iterable[dict]) -> "list[str]":
        """Append entries whose key is not listed yet. Returns the added keys.

        Each item needs key, kind, name; category, campaign, hint are optional.
        """
        items = [i for i in items if i.get("key")]
        with self._thread_lock:
            if self._known is not None and all(i["key"] in self._known for i in items):
                return []
            with self._locked():
                data = self._read()
                known = {e["key"] for e in data["queue"]}
                added = []
                for item in items:
                    if item["key"] in known:
                        continue
                    known.add(item["key"])
                    added.append(item["key"])
                    data["queue"].append({
                        "key": item["key"], "kind": item["kind"], "name": item["name"],
                        "category": item.get("category"), "campaign": item.get("campaign"),
                        "hint": item.get("hint"), "prompt": None, "status": "pending",
                        "attempts": 0, "last_error": None,
                        "first_seen": _now(), "updated": _now(),
                    })
                if added:
                    self._write(data)
                self._known = known
                return added

    def update(self, key: str, **fields) -> bool:
        """Change fields of one entry (status, prompt, attempts, last_error, …)."""
        if "status" in fields and fields["status"] not in STATUSES:
            raise ValueError(f"unknown status {fields['status']!r}")
        with self._thread_lock, self._locked():
            data = self._read()
            for entry in data["queue"]:
                if entry["key"] == key:
                    entry.update(fields)
                    entry["updated"] = _now()
                    self._write(data)
                    return True
            return False

    def forget_cache(self) -> None:
        """Drop the in-memory key set (after another process edited the file)."""
        with self._thread_lock:
            self._known = None
