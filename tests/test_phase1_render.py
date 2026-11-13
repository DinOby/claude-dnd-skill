"""Phase 1 - the map in the browser (B1/B2) and the vendored library.

- map-render.js computes river/road cells and the reach exactly like map_v2.py
  (node, no DOM), labels and exits, German hints and the legend.
- The server sends the blocked squares and the active style with every map.
- pixi.min.js is the checked file; the display loads no script from another
  server (fonts are stylesheets and listed separately in the report).
"""
from __future__ import annotations

import hashlib
import json
import pathlib
import re
import shutil
import subprocess
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
DISPLAY = ROOT / "skills" / "dnd" / "display"
JS = DISPLAY / "static" / "js"
for p in (str(DISPLAY), str(ROOT / "skills" / "dnd" / "scripts")):
    if p not in sys.path:
        sys.path.insert(0, p)

import grid_map as gm   # noqa: E402
import map_v2 as v      # noqa: E402

PIXI_SHA256 = None      # read from the vendor README (single source of truth)


def furt():
    return gm.normalize_map({"format": 2, "id": "furt", "cols": 18, "rows": 12, "floor": "grass",
        "layers": {"ground": [], "terrain": [], "walls": [{"at": "D1", "w": 1, "h": 6, "type": "wall"}],
                   "objects": [{"at": "I6", "w": 3, "h": 1, "type": "bridge"},
                               {"at": "A6", "type": "exit", "label": "Zum Dorf"},
                               {"at": "F3", "type": "door"}, {"at": "H9", "type": "stairs"}],
                   "paths": [{"type": "river", "points": ["J1", "J4", "I7", "K9", "K12"], "width": 2},
                             {"type": "road", "points": ["A6", "R6"]}]},
        "tokens": []})


HARNESS = r"""
const fs = require('fs'), vm = require('vm');
const sandbox = { console };
sandbox.window = sandbox;
vm.runInNewContext(fs.readFileSync(process.argv[process.argv.length - 1], 'utf8'), sandbox);
const H = sandbox.window.MapRenderer._helpers;
const M = sandbox.window.MapRenderer;
const input = JSON.parse(fs.readFileSync(0, 'utf8'));
const m = input.map;
const blocked = new Set(input.blocked.map(c => c[0] + ',' + c[1]));
const tokens = [
  { id: 'a', name: 'Alrik', kind: 'pc', x: 1, y: 1 }, { id: 'b', name: 'Bela', kind: 'npc', x: 2, y: 1 },
  { id: 'c', name: 'Corin', kind: 'npc', x: 9, y: 9 }, { id: 'd', name: 'Fass', kind: 'object', x: 10, y: 9 },
  { id: 'e', name: 'Wache', kind: 'enemy', x: 5, y: 5 }, { id: 'f', name: 'Wache 2', kind: 'enemy', x: 5, y: 6 }];
console.log(JSON.stringify({
  river: H.pathCells(m.layers.paths[0], m.cols, m.rows),
  road: H.pathCells(m.layers.paths[1], m.cols, m.rows),
  reach: [...H.reachCells(m.cols, m.rows, blocked, [1, 2], 3)],
  squares: [H.speedSquares(30, 5), H.speedSquares('25 ft', 5), H.speedSquares(null, 5), H.speedSquares(40, 10)],
  labelled: [...H.labelled(tokens, 'Wache')].sort(),
  exits: m.layers.objects.map(o => H.exitKind(o, m.cols, m.rows)),
  shortNames: ['Ingenieurin Vell', 'Schleusenwärter 2', 'Knorz', 'Brunnenwachenhauptmann'].map(H.shortName),
  outer: H.outerCorners(new Set(['1,1']), 1, 1),
  inner: H.innerCorners(new Set(['0,1', '1,0']), 1, 1),
  wall: H.wallMask(new Set(['1,1', '1,0', '2,1']), 1, 1),
  tipObj: M.describe({ what: 'object', type: 'bridge', label: '' }, []),
  tipExit: M.describe({ what: 'object', type: 'exit', label: 'Zum Dorf' }, []),
  tipTok: M.describe({ what: 'token', tok: { name: 'Alrik', kind: 'pc' } },
                     [{ name: 'Alrik', hp: { current: 5, max: 12 }, conditions: ['Vergiftet'] }]),
  tipEnemy: M.describe({ what: 'token', tok: { name: 'Wache', kind: 'enemy', stats: { hp: { current: 3, max: 7 } } } }, []),
  legend: M.legendItems().map(i => i.text),
  shapes: Object.fromEntries(Object.entries(H.KINDS).map(([k, x]) => [k, [x.shape, x.glyph]])),
}));
"""


