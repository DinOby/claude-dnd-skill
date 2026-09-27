"""grid_map.py — battle-grid maps for the main display: model, patches, storage.

A map is plain JSON:

    {"id": "kessel-schankraum", "name": "Schankraum im Kessel",
     "template": "tavern-small", "tags": ["tavern", "small"],
     "cols": 14, "rows": 10, "cell_ft": 5,
     "background": {"asset": "map:kessel-schankraum"},
     "terrain": [{"x": 5, "y": 4, "w": 3, "h": 1, "type": "table"}],
     "tokens":  [{"id": "flerb", "name": "Flerb", "kind": "pc", "x": 2, "y": 5,
                  "size": 1, "asset": "token:flerb"}],
     "rev": 7}

Coordinates are 0-based x (column) / y (row) in JSON. The DM uses chess-like
notation instead: column letters, row numbers, A1 = top-left (D5 → x=3, y=4;
after Z come AA, AB … like a spreadsheet). Every input that takes a position
accepts either {"x", "y"}, "at": "D5", or a "D5" string.

Partial updates never resend the whole map:

    {"map_id": "kessel-schankraum",
     "move":   [{"id": "flerb", "to": "D5"}],
     "add":    [{"name": "Goblin 2", "kind": "enemy", "at": "E7"}],
     "remove": ["goblin-1"]}

A patch is all-or-nothing: one bad operation rejects it with every problem
listed, so the DM can fix the command and nothing half-applies. Things that
are legal but probably unintended (ending on a wall, sharing a cell) come back
as warnings.

Storage keeps a map's layout apart from who stands on it:

    <data-root>/maps/library/<id>.json    layout (grid, terrain, background) —
                                          reused across campaigns
    <campaign>/maps/<id>.json             tokens + rev for this campaign
    <campaign>/maps/active.json           {"map_id": …} — the map on screen

Without an active campaign the per-campaign files go to the runtime dir.
"""

import copy
import json
import os
import re
import tempfile
import threading
import unicodedata
from typing import Optional

MAX_CELLS = 60                       # per side; a TV cannot show more legibly
TOKEN_KINDS = ("pc", "npc", "enemy", "object")
MAX_TOKEN_SIZE = 4                   # gargantuan
BLOCKING_TERRAIN = ("wall",)         # ending a move here is only a warning
LAYOUT_FIELDS = ("id", "name", "template", "tags", "cols", "rows", "cell_ft",
                 "background", "terrain")
_TOKEN_FIELDS = ("id", "name", "kind", "x", "y", "size", "asset", "archetype", "hidden")

_COORD = re.compile(r"^\s*([A-Za-z]{1,2})\s*(\d{1,3})\s*$")
_UMLAUTS = str.maketrans({"ä": "ae", "ö": "oe", "ü": "ue", "ß": "ss",
                          "Ä": "ae", "Ö": "oe", "Ü": "ue", "ẞ": "ss"})


class MapError(ValueError):
    """Invalid map or patch; .problems lists every reason."""

    def __init__(self, problems: "list[str]"):
        super().__init__("; ".join(problems))
        self.problems = problems


# ── Coordinates ───────────────────────────────────────────────────────────────

def col_label(x: int) -> str:
    """0 → A, 25 → Z, 26 → AA."""
    label = ""
    x += 1
    while x > 0:
        x, rem = divmod(x - 1, 26)
        label = chr(65 + rem) + label
    return label


def format_coord(x: int, y: int) -> str:
    return f"{col_label(x)}{y + 1}"


def parse_coord(text: str) -> "tuple[int, int]":
    """'D5' → (3, 4). Raises ValueError for anything else (bounds are not checked here)."""
    m = _COORD.match(text or "")
    if not m:
        raise ValueError(f"'{text}' is not a grid position (expected e.g. D5)")
    x = 0
    for ch in m.group(1).upper():
        x = x * 26 + (ord(ch) - 64)
    y = int(m.group(2))
    if y < 1:
        raise ValueError(f"'{text}': rows start at 1")
    return x - 1, y - 1


