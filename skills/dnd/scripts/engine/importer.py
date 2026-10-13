"""importer.py — S2: take the sheets into the engine, and compare the two afterwards.

import: read characters/*.md and tracker.json → a dry-run table. Only with
yes=True: back the campaign up, append one `import` event carrying the values,
rebuild game/state.json from the log.

compare (abgleich): the snapshot rebuilt from the log against what the sheets
and tracker.json say now. Reports, never corrects — in shadow mode Markdown and
tracker.json stay authoritative.
"""

import json
import pathlib
import re
import shutil
from datetime import datetime
from typing import Optional

from engine import eventlog, sheet, state as st

SHEET_FIELDS = ("level", "xp", "xp_next", "hp", "hp_max", "temp_hp", "ac", "hit_dice",
                "death_saves", "spell_slots", "inventory", "currency", "inspiration", "resources")

ROWS = [  # (label, getter) for the dry-run table and the comparison
    ("Stufe", lambda f: f.get("level")),
    ("EP", lambda f: _pair(f.get("xp"), f.get("xp_next"))),
    ("TP", lambda f: _pair(f.get("hp"), f.get("hp_max"))),
    ("Temporäre TP", lambda f: f.get("temp_hp")),
    ("RK", lambda f: f.get("ac")),
    ("Trefferwürfel", lambda f: _hd(f.get("hit_dice"))),
    ("Todesrettungswürfe", lambda f: _ds(f.get("death_saves"))),
    ("Zauberplätze", lambda f: _slots(f.get("spell_slots"))),
    ("Münzen", lambda f: _coins(f.get("currency"))),
    ("Inspiration", lambda f: "ja" if f.get("inspiration") else "nein"),
    ("Begrenzte Fähigkeiten", lambda f: _res(f.get("resources"))),
    ("Zustände", lambda f: ", ".join(f.get("conditions") or []) or "keine"),
    ("Konzentration", lambda f: f.get("concentration") or "keine"),
    ("Effekte", lambda f: ", ".join(e["name"] for e in f.get("effects") or []) or "keine"),
    ("Inventar (Einträge)", lambda f: sum(len(v) for v in (f.get("inventory") or {}).values())),
]


def _pair(a, b):
    return "–" if a is None else f"{a} / {b if b is not None else '–'}"


def _hd(hd):
    return "–" if not hd else f"{hd['remaining']} von {hd['total']}{hd['die']}"


def _ds(ds):
    if not ds:
        return "–"
    return f"{ds['successes']}✓ {ds['failures']}✗" + (" stabil" if ds.get("stable") else "")


def _slots(slots):
    if not slots:
        return "keine"
    return ", ".join(f"Grad {k}: {v['max'] - v['used']}/{v['max']}" for k, v in sorted(slots.items()))


def _coins(c):
    if not c:
        return "–"
    names = (("pp", "PM"), ("gp", "GM"), ("ep", "EM"), ("sp", "SM"), ("cp", "KM"))
    return " ".join(f"{c[k]} {label}" for k, label in names if c.get(k)) or "0"


def _res(res):
    if not res:
        return "keine"
    parts = []
    for name, v in res.items():
        short = name.split(" (")[0]
        parts.append(f"{short} {v['left']}/{v['max']}" if isinstance(v, dict) else f"{short} {v}")
    return ", ".join(parts)


# ── reading the Markdown side ───────────────────────────────────────────────

