"""textguard.py — free text in the event log is data, never instructions.

Every string that goes into the log passes clean(): control characters and
invisible formatting characters (bidi overrides, zero-width marks) are removed,
line breaks become spaces, and the length is capped. Reading the log back for
Claude happens inside a frame (game.py log), the same way player text is framed.
"""

import unicodedata

NAME_MAX = 64      # names: characters, conditions, spells, keys
TEXT_MAX = 200     # free text: why, note
ITEM_MAX = 240     # inventory lines ("Einbrecherpaket (Rucksack, 1000 Kugellager, …)")
LONG_FIELDS = {"why": TEXT_MAX, "note": TEXT_MAX, "item": ITEM_MAX, "inventory": ITEM_MAX}


def clean(value: str, limit: int = NAME_MAX) -> str:
    """Return `value` without control/format characters, on one line, at most `limit` chars."""
    text = str(value)
    text = text.replace("\r\n", " ").replace("\r", " ").replace("\n", " ").replace("\t", " ")
    text = "".join(ch for ch in text if unicodedata.category(ch) not in ("Cc", "Cf", "Co", "Cs"))
    text = " ".join(text.split())
    if len(text) > limit:
        text = text[: limit - 1].rstrip() + "…"
    return text


def clean_data(data, field: str = "", limit: "int | None" = None):
    """Clean every string inside `data`; keys are cleaned as names.

    A long field (why, note, item, inventory) sets the cap for everything below it.
    """
    if limit is None and field in LONG_FIELDS:
        limit = LONG_FIELDS[field]
    if isinstance(data, str):
        return clean(data, limit or NAME_MAX)
    if isinstance(data, dict):
        return {clean(str(k)): clean_data(v, str(k), limit) for k, v in data.items()}
    if isinstance(data, (list, tuple)):
        return [clean_data(v, field, limit) for v in data]
    return data