def _position(obj, where: str, problems: "list[str]") -> "Optional[tuple[int, int]]":
    """Position from "D5", {"to"/"at": "D5"} or {"x", "y"}; None (with a problem) if absent/bad."""
    if isinstance(obj, str):
        text = obj
    elif isinstance(obj, dict) and isinstance(obj.get("to") or obj.get("at"), str):
        text = obj.get("to") or obj.get("at")
    elif isinstance(obj, dict) and _is_int(obj.get("x")) and _is_int(obj.get("y")):
        return obj["x"], obj["y"]
    else:
        problems.append(f"{where}: position missing (use \"at\": \"D5\" or x/y)")
        return None
    try:
        return parse_coord(text)
    except ValueError as e:
        problems.append(f"{where}: {e}")
        return None


def _is_int(v) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


# ── Ids ───────────────────────────────────────────────────────────────────────

def slug(text: str) -> str:
    """'Goblin 2' → 'goblin-2', 'Wirtin Hilde' → 'wirtin-hilde' (same rules as asset keys)."""
    s = unicodedata.normalize("NFKC", text or "").translate(_UMLAUTS)
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode("ascii")
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")[:60].rstrip("-")


def _unique_id(base: str, taken: "set[str]") -> str:
    if base not in taken:
        return base
    n = 2
    while f"{base}-{n}" in taken:
        n += 1
    return f"{base}-{n}"


# ── Validation ────────────────────────────────────────────────────────────────

def _check_rect(rect: dict, cols: int, rows: int, where: str, problems: "list[str]") -> None:
    x, y, w, h = rect["x"], rect["y"], rect.get("w", 1), rect.get("h", 1)
    if x < 0 or y < 0 or x + w > cols or y + h > rows:
        problems.append(f"{where}: {format_coord(x, y)} (size {w}×{h}) is outside the {cols}×{rows} grid")


def _norm_terrain(raw, cols: int, rows: int, problems: "list[str]") -> "list[dict]":
    out = []
    if raw is None:
        return out
    if not isinstance(raw, list):
        problems.append("terrain should be a list")
        return out
    for i, t in enumerate(raw):
        where = f"terrain[{i}]"
        if not isinstance(t, dict) or not isinstance(t.get("type"), str) or not t["type"].strip():
            problems.append(f"{where}: needs a \"type\" (e.g. wall, table, water)")
            continue
        pos = _position(t, where, problems)
        w, h = t.get("w", 1), t.get("h", 1)
        if not (_is_int(w) and _is_int(h) and w >= 1 and h >= 1):
            problems.append(f"{where}: w/h should be whole numbers ≥ 1")
            continue
        if pos is None:
            continue
        rect = {"x": pos[0], "y": pos[1], "w": w, "h": h, "type": t["type"].strip()}
        if isinstance(t.get("label"), str) and t["label"].strip():
            rect["label"] = t["label"].strip()
        _check_rect(rect, cols, rows, where, problems)
        out.append(rect)
    return out


def _norm_token(raw, cols: int, rows: int, taken: "set[str]", where: str,
                problems: "list[str]") -> Optional[dict]:
    if isinstance(raw, str):
        raw = {"name": raw}
    if not isinstance(raw, dict):
        problems.append(f"{where}: a token should be an object")
        return None
    name = raw.get("name")
    if not isinstance(name, str) or not name.strip():
        problems.append(f"{where}: token needs a \"name\"")
        return None
    name = name.strip()
    kind = raw.get("kind", "npc")
    if kind not in TOKEN_KINDS:
        problems.append(f"{where} ({name}): kind should be one of {', '.join(TOKEN_KINDS)}")
        return None
    size = raw.get("size", 1)
    if not (_is_int(size) and 1 <= size <= MAX_TOKEN_SIZE):
        problems.append(f"{where} ({name}): size should be 1–{MAX_TOKEN_SIZE} cells")
        return None
    pos = _position(raw, f"{where} ({name})", problems)
    if pos is None:
        return None
    base = raw.get("id") if isinstance(raw.get("id"), str) and slug(raw["id"]) else name
    tid = slug(base)
    if not tid:
        problems.append(f"{where}: cannot derive an id from '{name}'")
        return None
    if isinstance(raw.get("id"), str) and tid in taken:
        problems.append(f"{where}: token id '{tid}' is already on the map")
        return None
    tid = _unique_id(tid, taken)
    tok = {"id": tid, "name": name, "kind": kind, "x": pos[0], "y": pos[1], "size": size}
    for field in ("asset", "archetype"):
        if isinstance(raw.get(field), str) and raw[field].strip():
            tok[field] = raw[field].strip()
    if raw.get("hidden") is True:
        tok["hidden"] = True
    _check_rect({"x": tok["x"], "y": tok["y"], "w": size, "h": size}, cols, rows,
                f"{where} ({name})", problems)
    taken.add(tid)
    return tok


