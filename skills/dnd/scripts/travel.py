#!/usr/bin/env python3
"""travel.py — journeys between places, with an automatic travel-event check per day.

The scene state (<campaign>/scene-state.json, see display/scene_state.py)
says whether the party is at a place, travelling, or in a travel event. This
script changes it and works without the display; a running display is told
afterwards and brings the battle map in line (hidden while travelling, an
event map during an event).

Usage:
    python3 travel.py [-c CAMPAIGN] start --to Dornfeld --days 4 [--terrain forest] [--via "Königsstraße"] [--from Ashveil]
    python3 travel.py [-c CAMPAIGN] day [--no-check] [--no-clock] [--seed N]
    python3 travel.py [-c CAMPAIGN] event [--id wolfsrudel] [--title "…" --template forest-clearing] [--seed N]
    python3 travel.py [-c CAMPAIGN] event-end
    python3 travel.py [-c CAMPAIGN] arrive [--location Dornfeld]
    python3 travel.py [-c CAMPAIGN] status [--json]

`day` = one travel day passes: the calendar advances 24 hours (calendar.py,
skipped with --no-clock or when the campaign has no calendar), then the
event check rolls d100 against `travel_event_chance: N` from state.md
Session Flags (default 15). On a hit an event is drawn from
display/config/travel-events.json for the journey's terrain and the scene
switches to travel_event. `event` forces one. CAMPAIGN defaults to the
active campaign of the display.

Terrains: road, forest, hills, mountain, river, plains, swamp (any other
word works too; it just only draws from the `any` events).
"""

import argparse
import json
import os
import random
import re
import ssl
import subprocess
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

from config_loader import ConfigFile, check_types        # noqa: E402
from paths import find_campaign                          # noqa: E402
from runtime_paths import rt                             # noqa: E402
import oracle                                            # noqa: E402
import scene_state as ss                                 # noqa: E402

TOKEN_KINDS = ("pc", "npc", "enemy", "object")


# ── Event table ───────────────────────────────────────────────────────────────

def validate_events(cfg: dict) -> "list[str]":
    problems = check_types(cfg, {"version": int, "terrains": dict})
    if problems:
        return problems
    for terrain, events in cfg["terrains"].items():
        if not isinstance(events, dict):
            problems.append(f"terrains.{terrain} should be an object of events")
            continue
        for eid, ev in events.items():
            where = f"terrains.{terrain}.{eid}"
            if not isinstance(ev, dict) or not isinstance(ev.get("title"), str) or not ev["title"].strip():
                problems.append(f"{where}: needs a title")
                continue
            w = ev.get("weight", 1)
            if not (isinstance(w, (int, float)) and not isinstance(w, bool) and w > 0):
                problems.append(f"{where}: weight should be a positive number")
            if "template" in ev and not isinstance(ev["template"], str):
                problems.append(f"{where}: template should be a template id")
            for i, tok in enumerate(ev.get("tokens", [])):
                if (not isinstance(tok, dict) or not isinstance(tok.get("name"), str)
                        or tok.get("kind", "enemy") not in TOKEN_KINDS
                        or not isinstance(tok.get("count", 1), int) or not 1 <= tok.get("count", 1) <= 12):
                    problems.append(f"{where}.tokens[{i}]: needs name, kind ({'/'.join(TOKEN_KINDS)}), count 1–12")
    return problems


def event_table() -> ConfigFile:
    return ConfigFile("travel-events.json", validator=validate_events)


def expand_tokens(tokens: list) -> "list[dict]":
    """[{"name": "Wolf", "count": 3}] → Wolf 1, Wolf 2, Wolf 3.

    Numbered like initiative entries, so the turn order, --stat-move and the
    map all use the same names; portraits still resolve via "Wolf".
    """
    out = []
    for tok in tokens or []:
        base = {k: v for k, v in tok.items() if k != "count"}
        base.setdefault("kind", "enemy")
        n = int(tok.get("count", 1))
        out += [dict(base, name=f"{base['name']} {i}" if n > 1 else base["name"]) for i in range(1, n + 1)]
    return out


def draw_event(terrain: str, cfg: dict, rng: random.Random) -> dict:
    """Weighted draw from the terrain's events plus `any`; Mythic focus when the table is empty."""
    terrains = cfg.get("terrains") or {}
    pool = [(eid, ev) for eid, ev in (terrains.get(terrain) or {}).items()]
    pool += [(eid, ev) for eid, ev in (terrains.get("any") or {}).items()]
    focus_roll, focus = oracle.random_event_focus(rng)
    if not pool:
        verb, noun = oracle.scene_meaning(rng)
        return {"id": "ereignis", "title": f"Unerwartetes Ereignis ({focus})", "template": None,
                "tokens": [], "hint": f"Mythic focus: {focus} (d100 {focus_roll}); meaning: {verb} / {noun}",
                "focus": focus}
    eid, ev = rng.choices(pool, weights=[float(ev.get("weight", 1)) for _, ev in pool])[0]
    return {"id": eid, "title": ev["title"].strip(), "template": ev.get("template"),
            "tokens": expand_tokens(ev.get("tokens")), "hint": ev.get("hint"), "focus": focus}


