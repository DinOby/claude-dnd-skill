"""build_supplemental.py must not turn German sheet text into rules entries.

On a German character sheet it used to pick up group headings ("### Barbar"),
table headers ("Damage", "Notes") and damage types ("Hieb", "Stich") as
spells/features, and stored a wikidot stub for each one.
"""
from __future__ import annotations

import importlib.util
import pathlib
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "skills" / "dnd" / "scripts"
sys.path.insert(0, str(SCRIPTS))
_spec = importlib.util.spec_from_file_location("build_supplemental", SCRIPTS / "build_supplemental.py")
bs = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bs)

SHEET = """# Grosch Zahnbrecher

## Attacks
| Name | Attack Bonus | Damage | Type | Notes |
|------|-------------|--------|------|-------|
| Zweihandaxt (Greataxe) | +5 | 1d12+3 | Hieb | Mastery **Cleave** |
| Speer | +5 | 1d6+3 | Stich | Wurf 20/60 ft |

## Features & Traits

### Barbar
- **Rage (Kampfrausch)** — 2 Nutzungen.

### Soldat Background
- **Origin Feat: Savage Attacker (Wilder Angreifer)** — einmal pro Zug.

## Equipment & Inventory
"""


class ExtractTests(unittest.TestCase):
    def setUp(self):
        self._d = tempfile.TemporaryDirectory()
        self.path = pathlib.Path(self._d.name) / "grosch.md"
        self.path.write_text(SHEET, encoding="utf-8")

    def tearDown(self):
        self._d.cleanup()

    def names(self):
        return [n for n, _ in bs._extract_names_from_character(str(self.path))]

    def test_group_headings_are_not_features(self):
        self.assertNotIn("Barbar", self.names())
        self.assertNotIn("Soldat Background", self.names())

    def test_only_the_first_attack_cell_is_a_name(self):
        names = self.names()
        for junk in ("Hieb", "Stich", "Damage", "Notes", "Name", "Cleave"):
            self.assertNotIn(junk, names)
        self.assertIn("Zweihandaxt (Greataxe)", names)

    def test_bullet_features_are_found(self):
        self.assertIn("Rage (Kampfrausch)", self.names())

    def test_english_half_of_a_bilingual_name_is_looked_up(self):
        # Both halves are checked against the SRD before anything is fetched,
        # so "Greataxe" being known is enough to skip the row.
        self.assertIn("Greataxe", bs._name_variants("Zweihandaxt (Greataxe)"))
        self.assertEqual(bs._fetch_name("Rage (Kampfrausch)"), "Rage")
        self.assertEqual(bs._fetch_name("Origin Feat: Savage Attacker (Wilder Angreifer)"),
                         "Savage Attacker")

    def test_german_only_name_with_umlaut_is_not_looked_up(self):
        self.assertIsNone(bs._fetch_name("Handaxt ×4"))
        self.assertIsNone(bs._fetch_name("Fälscherausrüstung"))


if __name__ == "__main__":
    unittest.main()
