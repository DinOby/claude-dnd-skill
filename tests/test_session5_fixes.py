"""Fixes after session 5 (12.11.2026) — package A.

A1  An XP award keeps its reason in words (the --note text), next to kind and
    difficulty, in the ledger and the game-state log. New kind: quest.
A2  The display follows sheet changes (armour class, inventory, features) without
    a restart: game.py sends them, and a sheet watcher catches hand edits.
A3  A requested roll is rolled on the DM's terms: die, bonus and advantage come
    from the request, whatever the dialog sends.
A4  Placeholders such as "keine (Ungerüstete Verteidigung)" are no items.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import pathlib
import sys
import tempfile
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "skills" / "dnd" / "scripts"
sys.path.insert(0, str(SCRIPTS))
_spec = importlib.util.spec_from_file_location("xp_s5", SCRIPTS / "xp.py")
xp = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(xp)


def _award_args(**kw):
    base = dict(campaign="c", characters="Alrik,Bela", difficulty=None, type=None,
                monsters=None, note=None)
    base.update(kw)
    return argparse.Namespace(**base)


class XpReasonTests(unittest.TestCase):
    def setUp(self):
        self._d = tempfile.TemporaryDirectory()
        self.base = pathlib.Path(self._d.name)
        (self.base / "c" / "characters").mkdir(parents=True)
        self.recorded = []
        patches = [
            mock.patch.object(xp, "CAMPAIGNS_DIR", self.base),
            mock.patch.object(xp, "_find_char_path", lambda c, n: self.base / f"{n}.md"),
            mock.patch.object(xp, "_read_char_state", lambda p: (100, 2)),
            mock.patch.object(xp, "_write_char_xp", lambda p, new, lvl: False),
            mock.patch.object(xp, "_sheet_title", lambda p: p.stem),
            mock.patch.object(xp, "_push_xp_display", lambda *a, **k: None),
            mock.patch.object(xp, "_shadow", None),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def tearDown(self):
        self._d.cleanup()

    def _rows(self):
        return xp._read_ledger("c")

    def test_the_reason_is_stored_not_just_the_difficulty(self):
        xp.cmd_award(_award_args(monsters="bandit:1/8:4", type="noncombat",
                                 note="Brenn-Knechte überredet"))
        rows = self._rows()
        self.assertEqual({r["note"] for r in rows}, {"Brenn-Knechte überredet"})
        self.assertEqual({r["kind"] for r in rows}, {"noncombat"})
        self.assertTrue(all(r.get("difficulty") for r in rows))

    def test_without_a_note_the_old_label_stays(self):
        xp.cmd_award(_award_args(difficulty="medium", type="combat"))
        self.assertEqual({r["note"] for r in self._rows()}, {"medium combat"})

    def test_quest_award_is_difficulty_rated_and_kept_as_quest(self):
        xp.cmd_award(_award_args(type="quest", difficulty="hard",
                                 note="Die vermissten Bergleute gefunden"))
        rows = self._rows()
        self.assertEqual({r["kind"] for r in rows}, {"quest"})
        self.assertEqual({r["awarded"] for r in rows}, {xp._xp_per_player("hard", 2)})
        self.assertEqual({r["note"] for r in rows}, {"Die vermissten Bergleute gefunden"})

    def test_quest_without_a_name_is_refused(self):
        with self.assertRaises(SystemExit):
            xp.cmd_award(_award_args(type="quest", difficulty="hard"))
        self.assertEqual(self._rows(), [])

    def test_quest_with_monsters_is_refused(self):
        with self.assertRaises(SystemExit):
            xp.cmd_award(_award_args(type="quest", monsters="goblin:1/4:2", note="x"))

    def test_the_engine_event_carries_reason_kind_and_difficulty(self):
        shadow = mock.Mock()
        with mock.patch.object(xp, "_shadow", shadow):
            xp.cmd_award(_award_args(difficulty="medium", type="noncombat",
                                     note="Verhör des Spitzels"))
        events = shadow.record.call_args[0][1]
        for _type, data in events:
            self.assertEqual(data["note"], "Verhör des Spitzels")
            self.assertEqual(data["kind"], "noncombat")
            self.assertEqual(data["difficulty"], "medium")

    def test_the_event_schema_accepts_the_new_fields(self):
        sys.path.insert(0, str(SCRIPTS))
        from engine import protocol
        event = {"v": 1, "seq": 1, "t": "2026-11-13T10:00:00+00:00", "by": "dm", "src": "xp",
                 "type": "xp.award", "data": {"who": "Alrik", "awarded": 50, "total_after": 150,
                                               "note": "Quest", "kind": "quest", "difficulty": "hard"}}
        self.assertEqual(protocol.check_event(event), [])
        event["data"]["kind"] = "erfunden"
        self.assertNotEqual(protocol.check_event(event), [])


SHEET = """# Alrik Eisenfaust

