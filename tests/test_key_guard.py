"""The key/network guard itself (tests/key_guard.py)."""

from __future__ import annotations

import os
import socket
import sys
import unittest
from pathlib import Path

from tests import key_guard
from tests.app_isolation import DISPLAY

if str(DISPLAY) not in sys.path:
    sys.path.insert(0, str(DISPLAY))
import tts                                     # noqa: E402
from image_providers import ImageRequest       # noqa: E402
from image_providers.gemini import GeminiProvider  # noqa: E402

KEY_VARS = ("GEMINI_API_KEY", "DND_TTS_KEY", "DND_IMAGE_KEY", "GOOGLE_API_KEY", "ANTHROPIC_API_KEY")


def run_inner(test_method) -> unittest.TestResult:
    """Run one throwaway test through the guarded TestCase.run and return its result."""
    case = type("Inner", (unittest.TestCase,), {"test_it": test_method})("test_it")
    result = unittest.TestResult()
    case.run(result)
    return result


class KeyGuardTests(unittest.TestCase):
    def test_no_key_variables_in_tests(self):
        for var in KEY_VARS:
            self.assertNotIn(var, os.environ, var)

    def test_keys_are_stripped_before_every_test(self):
        os.environ["GEMINI_API_KEY"] = "AQ.a-key-a-test-set"
        os.environ["DND_TTS_KEY"] = "another"
        seen = {}
        result = run_inner(lambda self: seen.update({v: os.environ.get(v) for v in KEY_VARS}))
        self.assertTrue(result.wasSuccessful())
        self.assertEqual(set(seen.values()), {None})

    def test_key_files_are_looked_up_in_an_empty_home(self):
        fake_home = Path(os.environ["USERPROFILE" if os.name == "nt" else "HOME"])
        self.assertNotEqual(fake_home, Path(key_guard.original_home))
        self.assertEqual(tts.KEY_FILE, fake_home / ".config" / "claude-dnd" / "tts.key")
        self.assertFalse(tts.KEY_FILE.exists())
        self.assertEqual(tts.key_source(), "unset")

    def test_reaching_gemini_fails_the_test_even_if_the_code_swallows_it(self):
        def narrate(self):
            os.environ["DND_TTS_KEY"] = "fake-key"        # a key, so tts really tries the network
            self.assertIsNone(tts.synthesize("Hallo"))     # tts turns the error into None
        result = run_inner(narrate)
        self.assertEqual(len(result.failures), 1)
        self.assertIn("tried to reach Gemini", result.failures[0][1])
        self.assertIn("generativelanguage.googleapis.com", result.failures[0][1])

    def test_image_provider_is_blocked_too(self):
        def draw(self):
            prov = GeminiProvider({"api_key_env": ["DND_IMAGE_KEY"], "key_files": []})
            os.environ["DND_IMAGE_KEY"] = "fake-key"
            with self.assertRaises(Exception):
                prov.generate(ImageRequest(prompt="ein Fass", kind="item", width=64, height=64))
        result = run_inner(draw)
        self.assertEqual(len(result.failures), 1)
        self.assertIn("tried to reach Gemini", result.failures[0][1])

    def test_other_hosts_are_untouched(self):
        socket.getaddrinfo("localhost", 80)
        self.assertEqual(key_guard.attempts, [])


if __name__ == "__main__":
    unittest.main()
