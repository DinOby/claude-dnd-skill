"""lead.py — stage S3b: the engine leads hit points, conditions and spell slots.

With `engine: an` (state.md → ## Session Flags) the log is the source of these
values. game.py and tracker.py write the log first; only what was logged
reaches the sheet. Every sheet carries a generated block

    <!-- engine:start · Werte aus der Spielstand-Engine – nicht von Hand ändern (game.py) -->
    - **Engine:** TP 11 / 23 · Temp-TP 0 · Zauberplätze: keine · Zustände: keine · Konzentration: keine
    <!-- engine:end · prüfsumme 1a2b3c4d5e6f -->

and the ordinary lines other tools read (HP line, spell-slot table) are kept
equal to the engine. The checksum shows when someone edited the block by hand;
the comparison (game.py abgleich) reports that and any ordinary line that no
longer matches the engine.

Way back: game.py export writes the engine values into the sheets and
tracker.json and removes the blocks; then the flag can go to `aus`.
"""

import hashlib
import json
import pathlib
import re
from typing import Optional

from engine import eventlog, sheet, state as st

LED = ("hp", "hp_max", "temp_hp", "spell_slots", "conditions", "concentration", "death_saves")
"""Figure fields the engine leads in S3b (plan: TP, Zustände, Zauberplätze)."""

START = "<!-- engine:start · Werte aus der Spielstand-Engine – nicht von Hand ändern (game.py) -->"
_BLOCK = re.compile(r"<!-- engine:start[^\n]*-->\n(?P<body>.*?)\n<!-- engine:end · prüfsumme (?P<sum>[0-9a-f]{12}) -->\n?",
                    re.DOTALL)

OK, MISSING, EDITED = "ok", "fehlt", "von Hand geändert"


def snapshot(campaign_dir) -> Optional[dict]:
    """The engine state rebuilt from the log; None before the first import."""
    events, _ = eventlog.read(campaign_dir)
    if not any(e["type"] == "import" for e in events):
        return None
    return st.replay(events)


def _checksum(body: str) -> str:
    return hashlib.sha256(body.encode("utf-8")).hexdigest()[:12]


def _slots_text(slots: dict) -> str:
    if not slots:
        return "keine"
    return ", ".join(f"Grad {k}: {v['max'] - v['used']}/{v['max']}" for k, v in sorted(slots.items()))


def render_body(fig: dict) -> str:
    hp = "–" if fig.get("hp") is None else f"{fig['hp']} / {fig.get('hp_max')}"
    conds = ", ".join(fig.get("conditions") or []) or "keine"
    ds = fig.get("death_saves") or {}
    saves = ""
    if ds.get("successes") or ds.get("failures") or ds.get("stable"):
        saves = f" · Todesrettungswürfe {ds.get('successes', 0)}✓ {ds.get('failures', 0)}✗" + \
                (" stabil" if ds.get("stable") else "")
    return (f"- **Engine:** TP {hp} · Temp-TP {fig.get('temp_hp') or 0} · "
            f"Zauberplätze: {_slots_text(fig.get('spell_slots') or {})} · Zustände: {conds} · "
            f"Konzentration: {fig.get('concentration') or 'keine'}{saves}")


def render_block(fig: dict) -> str:
    body = render_body(fig)
    return f"{START}\n{body}\n<!-- engine:end · prüfsumme {_checksum(body)} -->\n"


def block_status(text: str) -> str:
    m = _BLOCK.search(text)
    if not m:
        return MISSING
    return OK if _checksum(m.group("body")) == m.group("sum") else EDITED


def block_body(text: str) -> Optional[str]:
    m = _BLOCK.search(text)
    return m.group("body") if m else None


def remove_block(text: str) -> str:
    return _BLOCK.sub("", text, count=1)


def put_block(text: str, fig: dict) -> str:
    """Replace the block, or insert it right under '## Combat Stats'."""
    block = render_block(fig)
    if _BLOCK.search(text):
        return _BLOCK.sub(lambda m: block, text, count=1)
    m = re.search(r"^##[ \t]+Combat Stats[^\n]*\n", text, re.MULTILINE)
    if not m:
        raise sheet.SheetError("Abschnitt '## Combat Stats' fehlt – wohin mit dem Engine-Block?")
    return text[:m.end()] + block + text[m.end():]


