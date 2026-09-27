"""Tests for the battle-grid map model (grid_map.py), the /map route and push_stats.py map flags."""

from __future__ import annotations

import importlib.util
import io
import json
import os
import queue
import sys
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
SKILL = REPO / "skills" / "dnd" if (REPO / "skills" / "dnd").is_dir() else REPO
DISPLAY = SKILL / "display"
for p in (str(DISPLAY), str(SKILL / "scripts")):
    if p not in sys.path:
        sys.path.insert(0, p)

import grid_map as gm  # noqa: E402


def tavern(**extra) -> dict:
    m = {"id": "kessel-schankraum", "name": "Schankraum", "template": "tavern-small",
         "tags": ["tavern"], "cols": 14, "rows": 10,
         "background": {"asset": "map:taverne"},
         "terrain": [{"x": 5, "y": 4, "w": 3, "h": 1, "type": "table"},
                     {"at": "A1", "w": 14, "h": 1, "type": "wall"}],
         "tokens": [{"name": "Flerb", "kind": "pc", "at": "C6", "asset": "token:flerb"},
                    {"name": "Goblin", "kind": "enemy", "x": 9, "y": 5},
                    {"name": "Goblin", "kind": "enemy", "at": "K7"}]}
    m.update(extra)
    return m


class CoordinateTests(unittest.TestCase):
    def test_round_trip(self):
        for (x, y), label in (((0, 0), "A1"), ((3, 4), "D5"), ((25, 9), "Z10"),
                              ((26, 0), "AA1"), ((27, 59), "AB60")):
            self.assertEqual(gm.format_coord(x, y), label)
            self.assertEqual(gm.parse_coord(label), (x, y))
        self.assertEqual(gm.parse_coord(" d5 "), (3, 4))

    def test_rejects_garbage(self):
        for bad in ("", "5D", "D", "D0", "ABC1", "D-1", "D 5 x"):
            with self.assertRaises(ValueError, msg=bad):
                gm.parse_coord(bad)


class NormalizeTests(unittest.TestCase):
    def test_canonical_map(self):
        m = gm.normalize_map(tavern())
        self.assertEqual([t["id"] for t in m["tokens"]], ["flerb", "goblin", "goblin-2"])
        self.assertEqual((m["tokens"][0]["x"], m["tokens"][0]["y"], m["tokens"][0]["size"]), (2, 5, 1))
        self.assertEqual(m["terrain"][1], {"x": 0, "y": 0, "w": 14, "h": 1, "type": "wall"})
        self.assertEqual((m["cell_ft"], m["rev"], m["background"]), (5, 0, {"asset": "map:taverne"}))

    def test_collects_every_problem(self):
        bad = tavern(terrain=[{"at": "N10", "w": 2, "type": "table"}, {"at": "B2"}],
                     tokens=[{"name": "Oger", "at": "N10", "size": 2}, {"name": "X", "kind": "dragon", "at": "A1"},
                             {"name": "Y"}, {"name": "Z", "at": "Q99"}])
        with self.assertRaises(gm.MapError) as cm:
            gm.normalize_map(bad)
        text = " | ".join(cm.exception.problems)
        for needle in ("terrain[0]: N10 (size 2×1) is outside the 14×10 grid", "terrain[1]: needs a \"type\"",
                       "tokens[0] (Oger): N10 (size 2×2) is outside", "kind should be one of",
                       "tokens[2] (Y): position missing", "tokens[3] (Z): Q99 (size 1×1) is outside"):
            self.assertIn(needle, text)

    def test_size_limits_and_id(self):
        with self.assertRaises(gm.MapError):
            gm.normalize_map({"id": "x", "cols": 61, "rows": 5})
        with self.assertRaises(gm.MapError):
            gm.normalize_map({"cols": 5, "rows": 5})
        self.assertEqual(gm.normalize_map({"name": "Höhle am Fluss", "cols": 5, "rows": 5})["id"], "hoehle-am-fluss")

    def test_explicit_duplicate_id_is_an_error(self):
        with self.assertRaises(gm.MapError):
            gm.normalize_map(tavern(tokens=[{"id": "g", "name": "A", "at": "A2"}, {"id": "g", "name": "B", "at": "B2"}]))


