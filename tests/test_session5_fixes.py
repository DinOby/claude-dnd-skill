"""Fixes after session 5 (12.11.2026) — package A.

A1  An XP award keeps its reason in words (the --note text), next to kind and
    difficulty, in the ledger and the game-state log. New kind: quest.
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


if __name__ == "__main__":
    unittest.main()
