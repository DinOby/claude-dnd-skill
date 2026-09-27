"""Tests for action overlays: display/vfx.py, its routes, and send.py --vfx.

The bundled config files are validated here too, so a bad edit to
display/config/vfx-*.json fails CI instead of silently disabling overlays.
"""

from __future__ import annotations

import importlib.util
import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest import mock


REPO = Path(__file__).resolve().parent.parent
SKILL = REPO / "skills" / "dnd" if (REPO / "skills" / "dnd").is_dir() else REPO
DISPLAY = SKILL / "display"
CONFIG = DISPLAY / "config"
sys.path.insert(0, str(DISPLAY))


def _load_module(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, str(path))
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


vfx = _load_module(DISPLAY / "vfx.py", "vfx_under_test")


class _VfxCase(unittest.TestCase):
    """Fresh vfx state on bundled defaults, an empty user dir, captured broadcasts."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.user_dir = Path(self._tmp.name) / "config"
        self.icon_dir = Path(self._tmp.name) / "assets" / "vfx"
        self.user_dir.mkdir(parents=True)
        self.icon_dir.mkdir(parents=True)
        vfx.configure(default_dir=str(CONFIG), user_dir=str(self.user_dir),
                      user_icon_dir=str(self.icon_dir))
        vfx.set_languages(None)
        self.sent = []
        vfx.set_broadcast(self.sent.append)

    def tearDown(self):
        vfx.set_broadcast(None)
        vfx.configure()
        self._tmp.cleanup()

    def effects(self):
        return [m["vfx"]["effect"] for m in self.sent]


class BundledConfigTests(_VfxCase):
    def test_bundled_configs_are_valid(self):
        for cfg in (vfx._triggers_cfg, vfx._iconset_cfg):
            cfg.get()
            self.assertEqual(cfg.warnings, [], cfg.name)

    def test_every_triggered_effect_has_a_look(self):
        effects = set(vfx.get_iconset()["effects"])
        triggers = json.loads((CONFIG / "vfx-triggers.json").read_text(encoding="utf-8"))
        for lang, pack in triggers["triggers"].items():
            self.assertLessEqual(set(pack), effects, lang)

    def test_every_bundled_icon_exists(self):
        for effect, spec in vfx.get_iconset()["effects"].items():
            self.assertIsNotNone(vfx.icon_path(spec["icon"]), f"{effect}: {spec['icon']}")


class KeywordTests(_VfxCase):
    def test_german_narration_fires_effect(self):
        self.assertEqual(vfx.on_text("Kira stiehlt dem Händler den Beutel.", now=100.0), ["steal"])
        self.assertEqual(self.sent, [{"vfx": {"effect": "steal", "actor": None, "source": "keyword"}}])

    def test_specific_effects_win_over_generic_attack(self):
        # "greift an" (attack) and "schießt" (ranged) — pack order puts ranged first.
        self.assertEqual(vfx.on_text("Der Späher schießt und greift an.", now=100.0), ["ranged"])

    def test_steel_is_not_theft(self):
        # "Stahl" (steel) must not trip the theft overlay via the past tense of stehlen.
        self.assertEqual(vfx.on_text("Eine Klinge aus kaltem Stahl.", now=100.0), [])

    def test_cooldown(self):
        vfx.on_text("Mira zaubert.", now=100.0)
        self.assertEqual(vfx.on_text("Der Priester heilt sie.", now=101.0), [])
        self.assertEqual(vfx.on_text("Der Priester heilt sie.", now=103.0), ["heal"])
        self.assertEqual(self.effects(), ["spell", "heal"])

    def test_no_broadcast_without_wiring(self):
        vfx.set_broadcast(None)
        self.assertEqual(vfx.on_text("Er stiehlt.", now=100.0), [])

    def test_language_selection(self):
        vfx.set_languages(["en"])
        self.assertEqual(vfx.on_text("Kira stiehlt.", now=100.0), [])
        self.assertEqual(vfx.on_text("Kira steals.", now=200.0), ["steal"])
        vfx.set_languages(None)
        self.assertEqual(vfx.on_text("Kira stiehlt.", now=300.0), ["steal"])
        self.assertEqual(vfx.available_languages(), ["de", "en"])

    def test_user_override_applies_without_restart(self):
        self.assertEqual(vfx.on_text("Er bemächtigt sich der Krone.", now=100.0), [])
        (self.user_dir / "vfx-triggers.json").write_text(json.dumps({
            "cooldown_ms": 0, "max_per_chunk": 2,
            "triggers": {"de": {"steal": ["bemächtigt sich"]}},
        }), encoding="utf-8")
        self.assertEqual(vfx.on_text("Er bemächtigt sich der Krone und heilt.", now=100.1),
                         ["steal", "heal"])

    def test_broken_override_keeps_defaults(self):
        (self.user_dir / "vfx-triggers.json").write_text('{"triggers": ', encoding="utf-8")
        with redirect_stderr(io.StringIO()):
            self.assertEqual(vfx.on_text("Kira stiehlt.", now=100.0), ["steal"])


class ExplicitTests(_VfxCase):
    def test_trigger_with_actor(self):
        payload = vfx.trigger("Spell", " Mira ", now=100.0)
        self.assertEqual(payload, {"effect": "spell", "actor": "Mira", "source": "explicit"})
        self.assertEqual(self.sent, [{"vfx": payload}])

    def test_trigger_bypasses_and_restarts_cooldown(self):
        vfx.on_text("Mira zaubert.", now=100.0)
        self.assertIsNotNone(vfx.trigger("attack", now=100.5))
        self.assertEqual(vfx.on_text("Er stiehlt.", now=102.0), [], "cooldown restarted at 100.5")
        self.assertEqual(self.effects(), ["spell", "attack"])

    def test_invalid_names_rejected(self):
        for bad in ("", "Feuer Ball", "../x", "a" * 40, "<script>"):
            self.assertIsNone(vfx.trigger(bad), bad)
        self.assertEqual(self.sent, [])

    def test_actor_is_truncated(self):
        self.assertEqual(len(vfx.trigger("attack", "x" * 200)["actor"]), vfx.ACTOR_MAX)

    def test_parse_spec(self):
        self.assertEqual(vfx.parse_spec("attack:Flerb"), ("attack", "Flerb"))
        self.assertEqual(vfx.parse_spec(" heal "), ("heal", None))
        self.assertEqual(vfx.parse_spec("spell:Mira: die Weise"), ("spell", "Mira: die Weise"))
        self.assertEqual(vfx.parse_spec("bad name:X"), (None, None))


class IconTests(_VfxCase):
    def test_user_icon_wins(self):
        (self.icon_dir / "attack.png").write_bytes(b"\x89PNG user")
        self.assertEqual(vfx.icon_path("attack.png"), (str(self.icon_dir), "attack.png"))
        self.assertEqual(vfx.icon_path("heal.png"), (vfx.BUNDLED_ICON_DIR, "heal.png"))

    def test_rejects_paths_and_non_images(self):
        for bad in ("../vfx.py", "sub/attack.png", "vfx.py", ".png", ""):
            self.assertIsNone(vfx.icon_path(bad), bad)

    def test_iconset_validation(self):
        (self.user_dir / "vfx-iconset.json").write_text(json.dumps({
            "effects": {"attack": {"icon": "../../etc/passwd.png"}},
        }), encoding="utf-8")
        with redirect_stderr(io.StringIO()):
            iconset = vfx.get_iconset()
        self.assertEqual(iconset["effects"]["attack"]["icon"], "attack.png")
        self.assertTrue(vfx._iconset_cfg.warnings)


def _import_app():
    spec = importlib.util.spec_from_file_location(
        "_vfx_app_under_test", str(DISPLAY / "dnd-display-app.py"))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod   # Flask resolves its root via sys.modules
    spec.loader.exec_module(mod)
    return mod


class AppRouteTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = _import_app()
        cls.app._token_ok = lambda: True
        cls.client = cls.app.app.test_client()

    def setUp(self):
        self.vfx = self.app._vfx
        self.assertIsNotNone(self.vfx)
        self.vfx.configure(default_dir=str(CONFIG), user_dir=tempfile.mkdtemp(),
                           user_icon_dir=tempfile.mkdtemp())
        self.vfx.set_languages(None)
        self.sent = []
        self.vfx.set_broadcast(self.sent.append)

    def tearDown(self):
        self.vfx.set_broadcast(None)
        self.vfx.configure()

    def _get(self, url):
        resp = self.client.get(url)
        self.addCleanup(resp.close)
        return resp

    def test_post_vfx_spec(self):
        resp = self.client.post("/vfx", json={"spec": "attack:Flerb"})
        self.assertEqual(resp.status_code, 204)
        self.assertEqual(self.sent[0]["vfx"]["effect"], "attack")
        self.assertEqual(self.sent[0]["vfx"]["actor"], "Flerb")

    def test_post_vfx_rejects_bad_name(self):
        self.assertEqual(self.client.post("/vfx", json={"spec": "bad name"}).status_code, 400)
        self.assertEqual(self.client.post("/vfx", json={}).status_code, 400)
        self.assertEqual(self.sent, [])

    def test_post_vfx_requires_token(self):
        with mock.patch.object(self.app, "_token_ok", lambda: False):
            self.assertEqual(self.client.post("/vfx", json={"spec": "attack"}).status_code, 403)

    def test_iconset_and_icons(self):
        iconset = self._get("/vfx/iconset").get_json()
        self.assertIn("attack", iconset["effects"])
        resp = self._get("/vfx/icons/" + iconset["effects"]["attack"]["icon"])
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.mimetype, "image/png")
        self.assertEqual(self._get("/vfx/icons/..%2Fvfx.py").status_code, 404)
        self.assertEqual(self._get("/vfx/icons/nope.png").status_code, 404)

    def test_dm_narration_triggers_overlay_player_text_does_not(self):
        with mock.patch.object(self.app, "_persist_log"), \
             mock.patch.object(self.app, "_persist_tail"), \
             mock.patch.object(self.app, "_broadcast"):
            self.client.post("/chunk", json={"text": "Flerb stiehlt die Karte.", "player": "Flerb"})
            self.assertEqual(self.sent, [])
            self.client.post("/chunk", json={"text": "Die Diebin stiehlt die Karte."})
        self.assertEqual([m["vfx"]["effect"] for m in self.sent], ["steal"])


class CampaignFlagTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = sys.modules.get("_vfx_app_under_test") or _import_app()

    def _flag(self, state_md: str, flag: str):
        with tempfile.TemporaryDirectory() as tmp:
            camp_file = Path(tmp) / ".campaign"
            camp_file.write_text("ashveil", encoding="utf-8")
            camp_dir = Path(tmp) / "ashveil"
            camp_dir.mkdir()
            (camp_dir / "state.md").write_text(state_md, encoding="utf-8")
            with mock.patch.object(self.app, "rt", lambda name: str(Path(tmp) / name)), \
                 mock.patch.object(self.app, "_find_campaign", lambda name: camp_dir):
                return self.app._campaign_flag_list(flag)

    def test_reads_comma_list(self):
        md = "## Session Flags\nsfx_languages: de, en\nvfx_languages: de\n"
        self.assertEqual(self._flag(md, "sfx_languages"), ["de", "en"])
        self.assertEqual(self._flag(md, "vfx_languages"), ["de"])

    def test_missing_flag(self):
        self.assertIsNone(self._flag("## Session Flags\nsfx_languages: de\n", "vfx_languages"))

    def test_does_not_run_onto_next_line(self):
        # The old \s-based pattern swallowed a following plain-text line.
        md = "sfx_languages: de\nNotes about the party\n"
        self.assertEqual(self._flag(md, "sfx_languages"), ["de"])


class SendFlagTests(unittest.TestCase):
    """send.py --vfx: posted before the narration, and never fatal."""

    def _run(self, argv, post_result):
        send = _load_module(DISPLAY / "send.py", "_send_vfx_under_test")
        calls = []

        def fake_post(url, data, token):
            calls.append((url.rsplit("/", 1)[-1], json.loads(data)))
            ok = post_result(url)
            send._SEND_LOG.append({"endpoint": send._endpoint_label(url), "ok": ok,
                                   **({} if ok else {"reason": "http 400"})})
            return ok

        err = io.StringIO()
        with mock.patch.object(send, "_post", fake_post), \
             mock.patch.object(send, "_read_token", lambda: ""), \
             mock.patch.object(sys, "argv", ["send.py", *argv]), \
             mock.patch.object(sys, "stdin", io.StringIO("Die Axt trifft.\n")), \
             redirect_stderr(err):
            send.main()
        return calls, err.getvalue()

    def test_overlay_posted_before_text(self):
        calls, _ = self._run(["--vfx", "attack:Flerb", "--vfx", "defend"], lambda url: True)
        self.assertEqual([c[0] for c in calls], ["vfx", "vfx", "chunk"])
        self.assertEqual(calls[0][1], {"spec": "attack:Flerb"})

    def test_failed_overlay_only_warns(self):
        calls, err = self._run(["--vfx", "typo name"], lambda url: not url.endswith("/vfx"))
        self.assertEqual([c[0] for c in calls], ["vfx", "chunk"])
        self.assertIn("overlay 'typo name' skipped", err)
        self.assertNotIn("PARTIAL FAILURE", err)


if __name__ == "__main__":
    unittest.main()


NODE_HARNESS = r"""
const fs = require('fs');
const vm = require('vm');
const [modulesPath, vfxPath] = process.argv.slice(-2);

