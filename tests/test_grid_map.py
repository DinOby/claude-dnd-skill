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
import scene_state  # noqa: E402
from asset_queue import PendingQueue  # noqa: E402
from asset_store import AssetStore  # noqa: E402
from tests.app_isolation import IsolatedApp  # noqa: E402


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
                       "tokens[3] (Z): Q99 (size 1×1) is outside"):
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


class TemplateTests(unittest.TestCase):
    def setUp(self):
        self.templates = gm.MapTemplates()

    def test_bundled_templates_are_valid_and_match_the_seed_maps(self):
        listed = self.templates.list()
        ids = {t["id"] for t in listed}
        self.assertEqual(ids, {"tavern-small", "market-square", "forest-road", "forest-clearing", "mountain-pass",
                               "bridge", "camp", "cave-mouth", "dungeon-corridor", "ruins"})
        for f in os.listdir(gm.DEFAULT_TEMPLATE_DIR):
            self.assertEqual(self.templates.problems(f[:-5]), [], f)
        seed = json.loads((DISPLAY / "config" / "asset-seed.json").read_text(encoding="utf-8"))["maps"]
        by_template = {e["template"]: f"map:{slug}" for slug, e in seed.items()}
        for t in listed:
            self.assertEqual(t["background"], {"asset": by_template[t["id"]]}, t["id"])

    def test_every_spawn_zone_has_room(self):
        for t in self.templates.list():
            m = self.templates.get(t["id"])
            blocked = gm._blocked_cells(m)
            for kind in gm.SPAWN_KINDS:
                zone = {(r["x"] + dx, r["y"] + dy) for r in m["spawn"].get(kind, [])
                        for dx in range(r["w"]) for dy in range(r["h"])}
                self.assertGreaterEqual(len(zone - blocked), 3, f"{t['id']}.{kind}")

    def test_user_template_overrides_and_adds(self):
        user = Path(tempfile.mkdtemp())
        (user / "tavern-small.json").write_text(json.dumps({"name": "Meine Taverne", "cols": 5, "rows": 5}),
                                                encoding="utf-8")
        (user / "broken.json").write_text("{", encoding="utf-8")
        t = gm.MapTemplates([gm.DEFAULT_TEMPLATE_DIR, str(user)])
        self.assertEqual(t.get("tavern-small")["name"], "Meine Taverne")
        self.assertNotIn("broken", [x["id"] for x in t.list()])
        self.assertIsNone(t.get("nope"))


