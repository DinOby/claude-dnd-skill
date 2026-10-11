"""The players' logbook: served from <campaign>/logbuch.md, rendered safely.

Run from repo root:
    python3 -m unittest tests.test_logbook -v
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


class LogbookRouteTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.mod = import_app("dnd_display_app")
        cls.client = cls.mod.app.test_client()

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.camp = pathlib.Path(self._tmp.name) / "krone"
        self.camp.mkdir()
        for name, value in (("_current_campaign", lambda: "krone"),
                            ("_find_campaign", lambda name: self.camp)):
            p = mock.patch.object(self.mod, name, value)
            p.start()
            self.addCleanup(p.stop)

    def test_missing_logbook_is_empty_not_an_error(self):
        data = self.client.get("/logbook").get_json()
        self.assertEqual(data, {"markdown": "", "updated": None})

    def test_logbook_is_served(self):
        (self.camp / "logbuch.md").write_text("# Logbuch\r\n\r\n## Der Schlüssel\r\n- Benno\r\n", encoding="utf-8")
        data = self.client.get("/logbook").get_json()
        self.assertEqual(data["markdown"], "# Logbuch\n\n## Der Schlüssel\n- Benno\n")
        self.assertIsNotNone(data["updated"])

    def test_notify_needs_the_token(self):
        self.assertEqual(self.client.post("/logbook/notify").status_code, 403)


NODE_HARNESS = r"""
const fs = require('fs');
const vm = require('vm');
const files = process.argv.slice(-2);
const sandbox = { location: { search: '' }, URLSearchParams, console };
sandbox.window = sandbox;
for (const f of files) vm.runInNewContext(fs.readFileSync(f, 'utf8'), sandbox);
const R = sandbox.window.Logbook._render;
console.log(JSON.stringify({
  heading: R('# Logbuch\n## Der Schlüssel'),
  list: R('- **Benno** hat *gestanden*\n- [ ] Was ist hinter den Ketten?\n- [x] Benno gefunden'),
  table: R('| Name | Wo |\n| --- | --- |\n| Hanne | Schimmelsteg |'),
  script: R('<script>alert(1)</script> & "x"'),
  link: R('[Quelle](https://example.com/a) und [böse](javascript:alert(1))'),
  para: R('Erste Zeile\nzweite Zeile\n\nNeuer Absatz'),
}));
"""


@unittest.skipUnless(shutil.which("node"), "node not installed")
class LogbookRenderTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        out = subprocess.run(["node", "-e", NODE_HARNESS, str(JS / "modules.js"), str(JS / "logbook.js")],
                             capture_output=True, text=True, encoding="utf-8", timeout=60)
        if out.returncode != 0:
            raise AssertionError(out.stderr)
        cls.out = json.loads(out.stdout)

    def test_headings_shift_down_one_level(self):
        self.assertEqual(self.out["heading"], "<h2>Logbuch</h2>\n<h3>Der Schlüssel</h3>")

    def test_lists_tasks_and_emphasis(self):
        html = self.out["list"]
        self.assertIn("<li><strong>Benno</strong> hat <em>gestanden</em></li>", html)
        self.assertIn('<span class="lb-check">☐</span> Was ist hinter den Ketten?', html)
        self.assertIn('<span class="lb-check done">☑</span> Benno gefunden', html)

    def test_tables(self):
        self.assertIn("<th>Name</th><th>Wo</th>", self.out["table"])
        self.assertIn("<td>Hanne</td><td>Schimmelsteg</td>", self.out["table"])

    def test_markup_is_escaped(self):
        self.assertNotIn("<script>", self.out["script"])
        self.assertIn("&lt;script&gt;", self.out["script"])
        self.assertIn("&amp;", self.out["script"])

    def test_only_http_links_become_links(self):
        self.assertIn('<a href="https://example.com/a"', self.out["link"])
        self.assertNotIn('href="javascript', self.out["link"])

    def test_paragraphs(self):
        self.assertEqual(self.out["para"], "<p>Erste Zeile zweite Zeile</p>\n<p>Neuer Absatz</p>")


if __name__ == "__main__":
    unittest.main()