def normalize_map(raw: dict) -> dict:
    """Validated, canonical copy of a full map. Raises MapError."""
    problems: "list[str]" = []
    if not isinstance(raw, dict):
        raise MapError(["a map should be a JSON object"])
    mid = slug(raw.get("id") or raw.get("name") or "")
    if not mid:
        problems.append("map needs an \"id\" (or a \"name\" to derive one)")
    cols, rows = raw.get("cols"), raw.get("rows")
    if not (_is_int(cols) and _is_int(rows) and 1 <= cols <= MAX_CELLS and 1 <= rows <= MAX_CELLS):
        problems.append(f"cols/rows should be whole numbers between 1 and {MAX_CELLS}")
        raise MapError(problems)
    cell_ft = raw.get("cell_ft", 5)
    if not (_is_int(cell_ft) and cell_ft > 0):
        problems.append("cell_ft should be a positive whole number")
    out = {"id": mid, "name": (raw.get("name") or "").strip() or mid, "cols": cols, "rows": rows,
           "cell_ft": cell_ft if _is_int(cell_ft) else 5}
    if isinstance(raw.get("template"), str) and raw["template"].strip():
        out["template"] = raw["template"].strip()
    tags = raw.get("tags", [])
    if not isinstance(tags, list) or not all(isinstance(t, str) for t in tags):
        problems.append("tags should be a list of strings")
        tags = []
    out["tags"] = [t.strip() for t in tags if t.strip()]
    bg = raw.get("background")
    if bg is not None and not (isinstance(bg, dict) and isinstance(bg.get("asset"), str)):
        problems.append("background should be {\"asset\": \"map:…\"}")
        bg = None
    out["background"] = {"asset": bg["asset"]} if bg else None
    out["terrain"] = _norm_terrain(raw.get("terrain"), cols, rows, problems)
    taken: "set[str]" = set()
    tokens_raw = raw.get("tokens", [])
    if not isinstance(tokens_raw, list):
        problems.append("tokens should be a list")
        tokens_raw = []
    out["tokens"] = []
    for i, raw_tok in enumerate(tokens_raw):
        tok = _norm_token(raw_tok, cols, rows, taken, f"tokens[{i}]", problems)
        if tok:
            out["tokens"].append(tok)
    out["rev"] = raw["rev"] if _is_int(raw.get("rev")) and raw["rev"] >= 0 else 0
    if problems:
        raise MapError(problems)
    return out


# ── Lookup and warnings ───────────────────────────────────────────────────────

def find_token(m: dict, ref: str) -> Optional[dict]:
    """Token by id, then by name (case-insensitive), then by slug of the name."""
    if not isinstance(ref, str) or not ref.strip():
        return None
    ref = ref.strip()
    for t in m["tokens"]:
        if t["id"] == ref:
            return t
    low = ref.casefold()
    for t in m["tokens"]:
        if t["name"].casefold() == low:
            return t
    s = slug(ref)
    return next((t for t in m["tokens"] if t["id"] == s), None)


def _cells(x: int, y: int, size: int):
    return {(x + dx, y + dy) for dx in range(size) for dy in range(size)}


def token_warnings(m: dict, tok: dict) -> "list[str]":
    """Legal-but-suspicious placement: on blocking terrain or sharing cells."""
    out = []
    mine = _cells(tok["x"], tok["y"], tok["size"])
    for t in m["terrain"]:
        if t["type"] in BLOCKING_TERRAIN and mine & _cells_rect(t):
            out.append(f"{tok['name']} stands on {t['type']} at {format_coord(tok['x'], tok['y'])}")
            break
    for other in m["tokens"]:
        if other is not tok and other["id"] != tok["id"] and mine & _cells(other["x"], other["y"], other["size"]):
            out.append(f"{tok['name']} shares {format_coord(tok['x'], tok['y'])} with {other['name']}")
    return out


