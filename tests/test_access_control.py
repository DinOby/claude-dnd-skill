"""Access control of the display server: token in local mode, CORS, Host check.

Unlike the route tests elsewhere, nothing here switches the token check off.
"""

from __future__ import annotations

import os
import unittest
from pathlib import Path

from tests.app_isolation import import_app, isolate_process


class AccessControlTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.home = isolate_process()
        cls.app = import_app("_access_app_under_test")
        cls.client = cls.app.app.test_client()
        cls.token = cls.app._lan_token

    def post(self, url, token=None, **kw):
        headers = kw.pop("headers", {})
        if token:
            headers["X-DND-Token"] = token
        resp = self.client.post(url, json=kw.pop("json", {"x": 1}), headers=headers, **kw)
        self.addCleanup(resp.close)
        return resp

    def test_token_exists_in_local_mode(self):
        self.assertFalse(self.app._LAN_MODE)
        self.assertEqual(len(self.token), 64)
        token_file = Path(self.app.TOKEN_FILE)
        self.assertTrue(str(token_file).startswith(str(self.home)))       # isolated, never ~/.claude/dnd
        self.assertEqual(token_file.read_text(encoding="utf-8").strip(), self.token)

    def test_write_endpoints_need_the_token_locally(self):
        for url in ("/clear", "/audio-toggle", "/map", "/scene", "/vfx"):
            self.assertEqual(self.post(url).status_code, 403, url)
            self.assertEqual(self.post(url, token="wrong").status_code, 403, url)
        self.assertEqual(self.post("/audio-toggle", token=self.token, json={"sfx": False}).status_code, 200)

    def test_page_carries_the_token_for_its_own_requests(self):
        resp = self.client.get("/")
        self.addCleanup(resp.close)
        self.assertIn(f'<meta name="dnd-token" content="{self.token}">', resp.get_data(as_text=True))

    def test_cors_only_for_own_addresses(self):
        for origin in ("http://localhost:5001", "http://127.0.0.1:5001"):
            resp = self.client.options("/stats", headers={"Origin": origin, "Access-Control-Request-Method": "POST",
                                                          "Access-Control-Request-Headers": "X-DND-Token"})
            self.addCleanup(resp.close)
            self.assertEqual(resp.headers.get("Access-Control-Allow-Origin"), origin)
        for origin in ("https://evil.example", "http://localhost:8080", "null"):
            resp = self.client.options("/stats", headers={"Origin": origin, "Access-Control-Request-Method": "POST",
                                                          "Access-Control-Request-Headers": "X-DND-Token"})
            self.addCleanup(resp.close)
            self.assertIsNone(resp.headers.get("Access-Control-Allow-Origin"), origin)
            resp = self.client.get("/map", headers={"Origin": origin})
            self.addCleanup(resp.close)
            self.assertIsNone(resp.headers.get("Access-Control-Allow-Origin"), origin)

    def test_foreign_host_names_are_refused(self):
        for host in ("evil.example", "evil.example:5001", "192.0.2.1:5001"):
            resp = self.client.get("/", headers={"Host": host})
            self.addCleanup(resp.close)
            self.assertEqual(resp.status_code, 403, host)
        for host in ("localhost:5001", "127.0.0.1:5001", "[::1]:5001"):
            resp = self.client.get("/ping", headers={"Host": host})
            self.addCleanup(resp.close)
            self.assertEqual(resp.status_code, 200, host)


if __name__ == "__main__":
    unittest.main()
