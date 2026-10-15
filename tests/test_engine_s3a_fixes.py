"""Game-state engine: the fixes after the Sitzung-3 test evening (before S3b).

WHY
===
The shadow-mode evening of Sitzung 3 ended with 9 differences, none of them a
lost write — every one came from a path that wrote a value twice, not at all,
or as the wrong kind of thing:

(a) spell slots counted twice — game.py bogen --platz and send.py --stat-slot-use
    both logged the same cast;
(b) level-ups had no command, so level and hit-dice totals were edited by hand
    and --platz could not raise a slot total;
(c) every full display refresh (campaign load, display restart) logged "set"
    events built from a hand-made player list;
(d) coin totals sent as inventory lines became items in the log and the display;
(e) the dice drawer kept the previous die (d12 instead of d20, d20 instead of d8).

Run from repo root:
    python3 -m unittest tests.test_engine_s3a_fixes -v
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import pathlib
import shutil
import subprocess
import sys
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "skills" / "dnd" / "scripts"
DISPLAY = ROOT / "skills" / "dnd" / "display"
sys.path.insert(0, str(SCRIPTS))

from engine import coins, eventlog, protocol, shadow, sheet, state as st   # noqa: E402
from tests.test_engine_import import Base                                  # noqa: E402
from tests.test_engine_s3a import _load                                    # noqa: E402


class _GameBase(Base):
    """Imported test campaign; display pushes are captured instead of sent."""

    def setUp(self):
        super().setUp()
        self.game("import", "-c", "test", "--ja")
        self.pushes = []

    def game(self, *argv):
        import importlib.util
        spec = importlib.util.spec_from_file_location("game_fix_cli", SCRIPTS / "game.py")
        game = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(game)
        out = io.StringIO()
        pushes = getattr(self, "pushes", [])
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()), \
                mock.patch.object(game, "find_campaign", return_value=self.d), \
                mock.patch.object(game, "runtime_dir", return_value=self.runtime), \
                mock.patch.object(game, "_display_push", lambda payload: pushes.append(payload) or True):
            rc = game.main(list(argv))
        return rc, out.getvalue()

    def sheet_text(self, who="ilsa") -> str:
        return (self.d / "characters" / f"{who}.md").read_text(encoding="utf-8")

    def snapshot(self) -> dict:
        return st.replay(eventlog.read(self.d)[0])

    def assert_no_differences(self):
        rc, out = self.game("abgleich", "-c", "test")
        self.assertIn("stimmen überein", out, out)

    def assert_events_valid(self):
        for e in eventlog.read(self.d)[0]:
            self.assertEqual(protocol.check_event(e), [], e)


# ── (a) spell slots: one path ───────────────────────────────────────────────

class SpellSlotTests(_GameBase):
    def test_casting_through_game_py_counts_once_even_with_the_display_flag(self):
        rc, out = self.game("bogen", "-c", "test", "Ilsa", "--zauber", "1")
        self.assertEqual(rc, 0, out)
        self.assertIn("| 1st | 2 | 2 |", self.sheet_text())
        # the old habit: also tell the display — it shows, it is not logged
        shadow.record(self.d, shadow.events_from_payload(
            {"players": [{"name": "Ilsa", "_slot_use": 1}]}), src="send")
        self.assertEqual(self.snapshot()["characters"]["Ilsa Morgentau"]["spell_slots"],
                         {"1": {"max": 2, "used": 2}})
        self.assert_no_differences()

    def test_no_free_slot_changes_nothing(self):
        self.game("bogen", "-c", "test", "Ilsa", "--zauber", "1")
        before = self.sheet_text()
        with self.assertRaises(SystemExit):
            self.game("bogen", "-c", "test", "Ilsa", "--zauber", "1")
        self.assertEqual(self.sheet_text(), before)

    def test_giving_a_slot_back(self):
        self.game("bogen", "-c", "test", "Ilsa", "--zauber-zurueck", "1")
        self.assertIn("| 1st | 2 | 0 |", self.sheet_text())
        self.assert_no_differences()

    def test_platz_can_raise_the_total(self):
        rc, out = self.game("bogen", "-c", "test", "Ilsa", "--platz", "1=3/3")
        self.assertEqual(rc, 0, out)
        self.assertIn("| 1st | 3 | 0 |", self.sheet_text())
        self.assertEqual(self.snapshot()["characters"]["Ilsa Morgentau"]["spell_slots"]["1"],
                         {"max": 3, "used": 0})
        self.assert_no_differences()

    def test_the_change_is_shown_on_the_display(self):
        self.game("bogen", "-c", "test", "Ilsa", "--zauber", "1")
        self.assertEqual(self.pushes[-1]["players"][0]["spell_slots"], {"1": {"used": 2, "max": 2}})


# ── (b) level-up through game.py stufe ──────────────────────────────────────

class LevelUpTests(_GameBase):
    def test_level_up_changes_sheet_and_log_together(self):
        rc, out = self.game("stufe", "-c", "test", "Ilsa", "--stufe", "2", "--tp-max", "15",
                            "--platz", "1=3/3")
        self.assertEqual(rc, 0, out)
        text = self.sheet_text()
        self.assertIn("**Level:** 2", text)
        self.assertIn("**HP:** 15 / 15", text)                        # 9/9 → +6 current too
        self.assertIn("**Hit Dice:** 2d8 (remaining: 2)", text)
        self.assertIn("| 1st | 3 | 0 |", text)
        self.assertIn("**XP:** 100 / 900", text)
        fig = self.snapshot()["characters"]["Ilsa Morgentau"]
        self.assertEqual((fig["level"], fig["hp"], fig["hp_max"], fig["xp_next"]), (2, 15, 15, 900))
        self.assertEqual(fig["hit_dice"], {"die": "d8", "total": 2, "remaining": 2})
        self.assert_events_valid()
        self.assert_no_differences()

    def test_level_up_removes_the_pending_marker(self):
        path = self.d / "characters" / "ilsa.md"
        path.write_text(self.sheet_text().replace("**XP:** 100 / 300",
                                                  "**XP:** 320 / 900 ⚠ LEVEL UP PENDING (Level 2)"),
                        encoding="utf-8")
        self.game("import", "-c", "test", "--ja")
        self.game("stufe", "-c", "test", "Ilsa", "--stufe", "2", "--tp-max", "15")
        self.assertIn("**XP:** 320 / 900\n", self.sheet_text())
        self.assert_no_differences()

    def test_a_new_spell_level_gets_its_row(self):
        self.game("stufe", "-c", "test", "Ilsa", "--stufe", "3", "--tp-max", "21", "--platz", "1=4/4",
                  "--platz", "2=2/2")
        self.assertIn("| 2nd | 2 | 0 |", self.sheet_text())
        self.assert_no_differences()

    def test_explicit_hp_and_hit_dice_win(self):
        self.game("stufe", "-c", "test", "Brakka", "--stufe", "3", "--tp-max", "28", "--tp", "20",
                  "--trefferwuerfel", "1")
        text = self.sheet_text("brakka")
        self.assertIn("**HP:** 20 / 28", text)
        self.assertIn("**Hit Dice:** 3d10 (remaining: 1)", text)
        self.assert_no_differences()

    def test_no_level_down_and_nothing_written(self):
        before = self.sheet_text("brakka")
        with self.assertRaises(SystemExit):
            self.game("stufe", "-c", "test", "Brakka", "--stufe", "2", "--tp-max", "30")
        self.assertEqual(self.sheet_text("brakka"), before)

    def test_level_up_is_shown_on_the_display(self):
        self.game("stufe", "-c", "test", "Ilsa", "--stufe", "2", "--tp-max", "15")
        shown = self.pushes[-1]["players"][0]
        self.assertEqual((shown["level"], shown["hp"]["max"], shown["hit_dice"]["max"]), (2, 15, 2))


# ── (c) a display refresh never changes a value ─────────────────────────────

class DisplayRefreshTests(_GameBase):
    def _push_stats(self, *args):
        env = dict(os.environ)
        env.pop(shadow.ENV_PARENT, None)
        env["DND_CAMPAIGN_ROOT"] = str(self.base)
        env["DND_RUNTIME_DIR"] = str(self.runtime)
        env["PYTHONIOENCODING"] = "utf-8"
        (self.runtime / ".campaign").write_text("test", encoding="utf-8")
        return subprocess.run([sys.executable, str(DISPLAY / "push_stats.py"), *args],
                              capture_output=True, text=True, encoding="utf-8", env=env, timeout=30)

    def test_a_full_refresh_with_other_values_logs_nothing(self):
        before = eventlog.read(self.d)[0]
        stale = {"players": [{"name": "Ilsa Morgentau", "hp": {"current": 1, "max": 9, "temp": 0},
                              "spell_slots": {"1": {"used": 0, "max": 2}}, "conditions": [],
                              "xp": {"current": 0, "next": 300}, "inspiration": False}]}
        res = self._push_stats("--replace-players", "--json", json.dumps(stale))
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual(eventlog.read(self.d)[0], before)
        self.assert_no_differences()

    def test_anzeige_shows_the_engine_and_changes_nothing(self):
        before_log, before_state = eventlog.read(self.d)[0], self.snapshot()
        rc, out = self.game("anzeige", "-c", "test", "--quelle", "engine")
        self.assertEqual(rc, 0, out)
        self.assertEqual(eventlog.read(self.d)[0], before_log)
        self.assertEqual(self.snapshot(), before_state)
        shown = {p["name"]: p for p in self.pushes[-1]["players"]}
        self.assertEqual(shown["Ilsa Morgentau"]["hp"], {"current": 9, "max": 9, "temp": 0})
        self.assertEqual(shown["Ilsa Morgentau"]["conditions"], ["poisoned"])
        self.assertEqual(shown["Brakka Steinfaust"]["sheet"], {"currency": "25 GM 3 SM 0 KM"})

    def test_anzeige_from_the_sheets_includes_tracker_conditions(self):
        rc, out = self.game("anzeige", "-c", "test", "--quelle", "bogen")
        self.assertEqual(rc, 0, out)
        shown = {p["name"]: p for p in self.pushes[-1]["players"]}
        self.assertEqual(shown["Ilsa Morgentau"]["conditions"], ["poisoned"])
        self.assertEqual(shown["Ilsa Morgentau"]["concentration"], "Bless")

    def test_single_flags_are_still_logged(self):
        res = self._push_stats("--player", "Ilsa", "--conditions-add", "Vergiftet")
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual(eventlog.read(self.d)[0][-1]["type"], "condition.add")


# ── (d) money is never an inventory line ────────────────────────────────────

class MoneyTests(unittest.TestCase):
    def test_coin_lines_are_recognised(self):
        for line in ("31 GM 6 SM", "25gp 3sp 0cp", "37 GM 8 SM", "Geld: 12 Gold", "5 sp"):
            self.assertTrue(coins.is_coin_line(line), line)
        for line in ("50 Gildentaler (Beweis)", "4 Handäxte", "Kurzbogen, 20 Pfeile", "Gm-Amulett", ""):
            self.assertFalse(coins.is_coin_line(line), line)

    def test_the_shadow_writer_never_logs_a_coin_item(self):
        events = shadow.events_from_payload({"players": [{"name": "Grosch", "_inventory_add": "31 GM 6 SM"}]})
        self.assertEqual(events, [])

    def test_send_refuses_a_coin_item_but_keeps_real_items(self):
        send = _load(DISPLAY / "send.py", "_send_money_under_test")
        args = mock.Mock(**{k: None for k in ("stat_hp", "stat_temp_hp", "stat_slot_use", "stat_slot_restore",
                                               "stat_condition_add", "stat_condition_remove", "stat_concentrate",
                                               "effect_start", "effect_end")})
        args.stat_inventory_add = ["Grosch:31 GM 6 SM", "Grosch:Seil"]
        args.stat_inventory_remove = ["Grosch:31 GM 7 SM"]
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            payload = send._build_stats_payload(args)
        self.assertEqual(payload["players"], [{"name": "Grosch", "_inventory_add": "Seil"}])
        self.assertIn("ist Geld", err.getvalue())

    def test_push_stats_moves_coin_lines_out_of_a_full_sheet(self):
        ps = _load(DISPLAY / "push_stats.py", "_ps_money_under_test")
        player = {"name": "Zarith", "sheet": {"inventory": ["Kurzschwert", "37 GM 8 SM", "31 GM 8 SM"]}}
        ps._coins_out_of_inventory(player)
        self.assertEqual(player["sheet"], {"inventory": ["Kurzschwert"], "currency": "31 GM 8 SM"})


# ── xp.py: the threshold the sheet shows is the one that is logged and shown ─

class XpThresholdTests(_GameBase):
    def test_crossing_a_level_logs_the_next_threshold(self):
        env = dict(os.environ)
        env.pop(shadow.ENV_PARENT, None)
        env.update({"DND_CAMPAIGN_ROOT": str(self.base), "DND_RUNTIME_DIR": str(self.runtime),
                    "PYTHONIOENCODING": "utf-8"})
        shutil.copytree(self.d, self.base / "campaigns" / "xp")
        d = self.base / "campaigns" / "xp"
        ilsa = d / "characters" / "ilsa.md"                         # 280 + 50 crosses 300
        ilsa.write_text(ilsa.read_text(encoding="utf-8").replace("**XP:** 100 / 300", "**XP:** 280 / 300"),
                        encoding="utf-8")
        res = subprocess.run([sys.executable, str(SCRIPTS / "xp.py"), "award", "--campaign", "xp",
                              "--characters", "Ilsa", "--difficulty", "medium", "--type", "noncombat"],
                             capture_output=True, text=True, encoding="utf-8", env=env, timeout=60)
        self.assertEqual(res.returncode, 0, res.stderr)
        award = [e for e in eventlog.read(d)[0] if e["type"] == "xp.award"][-1]
        self.assertEqual(award["data"]["next"], 900)
        self.assertEqual(st.replay(eventlog.read(d)[0])["characters"]["Ilsa Morgentau"]["xp_next"], 900)


# ── engine: an — the display paths no longer change engine-led values ───────

class EngineOnTests(unittest.TestCase):
    def test_display_paths_log_only_values_the_engine_does_not_lead(self):
        events = [("hp.set", {"who": "G", "current": 3}), ("condition.add", {"who": "G", "name": "x"}),
                  ("inventory.add", {"who": "G", "item": "Seil"}), ("inspiration.set", {"who": "G", "value": True})]
        kept = shadow.display_filter(events, "send", "an")
        self.assertEqual([t for t, _ in kept], ["inventory.add", "inspiration.set"])
        self.assertEqual(shadow.display_filter(events, "bogen", "an"), events)
        self.assertEqual(len(shadow.display_filter(events, "push_stats", "schatten")), 4)


# ── (e) the dice drawer takes the requested die ─────────────────────────────

NODE_HARNESS = r"""
const fs = require('fs');
const html = fs.readFileSync(process.argv[1], 'utf8');
const start = html.indexOf('function _initDicePad()');
let depth = 0, end = -1;
for (let i = html.indexOf('{', start); i < html.length; i++) {
  if (html[i] === '{') depth++;
  else if (html[i] === '}') { depth--; if (depth === 0) { end = i + 1; break; } }
}
const src = html.slice(start, end);

