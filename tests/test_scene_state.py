"""Tests for the scene state machine (scene_state.py), scripts/travel.py and the /scene route."""

from __future__ import annotations

import importlib.util
import io
import json
import os
import queue
import random
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

import grid_map as gm        # noqa: E402
from asset_queue import PendingQueue  # noqa: E402
from asset_store import AssetStore    # noqa: E402
from tests.app_isolation import IsolatedApp  # noqa: E402
import scene_state as ss     # noqa: E402
from config_loader import ConfigFile  # noqa: E402


def _load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, str(path))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


class TransitionTests(unittest.TestCase):
    def test_journey_with_event(self):
        s = ss.transition(ss.default_state(), "scene-set", location="Ashveil")
        s = ss.transition(s, "travel-start", to="Dornfeld", days=3, terrain="forest")
        self.assertEqual((s["mode"], s["location"], s["travel"]["from"], s["travel"]["day"]),
                         ("travel", None, "Ashveil", 0))
        s = ss.transition(s, "travel-day")
        s = ss.transition(s, "event-start", id="wolfsrudel", title="Wolfsrudel am Waldrand",
                          template="forest-clearing", tokens=[{"name": "Wolf", "kind": "enemy"}])
        self.assertEqual((s["mode"], s["map_id"], s["event"]["id"], s["event"]["map_is_temporary"]),
                         ("travel_event", "event-wolfsrudel-d1", "wolfsrudel-d1", True))
        s = ss.transition(s, "event-end")
        self.assertEqual((s["mode"], s["map_id"], s["event"]), ("travel", None, None))
        s = ss.transition(s, "travel-end")
        self.assertEqual((s["mode"], s["location"], s["travel"]), ("stationary", "Dornfeld", None))
        self.assertEqual(s["rev"], 6)

    def test_forbidden_transitions_explain_what_to_do(self):
        travelling = ss.transition(ss.default_state(), "travel-start", to="X", days=1)
        event = ss.transition(travelling, "event-start", title="Sturm")
        for state, action, hint in ((travelling, "scene-set", "arrive first"),
                                    (event, "travel-day", "event-end"),
                                    (event, "travel-end", "event-end"),
                                    (ss.default_state(), "event-start", "only happen on a journey"),
                                    (ss.default_state(), "travel-day", "travel.py start")):
            with self.assertRaises(ss.SceneError) as cm:
                ss.transition(state, action, title="x")
            self.assertIn(hint, str(cm.exception))
        for bad in ({"to": "", "days": 2}, {"to": "X", "days": 0}, {"to": "X", "days": True}):
            with self.assertRaises(ss.SceneError):
                ss.transition(ss.default_state(), "travel-start", **bad)
        with self.assertRaises(ss.SceneError):
            ss.transition(ss.default_state(), "teleport")
        with self.assertRaises(ss.SceneError):
            ss.transition(ss.default_state(), "travel-day", days=2)

    def test_broken_file_falls_back(self):
        self.assertEqual(ss.validate({"mode": "flying"})["mode"], "stationary")
        self.assertEqual(ss.validate({"mode": "travel"})["mode"], "stationary")         # no travel data
        self.assertEqual(ss.validate({"mode": "travel_event", "travel": {"day": 1}})["mode"], "travel")

    def test_event_chance_flag(self):
        self.assertEqual(ss.event_chance(""), 15)
        self.assertEqual(ss.event_chance("## Session Flags\n- travel_event_chance: 40\n"), 40)
        self.assertEqual(ss.event_chance("travel_event_chance: 250 %"), 100)

    def test_store_round_trip(self):
        path = Path(tempfile.mkdtemp()) / "c" / "scene-state.json"
        store = ss.SceneStore(str(path))
        self.assertEqual(store.get()["mode"], "stationary")
        store.apply("travel-start", to="Dornfeld", days=2)
        self.assertEqual(ss.SceneStore(str(path)).get()["travel"]["to"], "Dornfeld")
        self.assertEqual(store.update(map_id="x")["map_id"], "x")
        path.write_text("{", encoding="utf-8")
        self.assertEqual(store.get()["mode"], "stationary")


class TravelScriptTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.travel = _load(SKILL / "scripts" / "travel.py", "_travel_under_test")

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        root = Path(self._tmp.name)
        self.camp = root / "campaigns" / "ashveil"
        self.camp.mkdir(parents=True)
        (self.camp / "state.md").write_text("## Session Flags\n- travel_event_chance: 100\n", encoding="utf-8")
        env = mock.patch.dict(os.environ, {"DND_CAMPAIGN_ROOT": str(root), "DND_RUNTIME_DIR": str(root / ".rt")})
        env.start()
        self.addCleanup(env.stop)
        notify = mock.patch.object(self.travel, "notify_display")
        self.notify = notify.start()
        self.addCleanup(notify.stop)

    def run_cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            rc = self.travel.main(["-c", "ashveil", *argv])
        return rc, out.getvalue(), err.getvalue()

    def state(self):
        return json.loads((self.camp / "scene-state.json").read_text(encoding="utf-8"))

    def test_event_table_is_valid_and_uses_known_templates(self):
        cfg = ConfigFile("travel-events.json", validator=self.travel.validate_events,
                         default_dir=str(DISPLAY / "config"), user_dir=tempfile.mkdtemp())
        data = cfg.get()
        self.assertEqual(cfg.warnings, [])
        templates = {t["id"] for t in gm.MapTemplates().list()}
        for terrain, events in data["terrains"].items():
            for eid, ev in events.items():
                self.assertIn(ev["template"], templates, f"{terrain}.{eid}")

    def test_draw_expands_counts_and_falls_back_to_mythic(self):
        cfg = {"terrains": {"forest": {"wolf": {"title": "Wölfe", "template": "forest-clearing",
                                                "tokens": [{"name": "Wolf", "count": 3}]}}}}
        ev = self.travel.draw_event("forest", cfg, random.Random(1))
        self.assertEqual([t["name"] for t in ev["tokens"]], ["Wolf"] * 3)
        self.assertEqual(ev["tokens"][0]["kind"], "enemy")
        empty = self.travel.draw_event("desert", {"terrains": {}}, random.Random(1))
        self.assertIn("Mythic focus", empty["hint"])

    def test_journey_day_by_day(self):
        with mock.patch.object(self.travel, "advance_clock", return_value="+1 days → 2 Frostfall") as clock:
            rc, out, _ = self.run_cli("start", "--to", "Dornfeld", "--days", "2", "--terrain", "forest")
            self.assertEqual(rc, 0)
            rc, out, _ = self.run_cli("day", "--seed", "3")            # chance 100 % → always an event
            clock.assert_called_once_with("ashveil")
        self.assertIn("EVENT:", out)
        st = self.state()
        self.assertEqual((st["mode"], st["travel"]["day"]), ("travel_event", 1))
        self.assertTrue(st["map_id"].startswith("event-"))
        rc, _, err = self.run_cli("day", "--no-clock")
        self.assertEqual(rc, 1)
        self.assertIn("event-end", err)
        self.run_cli("event-end")
        rc, out, _ = self.run_cli("day", "--no-clock", "--no-check")
        self.assertIn("day 2/2", out)
        rc, out, _ = self.run_cli("day", "--no-clock")
        self.assertEqual(rc, 1)
        self.assertIn("arrive", out)
        rc, out, _ = self.run_cli("arrive")
        self.assertEqual(self.state()["location"], "Dornfeld")
        self.assertEqual(self.notify.call_count, 6)                # every change except the refused ones' status

    def test_quiet_day_and_forced_event(self):
        (self.camp / "state.md").write_text("travel_event_chance: 0\n", encoding="utf-8")
        self.run_cli("start", "--to", "Dornfeld", "--days", "3", "--terrain", "river")
        rc, out, _ = self.run_cli("day", "--no-clock", "--seed", "1")
        self.assertIn("passes quietly", out)
        rc, out, _ = self.run_cli("event", "--id", "brueckentroll")
        self.assertIn("Zoll an der Brücke", out)
        self.assertEqual(self.state()["event"]["template"], "bridge")
        self.run_cli("event-end")
        rc, out, _ = self.run_cli("event", "--title", "Ein Bote holt die Gruppe ein", "--template", "forest-road")
        self.assertEqual(self.state()["event"]["title"], "Ein Bote holt die Gruppe ein")
        rc, out, _ = self.run_cli("status")
        self.assertIn("Travel event chance: 0 %", out)

    def test_clock_note_when_calendar_missing(self):
        self.assertIn("clock not advanced", self.travel.advance_clock("ashveil"))


