"""shadow.py — shadow mode: the old scripts also write what they changed into the log.

Active only while `engine: schatten` (or `an`) is set in state.md → ## Session
Flags; absent or `aus` means nothing happens. The old behaviour always runs
first and never depends on this: any failure here (no campaign, lock busy, bad
data, disk) becomes one warning on stderr and the script carries on.

A script that calls push_stats.py for its own display update (tracker.py,
xp.py) has already recorded the change; it passes child_env() so the
push_stats.py child does not record it a second time.
"""

import os
import pathlib
import re
import sys
from typing import Iterable, Optional

ENV_PARENT = "DND_SHADOW_PARENT"
MODES = ("aus", "schatten", "an")

_FLAG_RE = re.compile(r"^\s*[-*]?\s*\**engine\**\s*:\s*\**\s*(aus|schatten|an)\b", re.IGNORECASE | re.MULTILINE)


def warn(reason: str) -> None:
    try:
        print(f"⚠ Spielstand-Logbuch: nicht mitgeschrieben – {reason}. "
              f"Das Spiel läuft normal weiter.", file=sys.stderr)
    except Exception:
        pass


def engine_mode(campaign_dir) -> str:
    """The `engine:` flag from state.md → ## Session Flags; 'aus' when missing."""
    try:
        text = (pathlib.Path(campaign_dir) / "state.md").read_text(encoding="utf-8")
    except OSError:
        return "aus"
    start = text.find("## Session Flags")
    if start < 0:
        return "aus"
    section = text[start + len("## Session Flags"):]
    end = section.find("\n## ")
    if end >= 0:
        section = section[:end]
    m = _FLAG_RE.search(section)
    return m.group(1).lower() if m else "aus"


def child_env() -> dict:
    env = dict(os.environ)
    env[ENV_PARENT] = "1"
    return env


def record(campaign_dir, events: "Iterable[tuple[str, dict]]", src: str, by: str = "dm") -> int:
    """Append (type, data) pairs in shadow mode. Never raises; returns how many were written."""
    try:
        if os.environ.get(ENV_PARENT):
            return 0
        events = list(events)
        if not events or campaign_dir is None:
            return 0
        if not pathlib.Path(campaign_dir).is_dir():
            return 0
        if engine_mode(campaign_dir) == "aus":
            return 0
        from engine import eventlog
        written = 0
        for etype, data in events:
            eventlog.append(campaign_dir, etype, data, by=by, src=src)
            written += 1
        return written
    except Exception as exc:                       # noqa: BLE001 — shadow mode must never break play
        warn(str(exc) or type(exc).__name__)
        return 0


def active_campaign_dir() -> "Optional[pathlib.Path]":
    """The active campaign, or None.

    <runtime>/.campaign (written by push_stats.py --set-campaign) first, then
    <runtime>/active-campaign.json (written by /dm:dnd load) — the display may
    not have been told yet while the scripts already run.
    """
    try:
        import json
        from paths import runtime_dir, find_campaign
        base = pathlib.Path(runtime_dir())
        name = ""
        try:
            name = (base / ".campaign").read_text(encoding="utf-8").strip()
        except OSError:
            pass
        if not name:
            try:
                data = json.loads((base / "active-campaign.json").read_text(encoding="utf-8"))
                name = str(data.get("name") or "").strip() if isinstance(data, dict) else ""
            except (OSError, ValueError):
                pass
        if not name or "/" in name or "\\" in name or name.startswith("."):
            return None
        return find_campaign(name)
    except Exception:
        return None


# ── push_stats.py payload → events ──────────────────────────────────────────

def _int(value) -> "Optional[int]":
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def events_from_player(update: dict) -> "list[tuple[str, dict]]":
    """Translate one push_stats player update into log events (unknown keys are ignored)."""
    who = update.get("name")
    if not who:
        return []
    out: "list[tuple[str, dict]]" = []
    hp = update.get("hp")
    if isinstance(hp, dict):
        data = {"who": who}
        for key in ("current", "max"):
            if _int(hp.get(key)) is not None:
                data[key] = _int(hp[key])
        if len(data) > 1:
            out.append(("hp.set", data))
        if _int(hp.get("temp")) is not None:
            out.append(("hp.temp", {"who": who, "value": max(0, _int(hp["temp"]))}))
    if isinstance(update.get("conditions"), list):
        out.append(("condition.set", {"who": who, "list": [str(c) for c in update["conditions"]]}))
    if update.get("_conditions_add"):
        out.append(("condition.add", {"who": who, "name": str(update["_conditions_add"])}))
    if update.get("_conditions_remove"):
        out.append(("condition.remove", {"who": who, "name": str(update["_conditions_remove"])}))
    if "concentration" in update:
        spell = update["concentration"]
        out.append(("concentration.set", {"who": who, "spell": str(spell) if spell else None}))
    if isinstance(update.get("spell_slots"), dict):
        out.append(("slots.set", {"who": who, "slots": update["spell_slots"]}))
    for key, etype in (("_slot_use", "slot.use"), ("_slot_restore", "slot.restore")):
        if _int(update.get(key)) is not None:
            out.append((etype, {"who": who, "level": _int(update[key])}))
    if _int(update.get("_hd_use")):
        out.append(("hitdice.use", {"who": who, "n": _int(update["_hd_use"])}))
    if _int(update.get("_hd_restore")):
        out.append(("hitdice.restore", {"who": who, "n": _int(update["_hd_restore"])}))
    if update.get("_inventory_add"):
        out.append(("inventory.add", {"who": who, "item": str(update["_inventory_add"])}))
    if update.get("_inventory_remove"):
        out.append(("inventory.remove", {"who": who, "item": str(update["_inventory_remove"])}))
    xp = update.get("xp")
    if isinstance(xp, dict) and _int(xp.get("current")) is not None:
        data = {"who": who, "current": _int(xp["current"])}
        if _int(xp.get("next")) is not None:
            data["next"] = _int(xp["next"])
        out.append(("xp.set", data))
    if isinstance(update.get("inspiration"), bool):
        out.append(("inspiration.set", {"who": who, "value": update["inspiration"]}))
    if isinstance(update.get("second_wind"), bool):
        out.append(("feature.set", {"who": who, "name": "second_wind", "value": update["second_wind"]}))
    return out


def events_from_payload(payload: dict) -> "list[tuple[str, dict]]":
    """All log events for one /stats payload (players and turn order)."""
    out: "list[tuple[str, dict]]" = []
    for update in payload.get("players") or []:
        if isinstance(update, dict):
            out.extend(events_from_player(update))
    if "turn_order" in payload:
        turn = payload["turn_order"]
        if turn is None:
            out.append(("turn.clear", {}))
        elif isinstance(turn, dict) and isinstance(turn.get("order"), list):
            data = {"order": [str(n) for n in turn["order"]]}
            if turn.get("current") is not None:
                data["current"] = str(turn["current"])
            if _int(turn.get("round")) is not None:
                data["round"] = _int(turn["round"])
            out.append(("turn.order", data))
        elif isinstance(turn, dict) and turn.get("current"):
            data = {"who": str(turn["current"])}
            if _int(turn.get("round")) is not None:
                data["round"] = _int(turn["round"])
            out.append(("turn.current", data))
        elif isinstance(turn, dict) and _int(turn.get("round")) is not None:
            out.append(("turn.round", {"round": _int(turn["round"])}))
    return out