def _tracker(campaign_dir: pathlib.Path) -> dict:
    try:
        data = json.loads((campaign_dir / "tracker.json").read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _tracker_entry(tracker: dict, name: str) -> Optional[dict]:
    low = name.lower()
    if low in tracker:
        return tracker[low]
    first = [k for k in tracker if k == low.split()[0]]
    return tracker[first[0]] if len(first) == 1 else None


def collect(campaign_dir) -> "tuple[dict, list[str]]":
    """Characters as the sheets + tracker.json describe them now, and hints for the host."""
    campaign_dir = pathlib.Path(campaign_dir)
    tracker = _tracker(campaign_dir)
    characters, hints = {}, []
    for path in sorted((campaign_dir / "characters").glob("*.md")):
        text = path.read_text(encoding="utf-8")
        values = sheet.parse(text)
        name = values.get("name") or path.stem
        fig = st.blank_figure(name)
        for key in SHEET_FIELDS:
            if values.get(key) is not None:
                fig[key] = values[key]
        entry = _tracker_entry(tracker, name)
        if entry:
            fig["conditions"] = list(entry.get("conditions") or [])
            fig["concentration"] = entry.get("concentration")
            fig["effects"] = [{"name": e.get("name"), "duration": e.get("duration_type"),
                               "conc": bool(e.get("concentration"))} for e in entry.get("effects") or []]
            if entry.get("death_saves"):
                fig["death_saves"] = dict(entry["death_saves"])
        characters[name] = fig
        hints.extend(f"{name}: {line}" for line in _note_hints(text))
    return characters, hints


def _note_hints(text: str) -> "list[str]":
    """Lines under '## Notes…' that name numbers the table might contradict (uses, temp HP)."""
    out = []
    for m in re.finditer(r"^##[ \t]+Notes[^\n]*\n(.*?)(?=^##[ \t]|\Z)", text, re.MULTILINE | re.DOTALL):
        for line in m.group(1).splitlines():
            line = line.strip().lstrip("- ").strip()
            if re.search(r"\d+\s*/\s*\d+.*(genutzt|used|verbraucht)|temp(oräre)?\s*(TP|HP)", line, re.IGNORECASE):
                out.append(line)
    return out


# ── output ──────────────────────────────────────────────────────────────────

def table(characters: dict) -> str:
    names = list(characters)
    head = "| Wert | " + " | ".join(names) + " |"
    sep = "|---|" + "---|" * len(names)
    lines = [head, sep]
    for label, get in ROWS:
        lines.append(f"| {label} | " + " | ".join(str(get(characters[n])) for n in names) + " |")
    return "\n".join(lines)


# ── import ──────────────────────────────────────────────────────────────────

def backup(campaign_dir, runtime_dir=None, now: Optional[datetime] = None) -> pathlib.Path:
    """Copy the whole campaign (plus the display's stats.json) next to it; verify the copy."""
    campaign_dir = pathlib.Path(campaign_dir)
    stamp = (now or datetime.now()).strftime("%Y%m%d-%H%M%S")
    target = campaign_dir.parent / f"{campaign_dir.name}.sicherung-vor-engine-{stamp}"
    n = 1
    while target.exists():                         # two imports in one second: never overwrite a backup
        n += 1
        target = campaign_dir.parent / f"{campaign_dir.name}.sicherung-vor-engine-{stamp}-{n}"
    shutil.copytree(campaign_dir, target)
    if runtime_dir is not None:
        stats = pathlib.Path(runtime_dir) / "stats.json"
        if stats.is_file():
            shutil.copy2(stats, target / "_display-stats.json")
    for src in campaign_dir.rglob("*"):
        if src.is_file():
            copy = target / src.relative_to(campaign_dir)
            if not copy.is_file() or copy.stat().st_size != src.stat().st_size:
                raise OSError(f"Sicherung unvollständig: {copy}")
    return target


def run_import(campaign_dir, characters: dict) -> dict:
    """Append the import event and rebuild state.json. Returns the new snapshot."""
    eventlog.append(campaign_dir, "import", {"source": "Charakterbögen + tracker.json",
                                             "characters": characters}, src="game")
    return rebuild(campaign_dir)


def rebuild(campaign_dir) -> dict:
    events, _ = eventlog.read(campaign_dir)
    snapshot = st.replay(events)
    st.write(campaign_dir, snapshot)
    return snapshot


# ── comparison ──────────────────────────────────────────────────────────────

SHEET_SIDE_SOURCES = ("game", "bogen", "xp", "tracker")
"""Writers that change the Markdown side (sheets, tracker.json) and log it in the same step.
The display paths (push_stats, send) only change the display, never a sheet."""

HAND = "von Hand geändert"
NOT_FOLLOWED = "Bogen nicht nachgezogen"


def sheet_baseline(events: "list[dict]") -> dict:
    """What the sheets should say if only logged writers touched them: the last import plus
    every later event from a writer that also changes the Markdown side."""
    return st.replay([e for e in events if e["src"] in SHEET_SIDE_SOURCES or e["type"] == "import"])


def _items(fig: dict) -> "list[str]":
    return sorted(i.lower() for items in (fig.get("inventory") or {}).values() for i in items)


def compare(engine: dict, markdown: dict, baseline: Optional[dict] = None) -> "list[tuple]":
    """(character, value, engine says, Markdown says[, cause]) for every difference.

    With a baseline (sheet_baseline()['characters']) each difference gets a cause:
    "von Hand geändert" — the sheet no longer says what the logged writers left there;
    "Bogen nicht nachgezogen" — the sheet is as logged, the engine moved on (a display
    change that was not written into the sheet).
    """
    diffs = []

    def cause(base_value, sheet_value, base_fig) -> "tuple":
        if baseline is None:
            return ()
        if base_fig is None:
            return (HAND,)
        return (HAND if base_value != sheet_value else NOT_FOLLOWED,)

    for name in sorted(set(engine) | set(markdown)):
        e, m = engine.get(name), markdown.get(name)
        if e is None:
            diffs.append((name, "Figur", "fehlt", "vorhanden") + ((HAND,) if baseline is not None else ()))
            continue
        if m is None:
            diffs.append((name, "Figur", "vorhanden", "fehlt") + ((HAND,) if baseline is not None else ()))
            continue
        base = (baseline or {}).get(name)
        for label, get in ROWS:
            if label == "Inventar (Einträge)":
                continue
            a, b = get(e), get(m)
            if a != b:
                diffs.append((name, label, str(a), str(b)) + cause(get(base) if base else None, b, base))
        inv_e, inv_m = _items(e), _items(m)
        if inv_e != inv_m:
            only_e = len([i for i in inv_e if i not in inv_m])
            only_m = len([i for i in inv_m if i not in inv_e])
            diffs.append((name, "Inventar", f"{only_e} nur in Engine", f"{only_m} nur im Bogen")
                         + cause(_items(base) if base else None, inv_m, base))
    return diffs
