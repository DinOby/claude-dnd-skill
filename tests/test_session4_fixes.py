"""Fixes found at the table in session 4 (2026-10-18).

  * a second device's action for a character that already has one waiting is
    refused, not silently put in its place;
  * one dice request accepts one roll per character — a second device answering
    it is refused;
  * the DM sees when two devices play the same character;
  * XP for a fight is the raw monster XP — the multiplier only rates difficulty;
  * an image entry queued before its token gets the token's category;
  * armour class has its own engine event (ac.set).

Run from repo root:
    python3 -m unittest tests.test_session4_fixes -v
"""
import json
import os
import pathlib
import queue
import re
import subprocess
import sys
import tempfile
import unittest

REPO = pathlib.Path(__file__).resolve().parent.parent
SKILL = REPO / "skills" / "dnd" if (REPO / "skills" / "dnd").is_dir() else REPO
sys.path.insert(0, str(SKILL / "display"))
sys.path.insert(0, str(SKILL / "scripts"))

from tests.app_isolation import import_app  # noqa: E402


class _AppBase(unittest.TestCase):

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
        self.mod._dice_pending.clear()
        self.mod._dice_answered.clear()
        if os.path.exists(self.mod.QUEUE_FILE):
            os.remove(self.mod.QUEUE_FILE)

    def tearDown(self):
        if os.path.exists(self.mod.QUEUE_FILE):
            os.remove(self.mod.QUEUE_FILE)

    def _post(self, path, body, device=""):
        headers = {"X-DND-Device": device} if device else {}
        return self.client.post(path, data=json.dumps(body), content_type="application/json", headers=headers)


class StagedInputTests(_AppBase):

    def test_other_device_cannot_replace_a_waiting_action(self):
        r = self._post("/player-input/stage", {"character": "Zarith", "text": "schießt"}, device="dev-a")
        self.assertEqual(r.status_code, 204)
        r = self._post("/player-input/stage", {"character": "Zarith", "text": "rennt"}, device="dev-b")
        self.assertEqual(r.status_code, 409)
        self.assertEqual(self.mod._staged["Zarith"]["text"], "schießt")

    def test_same_device_may_correct_its_own_action(self):
        self._post("/player-input/stage", {"character": "Zarith", "text": "schießt"}, device="dev-a")
        r = self._post("/player-input/stage", {"character": "Zarith", "text": "zielt genauer"}, device="dev-a")
        self.assertEqual(r.status_code, 204)
        self.assertEqual(self.mod._staged["Zarith"]["text"], "zielt genauer")

    def test_device_id_is_not_broadcast(self):
        self._post("/player-input/stage", {"character": "Zarith", "text": "schießt"}, device="dev-a")
        self.assertNotIn("device", self.mod._staged_snapshot()["Zarith"])

    def test_long_actions_are_kept(self):
        text = "x" * 1400
        self._post("/player-input/stage", {"character": "Mordos", "text": text}, device="dev-a")
        self.assertEqual(len(self.mod._staged["Mordos"]["text"]), 1400)


class DiceRequestTests(_AppBase):

    def _request(self, char):
        r = self._post("/dice-request", {"characters": [char], "spec": "1d20", "modifier": 0})
        self.assertEqual(r.status_code, 200, r.data)
        return r.get_json()["request_id"]

    def test_second_answer_to_one_request_is_refused(self):
        rid = self._request("Zarith")
        r1 = self._post("/player-input/dice", {"character": "Zarith", "spec": "1d20", "request_id": rid})
        self.assertEqual(r1.status_code, 200)
        r2 = self._post("/player-input/dice", {"character": "Zarith", "spec": "1d20", "request_id": rid})
        self.assertEqual(r2.status_code, 409)
        with open(self.mod.QUEUE_FILE, encoding="utf-8") as f:
            self.assertEqual(f.read().count("[Zarith]: rolled"), 1)

    def test_free_rolls_without_request_still_work(self):
        for _ in range(2):
            r = self._post("/player-input/dice", {"character": "Grosch", "spec": "1d20"})
            self.assertEqual(r.status_code, 200)

    def test_cancelled_request_reports_complete(self):
        rid = self._request("Grosch")
        self.assertEqual(self.client.delete(f"/dice-request/{rid}").status_code, 204)
        self.assertTrue(self.client.get(f"/dice-request/{rid}").get_json()["complete"])


