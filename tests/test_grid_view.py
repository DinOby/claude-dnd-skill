"""Tests for the browser side of the battle grid: static/js/grid.js and static/js/scene-mode.js.

The pure helpers run in node (vm sandbox, no DOM) like the vfx.js tests;
skipped when node is not installed.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SKILL = REPO / "skills" / "dnd" if (REPO / "skills" / "dnd").is_dir() else REPO
DISPLAY = SKILL / "display"
JS = DISPLAY / "static" / "js"

NODE_HARNESS = r"""
const fs = require('fs');
const vm = require('vm');
const files = process.argv.slice(-4);

const sandbox = { location: { search: '' }, URLSearchParams, console };
sandbox.window = sandbox;
for (const f of files) vm.runInNewContext(fs.readFileSync(f, 'utf8'), sandbox);   // no document → no DOM work
const w = sandbox.window, G = w.GridView, S = w.SceneMode;

const map = { id: 'kessel', rev: 3, cols: 14, rows: 10, tokens: [
  { id: 'flerb', name: 'Flerb', kind: 'pc', x: 1, y: 7, size: 1 },
  { id: 'goblin', name: 'Goblin', kind: 'enemy', x: 9, y: 6, size: 1 }] };
const patched = G._applyPatch(map, { map_id: 'kessel', base: 3, rev: 4,
  move: [{ id: 'flerb', x: 3, y: 4 }], add: [{ id: 'goblin-2', name: 'Goblin', kind: 'enemy', x: 10, y: 6, size: 1 }],
  remove: ['goblin'] });

let notified = [];
G.onChange(m => notified.push(m && m.id));
w.DisplayModules.dispatch({ map: map });
w.DisplayModules.dispatch({ map_patch: { map_id: 'kessel', base: 3, rev: 4, move: [{ id: 'goblin', x: 0, y: 0 }] } });
const afterPatch = G.current();
w.DisplayModules.dispatch({ map: map, map_images: { floor: '/f.png', terrain: { table: '/t.png' }, tokens: { flerb: '/p.png' } } });
w.DisplayModules.dispatch({ map_patch: { map_id: 'kessel', base: 3, rev: 4, add: [{ id: 'g2', name: 'Goblin', kind: 'enemy', x: 1, y: 1, size: 1 }] },
                            map_images: { tokens: { g2: '/g.png' } } });
const imagesAfterPatch = G._images();
w.DisplayModules.dispatch({ stats: { turn_order: { current: 'Flerb', order: ['Flerb'] } } });
const turnAfter = G._turn();
w.DisplayModules.dispatch({ clear: true });

const travel = { mode: 'travel', travel: { from: 'Ashveil', to: 'Dornfeld', via: 'Königsstraße', day: 2, days_total: 4, terrain: 'forest' } };
const event = { mode: 'travel_event', travel: travel.travel, event: { title: 'Wolfsrudel am Waldrand' } };
const stationary = { mode: 'stationary', location: 'Ashveil' };

