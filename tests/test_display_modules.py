"""Tests for the frontend module dispatcher (display/static/js/modules.js).

Server side: /static serves the file and index.html wires it in (script tag
before the main script, dispatch at the end of the SSE handler).
Client side: the dispatcher's contract, run under Node when available.
"""

from __future__ import annotations

import importlib.util
import json
import pathlib
import shutil
import subprocess
import sys
import unittest

REPO = pathlib.Path(__file__).resolve().parent.parent
SKILL = REPO / "skills" / "dnd" if (REPO / "skills" / "dnd").is_dir() else REPO
DISPLAY = SKILL / "display"
sys.path.insert(0, str(DISPLAY))

MODULES_JS = DISPLAY / "static" / "js" / "modules.js"
INDEX_HTML = DISPLAY / "templates" / "index.html"


def _import_app():
    spec = importlib.util.spec_from_file_location(
        "_display_modules_app_under_test", str(DISPLAY / "dnd-display-app.py")
    )
    mod = importlib.util.module_from_spec(spec)
    # Flask resolves its root (and so /static) through sys.modules.
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


class StaticWiringTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = _import_app().app.test_client()

    def test_static_route_serves_modules_js(self):
        resp = self.client.get("/static/js/modules.js")
        try:
            self.assertEqual(resp.status_code, 200)
            self.assertIn("javascript", resp.content_type)
            self.assertIn(b"DisplayModules", resp.data)
        finally:
            resp.close()

    def test_static_route_refuses_path_escape(self):
        resp = self.client.get("/static/../dnd-display-app.py")
        try:
            self.assertEqual(resp.status_code, 404)
        finally:
            resp.close()

    def test_index_loads_modules_before_main_script_and_dispatches(self):
        html = INDEX_HTML.read_text(encoding="utf-8")
        tag = html.index('<script src="/static/js/modules.js"></script>')
        connect = html.index("function connect()")
        self.assertLess(tag, connect, "modules.js must load before the SSE stream opens")

        handler = html[html.index("evtSource.onmessage"):html.index("evtSource.onerror")]
        self.assertIn("DisplayModules.dispatch(payload)", handler)


NODE_HARNESS = r"""
const fs = require('fs');
const vm = require('vm');
const src = fs.readFileSync(process.argv[process.argv.length - 1], 'utf8');

function load(search) {
  const logs = [];
  const sandbox = {
    location: { search },
    URLSearchParams,
    console: { error: (...a) => logs.push(a.map(String).join(' ')) },
  };
  sandbox.window = sandbox;
  vm.runInNewContext(src, sandbox);
  return { DM: sandbox.window.DisplayModules, logs };
}

const out = {};

// Main display: every handler runs, in registration order, with the payload.
{
  const { DM, logs } = load('');
  const seen = [];
  DM.register('a', (p, ctx) => seen.push(['a', p.x, ctx.isPhone]));
  DM.register('boom', () => { throw new Error('kaputt'); });
  DM.register('b', p => seen.push(['b', p.x]), { displayOnly: true });
  DM.dispatch({ x: 1 });
  out.main = { seen: seen.slice(), logs, list: DM.list(), frozen: Object.isFrozen(DM) };

  DM.register('a', p => seen.push(['a2', p.x]));   // replace, moves to end
  DM.unregister('boom');
  seen.length = 0;
  DM.dispatch({ x: 2 });
  out.replaced = { seen, list: DM.list() };

  let threw = 0;
  try { DM.register('', () => {}); } catch (e) { threw++; }
  try { DM.register('x', 'nope'); } catch (e) { threw++; }
  out.badArgs = threw;
}

// Phone controller: displayOnly handlers are skipped.
for (const search of ['?char=Mira', '?character=Mira', '?view=input']) {
  const { DM } = load(search);
  const seen = [];
  DM.register('everywhere', () => seen.push('everywhere'));
  DM.register('tv', () => seen.push('tv'), { displayOnly: true });
  DM.dispatch({});
  out[search] = { seen, ctx: DM.context };
}

console.log(JSON.stringify(out));
"""


@unittest.skipIf(shutil.which("node") is None, "node not installed")
class DispatcherBehaviourTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        proc = subprocess.run(
            ["node", "-e", NODE_HARNESS, str(MODULES_JS)],
            capture_output=True, text=True, encoding="utf-8", timeout=30,
        )
        if proc.returncode != 0:
            raise AssertionError(f"node harness failed:\n{proc.stderr}")
        cls.out = json.loads(proc.stdout)

    def test_handlers_run_in_order_and_errors_are_isolated(self):
        main = self.out["main"]
        self.assertEqual(main["seen"], [["a", 1, False], ["b", 1]])
        self.assertEqual(len(main["logs"]), 1)
        self.assertIn("boom", main["logs"][0])
        self.assertIn("kaputt", main["logs"][0])
        self.assertEqual(main["list"], ["a", "boom", "b"])
        self.assertTrue(main["frozen"])

    def test_register_replaces_and_unregister_removes(self):
        self.assertEqual(self.out["replaced"]["seen"], [["b", 2], ["a2", 2]])
        self.assertEqual(self.out["replaced"]["list"], ["b", "a"])

    def test_invalid_registration_rejected(self):
        self.assertEqual(self.out["badArgs"], 2)

    def test_phone_skips_display_only(self):
        for search in ("?char=Mira", "?character=Mira", "?view=input"):
            res = self.out[search]
            self.assertEqual(res["seen"], ["everywhere"], search)
            self.assertTrue(res["ctx"]["isPhone"], search)
        self.assertEqual(self.out["?char=Mira"]["ctx"]["character"], "Mira")
        self.assertEqual(self.out["?view=input"]["ctx"]["character"], "")


if __name__ == "__main__":
    unittest.main()
