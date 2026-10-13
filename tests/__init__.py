"""Test package. Importing it arms tests/key_guard.py: no real API keys, no Gemini traffic."""

from tests import key_guard

key_guard.install()
