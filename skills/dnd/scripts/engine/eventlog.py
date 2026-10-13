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


class LogError(Exception):
    """The log could not be written (lock busy, invalid event, I/O)."""


class Skipped(NamedTuple):
    line: int
    reason: str


def log_path(campaign_dir) -> pathlib.Path:
    return pathlib.Path(campaign_dir) / "game" / LOG_NAME


if os.name == "nt":
    import msvcrt

    def _try_lock(fd: int) -> bool:
        try:
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            return True
        except OSError:
            return False

    def _unlock(fd: int) -> None:
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
else:
    import fcntl

    def _try_lock(fd: int) -> bool:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except OSError:
            return False

    def _unlock(fd: int) -> None:
        fcntl.flock(fd, fcntl.LOCK_UN)


class FileLock:
    """Cross-process lock held by the operating system on <path>.

    The lock is not the file's existence but an OS lock on it (LockFileEx on
    Windows, flock elsewhere). The OS drops it when the holder exits or crashes,
    so a lock file left behind after a crash locks nothing — there is no stale
    lock to guess about and nothing to delete, and two writers can never both
    hold it. A writer that cannot get it within `timeout` gives up (LogError);
    in shadow mode that is one warning and play goes on.
    """

    def __init__(self, path, timeout: Optional[float] = None):
        self.path = str(path)
        self.timeout = LOCK_TIMEOUT_S if timeout is None else timeout
        self._fd: Optional[int] = None

    def __enter__(self):
        try:
            fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
        except OSError as exc:
            raise LogError(f"Sperrdatei nicht anlegbar: {exc}") from exc
        deadline = time.monotonic() + self.timeout
        while not _try_lock(fd):
            if time.monotonic() > deadline:
                os.close(fd)
                raise LogError("Logbuch ist gerade von einem anderen Schreiber gesperrt")
            time.sleep(0.02)
        self._fd = fd
        return self

    def __exit__(self, *exc):
        fd, self._fd = self._fd, None
        if fd is None:
            return
        try:
            _unlock(fd)
        except OSError:
            pass
        os.close(fd)


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