## Combat Stats
- **AC:** 15
- **HP:** 20 / 32

## Features & Traits
- **Rage (Kampfrausch)** — Bonusaktion, +2 Schaden. **Rages: 2/3**
- **Pfad des Berserkers** — Stufe 3. *Raserei:* +2W6 beim ersten Treffer.
- Fertigkeiten: Athletik

## Equipment & Inventory
**Weapons:**
- Zweihandaxt
- Speer

**Armour:**
- keine (Ungerüstete Verteidigung)

**Currency:** 118gp 5sp 0cp
"""


class PlaceholderTests(unittest.TestCase):
    def setUp(self):
        from engine import sheet
        self.sheet = sheet

    def test_placeholder_lines_are_no_items(self):
        for text in ("keine", "Keine.", "keine (Ungerüstete Verteidigung)", "—", "none", "nichts: leer"):
            self.assertTrue(self.sheet.is_placeholder(text), text)
        for text in ("Speer", "Kein Problem-Schwert", "Keinohrhase", "Schild (+2 RK)"):
            self.assertFalse(self.sheet.is_placeholder(text), text)

    def test_the_sheet_parser_skips_them(self):
        inv = self.sheet.parse(SHEET)["inventory"]
        self.assertEqual(inv["Armour"], [])
        self.assertEqual(inv["Weapons"], ["Zweihandaxt", "Speer"])

    def test_an_old_import_with_placeholders_replays_without_them(self):
        from engine import state
        events = [{"v": 1, "seq": 1, "t": "x", "by": "dm", "src": "game", "type": "import",
                   "data": {"characters": {"Alrik": {"name": "Alrik", "inventory": {
                       "Armour": ["keine (Ungerüstete Verteidigung)"], "Weapons": ["Speer"]}}}}},
                  {"v": 1, "seq": 2, "t": "x", "by": "dm", "src": "bogen", "type": "inventory.add",
                   "data": {"who": "Alrik", "item": "keine"}}]
        fig = state.replay(events)["characters"]["Alrik"]
        self.assertEqual(fig["inventory"], {"Armour": [], "Weapons": ["Speer"]})

    def test_features_are_read_with_name_and_text(self):
        feats = self.sheet.features(SHEET)
        self.assertEqual([f["name"] for f in feats], ["Rage (Kampfrausch)", "Pfad des Berserkers"])
        self.assertIn("Raserei:", feats[1]["text"])
        self.assertNotIn("*", feats[1]["text"])


class GameDisplayPushTests(unittest.TestCase):
    def test_display_player_carries_ac_inventory_and_features(self):
        spec = importlib.util.spec_from_file_location("game_s5", SCRIPTS / "game.py")
        game = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(game)
        from engine import sheet
        fig = dict(sheet.parse(SHEET), name="Alrik Eisenfaust")
        p = game.display_player(fig, SHEET)
        self.assertEqual(p["ac"], 15)
        self.assertEqual(p["sheet"]["inventory"], ["Zweihandaxt", "Speer"])
        self.assertEqual(p["sheet"]["features"][0]["name"], "Rage (Kampfrausch)")
        self.assertEqual(p["sheet"]["currency"], "118 GM 5 SM 0 KM")


class SheetWatcherTests(unittest.TestCase):
    def setUp(self):
        sys.path.insert(0, str(ROOT / "skills" / "dnd" / "display"))
        import sheet_watch
        self.sw = sheet_watch
        self._d = tempfile.TemporaryDirectory()
        self.camp = pathlib.Path(self._d.name) / "c"
        (self.camp / "characters").mkdir(parents=True)
        self.path = self.camp / "characters" / "alrik.md"
        self.path.write_text(SHEET, encoding="utf-8")
        self.applied = []
        self.accept = True
        self.w = sheet_watch.SheetWatcher(lambda: self.camp, self._apply, interval=60)

    def tearDown(self):
        self._d.cleanup()

    def _apply(self, update):
        self.applied.append(update)
        return self.accept

    def test_first_scan_sends_the_sheet_values_but_never_hit_points(self):
        self.assertEqual(self.w.scan(), ["Alrik Eisenfaust"])
        u = self.applied[0]
        self.assertEqual(u["ac"], 15)
        self.assertEqual(u["sheet"]["inventory"], ["Zweihandaxt", "Speer"])
        for engine_led in ("hp", "conditions", "spell_slots", "effects"):
            self.assertNotIn(engine_led, u)

    def test_an_unchanged_sheet_is_not_sent_again(self):
        self.w.scan()
        self.assertEqual(self.w.scan(), [])

    def test_a_hand_edit_reaches_the_display(self):
        self.w.scan()
        import os
        text = SHEET.replace("- **AC:** 15", "- **AC:** 12").replace("- Speer\n", "")
        self.path.write_text(text, encoding="utf-8")
        st = self.path.stat()
        os.utime(self.path, ns=(st.st_atime_ns, st.st_mtime_ns + 5_000_000_000))
        self.assertEqual(self.w.scan(), ["Alrik Eisenfaust"])
        self.assertEqual(self.applied[-1]["ac"], 12)
        self.assertEqual(self.applied[-1]["sheet"]["inventory"], ["Zweihandaxt"])

    def test_a_player_not_yet_on_the_display_is_retried(self):
        self.accept = False
        self.assertEqual(self.w.scan(), [])
        self.accept = True
        self.assertEqual(self.w.scan(), ["Alrik Eisenfaust"])


class AppSheetMergeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from tests.app_isolation import import_app
        cls.mod = import_app("dnd_display_app_s5")

    def test_merge_updates_sheet_values_and_keeps_engine_values(self):
        self.mod._current_stats = {"players": [{
            "name": "Alrik Eisenfaust", "ac": 15, "hp": {"current": 9, "max": 32},
            "sheet": {"attacks": [{"name": "Axt"}], "inventory": ["Speer", "Alt"]}}]}
        with mock.patch.object(self.mod, "_broadcast") as bc,                 mock.patch.object(self.mod, "_persist_stats"):
            ok = self.mod._apply_sheet_update({"name": "Alrik Eisenfaust", "ac": 12,
                                               "sheet": {"inventory": ["Speer"], "features": []}})
        self.assertTrue(ok)
        p = self.mod._current_stats["players"][0]
        self.assertEqual(p["ac"], 12)
        self.assertEqual(p["hp"], {"current": 9, "max": 32})
        self.assertEqual(p["sheet"]["attacks"], [{"name": "Axt"}])
        self.assertEqual(p["sheet"]["inventory"], ["Speer"])
        bc.assert_called_once()

    def test_unknown_player_is_not_added(self):
        self.mod._current_stats = {"players": [{"name": "Bela"}]}
        with mock.patch.object(self.mod, "_broadcast") as bc:
            self.assertFalse(self.mod._apply_sheet_update({"name": "Fremder", "ac": 10}))
        self.assertEqual(len(self.mod._current_stats["players"]), 1)
        bc.assert_not_called()

    def test_the_watcher_is_not_started_on_import(self):
        self.assertIsNone(self.mod._sheet_watcher._thread)


class DiceTermsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from tests.app_isolation import import_app
        cls.mod = import_app("dnd_display_app_s5_dice")
        cls.mod._token_ok = lambda: True
        cls.mod._host_ok = lambda: True
        cls.client = cls.mod.app.test_client()

    def setUp(self):
        self.mod._dice_pending.clear()
        self.mod._dice_answered.clear()
        self.mod._dice_meta.clear()

    def _request(self, **kw):
        body = {"character": "Alrik", "spec": "1d20", "modifier": 3, "advantage": "normal",
                "label": "Tierführung"}
        body.update(kw)
        r = self.client.post("/dice-request", json=body)
        self.assertEqual(r.status_code, 200)
        return r.get_json()["request_id"]

    def _roll(self, **kw):
        body = {"character": "Alrik", "spec": "1d20", "modifier": 0, "advantage": "normal"}
        body.update(kw)
        return self.client.post("/player-input/dice", json=body)

    def test_a_changed_bonus_is_ignored(self):
        rid = self._request(modifier=3)
        res = self._roll(request_id=rid, modifier=4).get_json()
        self.assertEqual(res["modifier"], 3)
        self.assertEqual(res["total"], res["subtotal"] + 3)

    def test_advantage_cannot_replace_disadvantage(self):
        rid = self._request(modifier=2, advantage="disadvantage")
        res = self._roll(request_id=rid, modifier=2, advantage="advantage").get_json()
        self.assertEqual(res["advantage"], "disadvantage")
        self.assertEqual(res["kept"][0], min(res["both"]))

    def test_the_die_comes_from_the_request(self):
        rid = self._request(spec="1d6", modifier=0)
        res = self._roll(request_id=rid, spec="1d20").get_json()
        self.assertEqual(res["spec"], "1d6")
        self.assertLessEqual(res["subtotal"], 6)

    def test_an_unknown_request_is_refused(self):
        r = self._roll(request_id="abcdef123456", modifier=9)
        self.assertEqual(r.status_code, 410)

    def test_a_free_roll_keeps_the_players_choice(self):
        res = self._roll(modifier=5, advantage="advantage").get_json()
        self.assertEqual((res["modifier"], res["advantage"]), (5, "advantage"))

    def test_the_request_still_completes(self):
        rid = self._request()
        self._roll(request_id=rid, modifier=7)
        status = self.client.get(f"/dice-request/{rid}").get_json()
        self.assertTrue(status.get("complete"))


if __name__ == "__main__":
    unittest.main()
