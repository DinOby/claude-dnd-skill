"""Combat music: served by /audio/music/combat, played while initiative runs.

Run from repo root:
    python3 -m unittest tests.test_combat_music -v
"""
from __future__ import annotations

import json
import pathlib
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

REPO = pathlib.Path(__file__).resolve().parent.parent
SKILL = REPO / "skills" / "dnd" if (REPO / "skills" / "dnd").is_dir() else REPO
DISPLAY = SKILL / "display"
JS = DISPLAY / "static" / "js"
sys.path.insert(0, str(DISPLAY))

from tests.app_isolation import import_app  # noqa: E402


class MusicRouteTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.mod = import_app("dnd_display_app")
        cls.client = cls.mod.app.test_client()

    def test_synthesised_loop_when_no_own_file(self):
        if not self.mod._audio or not self.mod._audio.get_state()["available"]:
            self.skipTest("numpy not installed")
        with mock.patch.object(self.mod, "_own_music_file", lambda kind: None):
            r = self.client.get("/audio/music/combat")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.mimetype, "audio/wav")
        self.assertEqual(r.data[:4], b"RIFF")
        self.assertGreater(len(r.data), SKILL_MIN_WAV)

    def test_own_file_wins(self):
        with tempfile.TemporaryDirectory() as d:
            own = pathlib.Path(d) / "combat.mp3"
            own.write_bytes(b"ID3fake")
            with mock.patch.object(self.mod, "_own_music_file", lambda kind: str(own)):
                r = self.client.get("/audio/music/combat")
                data = r.data
                r.close()
        self.assertEqual(r.mimetype, "audio/mpeg")
        self.assertEqual(data, b"ID3fake")

    def test_unknown_kind(self):
        self.assertEqual(self.client.get("/audio/music/tavern").status_code, 404)


SKILL_MIN_WAV = 100_000   # several seconds of 16-bit audio


NODE_HARNESS = r"""
const fs = require('fs');
const vm = require('vm');
const files = process.argv.slice(-2);
const sandbox = { location: { search: '' }, URLSearchParams, console };
sandbox.window = sandbox;
for (const f of files) vm.runInNewContext(fs.readFileSync(f, 'utf8'), sandbox);
const M = sandbox.window.CombatMusic;
console.log(JSON.stringify([
  M._fightOn({ turn_order: { order: ['Grosch', 'Goblin'], current: 'Grosch' } }),
  M._fightOn({ turn_order: { current: 'Grosch' } }),
  M._fightOn({ turn_order: null }),
  M._fightOn({ turn_order: { order: [] } }),
  M._fightOn({}),
]));
"""


@unittest.skipUnless(shutil.which("node"), "node not installed")
class FightDetectionTests(unittest.TestCase):
    def test_music_follows_the_initiative_order(self):
        out = subprocess.run(["node", "-e", NODE_HARNESS, str(JS / "modules.js"), str(JS / "combat-music.js")],
                             capture_output=True, text=True, encoding="utf-8", timeout=60)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(json.loads(out.stdout), [True, False, False, False, False])


if __name__ == "__main__":
    unittest.main()
