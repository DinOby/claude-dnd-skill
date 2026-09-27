"""scene_state.py — where the party is: at a place, travelling, or in a travel event.

    {"mode": "travel_event",            # stationary | travel | travel_event
     "location": null,
     "map_id": "event-wolfsrudel-d2",
     "travel": {"from": "Ashveil", "to": "Dornfeld", "via": "Königsstraße",
                "day": 2, "days_total": 4, "terrain": "forest"},
     "event":  {"id": "wolfsrudel-d2", "title": "Wolfsrudel am Waldrand",
                "template": "forest-clearing", "tokens": [{"name": "Wolf", "kind": "enemy"}, …],
                "started": "2026-09-27T12:00:00Z", "map_is_temporary": true},
     "rev": 5, "updated": "…"}

Allowed transitions (anything else raises SceneError with a hint):

    stationary ──travel-start──▶ travel ──event-start──▶ travel_event
         ▲                        │  ▲                        │
         │                        │  └──────event-end─────────┘
         └──────travel-end────────┘
    stationary ──scene-set──▶ stationary   (change of place without a journey)
    travel ──travel-day──▶ travel          (one travel day passes)

Combat is not a state; it happens on the map of the current scene.

The state is campaign data (<campaign>/scene-state.json), written by
scripts/travel.py even when the display is not running; the display reads it
and brings the map in line (see dnd-display-app.py). Both sides write under
a lock file.
"""

import copy
import json
import os
import re
import sys
import tempfile
from datetime import datetime, timezone
from typing import Optional

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)
from asset_queue import FileLock

MODES = ("stationary", "travel", "travel_event")
ACTIONS = ("scene-set", "travel-start", "travel-day", "event-start", "event-end", "travel-end")
_FROM = {"scene-set": ("stationary",), "travel-start": ("stationary",), "travel-day": ("travel",),
         "event-start": ("travel",), "event-end": ("travel_event",), "travel-end": ("travel",)}
_PARAMS = {"scene-set": ("location", "map_id"), "travel-start": ("to", "days", "terrain", "via", "from"),
           "travel-day": (), "event-start": ("id", "title", "template", "tokens", "focus"),
           "event-end": (), "travel-end": ("location", "map_id")}
_HINTS = {
    ("travel", "scene-set"): "the party is travelling — arrive first (travel.py arrive)",
    ("travel_event", "scene-set"): "a travel event is running — end it (travel.py event-end), then arrive",
    ("travel", "travel-start"): "already travelling — arrive first",
    ("travel_event", "travel-start"): "a travel event is running — end it first",
    ("travel_event", "travel-day"): "a travel event is running — end it (travel.py event-end) before the next day",
    ("stationary", "travel-day"): "not travelling — start a journey (travel.py start --to …)",
    ("stationary", "event-start"): "travel events only happen on a journey",
    ("travel_event", "event-start"): "an event is already running — end it first",
    ("stationary", "event-end"): "no travel event is running",
    ("travel", "event-end"): "no travel event is running",
    ("stationary", "travel-end"): "not travelling",
    ("travel_event", "travel-end"): "a travel event is running — end it (travel.py event-end) first",
}


class SceneError(ValueError):
    pass


def default_state() -> dict:
    return {"mode": "stationary", "location": None, "map_id": None,
            "travel": None, "event": None, "rev": 0, "updated": None}


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _text(v) -> Optional[str]:
    return v.strip() if isinstance(v, str) and v.strip() else None


def _slug(text: str) -> str:
    from grid_map import slug
    return slug(text)


