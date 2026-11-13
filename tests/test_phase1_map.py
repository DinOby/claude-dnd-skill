"""Phase 1 - the map: format v2 with layers, paths, passability, walking paths.

B1  Layers (ground, terrain, walls, objects, figures), v1 maps converted on
    load, rivers and roads as paths (continuous, any bend), figures never on
    walls, in water or on objects (server refuses with the nearest free
    square), moves come with the way they walk (never through a wall).
"""
from __future__ import annotations

import json
import pathlib
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
DISPLAY = ROOT / "skills" / "dnd" / "display"
SCRIPTS = ROOT / "skills" / "dnd" / "scripts"
for p in (str(DISPLAY), str(SCRIPTS)):
    if p not in sys.path:
        sys.path.insert(0, p)

import grid_map as gm   # noqa: E402
import map_v2 as v      # noqa: E402


def v1_map(**extra):
    m = {"id": "hof", "name": "Hof", "cols": 12, "rows": 8,
         "terrain": [{"at": "A1", "w": 12, "h": 1, "type": "wall"},
                     {"at": "E4", "w": 2, "h": 1, "type": "table"},
                     {"at": "H3", "w": 2, "h": 3, "type": "water"},
                     {"at": "A5", "type": "door"}],
         "tokens": [{"name": "Alrik", "kind": "pc", "at": "B3"}]}
    m.update(extra)
    return m


def v2_map(**extra):
    m = {"format": 2, "id": "furt", "name": "Die Furt", "cols": 16, "rows": 10, "floor": "grass",
         "layers": {"ground": [{"at": "A1", "w": 3, "h": 2, "type": "dirt"}],
                    "terrain": [], "walls": [],
                    "objects": [{"at": "H5", "w": 2, "h": 1, "type": "bridge"},
                                {"at": "A5", "type": "exit", "label": "Zum Dorf"},
                                {"at": "C8", "type": "tree"}],
                    "paths": [{"type": "river", "points": ["I1", "I4", "H6", "H10"], "width": 2},
                              {"type": "road", "points": ["A5", "P5"]}]},
         "tokens": []}
    m.update(extra)
    return m


class FormatTests(unittest.TestCase):
    def test_v1_is_read_as_v2_with_layers(self):
        m = gm.normalize_map(v1_map())
        self.assertEqual(m["format"], 2)
        self.assertEqual([r["type"] for r in m["layers"]["walls"]], ["wall"])
        self.assertEqual([r["type"] for r in m["layers"]["terrain"]], ["water"])
        self.assertEqual(sorted(r["type"] for r in m["layers"]["objects"]), ["door", "table"])
        self.assertEqual(m["floor"], "cobble")
        self.assertEqual(len(m["terrain"]), 4)

    def test_v2_layers_and_paths_are_kept(self):
        m = gm.normalize_map(v2_map())
        self.assertEqual(m["layers"]["ground"][0]["type"], "dirt")
        self.assertEqual(m["layers"]["paths"][0]["points"][0], [8, 0])
        self.assertEqual(m["layers"]["objects"][1]["label"], "Zum Dorf")

    def test_unknown_layer_and_bad_path_are_refused(self):
        bad = v2_map()
        bad["layers"]["clouds"] = []
        bad["layers"]["paths"] = [{"type": "river", "points": ["Z99"]}]
        with self.assertRaises(gm.MapError) as err:
            gm.normalize_map(bad)
        text = " ".join(err.exception.problems)
        self.assertIn("layers.clouds", text)
        self.assertIn("paths[0].points[0]", text)

    def test_a_river_is_one_continuous_band(self):
        m = gm.normalize_map(v2_map())
        cells = set(v.path_cells(m["layers"]["paths"][0], m["cols"], m["rows"]))
        start = (8, 0)
        seen, todo = {start}, [start]
        while todo:
            x, y = todo.pop()
            for c in ((x + 1, y), (x - 1, y), (x, y + 1), (x, y - 1)):
                if c in cells and c not in seen:
                    seen.add(c)
                    todo.append(c)
        self.assertEqual(seen, cells)
        self.assertTrue(all(y in {c[1] for c in cells} for y in range(10)))

    def test_the_bridge_crosses_the_river(self):
        m = gm.normalize_map(v2_map())
        blocked = v.blockers(m)
        self.assertNotIn((7, 4), blocked)
        way = v.find_path(m, (0, 4), (15, 4), blocked)
        self.assertIsNotNone(way)
        self.assertTrue(any(c in ((7, 4), (8, 4)) for c in way))


