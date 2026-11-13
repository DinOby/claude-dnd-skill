"""Keep the display's character sheets in step with the Markdown sheets (Sitzung 5).

Some changes reach a sheet without a display push: `game.py bogen` values the
push did not carry, or a sheet edited by hand (a subclass, a new feature). The
display then kept showing the old armour class, items or features until a full
rebuild. This watcher polls the active campaign's `characters/*.md` and, when a
file changed, sends the sheet-led values of that character to the display:

    armour class, level, inventory (placeholders like "keine" removed),
    currency, features

Hit points, conditions, slots and effects are NOT taken from here - they come
from the engine through game.py / tracker.py, and a stale sheet line must never
overwrite them on the display.

Only characters already on the display are updated (no sheet of an unrelated
campaign or an NPC file appears as a new player card). Read-only on disk.
"""
from __future__ import annotations

import pathlib
import threading
from typing import Callable, Optional

try:
    from engine import sheet as _sheet
except Exception:                                   # noqa: BLE001 - display works without it
    _sheet = None

_COINS = (("pp", "PM"), ("gp", "GM"), ("ep", "EM"), ("sp", "SM"), ("cp", "KM"))


def _coin_text(coins: Optional[dict]) -> Optional[str]:
    if not coins:
        return None
    return " ".join(f"{coins.get(k, 0)} {label}" for k, label in _COINS
                    if coins.get(k) or k in ("gp", "sp", "cp"))


def player_from_sheet(text: str) -> Optional[dict]:
    """The sheet-led part of a display player, or None when the sheet has no name."""
    if _sheet is None:
        return None
    fig = _sheet.parse(text)
    name = fig.get("name")
    if not name:
        return None
    p: dict = {"name": name}
    if fig.get("ac") is not None:
        p["ac"] = fig["ac"]
    if fig.get("level") is not None:
        p["level"] = fig["level"]
    part: dict = {
        "inventory": [i for items in (fig.get("inventory") or {}).values() for i in items
                      if not _sheet.is_placeholder(i)],
        "features": _sheet.features(text),
    }
    money = _coin_text(fig.get("currency"))
    if money:
        part["currency"] = money
    p["sheet"] = part
    return p


class SheetWatcher:
    """Poll `<campaign>/characters/*.md`; call `apply(player_update)` for each changed sheet.

    `campaign_dir()` returns the active campaign folder (or None). The first scan
    of a campaign also applies every sheet, so a display restart shows current
    sheet values instead of the last saved stats.
    """

    def __init__(self, campaign_dir: Callable[[], Optional[pathlib.Path]],
                 apply: Callable[[dict], bool], interval: float = 2.0):
        self._campaign_dir = campaign_dir
        self._apply = apply
        self._interval = interval
        self._seen: dict = {}
        self._campaign: Optional[pathlib.Path] = None
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def scan(self) -> "list[str]":
        """One poll. Returns the names that were sent to the display."""
        d = self._campaign_dir()
        if d is None:
            return []
        d = pathlib.Path(d)
        if d != self._campaign:
            self._campaign, self._seen = d, {}
        sent = []
        for path in sorted((d / "characters").glob("*.md")):
            try:
                st = path.stat()
            except OSError:
                continue
            stamp = (st.st_mtime_ns, st.st_size)
            if self._seen.get(path) == stamp:
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue                      # half-written or unreadable: try again next poll
            update = player_from_sheet(text)
            if update is None:
                self._seen[path] = stamp
                continue
            if self._apply(update):
                self._seen[path] = stamp
                sent.append(update["name"])
            # not applied (player not on the display yet): retry on the next poll
        return sent

    def _run(self) -> None:
        while not self._stop.wait(self._interval):
            try:
                self.scan()
            except Exception:                 # noqa: BLE001 - never kill the display
                pass

    def start(self) -> None:
        if self._thread is None:
            try:
                self.scan()
            except Exception:                 # noqa: BLE001
                pass
            self._thread = threading.Thread(target=self._run, name="sheet-watch", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()
