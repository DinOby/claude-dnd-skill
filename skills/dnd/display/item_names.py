"""item_names.py — find the rules entry behind an inventory line as players write it.

Inventory lines on a German sheet look like "4 Handäxte", "Handaxt (4)",
"Kurzbogen + 20 Pfeile", "2x Diebeswerkzeug" or "Einbrecherpaket". The SRD
dataset only knows "Handaxe", "Shortbow", "Thieves' Tools", "Burglar's Pack".

  candidates(text)        → the names worth looking up, most specific first
  german_aliases(config)  → {german name (casefolded): SRD name}, built from the
                            image catalogue (config/asset-seed.json: name → srd)
                            plus config/item-aliases.json (packs, tools, clothes)
  campaign_items(path)    → {name (casefolded): (name, description)} from a
                            campaign's items.md ("## Name" + paragraph) for
                            things only this campaign has (a debt ledger, a badge)
"""
from __future__ import annotations

import json
import os
import re
from typing import Optional


def _variants(name: str) -> "list[str]":
    """The name plus a few plural/umlaut forms ("Handäxte" → "Handaxt")."""
    out = [name]
    for a, b in (("äxte", "axt"), ("äxt", "axt"), ("ten", "t"), ("en", ""), ("e", ""), ("n", ""), ("s", "")):
        if name.endswith(a) and len(name) > len(a) + 3:
            out.append(name[: -len(a)] + b)
    return out


def candidates(text: str) -> "list[str]":
    """Names to try for one inventory line, most specific first, no duplicates."""
    raw = (text or "").strip()
    if not raw:
        return []
    found: "list[str]" = [raw]
    s = raw
    s = re.split(r"\s+\+\s+|\s*,\s*", s)[0]                    # "Kurzbogen + 20 Pfeile" → "Kurzbogen"
    s = re.sub(r"^\s*\d+\s*[x×]?\s+", "", s)                     # "4 Handäxte", "2x Diebeswerkzeug"
    s = re.sub(r"\s*[x×]\s*\d+\s*$", "", s)                      # "Handaxt ×4"
    inner = re.findall(r"\(([^)]*)\)", s)
    s_plain = re.sub(r"\s*\([^)]*\)", "", s).strip()             # "Zweihandaxt (Greataxe)" → "Zweihandaxt"
    for v in [s, s_plain] + [i.strip() for i in inner if i.strip() and not i.strip().isdigit()]:
        for w in _variants(v):
            if w and w not in found:
                found.append(w)
    return found


def german_aliases(config_dir: str) -> "dict[str, str]":
    """German item name (casefolded) → SRD name."""
    out: "dict[str, str]" = {}
    seed = _read_json(os.path.join(config_dir, "asset-seed.json")) or {}
    for entry in (seed.get("items") or {}).values():
        if isinstance(entry, dict) and entry.get("srd") and entry.get("name"):
            out[str(entry["name"]).casefold()] = str(entry["srd"])
    extra = _read_json(os.path.join(config_dir, "item-aliases.json")) or {}
    for german, srd in (extra.get("aliases") or {}).items():
        if isinstance(german, str) and isinstance(srd, str):
            out[german.casefold()] = srd
    return out


def rules_aliases(config_dir: str) -> "dict[str, dict[str, str]]":
    """German spell / feature / condition names (casefolded) → SRD name, per
    dataset category ("spells", "features", "conditions") from rules-aliases.json."""
    out: "dict[str, dict[str, str]]" = {"spells": {}, "features": {}, "conditions": {}}
    data = _read_json(os.path.join(config_dir, "rules-aliases.json")) or {}
    for section in out:
        for german, srd in (data.get(section) or {}).items():
            if isinstance(german, str) and isinstance(srd, str):
                out[section][german.casefold()] = srd
    return out


def campaign_items(path: str) -> "dict[str, tuple[str, str]]":
    """items.md → {name casefolded: (name, description)}."""
    try:
        with open(path, encoding="utf-8") as f:
            text = f.read()
    except OSError:
        return {}
    out: "dict[str, tuple[str, str]]" = {}
    for block in re.split(r"(?m)^##\s+", text)[1:]:
        head, _, body = block.partition("\n")
        name = head.strip()
        if name:
            out[name.casefold()] = (name, body.strip())
    return out


def resolve(text: str, aliases: "dict[str, str]",
            items: "Optional[dict[str, tuple[str, str]]]" = None) -> "list[tuple[str, str]]":
    """[(kind, value)] to try in order: ("campaign", name) for a campaign item,
    ("srd", name) for a rules lookup."""
    out: "list[tuple[str, str]]" = []
    seen = set()
    for cand in candidates(text):
        key = cand.casefold()
        if items and key in items and ("campaign", key) not in seen:
            out.append(("campaign", key))
            seen.add(("campaign", key))
        for value in ([aliases[key]] if key in aliases else []) + [cand]:
            if ("srd", value.casefold()) not in seen:
                out.append(("srd", value))
                seen.add(("srd", value.casefold()))
    return out


def _read_json(path: str) -> Optional[dict]:
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None