def transition(state: dict, action: str, **p) -> dict:
    """New state after `action` (state is not modified). Raises SceneError."""
    if action not in ACTIONS:
        raise SceneError(f"unknown action '{action}' (use {', '.join(ACTIONS)})")
    mode = state.get("mode", "stationary")
    if mode not in _FROM[action]:
        raise SceneError(f"cannot {action} while {mode}: "
                         + _HINTS.get((mode, action), f"allowed from {', '.join(_FROM[action])}"))
    unknown = sorted(set(p) - set(_PARAMS[action]))
    if unknown:
        raise SceneError(f"{action}: unknown parameter(s) {', '.join(unknown)} "
                         f"(allowed: {', '.join(_PARAMS[action]) or 'none'})")
    new = copy.deepcopy(state)

    if action == "scene-set":
        loc = _text(p.get("location"))
        if not loc and "map_id" not in p:
            raise SceneError("scene-set needs a location and/or a map")
        if loc:
            new["location"] = loc
        if "map_id" in p:
            new["map_id"] = _text(p.get("map_id"))
    elif action == "travel-start":
        to = _text(p.get("to"))
        days = p.get("days")
        if not to:
            raise SceneError("travel-start needs a destination (--to)")
        if not (isinstance(days, int) and not isinstance(days, bool) and 1 <= days <= 365):
            raise SceneError("travel-start needs --days between 1 and 365")
        new.update(mode="travel", map_id=None, event=None, travel={
            "from": _text(p.get("from")) or state.get("location"), "to": to, "via": _text(p.get("via")),
            "day": 0, "days_total": days, "terrain": _text(p.get("terrain")) or "road"})
        new["location"] = None
    elif action == "travel-day":
        new["travel"]["day"] += 1
    elif action == "event-start":
        title = _text(p.get("title"))
        if not title:
            raise SceneError("event-start needs a title")
        eid = _slug(p.get("id") or title) or "ereignis"
        eid = f"{eid}-d{new['travel']['day']}"
        new.update(mode="travel_event", map_id=f"event-{eid}", event={
            "id": eid, "title": title, "template": _text(p.get("template")),
            "tokens": list(p.get("tokens") or []), "focus": _text(p.get("focus")),
            "started": _now(), "map_is_temporary": True})
    elif action == "event-end":
        new.update(mode="travel", map_id=None, event=None)
    elif action == "travel-end":
        loc = _text(p.get("location")) or state["travel"]["to"]
        new.update(mode="stationary", location=loc, map_id=_text(p.get("map_id")), travel=None, event=None)

    new["rev"] = int(state.get("rev") or 0) + 1
    new["updated"] = _now()
    return new


def validate(state) -> dict:
    """Stored state as a well-formed dict; a broken file means 'stationary, nowhere'."""
    if not isinstance(state, dict) or state.get("mode") not in MODES:
        return default_state()
    out = default_state()
    out.update({k: state.get(k) for k in out if k in state})
    if out["mode"] != "stationary" and not isinstance(out.get("travel"), dict):
        return default_state()
    if out["mode"] == "travel_event" and not isinstance(out.get("event"), dict):
        out.update(mode="travel", event=None, map_id=None)
    return out


class SceneStore:
    """scene-state.json of one campaign (or of the runtime dir without one)."""

    def __init__(self, path: str):
        self.path = path

    def get(self) -> dict:
        try:
            with open(self.path, encoding="utf-8-sig") as f:
                return validate(json.load(f))
        except (OSError, ValueError):
            return default_state()

    def _write(self, state: dict) -> None:
        directory = os.path.dirname(self.path) or "."
        os.makedirs(directory, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=directory, prefix=".scene.", suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
            f.write("\n")
        os.replace(tmp, self.path)

    def apply(self, action: str, **params) -> dict:
        """Run one transition under the lock and save it; returns the new state."""
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        with FileLock(self.path + ".lock"):
            new = transition(self.get(), action, **params)
            self._write(new)
            return new

    def update(self, **fields) -> dict:
        """Change fields without a transition (e.g. the map shown at the current place)."""
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        with FileLock(self.path + ".lock"):
            state = self.get()
            state.update(fields)
            state["rev"] = int(state.get("rev") or 0) + 1
            state["updated"] = _now()
            self._write(state)
            return state


# ── state.md flag ─────────────────────────────────────────────────────────────

DEFAULT_EVENT_CHANCE = 15
_CHANCE_RE = re.compile(r"^\s*[-*]?\s*travel_event_chance:\s*(\d{1,3})\s*%?\s*$", re.IGNORECASE | re.MULTILINE)


def event_chance(state_md_text: str) -> int:
    """Percent per travel day from `travel_event_chance: N` in state.md (0–100, default 15)."""
    m = _CHANCE_RE.search(state_md_text or "")
    return max(0, min(100, int(m.group(1)))) if m else DEFAULT_EVENT_CHANCE