function load(search) {
  const sandbox = { location: { search }, URLSearchParams, console };
  sandbox.window = sandbox;
  vm.runInNewContext(fs.readFileSync(modulesPath, 'utf8'), sandbox);
  vm.runInNewContext(fs.readFileSync(vfxPath, 'utf8'), sandbox);   // no document → no DOM work
  return sandbox.window;
}

const w = load('');
const V = w.VfxOverlay;
const set = {
  fallback: 'attack',
  effects: {
    attack: { icon: 'attack.png', animation: 'slash', duration_ms: 900, tint: '#e05a4a', label: 'Attack' },
    odd:    { icon: 'odd file.png', animation: 'explode', duration_ms: 99999, tint: 'red; x:y' },
  },
};
const out = {
  registered: w.DisplayModules.list(),
  attack: V._resolveEffect(set, 'attack'),
  unknown: V._resolveEffect(set, 'nope'),
  odd: V._resolveEffect(set, 'odd'),
  none: V._resolveEffect({ effects: {} }, 'attack'),
  nullSet: V._resolveEffect(null, 'attack'),
  caption: [V._caption({ label: 'Attack' }, 'Flerb'), V._caption({ label: 'Attack' }, null), V._caption({ label: '' }, null)],
  queue: V._enqueue(V._enqueue(V._enqueue([], 1, 2), 2, 2), 3, 2),
};
console.log(JSON.stringify(out));
"""


@unittest.skipIf(__import__("shutil").which("node") is None, "node not installed")
class OverlayScriptTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import subprocess
        js = DISPLAY / "static" / "js"
        proc = subprocess.run(["node", "-e", NODE_HARNESS, str(js / "modules.js"), str(js / "vfx.js")],
                              capture_output=True, text=True, encoding="utf-8", timeout=30)
        if proc.returncode != 0:
            raise AssertionError(f"node harness failed:\n{proc.stderr}")
        cls.out = json.loads(proc.stdout)

    def test_registers_with_dispatcher(self):
        self.assertEqual(self.out["registered"], ["vfx"])

    def test_resolves_look(self):
        self.assertEqual(self.out["attack"], {"icon": "/vfx/icons/attack.png", "animation": "slash",
                                              "duration": 900, "tint": "#e05a4a", "label": "Attack"})

    def test_unknown_effect_uses_fallback(self):
        self.assertEqual(self.out["unknown"], self.out["attack"])
        self.assertIsNone(self.out["none"])
        self.assertIsNone(self.out["nullSet"])

    def test_untrusted_values_are_sanitised(self):
        odd = self.out["odd"]
        self.assertEqual(odd["icon"], "/vfx/icons/odd%20file.png")
        self.assertEqual(odd["animation"], "pop")
        self.assertEqual(odd["duration"], 10000)
        self.assertEqual(odd["tint"], "#e8c05a")

    def test_caption_and_queue(self):
        self.assertEqual(self.out["caption"], ["Flerb · Attack", "Attack", ""])
        self.assertEqual(self.out["queue"], [2, 3])

    def test_index_loads_vfx_after_dispatcher_and_has_toggle(self):
        html = (DISPLAY / "templates" / "index.html").read_text(encoding="utf-8")
        self.assertLess(html.index('src="/static/js/modules.js"'), html.index('src="/static/js/vfx.js"'))
        self.assertLess(html.index('src="/static/js/vfx.js"'), html.index("function connect()"))
        self.assertIn('id="vfx-row"', html)
        self.assertIn('id="vfx-track"', html)