console.log(JSON.stringify({
  registered: w.DisplayModules.list(),
  initials: ['Flerb', 'Wirtin Hilde', 'Goblin 2', 'goblin-3', 'Ölaf', '', 'ab cd ef'].map(G._initials),
  labels: [0, 3, 25, 26, 27].map(G._colLabel),
  cells: [G._cellSize(14, 10, 800, 500), G._cellSize(14, 10, 0, 500), G._cellSize(60, 60, 100, 100)],
  patched: patched,
  input_untouched: map.tokens[0].x,
  stale: G._applyPatch(map, { map_id: 'kessel', base: 2, rev: 3, move: [] }),
  other_map: G._applyPatch(map, { map_id: 'x', base: 3, rev: 4 }),
  unknown_token: G._applyPatch(map, { map_id: 'kessel', base: 3, rev: 4, move: [{ id: 'nobody', x: 0, y: 0 }] }),
  no_map: G._applyPatch(null, { map_id: 'kessel', base: 3 }),
  after_patch: [afterPatch.rev, afterPatch.tokens[1].x],
  after_clear: G.current(),
  notified: notified,
  grid: {
    auto_stationary_map: S._showGrid(stationary, true, 'auto'),
    auto_stationary_nomap: S._showGrid(stationary, false, 'auto'),
    auto_travel: S._showGrid(travel, true, 'auto'),
    auto_event: S._showGrid(event, true, 'auto'),
    auto_no_state: S._showGrid(null, true, 'auto'),
    scene_mode: S._showGrid(event, true, 'scene'),
    grid_mode_travel: S._showGrid(travel, true, 'grid'),
    grid_mode_nomap: S._showGrid(travel, false, 'grid'),
  },
  banners: [S._bannerFor(travel), S._bannerFor(event), S._bannerFor(stationary), S._bannerFor(null),
            S._bannerFor({ mode: 'travel', travel: { to: 'X', day: 0, days_total: 3 } })],
  vfxHook: w.VfxOverlay.anchorFor === G.tokenCenter,
  anchorHidden: G.tokenCenter('Flerb'),
  found: ['Flerb', 'flerb', 'FLERB', 'Goblin 2', 'goblin-2', 'nobody', ''].map(r => {
    const t = G._findToken([{ id: 'flerb', name: 'Flerb' }, { id: 'goblin-2', name: 'Goblin' },
                            { id: 'spy', name: 'Spion', hidden: true }], r);
    return t ? t.id : null;
  }),
  hiddenNotFound: G._findToken([{ id: 'spy', name: 'Spion', hidden: true }], 'Spion'),
  turnIds: [G._turnId('Goblin 2'), G._turnId('Wirtin Hilde'), G._turnId('Ölaf'), G._turnId('')],
  sprite: [G._spriteMode('table'), G._spriteMode('Chair'), G._spriteMode('tree'), G._spriteMode('wall'), G._spriteMode('whatever')],
  spriteBg: [G._spriteBackground('/s/table.png', 'table'), G._spriteBackground('/s/a"b.png', 'tree')],
  merged: G._mergeImages({ floor: '/f', terrain: { a: 1 }, tokens: { x: 1 } }, { tokens: { y: 2 } }),
  imagesAfterPatch: imagesAfterPatch,
  turnAfter: turnAfter,
  imagesAfterClear: G._images(),
  modes: [S._nextMode('auto'), S._nextMode('scene'), S._nextMode('grid'), S._nextMode('bogus')],
}));
"""


@unittest.skipIf(shutil.which("node") is None, "node not installed")
class GridScriptTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        proc = subprocess.run(["node", "-e", NODE_HARNESS, str(JS / "modules.js"), str(JS / "vfx.js"),
                               str(JS / "grid.js"), str(JS / "scene-mode.js")],
                              capture_output=True, text=True, encoding="utf-8", timeout=30)
        if proc.returncode != 0:
            raise AssertionError(f"node harness failed:\n{proc.stderr}")
        cls.out = json.loads(proc.stdout)

    def test_registration_order(self):
        self.assertEqual(self.out["registered"], ["vfx", "grid", "scene-mode"])   # page order: grid state before the rule

    def test_overlays_anchor_on_tokens(self):
        self.assertTrue(self.out["vfxHook"])                 # grid.js wires VfxOverlay.anchorFor
        self.assertIsNone(self.out["anchorHidden"])          # grid not on screen → overlay stays centred
        self.assertEqual(self.out["found"], ["flerb", "flerb", "flerb", "goblin-2", "goblin-2", None, None])
        self.assertIsNone(self.out["hiddenNotFound"])        # the DM's hidden tokens never get an overlay

    def test_initials_and_labels(self):
        self.assertEqual(self.out["initials"], ["Fl", "WH", "G2", "G3", "Öl", "?", "AC"])
        self.assertEqual(self.out["labels"], ["A", "D", "Z", "AA", "AB"])
        self.assertEqual(self.out["cells"], [46, 0, 8])   # min(800/14.8, 500/10.8)

    def test_patch(self):
        p = self.out["patched"]
        self.assertEqual(p["rev"], 4)
        self.assertEqual([(t["id"], t["x"], t["y"]) for t in p["tokens"]], [("flerb", 3, 4), ("goblin-2", 10, 6)])
        self.assertEqual(self.out["input_untouched"], 1)
        for key in ("stale", "other_map", "unknown_token", "no_map"):
            self.assertIsNone(self.out[key], key)   # → the browser re-reads GET /map

    def test_messages_update_state(self):
        self.assertEqual(self.out["after_patch"], [4, 0])
        self.assertIsNone(self.out["after_clear"])
        self.assertEqual(self.out["notified"], ["kessel", "kessel", "kessel", "kessel", None])

    def test_pictures(self):
        self.assertEqual(self.out["sprite"], ["stretch", "stretch", "tile", "tile", "tile"])
        self.assertEqual(self.out["spriteBg"], ['url("/s/table.png") center / 100% 100% no-repeat',
                                                'url("/s/a%22b.png") 0 0 / var(--cell) var(--cell) repeat'])
        self.assertEqual(self.out["merged"], {"floor": "/f", "terrain": {"a": 1}, "tokens": {"x": 1, "y": 2}})
        after = self.out["imagesAfterPatch"]
        self.assertEqual((after["floor"], after["terrain"], after["tokens"]),
                         ("/f.png", {"table": "/t.png"}, {"flerb": "/p.png", "g2": "/g.png"}))
        self.assertEqual(self.out["turnAfter"], "flerb")
        self.assertEqual(self.out["turnIds"], ["goblin-2", "wirtin-hilde", "oelaf", ""])
        self.assertEqual(self.out["imagesAfterClear"], {"floor": None, "terrain": {}, "tokens": {}})

    def test_display_rule(self):
        self.assertEqual(self.out["grid"], {
            "auto_stationary_map": True, "auto_stationary_nomap": False, "auto_travel": False,
            "auto_event": True, "auto_no_state": True, "scene_mode": False,
            "grid_mode_travel": True, "grid_mode_nomap": False})

    def test_banners(self):
        travel, event, stationary, none, day0 = self.out["banners"]
        self.assertEqual(travel, {"kind": "travel", "kicker": "On the road", "title": "Ashveil → Dornfeld",
                                  "sub": "Day 2 of 4 · via Königsstraße · forest"})
        self.assertEqual(event, {"kind": "event", "kicker": "Travel event", "title": "Wolfsrudel am Waldrand",
                                 "sub": "Day 2 of 4 · on the way to Dornfeld"})
        self.assertIsNone(stationary)
        self.assertIsNone(none)
        self.assertEqual(day0["sub"], "Day 1 of 3")          # before the first `travel.py day`
        self.assertEqual(self.out["modes"], ["scene", "grid", "auto", "auto"])


class PageWiringTests(unittest.TestCase):
    def test_index_loads_modules_in_order_and_has_the_setting(self):
        html = (DISPLAY / "templates" / "index.html").read_text(encoding="utf-8")
        order = [html.index(f'src="/static/js/{name}"') for name in ("modules.js", "assets.js", "grid.js", "scene-mode.js")]
        self.assertEqual(order, sorted(order))
        self.assertLess(order[-1], html.index("function connect()"))
        self.assertIn('id="mapview-row"', html)
        self.assertIn('id="mapview-label"', html)

    def test_stylesheets_exist(self):
        for name in ("grid.css", "scene-mode.css"):
            self.assertTrue((DISPLAY / "static" / "css" / name).is_file(), name)


if __name__ == "__main__":
    unittest.main()
