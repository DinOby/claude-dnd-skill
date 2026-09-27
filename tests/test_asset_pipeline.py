"""Tests for the after-play image pipeline: providers, asset_pipeline.py, scripts/assets.py."""

from __future__ import annotations

import importlib.util
import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock


REPO = Path(__file__).resolve().parent.parent
SKILL = REPO / "skills" / "dnd" if (REPO / "skills" / "dnd").is_dir() else REPO
DISPLAY = SKILL / "display"
for p in (str(DISPLAY), str(SKILL / "scripts")):
    if p not in sys.path:
        sys.path.insert(0, p)

import asset_pipeline as ap          # noqa: E402
import asset_seed as aseed           # noqa: E402
import image_providers as ip         # noqa: E402
from asset_queue import PendingQueue  # noqa: E402
from asset_store import AssetStore    # noqa: E402
from config_loader import ConfigFile  # noqa: E402


class _Flaky:
    """Provider double: fails for prompts containing 'boom', else returns a PNG."""
    name = "flaky"

    def __init__(self, ready=True):
        self.ready, self.calls = ready, []

    def available(self):
        return self.ready, "ok" if self.ready else "KEY not set"

    def generate(self, req):
        self.calls.append(req)
        if "boom" in req.prompt.lower():
            raise ip.ProviderError("rate limited", retryable=True)
        return ip.ImageResult(data=b"\x89PNG fake", mime="image/png", meta={"model": "m1"})