def _cells_rect(r: dict):
    return {(r["x"] + dx, r["y"] + dy) for dx in range(r["w"]) for dy in range(r["h"])}


# ── Patches ───────────────────────────────────────────────────────────────────

def apply_patch(m: dict, patch: dict) -> "tuple[dict, dict, list[str]]":
    """Apply move/add/remove to map `m` (not modified).

    Returns (new_map, applied_patch, warnings); `applied_patch` is what the
    browser needs: {"map_id", "base", "rev", "move": [{id,x,y}], "add": [token],
    "remove": [id]}. Raises MapError listing every problem; nothing applies then.
    """
    if not isinstance(patch, dict):
        raise MapError(["a map patch should be a JSON object"])
    problems: "list[str]" = []
    if patch.get("map_id") not in (None, m["id"]):
        raise MapError([f"patch is for map '{patch.get('map_id')}', but '{m['id']}' is on screen"])
    new = copy.deepcopy(m)
    applied = {"map_id": m["id"], "base": m["rev"], "move": [], "add": [], "remove": []}
    touched: "list[dict]" = []

    for i, ref in enumerate(_as_list(patch.get("remove"), "remove", problems)):
        tok = find_token(new, ref if isinstance(ref, str) else (ref or {}).get("id", ""))
        if tok is None:
            problems.append(f"remove[{i}]: no token '{ref}' on the map")
            continue
        new["tokens"].remove(tok)
        applied["remove"].append(tok["id"])

    for i, mv in enumerate(_as_list(patch.get("move"), "move", problems)):
        ref = mv.get("id") or mv.get("name") if isinstance(mv, dict) else None
        tok = find_token(new, ref) if ref else None
        if tok is None:
            problems.append(f"move[{i}]: no token '{ref}' on the map")
            continue
        pos = _position(mv, f"move[{i}] ({tok['name']})", problems)
        if pos is None:
            continue
        before = len(problems)
        _check_rect({"x": pos[0], "y": pos[1], "w": tok["size"], "h": tok["size"]},
                    new["cols"], new["rows"], f"move[{i}] ({tok['name']})", problems)
        if len(problems) > before:
            continue
        tok["x"], tok["y"] = pos
        applied["move"] = [a for a in applied["move"] if a["id"] != tok["id"]]
        applied["move"].append({"id": tok["id"], "x": pos[0], "y": pos[1]})
        touched.append(tok)

    taken = {t["id"] for t in new["tokens"]}
    for i, raw in enumerate(_as_list(patch.get("add"), "add", problems)):
        tok = _norm_token(raw, new["cols"], new["rows"], taken, f"add[{i}]", problems)
        if tok:
            new["tokens"].append(tok)
            applied["add"].append(tok)
            touched.append(tok)

    if problems:
        raise MapError(problems)
    if not (applied["move"] or applied["add"] or applied["remove"]):
        raise MapError(["the patch changes nothing (use move, add or remove)"])
    new["rev"] = m["rev"] + 1
    applied["rev"] = new["rev"]
    warnings = []
    for tok in touched:
        if tok in new["tokens"]:
            warnings += token_warnings(new, tok)
    return new, {k: v for k, v in applied.items() if v or k in ("map_id", "base", "rev")}, warnings


def _as_list(val, name: str, problems: "list[str]") -> list:
    if val is None:
        return []
    if not isinstance(val, list):
        problems.append(f"{name} should be a list")
        return []
    return val


# ── Storage ───────────────────────────────────────────────────────────────────

def _read_json(path: str) -> Optional[dict]:
    try:
        with open(path, encoding="utf-8-sig") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


def _write_json(path: str, data: dict) -> None:
    directory = os.path.dirname(path)
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".map.", suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")
    os.replace(tmp, path)


