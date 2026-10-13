"""Game-state engine, stage S2: import of the sheets and the engine/Markdown comparison.

WHY
===
S2 takes the numbers of the character sheets into game/state.json once — only
after a dry run the host has read, and only after a verified backup — and from
then on compares engine and Markdown on every load without correcting anything.
The sheets here are invented (anonymised); tests never read a real campaign.

Run from repo root:
    python3 -m unittest tests.test_engine_import -v
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import pathlib
import sys
import tempfile
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "skills" / "dnd" / "scripts"
sys.path.insert(0, str(SCRIPTS))

from engine import eventlog, importer, shadow, sheet, state as st   # noqa: E402

SHEET_A = """# Brakka Steinfaust
**Player:** Testspieler A  **Campaign:** test  **Last Updated:** 2026-10-13

## Identity
- **Race:** Zwerg | **Class:** Kämpfer | **Level:** 2 | **Background:** Soldat
- **Alignment:** Neutral | **XP:** 400 / 900

## Combat Stats
- **HP:** 15 / 20 | **Temp HP:** 3
- **AC:** 16 (Kettenhemd) | **Initiative:** +1 | **Speed:** 25 ft
- **Hit Dice:** 2d10 (remaining: 1)
- **Death Saves:** Successes: 0 | Failures: 0

## Spell Slots (if applicable)
*(keine)*

## Features & Traits
### Kämpfer
- **Second Wind (Durchschnaufen)** — Bonusaktion. **1/1**
- **Action Surge** — kein Zähler hier

## Equipment & Inventory
**Weapons:**
- Streitaxt
- 2 Wurfspeere

**Adventuring Gear:**
- Seil (50 ft)

**Currency:** 25gp 3sp 0cp

## Notes (Sitzung 1)
- Durchschnaufen 1/1 genutzt, 3 temp TP von Segen.
"""

SHEET_B = """# Ilsa Morgentau
## Identity
- **Race:** Mensch | **Class:** Klerikerin | **Level:** 1 | **Background:** Akolythin
- **Alignment:** Gut | **XP:** 100 / 300

## Combat Stats
- **HP:** 9 / 9 | **Temp HP:** 0
- **AC:** 13 | **Initiative:** +0 | **Speed:** 30 ft
- **Hit Dice:** 1d8 (remaining: 1)
- **Death Saves:** Successes: 0 | Failures: 0

## Spell Slots (if applicable)
| Level | Total | Used |
|-------|-------|------|
| 1st | 2 | 1 |

## Equipment & Inventory
**Weapons:**
- Streitkolben

**Currency:** 8gp 0sp 5cp

