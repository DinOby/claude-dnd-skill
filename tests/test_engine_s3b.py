"""Game-state engine, stage S3b: the engine leads hit points, conditions and spell slots.

WHY
===
After the shadow-mode evenings the engine becomes the source of the values a
fight changes most: hit points (and temporary HP), conditions, concentration,
death saves and spell slots. With `engine: an`:

- game.py writes the log first; when the log cannot be written nothing changes;
- every sheet carries a generated engine block with a checksum, and the ordinary
  lines (HP line, slot table) stay equal to the engine;
- tracker.py takes conditions from the engine and writes tracker.json only after
  the log took the change; expired effects are logged as ended;
- the display paths (send.py, push_stats.py) only show these values;
- game.py export is the way back.

Run from repo root:
    python3 -m unittest tests.test_engine_s3b -v
"""
from __future__ import annotations

import json
import os
import pathlib
import subprocess
import sys
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "skills" / "dnd" / "scripts"
sys.path.insert(0, str(SCRIPTS))

from engine import eventlog, lead, shadow, sheet, state as st   # noqa: E402
from tests.test_engine_s3a_fixes import _GameBase              # noqa: E402

ON = "## Session Flags\n- engine: an\n"


class _OnBase(_GameBase):
    def setUp(self):
        super().setUp()                                       # imported in shadow mode
        (self.d / "state.md").write_text(ON, encoding="utf-8")
        rc, out = self.game("bloecke", "-c", "test")
        self.assertEqual(rc, 0, out)

    def fig(self, name="Ilsa Morgentau") -> dict:
        return self.snapshot()["characters"][name]

    def tracker(self, *argv):
        env = dict(os.environ)
        env.pop(shadow.ENV_PARENT, None)
        env.update({"DND_CAMPAIGN_ROOT": str(self.base), "DND_RUNTIME_DIR": str(self.runtime),
                    "PYTHONIOENCODING": "utf-8"})
        return subprocess.run([sys.executable, str(SCRIPTS / "tracker.py"), "-c", "test", *argv],
                              capture_output=True, text=True, encoding="utf-8", env=env, timeout=60)


class BlockTests(_OnBase):
    def test_every_sheet_gets_an_intact_block_and_the_comparison_is_clean(self):
        for who in ("ilsa", "brakka"):
            self.assertEqual(lead.block_status(self.sheet_text(who)), lead.OK)
        self.assertIn("Zustände: poisoned", self.sheet_text())
        self.assertIn("Konzentration: Bless", self.sheet_text())
        self.assert_no_differences()

    def test_the_block_does_not_disturb_the_sheet_reader(self):
        v = sheet.parse(self.sheet_text("brakka"))
        self.assertEqual((v["hp"], v["hp_max"], v["temp_hp"]), (15, 20, 3))

    def test_a_block_edited_by_hand_is_reported(self):
        path = self.d / "characters" / "brakka.md"
        path.write_text(self.sheet_text("brakka").replace("TP 15 / 20", "TP 20 / 20"), encoding="utf-8")
        rc, out = self.game("abgleich", "-c", "test")
        self.assertIn("| Brakka Steinfaust | Engine-Block | – | Prüfsumme falsch | von Hand geändert |", out)

    def test_a_hp_line_edited_by_hand_is_reported(self):
        path = self.d / "characters" / "brakka.md"
        path.write_text(self.sheet_text("brakka").replace("**HP:** 15 / 20", "**HP:** 2 / 20"), encoding="utf-8")
        rc, out = self.game("abgleich", "-c", "test")
        self.assertIn("| Brakka Steinfaust | TP | 15 / 20 | 2 / 20 | von Hand geändert |", out)