class _Case(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        base = Path(self._tmp.name)
        self.assets = base / "assets"
        self.user_cfg = base / "config"
        self.user_cfg.mkdir()
        # The bundled default is gemini (needs a key); tests run on the offline
        # dummy through a user override — which also exercises the override path.
        (self.user_cfg / "image-provider.json").write_text(
            json.dumps({"active": {"default": "dummy"}}), encoding="utf-8")
        self.store = AssetStore(global_root=str(self.assets))
        self.queue = PendingQueue(str(self.assets / "pending-assets.json"))
        self.cfg = ConfigFile("image-provider.json", validator=ap.validate_provider_config,
                              default_dir=str(DISPLAY / "config"), user_dir=str(self.user_cfg))
        self.flaky = _Flaky()
        loader = lambda name, cfg: self.flaky if name == "flaky" else ip.load_provider(name, cfg, user_dir=str(base / "providers"))
        self.p = ap.Pipeline(self.store, self.queue, config=self.cfg, loader=loader)
        self.log = []

    def queue_items(self, *names, category="weapon"):
        self.queue.add_many([{"key": ap.parse_key(n), "kind": "item", "name": n, "category": category}
                             for n in names])


class ConfigAndPromptTests(_Case):
    def test_bundled_config_is_valid(self):
        self.cfg.get()
        self.assertEqual(self.cfg.warnings, [])

    def test_bad_override_is_ignored(self):
        (self.user_cfg / "image-provider.json").write_text(
            json.dumps({"active": {"default": "nowhere"}}), encoding="utf-8")
        with redirect_stderr(io.StringIO()):
            self.assertEqual(self.p.provider_name("item"), "gemini")   # bundled default

    def test_parse_key(self):
        self.assertEqual(ap.parse_key("item:Flammenschwert der Asche"), "item:flammenschwert-der-asche")
        self.assertEqual(ap.parse_key("Heiltrank (2)"), "item:heiltrank")
        self.assertEqual(ap.parse_key("token:Wirtin Hilde"), "token:wirtin-hilde")

    def test_build_prompt(self):
        cfg = {"style": {"base": "painted art", "item": "centred object."}}
        self.assertEqual(ap.build_prompt({"key": "item:x", "kind": "item", "name": "Dolch",
                                          "category": "weapon", "hint": "rostig"}, cfg),
                         "Dolch (weapon). rostig. Centred object. Painted art.")
        own = ap.build_prompt({"key": "item:x", "kind": "item", "name": "Dolch", "prompt": "a rusty dagger"}, cfg)
        self.assertEqual(own, "A rusty dagger. Centred object. Painted art.")


class GenerateTests(_Case):
    def test_dummy_end_to_end(self):
        self.queue_items("Flammenschwert der Asche", "Siegelring")
        summary = self.p.generate(log=self.log.append)
        self.assertEqual(summary["done"], ["item:flammenschwert-der-asche", "item:siegelring"])
        png = (self.assets / "items" / "flammenschwert-der-asche.png").read_bytes()
        self.assertTrue(png.startswith(b"\x89PNG\r\n\x1a\n"))
        info = self.store.resolve("item", "Flammenschwert der Asche")
        self.assertIsNotNone(info["url"])
        entry = self.store.global_manifest.entry("item:flammenschwert-der-asche")
        self.assertEqual((entry["source"], entry["category"]), ("dummy", "weapon"))
        self.assertEqual({e["status"] for e in self.queue.entries()}, {"done"})

    def test_dry_run_changes_nothing(self):
        self.queue_items("Dolch")
        summary = self.p.generate(dry_run=True)
        self.assertEqual(summary["planned"][0]["key"], "item:dolch")
        self.assertIn("Dolch", summary["planned"][0]["prompt"])
        self.assertFalse((self.assets / "items").exists())
        self.assertEqual(self.queue.entries()[0]["status"], "pending")

    def test_limit_keys_and_skip(self):
        self.queue_items("A-Schwert", "B-Schwert", "C-Schwert")
        self.queue.update("item:b-schwert", status="skipped")
        self.assertEqual(self.p.generate(limit=1, log=self.log.append)["done"], ["item:a-schwert"])
        self.assertEqual(self.p.generate(keys=["item:c-schwert"], log=self.log.append)["done"], ["item:c-schwert"])
        self.assertEqual(self.queue.entries("skipped")[0]["key"], "item:b-schwert")

    def test_failures_retry_then_give_up(self):
        self.queue_items("Boom-Klinge", "Gute Klinge")
        for _ in range(3):
            summary = self.p.generate(provider="flaky", log=self.log.append)
        self.assertEqual(summary["failed"], ["item:boom-klinge"])
        e = {x["key"]: x for x in self.queue.entries()}
        self.assertEqual((e["item:boom-klinge"]["status"], e["item:boom-klinge"]["attempts"]), ("failed", 3))
        self.assertIn("rate limited", e["item:boom-klinge"]["last_error"])
        self.assertEqual(e["item:gute-klinge"]["status"], "done")

    def test_unavailable_provider_blocks_without_burning_attempts(self):
        self.flaky.ready = False
        self.queue_items("A-Schwert", "B-Schwert")
        summary = self.p.generate(provider="flaky", log=self.log.append)
        self.assertEqual(summary["blocked"], ["item:a-schwert", "item:b-schwert"])
        self.assertEqual(self.flaky.calls, [])
        self.assertEqual({e["attempts"] for e in self.queue.entries()}, {0})

    def test_existing_image_marks_done_without_generating(self):
        self.queue_items("Dolch")
        src = Path(self._tmp.name) / "mein-dolch.png"
        src.write_bytes(b"\x89PNG own")
        self.p.add_file("item:dolch", str(src))
        self.assertEqual(self.queue.entries()[0]["status"], "done")
        self.queue.update("item:dolch", status="pending")
        self.assertEqual(self.p.generate(provider="flaky", log=self.log.append)["done"], [])
        self.assertEqual(self.flaky.calls, [])

    def test_bad_mime_is_a_failure(self):
        self.flaky.generate = lambda req: ip.ImageResult(data=b"GIF89a", mime="image/gif")
        self.queue_items("Dolch")
        self.assertEqual(self.p.generate(provider="flaky", log=self.log.append)["failed"], ["item:dolch"])


class AddFileTests(_Case):
    def test_add_validates(self):
        txt = Path(self._tmp.name) / "notes.txt"
        txt.write_text("x", encoding="utf-8")
        with self.assertRaises(ValueError):
            self.p.add_file("item:dolch", str(txt))
        with self.assertRaises(FileNotFoundError):
            self.p.add_file("item:dolch", str(Path(self._tmp.name) / "missing.png"))
        png = Path(self._tmp.name) / "d.png"
        png.write_bytes(b"\x89PNG")
        with self.assertRaises(ValueError):
            self.p.add_file("spell:feuerball", str(png))
        with self.assertRaises(ValueError):
            self.p.add_file("item:dolch", str(png), category="banana")
        self.assertEqual(self.p.add_file("item:dolch", str(png), category="weapon"), "items/dolch.png")


class ProviderLoaderTests(unittest.TestCase):
    def test_bundled_dummy(self):
        prov = ip.load_provider("dummy", {"providers": {"dummy": {"module": "dummy", "size": 16}}},
                                user_dir=tempfile.mkdtemp())
        res = prov.generate(ip.ImageRequest(prompt="x", kind="item"))
        self.assertEqual(res.mime, "image/png")
        self.assertEqual(res.data, prov.generate(ip.ImageRequest(prompt="x", kind="item")).data)

    def test_user_provider_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "mine.py").write_text(
                "from image_providers import ImageResult\n"
                "class P:\n    name='mine'\n    def __init__(s, c): s.c = c\n"
                "    def available(s): return True, s.c['url']\n"
                "    def generate(s, r): return ImageResult(b'x', 'image/png')\n"
                "def create(config): return P(config)\n", encoding="utf-8")
            prov = ip.load_provider("mine", {"providers": {"mine": {"module": "mine", "url": "http://x"}}},
                                    user_dir=tmp)
            self.assertEqual(prov.available(), (True, "http://x"))

    def test_rejects_unknown_and_path_like_modules(self):
        for cfg in ({"providers": {}},
                    {"providers": {"x": {"module": "../evil"}}},
                    {"providers": {"x": {"module": "os.path"}}},
                    {"providers": {"x": {"module": "nope_not_here"}}}):
            with self.assertRaises(ip.ProviderError):
                ip.load_provider("x", cfg, user_dir=tempfile.mkdtemp())