// Minimal DOM: buttons ignore click() while disabled, like a browser.
function el(id, cls = [], data = {}) {
  const e = { id, dataset: data, _attrs: {}, _cls: new Set(cls), _h: {}, textContent: '', value: '',
    style: {}, innerHTML: '', disabled: false,
    classList: { add: c => e._cls.add(c), remove: (...c) => c.forEach(x => e._cls.delete(x)),
                 contains: c => e._cls.has(c) },
    setAttribute: (k, v) => { e._attrs[k] = v; }, removeAttribute: k => { delete e._attrs[k]; },
    hasAttribute: k => k in e._attrs,
    addEventListener: (t, f) => { e._h[t] = f; },
    click: () => { if (!('disabled' in e._attrs) && !e.disabled && e._h.click) return e._h.click(); },
    querySelector: () => null, appendChild: () => {}, remove: () => {} };
  return e;
}
const dice = ['1d20', '1d4', '1d6', '1d8', '1d10', '1d12', '1d100', '2d6'].map(s => el('', ['dp-die'], { spec: s }));
const advs = ['normal', 'advantage', 'disadvantage'].map(a => el('', ['dp-adv'], { adv: a }));
const byId = { 'dp-reel': el('dp-reel'), 'dp-roll': el('dp-roll'), 'dp-mod-val': el('dp-mod-val'),
  'dp-name': el('dp-name'), 'dp-label': el('dp-label'), 'dp-bound': null, 'dp-hint': el('dp-hint'),
  'dp-result-line': el('dp-result-line'), 'input-body': el('input-body') };
