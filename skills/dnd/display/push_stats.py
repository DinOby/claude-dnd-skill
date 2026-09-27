#!/usr/bin/env python3
"""
push_stats.py — push character/combat stats to the DnD display server.

Stats are merged by player name on the server, so partial updates work.

Usage:
    # Full stats push (on campaign load — read from character sheet):
    python3 push_stats.py --json '{"players":[{"name":"Flerb","race":"Tiefling",...}]}'

    # Quick HP update (damage / healing):
    python3 push_stats.py --player Flerb --hp 7 12         # current max

    # Temp HP (e.g. Symbiotic Entity, Aid spell):
    python3 push_stats.py --player Flerb --temp-hp 8       # set temp HP
    python3 push_stats.py --player Flerb --temp-hp 0       # clear temp HP

    # Quick XP update:
    python3 push_stats.py --player Flerb --xp 220 300      # current next_level

    # Hit dice (short rest):
    python3 push_stats.py --player Flerb --hit-dice-use    # spend one hit die
    python3 push_stats.py --player Flerb --hit-dice-restore 2  # restore N hit dice

    # Feature flag (e.g. Second Wind used):
    python3 push_stats.py --player Flerb --second-wind false

    # Conditions — full replace:
    python3 push_stats.py --player Flerb --conditions "Poisoned,Frightened"
    python3 push_stats.py --player Flerb --conditions ""   # clear all

    # Conditions — granular add/remove (preferred mid-session):
    python3 push_stats.py --player Flerb --conditions-add "Poisoned"
    python3 push_stats.py --player Flerb --conditions-remove "Poisoned"

    # Concentration:
    python3 push_stats.py --player Flerb --concentrate "Bless"
    python3 push_stats.py --player Flerb --concentrate ""   # clear

    # Spell slots — full replace:
    python3 push_stats.py --player Flerb --spell-slots '{"1":{"used":1,"max":4},"2":{"used":0,"max":2}}'

    # Spell slots — granular use/restore (preferred mid-session):
    python3 push_stats.py --player Flerb --slot-use 1      # expend one 1st-level slot
    python3 push_stats.py --player Flerb --slot-restore 2  # restore one 2nd-level slot

    # Inventory — granular add/remove (preferred to full --sheet rewrite):
    python3 push_stats.py --player Flerb --inventory-add "Iron key"
    python3 push_stats.py --player Flerb --inventory-remove "Folded paper"

    # Faction standings (party-wide):
    python3 push_stats.py --factions '[{"name":"Pale Court","standing":"Suspicious"},{"name":"Merchant Guild","standing":"Friendly"}]'
    python3 push_stats.py --factions '[]'   # clear all

    # Combat — set full turn order:
    python3 push_stats.py --turn-order '{"order":["Goblin 1","Flerb"],"current":"Goblin 1","round":1}'

    # Combat — advance turn pointer:
    python3 push_stats.py --turn-current "Flerb"

    # Combat — advance round:
    python3 push_stats.py --turn-round 2

    # Combat ended — clear turn order:
    python3 push_stats.py --turn-clear

    # Clear all text + stats (use on /dnd load — token-aware, works in LAN mode):
    python3 push_stats.py --clear

    # Battle map (main display only). Positions: column letter + row, A1 = top-left.
    python3 push_stats.py --map-set '{"id":"kessel-schankraum","cols":14,"rows":10,"terrain":[...],"tokens":[...]}'
    python3 push_stats.py --map-set @map.json               # same, from a file
    python3 push_stats.py --map-show kessel-schankraum       # library layout + this campaign's tokens
    python3 push_stats.py --map-new tavern-small --map-id kessel-schankraum --map-name "Schankraum im Kessel"
                                                             # from a template (stored layout if the id exists)
    python3 push_stats.py --token-party                      # all player characters onto the pc spawn zone
    python3 push_stats.py --token-add "Goblin:enemy"         # no position -> free square in the enemy zone
    python3 push_stats.py --map-hide
    python3 push_stats.py --stat-move "Flerb:D5" --stat-move "Goblin 1:E6"
    python3 push_stats.py --token-add "Goblin 2:E7:enemy"    # NAME[:POS][:pc|npc|enemy|object]
    python3 push_stats.py --token-add '{"name":"Oger","kind":"enemy","at":"H3","size":2}'
    python3 push_stats.py --token-remove "Goblin 1"
    # Move/add/remove in one call apply together or not at all; problems are printed.

    # Scene: a new place (without a journey) — clears the old map; combine with --map-* for the new one
    python3 push_stats.py --scene-set "Zum grünen Kessel" --map-new tavern-small --map-id zum-gruenen-kessel
    # Journeys and travel events: scripts/travel.py (start | day | event | event-end | arrive)
"""