class SeedTests(_Case):
    CATALOGUE = {
        "version": 1,
        "items": {
            "langschwert": {"name": "Langschwert", "category": "weapon", "srd": "Longsword",
                            "aliases": ["Longsword"], "prompt": "a plain steel longsword"},
            "heiltrank": {"name": "Heiltrank", "category": "potion", "prompt": "a red potion"},
            "fackel": {"name": "Fackel", "category": "gear", "prompt": "a burning torch"},
        },
        "portraits": {"goblin": {"name": "Goblin", "category": "enemy", "prompt": "a small goblin"}},
        "maps": {"taverne": {"name": "Taverne", "category": "map", "template": "tavern-small",
                             "prompt": "a tavern interior"}},
    }

    def setUp(self):
        super().setUp()
        seed_dir = Path(self._tmp.name) / "seed-default"
        seed_dir.mkdir()
        (seed_dir / "asset-seed.json").write_text(json.dumps(self.CATALOGUE), encoding="utf-8")
        self.catalog = aseed.SeedCatalog(ConfigFile("asset-seed.json", validator=aseed.validate_seed_config,
                                                    default_dir=str(seed_dir), user_dir=str(self.user_cfg)))

    def test_bundled_catalogue_is_valid_and_matches_the_srd(self):
        cfg = ConfigFile("asset-seed.json", validator=aseed.validate_seed_config,
                         default_dir=str(DISPLAY / "config"), user_dir=str(self.user_cfg))
        entries = aseed.SeedCatalog(cfg).entries()
        self.assertEqual(cfg.warnings, [])
        self.assertEqual({e["set"] for e in entries}, set(aseed.SETS))
        srd = json.loads((SKILL / "data" / "dnd5e_srd.json").read_text(encoding="utf-8"))
        known = {"items": {r["name"] for s in ("equipment", "magic_items") for r in srd[s]},
                 "portraits": {r["name"] for r in srd["monsters"]}}
        for e in entries:
            if e["srd"]:
                self.assertIn(e["srd"], known[e["set"]], e["key"])
            if e["set"] == "maps":
                self.assertTrue(e["template"], e["key"])

    def test_validator(self):
        bad = json.loads(json.dumps(self.CATALOGUE))
        bad["items"]["schwert"] = {"name": "Langes Schwert", "prompt": "x"}                  # slug ≠ name
        bad["items"]["dolch"] = {"name": "Dolch", "category": "npc", "prompt": "x"}          # wrong category
        bad["items"]["axt"] = {"name": "Axt", "aliases": ["Longsword"], "prompt": "x"}       # alias taken
        bad["maps"]["hoehle"] = {"name": "Höhle"}                                             # no prompt
        problems = " | ".join(aseed.validate_seed_config(bad))
        for needle in ("slug should be 'langes-schwert'", "unknown category 'npc'",
                       "alias 'Longsword' is already used", "maps.hoehle.prompt is required"):
            self.assertIn(needle, problems)
        with self.assertRaises(ValueError):
            self.catalog.entries(["monsters"])

    def test_seed_end_to_end(self):
        summary = self.p.seed(self.catalog, log=self.log.append)
        self.assertEqual(summary["done"], ["item:langschwert", "item:heiltrank", "item:fackel",
                                           "token:goblin", "map:taverne"])
        entry = self.store.global_manifest.entry("item:langschwert")
        self.assertTrue(entry["generic"])
        self.assertEqual((entry["seed"], entry["srd"], entry["category"]), ("items", "Longsword", "weapon"))
        self.assertTrue(entry["prompt"].startswith("A plain steel longsword. "))
        self.assertEqual(self.store.global_manifest.entry("map:taverne")["template"], "tavern-small")
        self.assertTrue((self.assets / "maps" / "taverne.png").is_file())
        # the English SRD name resolves to the same generic image
        self.assertEqual(self.store.resolve("item", "Longsword")["key"], "item:langschwert")
        self.assertIsNotNone(self.store.resolve("item", "Langschwert")["url"])
        self.assertEqual(self.queue.entries(), [])             # seeding never touches the wait-list
        again = self.p.seed(self.catalog, log=self.log.append)
        self.assertEqual((again["done"], len(again["existing"])), ([], 5))

    def test_categories_limit_and_dry_run(self):
        planned = self.p.seed(self.catalog, sets=["items"], dry_run=True)["planned"]
        self.assertEqual([x["key"] for x in planned], ["item:langschwert", "item:heiltrank", "item:fackel"])
        self.assertFalse((self.assets / "items").exists())
        self.p.add_file("item:heiltrank", str(self._png("own.png")))
        summary = self.p.seed(self.catalog, sets=["items", "maps"], limit=2, log=self.log.append)
        self.assertEqual(summary["done"], ["item:langschwert", "item:fackel"])   # existing one does not count
        self.assertEqual(summary["existing"], ["item:heiltrank"])
        self.assertEqual(self.store.global_manifest.entry("item:heiltrank")["source"], "manual")

    def test_pending_wait_list_entry_is_left_to_generate(self):
        self.queue_items("Langschwert")
        summary = self.p.seed(self.catalog, sets=["items"], log=self.log.append)
        self.assertEqual(summary["queued"], ["item:langschwert"])
        self.assertIsNone(self.store.global_manifest.entry("item:langschwert"))

    def test_specific_image_replaces_the_generic_one(self):
        self.p.seed(self.catalog, sets=["items"], log=self.log.append)
        self.queue_items("Langschwert")
        self.queue.update("item:langschwert", prompt="a longsword whose blade glows like embers")
        summary = self.p.generate(log=self.log.append)
        self.assertEqual(summary["done"], ["item:langschwert"])
        entry = self.store.global_manifest.entry("item:langschwert")
        self.assertNotIn("generic", entry)
        self.assertIn("embers", entry["prompt"])
        # a later seed run keeps the specific image
        self.assertEqual(self.p.seed(self.catalog, sets=["items"], log=self.log.append)["existing"],
                         ["item:langschwert", "item:heiltrank", "item:fackel"])

    def test_failures_are_not_recorded(self):
        self.flaky.generate = mock.Mock(side_effect=ip.ProviderError("rate limited"))
        summary = self.p.seed(self.catalog, sets=["portraits"], provider="flaky", log=self.log.append)
        self.assertEqual(summary["failed"], ["token:goblin"])
        self.assertIsNone(self.store.global_manifest.entry("token:goblin"))
        self.assertEqual(self.queue.entries(), [])

    def _png(self, name):
        path = Path(self._tmp.name) / name
        path.write_bytes(b"\x89PNG\r\n\x1a\n own")
        return path