# ── Campaign plumbing ─────────────────────────────────────────────────────────

def active_campaign(explicit: "str | None") -> str:
    if explicit:
        return explicit
    try:
        name = open(rt(".campaign"), encoding="utf-8").read().strip()
    except OSError:
        name = ""
    if not name:
        raise SystemExit("no campaign given and no active campaign — use -c CAMPAIGN")
    return name


def store_for(campaign: str) -> ss.SceneStore:
    return ss.SceneStore(str(find_campaign(campaign) / "scene-state.json"))


def state_md_text(campaign: str) -> str:
    try:
        return (find_campaign(campaign) / "state.md").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def advance_clock(campaign: str) -> str:
    """calendar.py advance 1 day; returns a note for the output (never fails the journey)."""
    try:
        proc = subprocess.run([sys.executable, os.path.join(_HERE, "calendar.py"), "-c", campaign,
                               "advance", "1", "days"], capture_output=True, text=True,
                              encoding="utf-8", timeout=15)
    except (OSError, subprocess.TimeoutExpired) as e:
        return f"clock not advanced ({e})"
    out = (proc.stdout or proc.stderr).strip().splitlines()
    if proc.returncode != 0:
        return "clock not advanced: " + (out[-1] if out else f"calendar.py exit {proc.returncode}")
    return out[-1].strip() if out else "clock +1 day"


def notify_display() -> None:
    """Ask a running display to re-read the scene state; silent when it is not running."""
    try:
        scheme_file = os.path.join(_DISPLAY, ".scheme")
        scheme = open(scheme_file, encoding="utf-8").read().strip() if os.path.exists(scheme_file) else "http"
        try:
            token = open(rt(".token"), encoding="utf-8").read().strip()
        except OSError:
            token = ""
        headers = {"Content-Type": "application/json"}
        if token:
            headers["X-DND-Token"] = token
        req = urllib.request.Request(f"{scheme}://localhost:5001/scene", data=b'{"sync": true}',
                                     method="POST", headers=headers)
        ctx = None
        if scheme == "https":
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
        urllib.request.urlopen(req, timeout=3, context=ctx)
    except Exception:
        pass


# ── Output ────────────────────────────────────────────────────────────────────

def describe(state: dict) -> str:
    t = state.get("travel") or {}
    if state["mode"] == "stationary":
        where = state.get("location") or "(no place set)"
        return f"At {where}" + (f", map {state['map_id']}" if state.get("map_id") else "")
    route = f"{t.get('from') or '?'} → {t.get('to')}" + (f" via {t['via']}" if t.get("via") else "")
    line = f"Travelling {route}, day {t.get('day')}/{t.get('days_total')} ({t.get('terrain')})"
    if state["mode"] == "travel_event":
        ev = state.get("event") or {}
        line += f"\nEvent: {ev.get('title')} — map {state.get('map_id')}"
    return line


def describe_event(ev: dict) -> str:
    counts: "dict[str, int]" = {}
    for tok in ev.get("tokens") or []:
        name = re.sub(r"\s+\d+$", "", tok["name"])   # "Wolf 2" counts as a Wolf
        counts[name] = counts.get(name, 0) + 1
    who = ", ".join(f"{n}× {name}" if n > 1 else name for name, n in counts.items()) or "no tokens"
    lines = [f"EVENT: {ev['title']}  (map template {ev.get('template') or 'none'}; {who})"]
    if ev.get("hint"):
        lines.append(f"  Hint: {ev['hint']}")
    if ev.get("focus"):
        lines.append(f"  Mythic focus: {ev['focus']}")
    return "\n".join(lines)


# ── Commands ──────────────────────────────────────────────────────────────────

def _start_event(store: ss.SceneStore, ev: dict) -> dict:
    return store.apply("event-start", id=ev["id"], title=ev["title"], template=ev.get("template"),
                       tokens=ev.get("tokens"), focus=ev.get("focus"))


def cmd_start(camp, store, args) -> int:
    state = store.apply("travel-start", to=args.to, days=args.days, terrain=args.terrain,
                        via=args.via, **({"from": args.from_} if args.from_ else {}))
    print(describe(state))
    return 0


