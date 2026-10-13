"""sheet.py — read the numbers the engine takes over from a Markdown character sheet.

Only the values listed in docs/plans/spielzustand-engine.md ("Werte und ihre
Quelle") are read; everything else stays Markdown. A value the sheet does not
state is left out (None), never guessed — the import table shows it as "–".

Sheet format: templates/character-sheet.md ("## Combat Stats", "## Spell Slots",
"## Equipment & Inventory", "**Currency:** 40gp 11sp 4cp", …).
"""

import re
from typing import Optional

_ORDINAL = re.compile(r"^(\d)")


def _section(text: str, title: str) -> str:
    """Body of the first '## <title>…' section ('' when missing)."""
    m = re.search(rf"^##[ \t]+{re.escape(title)}[^\n]*\n", text, re.MULTILINE)
    if not m:
        return ""
    rest = text[m.end():]
    end = re.search(r"^##[ \t]", rest, re.MULTILINE)
    return rest[: end.start()] if end else rest


def _int(pattern: str, text: str, flags=0) -> Optional[int]:
    m = re.search(pattern, text, flags)
    return int(m.group(1)) if m else None


def parse(text: str) -> dict:
    """The engine values of one sheet; keys with unknown values are None."""
    name_m = re.search(r"^#[ \t]+(.+?)[ \t]*$", text, re.MULTILINE)
    combat = _section(text, "Combat Stats")

    hp_m = re.search(r"\*\*HP:\*\*\s*(\d+)\s*/\s*(\d+)", combat)
    hd_m = re.search(r"\*\*Hit Dice:\*\*\s*(\d+)\s*d(\d+)\s*\(remaining:\s*(\d+)\)", combat)
    xp_m = re.search(r"\*\*XP:\*\*\s*(\d+)\s*/\s*(\d+)", text)

    return {
        "name": name_m.group(1).strip() if name_m else None,
        "level": _int(r"\*\*Level:\*\*\s*(\d+)", text),
        "xp": int(xp_m.group(1)) if xp_m else None,
        "xp_next": int(xp_m.group(2)) if xp_m else None,
        "hp": int(hp_m.group(1)) if hp_m else None,
        "hp_max": int(hp_m.group(2)) if hp_m else None,
        "temp_hp": _int(r"\*\*Temp HP:\*\*\s*(\d+)", combat),
        "ac": _int(r"\*\*AC:\*\*\s*(\d+)", combat),
        "hit_dice": ({"die": f"d{hd_m.group(2)}", "total": int(hd_m.group(1)),
                      "remaining": int(hd_m.group(3))} if hd_m else None),
        "death_saves": _death_saves(combat),
        "spell_slots": _spell_slots(_section(text, "Spell Slots")),
        "inventory": _inventory(_section(text, "Equipment & Inventory")),
        "currency": _currency(_section(text, "Equipment & Inventory")),
        "inspiration": _inspiration(text),
        "resources": _resources(_section(text, "Features & Traits")),
    }


def _death_saves(combat: str) -> Optional[dict]:
    m = re.search(r"\*\*Death Saves:\*\*\s*Successes:\s*(\d)\s*\|\s*Failures:\s*(\d)", combat)
    if not m:
        return None
    return {"successes": int(m.group(1)), "failures": int(m.group(2)), "stable": False}


def _spell_slots(section: str) -> dict:
    slots = {}
    for row in re.finditer(r"^\|\s*(\d)(?:st|nd|rd|th|\.)?\s*\|\s*(\d+)\s*\|\s*(\d*)\s*\|", section, re.MULTILINE):
        slots[row.group(1)] = {"max": int(row.group(2)), "used": int(row.group(3) or 0)}
    return slots


def _inventory(section: str) -> dict:
    """{group: [line, …]} from '**Group:**' headers and '- ' bullets; currency excluded."""
    groups: dict = {}
    current = None
    for line in section.splitlines():
        head = re.match(r"^\*\*(.+?):\*\*\s*(.*)$", line.strip())
        if head:
            current = None if head.group(1).lower() == "currency" else head.group(1).strip()
            if current is not None:
                groups.setdefault(current, [])
            continue
        item = re.match(r"^\s*-\s+(.+?)\s*$", line)
        if item and current is not None:
            groups[current].append(item.group(1))
    return groups


def _currency(section: str) -> Optional[dict]:
    m = re.search(r"\*\*Currency:\*\*\s*([^\n]*)", section)
    if not m:
        return None
    coins = {"pp": 0, "gp": 0, "ep": 0, "sp": 0, "cp": 0}
    found = False
    for amount, coin in re.findall(r"(\d+)\s*(pp|gp|ep|sp|cp)\b", m.group(1)):
        coins[coin] += int(amount)
        found = True
    return coins if found else None


def _inspiration(text: str) -> bool:
    """True when a line names (Heroic) Inspiration with a check mark."""
    return any("inspiration" in line.lower() and "✓" in line for line in text.splitlines())


def _resources(section: str) -> dict:
    """Limited uses like '- **Rage (Kampfrausch)** … **Rages: 2/2**' → {'Rage (Kampfrausch)': '2/2'}."""
    out = {}
    for line in section.splitlines():
        m = re.match(r"^\s*-\s+\*\*(.+?)\*\*", line)
        if not m:
            continue
        uses = re.findall(r"\*\*(?:[^*\d]*?:\s*)?(\d+)\s*/\s*(\d+)\*\*", line)
        if uses:
            left, total = uses[-1]
            out[m.group(1).strip()] = {"left": int(left), "max": int(total)}
    return out