## Notes (Sitzung 1)
- Heroic Inspiration ✓
"""


class Base(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.base = pathlib.Path(tmp.name)
        self.d = self.base / "campaigns" / "test"
        (self.d / "characters").mkdir(parents=True)
        (self.d / "characters" / "brakka.md").write_text(SHEET_A, encoding="utf-8")
        (self.d / "characters" / "ilsa.md").write_text(SHEET_B, encoding="utf-8")
        (self.d / "state.md").write_text("## Session Flags\n- engine: schatten\n", encoding="utf-8")
        (self.d / "tracker.json").write_text(json.dumps({"ilsa": {
            "name": "Ilsa", "conditions": ["poisoned"], "concentration": "Bless",
            "effects": [{"name": "Bless", "concentration": True, "duration_type": "rounds"}],
            "death_saves": {"successes": 0, "failures": 0, "stable": False}}}), encoding="utf-8")
        self.runtime = self.base / ".runtime"
        self.runtime.mkdir()
        (self.runtime / "stats.json").write_text('{"players": []}', encoding="utf-8")

    def game(self, *argv):
        import importlib.util
        spec = importlib.util.spec_from_file_location("game_import_cli", SCRIPTS / "game.py")
        game = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(game)
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()), \
                mock.patch.object(game, "find_campaign", return_value=self.d), \
                mock.patch.object(game, "runtime_dir", return_value=self.runtime):
            rc = game.main(list(argv))
        return rc, out.getvalue()


class SheetParserTests(unittest.TestCase):
    def test_the_engine_values_are_read_and_nothing_is_guessed(self):
        v = sheet.parse(SHEET_A)
        self.assertEqual((v["name"], v["level"], v["xp"], v["xp_next"]), ("Brakka Steinfaust", 2, 400, 900))
        self.assertEqual((v["hp"], v["hp_max"], v["temp_hp"], v["ac"]), (15, 20, 3, 16))
        self.assertEqual(v["hit_dice"], {"die": "d10", "total": 2, "remaining": 1})
        self.assertEqual(v["currency"], {"pp": 0, "gp": 25, "ep": 0, "sp": 3, "cp": 0})
        self.assertEqual(v["inventory"], {"Weapons": ["Streitaxt", "2 Wurfspeere"],
                                          "Adventuring Gear": ["Seil (50 ft)"]})
        self.assertEqual(v["resources"], {"Second Wind (Durchschnaufen)": {"left": 1, "max": 1}})
        self.assertFalse(v["inspiration"])
        b = sheet.parse(SHEET_B)
        self.assertEqual(b["spell_slots"], {"1": {"max": 2, "used": 1}})
        self.assertTrue(b["inspiration"])
        self.assertIsNone(sheet.parse("# Leer\n")["hp"])


class ImportTests(Base):
    def test_the_dry_run_shows_a_table_and_writes_nothing(self):
        before = sorted(p.name for p in self.d.rglob("*"))
        rc, out = self.game("import", "-c", "test")
        self.assertEqual(rc, 0)
        self.assertIn("| TP | 15 / 20 | 9 / 9 |", out)
        self.assertIn("| Zustände | keine | poisoned |", out)                       # from tracker.json
        self.assertIn("Durchschnaufen 1/1 genutzt", out)                             # hint from the notes
        self.assertIn("Probelauf – nichts geschrieben", out)
        self.assertEqual(sorted(p.name for p in self.d.rglob("*")), before)
        self.assertEqual(list(self.base.joinpath("campaigns").iterdir()), [self.d])   # no backup either

    def test_yes_backs_up_first_then_writes_the_import_event_and_state(self):
        rc, out = self.game("import", "-c", "test", "--ja")
        self.assertEqual(rc, 0, out)
        backups = [p for p in self.d.parent.iterdir() if ".sicherung-vor-engine-" in p.name]
        self.assertEqual(len(backups), 1)
        self.assertEqual((backups[0] / "characters" / "brakka.md").read_text(encoding="utf-8"), SHEET_A)
        self.assertTrue((backups[0] / "_display-stats.json").is_file())
        events, skipped = eventlog.read(self.d)
        self.assertEqual(skipped, [])
        self.assertEqual([e["type"] for e in events], ["import"])
        snap = st.read(self.d)
        self.assertEqual(snap["characters"]["Brakka Steinfaust"]["hp"], 15)
        self.assertEqual(snap["characters"]["Ilsa Morgentau"]["concentration"], "Bless")
        self.assertEqual(snap["imported_seq"], 1)
        self.assertEqual(SHEET_A, (self.d / "characters" / "brakka.md").read_text(encoding="utf-8"))  # sheets untouched

    def test_a_failed_backup_writes_nothing(self):
        with mock.patch.object(importer.shutil, "copytree", side_effect=OSError("Platte voll")):
            with self.assertRaises(OSError):
                self.game("import", "-c", "test", "--ja")
        self.assertFalse(eventlog.log_path(self.d).exists())
        self.assertIsNone(st.read(self.d))


class ReplayTests(Base):
    def test_replaying_the_log_gives_the_snapshot(self):
        self.game("import", "-c", "test", "--ja")
        shadow.record(self.d, [("hp.set", {"who": "Brakka", "current": 9}),        # first name resolves
                               ("condition.add", {"who": "Brakka", "name": "prone"}),
                               ("slot.use", {"who": "Ilsa Morgentau", "level": 1}),
                               ("hp.set", {"who": "Goblin 1", "current": 3, "max": 7}),
                               ("turn.order", {"order": ["Goblin 1", "Brakka"], "current": "Goblin 1", "round": 1}),
                               ("xp.award", {"who": "Ilsa", "awarded": 50, "total_after": 150})], src="test")
        rc, _ = self.game("stand", "-c", "test")
        self.assertEqual(rc, 0)
        snap = st.read(self.d)
        events, _ = eventlog.read(self.d)
        self.assertEqual(st.replay(events), snap)
        brakka, ilsa = snap["characters"]["Brakka Steinfaust"], snap["characters"]["Ilsa Morgentau"]
        self.assertEqual((brakka["hp"], brakka["conditions"]), (9, ["prone"]))
        self.assertEqual((ilsa["spell_slots"]["1"]["used"], ilsa["xp"]), (2, 150))
        self.assertEqual(snap["others"]["Goblin 1"]["hp"], 3)
        self.assertEqual(snap["turn"]["current"], "Goblin 1")

    def test_only_the_last_import_counts(self):
        self.game("import", "-c", "test", "--ja")
        shadow.record(self.d, [("hp.set", {"who": "Brakka", "current": 1})], src="test")
        self.game("import", "-c", "test", "--ja")
        self.assertEqual(st.read(self.d)["characters"]["Brakka Steinfaust"]["hp"], 15)


class CompareTests(Base):
    def test_no_import_means_nothing_to_compare(self):
        rc, out = self.game("abgleich", "-c", "test")
        self.assertEqual(rc, 0)
        self.assertIn("Noch kein Import", out)

    def test_matching_engine_and_sheets(self):
        self.game("import", "-c", "test", "--ja")
        rc, out = self.game("abgleich", "-c", "test")
        self.assertIn("stimmen überein", out)

    def test_differences_are_reported_not_corrected(self):
        self.game("import", "-c", "test", "--ja")
        shadow.record(self.d, [("hp.set", {"who": "Brakka", "current": 9})], src="test")   # the log says 9 …
        changed = SHEET_A.replace("- Seil (50 ft)", "- Seil (50 ft)\n- Fackel")               # … sheet: new item
        (self.d / "characters" / "brakka.md").write_text(changed, encoding="utf-8")
        rc, out = self.game("abgleich", "-c", "test")
        self.assertEqual(rc, 0)
        self.assertIn("| Brakka Steinfaust | TP | 9 / 20 | 15 / 20 |", out)
        self.assertIn("| Brakka Steinfaust | Inventar | 0 nur in Engine | 1 nur im Bogen |", out)
        self.assertEqual((self.d / "characters" / "brakka.md").read_text(encoding="utf-8"), changed)
        self.assertEqual(st.read(self.d)["characters"]["Brakka Steinfaust"]["hp"], 9)
        self.assertIn("SPIELSTAND-LOGBUCH · Daten", out)


class ActiveCampaignTests(unittest.TestCase):
    def test_active_campaign_json_is_used_when_dot_campaign_is_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            (root / "campaigns" / "test").mkdir(parents=True)
            rt = root / ".runtime"
            rt.mkdir()
            (rt / "active-campaign.json").write_text('{"name": "test"}', encoding="utf-8")
            with mock.patch.dict(os.environ, {"DND_CAMPAIGN_ROOT": str(root), "DND_RUNTIME_DIR": str(rt)}):
                self.assertEqual(shadow.active_campaign_dir(), root / "campaigns" / "test")
                (rt / "active-campaign.json").write_text('{"name": "../draussen"}', encoding="utf-8")
                self.assertIsNone(shadow.active_campaign_dir())


if __name__ == "__main__":
    unittest.main()