class PatchTests(unittest.TestCase):
    def setUp(self):
        self.m = gm.normalize_map(tavern())

    def test_move_add_remove(self):
        new, applied, warnings = gm.apply_patch(self.m, {
            "move": [{"id": "Flerb", "to": "D5"}, {"id": "goblin-2", "x": 10, "y": 6}],
            "add": [{"name": "Goblin", "kind": "enemy", "at": "L8"}],
            "remove": ["goblin"]})
        self.assertEqual(self.m["rev"], 0)                                   # input untouched
        self.assertEqual(new["rev"], 1)
        self.assertEqual(applied["base"], 0)
        self.assertEqual(applied["move"], [{"id": "flerb", "x": 3, "y": 4}, {"id": "goblin-2", "x": 10, "y": 6}])
        self.assertEqual(applied["remove"], ["goblin"])
        # the freed id is reused by the new goblin
        self.assertEqual([t["id"] for t in new["tokens"]], ["flerb", "goblin-2", "goblin"])
        self.assertEqual(warnings, [])

    def test_all_or_nothing(self):
        with self.assertRaises(gm.MapError) as cm:
            gm.apply_patch(self.m, {"move": [{"id": "Flerb", "to": "D5"}, {"id": "Nobody", "to": "A2"},
                                             {"id": "Goblin", "to": "O1"}],
                                    "remove": ["ghost"]})
        text = " | ".join(cm.exception.problems)
        self.assertIn("no token 'Nobody'", text)
        self.assertIn("O1 (size 1×1) is outside the 14×10 grid", text)
        self.assertIn("remove[0]: no token 'ghost'", text)
        self.assertEqual(self.m["tokens"][0]["x"], 2)

    def test_warnings_and_wrong_map(self):
        _, _, warnings = gm.apply_patch(self.m, {"move": [{"id": "flerb", "to": "B1"},
                                                          {"id": "goblin-2", "to": "J6"}]})
        self.assertEqual(warnings, ["Flerb stands on wall at B1", "Goblin shares J6 with Goblin"])
        with self.assertRaises(gm.MapError):
            gm.apply_patch(self.m, {"map_id": "elsewhere", "move": [{"id": "flerb", "to": "B2"}]})
        with self.assertRaises(gm.MapError):
            gm.apply_patch(self.m, {})

    def test_name_lookup(self):
        self.assertEqual(gm.find_token(self.m, "goblin 2")["id"], "goblin-2")
        self.assertEqual(gm.find_token(self.m, "FLERB")["id"], "flerb")
        self.assertIsNone(gm.find_token(self.m, ""))


class StoreTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        base = Path(self._tmp.name)
        self.lib, self.camp_a, self.camp_b = base / "library", base / "a" / "maps", base / "b" / "maps"
        self.store = gm.MapStore(str(self.lib), str(self.camp_a))

    def test_layout_and_placement_are_split(self):
        m = self.store.set_map(tavern())
        self.assertEqual(m["rev"], 1)
        layout = json.loads((self.lib / "kessel-schankraum.json").read_text(encoding="utf-8"))
        self.assertNotIn("tokens", layout)
        self.assertEqual(layout["terrain"][0]["type"], "table")
        placement = json.loads((self.camp_a / "kessel-schankraum.json").read_text(encoding="utf-8"))
        self.assertEqual([t["id"] for t in placement["tokens"]], ["flerb", "goblin", "goblin-2"])
        self.assertEqual(json.loads((self.camp_a / "active.json").read_text(encoding="utf-8")),
                         {"map_id": "kessel-schankraum"})

    def test_survives_restart_and_switches_campaign(self):
        self.store.set_map(tavern())
        self.store.patch({"move": [{"id": "flerb", "to": "D5"}]})
        again = gm.MapStore(str(self.lib), str(self.camp_a))
        self.assertEqual((again.current()["rev"], again.current()["tokens"][0]["x"]), (2, 3))
        # other campaign: same library, own (empty) placement, nothing on screen yet
        again.set_placement_dir(str(self.camp_b))
        self.assertIsNone(again.current())
        shown = again.show("kessel-schankraum")
        self.assertEqual((shown["tokens"], shown["terrain"][0]["type"]), ([], "table"))
        self.assertEqual(again.library(), ["kessel-schankraum"])
        again.set_placement_dir(str(self.camp_a))
        self.assertEqual(again.current()["tokens"][0]["x"], 3)

    def test_resending_layout_keeps_tokens(self):
        self.store.set_map(tavern())
        narrow = tavern(cols=10, terrain=[{"x": 5, "y": 4, "w": 3, "h": 1, "type": "table"}])
        m = self.store.set_map({k: v for k, v in narrow.items() if k != "tokens"})
        self.assertEqual(m["rev"], 2)
        self.assertEqual([t["id"] for t in m["tokens"]], ["flerb", "goblin"])   # K7 no longer fits

    def test_errors(self):
        with self.assertRaises(gm.MapError):
            self.store.patch({"move": [{"id": "x", "to": "A1"}]})
        with self.assertRaises(gm.MapError):
            self.store.show("nirgendwo")
        self.store.set_map(tavern())
        self.store.hide()
        self.assertIsNone(self.store.current())
        self.assertIsNone(gm.MapStore(str(self.lib), str(self.camp_a)).current())


def _import_app():
    spec = importlib.util.spec_from_file_location("_grid_app_under_test", str(DISPLAY / "dnd-display-app.py"))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


class RouteTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = _import_app()
        cls.app._token_ok = lambda: True
        cls.client = cls.app.app.test_client()

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        base = Path(self._tmp.name)
        patcher = mock.patch.object(self.app, "_maps", gm.MapStore(str(base / "lib"), str(base / "camp")))
        patcher.start()
        self.addCleanup(patcher.stop)
        # one main display and one phone
        self.main, self.phone = queue.Queue(), queue.Queue()
        clients = mock.patch.object(self.app, "_clients", [self.main, self.phone])
        chars = mock.patch.object(self.app, "_client_chars", {self.phone: "flerb"})
        clients.start(); chars.start()
        self.addCleanup(clients.stop); self.addCleanup(chars.stop)

    def post(self, body):
        resp = self.client.post("/map", json=body)
        return resp.status_code, resp.get_json()

    def test_set_patch_hide_reach_main_display_only(self):
        code, data = self.post({"map": tavern()})
        self.assertEqual((code, data["map_id"], data["rev"]), (200, "kessel-schankraum", 1))
        self.assertEqual(self.main.get_nowait()["map"]["id"], "kessel-schankraum")
        code, data = self.post({"patch": {"move": [{"id": "Flerb", "to": "B1"}]}})
        self.assertEqual((code, data["rev"]), (200, 2))
        self.assertEqual(data["warnings"], ["Flerb stands on wall at B1"])
        self.assertEqual(self.main.get_nowait()["map_patch"]["move"], [{"id": "flerb", "x": 1, "y": 0}])
        self.assertEqual(self.post({"hide": True})[0], 200)
        self.assertEqual(self.main.get_nowait(), {"map": None})
        self.assertTrue(self.phone.empty())
        got = self.client.get("/map").get_json()
        self.assertEqual((got["map"], got["library"]), (None, ["kessel-schankraum"]))

    def test_rejections(self):
        code, data = self.post({"patch": {"move": [{"id": "Flerb", "to": "D5"}]}})
        self.assertEqual(code, 400)
        self.assertIn("no map is on screen", data["errors"][0])
        self.post({"map": tavern()})
        self.main.get_nowait()
        code, data = self.post({"patch": {"move": [{"id": "Flerb", "to": "Z99"}]}})
        self.assertEqual(code, 400)
        self.assertTrue(self.main.empty())
        self.assertEqual(self.post({"nonsense": 1})[0], 400)
        with mock.patch.object(self.app, "_token_ok", lambda: False):
            self.assertEqual(self.client.post("/map", json={"hide": True}).status_code, 403)


class PushStatsFlagTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        spec = importlib.util.spec_from_file_location("_push_stats_under_test", str(DISPLAY / "push_stats.py"))
        cls.ps = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.ps)

    def args(self, **kw):
        base = dict(map_set=None, map_show=None, map_hide=False, stat_move=None, token_add=None, token_remove=None)
        base.update(kw)
        return SimpleNamespace(**base)

    def test_specs(self):
        self.assertEqual(self.ps._move_spec("Goblin 1:E6"), {"id": "Goblin 1", "to": "E6"})
        self.assertEqual(self.ps._token_spec("Goblin 2:E7:enemy"), {"name": "Goblin 2", "at": "E7", "kind": "enemy"})
        self.assertEqual(self.ps._token_spec("Wirtin Hilde:B3"), {"name": "Wirtin Hilde", "at": "B3", "kind": "npc"})
        self.assertEqual(self.ps._token_spec('{"name":"Oger","at":"H3","size":2}')["size"], 2)
        for bad in ("Flerb", ":D5"):
            with self.assertRaises(ValueError):
                self.ps._move_spec(bad)

    def test_one_patch_in_order(self):
        bodies, err = self.ps._map_body(self.args(map_show="kessel", stat_move=["Flerb:D5"],
                                                  token_add=["Goblin 2:E7:enemy"], token_remove=["Goblin 1"],
                                                  map_hide=False))
        self.assertIsNone(err)
        self.assertEqual(bodies, [{"show": "kessel"},
                                  {"patch": {"move": [{"id": "Flerb", "to": "D5"}],
                                             "add": [{"name": "Goblin 2", "at": "E7", "kind": "enemy"}],
                                             "remove": ["Goblin 1"]}}])
        path = Path(tempfile.mkdtemp()) / "m.json"
        path.write_text(json.dumps({"id": "x", "cols": 3, "rows": 3}), encoding="utf-8")
        self.assertEqual(self.ps._map_body(self.args(map_set="@" + str(path)))[0], [{"map": {"id": "x", "cols": 3, "rows": 3}}])
        self.assertIsNotNone(self.ps._map_body(self.args(map_set="{broken"))[1])


if __name__ == "__main__":
    unittest.main()