@unittest.skipIf(shutil.which("node") is None, "node not installed")
class RendererHelperTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.m = furt()
        cls.blocked = v.blockers(cls.m)
        data = json.dumps({"map": cls.m, "blocked": [list(c) for c in cls.blocked]})
        proc = subprocess.run(["node", "-e", HARNESS, str(JS / "map-render.js")], input=data,
                              capture_output=True, text=True, encoding="utf-8", timeout=60)
        if proc.returncode:
            raise AssertionError(proc.stderr)
        cls.out = json.loads(proc.stdout.strip().splitlines()[-1])

    def test_paths_are_the_same_cells_as_on_the_server(self):
        for i, name in ((0, "river"), (1, "road")):
            py = [list(c) for c in v.path_cells(self.m["layers"]["paths"][i], 18, 12)]
            self.assertEqual(self.out[name], py, name)

    def test_reach_is_the_same_as_on_the_server(self):
        py = {f"{x},{y}" for x, y in v.reach(self.m, (1, 2), 3, self.blocked)}
        self.assertEqual(set(self.out["reach"]), py)

    def test_speed_in_squares(self):
        self.assertEqual(self.out["squares"], [6, 5, 6, 4])

    def test_names_stay_for_players_the_active_figure_and_figures_alone(self):
        # Alrik (pc) and Wache (active) always; Corin alone (the barrel next to him does not count);
        # Bela next to Alrik, Wache 2 next to Wache and the barrel next to Corin: name on hover only
        self.assertEqual(self.out["labelled"], ["a", "c", "e"])

    def test_exits_point_out_of_the_map_doors_and_stairs_get_icons(self):
        kinds = {o["type"]: k for o, k in zip(self.m["layers"]["objects"], self.out["exits"])}
        self.assertEqual(kinds["exit"], {"arrow": [-1, 0]})
        self.assertEqual(kinds["door"], {"icon": "door"})
        self.assertEqual(kinds["stairs"], {"icon": "stairs"})

    def test_short_names(self):
        self.assertEqual(self.out["shortNames"], ["Ingenieurin", "Schleusenwärter 2", "Knorz", "Brunnenwach…"])

    def test_autotile_corners_and_wall_mask(self):
        self.assertEqual(self.out["outer"], [True, True, True, True])
        self.assertEqual(self.out["inner"], [True, False, False, False])
        self.assertEqual(self.out["wall"], 1 | 2)

    def test_hints_are_german_and_say_what_you_can_do(self):
        self.assertEqual(self.out["tipObj"]["title"], "Brücke")
        self.assertIn("Begehbar", self.out["tipObj"]["lines"][0])
        self.assertEqual(self.out["tipExit"]["title"], "Zum Dorf")
        self.assertEqual(self.out["tipExit"]["lines"][0], "Ausgang")
        tok = self.out["tipTok"]
        self.assertEqual(tok["title"], "Alrik")
        self.assertIn("Spielerfigur", tok["lines"])
        self.assertIn("Trefferpunkte 5 / 12", tok["lines"])
        self.assertIn("Zustand: Vergiftet", tok["lines"])
        self.assertIn("Angreifen, ausweichen, verhandeln", self.out["tipEnemy"]["lines"])

    def test_kinds_differ_in_shape_and_letter_not_only_colour(self):
        shapes = self.out["shapes"]
        self.assertEqual(len({s for s, _ in shapes.values()}), 4)
        self.assertEqual(len({g for _, g in shapes.values()}), 4)
        self.assertTrue(any("Gegner" in t for t in self.out["legend"]))


class ServerMetaTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from tests.app_isolation import import_app
        cls.mod = import_app("dnd_display_app_p1")

    def test_a_map_message_carries_blocked_squares_and_the_style(self):
        msg = self.mod._map_message(furt())
        meta = msg["map_meta"]
        self.assertIn([3, 0], meta["blocked"])          # the wall at D1
        self.assertNotIn([8, 5], meta["blocked"])        # the bridge over the river
        self.assertEqual(meta["style"]["id"], "neutral")
        self.assertIn("water", meta["style"]["palette"])

    def test_no_map_no_meta(self):
        self.assertEqual(self.mod._map_message(None), {"map": None, "map_tabs": self.mod._maps.tabs()})


class VendorAndNetworkTests(unittest.TestCase):
    def test_pixi_is_the_checked_file(self):
        readme = (DISPLAY / "static" / "vendor" / "README.md").read_text(encoding="utf-8")
        want = re.search(r"sha256 of `pixi.min.js` \| `([0-9a-f]{64})`", readme).group(1)
        got = hashlib.sha256((DISPLAY / "static" / "vendor" / "pixi.min.js").read_bytes()).hexdigest()
        self.assertEqual(got, want)
        self.assertIn("pixi.js - v7.4.2", (DISPLAY / "static" / "vendor" / "pixi.min.js").read_text(encoding="utf-8")[:200])
        self.assertTrue((DISPLAY / "static" / "vendor" / "pixi.LICENSE.txt").read_text(encoding="utf-8").startswith("The MIT License"))

    def test_the_display_loads_no_script_from_another_server(self):
        remote = re.compile(r"""(?:src\s*=\s*["']|\.src\s*=\s*["']|import\s*\(\s*["']|importScripts\s*\(\s*["']|new\s+Worker\s*\(\s*["'])(?:https?:)?//""",
                            re.IGNORECASE)
        files = [DISPLAY / "templates" / "index.html", *sorted((DISPLAY / "static" / "js").glob("*.js"))]
        hits = [f"{f.name}: {m.group(0)}" for f in files for m in remote.finditer(f.read_text(encoding="utf-8"))]
        self.assertEqual(hits, [])
        # the scripts grid.js loads at run time are local paths
        grid = (JS / "grid.js").read_text(encoding="utf-8")
        self.assertIn("'/static/vendor/pixi.min.js'", grid)
        self.assertIn("'/static/js/map-render.js'", grid)
        self.assertNotRegex((JS / "map-render.js").read_text(encoding="utf-8"), r"fetch\(|XMLHttpRequest|WebSocket")


if __name__ == "__main__":
    unittest.main()