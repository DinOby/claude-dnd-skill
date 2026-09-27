"""asset_seed.py — catalogue of campaign-independent images made ahead of play.

The wait-list (asset_queue.py) only learns about content when it shows up in
a campaign. Standard content — SRD equipment, archetype portraits, map
backgrounds for recurring scene types — is known in advance, so
`scripts/assets.py seed` can generate it once and every campaign reuses it.

The catalogue is plugin config (config/asset-seed.json, override in
<data-root>/config/), not campaign data:

    {"version": 1,
     "items":     {"langschwert": {"name": "Langschwert", "category": "weapon",
                                   "srd": "Longsword", "aliases": ["Longsword"],
                                   "prompt": "a straight double-edged steel longsword …"}},
     "portraits": {"goblin": {"name": "Goblin", "category": "enemy", "prompt": "…"}},
     "maps":      {"taverne": {"name": "Taverne", "category": "map",
                               "template": "tavern-small", "prompt": "…"}}}

Sets are objects keyed by slug so an override can add or remove (null) single
entries. The slug must be the key slug of `name`, so the generic key is the
one the display derives from the name anyway (item:langschwert). Seeded
manifest entries carry "generic": true; anything specific written for the
same key later replaces them, a seed never replaces anything.
"""

import os
import sys
from typing import Iterable, Optional

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)
from asset_store import DEFAULT_CATEGORY, ITEM_CATEGORIES, TOKEN_CATEGORIES, make_key
from config_loader import ConfigFile, check_types

# set name (CLI --category) → asset kind
SETS = {"items": "item", "portraits": "token", "maps": "map"}
_SET_CATEGORIES = {"items": ITEM_CATEGORIES, "portraits": TOKEN_CATEGORIES, "maps": ("map",)}


def validate_seed_config(cfg: dict) -> "list[str]":
    problems = check_types(cfg, {"version": int, **{s: dict for s in SETS}})
    if problems:
        return problems
    for set_name, kind in SETS.items():
        aliases: "dict[str, str]" = {}
        for slug, entry in cfg[set_name].items():
            where = f"{set_name}.{slug}"
            if not isinstance(entry, dict):
                problems.append(f"{where} should be an object")
                continue
            name, prompt = entry.get("name"), entry.get("prompt")
            if not isinstance(name, str) or not name.strip():
                problems.append(f"{where}.name is required")
                continue
            if make_key(kind, name) != f"{kind}:{slug}":
                problems.append(f"{where}: slug should be '{make_key(kind, name).partition(':')[2]}' for name '{name}'")
            if not isinstance(prompt, str) or not prompt.strip():
                problems.append(f"{where}.prompt is required")
            if entry.get("category", DEFAULT_CATEGORY[kind]) not in _SET_CATEGORIES[set_name]:
                problems.append(f"{where}: unknown category '{entry.get('category')}'")
            for field in ("srd", "template"):
                if field in entry and not isinstance(entry[field], str):
                    problems.append(f"{where}.{field} should be a string")
            alias_list = entry.get("aliases", [])
            if not isinstance(alias_list, list) or not all(isinstance(a, str) for a in alias_list):
                problems.append(f"{where}.aliases should be a list of strings")
                continue
            for alias in alias_list:
                akey = make_key(kind, alias)
                if akey in aliases and aliases[akey] != slug:
                    problems.append(f"{where}: alias '{alias}' is already used by {set_name}.{aliases[akey]}")
                aliases[akey] = slug
    return problems


def seed_config() -> ConfigFile:
    return ConfigFile("asset-seed.json", validator=validate_seed_config)


class SeedCatalog:
    def __init__(self, config: Optional[ConfigFile] = None):
        self.config = config or seed_config()

    def entries(self, sets: Optional[Iterable[str]] = None) -> "list[dict]":
        """Catalogue entries in file order, shaped like wait-list entries.

        Each: {key, kind, set, name, category, prompt, aliases, srd, template}.
        """
        cfg = self.config.get()
        wanted = list(sets) if sets else list(SETS)
        unknown = [s for s in wanted if s not in SETS]
        if unknown:
            raise ValueError(f"unknown seed set(s): {', '.join(unknown)} (use {', '.join(SETS)})")
        out = []
        for set_name in wanted:
            kind = SETS[set_name]
            for slug, entry in (cfg.get(set_name) or {}).items():
                out.append({"key": f"{kind}:{slug}", "kind": kind, "set": set_name,
                            "name": entry["name"],
                            "category": entry.get("category", DEFAULT_CATEGORY[kind]),
                            "prompt": entry["prompt"], "aliases": list(entry.get("aliases", [])),
                            "srd": entry.get("srd"), "template": entry.get("template")})
        return out
