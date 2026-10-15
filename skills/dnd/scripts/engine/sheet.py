"""sheet.py — read (and, for game.py bogen, change) the engine's numbers in a Markdown sheet.

Only the values listed in docs/plans/spielzustand-engine.md ("Werte und ihre
Quelle") are read; everything else stays Markdown. A value the sheet does not
state is left out (None), never guessed — the import table shows it as "–".

change() rewrites exactly the lines that hold those values and nothing else;
a line it cannot find is an error, never a guess.

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


_INSPIRATION_LINE = re.compile(r"^(\s*-\s+\*\*(?:Heroic\s+)?Inspiration:\*\*\s*)(\S+)", re.IGNORECASE | re.MULTILINE)


def _inspiration(text: str) -> bool:
    """The '**Heroic Inspiration:** ja|nein' line under Combat Stats when there is one
    (game.py bogen writes it); otherwise any line naming Inspiration with a check mark."""
    m = _INSPIRATION_LINE.search(_section(text, "Combat Stats"))
    if m:
        return m.group(2).strip().lower() in ("ja", "✓", "yes", "true")
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


# ── changing a sheet (game.py bogen) ────────────────────────────────────────

class SheetError(Exception):
    """A requested change has no line to go to (the message says which)."""


def _section_span(text: str, title: str) -> "tuple[int, int]":
    """(start, end) of the body of '## <title>…' in text; SheetError when missing."""
    m = re.search(rf"^##[ \t]+{re.escape(title)}[^\n]*\n", text, re.MULTILINE)
    if not m:
        raise SheetError(f"Abschnitt '## {title}' fehlt im Bogen")
    end = re.search(r"^##[ \t]", text[m.end():], re.MULTILINE)
    return m.end(), (m.end() + end.start()) if end else len(text)


def _sub_in_section(text: str, title: str, pattern: str, repl, what: str, flags=re.MULTILINE) -> str:
    start, end = _section_span(text, title)
    body, n = re.subn(pattern, repl, text[start:end], count=1, flags=flags)
    if not n:
        raise SheetError(f"{what} nicht im Abschnitt '## {title}' gefunden")
    return text[:start] + body + text[end:]


def _coins_text(coins: dict) -> str:
    parts = [f"{coins[c]}{c}" for c in ("pp", "ep") if coins.get(c)]
    parts += [f"{coins.get(c, 0)}{c}" for c in ("gp", "sp", "cp")]
    return " ".join(parts)


def _slot_row_label(level: int) -> str:
    return {1: "1st", 2: "2nd", 3: "3rd"}.get(level, f"{level}th")


def _set_slot_row(text: str, level: int, used: int, total: Optional[int]) -> str:
    """Set 'Used' (and 'Total' when given) of one spell-slot row; add the row when a new
    level is given with its total. SheetError when there is no table to write into."""
    start, end = _section_span(text, "Spell Slots")
    body = text[start:end]
    row = re.compile(rf"^(\|\s*{int(level)}(?:st|nd|rd|th|\.)?\s*\|\s*)(\d+)(\s*\|\s*)\d*(\s*\|)", re.MULTILINE)
    m = row.search(body)
    if m:
        new_total = str(total) if total is not None else m.group(2)
        body = body[:m.start()] + f"{m.group(1)}{new_total}{m.group(3)}{used}{m.group(4)}" + body[m.end():]
    else:
        if total is None:
            raise SheetError(f"Zauberplatz-Zeile Grad {level} nicht im Abschnitt '## Spell Slots' gefunden")
        rows = list(re.finditer(r"^\|.*\|[ \t]*$", body, re.MULTILINE))
        if not rows:
            raise SheetError("Im Abschnitt '## Spell Slots' steht keine Tabelle")
        pos = rows[-1].end()
        body = body[:pos] + f"\n| {_slot_row_label(int(level))} | {total} | {used} |" + body[pos:]
    return text[:start] + body + text[end:]


def change(text: str, *, hp: Optional[int] = None, hp_max: Optional[int] = None,
           temp: Optional[int] = None, currency: Optional[dict] = None,
           resources: Optional[dict] = None, inspiration: Optional[bool] = None,
           add: "tuple[str, ...]" = (), remove: "tuple[str, ...]" = (),
           hit_dice: Optional[int] = None, slots: Optional[dict] = None,
           level: Optional[int] = None, hit_dice_total: Optional[int] = None,
           xp_next: Optional[int] = None) -> str:
    """The sheet with the given values changed. Raises SheetError, never half-changes.

    slots: {level: used} or {level: (used, total)} — a total also changes the
    'Total' column (level-up) and may add a row for a new spell level.
    level / hit_dice_total / xp_next: only for game.py stufe (level-up).
    """
    if level is not None:
        text, n = re.subn(r"(\*\*Level:\*\*\s*)\d+", lambda m: f"{m.group(1)}{int(level)}", text, count=1)
        if not n:
            raise SheetError("'**Level:**' nicht im Bogen gefunden")
    if xp_next is not None:
        text, n = re.subn(r"(\*\*XP:\*\*\s*\d+\s*/\s*)\d+[^\n|]*?(?=\s*(?:\||$))",
                          lambda m: f"{m.group(1)}{int(xp_next)}", text, count=1, flags=re.MULTILINE)
        if not n:
            raise SheetError("'**XP:** … / …' nicht im Bogen gefunden")
    if hit_dice_total is not None:
        text = _sub_in_section(text, "Combat Stats", r"(\*\*Hit Dice:\*\*\s*)\d+(\s*d\d+\s*\(remaining:\s*\d+\))",
                               lambda m: f"{m.group(1)}{int(hit_dice_total)}{m.group(2)}", "Die Trefferwürfel-Zeile")
    if hp is not None or hp_max is not None:
        def _hp(m):
            cur = hp if hp is not None else int(m.group(2))
            mx = hp_max if hp_max is not None else int(m.group(3))
            return f"{m.group(1)}{cur} / {mx}"
        text = _sub_in_section(text, "Combat Stats", r"(\*\*HP:\*\*\s*)(\d+)\s*/\s*(\d+)", _hp, "Die TP-Zeile")
    if temp is not None:
        text = _sub_in_section(text, "Combat Stats", r"(\*\*Temp HP:\*\*\s*)\d+",
                               lambda m: f"{m.group(1)}{temp}", "'**Temp HP:**'")
    if hit_dice is not None:
        text = _sub_in_section(text, "Combat Stats", r"(\*\*Hit Dice:\*\*\s*\d+\s*d\d+\s*\(remaining:\s*)\d+",
                               lambda m: f"{m.group(1)}{hit_dice}", "Die Trefferwürfel-Zeile")
    if inspiration is not None:
        value = "ja" if inspiration else "nein"
        start, end = _section_span(text, "Combat Stats")
        body = text[start:end]
        if _INSPIRATION_LINE.search(body):
            body = _INSPIRATION_LINE.sub(lambda m: f"{m.group(1)}{value}", body, count=1)
        else:
            hp_line = re.search(r"^.*\*\*HP:\*\*.*$", body, re.MULTILINE)
            if not hp_line:
                raise SheetError("Die TP-Zeile fehlt – wohin mit der Inspiration?")
            body = body[:hp_line.end()] + f"\n- **Heroic Inspiration:** {value}" + body[hp_line.end():]
        text = text[:start] + body + text[end:]
    for name, (left, total) in (resources or {}).items():
        low = name.strip().lower()
        start, end = _section_span(text, "Features & Traits")
        lines = text[start:end].split("\n")
        for i, line in enumerate(lines):
            m = re.match(r"^\s*-\s+\*\*(.+?)\*\*", line)
            if not m:
                continue
            key = m.group(1).strip().lower()
            if key != low and key.split(" (")[0].strip() != low:
                continue
            spans = list(re.finditer(r"(\*\*(?:[^*\d]*?:\s*)?)(\d+)\s*/\s*(\d+)(\*\*)", line))
            if not spans:
                raise SheetError(f"'{name}' hat im Bogen keinen Zähler wie **1/2**")
            s = spans[-1]
            lines[i] = line[:s.start()] + f"{s.group(1)}{left}/{total}{s.group(4)}" + line[s.end():]
            break
        else:
            raise SheetError(f"Fähigkeit '{name}' nicht unter '## Features & Traits' gefunden")
        text = text[:start] + "\n".join(lines) + text[end:]
    for slot_level, value in (slots or {}).items():
        used, total = value if isinstance(value, tuple) else (value, None)
        text = _set_slot_row(text, int(slot_level), int(used), total)
    for item in remove:
        start, end = _section_span(text, "Equipment & Inventory")
        lines = text[start:end].split("\n")
        hit = [i for i, line in enumerate(lines)
               if re.match(r"^\s*-\s+(.+?)\s*$", line)
               and re.match(r"^\s*-\s+(.+?)\s*$", line).group(1).lower() == item.strip().lower()]
        if not hit:
            raise SheetError(f"Gegenstand '{item}' steht nicht im Inventar (genauer Wortlaut nötig)")
        del lines[hit[0]]
        text = text[:start] + "\n".join(lines) + text[end:]
    if add:
        start, end = _section_span(text, "Equipment & Inventory")
        body = text[start:end]
        new_lines = "".join(f"- {item.strip()}\n" for item in add)
        head = re.search(r"^\*\*Neu erhalten:\*\*\s*\n", body, re.MULTILINE)
        if head:
            pos = head.end()
            while pos < len(body) and re.match(r"^\s*-\s+", body[pos:]):
                pos = body.find("\n", pos) + 1 or len(body)
            body = body[:pos] + new_lines + body[pos:]
        else:
            cur = re.search(r"^\*\*Currency:\*\*", body, re.MULTILINE)
            block = f"**Neu erhalten:**\n{new_lines}\n"
            body = body[:cur.start()] + block + body[cur.start():] if cur else body.rstrip("\n") + "\n\n" + block
        text = text[:start] + body + text[end:]
    if currency is not None:
        text = _sub_in_section(text, "Equipment & Inventory", r"(\*\*Currency:\*\*)[^\n]*",
                               lambda m: f"{m.group(1)} {_coins_text(currency)}", "'**Currency:**'")
    return text
