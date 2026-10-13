"""Game-state engine, stage S3a: every path that changes a value lands in the log.

WHY
===
The first shadow-mode evening (S2) ended with 8 differences between engine and
sheets. Each came from a path the log did not see: send.py's --stat-*,
--effect-* and --inspiration-* flags, and changes written straight into the
Markdown sheets. S3a closes those paths — the engine still only listens, the
sheets stay authoritative — and the comparison now says *why* something differs:
"von Hand geändert" (the sheet was edited past the log) or "Bogen nicht
nachgezogen" (the display changed, the sheet was not updated).

One test per change path shows that the change reaches the log.

Run from repo root:
    python3 -m unittest tests.test_engine_s3a -v
"""
from __future__ import annotations

import contextlib
import importlib.util
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
DISPLAY = ROOT / "skills" / "dnd" / "display"
sys.path.insert(0, str(SCRIPTS))

from engine import eventlog, importer, protocol, shadow, sheet, state as st   # noqa: E402
from tests.test_engine_import import SHEET_A, Base                           # noqa: E402


def _load(path: pathlib.Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class SendPathTests(unittest.TestCase):
    """send.py flags that change values: each one is logged with src 'send'."""

    FLAGS = [   # (argv, expected (type, data))
        (["--stat-hp", "Grosch:6:14"], ("hp.set", {"who": "Grosch", "current": 6, "max": 14})),
        (["--stat-temp-hp", "Grosch:2"], ("hp.temp", {"who": "Grosch", "value": 2})),
        (["--stat-slot-use", "Mordos:1"], ("slot.use", {"who": "Mordos", "level": 1})),
        (["--stat-slot-restore", "Mordos:1"], ("slot.restore", {"who": "Mordos", "level": 1})),
        (["--stat-condition-add", "Zarith:Vergiftet"], ("condition.add", {"who": "Zarith", "name": "Vergiftet"})),
        (["--stat-condition-remove", "Zarith:Vergiftet"], ("condition.remove", {"who": "Zarith", "name": "Vergiftet"})),
        (["--stat-concentrate", "Mordos:Segnen"], ("concentration.set", {"who": "Mordos", "spell": "Segnen"})),
        (["--stat-inventory-add", "Mordos:Messingpfeife"], ("inventory.add", {"who": "Mordos", "item": "Messingpfeife"})),
        (["--stat-inventory-remove", "Mordos:Messingpfeife"], ("inventory.remove", {"who": "Mordos", "item": "Messingpfeife"})),
        (["--effect-start", "Grosch:Kampfrausch:10r"],
         ("effect.start", {"who": "Grosch", "name": "Kampfrausch", "conc": False, "duration": "10r"})),
        (["--effect-end", "Grosch:Kampfrausch"], ("effect.end", {"who": "Grosch", "name": "Kampfrausch"})),
        (["--inspiration-award", "Zarith"], ("inspiration.set", {"who": "Zarith", "value": True})),
        (["--inspiration-spend", "Zarith"], ("inspiration.set", {"who": "Zarith", "value": False})),
    ]

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.base = pathlib.Path(tmp.name)
        self.d = self.base / "campaigns" / "test"
        self.d.mkdir(parents=True)
        (self.d / "state.md").write_text("## Session Flags\n- engine: schatten\n", encoding="utf-8")
        rt = self.base / ".runtime"
        rt.mkdir()
        (rt / ".campaign").write_text("test", encoding="utf-8")
        env = mock.patch.dict(os.environ, {"DND_CAMPAIGN_ROOT": str(self.base), "DND_RUNTIME_DIR": str(rt)})
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop(shadow.ENV_PARENT, None)

    def _send(self, argv, display_up=True):
        send = _load(DISPLAY / "send.py", "_send_s3a_under_test")

        def fake_post(url, data, token):
            send._SEND_LOG.append({"endpoint": send._endpoint_label(url), "ok": display_up,
                                   **({} if display_up else {"reason": "display offline"})})
            return display_up

        with mock.patch.object(send, "_post", fake_post), \
             mock.patch.object(send, "_read_token", lambda: ""), \
             mock.patch.object(sys, "argv", ["send.py", *argv]), \
             mock.patch.object(sys, "stdin", io.StringIO("")), \
             contextlib.redirect_stderr(io.StringIO()), contextlib.suppress(SystemExit):
            send.main()
        return eventlog.read(self.d)[0]

    def test_every_value_flag_lands_in_the_log(self):
        for argv, (etype, data) in self.FLAGS:
            with self.subTest(flag=argv[0]):
                before = len(eventlog.read(self.d)[0])
                events = self._send(argv)[before:]
                self.assertEqual([(e["type"], e["src"], e["data"]) for e in events], [(etype, "send", data)])
                self.assertEqual(protocol.check_event(events[0]), [])

    def test_a_change_is_logged_even_when_the_display_is_offline(self):
        events = self._send(["--stat-hp", "Grosch:5:14"], display_up=False)
        self.assertEqual([(e["type"], e["data"]["current"]) for e in events], [("hp.set", 5)])

    def test_engine_aus_writes_nothing(self):
        (self.d / "state.md").write_text("## Session Flags\n- engine: aus\n", encoding="utf-8")
        self.assertEqual(self._send(["--stat-hp", "Grosch:5:14"]), [])

    def test_narration_without_value_flags_writes_nothing(self):
        send = _load(DISPLAY / "send.py", "_send_s3a_plain")
        with mock.patch.object(send, "_post", lambda *a: True), \
             mock.patch.object(send, "_read_token", lambda: ""), \
             mock.patch.object(sys, "argv", ["send.py"]), \
             mock.patch.object(sys, "stdin", io.StringIO("Die Glocken schlagen neun.\n")), \
             contextlib.redirect_stderr(io.StringIO()), contextlib.suppress(SystemExit):
            send.main()
        self.assertEqual(eventlog.read(self.d)[0], [])


class SheetPathTests(Base):
    """game.py bogen: the sheet changes and the same change is logged with src 'bogen'."""

    def setUp(self):
        super().setUp()
        self.game("import", "-c", "test", "--ja")

    def _sheet(self) -> str:
        return (self.d / "characters" / "brakka.md").read_text(encoding="utf-8")

    def _new_events(self, *argv):
        before = len(eventlog.read(self.d)[0])
        rc, out = self.game("bogen", "-c", "test", "Brakka", *argv)
        self.assertEqual(rc, 0, out)
        return [(e["type"], e["src"], e["data"]) for e in eventlog.read(self.d)[0][before:]]

    def test_hit_points(self):
        self.assertEqual(self._new_events("--tp", "9/20"),
                         [("hp.set", "bogen", {"who": "Brakka Steinfaust", "current": 9, "max": 20})])
        self.assertIn("**HP:** 9 / 20", self._sheet())

    def test_temporary_hit_points(self):
        self.assertEqual(self._new_events("--temp", "0"),
                         [("hp.temp", "bogen", {"who": "Brakka Steinfaust", "value": 0})])
        self.assertIn("**Temp HP:** 0", self._sheet())

    def test_coins(self):
        events = self._new_events("--muenzen", "30 GM 1 SM")
        self.assertEqual(events, [("currency.set", "bogen", {"who": "Brakka Steinfaust", "pp": 0, "gp": 30,
                                                             "ep": 0, "sp": 1, "cp": 0})])
        self.assertIn("**Currency:** 30gp 1sp 0cp", self._sheet())

    def test_limited_feature(self):
        self.assertEqual(self._new_events("--ressource", "Second Wind=0/1"),
                         [("resource.set", "bogen", {"who": "Brakka Steinfaust", "name": "Second Wind",
                                                     "left": 0, "max": 1})])
        self.assertIn("Bonusaktion. **0/1**", self._sheet())

    def test_inspiration(self):
        self.assertEqual(self._new_events("--inspiration", "ja"),
                         [("inspiration.set", "bogen", {"who": "Brakka Steinfaust", "value": True})])
        self.assertTrue(sheet.parse(self._sheet())["inspiration"])
        self._new_events("--inspiration", "nein")
        self.assertFalse(sheet.parse(self._sheet())["inspiration"])

    def test_inventory_in_and_out(self):
        self.assertEqual(self._new_events("--plus", "Fackel", "--minus", "Seil (50 ft)"),
                         [("inventory.remove", "bogen", {"who": "Brakka Steinfaust", "item": "Seil (50 ft)"}),
                          ("inventory.add", "bogen", {"who": "Brakka Steinfaust", "item": "Fackel"})])
        text = self._sheet()
        self.assertIn("**Neu erhalten:**\n- Fackel", text)
        self.assertNotIn("- Seil (50 ft)", text)
        self.assertIn("**Currency:** 25gp 3sp 0cp", text)            # untouched

    def test_hit_dice(self):
        self.assertEqual(self._new_events("--trefferwuerfel", "2"),
                         [("hitdice.restore", "bogen", {"who": "Brakka Steinfaust", "n": 1})])

    def test_spell_slots(self):
        before = len(eventlog.read(self.d)[0])
        rc, out = self.game("bogen", "-c", "test", "Ilsa", "--platz", "1=2/2")
        self.assertEqual(rc, 0, out)
        events = eventlog.read(self.d)[0][before:]
        self.assertEqual([(e["type"], e["data"]["slots"]) for e in events], [("slots.set", {"1": {"max": 2, "used": 0}})])

    def test_after_bogen_changes_engine_and_sheets_agree(self):
        self._new_events("--tp", "9/20", "--temp", "0", "--muenzen", "30gp", "--ressource", "Second Wind=0/1",
                         "--inspiration", "ja", "--plus", "Fackel", "--minus", "Streitaxt", "--trefferwuerfel", "2")
        rc, out = self.game("abgleich", "-c", "test")
        self.assertIn("stimmen überein", out, out)

    def test_an_impossible_change_writes_nothing(self):
        before_text, before_log = self._sheet(), eventlog.read(self.d)[0]
        with self.assertRaises(SystemExit):
            self.game("bogen", "-c", "test", "Brakka", "--tp", "3", "--minus", "Zauberstab")
        self.assertEqual(self._sheet(), before_text)
        self.assertEqual(eventlog.read(self.d)[0], before_log)


class CauseTests(Base):
    """The comparison names the cause of each difference."""

    def setUp(self):
        super().setUp()
        self.game("import", "-c", "test", "--ja")

    def test_a_sheet_edited_by_hand_is_reported_as_such(self):
        path = self.d / "characters" / "brakka.md"
        path.write_text(SHEET_A.replace("**HP:** 15 / 20", "**HP:** 4 / 20"), encoding="utf-8")
        rc, out = self.game("abgleich", "-c", "test")
        self.assertIn("| Brakka Steinfaust | TP | 15 / 20 | 4 / 20 | von Hand geändert |", out)

    def test_a_display_only_change_says_the_sheet_was_not_followed(self):
        shadow.record(self.d, [("hp.set", {"who": "Brakka", "current": 9})], src="send")
        rc, out = self.game("abgleich", "-c", "test")
        self.assertIn("| Brakka Steinfaust | TP | 9 / 20 | 15 / 20 | Bogen nicht nachgezogen |", out)

    def test_xp_and_tracker_changes_count_as_sheet_side(self):
        shadow.record(self.d, [("xp.award", {"who": "Brakka", "awarded": 50, "total_after": 450})], src="xp")
        path = self.d / "characters" / "brakka.md"
        path.write_text(SHEET_A.replace("**XP:** 400 / 900", "**XP:** 450 / 900"), encoding="utf-8")
        rc, out = self.game("abgleich", "-c", "test")
        self.assertIn("stimmen überein", out, out)


class StateReplayTests(unittest.TestCase):
    def test_coins_and_feature_counters_replay(self):
        fig = st.blank_figure("Brakka")
        fig["resources"] = {"Second Wind (Durchschnaufen)": {"left": 1, "max": 1}}
        state = st.empty()
        state["characters"]["Brakka"] = fig
        st.apply(state, {"seq": 1, "type": "currency.set", "data": {"who": "Brakka", "gp": 3, "sp": 2}})
        st.apply(state, {"seq": 2, "type": "resource.set",
                         "data": {"who": "Brakka", "name": "Second Wind", "left": 0, "max": 1}})
        self.assertEqual(fig["currency"], {"pp": 0, "gp": 3, "ep": 0, "sp": 2, "cp": 0})
        self.assertEqual(fig["resources"], {"Second Wind (Durchschnaufen)": {"left": 0, "max": 1}})


if __name__ == "__main__":
    unittest.main()