class PlacementTests(unittest.TestCase):
    def test_a_figure_in_a_wall_is_refused_with_the_nearest_free_square(self):
        with self.assertRaises(gm.MapError) as err:
            gm.normalize_map(v1_map(tokens=[{"name": "Alrik", "kind": "pc", "at": "C1"}]), strict=True)
        msg = err.exception.problems[0]
        self.assertIn("C1 is a wall", msg)
        self.assertIn("Wand", msg)
        self.assertIn("Nearest free square: C2", msg)

    def test_water_is_refused_even_with_force(self):
        m = gm.normalize_map(v1_map())
        with self.assertRaises(gm.MapError) as err:
            gm.apply_patch(m, {"move": [{"id": "alrik", "to": "H3", "force": True}]})
        self.assertIn("water", err.exception.problems[0])

    def test_an_object_needs_force(self):
        m = gm.normalize_map(v1_map())
        with self.assertRaises(gm.MapError) as err:
            gm.apply_patch(m, {"add": [{"name": "Bela", "kind": "npc", "at": "E4"}]})
        self.assertIn("force", err.exception.problems[0])
        new, _, _ = gm.apply_patch(m, {"add": [{"name": "Bela", "kind": "npc", "at": "E4", "force": True}]})
        self.assertEqual(gm.find_token(new, "Bela")["x"], 4)

    def test_doors_are_walkable_and_object_markers_free(self):
        m = gm.normalize_map(v1_map())
        gm.apply_patch(m, {"move": [{"id": "alrik", "to": "A5"}]})
        new, _, _ = gm.apply_patch(m, {"add": [{"name": "Brunnen", "kind": "object", "at": "E4"}]})
        self.assertEqual(gm.find_token(new, "Brunnen")["x"], 4)

    def test_stored_maps_still_load_with_a_figure_on_a_wall(self):
        m = gm.normalize_map(v1_map(tokens=[{"name": "Alrik", "kind": "pc", "at": "C1"}]))
        self.assertEqual(m["tokens"][0]["y"], 0)

    def test_auto_placement_avoids_water_and_objects(self):
        m = gm.normalize_map(v1_map(tokens=[{"name": "Held %d" % i, "kind": "pc"} for i in range(30)]))
        blocked = v.blockers(m)
        self.assertFalse(any((t["x"], t["y"]) in blocked for t in m["tokens"]))


class WalkingTests(unittest.TestCase):
    def setUp(self):
        self.m = gm.normalize_map({"id": "gang", "cols": 9, "rows": 5,
                                   "terrain": [{"at": "E1", "w": 1, "h": 4, "type": "wall"}],
                                   "tokens": [{"name": "Alrik", "kind": "pc", "at": "B2"}]})

    def test_a_move_walks_around_the_wall(self):
        _, applied, _ = gm.apply_patch(self.m, {"move": [{"id": "alrik", "to": "H2"}]})
        path = [tuple(c) for c in applied["move"][0]["path"]]
        self.assertEqual((path[0], path[-1]), ((1, 1), (7, 1)))
        self.assertFalse(any(c[0] == 4 and c[1] < 4 for c in path))
        self.assertIn((4, 4), path)
        for (x0, y0), (x1, y1) in zip(path, path[1:]):
            self.assertLessEqual(max(abs(x1 - x0), abs(y1 - y0)), 1)

    def test_no_corner_cutting(self):
        way = v.find_path(self.m, (3, 3), (5, 4))
        self.assertNotIn(((3, 3), (4, 4)), list(zip(way, way[1:])))
        self.assertEqual(way[1], (3, 4))

    def test_an_unreachable_square_is_a_jump(self):
        closed = gm.normalize_map({"id": "zelle", "cols": 7, "rows": 5,
                                   "terrain": [{"at": "D1", "w": 1, "h": 5, "type": "wall"}],
                                   "tokens": [{"name": "Alrik", "kind": "pc", "at": "A1"}]})
        _, applied, _ = gm.apply_patch(closed, {"move": [{"id": "alrik", "to": "F3"}]})
        self.assertTrue(applied["move"][0].get("jump"))
        self.assertNotIn("path", applied["move"][0])

    def test_reach_counts_diagonals_as_one_and_respects_walls(self):
        cells = v.reach(self.m, (1, 1), 2)
        self.assertIn((3, 3), cells)
        self.assertNotIn((4, 1), cells)
        self.assertNotIn((5, 1), cells)


class ConvertCommandTests(unittest.TestCase):
    def test_convert_writes_copies_and_never_the_library(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location("maps_p1", SCRIPTS / "maps.py")
        maps = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(maps)
        with tempfile.TemporaryDirectory() as d:
            lib_dir = pathlib.Path(d) / "library"
            lib_dir.mkdir()
            original = json.dumps(v1_map(tokens=[]))
            (lib_dir / "hof.json").write_text(original, encoding="utf-8")
            lib = maps.Library(library_dir=lib_dir, campaigns=pathlib.Path(d) / "c", store=object())
            out = pathlib.Path(d) / "v2"
            self.assertEqual(maps.main(["convert", "--out", str(out)], lib=lib), 0)
            data = json.loads((out / "hof.json").read_text(encoding="utf-8"))
            self.assertEqual((data["format"], len(data["layers"]["walls"])), (2, 1))
            self.assertEqual((lib_dir / "hof.json").read_text(encoding="utf-8"), original)
            self.assertEqual(maps.main(["convert", "--out", str(lib_dir)], lib=lib), 2)


if __name__ == "__main__":
    unittest.main()