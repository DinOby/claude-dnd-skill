"""Guard: tests never load the display app against the real data root.

Loading dnd-display-app.py binds its stores to the data root (normally the
user's ~/.claude/dnd with real campaigns). tests/app_isolation.py redirects
that to a temp dir; this test fails when a test module loads the app some
other way, and checks that the redirect really reaches runtime paths.
"""

from __future__ import annotations

import os
import re
import sys
import unittest
from pathlib import Path

from tests.app_isolation import DISPLAY, isolate_process

TESTS = Path(__file__).resolve().parent
_LOADS_APP = re.compile(r"spec_from_file_location\([^)]*dnd-display-app\.py", re.S)


class IsolationGuardTests(unittest.TestCase):
    def test_no_test_module_loads_the_app_directly(self):
        offenders = []
        for path in sorted(TESTS.glob("test_*.py")):
            src = path.read_text(encoding="utf-8")
            if _LOADS_APP.search(src):
                offenders.append(path.name)
        self.assertEqual(offenders, [], "load the app with tests.app_isolation.import_app / IsolatedApp")

    def test_runtime_paths_follow_the_isolated_root(self):
        home = isolate_process()
        if str(DISPLAY) not in sys.path:
            sys.path.insert(0, str(DISPLAY))
        import runtime_paths
        self.assertTrue(runtime_paths.rt("stats.json").startswith(str(home)))
        real = os.path.join(os.path.expanduser("~"), ".claude", "dnd")
        self.assertFalse(runtime_paths.rt("stats.json").startswith(real))


if __name__ == "__main__":
    unittest.main()