def apply_to_sheet(text: str, fig: dict, with_block: bool = True) -> str:
    """The sheet with HP line, temp HP, spell slots (and the block) equal to the engine figure."""
    old = sheet.parse(text)
    slots = {int(k): (v["used"], v["max"]) for k, v in (fig.get("spell_slots") or {}).items()
             if str(k).isdigit() and (str(k) in (old.get("spell_slots") or {}) or v.get("max"))}
    text = sheet.change(text,
                        hp=fig.get("hp") if old.get("hp") is not None else None,
                        hp_max=fig.get("hp_max") if old.get("hp_max") is not None else None,
                        temp=(fig.get("temp_hp") or 0) if old.get("temp_hp") is not None else None,
                        slots=slots or None)
    return put_block(text, fig) if with_block else remove_block(text)


def sheet_paths(campaign_dir) -> "dict[str, pathlib.Path]":
    out = {}
    for path in sorted((pathlib.Path(campaign_dir) / "characters").glob("*.md")):
        name = sheet.parse(path.read_text(encoding="utf-8")).get("name") or path.stem
        out[name] = path
    return out


def sync_sheets(campaign_dir, names=None, with_block: bool = True) -> "list[str]":
    """Write the engine values into the sheets (all, or the named characters).
    Returns the sheet file names that changed. Never touches anything else."""
    snap = snapshot(campaign_dir)
    if snap is None:
        return []
    changed = []
    wanted = None if names is None else {n.lower() for n in names}
    for name, path in sheet_paths(campaign_dir).items():
        if wanted is not None and name.lower() not in wanted:
            continue
        fig = snap["characters"].get(name)
        if fig is None:
            continue
        text = path.read_text(encoding="utf-8")
        new = apply_to_sheet(text, fig, with_block=with_block)
        if new != text:
            tmp = path.with_suffix(".md.tmp")
            tmp.write_text(new, encoding="utf-8", newline="")
            tmp.replace(path)
            changed.append(path.name)
    return changed


def tracker_overlay(campaign_dir, tracker: dict) -> dict:
    """tracker.json content with conditions, concentration and death saves taken from the
    engine (S3b: the engine wins). Timed effects keep their tracker bookkeeping, but only
    those the engine still knows remain."""
    snap = snapshot(campaign_dir)
    if snap is None:
        return tracker
    src = json.loads(json.dumps(tracker))            # deep copy, JSON types only
    out: dict = {}
    seen = set()
    for key, entry in src.items():
        if not isinstance(entry, dict):
            out[key] = entry
            continue
        group, fkey = st.resolve(snap, entry.get("name") or key)
        fig = snap[group].get(fkey)
        if fig is None:
            out[key] = entry
            continue
        # one entry per figure, under its full name ("ilsa" and "ilsa morgentau" are one person)
        name = fig.get("name") or fkey
        merged = out.setdefault(name.lower(), {"name": name, "effects": []})
        known = {e["name"].lower() for e in fig.get("effects") or []}
        have = {str(e.get("name", "")).lower() for e in merged["effects"]}
        merged["effects"] += [e for e in entry.get("effects") or []
                              if str(e.get("name", "")).lower() in known - have]
        merged["conditions"] = list(fig.get("conditions") or [])
        merged["concentration"] = fig.get("concentration")
        merged["death_saves"] = dict(fig.get("death_saves") or {"successes": 0, "failures": 0, "stable": False})
        seen.add((group, fkey))
    for group in ("characters", "others"):
        for fkey, fig in snap[group].items():
            if (group, fkey) in seen or not (fig.get("conditions") or fig.get("concentration")):
                continue
            out[(fig.get("name") or fkey).lower()] = {
                "name": fig.get("name") or fkey, "conditions": list(fig.get("conditions") or []),
                "concentration": fig.get("concentration"), "effects": [],
                "death_saves": dict(fig.get("death_saves") or {"successes": 0, "failures": 0, "stable": False})}
    return out
