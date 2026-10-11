"""Inventory lines in German still open their rules entry on the display.

Clicking "Einbrecherpaket" on a German sheet said "not in the local dataset";
the SRD only knows "Burglar's Pack". item_names.py turns the line into names
worth looking up.

Run from repo root:
    python3 -m unittest tests.test_item_names -v
"""
from __future__ import annotations

import pathlib
import sys
import tempfile
import unittest

REPO = pathlib.Path(__file__).resolve().parent.parent
SKILL = REPO / "skills" / "dnd" if (REPO / "skills" / "dnd").is_dir() else REPO
DISPLAY = SKILL / "display"
sys.path.insert(0, str(DISPLAY))

import item_names as n  # noqa: E402

ALIASES = n.german_aliases(str(DISPLAY / "config"))


def srd_names(text, items=None):
    return [v for k, v in n.resolve(text, ALIASES, items) if k == "srd"]


class CandidateTests(unittest.TestCase):
    def test_counts_and_bundles_are_stripped(self):
        self.assertIn("Handaxt", n.candidates("4 Handäxte"))
        self.assertIn("Handaxt", n.candidates("Handaxt (4)"))
        self.assertIn("Handaxt", n.candidates("Handaxt ×4"))
        self.assertIn("Kurzbogen", n.candidates("Kurzbogen + 20 Pfeile"))
        self.assertIn("Diebeswerkzeug", n.candidates("2x Diebeswerkzeug"))

    def test_bracketed_name_is_tried_too(self):
        self.assertIn("Greataxe", n.candidates("Zweihandaxt (Greataxe)"))

    def test_empty(self):
        self.assertEqual(n.candidates("  "), [])


class AliasTests(unittest.TestCase):
    def test_catalogue_and_extra_aliases(self):
        self.assertEqual(ALIASES["zweihandaxt"], "Greataxe")       # from asset-seed.json
        self.assertEqual(ALIASES["einbrecherpaket"], "Burglar's Pack")   # from item-aliases.json

    def test_inventory_lines_reach_the_srd_name(self):
        self.assertIn("Handaxe", srd_names("4 Handäxte"))
        self.assertIn("Shortbow", srd_names("Kurzbogen + 20 Pfeile"))
        self.assertIn("Thieves' Tools", srd_names("2x Diebeswerkzeug"))
        self.assertEqual(srd_names("Einbrecherpaket")[0], "Burglar's Pack")
        self.assertIn("Healer's Kit", srd_names("Heilerset (10)"))


class CampaignItemTests(unittest.TestCase):
    def test_items_md_wins_before_the_srd(self):
        with tempfile.TemporaryDirectory() as d:
            p = pathlib.Path(d) / "items.md"
            p.write_text("# Gegenstände\n\n## Das Schuldbuch\nGerüchte, Schuldscheine, Namen.\n\n"
                         "## Alte Schleusenwärter-Marke\nMessing, ungültig.\n", encoding="utf-8")
            items = n.campaign_items(str(p))
            self.assertEqual(items["das schuldbuch"], ("Das Schuldbuch", "Gerüchte, Schuldscheine, Namen."))
            self.assertEqual(n.resolve("Das Schuldbuch", ALIASES, items)[0], ("campaign", "das schuldbuch"))

    def test_missing_items_md(self):
        self.assertEqual(n.campaign_items("/nonexistent/items.md"), {})


if __name__ == "__main__":
    unittest.main()