class SceneRouteTests(IsolatedApp):
    app_module_name = "_scene_app_under_test"

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        base = Path(self._tmp.name)
        self.lib, self.camp = base / "lib", base / "camp"
        self.scenes = ss.SceneStore(str(self.camp / "scene-state.json"))
        self.main = queue.Queue()
        for name, value in (("_maps", gm.MapStore(str(self.lib), str(self.camp))), ("_scenes", self.scenes),
                            ("_assets", AssetStore(global_root=str(base / "assets"))),
                            ("_asset_queue", PendingQueue(str(base / "assets" / "pending-assets.json"))),
                            ("_clients", [self.main]), ("_client_chars", {}),
                            ("_current_stats", {"players": [{"name": "Flerb"}, {"name": "Mira"}]})):
            patcher = mock.patch.object(self.app, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def post(self, url, body):
        resp = self.client.post(url, json=body)
        return resp.status_code, resp.get_json()

    def test_scene_set_travel_event_and_back(self):
        self.post("/map", {"map": {"template": "tavern-small", "id": "kessel"}})
        self.assertEqual(self.scenes.get()["map_id"], "kessel")            # the scene's map now
        code, data = self.post("/scene", {"action": "scene-set", "location": "Ashveil"})
        self.assertEqual((code, data["state"]["location"]), (200, "Ashveil"))
        self.assertIsNone(self.app._maps.current())                         # new place, old map gone
        self.post("/map", {"show": "kessel"})

        # travel.py changes the file directly, then asks for a sync
        self.scenes.apply("travel-start", to="Dornfeld", days=2, terrain="forest")
        code, data = self.post("/scene", {"sync": True})
        self.assertIsNone(self.app._maps.current())
        code, data = self.post("/map", {"show": "kessel"})
        self.assertEqual(code, 409)
        self.assertIn("travelling", data["errors"][0])

        self.scenes.apply("travel-day")
        self.scenes.apply("event-start", id="wolfsrudel", title="Wolfsrudel", template="forest-clearing",
                          tokens=[{"name": "Wolf", "kind": "enemy"}] * 2)
        self.post("/scene", {"sync": True})
        m = self.app._maps.current()
        self.assertEqual((m["id"], m["temporary"], m["template"]), ("event-wolfsrudel-d1", True, "forest-clearing"))
        self.assertEqual(sorted(t["name"] for t in m["tokens"]), ["Flerb", "Mira", "Wolf", "Wolf"])
        self.assertNotIn("event-wolfsrudel-d1", self.app._maps.library())
        self.assertEqual(self.post("/map", {"patch": {"move": [{"id": "wolf", "to": "E5"}]}})[0], 200)
        self.assertTrue((self.camp / "event-wolfsrudel-d1.json").is_file())

        self.scenes.apply("event-end")
        self.post("/scene", {"sync": True})
        self.assertIsNone(self.app._maps.current())
        self.assertFalse((self.camp / "event-wolfsrudel-d1.json").exists())  # discarded
        msgs = []
        while not self.main.empty():
            msgs.append(self.main.get_nowait())
        self.assertIn("scene_state", msgs[-1])

    def test_route_errors(self):
        code, data = self.post("/scene", {"action": "event-end"})
        self.assertEqual(code, 409)
        self.assertIn("no travel event", data["errors"][0])
        code, data = self.post("/scene", {"action": "scene-set", "locaton": "Ashveil"})
        self.assertEqual(code, 409)
        self.assertIn("unknown parameter(s) locaton", data["errors"][0])
        self.assertEqual(self.client.get("/scene").get_json()["mode"], "stationary")

    def test_startup_alignment_leaves_a_stationary_map_alone(self):
        self.post("/map", {"map": {"template": "camp", "id": "rast"}})
        self.scenes.update(map_id=None)
        self.app._reconcile_scene(broadcast=False, full=False)
        self.assertEqual(self.app._maps.current()["id"], "rast")


if __name__ == "__main__":
    unittest.main()
