"""Painted map from a sketch (battlemap.py) - everything except the painter itself.

The painter is Gemini; here a fake stands in, so no test ever costs money.
"""
from __future__ import annotations

import io
import json
import pathlib
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
DISPLAY = ROOT / "skills" / "dnd" / "display"
for p in (str(DISPLAY), str(ROOT / "skills" / "dnd" / "scripts")):
    if p not in sys.path:
        sys.path.insert(0, p)

import battlemap as bm   # noqa: E402
import grid_map as gm    # noqa: E402
import map_v2 as v       # noqa: E402
from PIL import Image    # noqa: E402


def tavern():
    return gm.normalize_map({"id": "schenke", "name": "Schenke", "cols": 10, "rows": 8, "floor": "wood",
        "terrain": [{"at": "A1", "w": 10, "type": "wall"}, {"at": "A8", "w": 10, "type": "wall"},
                    {"at": "A2", "h": 6, "type": "wall"}, {"at": "J2", "h": 6, "type": "wall"},
                    {"at": "D4", "w": 2, "type": "table"}, {"at": "D3", "type": "chair"},
                    {"at": "E8", "type": "door"}, {"at": "H3", "type": "fireplace"}]})


def paint_from_sketch(m, shift=0):
    """A 'perfect painter': the sketch itself, optionally shifted by `shift` squares."""
    png, box, _ = bm.sketch(m)
    img = Image.open(io.BytesIO(png)).convert("RGB")
    if shift:
        cell = bm.SKETCH_CELL
        moved = Image.new("RGB", img.size, bm.PAD)
        moved.paste(img, (shift * cell, 0))
        img = moved
    return img, box


class FurnitureTests(unittest.TestCase):
    def test_furniture_blocks_by_default_and_can_be_overridden(self):
        self.assertEqual(v.object_props({"type": "table"}), (True, "half"))
        self.assertEqual(v.object_props({"type": "rug"}), (False, "none"))
        self.assertEqual(v.object_props({"type": "table", "blocks": False, "cover": "full"}), (False, "full"))
        m = gm.normalize_map({"id": "x", "cols": 4, "rows": 3,
                              "terrain": [{"at": "B2", "type": "table", "blocks": False, "cover": "half"}]})
        self.assertNotIn((1, 1), v.blockers(m))
        self.assertEqual(m["layers"]["objects"][0]["cover"], "half")
        with self.assertRaises(gm.MapError):
            gm.normalize_map({"id": "x", "cols": 4, "rows": 3, "terrain": [{"at": "B2", "type": "table", "cover": "lots"}]})

    def test_one_figure_per_square(self):
        m = gm.normalize_map({"id": "x", "cols": 5, "rows": 5,
                              "tokens": [{"name": "Alrik", "kind": "pc", "at": "B2"}]})
        with self.assertRaises(gm.MapError) as err:
            gm.apply_patch(m, {"add": [{"name": "Bela", "kind": "npc", "at": "B2"}]})
        self.assertIn("B2 is taken by Alrik", err.exception.problems[0])
        new, _, _ = gm.apply_patch(m, {"add": [{"name": "Kiste", "kind": "object", "at": "B2"}]})
        self.assertEqual(len(new["tokens"]), 2)            # an object marker may share


class SketchAndCheckTests(unittest.TestCase):
    def test_the_sketch_puts_every_square_where_the_logic_map_has_it(self):
        m = tavern()
        img, box = paint_from_sketch(m)
        crop = img.crop(box)
        self.assertTrue(bm.verify(m, crop)["ok"])
        self.assertEqual(crop.size, (10 * bm.SKETCH_CELL, 8 * bm.SKETCH_CELL))

    def test_a_shifted_painting_fails(self):
        m = tavern()
        img, box = paint_from_sketch(m, shift=1)
        r = bm.verify(m, img.crop(box))
        self.assertFalse(r["ok"])
        self.assertTrue(r["reason"])

    def test_inventory_names_every_object_with_its_squares(self):
        text = bm.inventory(tavern())
        self.assertIn("table (D4-E4)", text)
        self.assertIn("fireplace (H3)", text)
        self.assertIn("nothing else", text)

    def test_prompt_comes_from_the_style_file(self):
        p = bm.build_prompt(tavern())
        self.assertIn("Schenke", p)
        self.assertIn("10 by 8", p)
        self.assertIn("black = thick stone walls", p)

    def test_layout_hash_changes_with_layout_style_and_prompt(self):
        m = tavern()
        h = bm.layout_hash(m, "neutral", 1)
        self.assertNotEqual(h, bm.layout_hash(m, "aquarell", 1))
        self.assertNotEqual(h, bm.layout_hash(m, "neutral", 2))
        m2 = json.loads(json.dumps(m)); m2["layers"]["objects"][0]["x"] = 6
        self.assertNotEqual(h, bm.layout_hash(m2, "neutral", 1))