class PlayAsConflictTests(_AppBase):

    def tearDown(self):
        super().tearDown()
        with self.mod._clients_lock:
            for q in list(self.mod._client_addr):
                self.mod._client_chars.pop(q, None)
                self.mod._client_addr.pop(q, None)

    def _connect(self, char, addr):
        q = queue.Queue()
        with self.mod._clients_lock:
            self.mod._client_chars[q] = char
            self.mod._client_addr[q] = addr
        return q

    def test_two_devices_on_one_character_are_reported(self):
        self._connect("zarith", "127.0.0.1")
        self._connect("zarith", "100.64.0.7")
        self._connect("mordos", "100.64.0.8")
        data = self.client.get("/play-as/conflicts").get_json()
        self.assertEqual(data, {"zarith": ["100.64.0.7", "127.0.0.1"]})

    def test_one_device_per_character_is_quiet(self):
        self._connect("zarith", "127.0.0.1")
        self._connect("mordos", "100.64.0.8")
        self.assertEqual(self.client.get("/play-as/conflicts").get_json(), {})


class XpRawAwardTests(unittest.TestCase):

    def test_multiplier_rates_difficulty_but_is_not_awarded(self):
        out = subprocess.run(
            [sys.executable, str(SKILL / "scripts" / "xp.py"), "calc", "--level", "2", "--players", "1",
             "--monsters", "veteran:2:1,guard:1/8:4"],
            capture_output=True, text=True, encoding="utf-8", check=True).stdout
        self.assertRegex(out, r"Per player:\s+550 XP")
        self.assertRegex(out, r"Adjusted XP:\s+1,100")


class QueueCategoryTests(unittest.TestCase):

    def test_token_fills_missing_category(self):
        from asset_queue import PendingQueue
        with tempfile.TemporaryDirectory() as tmp:
            q = PendingQueue(os.path.join(tmp, "pending.json"))
            q.add_many([{"key": "token:amboss", "kind": "token", "name": "amboss"}])
            q.add_many([{"key": "token:amboss", "kind": "token", "name": "amboss", "category": "object"}])
            entry = [e for e in q.entries("pending") if e["key"] == "token:amboss"][0]
            self.assertEqual(entry["category"], "object")

    def test_existing_category_is_kept(self):
        from asset_queue import PendingQueue
        with tempfile.TemporaryDirectory() as tmp:
            q = PendingQueue(os.path.join(tmp, "pending.json"))
            q.add_many([{"key": "token:knorz", "kind": "token", "name": "knorz", "category": "npc"}])
            q.add_many([{"key": "token:knorz", "kind": "token", "name": "knorz", "category": "object"}])
            entry = [e for e in q.entries("pending") if e["key"] == "token:knorz"][0]
            self.assertEqual(entry["category"], "npc")


class ArmourClassEventTests(unittest.TestCase):

    def test_schema_allows_ac_set(self):
        from engine import protocol
        schema = protocol.load_schema("event")
        data_schema = schema["dataSchemas"]["ac.set"]
        self.assertEqual(protocol.check({"who": "Grosch", "value": 15}, data_schema), [])
        self.assertTrue(protocol.check({"who": "Grosch", "value": 99}, data_schema))

    def test_state_applies_ac_set(self):
        from engine import state as st
        s = st.empty()
        st.apply(s, {"seq": 1, "type": "ac.set", "data": {"who": "Grosch", "value": 15}})
        figure = next(iter(s["characters"].values()), None) or next(iter(s["others"].values()))
        self.assertEqual(figure["ac"], 15)


if __name__ == "__main__":
    unittest.main()