byId['dp-name'].value = 'Mordos';
global.document = {
  getElementById: id => byId[id] || null,
  querySelectorAll: sel => sel.includes('dp-die') ? dice : sel.includes('dp-adv') ? advs : [],
  querySelector: sel => {
    let m = /data-spec="([^"]+)"/.exec(sel); if (m) return dice.find(d => d.dataset.spec === m[1]) || null;
    m = /data-adv="([^"]+)"/.exec(sel); if (m) return advs.find(a => a.dataset.adv === m[1]) || null;
    return null; },
};
global.window = {}; global.location = { search: '' };
global.localStorage = { getItem: k => (k === 'dnd_player_name' ? 'Mordos' : ''), setItem: () => {} };
let clock = 0;
global.navigator = {}; global.performance = { now: () => (clock += 500) };
global._authHeaders = () => ({}); global._setActiveTab = () => {};
global.setTimeout = f => f();
const posted = [];
global.fetch = async (url, opt) => { const b = JSON.parse(opt.body); posted.push(b.spec);
  return { ok: true, json: async () => ({ spec: b.spec, modifier: 0, total: 5, kept: [5], subtotal: 5 }) }; };
eval(src + '; _initDicePad();');
(async () => {
  window._onDiceRequest({ request_id: 'r1', character: 'Mordos', spec: '1d12' });
  await byId['dp-roll'].click();
  window._onDiceRequest({ request_id: 'r2', character: 'Mordos', spec: '1d20' });
  await byId['dp-roll'].click();
  window._onDiceRequest({ request_id: 'r3', character: 'Mordos', spec: '1d8' });
  await byId['dp-roll'].click();
  console.log(JSON.stringify(posted));
})();
"""


@unittest.skipIf(shutil.which("node") is None, "node not installed")
class DiceDrawerTests(unittest.TestCase):
    def test_each_request_rolls_its_own_die(self):
        proc = subprocess.run(["node", "-e", NODE_HARNESS, str(DISPLAY / "templates" / "index.html")],
                              capture_output=True, text=True, encoding="utf-8", timeout=60)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(json.loads(proc.stdout.strip().splitlines()[-1]), ["1d12", "1d20", "1d8"])


if __name__ == "__main__":
    unittest.main()
