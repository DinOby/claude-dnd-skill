"""Party-input actions must reach the DM — none may be dropped.

Session 1 of a three-player online game lost actions twice:
  * a player who typed an action but never pressed Ready was invisible at the
    DM's next turn (the queue file is only written once everyone is ready);
  * a second batch that became ready before the DM read the first replaced it.

Run from repo root:
    python3 -m unittest tests.test_party_input -v
"""
import json
import os
import pathlib
import sys
import unittest

REPO = pathlib.Path(__file__).resolve().parent.parent
SKILL = REPO / "skills" / "dnd" if (REPO / "skills" / "dnd").is_dir() else REPO
sys.path.insert(0, str(SKILL / "display"))

from tests.app_isolation import import_app  # noqa: E402


class PartyInputTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.mod = import_app("dnd_display_app")
        cls.mod._token_ok = lambda: True
        cls.mod._host_ok = lambda: True
        cls.mod._rate_ok = lambda addr: True
        cls.mod._device_ok = lambda device, addr: "approved"
        cls.client = cls.mod.app.test_client()

    def setUp(self):
        self.mod._current_stats = {"players": [{"name": "Grosch"}, {"name": "Mordos"}, {"name": "Zarith"}]}
        self.mod._staged.clear()
        self.mod._expected_count = 3
        self.mod._autorun_threshold = None
        if os.path.exists(self.mod.QUEUE_FILE):
            os.remove(self.mod.QUEUE_FILE)

    def tearDown(self):
        if os.path.exists(self.mod.QUEUE_FILE):
            os.remove(self.mod.QUEUE_FILE)

    def _stage(self, char, text, **extra):
        body = {"character": char, "text": text, **extra}
        r = self.client.post("/player-input/stage", data=json.dumps(body), content_type="application/json")
        self.assertIn(r.status_code, (200, 204), r.data)

    def _queue(self):
        if not os.path.exists(self.mod.QUEUE_FILE):
            return ""
        with open(self.mod.QUEUE_FILE, encoding="utf-8") as f:
            return f.read()

    def test_sent_actions_count_as_ready(self):
        self._stage("Grosch", "brüllt")
        self.assertTrue(self.mod._staged["Grosch"]["ready"])

    def test_queue_written_once_everyone_sent(self):
        for c in ("Grosch", "Mordos", "Zarith"):
            self._stage(c, f"{c} handelt")
        q = self._queue()
        for c in ("Grosch", "Mordos", "Zarith"):
            self.assertIn(f"[{c}]: {c} handelt", q)

    def test_second_batch_is_appended_not_overwritten(self):
        for c in ("Grosch", "Mordos", "Zarith"):
            self._stage(c, "erste Runde")
        for c in ("Grosch", "Mordos", "Zarith"):
            self._stage(c, "zweite Runde")
        q = self._queue()
        self.assertEqual(q.count("erste Runde"), 3)
        self.assertEqual(q.count("zweite Runde"), 3)

    def test_collect_hands_over_unfinished_actions(self):
        self._stage("Grosch", "sucht weiter")
        self._stage("Mordos", "zögert noch", ready=False)
        r = self.client.post("/player-input/collect", data="{}", content_type="application/json")
        got = {e["character"]: e for e in r.get_json()}
        self.assertEqual(got["Grosch"]["text"], "sucht weiter")
        self.assertFalse(got["Mordos"]["ready"])
        self.assertEqual(self.mod._staged, {})

    def test_a_player_roll_reaches_the_dm_turn(self):
        body = {"character": "Grosch", "spec": "1d20", "modifier": 3, "label": "Wahrnehmung"}
        r = self.client.post("/player-input/dice", data=json.dumps(body), content_type="application/json")
        self.assertEqual(r.status_code, 200, r.data)
        total = r.get_json()["total"]
        q = self._queue()
        self.assertIn("[Grosch]: rolled 1d20+3 (Wahrnehmung) = " + str(total), q)


if __name__ == "__main__":
    unittest.main()
