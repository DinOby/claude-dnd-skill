"""Tests for display/triggers.py and audio.py's use of it.

The legacy test pins the refactor: every shipped SFX pack must compile to
exactly the regexes the pre-triggers.py code produced, so moving the matcher
out of audio.py cannot change which sounds fire.
"""

from __future__ import annotations

import importlib.util
import re
import sys
import unittest
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


triggers = _load_module(DISPLAY / "triggers.py", "triggers_under_test")
audio = _load_module(DISPLAY / "audio.py", "audio_under_test")


def _legacy_compile(triggers_list, is_unspaced):
    """Verbatim copy of audio._compile_trigger_list before the extraction."""
    parts = []
    for t in triggers_list:
        t = t.strip()
        if not t:
            continue
        if is_unspaced:
            parts.append(re.escape(t))
        elif t.endswith(" +"):
            word = re.escape(t[:-2].strip())
            parts.append(r"\b" + word + r"\s+\w+")
        elif " " in t:
            words = [re.escape(w) for w in t.split()]
            parts.append(r"\b" + r"\s+".join(words) + r"\b")
        else:
            parts.append(r"\b" + re.escape(t) + r"\b")
    return "|".join(parts)


def _matcher(pack, lang="de"):
    return triggers.TriggerMatcher({lang: pack}, languages=[lang])


class PhraseSyntaxTests(unittest.TestCase):
    def test_whole_word_case_insensitive(self):
        m = _matcher({"steal": ["stiehlt"]})
        self.assertEqual(m.first_match("Flerb STIEHLT den Beutel."), "steal")
        self.assertIsNone(m.first_match("Der Diebstiehltrick"))

    def test_phrase_allows_any_whitespace(self):
        m = _matcher({"attack": ["greift an"]})
        self.assertEqual(m.first_match("Der Ork greift\n an!"), "attack")
        self.assertIsNone(m.first_match("Der Ork greift sie an"))

    def test_follow_any_word(self):
        m = _matcher({"spell": ["wirkt +"]})
        self.assertEqual(m.first_match("Mira wirkt Feuerball"), "spell")
        self.assertIsNone(m.first_match("Das wirkt."))

    def test_prefix_word(self):
        m = _matcher({"attack": ["schlag*"]})
        for hit in ("Schlag", "schlagen", "ein Schlaghagel"):
            self.assertEqual(m.first_match(hit), "attack", hit)
        # Prefix, not substring — and an umlaut is a different stem.
        self.assertIsNone(m.first_match("Rückschlag"))
        self.assertIsNone(m.first_match("er schlägt"))

    def test_prefix_inside_phrase(self):
        m = _matcher({"attack": ["greif* an"]})
        self.assertEqual(m.first_match("Sie greifen an"), "attack")
        self.assertEqual(m.first_match("Er greift an"), "attack")

    def test_prefix_with_follow_any(self):
        m = _matcher({"spell": ["wirk* +"]})
        self.assertEqual(m.first_match("Sie wirken Magie"), "spell")

    def test_unspaced_substring_and_star_ignored(self):
        m = triggers.TriggerMatcher({"zh": {"fire": ["火焰*"]}}, languages=["zh"])
        self.assertEqual(m.first_match("一团火焰升起"), "fire")

    def test_unusable_phrases_are_skipped(self):
        for bad in ("", "   ", "*", "a ** b"):
            self.assertEqual(triggers.compile_phrase(bad, False), "", repr(bad))
        m = _matcher({"x": ["", "*"], "y": ["ok"]})
        self.assertEqual([e for _, _, e in m.debug_patterns()], ["y"])

    def test_regex_metacharacters_are_literal(self):
        m = _matcher({"x": ["a.b"]})
        self.assertIsNone(m.first_match("axb"))
        self.assertEqual(m.first_match("a.b"), "x")


class MatcherTests(unittest.TestCase):
    PACKS = {
        "de": {"attack": ["greift an"], "steal": ["stiehlt"]},
        "en": {"steal": ["steals"], "heal": ["heals"]},
    }

    def test_language_order_decides_priority(self):
        m = triggers.TriggerMatcher(self.PACKS, languages=["en", "de"])
        self.assertEqual(m.first_match("greift an and heals"), "heal")
        m.set_languages(["de", "en"])
        self.assertEqual(m.first_match("greift an and heals"), "attack")

    def test_unknown_languages_dropped(self):
        m = triggers.TriggerMatcher(self.PACKS, languages=["xx", "de", " "])
        self.assertEqual(m.languages, ["de"])
        self.assertEqual(m.available_languages(), ["de", "en"])

    def test_inactive_language_does_not_match(self):
        m = triggers.TriggerMatcher(self.PACKS, languages=["de"])
        self.assertIsNone(m.first_match("she heals"))

    def test_all_matches_distinct_in_order(self):
        m = triggers.TriggerMatcher(self.PACKS, languages=["de", "en"])
        self.assertEqual(m.all_matches("stiehlt, greift an, steals"), ["attack", "steal"])


class AudioCompatTests(unittest.TestCase):
    def tearDown(self):
        audio.set_sfx_languages(["en"])

    def test_every_pack_compiles_like_before(self):
        for lang in audio.available_languages():
            unspaced = lang in audio._UNSPACED_LANGS
            flags = re.UNICODE if unspaced else (re.IGNORECASE | re.UNICODE)
            expected = [
                (re.compile(_legacy_compile(phrases, unspaced), flags).pattern,
                 int(re.compile("", flags).flags), name)
                for name, phrases in audio._SFX_TRIGGERS[lang].items()
                if _legacy_compile(phrases, unspaced)
            ]
            audio.set_sfx_languages([lang])
            self.assertEqual(audio._matcher.debug_patterns(), expected, lang)

    def test_on_text_broadcasts_first_match(self):
        sent = []
        audio.set_broadcast(sent.append)
        audio.set_sfx(True)
        try:
            audio.set_sfx_languages(["de"])
            audio.on_text("Die Tür knarrt, dann lodert eine Flamme auf.")
            audio.on_text("Stille.")
        finally:
            audio.set_sfx(False)
            audio.set_broadcast(None)
        self.assertEqual(sent, [{"sfx": "door"}])

    def test_on_text_silent_when_disabled(self):
        sent = []
        audio.set_broadcast(sent.append)
        try:
            audio.on_text("sword and fire")
        finally:
            audio.set_broadcast(None)
        self.assertEqual(sent, [])


if __name__ == "__main__":
    unittest.main()