class PlacementTests(unittest.TestCase):
    def setUp(self):
        self.templates = gm.MapTemplates()

    def build(self, template="tavern-small", **raw):
        return gm.normalize_map(gm.expand_template(dict(raw, template=template), self.templates))

    def test_tokens_without_position_use_their_zone(self):
        m = self.build(id="kessel-schankraum", name="Schankraum im Kessel", tokens=[
            {"name": "Flerb", "kind": "pc"}, {"name": "Mira", "kind": "pc"},
            {"name": "Goblin", "kind": "enemy"}, {"name": "Wirtin Hilde"}, {"name": "Kiste", "kind": "object"},
            {"name": "Oger", "kind": "enemy", "size": 2}])
        self.assertEqual((m["id"], m["name"], m["template"]), ("kessel-schankraum", "Schankraum im Kessel", "tavern-small"))
        pos = {t["id"]: gm.format_coord(t["x"], t["y"]) for t in m["tokens"]}
        self.assertEqual(pos, {"flerb": "B8", "mira": "C8", "goblin": "J7", "wirtin-hilde": "B3",
                               "kiste": "C3", "oger": "K7"})
        for t in m["tokens"]:
            self.assertEqual(gm.token_warnings(m, t), [], t["name"])

    def test_full_zone_spills_to_the_nearest_free_square(self):
        m = self.build(tokens=[{"name": f"Held {i}", "kind": "pc"} for i in range(10)])
        blocked, seen = gm._blocked_cells(m), set()
        for t in m["tokens"]:
            cell = (t["x"], t["y"])
            self.assertNotIn(cell, blocked)
            self.assertNotIn(cell, seen)
            seen.add(cell)
        self.assertEqual(sum(1 for t in m["tokens"] if t["y"] >= 7 and 1 <= t["x"] <= 4), 8)   # zone B8:E9 full

    def test_map_without_spawn_and_full_map(self):
        m = gm.normalize_map({"id": "leer", "cols": 5, "rows": 5, "tokens": [{"name": "A"}]})
        self.assertEqual((m["tokens"][0]["x"], m["tokens"][0]["y"]), (2, 2))          # centre
        with self.assertRaises(gm.MapError) as cm:
            gm.normalize_map({"id": "eng", "cols": 1, "rows": 1, "tokens": [{"name": "A"}, {"name": "B"}]})
        self.assertIn("no free square left for B", cm.exception.problems)

    def test_patch_add_without_position(self):
        m = self.build(tokens=[{"name": "Goblin", "kind": "enemy", "at": "J7"}])
        new, applied, _ = gm.apply_patch(m, {"add": [{"name": "Goblin", "kind": "enemy"}]})
        self.assertEqual((applied["add"][0]["id"], gm.format_coord(applied["add"][0]["x"], applied["add"][0]["y"])),
                         ("goblin-2", "K7"))
        _, _, warnings = gm.apply_patch(new, {"add": [{"name": "Wirtin Hilde"}, {"name": "wirtin hilde"}]})
        self.assertEqual(warnings, ["wirtin hilde was already on the map — added another one as 'wirtin-hilde-2'"])

    def test_bad_spawn(self):
        with self.assertRaises(gm.MapError) as cm:
            gm.normalize_map({"id": "x", "cols": 5, "rows": 5,
                              "spawn": {"pc": [{"at": "E5", "w": 2}], "dragon": [], "npc": "A1"}})
        text = " | ".join(cm.exception.problems)
        self.assertIn("spawn.pc[0]: E5 (size 2×1) is outside", text)
        self.assertIn("unknown kind 'dragon'", text)
        self.assertIn("spawn.npc should be a list", text)

    def test_unknown_template(self):
        with self.assertRaises(gm.MapError) as cm:
            gm.expand_template({"template": "schloss"}, self.templates)
        self.assertIn("available: bridge", cm.exception.problems[0])


class TemplateStoreTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        base = Path(self._tmp.name)
        self.lib = base / "library"
        self.store = gm.MapStore(str(self.lib), str(base / "a"))

    def test_new_from_template_then_reuse_the_stored_layout(self):
        m = self.store.set_map({"template": "forest-road", "id": "koenigsstrasse-west",
                                "tokens": [{"name": "Flerb", "kind": "pc"}]})
        self.assertEqual((m["id"], m["cols"], m["background"]), ("koenigsstrasse-west", 18, {"asset": "map:waldweg"}))
        # the DM edits the stored layout; a later --map-new with the same id keeps that edit and the tokens
        layout = json.loads((self.lib / "koenigsstrasse-west.json").read_text(encoding="utf-8"))
        layout["terrain"].append({"x": 8, "y": 0, "w": 1, "h": 1, "type": "shrine"})
        (self.lib / "koenigsstrasse-west.json").write_text(json.dumps(layout), encoding="utf-8")
        again = self.store.set_map({"template": "forest-road", "id": "koenigsstrasse-west"})
        self.assertEqual(again["terrain"][-1]["type"], "shrine")
        self.assertEqual([t["id"] for t in again["tokens"]], ["flerb"])
        self.assertEqual(again["rev"], 2)


