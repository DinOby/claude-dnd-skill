"""Tests for scripts/maps.py — list, reset (archive) and restore of battle-map layouts."""

from __future__ import annotations

import importlib.util
import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SKILL = REPO / "skills" / "dnd" if (REPO / "skills" / "dnd").is_dir() else REPO
for p in (str(SKILL / "display"), str(SKILL / "scripts")):
    if p not in sys.path:
        sys.path.insert(0, p)

from asset_store import AssetStore  # noqa: E402
import grid_map as gm               # noqa: E402

PNG = b"\x89PNG\r\n\x1a\n img"


def _load_cli():
    spec = importlib.util.spec_from_file_location("_maps_cli_under_test", str(SKILL / "scripts" / "maps.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class MapsLibraryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cli = _load_cli()

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        root = Path(self._tmp.name)
        self.libdir, self.camps, self.assets = root / "maps" / "library", root / "campaigns", root / "assets"
        self.store = AssetStore(global_root=str(self.assets))
        self.lib = self.cli.Library(self.libdir, self.camps, self.store)
        maps = gm.MapStore(str(self.libdir), str(self.camps / "ashveil" / "maps"))
        maps.set_map({"template": "tavern-small", "id": "kessel", "tokens": [{"name": "Flerb", "kind": "pc"}]})
        maps.set_map({"template": "forest-road", "id": "koenigsstrasse"})
        # the tavern has its own generated picture; the road uses the generic seed image
        (self.assets / "maps").mkdir(parents=True)
        (self.assets / "maps" / "kessel.png").write_bytes(PNG)
        (self.assets / "maps" / "waldweg.png").write_bytes(PNG)
        self.store.global_manifest.set_entry("map:kessel", {"file": "maps/kessel.png", "category": "map"})
        self.store.global_manifest.set_entry("map:waldweg", {"file": "maps/waldweg.png", "category": "map",
                                                             "generic": True})

    def run_cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            rc = self.cli.main(list(argv), lib=self.lib)
        return rc, out.getvalue(), err.getvalue()

    def test_list(self):
        rows = {r["id"]: r for r in self.lib.rows()}
        self.assertEqual((rows["kessel"]["image"], rows["kessel"]["campaigns"]), ("own", ["ashveil"]))
        self.assertEqual((rows["koenigsstrasse"]["image"], rows["koenigsstrasse"]["template"]), ("generic", "forest-road"))
        rc, out, _ = self.run_cli("list", "--tag", "tavern")
        self.assertIn("kessel", out)
        self.assertNotIn("koenigsstrasse", out)

    def test_reset_archives_layout_and_own_image_only(self):
        rc, out, _ = self.run_cli("reset", "--all", "--dry-run")
        self.assertIn("would archive kessel: layout + own image", out)
        self.assertTrue((self.libdir / "kessel.json").exists())
        rc, out, _ = self.run_cli("reset", "--all")
        self.assertEqual(rc, 0)
        self.assertEqual(self.lib.ids(), [])
        self.assertIsNone(self.store.global_manifest.entry("map:kessel"))
        self.assertFalse((self.assets / "maps" / "kessel.png").exists())
        self.assertTrue((self.assets / "maps" / "waldweg.png").exists())             # generic stays
        self.assertIsNotNone(self.store.global_manifest.entry("map:waldweg"))
        self.assertTrue((self.camps / "ashveil" / "maps" / "kessel.json").exists())  # placement stays
        archived = self.lib.archived()
        self.assertEqual(sorted(a["id"] for a in archived), ["kessel", "koenigsstrasse"])
        self.assertTrue(next(a for a in archived if a["id"] == "kessel")["image"])

    def test_keep_image_and_unknown_id(self):
        rc, _, _ = self.run_cli("reset", "kessel", "--keep-image")
        self.assertEqual(rc, 0)
        self.assertTrue((self.assets / "maps" / "kessel.png").exists())
        rc, _, err = self.run_cli("reset", "nirgendwo")
        self.assertEqual(rc, 1)
        self.assertIn("no map 'nirgendwo'", err)
        self.assertEqual(self.run_cli("reset")[0], 1)

    def test_restore_brings_back_layout_and_image(self):
        self.run_cli("reset", "kessel")
        # rebuilt from the template in between, then the old one is wanted back
        gm.MapStore(str(self.libdir), str(self.camps / "b")).set_map({"template": "camp", "id": "kessel"})
        rc, out, _ = self.run_cli("restore", "kessel")
        self.assertEqual(rc, 0)
        self.assertEqual(json.loads((self.libdir / "kessel.json").read_text(encoding="utf-8"))["template"],
                         "tavern-small")
        self.assertTrue((self.assets / "maps" / "kessel.png").exists())
        self.assertEqual(self.store.global_manifest.entry("map:kessel")["file"], "maps/kessel.png")
        self.assertEqual(len(self.lib.archived("kessel")), 2)                    # the camp version is archived too
        rc, _, err = self.run_cli("restore", "koenigsstrasse")
        self.assertEqual(rc, 1)
        self.assertIn("nothing archived", err)


if __name__ == "__main__":
    unittest.main()