import sys
import json
import argparse
import os
import re
import ssl
import time
import urllib.error
import urllib.request
from typing import Optional

# Windows CJK fix: piped stdout defaults to the system codepage (cp936/GBK),
# which garbles or crashes on Chinese — force UTF-8.
try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

_DISPLAY_DIR = os.path.dirname(os.path.abspath(__file__))
if _DISPLAY_DIR not in sys.path:
    sys.path.insert(0, _DISPLAY_DIR)
from runtime_paths import rt          # writable runtime dir (update-safe)
_SCHEME_FILE = os.path.join(_DISPLAY_DIR, ".scheme")   # launch marker → code dir
_SCHEME = open(_SCHEME_FILE, encoding="utf-8").read().strip() if os.path.exists(_SCHEME_FILE) else "http"
FLASK_URL  = f"{_SCHEME}://localhost:5001/stats"
TOKEN_FILE = rt(".token")
TIMEOUT    = 2.0

# SSL context — only used when running HTTPS (self-signed cert)
if _SCHEME == "https":
    _SSL_CTX = ssl.create_default_context()
    _SSL_CTX.check_hostname = False
    _SSL_CTX.verify_mode = ssl.CERT_NONE
else:
    _SSL_CTX = None


def _read_token() -> str:
    try:
        return open(TOKEN_FILE, encoding="utf-8").read().strip()
    except FileNotFoundError:
        return ""


def _send(url: str, data: bytes, token: str) -> None:
    headers = {"Content-Type": "application/json"}
    if token:
        headers["X-DND-Token"] = token
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        urllib.request.urlopen(req, timeout=TIMEOUT, context=_SSL_CTX)
    except Exception:
        pass  # Display not running — fail silently


def _post_map(body: dict, token: str, route: str = "/map") -> int:
    """POST /map (or /scene) and print the outcome — these commands need feedback."""
    headers = {"Content-Type": "application/json"}
    if token:
        headers["X-DND-Token"] = token
    req = urllib.request.Request(FLASK_URL.replace("/stats", route), data=json.dumps(body).encode("utf-8"),
                                 headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT, context=_SSL_CTX) as resp:
            result = json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as e:
        try:
            problems = json.loads(e.read() or b"{}").get("errors") or [f"HTTP {e.code}"]
        except ValueError:
            problems = [f"HTTP {e.code}"]
        for p in problems:
            print(f"{route[1:]}: {p}", file=sys.stderr)
        return 1
    except Exception:
        print(f"{route[1:]}: display not reachable — nothing changed", file=sys.stderr)
        return 0
    for w in result.get("warnings") or []:
        print(f"{route[1:]} warning: {w}", file=sys.stderr)
    if result.get("map_id"):
        print(f"map {result['map_id']} rev {result['rev']}")
    if isinstance(result.get("state"), dict):
        st = result["state"]
        print(f"scene {st.get('mode')}: {st.get('location') or '-'}")
    return 0


_KINDS = ("pc", "npc", "enemy", "object")
_POS_RE = re.compile(r"^[A-Za-z]{1,2}\d{1,3}$")


def _token_spec(spec: str) -> dict:
    """'Goblin 2:E7:enemy', 'Goblin:enemy', 'Wirtin Hilde:B3', 'Wirtin Hilde' or JSON → token.

    Without a position the server puts the token on a free square of its
    kind's spawn zone.
    """
    spec = spec.strip()
    if spec.startswith("{"):
        return json.loads(spec)
    parts = spec.split(":")
    kind = "npc"
    if len(parts) > 1 and parts[-1].strip().lower() in _KINDS:
        kind = parts.pop().strip().lower()
    pos = None
    if len(parts) > 1 and (_POS_RE.match(parts[-1].strip()) or not parts[-1].strip()):
        pos = parts.pop().strip() or None
    name = ":".join(parts).strip()
    if not name:
        raise ValueError(f"expected NAME[:POSITION][:KIND], got {spec!r}")
    tok = {"name": name, "kind": kind}
    if pos:
        tok["at"] = pos
    return tok