class RouteTests(IsolatedApp):
    app_module_name = "_grid_app_under_test"

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        base = Path(self._tmp.name)
        for name, value in (("_maps", gm.MapStore(str(base / "lib"), str(base / "camp"))),
                            ("_scenes", scene_state.SceneStore(str(base / "camp" / "scene-state.json"))),
                            ("_assets", AssetStore(global_root=str(base / "assets"))),
                            ("_asset_queue", PendingQueue(str(base / "assets" / "pending-assets.json")))):
            patcher = mock.patch.object(self.app, name, value)   # never touch the real campaign
            patcher.start()
            self.addCleanup(patcher.stop)
        # one main display and one phone
        self.main, self.phone = queue.Queue(), queue.Queue()
        clients = mock.patch.object(self.app, "_clients", [self.main, self.phone])
        chars = mock.patch.object(self.app, "_client_chars", {self.phone: "flerb"})
        clients.start(); chars.start()
        self.addCleanup(clients.stop); self.addCleanup(chars.stop)

    def post(self, body, url="/map"):
        resp = self.client.post(url, json=body)
        return resp.status_code, resp.get_json()

    def drain(self, key):
        """Messages of one kind sent to the main display (others are dropped)."""
        out = []
        while not self.main.empty():
            msg = self.main.get_nowait()
            if key in msg:
                out.append(msg[key])
        return out

    def test_set_patch_hide_reach_main_display_only(self):
        code, data = self.post({"map": tavern()})
        self.assertEqual((code, data["map_id"], data["rev"]), (200, "kessel-schankraum", 1))
        self.assertEqual(self.drain("map")[0]["id"], "kessel-schankraum")
        code, data = self.post({"patch": {"move": [{"id": "Flerb", "to": "B1"}]}})
        self.assertEqual((code, data["rev"]), (200, 2))
        self.assertEqual(data["warnings"], ["Flerb stands on wall at B1"])
        self.assertEqual(self.drain("map_patch")[0]["move"], [{"id": "flerb", "x": 1, "y": 0}])
        self.assertEqual(self.post({"hide": True})[0], 200)
        self.assertEqual(self.drain("map"), [None])
        self.assertTrue(self.phone.empty())
        got = self.client.get("/map").get_json()
        self.assertEqual((got["map"], got["library"]), (None, ["kessel-schankraum"]))

    def test_template_and_party(self):
        with mock.patch.object(self.app, "_current_stats", {"players": [{"name": "Flerb"}, {"name": "Mira"}]}):
            code, data = self.post({"map": {"template": "camp", "id": "rast"}})
            self.assertEqual((code, data["map_id"]), (200, "rast"))
            code, data = self.post({"patch": {"add_party": True, "add": [{"name": "Wolf", "kind": "enemy"}]}})
            self.assertEqual(code, 200)
            names = {t["name"] for t in self.app._maps.current()["tokens"]}
            self.assertEqual(names, {"Flerb", "Mira", "Wolf"})
            code, data = self.post({"patch": {"add_party": True}})
            self.assertEqual((code, data["warnings"]), (200, ["every player character is already on the map"]))
        got = self.client.get("/map").get_json()
        self.assertIn("tavern-small", [t["id"] for t in got["templates"]])

    def test_rejections(self):
        code, data = self.post({"patch": {"move": [{"id": "Flerb", "to": "D5"}]}})
        self.assertEqual(code, 400)
        self.assertIn("no map is on screen", data["errors"][0])
        self.post({"map": tavern()})
        self.drain("map")
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
        base = dict(map_set=None, map_show=None, map_hide=False, stat_move=None, token_add=None, token_remove=None,
                    map_new=None, map_id=None, map_name=None, token_party=False, scene_set=None)
        base.update(kw)
        return SimpleNamespace(**base)

    def test_specs(self):
        self.assertEqual(self.ps._move_spec("Goblin 1:E6"), {"id": "Goblin 1", "to": "E6"})
        self.assertEqual(self.ps._token_spec("Goblin 2:E7:enemy"), {"name": "Goblin 2", "at": "E7", "kind": "enemy"})
        self.assertEqual(self.ps._token_spec("Wirtin Hilde:B3"), {"name": "Wirtin Hilde", "at": "B3", "kind": "npc"})
        self.assertEqual(self.ps._token_spec('{"name":"Oger","at":"H3","size":2}')["size"], 2)
        self.assertEqual(self.ps._token_spec("Goblin:enemy"), {"name": "Goblin", "kind": "enemy"})
        self.assertEqual(self.ps._token_spec("Wirtin Hilde"), {"name": "Wirtin Hilde", "kind": "npc"})
        self.assertEqual(self.ps._token_spec("Flerb::pc"), {"name": "Flerb", "kind": "pc"})
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

    def test_map_new_and_party(self):
        bodies, err = self.ps._map_body(self.args(map_new="tavern-small", map_id="kessel", map_name="Kessel",
                                                  token_party=True))
        self.assertEqual(bodies, [{"map": {"template": "tavern-small", "id": "kessel", "name": "Kessel"}},
                                  {"patch": {"add_party": True}}])
        self.assertIn("--map-new", self.ps._map_body(self.args(map_id="kessel"))[1])


if __name__ == "__main__":
    unittest.main()
