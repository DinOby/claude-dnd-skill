"""eventlog.py — the append-only event log <campaign>/game/events.jsonl.

One JSON object per line (protocol/v1/event.schema.json). Lines are only ever
appended, under a lock file, so two writers can never interleave or hand out
the same sequence number. Reading skips a broken line and reports it instead of
failing: one bad line must not make the rest of the log unusable.
"""

import json
import os
import pathlib
import time
from datetime import datetime, timezone
from typing import Iterator, NamedTuple, Optional

from engine import PROTOCOL_VERSION
from engine.protocol import check_event
from engine.textguard import clean, clean_data

LOG_NAME = "events.jsonl"
LOCK_TIMEOUT_S = 2.0
LOCK_STALE_S = 10.0


class LogError(Exception):
    """The log could not be written (lock busy, invalid event, I/O)."""


class Skipped(NamedTuple):
    line: int
    reason: str


def log_path(campaign_dir) -> pathlib.Path:
    return pathlib.Path(campaign_dir) / "game" / LOG_NAME


class FileLock:
    """Cross-process lock via O_EXCL; a lock older than LOCK_STALE_S is broken."""

    def __init__(self, path, timeout: float = LOCK_TIMEOUT_S):
        self.path = str(path)
        self.timeout = timeout

    def __enter__(self):
        deadline = time.monotonic() + self.timeout
        while True:
            try:
                fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.write(fd, str(os.getpid()).encode())
                os.close(fd)
                return self
            except FileExistsError:
                try:
                    if time.time() - os.path.getmtime(self.path) > LOCK_STALE_S:
                        os.remove(self.path)
                        continue
                except OSError:
                    continue
                if time.monotonic() > deadline:
                    raise LogError(f"Logbuch ist gesperrt ({self.path})")
                time.sleep(0.05)

    def __exit__(self, *exc):
        try:
            os.remove(self.path)
        except OSError:
            pass


def read(campaign_dir) -> "tuple[list[dict], list[Skipped]]":
    """All valid events in order, plus the lines that were skipped and why."""
    events: "list[dict]" = []
    skipped: "list[Skipped]" = []
    for item in _iter_lines(log_path(campaign_dir)):
        if isinstance(item, Skipped):
            skipped.append(item)
        else:
            events.append(item)
    return events, skipped


def _iter_lines(path: pathlib.Path) -> "Iterator[dict | Skipped]":
    try:
        f = open(path, encoding="utf-8", errors="replace")
    except FileNotFoundError:
        return
    with f:
        for n, raw in enumerate(f, start=1):
            if not raw.strip():
                continue
            try:
                event = json.loads(raw)
            except json.JSONDecodeError as exc:
                yield Skipped(n, f"kein gültiges JSON ({exc.msg})")
                continue
            problems = check_event(event)
            if problems:
                yield Skipped(n, problems[0])
                continue
            yield event


def _last_seq(path: pathlib.Path) -> int:
    """Highest sequence number among the valid lines (0 for an empty log)."""
    last = 0
    for item in _iter_lines(path):
        if not isinstance(item, Skipped):
            last = max(last, item["seq"])
    return last


def append(campaign_dir, etype: str, data: dict, *, by: str = "dm", src: str = "game",
           now: Optional[datetime] = None) -> dict:
    """Append one event and return it. Raises LogError if it cannot be written."""
    path = log_path(campaign_dir)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise LogError(f"Ordner nicht anlegbar: {exc}") from exc
    event = {
        "v": PROTOCOL_VERSION,
        "seq": 0,
        "t": (now or datetime.now(timezone.utc)).isoformat(timespec="seconds"),
        "by": clean(by),
        "src": src,
        "type": etype,
        "data": clean_data(data or {}),
    }
    with FileLock(str(path) + ".lock"):
        event["seq"] = _last_seq(path) + 1
        problems = check_event(event)
        if problems:
            raise LogError("; ".join(problems))
        try:
            with open(path, "a", encoding="utf-8", newline="\n") as f:
                f.write(json.dumps(event, ensure_ascii=False) + "\n")
        except OSError as exc:
            raise LogError(f"nicht schreibbar: {exc}") from exc
    return event
