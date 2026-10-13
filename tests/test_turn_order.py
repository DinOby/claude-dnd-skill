"""The server keeps the whole initiative order when only the current turn moves.

`push_stats.py --turn-current NAME` sends {"turn_order": {"current": NAME}}.
The browser merged that into its order, but the server replaced the stored
order with it — so anyone who reloaded the page mid-fight saw no order at all.

Run from repo root:
    python3 -m unittest tests.test_turn_order -v
"""
import json
import pathlib
import sys
import unittest

REPO = pathlib.Path(__file__).resolve().parent.parent
SKILL = REPO / "skills" / "dnd" if (REPO / "skills" / "dnd").is_dir() else REPO
sys.path.insert(0, str(SKILL / "display"))

from tests.app_isolation import import_app  # noqa: E402

ORDER = ["Mordos", "Grosch", "Lampenträger 3", "Zarith"]


class TurnOrderMergeTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.mod = import_app("dnd_display_app")
        cls.mod._token_ok = lambda: True
        cls.mod._host_ok = lambda: True
        cls.client = cls.mod.app.test_client()

    def setUp(self):
        self.mod._current_stats = {"players": []}
        self._post({"turn_order": {"order": ORDER, "current": "Mordos", "round": 1}})

    def _post(self, body):
        r = self.client.post("/stats", data=json.dumps(body), content_type="application/json")
        self.assertIn(r.status_code, (200, 204), msg=r.data)

    def _stored(self):
        return self.mod._current_stats["turn_order"]

    def test_turn_current_keeps_the_order(self):
        self._post({"turn_order": {"current": "Grosch"}})
        self.assertEqual(self._stored()["order"], ORDER)
        self.assertEqual(self._stored()["current"], "Grosch")
        self.assertEqual(self._stored()["round"], 1)

    def test_new_round_keeps_the_order(self):
        self._post({"turn_order": {"current": "Mordos", "round": 2}})
        self.assertEqual(self._stored()["order"], ORDER)
        self.assertEqual(self._stored()["round"], 2)

    def test_full_order_replaces(self):
        self._post({"turn_order": {"order": ["Zarith", "Mordos"], "current": "Zarith", "round": 1}})
        self.assertEqual(self._stored()["order"], ["Zarith", "Mordos"])

    def test_clear_still_clears(self):
        self._post({"turn_order": None})
        self.assertIsNone(self._stored())


if __name__ == "__main__":
    unittest.main()