class HitPointTests(_OnBase):
    def test_damage_takes_temporary_hp_first_and_everything_follows_the_engine(self):
        rc, out = self.game("bogen", "-c", "test", "Brakka", "--schaden", "5")
        self.assertEqual(rc, 0, out)
        fig = self.fig("Brakka Steinfaust")
        self.assertEqual((fig["hp"], fig["temp_hp"]), (13, 0))                  # 3 temp, then 2
        self.assertIn("**HP:** 13 / 20 | **Temp HP:** 0", self.sheet_text("brakka"))
        self.assertIn("TP 13 / 20 · Temp-TP 0", self.sheet_text("brakka"))
        self.assertEqual(self.pushes[-1]["players"][0]["hp"], {"current": 13, "max": 20, "temp": 0})
        self.assert_no_differences()

    def test_healing_stops_at_the_maximum(self):
        self.game("bogen", "-c", "test", "Brakka", "--heilung", "50")
        self.assertEqual(self.fig("Brakka Steinfaust")["hp"], 20)
        self.assert_no_differences()

    def test_no_log_no_change(self):
        before = self.sheet_text("brakka")
        with mock.patch.object(shadow, "record", return_value=0), self.assertRaises(SystemExit):
            self.game("bogen", "-c", "test", "Brakka", "--schaden", "5")
        self.assertEqual(self.sheet_text("brakka"), before)

    def test_display_paths_no_longer_change_hit_points(self):
        shadow.record(self.d, [("hp.set", {"who": "Brakka", "current": 1, "max": 20})], src="send")
        self.assertEqual(self.fig("Brakka Steinfaust")["hp"], 15)


class SpellSlotTests(_OnBase):
    def test_casting_updates_engine_table_and_block(self):
        self.game("bogen", "-c", "test", "Ilsa", "--zauber", "1")
        self.assertEqual(self.fig()["spell_slots"]["1"], {"max": 2, "used": 2})
        self.assertIn("| 1st | 2 | 2 |", self.sheet_text())
        self.assertIn("Zauberplätze: Grad 1: 0/2", self.sheet_text())
        self.assert_no_differences()


class ConditionTests(_OnBase):
    def test_tracker_writes_the_log_and_the_block(self):
        res = self.tracker("condition", "add", "Ilsa Morgentau", "prone")
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual(self.fig()["conditions"], ["poisoned", "prone"])
        self.assertIn("Zustände: poisoned, prone", self.sheet_text())
        self.assert_no_differences()

    def test_the_engine_wins_over_a_stale_tracker_json(self):
        stale = json.loads((self.d / "tracker.json").read_text(encoding="utf-8"))
        stale["ilsa"]["conditions"] = []                      # someone edited tracker.json
        (self.d / "tracker.json").write_text(json.dumps(stale), encoding="utf-8")
        self.tracker("condition", "add", "Ilsa Morgentau", "prone")
        saved = json.loads((self.d / "tracker.json").read_text(encoding="utf-8"))
        self.assertEqual(saved["ilsa morgentau"]["conditions"], ["poisoned", "prone"])   # one entry, full name
        self.assertNotIn("ilsa", saved)
        self.assert_no_differences()

    def test_an_expiring_effect_is_logged_as_ended(self):
        self.tracker("effect", "start", "Ilsa Morgentau", "Segen", "1r", "conc")
        self.tracker("effect", "tick", "Ilsa Morgentau")
        fig = self.fig()
        self.assertNotIn("Segen", [e["name"] for e in fig["effects"]])
        self.assertIsNone(fig["concentration"])
        self.assert_no_differences()


class WayBackTests(_OnBase):
    def test_export_then_aus_leaves_consistent_sheets(self):
        self.game("bogen", "-c", "test", "Brakka", "--schaden", "5")
        self.tracker("condition", "add", "Ilsa Morgentau", "prone")
        rc, out = self.game("export", "-c", "test")
        self.assertEqual(rc, 0, out)
        for who in ("ilsa", "brakka"):
            self.assertEqual(lead.block_status(self.sheet_text(who)), lead.MISSING)
        self.assertIn("**HP:** 13 / 20", self.sheet_text("brakka"))
        tracker = json.loads((self.d / "tracker.json").read_text(encoding="utf-8"))
        self.assertEqual(tracker["ilsa morgentau"]["conditions"], ["poisoned", "prone"])
        (self.d / "state.md").write_text("## Session Flags\n- engine: schatten\n", encoding="utf-8")
        self.assert_no_differences()


if __name__ == "__main__":
    unittest.main()
