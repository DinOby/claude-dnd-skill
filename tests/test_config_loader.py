"""Tests for display/config_loader.py — layered JSON config.

Covers the merge rules (objects merge, lists/scalars replace, null deletes),
the failure policy (a bad user override never takes the display down), and
reload-on-change.
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stderr
from io import StringIO
from pathlib import Path


REPO = Path(__file__).resolve().parent.parent
SKILL = REPO / "skills" / "dnd" if (REPO / "skills" / "dnd").is_dir() else REPO
DISPLAY = SKILL / "display"


def _load_module(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, str(path))
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


cl = _load_module(DISPLAY / "config_loader.py", "config_loader_under_test")


DEFAULT = {
    "version": 1,
    "cooldown_ms": 1500,
    "triggers": {
        "de": {"attack": ["greift an"], "steal": ["stiehlt"]},
        "en": {"attack": ["attacks"]},
    },
}


def _validate(cfg: dict) -> list:
    return cl.check_types(cfg, {"version": int, "cooldown_ms": int, "triggers": dict})


class DeepMergeTests(unittest.TestCase):
    def test_objects_merge_lists_replace(self):
        merged = cl.deep_merge(DEFAULT, {"triggers": {"de": {"steal": ["klaut"]}}})
        self.assertEqual(merged["triggers"]["de"]["steal"], ["klaut"])
        self.assertEqual(merged["triggers"]["de"]["attack"], ["greift an"])
        self.assertEqual(merged["triggers"]["en"], {"attack": ["attacks"]})

    def test_null_removes_key(self):
        merged = cl.deep_merge(DEFAULT, {"triggers": {"en": None}})
        self.assertNotIn("en", merged["triggers"])

    def test_inputs_not_mutated(self):
        override = {"triggers": {"de": {"heal": ["heilt"]}}}
        cl.deep_merge(DEFAULT, override)
        self.assertNotIn("heal", DEFAULT["triggers"]["de"])
        self.assertEqual(override, {"triggers": {"de": {"heal": ["heilt"]}}})

    def test_object_replaces_scalar_and_vice_versa(self):
        self.assertEqual(cl.deep_merge({"a": 1}, {"a": {"b": 2}}), {"a": {"b": 2}})
        self.assertEqual(cl.deep_merge({"a": {"b": 2}}, {"a": 3}), {"a": 3})


class CheckTypesTests(unittest.TestCase):
    def test_reports_missing_and_wrong_types(self):
        problems = cl.check_types({"version": "1"}, {"version": int, "triggers": dict})
        self.assertEqual(len(problems), 2)

    def test_bool_is_not_an_int(self):
        self.assertTrue(cl.check_types({"version": True}, {"version": int}))

    def test_tuple_of_types(self):
        self.assertEqual(cl.check_types({"x": 1.5}, {"x": (int, float)}), [])


class ConfigFileTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        base = Path(self._tmp.name)
        self.default_dir = base / "default"
        self.user_dir = base / "user"
        self.default_dir.mkdir()
        self.user_dir.mkdir()
        self._write(self.default_dir / "vfx.json", DEFAULT)
        self._stderr = StringIO()

    def tearDown(self):
        self._tmp.cleanup()

    @staticmethod
    def _write(path: Path, data, raw: "str | None" = None, encoding: str = "utf-8"):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(raw if raw is not None else json.dumps(data, ensure_ascii=False),
                        encoding=encoding)

    def _cfg(self, name: str = "vfx.json", validator=_validate):
        return cl.ConfigFile(name, validator=validator,
                             default_dir=str(self.default_dir), user_dir=str(self.user_dir))

    def _get(self, cfg):
        with redirect_stderr(self._stderr):
            return cfg.get()

    def test_default_only(self):
        cfg = self._cfg()
        self.assertEqual(self._get(cfg), DEFAULT)
        self.assertFalse(cfg.override_active)
        self.assertEqual(cfg.warnings, [])

    def test_override_beats_default(self):
        self._write(self.user_dir / "vfx.json", {"cooldown_ms": 500,
                                                 "triggers": {"de": {"steal": ["mopst"]}}})
        cfg = self._cfg()
        data = self._get(cfg)
        self.assertEqual(data["cooldown_ms"], 500)
        self.assertEqual(data["triggers"]["de"]["steal"], ["mopst"])
        self.assertEqual(data["triggers"]["de"]["attack"], ["greift an"])
        self.assertTrue(cfg.override_active)

    def test_broken_override_json_falls_back_to_default(self):
        self._write(self.user_dir / "vfx.json", None, raw='{"cooldown_ms": 500,')
        cfg = self._cfg()
        self.assertEqual(self._get(cfg), DEFAULT)
        self.assertFalse(cfg.override_active)
        self.assertEqual(len(cfg.warnings), 1)
        self.assertIn("override ignored", self._stderr.getvalue())

    def test_override_that_breaks_validation_is_ignored(self):
        self._write(self.user_dir / "vfx.json", {"cooldown_ms": "schnell"})
        cfg = self._cfg()
        self.assertEqual(self._get(cfg), DEFAULT)
        self.assertIn("cooldown_ms", cfg.warnings[0])

    def test_override_must_be_an_object(self):
        self._write(self.user_dir / "vfx.json", ["not", "an", "object"])
        cfg = self._cfg()
        self.assertEqual(self._get(cfg), DEFAULT)
        self.assertTrue(cfg.warnings)

    def test_bom_and_umlauts_are_read(self):
        # Windows Notepad saves UTF-8 with a BOM.
        self._write(self.user_dir / "vfx.json", {"triggers": {"de": {"attack": ["schlägt zu"]}}},
                    encoding="utf-8-sig")
        data = self._get(self._cfg())
        self.assertEqual(data["triggers"]["de"]["attack"], ["schlägt zu"])

    def test_missing_default_yields_empty_with_warning(self):
        cfg = self._cfg("absent.json", validator=None)
        self.assertEqual(self._get(cfg), {})
        self.assertIn("bundled default missing", cfg.warnings[0])

    def test_reload_on_change(self):
        cfg = self._cfg()
        self._get(cfg)
        gen = cfg.generation
        self.assertEqual(self._get(cfg)["cooldown_ms"], 1500)
        self.assertEqual(cfg.generation, gen, "unchanged files must not reload")

        user = self.user_dir / "vfx.json"
        self._write(user, {"cooldown_ms": 900})
        self.assertEqual(self._get(cfg)["cooldown_ms"], 900)
        self.assertEqual(cfg.generation, gen + 1)

        user.unlink()
        self.assertEqual(self._get(cfg)["cooldown_ms"], 1500)
        self.assertFalse(cfg.override_active)

    def test_same_size_edit_is_detected_via_mtime(self):
        user = self.user_dir / "vfx.json"
        self._write(user, {"cooldown_ms": 900})
        cfg = self._cfg()
        self.assertEqual(self._get(cfg)["cooldown_ms"], 900)
        self._write(user, {"cooldown_ms": 800})
        st = user.stat()
        os.utime(user, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000_000))
        self.assertEqual(self._get(cfg)["cooldown_ms"], 800)

    def test_get_returns_a_copy(self):
        cfg = self._cfg()
        self._get(cfg)["triggers"]["de"]["attack"].append("mutated")
        self.assertEqual(self._get(cfg)["triggers"]["de"]["attack"], ["greift an"])

    def test_subdirectory_names(self):
        self._write(self.default_dir / "map-templates" / "bridge.json", {"id": "bridge", "cols": 12})
        self._write(self.user_dir / "map-templates" / "bridge.json", {"cols": 16})
        data = self._get(self._cfg("map-templates/bridge.json", validator=None))
        self.assertEqual(data, {"id": "bridge", "cols": 16})

    def test_rejects_path_escapes(self):
        for bad in ("../secrets.json", os.path.abspath("x.json"), "vfx.txt"):
            with self.assertRaises(ValueError):
                self._cfg(bad)

    def test_load_config_one_shot(self):
        self._write(self.user_dir / "vfx.json", {"version": 2})
        data = cl.load_config("vfx.json", validator=_validate,
                              default_dir=str(self.default_dir), user_dir=str(self.user_dir))
        self.assertEqual(data["version"], 2)


class UserDirResolutionTests(unittest.TestCase):
    def test_user_dir_follows_campaign_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            old = os.environ.get("DND_CAMPAIGN_ROOT")
            os.environ["DND_CAMPAIGN_ROOT"] = tmp
            try:
                resolved = Path(cl._default_user_dir()).resolve()
            finally:
                if old is None:
                    os.environ.pop("DND_CAMPAIGN_ROOT", None)
                else:
                    os.environ["DND_CAMPAIGN_ROOT"] = old
            self.assertEqual(resolved, (Path(tmp) / "config").resolve())


if __name__ == "__main__":
    unittest.main()
