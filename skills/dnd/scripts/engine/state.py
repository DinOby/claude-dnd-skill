"""state.py — the snapshot game/state.json, always computable from the log.

replay(events) starts at the last `import` event (which carries the imported
values) and applies every later event. state.json is only ever written from
replay(), so it can never drift from the log.

Names: events say `who` the way the scripts were called ("Brakka",
"brakka steinfaust", "Goblin 2"). resolve() maps that to a character by exact
name, then by unique first name, case-insensitive. Anyone else (monsters, NPCs)
lands in "others".
"""

import copy
import json
import os
import pathlib
import tempfile
from typing import Optional

from engine import PROTOCOL_VERSION

STATE_NAME = "state.json"


def state_path(campaign_dir) -> pathlib.Path:
    return pathlib.Path(campaign_dir) / "game" / STATE_NAME


def empty() -> dict:
    return {"v": PROTOCOL_VERSION, "seq": 0, "imported_seq": None,
            "characters": {}, "others": {}, "turn": None}


def blank_figure(name: str) -> dict:
    return {"name": name, "level": None, "xp": None, "xp_next": None,
            "hp": None, "hp_max": None, "temp_hp": 0, "ac": None,
            "hit_dice": None, "death_saves": {"successes": 0, "failures": 0, "stable": False},
            "spell_slots": {}, "inventory": {}, "currency": None, "inspiration": False,
            "resources": {}, "conditions": [], "concentration": None, "effects": []}


def resolve(state: dict, who: str) -> "tuple[str, str]":
    """('characters' | 'others', key) for an event's `who`."""
    low = (who or "").strip().lower()
    chars = state["characters"]
    for key, fig in chars.items():
        if key.lower() == low or (fig.get("name") or "").lower() == low:
            return "characters", key
    first = low.split()[0] if low else ""
    firsts = [k for k, f in chars.items() if (f.get("name") or k).lower().split()[:1] == [first]] if first else []
    if " " not in low and len(firsts) == 1:
        return "characters", firsts[0]
    return "others", (who or "?").strip()


def _figure(state: dict, who: str) -> dict:
    group, key = resolve(state, who)
    figs = state[group]
    if key not in figs:
        figs[key] = blank_figure(key)
    return figs[key]


def _ci_remove(items: list, name: str) -> list:
    return [i for i in items if str(i).lower() != str(name).lower()]


def apply(state: dict, event: dict) -> dict:
    """Apply one event to `state` (in place) and return it."""
    t, d = event["type"], event["data"]
    if t == "import":
        new = empty()
        new["characters"] = copy.deepcopy(d.get("characters", {}))
        new["imported_seq"] = event["seq"]
        state.clear()
        state.update(new)
    elif t.startswith("turn."):
        if t == "turn.clear":
            state["turn"] = None
        elif t == "turn.order":
            state["turn"] = {"order": list(d["order"]), "current": d.get("current"), "round": d.get("round")}
        else:
            turn = state["turn"] or {"order": [], "current": None, "round": None}
            if t == "turn.current":
                turn["current"] = d["who"]
            if "round" in d:
                turn["round"] = d["round"]
            state["turn"] = turn
    elif t == "tracker.clear":
        for group in ("characters", "others"):
            for fig in state[group].values():
                fig["conditions"], fig["concentration"], fig["effects"] = [], None, []
                if d.get("all"):
                    fig["death_saves"] = {"successes": 0, "failures": 0, "stable": False}
    elif "who" in d:
        _apply_figure(_figure(state, d["who"]), t, d)
    state["seq"] = max(state.get("seq") or 0, event["seq"])
    return state