def _move_spec(spec: str) -> dict:
    """'Flerb:D5' → {"id": "Flerb", "to": "D5"} (the server matches names too)."""
    name, _, pos = spec.strip().rpartition(":")
    if not name.strip() or not pos.strip():
        raise ValueError(f"expected NAME:POSITION, got {spec!r}")
    return {"id": name.strip(), "to": pos.strip()}


def _map_body(args) -> "tuple[list[dict], Optional[str]]":
    """Requests for /map from the --map-* / --stat-move / --token-* flags (in order)."""
    bodies = []
    try:
        if args.scene_set is not None:
            if not args.scene_set.strip():
                raise ValueError("--scene-set needs a place name")
            bodies.append({"_route": "/scene", "action": "scene-set", "location": args.scene_set.strip()})
        if args.map_set:
            raw = args.map_set
            if raw.startswith("@"):
                with open(raw[1:], encoding="utf-8-sig") as f:
                    raw = f.read()
            bodies.append({"map": json.loads(raw)})
        if args.map_show:
            bodies.append({"show": args.map_show})
        if args.map_new:
            new = {"template": args.map_new}
            if args.map_id:
                new["id"] = args.map_id
            if args.map_name:
                new["name"] = args.map_name
            bodies.append({"map": new})
        elif args.map_id or args.map_name:
            raise ValueError("--map-id/--map-name need --map-new TEMPLATE")
        patch = {}
        if args.stat_move:
            patch["move"] = [_move_spec(s) for s in args.stat_move]
        if args.token_add:
            patch["add"] = [_token_spec(s) for s in args.token_add]
        if args.token_remove:
            patch["remove"] = [s.strip() for s in args.token_remove]
        if args.token_party:
            patch["add_party"] = True
        if patch:
            bodies.append({"patch": patch})
        if args.map_hide:
            bodies.append({"hide": True})
    except (ValueError, OSError) as e:
        return [], str(e)
    return bodies, None


