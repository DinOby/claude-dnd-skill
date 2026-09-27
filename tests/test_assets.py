"""Tests for content images: display/asset_store.py, its routes, and assets.js."""

from __future__ import annotations

import importlib.util
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest import mock


REPO = Path(__file__).resolve().parent.parent
SKILL = REPO / "skills" / "dnd" if (REPO / "skills" / "dnd").is_dir() else REPO
DISPLAY = SKILL / "display"
sys.path.insert(0, str(DISPLAY))

PNG = (b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00"
       b"\x1f\x15\xc4\x89\x00\x00\x00\rIDATx\x9cc\xf8\xff\xff?\x00\x05\xfe\x02\xfe\xa7\x35\x81\x84"
       b"\x00\x00\x00\x00IEND\xaeB`\x82")


def _load_module(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, str(path))
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


store_mod = _load_module(DISPLAY / "asset_store.py", "asset_store_under_test")


class KeyTests(unittest.TestCase):
    def test_slugify(self):
        cases = {
            "Flammenschwert der Asche": "flammenschwert-der-asche",
            "Größe & Stärke": "groesse-staerke",
            "Äxte, Übel, Öl": "aexte-uebel-oel",
            "Épée de Flambé": "epee-de-flambe",
            "  --Bag of   Holding--  ": "bag-of-holding",
            "Ring +1": "ring-1",
            "火焰": "",
        }
        for text, slug in cases.items():
            self.assertEqual(store_mod.slugify(text), slug, text)

    def test_item_quantities_are_dropped(self):
        for name in ("Heiltrank (2)", "Heiltrank x5", "Heiltrank ×3", "3x Heiltrank",
                     "2 Heiltrank", "Heiltrank [4]", "heiltrank"):
            self.assertEqual(store_mod.make_key("item", name), "item:heiltrank", name)

    def test_quantities_kept_for_tokens(self):
        self.assertEqual(store_mod.make_key("token", "Goblin 2"), "token:goblin-2")

    def test_empty_and_unknown_kind(self):
        self.assertEqual(store_mod.make_key("item", "  "), "")
        with self.assertRaises(ValueError):
            store_mod.make_key("spell", "Fireball")

    def test_every_category_has_a_placeholder(self):
        for cat in store_mod.CATEGORIES:
            directory, name = store_mod.placeholder_file(cat)
            self.assertTrue((Path(directory) / name).is_file(), cat)
        self.assertEqual(store_mod.placeholder_file("nonsense")[1], "gear.svg")


class StoreTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        base = Path(self._tmp.name)
        self.g = base / "global"
        self.c = base / "campaign"
        for d in (self.g / "items", self.c / "items"):
            d.mkdir(parents=True)
        self.store = store_mod.AssetStore(global_root=str(self.g))

    def tearDown(self):
        self._tmp.cleanup()

    def _manifest(self, root: Path, entries: dict, aliases: dict | None = None):
        (root / "manifest.json").write_text(json.dumps(
            {"version": 1, "entries": entries, "aliases": aliases or {}}), encoding="utf-8")

    def test_resolves_manifest_entry(self):
        (self.g / "items" / "flammenschwert.png").write_bytes(PNG)
        self._manifest(self.g, {"item:flammenschwert-der-asche": {
            "file": "items/flammenschwert.png", "category": "weapon"}})
        info = self.store.resolve("item", "Flammenschwert der Asche")
        self.assertEqual(info["key"], "item:flammenschwert-der-asche")
        self.assertTrue(info["url"].startswith("/assets/file/global/items/flammenschwert.png?v="))
        self.assertEqual(info["category"], "weapon")
        self.assertTrue(self.store.has_image("item", "Flammenschwert der Asche"))

    def test_missing_image_gives_placeholder(self):
        info = self.store.resolve("item", "Unbekanntes Ding")
        self.assertIsNone(info["url"])
        self.assertEqual(info["category"], "gear")
        self.assertEqual(info["placeholder"], "/assets/placeholder/gear.svg")

    def test_entry_whose_file_is_gone_is_a_placeholder(self):
        self._manifest(self.g, {"item:dolch": {"file": "items/dolch.png", "category": "weapon"}})
        self.assertIsNone(self.store.resolve("item", "Dolch")["url"])

    def test_explicit_category_and_token_default(self):
        self.assertEqual(self.store.resolve("item", "Dolch", "weapon")["category"], "weapon")
        self.assertEqual(self.store.resolve("item", "Dolch", "bogus")["category"], "gear")
        self.assertEqual(self.store.resolve("token", "Wirtin Hilde")["category"], "npc")

    def test_categorizer_hook_only_without_entry(self):
        calls = []
        self.store.categorize = lambda kind, name: calls.append(name) or "potion"
        self.assertEqual(self.store.resolve("item", "Heiltrank")["category"], "potion")
        (self.g / "items" / "dolch.png").write_bytes(PNG)
        self._manifest(self.g, {"item:dolch": {"file": "items/dolch.png", "category": "weapon"}})
        self.assertEqual(self.store.resolve("item", "Dolch")["category"], "weapon")
        self.assertEqual(calls, ["Heiltrank"])

    def test_broken_categorizer_is_ignored(self):
        self.store.categorize = lambda kind, name: 1 / 0
        self.assertEqual(self.store.resolve("item", "X")["category"], "gear")

    def test_campaign_wins_over_global(self):
        (self.g / "items" / "a.png").write_bytes(PNG)
        (self.c / "items" / "b.png").write_bytes(PNG)
        self._manifest(self.g, {"item:dolch": {"file": "items/a.png"}})
        self._manifest(self.c, {"item:dolch": {"file": "items/b.png"}})
        self.assertIn("/global/items/a.png", self.store.resolve("item", "Dolch")["url"])
        self.store.set_campaign_root(str(self.c))
        self.assertIn("/campaign/items/b.png", self.store.resolve("item", "Dolch")["url"])
        self.store.set_campaign_root(None)
        self.assertIn("/global/", self.store.resolve("item", "Dolch")["url"])

    def test_alias(self):
        (self.g / "items" / "f.png").write_bytes(PNG)
        self._manifest(self.g, {"item:flammenschwert-der-asche": {"file": "items/f.png"}},
                       {"Flame Sword of Ash": "item:flammenschwert-der-asche"})
        info = self.store.resolve("item", "flame sword of ash")
        self.assertEqual(info["key"], "item:flammenschwert-der-asche")
        self.assertIsNotNone(info["url"])

    def test_path_escapes_are_refused(self):
        secret = Path(self._tmp.name) / "secret.png"
        secret.write_bytes(PNG)
        self._manifest(self.g, {"item:x": {"file": "../secret.png"},
                                "item:y": {"file": str(secret)},
                                "item:z": {"file": "items/notes.txt"}})
        for name in ("x", "y", "z"):
            self.assertIsNone(self.store.resolve("item", name)["url"], name)
        self.assertIsNone(self.store.file_for("global", "../secret.png"))
        self.assertIsNone(self.store.file_for("nope", "items/a.png"))

    def test_broken_manifest_degrades_to_placeholders(self):
        (self.g / "manifest.json").write_text("{ nope", encoding="utf-8")
        with redirect_stderr(io.StringIO()) as err:
            self.assertIsNone(self.store.resolve("item", "Dolch")["url"])
        self.assertIn("manifest ignored", err.getvalue())

    def test_manifest_reloads_and_set_entry_keeps_fields(self):
        self._manifest(self.g, {"item:alt": {"file": "items/alt.png", "note": "keep me"}})
        self.assertIsNone(self.store.resolve("item", "Neu")["url"])
        (self.g / "items" / "neu.png").write_bytes(PNG)
        self.store._manifests["global"].set_entry("item:neu", {"file": "items/neu.png", "category": "ring"})
        self.assertIsNotNone(self.store.resolve("item", "Neu")["url"])
        raw = json.loads((self.g / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(raw["entries"]["item:alt"]["note"], "keep me")
        self.assertFalse([p for p in self.g.iterdir() if p.name.endswith(".tmp")])


def _import_app():
    spec = importlib.util.spec_from_file_location("_assets_app_under_test", str(DISPLAY / "dnd-display-app.py"))
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
        root = Path(self._tmp.name)
        (root / "items").mkdir()
        (root / "items" / "dolch.png").write_bytes(PNG)
        (root / "manifest.json").write_text(json.dumps({"entries": {
            "item:dolch": {"file": "items/dolch.png", "category": "weapon"}}}), encoding="utf-8")
        self._orig = self.app._assets
        self.app._assets = store_mod.AssetStore(global_root=str(root))
        # Cleanups run LIFO after tearDown: responses (registered later) close
        # first, so Windows can delete the files they were serving.
        self.addCleanup(self._tmp.cleanup)

    def tearDown(self):
        self.app._assets = self._orig

    def _get(self, url):
        resp = self.client.get(url)
        self.addCleanup(resp.close)
        return resp

    def test_resolve_batch(self):
        data = self._get("/assets/resolve?kind=item&name=Dolch&name=Seil%20(50%20ft)&name=").get_json()
        self.assertEqual([i["name"] for i in data["items"]], ["Dolch", "Seil (50 ft)"])
        self.assertIsNotNone(data["items"][0]["url"])
        self.assertIsNone(data["items"][1]["url"])
        self.assertEqual(self._get("/assets/resolve?kind=spell&name=x").status_code, 400)

    def test_file_and_placeholder_routes(self):
        url = self._get("/assets/resolve?kind=item&name=Dolch").get_json()["items"][0]["url"]
        resp = self._get(url)
        self.assertEqual((resp.status_code, resp.mimetype), (200, "image/png"))
        self.assertEqual(self._get("/assets/file/global/../manifest.json").status_code, 404)
        self.assertEqual(self._get("/assets/file/global/manifest.json").status_code, 404)
        for name, expect in (("weapon.svg", b"weapon"), ("bogus.svg", b"gear")):
            resp = self._get("/assets/placeholder/" + name)
            self.assertEqual(resp.mimetype, "image/svg+xml")
            self.assertIn(expect, resp.data)

    def test_changed_broadcasts(self):
        with mock.patch.object(self.app, "_broadcast") as bc:
            self.assertEqual(self.client.post("/assets/changed").status_code, 204)
        bc.assert_called_once_with({"assets_changed": True})
        with mock.patch.object(self.app, "_token_ok", lambda: False):
            self.assertEqual(self.client.post("/assets/changed").status_code, 403)

    def test_inventory_hook_in_index(self):
        html = (DISPLAY / "templates" / "index.html").read_text(encoding="utf-8")
        self.assertIn("AssetResolver.decorate(list, 'item')", html)
        self.assertLess(html.index('src="/static/js/assets.js"'), html.index("function connect()"))


NODE_HARNESS = r"""
const fs = require('fs'); const vm = require('vm');
const [modulesPath, assetsPath] = process.argv.slice(-2);
const requests = [];
const sandbox = {
  location: { search: '' }, URLSearchParams, console,
  fetch: url => { requests.push(url); const q = new URLSearchParams(url.split('?')[1]);
    return Promise.resolve({ ok: true, json: () => Promise.resolve({ items: q.getAll('name').map(n =>
      ({ name: n, key: 'item:' + n.toLowerCase(), url: n === 'Dolch' ? '/assets/file/global/d.png' : null,
         category: 'gear', placeholder: '/assets/placeholder/gear.svg' })) }) }); },
};
sandbox.window = sandbox;
vm.runInNewContext(fs.readFileSync(modulesPath, 'utf8'), sandbox);
vm.runInNewContext(fs.readFileSync(assetsPath, 'utf8'), sandbox);
const A = sandbox.window.AssetResolver;
(async () => {
  const first = await A.resolve('item', ['Dolch', 'Seil', 'Dolch']);
  const second = await A.resolve('item', ['Seil', 'Fackel']);
  sandbox.window.DisplayModules.dispatch({ assets_changed: true });
  await A.resolve('item', ['Dolch']);
  console.log(JSON.stringify({
    requests, modules: sandbox.window.DisplayModules.list(),
    images: [A.imageFor(first.get('Dolch')), A.imageFor(first.get('Seil')), A.imageFor(null)],
    secondKeys: Array.from(second.keys()),
  }));
})();
"""


@unittest.skipIf(shutil.which("node") is None, "node not installed")
class ResolverScriptTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        js = DISPLAY / "static" / "js"
        proc = subprocess.run(["node", "-e", NODE_HARNESS, str(js / "modules.js"), str(js / "assets.js")],
                              capture_output=True, text=True, encoding="utf-8", timeout=30)
        if proc.returncode != 0:
            raise AssertionError(f"node harness failed:\n{proc.stderr}")
        cls.out = json.loads(proc.stdout)

    def test_batches_and_caches(self):
        self.assertEqual(self.out["requests"], [
            "/assets/resolve?kind=item&name=Dolch&name=Seil",
            "/assets/resolve?kind=item&name=Fackel",
            "/assets/resolve?kind=item&name=Dolch",      # cache cleared by assets_changed
        ])
        self.assertEqual(self.out["secondKeys"], ["Seil", "Fackel"])
        self.assertEqual(self.out["modules"], ["assets"])

    def test_image_or_placeholder(self):
        self.assertEqual(self.out["images"], ["/assets/file/global/d.png",
                                              "/assets/placeholder/gear.svg",
                                              "/assets/placeholder/gear.svg"])


if __name__ == "__main__":
    unittest.main()