class MapStore:
    """Current map of the display plus its files; thread-safe.

    library_dir holds layouts shared by all campaigns, placement_dir the
    tokens of the active campaign (switch with set_placement_dir).
    """

    def __init__(self, library_dir: str, placement_dir: str):
        self.library_dir = library_dir
        self.placement_dir = placement_dir
        self._lock = threading.Lock()
        self._current: Optional[dict] = None
        self._load_active()

    # ── files ──
    def _layout_path(self, map_id: str) -> str:
        return os.path.join(self.library_dir, f"{slug(map_id)}.json")

    def _placement_path(self, map_id: str) -> str:
        return os.path.join(self.placement_dir, f"{slug(map_id)}.json")

    def _active_path(self) -> str:
        return os.path.join(self.placement_dir, "active.json")

    def _save(self, m: dict, layout: bool) -> None:
        if layout:
            _write_json(self._layout_path(m["id"]), {k: m[k] for k in LAYOUT_FIELDS if k in m})
        _write_json(self._placement_path(m["id"]), {"map_id": m["id"], "rev": m["rev"],
                                                    "tokens": m["tokens"]})
        _write_json(self._active_path(), {"map_id": m["id"]})

    def load(self, map_id: str) -> Optional[dict]:
        """Library layout + this campaign's tokens; None if the layout is unknown/broken."""
        layout = _read_json(self._layout_path(map_id))
        if layout is None:
            return None
        placement = _read_json(self._placement_path(map_id)) or {}
        raw = dict(layout, tokens=placement.get("tokens", []), rev=placement.get("rev", 0))
        try:
            return normalize_map(raw)
        except MapError:
            try:   # a layout edit left old tokens outside the grid → keep the layout
                return normalize_map(dict(raw, tokens=[]))
            except MapError:
                return None

    def _load_active(self) -> None:
        active = _read_json(self._active_path()) or {}
        self._current = self.load(active["map_id"]) if isinstance(active.get("map_id"), str) else None

    def set_placement_dir(self, placement_dir: str) -> None:
        with self._lock:
            self.placement_dir = placement_dir
            self._load_active()

    def library(self) -> "list[str]":
        try:
            return sorted(f[:-5] for f in os.listdir(self.library_dir) if f.endswith(".json"))
        except OSError:
            return []

    # ── state ──
    def current(self) -> Optional[dict]:
        with self._lock:
            return copy.deepcopy(self._current)

    def set_map(self, raw: dict) -> dict:
        """Show a full map; the layout is saved to the library.

        Without a "tokens" key the campaign's saved tokens for this map are
        kept (those that still fit the grid), so re-sending a layout does not
        clear the board.
        """
        m = normalize_map(raw)
        if isinstance(raw, dict) and "tokens" not in raw:
            saved = (_read_json(self._placement_path(m["id"])) or {}).get("tokens") or []
            for tok in saved if isinstance(saved, list) else []:
                try:
                    m = normalize_map(dict(m, tokens=m["tokens"] + [tok]))
                except MapError:
                    pass
        with self._lock:
            old = self._current if self._current and self._current["id"] == m["id"] else None
            m["rev"] = (old["rev"] + 1) if old else max(m["rev"], 1)
            self._save(m, layout=True)
            self._current = m
            return copy.deepcopy(m)

    def show(self, map_id: str) -> dict:
        """Show a map from the library with this campaign's tokens."""
        with self._lock:
            m = self.load(map_id)
            if m is None:
                raise MapError([f"no map '{slug(map_id)}' in the library"])
            _write_json(self._active_path(), {"map_id": m["id"]})
            self._current = m
            return copy.deepcopy(m)

    def patch(self, patch: dict) -> "tuple[dict, list[str]]":
        """Apply a patch to the map on screen; returns (applied_patch, warnings)."""
        with self._lock:
            if self._current is None:
                raise MapError(["no map is on screen (set one first)"])
            new, applied, warnings = apply_patch(self._current, patch)
            self._save(new, layout=False)
            self._current = new
            return applied, warnings

    def hide(self) -> None:
        """Take the map off screen; files stay."""
        with self._lock:
            self._current = None
            try:
                os.remove(self._active_path())
            except OSError:
                pass
