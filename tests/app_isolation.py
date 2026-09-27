"""Load dnd-display-app.py for tests without touching the real data root.

Importing the app binds its stores (maps, scene state, asset store, wait-list,
stats, runtime files) to the data root — normally the user's ~/.claude/dnd
with real campaigns. Every test that loads the app goes through this module:

    from tests.app_isolation import import_app, IsolatedApp

    app = import_app("_my_app_under_test")        # module-level / setUpClass helper
    class MyRouteTests(IsolatedApp): ...           # or: app + test client per class

The first call points DND_CAMPAIGN_ROOT and DND_RUNTIME_DIR at a temporary
directory for the rest of the test process (removed at exit), so every path
the app or any display script computes — at import or later — lands there.
tests/test_test_isolation.py fails if a test module loads the app without it.
"""

from __future__ import annotations

import atexit
import importlib.util
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SKILL = REPO / "skills" / "dnd" if (REPO / "skills" / "dnd").is_dir() else REPO
DISPLAY = SKILL / "display"

_HOME: "Path | None" = None


def isolate_process() -> Path:
    """Point the data root of this test process at a temp dir (once); returns it."""
    global _HOME
    if _HOME is None:
        _HOME = Path(tempfile.mkdtemp(prefix="dnd-test-data-"))
        atexit.register(shutil.rmtree, _HOME, True)
    os.environ["DND_CAMPAIGN_ROOT"] = str(_HOME)
    os.environ["DND_RUNTIME_DIR"] = str(_HOME / ".runtime")
    return _HOME


def import_app(module_name: str):
    """Execute dnd-display-app.py under `module_name` against the isolated data root."""
    isolate_process()
    spec = importlib.util.spec_from_file_location(module_name, str(DISPLAY / "dnd-display-app.py"))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod   # Flask resolves its root (and /static) through sys.modules
    spec.loader.exec_module(mod)
    return mod


class IsolatedApp(unittest.TestCase):
    """Base class: self.app / self.client on an isolated app, token checks off."""
    app_module_name = "_isolated_app_under_test"

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.home = isolate_process()
        cls.app = import_app(cls.app_module_name)
        cls.app._token_ok = lambda: True
        cls.client = cls.app.app.test_client()
