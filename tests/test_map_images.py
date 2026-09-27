"""Tests for pictures on the battle grid: sprite cut-out, catalogue coverage, map image URLs and the wait-list."""

from __future__ import annotations

import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
SKILL = REPO / "skills" / "dnd" if (REPO / "skills" / "dnd").is_dir() else REPO
DISPLAY = SKILL / "display"
for p in (str(DISPLAY), str(SKILL / "scripts")):
    if p not in sys.path:
        sys.path.insert(0, p)

import asset_pipeline as ap           # noqa: E402
import asset_seed as aseed            # noqa: E402
import grid_map as gm                 # noqa: E402
import image_providers as ip          # noqa: E402
import scene_state                    # noqa: E402
from asset_queue import PendingQueue  # noqa: E402
from asset_store import AssetStore    # noqa: E402
from config_loader import ConfigFile  # noqa: E402
from tests.app_isolation import IsolatedApp  # noqa: E402

try:
    from PIL import Image
except ImportError:   # pragma: no cover
    Image = None

PNG = b"\x89PNG\r\n\x1a\n img"


def magenta_sprite() -> bytes:
    """64×64 magenta canvas with a red, a green and a blue block (colours that must survive)."""
    img = Image.new("RGB", (64, 64), (255, 0, 255))
    for x0, colour in ((8, (200, 30, 30)), (26, (40, 160, 40)), (44, (30, 60, 200))):
        for x in range(x0, x0 + 12):
            for y in range(20, 44):
                img.putpixel((x, y), colour)
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


@unittest.skipIf(Image is None, "Pillow not installed")
class ChromaKeyTests(unittest.TestCase):
    def test_background_goes_objects_stay(self):
        data, mime = ap.chroma_key(magenta_sprite())
        self.assertEqual(mime, "image/png")
        with Image.open(io.BytesIO(data)) as img:
            self.assertEqual(img.mode, "RGBA")
            self.assertEqual(img.size, (48, 24))               # cropped to the three blocks
            px = img.load()
            self.assertEqual(px[4, 12][3], 255)                # red block
            self.assertEqual(px[24, 12][3], 255)               # green block
            self.assertEqual(px[40, 12][3], 255)               # blue block
            self.assertEqual(px[15, 12][3], 0)                 # magenta gap between blocks

    def test_pipeline_cuts_out_sprites_only(self):
        class Magenta:
            name = "magenta"
            def available(self): return True, "ok"
            def generate(self, req): return ip.ImageResult(data=magenta_sprite(), mime="image/png", meta={})
        tmp = Path(tempfile.mkdtemp())
        user_cfg = tmp / "config"
        user_cfg.mkdir()
        (user_cfg / "image-provider.json").write_text(json.dumps({"active": {"default": "dummy"}}), encoding="utf-8")
        store = AssetStore(global_root=str(tmp / "assets"))
        queue = PendingQueue(str(tmp / "assets" / "pending-assets.json"))
        cfg = ConfigFile("image-provider.json", validator=ap.validate_provider_config,
                         default_dir=str(DISPLAY / "config"), user_dir=str(user_cfg))
        p = ap.Pipeline(store, queue, config=cfg, loader=lambda n, c: Magenta())
        queue.add_many([{"key": "sprite:table", "kind": "sprite", "name": "table", "category": "sprite"},
                        {"key": "item:dolch", "kind": "item", "name": "Dolch", "category": "weapon"}])
        summary = p.generate(provider="magenta", log=lambda *_: None)
        self.assertEqual(sorted(summary["done"]), ["item:dolch", "sprite:table"])
        with Image.open(tmp / "assets" / "sprites" / "table.png") as img:
            self.assertEqual(img.mode, "RGBA")
        with Image.open(tmp / "assets" / "items" / "dolch.png") as img:
            self.assertEqual(img.mode, "RGB")                  # inventory icons keep their background
        # the wait-listed sprite used the seed catalogue's description
        self.assertTrue(store.global_manifest.entry("sprite:table")["prompt"].startswith("A rectangular wooden tavern table"))

    def test_without_pillow_sprites_are_blocked(self):
        tmp = Path(tempfile.mkdtemp())
        store = AssetStore(global_root=str(tmp / "assets"))
        queue = PendingQueue(str(tmp / "assets" / "pending-assets.json"))
        queue.add_many([{"key": "sprite:tree", "kind": "sprite", "name": "tree", "category": "sprite"}])
        p = ap.Pipeline(store, queue, loader=lambda n, c: None)
        log = []
        with mock.patch.object(ap, "pillow_available", return_value=False):
            summary = p.generate(log=log.append)
        self.assertEqual(summary["blocked"], ["sprite:tree"])
        self.assertIn("pip install pillow", " ".join(log))
        self.assertEqual(queue.entries()[0]["attempts"], 0)