class CliTests(_Case):
    def run_cli(self, *argv):
        spec = importlib.util.spec_from_file_location("assets_cli_under_test", SKILL / "scripts" / "assets.py")
        cli = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cli)
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(cli, "_pipeline", lambda: self.p), \
             mock.patch.object(cli, "_notify_display") as notify, \
             redirect_stdout(out), redirect_stderr(err):
            rc = cli.main(list(argv))
        return rc, out.getvalue(), err.getvalue(), notify

    def test_status_prompt_generate_skip_retry(self):
        self.queue_items("Flammenschwert der Asche", "Diebeswerkzeug")
        rc, out, _, _ = self.run_cli("status")
        self.assertIn("2 pending", out)
        rc, out, _, _ = self.run_cli("prompt", "item:Flammenschwert der Asche", "a blade of embers")
        self.assertEqual(rc, 0)
        rc, _, _, _ = self.run_cli("skip", "Diebeswerkzeug")
        rc, out, _, notify = self.run_cli("generate")
        self.assertIn("Done: 1", out)
        notify.assert_called_once()
        self.assertTrue(self.store.global_manifest.entry("item:flammenschwert-der-asche")["prompt"]
                        .startswith("A blade of embers. "))
        rc, _, _, _ = self.run_cli("retry", "Diebeswerkzeug")
        self.assertEqual({e["key"]: e["status"] for e in self.queue.entries()}["item:diebeswerkzeug"], "pending")
        rc, _, err, _ = self.run_cli("prompt", "item:unbekannt", "x")
        self.assertEqual(rc, 1)
        self.assertIn("not on the wait-list", err)

    def test_seed_and_prompt_add(self):
        catalog = aseed.SeedCatalog(ConfigFile("asset-seed.json", validator=aseed.validate_seed_config,
                                               default_dir=str(DISPLAY / "config"), user_dir=str(self.user_cfg)))
        with mock.patch.object(aseed, "SeedCatalog", lambda: catalog):
            rc, out, _, notify = self.run_cli("seed", "--category", "maps", "--dry-run")
            self.assertEqual(rc, 0)
            self.assertIn("map:taverne via dummy", out)
            notify.assert_not_called()
            rc, out, _, notify = self.run_cli("seed", "--category", "items", "--limit", "2")
            self.assertIn("Done: 2, failed: 0", out)
            notify.assert_called_once()
        self.assertTrue(self.store.global_manifest.entry("item:dolch")["generic"])
        rc, _, err, _ = self.run_cli("prompt", "Dolch", "a dagger with a bone handle")
        self.assertEqual(rc, 1)
        self.assertIn("--add", err)
        rc, out, _, _ = self.run_cli("prompt", "Dolch", "a dagger with a bone handle", "--add")
        self.assertEqual(rc, 0)
        entry = self.queue.entries()[0]
        self.assertEqual((entry["key"], entry["name"], entry["category"], entry["status"]),
                         ("item:dolch", "Dolch", "weapon", "pending"))
        rc, out, _, _ = self.run_cli("generate")
        self.assertIn("Done: 1", out)
        self.assertNotIn("generic", self.store.global_manifest.entry("item:dolch"))

    def test_blocked_generate_exits_nonzero(self):
        self.flaky.ready = False
        self.queue_items("Dolch")
        rc, out, _, notify = self.run_cli("generate", "--provider", "flaky")
        self.assertEqual(rc, 1)
        self.assertIn("blocked (provider not ready): 1", out)
        notify.assert_not_called()


if __name__ == "__main__":
    unittest.main()
