"""Load dnd-display-app.py for route tests without touching the real data root.

Importing the app binds its stores (maps, scene state, asset store, wait-list,
stats) to the data root — normally the user's ~/.claude/dnd with real
campaigns. IsolatedApp points DND_CAMPAIGN_ROOT / DND_RUNTIME_DIR at a temp
directory for the whole test class, so every path the app computes, at import
or later, lands there.

    class MyRouteTests(IsolatedApp):
        def test_x(self):
            self.client.post("/map", json=…)
"""

from __future__ import annotations

import importlib.util
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
SKILL = REPO / "skills" / "dnd" if (REPO / "skills" / "dnd").is_dir() else REPO
DISPLAY = SKILL / "display"


class IsolatedApp(unittest.TestCase):
    app_module_name = "_isolated_app_under_test"

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls._home = tempfile.TemporaryDirectory()
        cls.home = Path(cls._home.name)
        cls._env = mock.patch.dict(os.environ, {"DND_CAMPAIGN_ROOT": str(cls.home),
                                                "DND_RUNTIME_DIR": str(cls.home / ".runtime")})
        cls._env.start()
        spec = importlib.util.spec_from_file_location(cls.app_module_name, str(DISPLAY / "dnd-display-app.py"))
        cls.app = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = cls.app
        spec.loader.exec_module(cls.app)
        cls.app._token_ok = lambda: True
        cls.client = cls.app.app.test_client()

    @classmethod
    def tearDownClass(cls):
        cls._env.stop()
        cls._home.cleanup()
        super().tearDownClass()
