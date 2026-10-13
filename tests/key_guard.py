"""Tests never use real API keys and never reach Gemini.

Armed by tests/__init__.py as soon as the test package is imported (unittest
discover and pytest both import it before any test module):

- Key variables (GEMINI_API_KEY, DND_TTS_KEY, DND_IMAGE_KEY, GOOGLE_API_KEY …)
  are removed from the environment once at start and again before every test.
- HOME / USERPROFILE point at an empty temp dir, so key files such as
  ~/.config/claude-dnd/tts.key are not found either (subprocesses inherit it).
- Any name lookup or connection to *.googleapis.com is refused and recorded;
  a test during which that happened fails, even when the code under test
  swallowed the error (the TTS and image paths turn network errors into
  quiet fallbacks).

A test that needs Gemini behaviour mocks it (tts.synthesize_with_usage) or
points the provider at a local fake server (base_url), as the existing tests do.
"""

from __future__ import annotations

import atexit
import os
import re
import shutil
import socket
import sys
import tempfile
import unittest

_KEY_VAR = re.compile(r"^(GEMINI_.*|GOOGLE_.*KEY.*|DND_.*KEY.*|.*_API_KEY|ANTHROPIC_AUTH_TOKEN)$", re.I)
_BLOCKED = re.compile(r"(^|\.)googleapis\.com\.?$", re.I)

attempts: "list[str]" = []   # hosts a test tried to reach; cleared per test
original_home = os.path.expanduser("~")   # the real home, before install() replaced it


class GeminiNetworkBlocked(OSError):
    """Raised instead of a connection to Gemini during tests."""


def strip_keys() -> "list[str]":
    """Remove every key-like variable from os.environ; returns their names."""
    gone = [k for k in os.environ if _KEY_VAR.match(k)]
    for k in gone:
        os.environ.pop(k, None)
    return gone


def _check(host) -> None:
    name = host.decode() if isinstance(host, bytes) else str(host or "")
    if _BLOCKED.search(name):
        attempts.append(name)
        raise GeminiNetworkBlocked(f"test tried to reach {name} — mock Gemini instead (tests/key_guard.py)")


_installed = False


def install() -> None:
    global _installed
    if _installed:
        return
    _installed = True
    strip_keys()

    home = tempfile.mkdtemp(prefix="dnd-test-home-")
    atexit.register(shutil.rmtree, home, True)
    for var in ("HOME", "USERPROFILE"):
        os.environ[var] = home

    real_getaddrinfo, real_create_connection = socket.getaddrinfo, socket.create_connection

    def getaddrinfo(host, *args, **kwargs):
        _check(host)
        return real_getaddrinfo(host, *args, **kwargs)

    def create_connection(address, *args, **kwargs):
        _check(address[0] if isinstance(address, tuple) else address)
        return real_create_connection(address, *args, **kwargs)

    socket.getaddrinfo = getaddrinfo
    socket.create_connection = create_connection

    real_run = unittest.TestCase.run

    def run(self, result=None):
        strip_keys()
        attempts.clear()
        outcome = real_run(self, result)
        if attempts and result is not None:
            hosts = sorted(set(attempts))
            attempts.clear()
            try:
                raise AssertionError(f"{self.id()} tried to reach Gemini ({', '.join(hosts)}); "
                                     f"tests must mock it — see tests/key_guard.py")
            except AssertionError:
                result.addFailure(self, sys.exc_info())
        return outcome

    unittest.TestCase.run = run