class PaintTests(unittest.TestCase):
    def _fake(self, shifts):
        calls = []

        class Prov:
            paid, model = True, "fake"
            def available(self): return True, "fake"
            def generate(self, req):
                calls.append(req)
                img, _ = paint_from_sketch(tavern(), shift=shifts[min(len(calls) - 1, len(shifts) - 1)])
                buf = io.BytesIO(); img.save(buf, "PNG")
                from image_providers import ImageResult
                return ImageResult(data=buf.getvalue(), mime="image/png")

        class Budget:
            charged = []
            def image_cost(self, model): return 0.03
            def check(self, cost): pass
            def charge(self, kind, cost, detail=""): Budget.charged.append(detail)
        return Prov(), Budget(), calls

    def test_retries_then_stores_and_is_reused(self):
        prov, budget, calls = self._fake([1, 0])
        with tempfile.TemporaryDirectory() as d:
            meta = bm.paint(tavern(), prov, budget, root=d, log=lambda *_: None)
            self.assertTrue(meta["ok"])
            self.assertEqual((meta["attempts"], len(calls)), (2, 2))
            self.assertIsNotNone(calls[0].reference)               # the sketch goes along
            self.assertTrue((pathlib.Path(d) / "schenke.jpg").exists())
            url, _ = bm.current(tavern(), root=d)
            self.assertIn("/assets/file/global/battlemaps/schenke.jpg", url)
            bm.paint(tavern(), prov, budget, root=d, log=lambda *_: None)
            self.assertEqual(len(calls), 2)                        # reused, not painted again

    def test_three_misses_fall_back_to_tiles(self):
        prov, budget, calls = self._fake([1, 1, 1])
        with tempfile.TemporaryDirectory() as d:
            meta = bm.paint(tavern(), prov, budget, root=d, log=lambda *_: None)
            self.assertFalse(meta["ok"])
            self.assertEqual(len(calls), 3)
            self.assertTrue(meta["reason"])
            url, stored = bm.current(tavern(), root=d)
            self.assertIsNone(url)
            self.assertFalse(stored["ok"])
            self.assertTrue((pathlib.Path(d) / "schenke.rejected.jpg").exists())

    def test_recheck_takes_a_turned_down_picture_that_fits_now(self):
        prov, budget, calls = self._fake([1, 1, 1])
        with tempfile.TemporaryDirectory() as d:
            bm.paint(tavern(), prov, budget, root=d, log=lambda *_: None)
            meta = bm.recheck(tavern(), root=d)                  # a shifted picture still fails
            self.assertFalse(meta["ok"])
            good, box = paint_from_sketch(tavern())
            good.crop(box).save(pathlib.Path(d) / "schenke.rejected.jpg")
            meta = bm.recheck(tavern(), root=d)
            self.assertTrue(meta["ok"])
            self.assertEqual(len(calls), 3)                      # no new painting
            self.assertIsNotNone(bm.current(tavern(), root=d)[0])

    def test_the_cost_cap_stops_a_run(self):
        prov, budget, calls = self._fake([1, 1, 1])
        with tempfile.TemporaryDirectory() as d:
            meta = bm.paint(tavern(), prov, budget, root=d, log=lambda *_: None, max_eur=0.05)
            self.assertEqual(len(calls), 1)
            self.assertFalse(meta["ok"])


class ModelTests(unittest.TestCase):
    def test_painted_maps_use_the_model_from_the_style_file(self):
        from unittest import mock
        seen = {}

        class Prov:
            paid, model = True, "x"
            def available(self): return False, "test stops here"

        def fake_load(name, cfg):
            seen["model"] = cfg["providers"]["gemini"]["model"]
            return Prov()

        with mock.patch("image_providers.load_provider", fake_load), tempfile.TemporaryDirectory() as d:
            with self.assertRaises(RuntimeError):
                bm.paint(tavern(), root=d, log=lambda *_: None)
        want = bm._style_cfg()["painted_map"]["model"]
        self.assertEqual(seen["model"], want)
        self.assertEqual(want, "gemini-3.1-flash-image")


class ServerTests(unittest.TestCase):
    def test_tests_never_start_the_painter(self):
        from tests.app_isolation import import_app
        mod = import_app("dnd_display_app_paint")
        self.assertFalse(mod._painter_enabled())
        meta = mod._map_meta(tavern())
        self.assertEqual(meta["painted"]["status"], "off")

    def test_looking_at_a_tab_never_starts_a_painting(self):
        from unittest import mock
        from tests.app_isolation import import_app
        mod = import_app("dnd_display_app_paint2")
        with mock.patch.object(mod._painter, "request") as req,              mock.patch.object(mod, "_painter_enabled", return_value=True):
            mod._map_meta(tavern(), paint=False)
            req.assert_not_called()
            mod._map_meta(tavern())
            req.assert_called_once()


if __name__ == "__main__":
    unittest.main()