"""triggers.py — keyword → event matching over narration text.

Shared by audio.py (sound effects) and, later, vfx.py (action overlays). The
matcher only knows phrases and event names; what an event *does* is the
caller's business, so trigger packs can live in code or in JSON config.

Pack shape:  {lang: {event_name: [phrase, ...]}}

Phrase syntax (spaced scripts — Latin, Cyrillic, Greek, Indic, …):
  "word"         whole word, case-insensitive
  "word1 word2"  words in sequence, any whitespace between
  "word +"       the word followed by any one other word
  "stem*"        any word starting with stem — "schlag*" matches "schlagen",
                 "Schlaghand"; may be used inside phrases: "greif* an"

Unspaced scripts (zh, ja, ko, th, ar) match phrases as literal substrings,
which already behaves like a prefix match, so a trailing "*" is ignored.

Matching order is language order first, then event order within the pack;
first_match() returns the first event whose phrases hit.
"""

import re
from typing import Iterable, Optional

# Scripts without word-boundary tokens (or, for Arabic, with connected glyphs):
# no \b anchors, phrases match anywhere.
UNSPACED_LANGS = frozenset(["zh", "ja", "ko", "th", "ar"])


def _word_regex(word: str) -> str:
    if word.endswith("*"):
        stem = word.rstrip("*")
        return re.escape(stem) + r"\w*" if stem else ""
    return re.escape(word)


def compile_phrase(phrase: str, unspaced: bool) -> str:
    """Regex source for one phrase; "" when the phrase is empty/unusable."""
    t = phrase.strip()
    if not t:
        return ""
    if unspaced:
        t = t.rstrip("*").strip()
        return re.escape(t) if t else ""

    follow_any = t.endswith(" +")
    if follow_any:
        t = t[:-2].strip()
    words = [_word_regex(w) for w in t.split()]
    if not words or not all(words):
        return ""
    if follow_any:
        # Without "*" keep the historical form (phrase escaped as one string),
        # so existing packs compile byte-identically to earlier releases.
        head = re.escape(t) if "*" not in t else r"\s+".join(words)
        return r"\b" + head + r"\s+\w+"
    if len(words) == 1:
        return r"\b" + words[0] + r"\b"
    return r"\b" + r"\s+".join(words) + r"\b"


def compile_trigger_list(triggers: Iterable[str], unspaced: bool) -> str:
    """Regex alternation for a list of phrases ("" when none are usable)."""
    parts = [compile_phrase(t, unspaced) for t in triggers]
    return "|".join(p for p in parts if p)


class TriggerMatcher:
    """Compiled matcher over one set of language packs."""

    def __init__(self, packs: "dict[str, dict[str, list[str]]]",
                 languages: Iterable[str] = ("en",),
                 unspaced_langs: Iterable[str] = UNSPACED_LANGS):
        self._packs = packs
        self._unspaced = frozenset(unspaced_langs)
        self._languages: "list[str]" = []
        self._compiled: "list[tuple[re.Pattern, str]]" = []
        self.set_languages(languages)

    @property
    def languages(self) -> "list[str]":
        """Active languages, in priority order (unknown codes dropped)."""
        return list(self._languages)

    def available_languages(self) -> "list[str]":
        return sorted(self._packs)

    def set_languages(self, languages: Iterable[str]) -> "list[str]":
        """Activate packs in priority order; unknown codes are skipped."""
        wanted = [l.strip() for l in languages if l and l.strip()]
        self._languages = [l for l in wanted if l in self._packs]
        compiled = []
        for lang in self._languages:
            unspaced = lang in self._unspaced
            # Case has no meaning in unspaced scripts; elsewhere ignore it.
            flags = re.UNICODE if unspaced else (re.IGNORECASE | re.UNICODE)
            for event, phrases in self._packs[lang].items():
                source = compile_trigger_list(phrases, unspaced)
                if source:
                    compiled.append((re.compile(source, flags), event))
        self._compiled = compiled
        return self.languages

    def first_match(self, text: str) -> Optional[str]:
        for pattern, event in self._compiled:
            if pattern.search(text):
                return event
        return None

    def all_matches(self, text: str) -> "list[str]":
        """Every matching event once, in matching order."""
        seen: "list[str]" = []
        for pattern, event in self._compiled:
            if event not in seen and pattern.search(text):
                seen.append(event)
        return seen

    def debug_patterns(self) -> "list[tuple[str, int, str]]":
        """(regex source, flags, event) for every compiled pattern."""
        return [(p.pattern, int(p.flags), e) for p, e in self._compiled]