def _apply_figure(fig: dict, t: str, d: dict) -> None:
    if t == "hp.set":
        for key, field in (("current", "hp"), ("max", "hp_max")):
            if key in d:
                fig[field] = d[key]
    elif t == "hp.temp":
        fig["temp_hp"] = d["value"]
    elif t == "condition.set":
        fig["conditions"] = list(dict.fromkeys(d["list"]))
    elif t == "condition.add":
        if d["name"].lower() not in (c.lower() for c in fig["conditions"]):
            fig["conditions"].append(d["name"])
    elif t == "condition.remove":
        fig["conditions"] = _ci_remove(fig["conditions"], d["name"])
    elif t == "condition.clear":
        fig["conditions"] = []
    elif t == "concentration.set":
        fig["concentration"] = d.get("spell")
    elif t == "effect.start":
        fig["effects"] = [e for e in fig["effects"] if e["name"].lower() != d["name"].lower()]
        fig["effects"].append({"name": d["name"], "duration": d.get("duration"), "conc": bool(d.get("conc"))})
        if d.get("conc"):
            fig["concentration"] = d["name"]
    elif t == "effect.end":
        fig["effects"] = [e for e in fig["effects"] if e["name"].lower() != d["name"].lower()]
        if (fig.get("concentration") or "").lower() == d["name"].lower():
            fig["concentration"] = None
    elif t == "deathsave":
        ds = fig["death_saves"]
        if d["result"] == "success":
            ds["successes"] = min(3, ds["successes"] + 1)
        elif d["result"] == "failure":
            ds["failures"] = min(3, ds["failures"] + 1)
        elif d["result"] == "stable":
            ds["stable"] = True
        else:
            fig["death_saves"] = {"successes": 0, "failures": 0, "stable": False}
    elif t == "slots.set":
        fig["spell_slots"] = {str(k): {"max": int(v.get("max", 0)), "used": int(v.get("used", 0))}
                              for k, v in d["slots"].items() if isinstance(v, dict)}
    elif t in ("slot.use", "slot.restore"):
        slot = fig["spell_slots"].setdefault(str(d["level"]), {"max": 0, "used": 0})
        if t == "slot.use":
            slot["used"] = min(slot["used"] + 1, slot["max"]) if slot["max"] else slot["used"] + 1
        else:
            slot["used"] = max(0, slot["used"] - 1)
    elif t in ("hitdice.use", "hitdice.restore") and fig.get("hit_dice"):
        hd = fig["hit_dice"]
        delta = -d["n"] if t == "hitdice.use" else d["n"]
        hd["remaining"] = max(0, min(hd["total"], hd["remaining"] + delta))
    elif t == "hitdice.set":
        hd = dict(fig.get("hit_dice") or {"die": d.get("die") or "d8", "total": 0, "remaining": 0})
        if d.get("die"):
            hd["die"] = d["die"]
        hd["total"] = d["total"]
        hd["remaining"] = max(0, min(d["total"], d["remaining"]))
        fig["hit_dice"] = hd
    elif t == "level.set":
        fig["level"] = d["level"]
        if "xp_next" in d:
            fig["xp_next"] = d["xp_next"]
    elif t == "inventory.add":
        fig["inventory"].setdefault("Neu", []).append(d["item"])
    elif t == "inventory.remove":
        for group, items in fig["inventory"].items():
            kept = _ci_remove(items, d["item"])
            if len(kept) != len(items):
                fig["inventory"][group] = kept
                break
    elif t == "xp.set":
        fig["xp"] = d["current"]
        if "next" in d:
            fig["xp_next"] = d["next"]
    elif t == "xp.award" and "total_after" in d:
        fig["xp"] = d["total_after"]
        if "next" in d:
            fig["xp_next"] = d["next"]
    elif t == "inspiration.set":
        fig["inspiration"] = d["value"]
    elif t == "feature.set":
        fig["resources"][d["name"]] = d["value"]
    elif t == "currency.set":
        coins = dict(fig.get("currency") or {"pp": 0, "gp": 0, "ep": 0, "sp": 0, "cp": 0})
        for coin in ("pp", "gp", "ep", "sp", "cp"):
            if coin in d:
                coins[coin] = d[coin]
        fig["currency"] = coins
    elif t == "resource.set":
        fig["resources"][_resource_key(fig["resources"], d["name"])] = {"left": d["left"], "max": d["max"]}


def _resource_key(resources: dict, name: str) -> str:
    """The existing key a resource name refers to: 'Rage' finds 'Rage (Kampfrausch)'."""
    low = name.strip().lower()
    for key in resources:
        if key.lower() == low or key.split(" (")[0].strip().lower() == low:
            return key
    return name.strip()


def replay(events: "list[dict]") -> dict:
    """The snapshot for a list of events (only the last import and what follows counts)."""
    start = 0
    for i, e in enumerate(events):
        if e["type"] == "import":
            start = i
    state = empty()
    for e in events[start:]:
        apply(state, e)
    return state


def write(campaign_dir, state: dict) -> pathlib.Path:
    """Write state.json atomically (temp file + replace)."""
    path = state_path(campaign_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".state.", suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
        f.write("\n")
    os.replace(tmp, path)
    return path


def read(campaign_dir) -> Optional[dict]:
    try:
        with open(state_path(campaign_dir), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None
