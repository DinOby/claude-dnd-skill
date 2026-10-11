"""'Playing as': a remote player's main display counts as that character's device.

Server side: a stream opened with ?as=<name> is registered for dice routing
(the roll goes there, not to the host's shared screen) yet stays a main
display that receives the battle map. Browser side: play-as.js decides which
dice requests open its roll dialog.

Run from repo root:
    python3 -m unittest tests.test_play_as -v
"""
from __future__ import annotations

import json
import os
import pathlib
import queue
import shutil
import subprocess
import sys
import unittest

REPO = pathlib.Path(__file__).resolve().parent.parent
SKILL = REPO / "skills" / "dnd" if (REPO / "skills" / "dnd").is_dir() else REPO
DISPLAY = SKILL / "display"
JS = DISPLAY / "static" / "js"
sys.path.insert(0, str(DISPLAY))

from tests.app_isolation import import_app  # noqa: E402


class PlayAsRoutingTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.mod = import_app("dnd_display_app")

    def setUp(self):
        self.main, self.phone, self.remote = queue.Queue(), queue.Queue(), queue.Queue()
        with self.mod._clients_lock:
            self._saved = (list(self.mod._clients), dict(self.mod._client_chars), set(self.mod._client_as))
            self.mod._clients[:] = [self.main, self.phone, self.remote]
            self.mod._client_chars.clear()
            self.mod._client_chars.update({self.phone: "mordos", self.remote: "grosch"})
            self.mod._client_as.clear()
            self.mod._client_as.add(self.remote)

    def tearDown(self):
        with self.mod._clients_lock:
            self.mod._clients[:] = self._saved[0]
            self.mod._client_chars.clear()
            self.mod._client_chars.update(self._saved[1])
            self.mod._client_as.clear()
            self.mod._client_as.update(self._saved[2])

    def test_remote_player_counts_as_present(self):
        self.assertTrue(self.mod._phone_present("Grosch"))
        self.assertFalse(self.mod._phone_present("Zarith"))

    def test_remote_player_still_gets_the_map(self):
        self.mod._broadcast_main({"map": None})
        self.assertFalse(self.main.empty())
        self.assertFalse(self.remote.empty())
        self.assertTrue(self.phone.empty())          # phones never get the map

    def test_whisper_reaches_only_the_addressee(self):
        self.assertEqual(self.mod._send_to_character("Grosch", {"private": {"text": "psst"}}), 1)
        self.assertEqual(self.remote.get_nowait(), {"private": {"text": "psst"}})
        self.assertTrue(self.main.empty())
        self.assertTrue(self.phone.empty())
        self.assertEqual(self.mod._send_to_character("Zarith", {"private": {"text": "x"}}), 0)


class SecretRouteTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.mod = import_app("dnd_display_app")
        cls.mod._token_ok = lambda: True
        cls.mod._rate_ok = lambda addr: True
        cls.mod._device_ok = lambda device, addr: "approved"
        cls.client = cls.mod.app.test_client()

    def setUp(self):
        self.mod._current_stats = {"players": [{"name": "Zarith"}, {"name": "Grosch"}]}
        if os.path.exists(self.mod.QUEUE_FILE):
            os.remove(self.mod.QUEUE_FILE)
        self.everyone = queue.Queue()
        with self.mod._clients_lock:
            self._saved = list(self.mod._clients)
            self.mod._clients[:] = [self.everyone]

    def tearDown(self):
        with self.mod._clients_lock:
            self.mod._clients[:] = self._saved
        if os.path.exists(self.mod.QUEUE_FILE):
            os.remove(self.mod.QUEUE_FILE)

    def test_secret_action_goes_to_the_dm_only(self):
        r = self.client.post("/player-input/secret",
                             data=json.dumps({"character": "Zarith", "text": "Ich tausche den Becher."}),
                             content_type="application/json")
        self.assertEqual(r.status_code, 204, r.data)
        with open(self.mod.QUEUE_FILE, encoding="utf-8") as f:
            q = f.read()
        self.assertIn("[Zarith] (SECRET", q)
        self.assertIn("Ich tausche den Becher.", q)
        self.assertTrue(self.everyone.empty())       # nothing on the shared feed

    def test_secret_for_everybody_is_refused(self):
        r = self.client.post("/player-input/secret", data=json.dumps({"character": "Everybody", "text": "x"}),
                             content_type="application/json")
        self.assertEqual(r.status_code, 403)

    def test_dm_whisper_reports_delivery(self):
        r = self.client.post("/private", data=json.dumps({"to": "Grosch", "text": "Du hörst Schritte."}),
                             content_type="application/json")
        self.assertEqual(r.get_json(), {"delivered": 0})
        self.assertTrue(self.everyone.empty())


NODE_HARNESS = r"""
const fs = require('fs');
const vm = require('vm');
const files = process.argv.slice(-2);
const sandbox = { location: { search: '' }, URLSearchParams, console };
sandbox.window = sandbox;
for (const f of files) vm.runInNewContext(fs.readFileSync(f, 'utf8'), sandbox);
const P = sandbox.window.PlayAs;
console.log(JSON.stringify({
  single: P._targetsMe({ characters: ['Grosch'] }, 'grosch'),
  several: P._targetsMe({ characters: ['Mordos', 'Grosch'] }, 'Grosch'),
  legacy: P._targetsMe({ character: 'Grosch' }, 'Grosch'),
  other: P._targetsMe({ characters: ['Zarith'] }, 'Grosch'),
  nobody: P._targetsMe({ characters: ['Grosch'] }, ''),
  any: P._targetsMe({ characters: ['any'] }, 'Grosch'),
  query: P.streamQuery(),
  registered: sandbox.window.DisplayModules.list(),
}));
"""


@unittest.skipUnless(shutil.which("node"), "node not installed")
class PlayAsScriptTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        out = subprocess.run(["node", "-e", NODE_HARNESS, str(JS / "modules.js"), str(JS / "play-as.js")],
                             capture_output=True, text=True, encoding="utf-8", timeout=60)
        if out.returncode != 0:
            raise AssertionError(out.stderr)
        cls.out = json.loads(out.stdout)

    def test_dialog_opens_only_for_this_screens_character(self):
        self.assertTrue(self.out["single"])
        self.assertTrue(self.out["several"])
        self.assertTrue(self.out["legacy"])
        self.assertFalse(self.out["other"])
        self.assertFalse(self.out["nobody"])
        self.assertFalse(self.out["any"])

    def test_no_binding_means_a_plain_stream(self):
        self.assertEqual(self.out["query"], "")
        self.assertIn("play-as", self.out["registered"])


if __name__ == "__main__":
    unittest.main()
