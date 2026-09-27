"""item_categories.py — guess an item's placeholder category from its name.

Order:
  1. Exact SRD match (equipment / magic items) via the "srd" table, e.g.
     "Bag of Holding" → Wondrous Items → wondrous.
  2. Keywords from config/item-categories.json. Names are transliterated
     (ä→ae, ß→ss) and split into words; a keyword matches a word that equals
     it or ENDS with it, because German compounds put the head noun last:
     "Flammenschwert" → schwert → weapon, while "Schwertscheide" does not.
     When several keywords match, the longest wins ("kampfstab" beats "stab").
     A keyword written as "=orb" only matches the whole word — for short
     English words that would otherwise end German ones ("Weidenkorb").
  3. None — the caller falls back to "gear".

Used as AssetStore.categorize; also decides which categories go on the
image wait-list (queue_categories).
"""

import json
import os
import re
import sys
from typing import Optional

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)
from asset_store import ITEM_CATEGORIES, base_name, slugify
from config_loader import ConfigFile, check_types

DEFAULT_SRD_FILE = os.path.join(_HERE, os.pardir, "data", "dnd5e_srd.json")


def _validate(cfg: dict) -> "list[str]":
    problems = check_types(cfg, {"version": int, "srd": dict, "keywords": dict,
                                 "queue_categories": list})
    if problems:
        return problems
    for srd_cat, cat in cfg["srd"].items():
        if cat not in ITEM_CATEGORIES:
            problems.append(f"srd.{srd_cat}: unknown category '{cat}'")
    for cat, words in cfg["keywords"].items():
        if cat not in ITEM_CATEGORIES:
            problems.append(f"keywords: unknown category '{cat}'")
        if not isinstance(words, list) or not all(isinstance(w, str) for w in words):
            problems.append(f"keywords.{cat} should be a list of strings")
    for cat in cfg["queue_categories"]:
        if cat not in ITEM_CATEGORIES:
            problems.append(f"queue_categories: unknown category '{cat}'")
    return problems


def _words(name: str) -> "list[str]":
    return [w for w in slugify(base_name("item", name), max_len=200).split("-") if w]


class ItemCategorizer:
    def __init__(self, config: Optional[ConfigFile] = None, srd_file: Optional[str] = DEFAULT_SRD_FILE):
        self._config = config or ConfigFile("item-categories.json", validator=_validate)
        self._srd_file = srd_file
        self._srd_index: "Optional[dict[str, str]]" = None   # slug → SRD category label
        self._kw_gen = -1
        self._keywords: "list[tuple[str, str, bool]]" = []     # (slug, category, whole word), longest first

    # ── SRD ──
    def _srd(self) -> "dict[str, str]":
        if self._srd_index is None:
            index: "dict[str, str]" = {}
            try:
                with open(self._srd_file, encoding="utf-8") as f:
                    data = json.load(f)
                for section in ("equipment", "magic_items"):
                    for rec in data.get(section, []):
                        if isinstance(rec, dict) and rec.get("name") and rec.get("category"):
                            index.setdefault(slugify(rec["name"]), str(rec["category"]))
            except (OSError, ValueError, TypeError):
                pass
            self._srd_index = index
        return self._srd_index

    # ── Keywords ──
    def _keyword_table(self, cfg: dict) -> "list[tuple[str, str, bool]]":
        if self._kw_gen != self._config.generation:
            table = []
            for cat, words in cfg.get("keywords", {}).items():
                for w in words:
                    whole = w.startswith("=")
                    slug = slugify(w.lstrip("=")).replace("-", "")
                    if slug:
                        table.append((slug, cat, whole))
            table.sort(key=lambda t: -len(t[0]))
            self._keywords, self._kw_gen = table, self._config.generation
        return self._keywords

    def categorize(self, kind: str, name: str) -> Optional[str]:
        if kind != "item" or not name:
            return None
        cfg = self._config.get()
        srd_label = self._srd().get(slugify(base_name("item", name)))
        if srd_label:
            cat = cfg.get("srd", {}).get(srd_label)
            if cat in ITEM_CATEGORIES:
                return cat
        words = _words(name)
        for keyword, cat, whole in self._keyword_table(cfg):
            if any(w == keyword or (not whole and w.endswith(keyword)) for w in words):
                return cat
        return None

    __call__ = categorize

    def queue_categories(self) -> "set[str]":
        return set(self._config.get().get("queue_categories", []))