def main() -> None:
    parser = argparse.ArgumentParser(description="Push stats to the DnD display server.")
    parser.add_argument("--json", metavar="JSON",
                        help="Full or partial stats JSON (top-level keys: players, turn_order)")
    parser.add_argument("--player", metavar="NAME",
                        help="Target player name for shorthand flags below")
    parser.add_argument("--hp", nargs=2, metavar=("CURRENT", "MAX"), type=int,
                        help="Update HP current and max (requires --player)")
    parser.add_argument("--temp-hp", metavar="N", type=int,
                        help="Set temp HP; use 0 to clear (requires --player)")
    parser.add_argument("--xp", nargs=2, metavar=("CURRENT", "NEXT"), type=int,
                        help="Update XP (requires --player)")
    parser.add_argument("--hit-dice-use", action="store_true",
                        help="Spend one hit die (requires --player)")
    parser.add_argument("--hit-dice-restore", metavar="N", type=int,
                        help="Restore N hit dice (requires --player)")
    parser.add_argument("--second-wind", metavar="BOOL",
                        help="Second Wind available: true or false (requires --player)")
    parser.add_argument("--conditions", metavar="LIST",
                        help="Comma-separated active conditions, full replace (requires --player); empty string clears all")
    parser.add_argument("--conditions-add", metavar="CONDITION",
                        help="Add one condition without replacing others (requires --player)")
    parser.add_argument("--conditions-remove", metavar="CONDITION",
                        help="Remove one condition by name, case-insensitive (requires --player)")
    parser.add_argument("--concentrate", metavar="SPELL",
                        help="Spell being concentrated on (requires --player); empty string clears")
    parser.add_argument("--spell-slots", metavar="JSON",
                        help='Spell slots per level, full replace: {"1":{"used":1,"max":4},...} (requires --player)')
    parser.add_argument("--slot-use", metavar="LEVEL", type=int,
                        help="Expend one slot at the given level (requires --player)")
    parser.add_argument("--slot-restore", metavar="LEVEL", type=int,
                        help="Restore one slot at the given level (requires --player)")
    parser.add_argument("--inventory-add", metavar="ITEM",
                        help="Append one item to inventory (requires --player)")
    parser.add_argument("--inventory-remove", metavar="ITEM",
                        help="Remove one item from inventory by name, case-insensitive (requires --player)")
    parser.add_argument("--ability-scores", metavar="JSON",
                        help='Set ability scores: {"str":18,"dex":16,...} or {"str":{"score":18,"mod":"+4"},...} (requires --player)')
    parser.add_argument("--spells", metavar="JSON",
                        help='Set spells inside the sheet tab: {"cantrips":["Mending"],"level1":["Cure Wounds"],...} (requires --player)')
    parser.add_argument("--inspiration", metavar="BOOL",
                        help="Set Inspiration: true or false (requires --player)")
    parser.add_argument("--sheet", metavar="JSON",
                        help='Full character sheet data: {"attacks":[...],"spells":{...},"features":[...],"inventory":[...]} (requires --player)')
    parser.add_argument("--factions", metavar="JSON",
                        help='Party faction standings: [{"name":"Pale Court","standing":"Suspicious"},...]; [] clears')
    parser.add_argument("--quests", metavar="JSON",
                        help='Quest tracker: [{"name":"The Ward-Points","status":"resolved"},{"name":"Vedra Ceth","status":"threat"},...]; [] clears. Status values: active, threat, resolved, failed')
    parser.add_argument("--turn-order", metavar="JSON",
                        help='Full turn order JSON: {"order":[...],"current":"Name","round":1}')
    parser.add_argument("--turn-current", metavar="NAME",
                        help="Advance the turn pointer to this combatant name")
    parser.add_argument("--turn-round", metavar="N", type=int,
                        help="Set the current round number")
    parser.add_argument("--turn-clear", action="store_true",
                        help="Clear the turn order (combat ended)")
    parser.add_argument("--replace-players", action="store_true",
                        help="Replace the entire player list (use on /dnd load to clear stale characters)")
    parser.add_argument("--clear", action="store_true",
                        help="Clear all text and stats on the display (token-aware; safe in LAN mode)")
    parser.add_argument("--world-time", metavar="JSON",
                        help='World time: {"date":"19 Ashveil 1312 AR","day_name":"Moonday","time":"morning","season":"Long Hollow","weather":"calm"}')
    parser.add_argument("--autorun-waiting", metavar="BOOL",
                        help="true = show autorun waiting indicator on display; false = hide it")
    parser.add_argument("--autorun-cycle", metavar="SECONDS", type=int,
                        help="Start autorun countdown: broadcast interval + current timestamp to display")
    parser.add_argument("--autorun-threshold", metavar="N", type=int,
                        help="Set min players needed to auto-fire (0 or omit to reset to player count)")
    parser.add_argument("--set-campaign", metavar="NAME",
                        help="Set the active campaign name (written to .campaign for dm_help.py). "
                             "Server auto-resolves ruleset from the campaign's state.md.")
    parser.add_argument("--ruleset", metavar="2014|2024",
                        help="Override the ruleset badge displayed in the sidebar. "
                             "Normally the server resolves this from the campaign on --set-campaign.")
    parser.add_argument("--map-set", metavar="JSON|@FILE",
                        help="Show a full battle map (layout is saved to the map library)")
    parser.add_argument("--map-show", metavar="ID",
                        help="Show a map from the library with this campaign's tokens")
    parser.add_argument("--map-hide", action="store_true", help="Take the battle map off screen")
    parser.add_argument("--scene-set", metavar="PLACE",
                        help="The party is now at PLACE (no journey); the previous map is cleared — "
                             "add one with --map-new/--map-show in the same call")
    parser.add_argument("--map-new", metavar="TEMPLATE",
                        help="Show a map built from a template (tavern-small, forest-road, ...); "
                             "an existing library map with the same id is reused")
    parser.add_argument("--map-id", metavar="ID", help="Id for --map-new (default: the template id)")
    parser.add_argument("--map-name", metavar="NAME", help="Display name for --map-new")
    parser.add_argument("--token-party", action="store_true",
                        help="Put every player character that is not on the map onto the pc spawn zone")
    parser.add_argument("--stat-move", metavar="NAME:POS", action="append",
                        help='Move a token, e.g. "Flerb:D5" (repeatable)')
    parser.add_argument("--token-add", metavar="NAME[:POS][:KIND]", action="append",
                        help='Add a token, e.g. "Goblin 2:E7:enemy" or "Goblin:enemy" (spawn zone), or JSON (repeatable)')
    parser.add_argument("--token-remove", metavar="NAME", action="append",
                        help="Remove a token by name or id (repeatable)")
    args = parser.parse_args()

    # ── Battle map (own endpoint; may be combined with stat flags) ────────────
    map_bodies, map_error = _map_body(args)
    if map_error:
        print(f"map: {map_error}", file=sys.stderr)
        sys.exit(1)

    payload: dict = {}

    # ── Full JSON passthrough ──────────────────────────────────────────────────
    if args.json:
        try:
            payload = json.loads(args.json)
        except json.JSONDecodeError as e:
            print(f"Invalid JSON: {e}", file=sys.stderr)
            sys.exit(1)

    # ── Per-player shorthands ──────────────────────────────────────────────────
    _player_flags = (
        args.hp or args.temp_hp is not None or args.xp
        or args.second_wind is not None
        or args.conditions is not None or args.conditions_add or args.conditions_remove
        or args.concentrate is not None
        or args.spell_slots is not None or args.slot_use or args.slot_restore
        or args.hit_dice_use or args.hit_dice_restore
        or args.sheet is not None or args.inventory_add or args.inventory_remove
        or args.ability_scores is not None or args.spells is not None
        or args.inspiration is not None
    )
    if _player_flags:
        if not args.player:
            print("Per-player flags require --player NAME", file=sys.stderr)
            sys.exit(1)
        player_update: dict = {"name": args.player}
        if args.hp:
            player_update["hp"] = {"current": args.hp[0], "max": args.hp[1]}
        if args.temp_hp is not None:
            player_update["hp"] = player_update.get("hp") or {}
            player_update["hp"]["temp"] = args.temp_hp
        if args.xp:
            player_update["xp"] = {"current": args.xp[0], "next": args.xp[1]}
        if args.second_wind is not None:
            player_update["second_wind"] = args.second_wind.lower() == "true"
        if args.conditions is not None:
            player_update["conditions"] = (
                [c.strip() for c in args.conditions.split(",") if c.strip()]
                if args.conditions.strip() else []
            )
        if args.conditions_add:
            player_update["_conditions_add"] = args.conditions_add
        if args.conditions_remove:
            player_update["_conditions_remove"] = args.conditions_remove
        if args.concentrate is not None:
            player_update["concentration"] = args.concentrate.strip() or None
        if args.spell_slots is not None:
            try:
                player_update["spell_slots"] = json.loads(args.spell_slots)
            except json.JSONDecodeError as e:
                print(f"Invalid spell-slots JSON: {e}", file=sys.stderr)
                sys.exit(1)
        if args.slot_use:
            player_update["_slot_use"] = args.slot_use
        if args.slot_restore:
            player_update["_slot_restore"] = args.slot_restore
        if args.sheet is not None:
            try:
                player_update["sheet"] = json.loads(args.sheet)
            except json.JSONDecodeError as e:
                print(f"Invalid sheet JSON: {e}", file=sys.stderr)
                sys.exit(1)
        if args.ability_scores is not None:
            try:
                raw = json.loads(args.ability_scores)
                # Accept shorthand {str:18, dex:16, ...} and expand to {str:{score:18,mod:"+N"}}
                expanded: dict = {}
                for stat, val in raw.items():
                    if isinstance(val, int):
                        mod = (val - 10) // 2
                        expanded[stat] = {"score": val, "mod": f"+{mod}" if mod >= 0 else str(mod)}
                    else:
                        expanded[stat] = val
                player_update["ability_scores"] = expanded
            except json.JSONDecodeError as e:
                print(f"Invalid ability-scores JSON: {e}", file=sys.stderr)
                sys.exit(1)
        if args.spells is not None:
            try:
                spells_data = json.loads(args.spells)
                # Merge into sheet.spells — uses _sheet_patch key so server merges rather than replaces
                player_update["_sheet_spells"] = spells_data
            except json.JSONDecodeError as e:
                print(f"Invalid spells JSON: {e}", file=sys.stderr)
                sys.exit(1)
        if args.inspiration is not None:
            player_update["inspiration"] = args.inspiration.lower() in ("true", "1", "yes")
        if args.inventory_add:
            player_update["_inventory_add"] = args.inventory_add
        if args.inventory_remove:
            player_update["_inventory_remove"] = args.inventory_remove
        if args.hit_dice_use:
            player_update["_hd_use"] = 1
        if args.hit_dice_restore:
            player_update["_hd_restore"] = args.hit_dice_restore
        payload.setdefault("players", []).append(player_update)

    # ── Factions ───────────────────────────────────────────────────────────────
    if args.factions is not None:
        try:
            payload["factions"] = json.loads(args.factions)
        except json.JSONDecodeError as e:
            print(f"Invalid factions JSON: {e}", file=sys.stderr)
            sys.exit(1)

    # ── Quests ─────────────────────────────────────────────────────────────────
    if args.quests is not None:
        try:
            payload["quests"] = json.loads(args.quests)
        except json.JSONDecodeError as e:
            print(f"Invalid quests JSON: {e}", file=sys.stderr)
            sys.exit(1)

    # ── Turn order ─────────────────────────────────────────────────────────────
    if args.turn_order:
        try:
            payload["turn_order"] = json.loads(args.turn_order)
        except json.JSONDecodeError as e:
            print(f"Invalid turn-order JSON: {e}", file=sys.stderr)
            sys.exit(1)

    if args.turn_current:
        # Partial update: just advance the pointer
        payload["turn_order"] = {"current": args.turn_current}
        if args.turn_round:
            payload["turn_order"]["round"] = args.turn_round

    if args.turn_round and not args.turn_current:
        payload["turn_order"] = {"round": args.turn_round}

    if args.turn_clear:
        payload["turn_order"] = None

    # ── Replace-players flag ───────────────────────────────────────────────────
    if args.replace_players:
        payload["replace_players"] = True

    # ── World time ─────────────────────────────────────────────────────────────
    if args.world_time:
        try:
            payload["world_time"] = json.loads(args.world_time)
        except json.JSONDecodeError as e:
            print(f"Invalid world-time JSON: {e}", file=sys.stderr)
            sys.exit(1)

    if args.autorun_waiting is not None:
        payload["autorun_waiting"] = args.autorun_waiting.lower() == "true"

    if args.autorun_cycle is not None:
        payload["autorun_cycle"] = {"interval": args.autorun_cycle, "ts": time.time()}

    if args.autorun_threshold is not None:
        # 0 = reset to auto (use player count); positive int = explicit minimum
        payload["autorun_threshold"] = args.autorun_threshold if args.autorun_threshold > 0 else None

    if args.set_campaign:
        payload["campaign"] = args.set_campaign

    if args.ruleset:
        rs = args.ruleset.strip()
        if rs not in ("2014", "2024"):
            print(f"--ruleset must be 2014 or 2024 (got {rs!r})", file=sys.stderr)
            sys.exit(2)
        payload["ruleset"] = rs

    # ── Clear display ─────────────────────────────────────────────────────────
    if args.clear:
        _send(FLASK_URL.replace("/stats", "/clear"), b"", _read_token())
        if not payload and not map_bodies:
            return

    rc = 0
    for body in map_bodies:
        body = dict(body)
        rc = _post_map(body, _read_token(), body.pop("_route", "/map")) or rc
        if rc:
            break
    if not payload:
        if not map_bodies:
            print("Nothing to push. Use --help for usage.", file=sys.stderr)
        sys.exit(rc)

    _send(FLASK_URL, json.dumps(payload).encode("utf-8"), _read_token())
    sys.exit(rc)


if __name__ == "__main__":
    main()
