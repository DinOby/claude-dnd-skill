"""Phase 1 - B3: the central style file and the style comparison (no Gemini in tests)."""
from __future__ import annotations

import io
import json
import pathlib
import re
import sys
import tempfile
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parent.parent
DISPLAY = ROOT / "skills" / "dnd" / "display"
for p in (str(DISPLAY), str(ROOT / "skills" / "dnd" / "scripts")):
    if p not in sys.path:
        sys.path.insert(0, p)

import style_compare as sc   # noqa: E402


class StyleFileTests(unittest.TestCase):
    def setUp(self):
        self.cfg = json.loads((DISPLAY / "config" / "map-style.json").read_text(encoding="utf-8"))

    def test_active_style_exists_and_rules_are_set(self):
        self.assertIn(self.cfg["active"], self.cfg["styles"])
        rules = self.cfg["rules"]
        for k in ("perspective", "light", "readability", "forbidden"):
            self.assertTrue(rules.get(k), k)

    def test_four_candidates_with_prompt_and_a_full_palette(self):
        cands = {k: v for k, v in self.cfg["styles"].items() if v.get("candidate")}
        self.assertEqual(len(cands), 4)
        neutral = set(self.cfg["styles"]["neutral"]["palette"])
        for sid, st in cands.items():
            self.assertTrue(st["prompt"] and st["name"] and st["description"], sid)
            self.assertLessEqual(neutral, set(st["palette"]), sid)
            for col in st["palette"].values():
                self.assertRegex(col, r"^#[0-9a-f]{6}$")


class CompareTests(unittest.TestCase):
    def test_the_test_map_is_valid_and_crossable(self):
        import map_v2
        m = sc.test_map()
        way = map_v2.find_path(m, (0, 6), (21, 6))
        self.assertIsNotNone(way)                               # over the bridge
        self.assertTrue(any(c[0] in (9, 10, 11) for c in way))

    def test_prompts_carry_the_style_and_the_rules(self):
        st = {"prompt": "ink on parchment"}
        p = sc.texture_prompt(st, {"light": "light from the upper left"}, "grass")
        self.assertIn("ink on parchment", p)
        self.assertIn("Seamless", p)
        self.assertIn("no text", p)
        self.assertIn("magenta", sc.sprite_prompt(st, {}, "a tree"))

    def test_seamless_texture_has_matching_edges(self):
        from PIL import Image
        img = Image.new("RGB", (64, 64))
        for x in range(64):
            for y in range(64):
                img.putpixel((x, y), (x * 4, y * 4, 0))         # a hard seam when tiled
        buf = io.BytesIO(); img.save(buf, "PNG")
        out = Image.open(io.BytesIO(sc.make_seamless(buf.getvalue(), 64))).convert("RGB")
        left = [out.getpixel((0, y))[0] for y in range(64)]
        right = [out.getpixel((63, y))[0] for y in range(64)]
        self.assertLess(max(abs(a - b) for a, b in zip(left, right)), 40)

    def test_plan_skips_pictures_that_exist(self):
        with tempfile.TemporaryDirectory() as d:
            styles = {"a": {"prompt": "x"}}
            self.assertEqual(len(sc.plan(styles, d)), 6)
            (pathlib.Path(d) / "a").mkdir()
            (pathlib.Path(d) / "a" / "grass.jpg").write_bytes(b"x")
            self.assertEqual(len(sc.plan(styles, d)), 5)

    def test_the_page_is_self_contained(self):
        cfg = json.loads((DISPLAY / "config" / "map-style.json").read_text(encoding="utf-8"))
        with tempfile.TemporaryDirectory() as d:
            page = sc.build_page({"aquarell": cfg["styles"]["aquarell"]}, cfg["styles"], d,
                                 str(pathlib.Path(d) / "s.html"))
            html = pathlib.Path(page).read_text(encoding="utf-8")
        self.assertIn("pixi.js - v7.4.2", html)
        self.assertIn("MapRenderer", html)
        self.assertIn("Furt im Wald", html)
        self.assertEqual(re.findall(r"<script[^>]+src=", html), [])
        self.assertNotRegex(html, r"(?:src|href)=[\"'](?:https?:)?//")

    def test_a_run_never_passes_its_cost_cap(self):
        calls = []

        class FakeProv:
            paid, model = True, "m"
            def available(self): return True, "ok"
            def generate(self, req):
                calls.append(req)
                raise RuntimeError("no network in tests")

        class FakeBudget:
            def image_cost(self, model): return 0.04
            def check(self, cost): pass
            def charge(self, *a, **k): pass

        jobs = sc.plan({"a": {"prompt": "x"}, "b": {"prompt": "y"}}, tempfile.mkdtemp())
        with mock.patch("image_providers.load_provider", return_value=FakeProv()), \
             mock.patch("gemini_budget.Budget", FakeBudget):
            made, spent = sc.generate(jobs, {"a": {"prompt": "x"}, "b": {"prompt": "y"}}, {}, 0.1, log=lambda *_: None)
        self.assertEqual((made, spent), (0, 0.0))     # failures cost nothing and are not counted


if __name__ == "__main__":
    unittest.main()