class CatalogueCoverageTests(unittest.TestCase):
    def test_every_template_terrain_type_has_a_sprite(self):
        cfg = ConfigFile("asset-seed.json", validator=aseed.validate_seed_config,
                         default_dir=str(DISPLAY / "config"), user_dir=tempfile.mkdtemp())
        sprites = {e["key"] for e in aseed.SeedCatalog(cfg).entries(["sprites"])}
        self.assertEqual(cfg.warnings, [])
        for t in gm.MapTemplates().list():
            for terrain in gm.MapTemplates().get(t["id"])["terrain"]:
                self.assertIn(f"sprite:{terrain['type']}", sprites, f"{t['id']}: {terrain['type']}")

    def test_tavern_has_chairs_and_room_to_spawn(self):
        m = gm.MapTemplates().get("tavern-small")
        self.assertEqual(sum(1 for t in m["terrain"] if t["type"] == "chair"), 12)

    def test_provider_styles(self):
        cfg = json.loads((DISPLAY / "config" / "image-provider.json").read_text(encoding="utf-8"))
        self.assertIn("#FF00FF", cfg["style"]["sprite"])
        self.assertIn("tileable", cfg["style"]["map"])
        self.assertEqual(cfg["sizes"]["sprite"], [256, 256])


class MapImageRouteTests(IsolatedApp):
    app_module_name = "_map_images_app_under_test"

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        base = Path(self._tmp.name)
        self.assets = base / "assets"
        for sub, name in (("tokens", "goblin"), ("tokens", "wirtin"), ("sprites", "table"), ("maps", "taverne")):
            (self.assets / sub).mkdir(parents=True, exist_ok=True)
            (self.assets / sub / f"{name}.png").write_bytes(PNG)
        self.store = AssetStore(global_root=str(self.assets))
        for key, rel in (("token:goblin", "tokens/goblin.png"), ("token:wirtin", "tokens/wirtin.png"),
                         ("sprite:table", "sprites/table.png"), ("map:taverne", "maps/taverne.png")):
            self.store.global_manifest.set_entry(key, {"file": rel, "category": key.split(":")[0]})
        self.queue = PendingQueue(str(self.assets / "pending-assets.json"))
        self.main = __import__("queue").Queue()
        for name, value in (("_maps", gm.MapStore(str(base / "lib"), str(base / "camp"))),
                            ("_scenes", scene_state.SceneStore(str(base / "camp" / "scene-state.json"))),
                            ("_assets", self.store), ("_asset_queue", self.queue),
                            ("_clients", [self.main]), ("_client_chars", {}),
                            ("_current_stats", {"players": [{"name": "Flerb", "race": "Tiefling", "class": "Warlock"}]})):
            patcher = mock.patch.object(self.app, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def messages(self, key):
        out = []
        while not self.main.empty():
            msg = self.main.get_nowait()
            if key in msg:
                out.append(msg)
        return out

    def test_full_map_carries_images_and_queues_the_rest(self):
        self.client.post("/map", json={"map": {"template": "tavern-small", "id": "kessel", "tokens": [
            {"name": "Flerb", "kind": "pc"}, {"name": "Goblin", "kind": "enemy"}, {"name": "Goblin", "kind": "enemy"},
            {"name": "Wirtin Hilde", "archetype": "Wirtin"}, {"name": "Kiste", "kind": "object"}]}})
        msg = self.messages("map")[-1]
        imgs = msg["map_images"]
        self.assertTrue(imgs["floor"].startswith("/assets/file/global/maps/taverne.png"))
        self.assertTrue(imgs["terrain"]["table"].startswith("/assets/file/global/sprites/table.png"))
        self.assertIsNone(imgs["terrain"]["chair"])
        self.assertTrue(imgs["tokens"]["goblin-2"].startswith("/assets/file/global/tokens/goblin.png"))
        self.assertTrue(imgs["tokens"]["wirtin-hilde"].startswith("/assets/file/global/tokens/wirtin.png"))
        self.assertIsNone(imgs["tokens"]["flerb"])
        queued = {e["key"]: e for e in self.queue.entries()}
        self.assertEqual(queued["token:flerb"]["hint"], "Tiefling Warlock")
        self.assertEqual(queued["token:flerb"]["category"], "pc")
        self.assertIn("sprite:chair", queued)
        self.assertNotIn("token:goblin", queued)
        self.assertNotIn("token:kiste", queued)          # objects never get portraits
        self.assertEqual(self.client.get("/map").get_json()["images"]["tokens"]["goblin"], imgs["tokens"]["goblin"])

    def test_patch_sends_images_of_added_tokens(self):
        self.client.post("/map", json={"map": {"template": "tavern-small", "id": "kessel"}})
        self.messages("map")
        self.client.post("/map", json={"patch": {"add": [{"name": "Goblin 3", "kind": "enemy"}]}})
        msg = self.messages("map_patch")[-1]
        self.assertEqual(list(msg["map_images"]["tokens"]), ["goblin-3"])
        self.assertTrue(msg["map_images"]["tokens"]["goblin-3"].startswith("/assets/file/global/tokens/goblin.png"))

    def test_event_map_floor_is_not_queued(self):
        scenes = self.app._scenes
        scenes.apply("travel-start", to="Dornfeld", days=2)
        scenes.apply("event-start", title="Sturm", template="camp")
        self.client.post("/scene", json={"sync": True})
        keys = {e["key"] for e in self.queue.entries()}
        self.assertNotIn("map:lager", keys)
        self.assertIn("sprite:tent", keys)


if __name__ == "__main__":
    unittest.main()