def cmd_day(camp, store, args) -> int:
    before = store.get()
    if before["mode"] == "travel" and before["travel"]["day"] >= before["travel"]["days_total"]:
        print(f"All {before['travel']['days_total']} travel days are done — "
              f"arrive with `travel.py arrive` (or start a longer journey).")
        return 1
    state = store.apply("travel-day")
    if not args.no_clock:
        print(advance_clock(camp))
    print(describe(state))
    if args.no_check:
        return 0
    rng = random.Random(args.seed)
    chance = ss.event_chance(state_md_text(camp))
    roll = rng.randint(1, 100)
    if roll > chance:
        print(f"Event check: d100 {roll} > {chance} % — the day passes quietly.")
        t = state["travel"]
        if t["day"] >= t["days_total"]:
            print(f"Destination {t['to']} reached at the end of the day — `travel.py arrive`.")
        return 0
    print(f"Event check: d100 {roll} ≤ {chance} % — something happens.")
    ev = draw_event(state["travel"]["terrain"], event_table().get(), rng)
    state = _start_event(store, ev)
    print(describe_event(ev))
    print(f"Scene: travel_event, map {state['map_id']}. End it with `travel.py event-end`.")
    return 0


def cmd_event(camp, store, args) -> int:
    state = store.get()
    cfg = event_table().get()
    rng = random.Random(args.seed)
    if args.title:
        ev = {"id": args.id or args.title, "title": args.title, "template": args.template, "tokens": [],
              "hint": None, "focus": None}
    elif args.id:
        found = [(t, e) for t, evs in (cfg.get("terrains") or {}).items() for eid, e in evs.items() if eid == args.id]
        if not found:
            print(f"unknown event '{args.id}' in travel-events.json", file=sys.stderr)
            return 1
        e = found[0][1]
        ev = {"id": args.id, "title": e["title"], "template": e.get("template"),
              "tokens": expand_tokens(e.get("tokens")), "hint": e.get("hint"), "focus": None}
    else:
        terrain = (state.get("travel") or {}).get("terrain") or "road"
        ev = draw_event(terrain, cfg, rng)
    if args.template:
        ev["template"] = args.template
    state = _start_event(store, ev)
    print(describe_event(ev))
    print(f"Scene: travel_event, map {state['map_id']}. End it with `travel.py event-end`.")
    return 0


def cmd_event_end(camp, store, args) -> int:
    state = store.apply("event-end")
    print("Event over — the journey continues (event map discarded).")
    print(describe(state))
    return 0


def cmd_arrive(camp, store, args) -> int:
    state = store.apply("travel-end", location=args.location)
    print(describe(state))
    return 0


def cmd_status(camp, store, args) -> int:
    state = store.get()
    if args.json:
        print(json.dumps(state, ensure_ascii=False, indent=2))
    else:
        print(describe(state))
        print(f"Travel event chance: {ss.event_chance(state_md_text(camp))} % per day")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="travel", description="Journeys and travel events.")
    ap.add_argument("-c", "--campaign", help="campaign name (default: active campaign)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    st = sub.add_parser("start", help="set out on a journey")
    st.add_argument("--to", required=True)
    st.add_argument("--days", type=int, required=True)
    st.add_argument("--terrain", default="road")
    st.add_argument("--via")
    st.add_argument("--from", dest="from_")
    d = sub.add_parser("day", help="one travel day passes (clock + event check)")
    d.add_argument("--no-check", action="store_true", help="skip the event check")
    d.add_argument("--no-clock", action="store_true", help="do not advance the calendar")
    d.add_argument("--seed", type=int)
    ev = sub.add_parser("event", help="start a travel event now")
    ev.add_argument("--id", help="event id from travel-events.json")
    ev.add_argument("--title", help="own event instead of the table")
    ev.add_argument("--template", help="map template for the event map")
    ev.add_argument("--seed", type=int)
    sub.add_parser("event-end", help="end the travel event, continue the journey")
    ar = sub.add_parser("arrive", help="end the journey")
    ar.add_argument("--location", help="where the party is now (default: the destination)")
    s = sub.add_parser("status", help="current scene state")
    s.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    camp = active_campaign(args.campaign)
    store = store_for(camp)
    handlers = {"start": cmd_start, "day": cmd_day, "event": cmd_event, "event-end": cmd_event_end,
                "arrive": cmd_arrive, "status": cmd_status}
    try:
        rc = handlers[args.cmd](camp, store, args)
    except ss.SceneError as e:
        print(f"travel: {e}", file=sys.stderr)
        return 1
    if args.cmd != "status":
        notify_display()
    return rc


if __name__ == "__main__":
    sys.exit(main())
