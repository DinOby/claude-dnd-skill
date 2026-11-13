"""map_v2.py - map format v2: layers, conversion, passability, paths (Phase 1).

A v2 layout keeps what is drawn on separate layers, bottom to top:

    {"format": 2, "id": "furt", "name": "Die Furt", "cols": 16, "rows": 12,
     "cell_ft": 5, "floor": "grass",
     "layers": {
       "ground":  [{"x": 0, "y": 0, "w": 4, "h": 3, "type": "dirt"}],
       "terrain": [{"x": 6, "y": 0, "w": 2, "h": 12, "type": "water"}],
       "walls":   [{"x": 0, "y": 0, "w": 16, "h": 1, "type": "wall"}],
       "objects": [{"x": 6, "y": 5, "w": 2, "h": 2, "type": "bridge"},
                   {"x": 0, "y": 6, "type": "exit", "label": "Zum Dorf"}],
       "paths":   [{"type": "river", "points": [[7, 0], [7, 5], [9, 11]], "width": 2},
                   {"type": "road",  "points": ["A7", "P7"]}]},
     "tokens": [...]}                        # figures: the top layer

Figures (tokens) are stored per campaign as before; the layers belong to the
shared layout. Paths are drawn lines (rivers, roads, trails); they become
terrain cells, so the renderer's autotiling makes them one continuous river or
road with banks, whatever their bends.

A v1 map (one flat "terrain" list) is converted on load, losslessly and only
in memory: every rect goes to the layer its type belongs to. Nothing on disk is
rewritten until the DM saves the map again.

Passability (where a figure may stand, and the way a move animates):
blocked are walls, deep or open water, cliffs and chasms, and objects -
except the ones you walk on or through (doors, stairs, bridges, rugs, exits,
ladders, trapdoors). A bridge or a ford makes its cells walkable even over
water. Shallow water and difficult ground are walkable.
"""
from __future__ import annotations

import heapq
from typing import Iterable, Optional

FORMAT = 2
LAYERS = ("ground", "terrain", "walls", "objects")

GROUND_TYPES = ("grass", "dirt", "stone", "wood", "sand", "cobble", "floor", "snow", "mud", "moss",
                "earth", "tiles", "gravel")
TERRAIN_TYPES = ("water", "deep-water", "shallow-water", "road", "path", "difficult", "cliff",
                 "chasm", "lava", "ice", "rubble", "swamp")
WALL_TYPES = ("wall", "bars", "palisade", "fence", "hedge")
PATH_TYPES = {"river": "water", "stream": "shallow-water", "creek": "shallow-water",
              "road": "road", "path": "path", "trail": "path", "lava": "lava", "chasm": "chasm"}

BLOCKING_TERRAIN = ("water", "deep-water", "cliff", "chasm", "lava")
# Objects you can stand on or walk through; every other object blocks.
WALKABLE_OBJECTS = ("door", "stairs", "bridge", "rug", "exit", "ladder", "trapdoor", "ford",
                    "plank", "carpet", "gate")
# Objects that make their cells walkable even over blocking terrain.
SPANNING_OBJECTS = ("bridge", "ford", "plank")
# Objects that mark a way in or out of the place (shown with a marker).
EXIT_OBJECTS = ("exit", "door", "stairs", "ladder", "trapdoor", "gate")

# Furniture and fittings: does it block the square, and what cover does it give?
# A single object in a layout may override both: {"type": "table", "blocks": false,
# "cover": "half"} (a low table you can climb on). Cover: none | half | three-quarters | full.
COVER = ("none", "half", "three-quarters", "full")
OBJECT_PROPS = {
    "table": (True, "half"), "chair": (True, "none"), "stool": (True, "none"), "bench": (True, "half"),
    "bed": (True, "half"), "bar": (True, "half"), "counter": (True, "half"), "shelf": (True, "three-quarters"),
    "cupboard": (True, "three-quarters"), "wardrobe": (True, "three-quarters"), "chest": (True, "half"),
    "barrel": (True, "half"), "crate": (True, "half"), "sack": (True, "none"), "plant": (True, "none"),
    "pot": (True, "none"), "tree": (True, "three-quarters"), "bush": (True, "half"), "rock": (True, "half"),
    "pillar": (True, "three-quarters"), "statue": (True, "three-quarters"), "altar": (True, "half"),
    "shrine": (True, "half"), "well": (True, "half"), "fireplace": (True, "half"), "fire": (True, "none"),
    "stall": (True, "half"), "tent": (True, "full"), "wagon": (True, "three-quarters"), "log": (True, "half"),
    "anvil": (True, "half"), "forge": (True, "half"), "cage": (True, "half"), "throne": (True, "half"),
    "door": (False, "none"), "gate": (False, "none"), "stairs": (False, "none"), "ladder": (False, "none"),
    "trapdoor": (False, "none"), "exit": (False, "none"), "bridge": (False, "none"), "ford": (False, "none"),
    "plank": (False, "none"), "rug": (False, "none"), "carpet": (False, "none"),
}


def object_props(obj: dict) -> "tuple[bool, str]":
    """(blocks, cover) of a layout object: its own override, else the type's default
    (an unknown thing blocks and gives half cover)."""
    blocks, cover = OBJECT_PROPS.get(obj.get("type"), (True, "half"))
    if isinstance(obj.get("blocks"), bool):
        blocks = obj["blocks"]
    if obj.get("cover") in COVER:
        cover = obj["cover"]
    return blocks, cover


MAX_PATH_POINTS = 64
MAX_PATH_WIDTH = 4


def layer_of(kind: str) -> str:
    """The layer a v1 terrain type belongs to."""
    k = (kind or "").strip().lower()
    if k in WALL_TYPES:
        return "walls"
    if k in TERRAIN_TYPES:
        return "terrain"
    if k in GROUND_TYPES:
        return "ground"
    return "objects"


def from_v1(terrain: Iterable[dict]) -> dict:
    """Layers for a v1 flat terrain list (rects already normalised)."""
    layers = {name: [] for name in LAYERS}
    layers["paths"] = []
    for rect in terrain or []:
        layers[layer_of(rect.get("type", ""))].append(dict(rect))
    return layers


def flat_terrain(layers: dict, cols: int, rows: int) -> "list[dict]":
    """The v1 view of v2 layers: one rect list (terrain, walls, objects, path cells).

    Kept for every part that still reads `terrain` (sprites, warnings, the
    DOM renderer). Ground patches are not in it - they only change the look.
    """
    out = []
    for name in ("terrain", "walls", "objects"):
        out += [dict(r) for r in layers.get(name) or []]
    for path in layers.get("paths") or []:
        kind = PATH_TYPES.get(path.get("type"), "road")
        for x, y in path_cells(path, cols, rows):
            out.append({"x": x, "y": y, "w": 1, "h": 1, "type": kind, "from_path": True})
    return out


# -- paths (rivers, roads) ----------------------------------------------------

def _line(a, b):
    """Cells of a 4-connected line from a to b (no diagonal gaps: water never leaks)."""
    (x0, y0), (x1, y1) = a, b
    cells = [(x0, y0)]
    dx, dy = abs(x1 - x0), abs(y1 - y0)
    sx = 1 if x1 > x0 else -1
    sy = 1 if y1 > y0 else -1
    err = dx - dy
    x, y = x0, y0
    while (x, y) != (x1, y1):
        e2 = 2 * err
        if x != x1 and (e2 > -dy or y == y1):
            err -= dy
            x += sx
        else:
            err += dx
            y += sy
        cells.append((x, y))
    return cells


def path_cells(path: dict, cols: int, rows: int) -> "list[tuple[int, int]]":
    """Every cell a path covers, in order, `width` cells wide, inside the grid."""
    pts = [tuple(p) for p in path.get("points") or []]
    width = max(1, min(MAX_PATH_WIDTH, int(path.get("width") or 1)))
    centre = []
    for a, b in zip(pts, pts[1:]):
        for c in _line(a, b):
            if not centre or centre[-1] != c:
                centre.append(c)
    if len(pts) == 1:
        centre = [pts[0]]
    lo = -((width - 1) // 2)
    seen, out = set(), []
    for i, (cx, cy) in enumerate(centre):
        prev = centre[i - 1] if i else None
        nxt = centre[i + 1] if i + 1 < len(centre) else None
        ref = nxt or prev
        horizontal = ref is not None and ref[1] == cy
        for k in range(lo, lo + width):
            cells = [(cx, cy + k)] if horizontal else [(cx + k, cy)]
            if ref is None:
                cells = [(cx + k, cy + j) for j in range(lo, lo + width)]
            for c in cells:
                if 0 <= c[0] < cols and 0 <= c[1] < rows and c not in seen:
                    seen.add(c)
                    out.append(c)
    return out


def _point(q, parse_coord):
    if isinstance(q, str):
        return parse_coord(q)
    if isinstance(q, (list, tuple)) and len(q) == 2 and all(isinstance(v, int) for v in q):
        return q[0], q[1]
    if isinstance(q, dict) and isinstance(q.get("x"), int) and isinstance(q.get("y"), int):
        return q["x"], q["y"]
    raise ValueError(repr(q))


def norm_paths(raw, cols: int, rows: int, problems: "list[str]", parse_coord) -> "list[dict]":
    out = []
    if raw is None:
        return out
    if not isinstance(raw, list):
        problems.append("layers.paths should be a list")
        return out
    for i, p in enumerate(raw):
        where = "paths[%d]" % i
        if not isinstance(p, dict) or p.get("type") not in PATH_TYPES:
            problems.append(where + ": type should be one of " + ", ".join(PATH_TYPES))
            continue
        pts_raw = p.get("points")
        if not isinstance(pts_raw, list) or not 1 <= len(pts_raw) <= MAX_PATH_POINTS:
            problems.append(where + ": points should be a list of 1-%d squares" % MAX_PATH_POINTS)
            continue
        pts, ok = [], True
        for j, q in enumerate(pts_raw):
            try:
                x, y = _point(q, parse_coord)
            except ValueError as e:
                problems.append("%s.points[%d]: not a square (%s)" % (where, j, e))
                ok = False
                break
            if not (0 <= x < cols and 0 <= y < rows):
                problems.append("%s.points[%d]: outside the %dx%d grid" % (where, j, cols, rows))
                ok = False
                break
            pts.append([x, y])
        if not ok:
            continue
        width = p.get("width", 1)
        if not (isinstance(width, int) and 1 <= width <= MAX_PATH_WIDTH):
            problems.append(where + ": width should be 1-%d" % MAX_PATH_WIDTH)
            continue
        entry = {"type": p["type"], "points": pts, "width": width}
        if isinstance(p.get("label"), str) and p["label"].strip():
            entry["label"] = p["label"].strip()
        out.append(entry)
    return out


# -- passability --------------------------------------------------------------

def _rect_cells(r: dict):
    return ((r["x"] + dx, r["y"] + dy) for dy in range(r.get("h", 1)) for dx in range(r.get("w", 1)))


def blockers(m: dict) -> dict:
    """{cell: (layer, type)} for every cell a figure cannot stand on.

    `m` is a normalised map (has `layers` and `cols`/`rows`).
    """
    layers = m.get("layers") or from_v1(m.get("terrain") or [])
    cols, rows = m["cols"], m["rows"]
    out: dict = {}
    span = set()
    for r in layers.get("objects") or []:
        if r["type"] in SPANNING_OBJECTS:
            span.update(_rect_cells(r))
    for r in layers.get("terrain") or []:
        if r["type"] in BLOCKING_TERRAIN:
            for c in _rect_cells(r):
                if c not in span:
                    out[c] = ("terrain", r["type"])
    for path in layers.get("paths") or []:
        kind = PATH_TYPES.get(path["type"])
        if kind in BLOCKING_TERRAIN:
            for c in path_cells(path, cols, rows):
                if c not in span:
                    out[c] = ("terrain", kind)
    for r in layers.get("objects") or []:
        if object_props(r)[0]:
            for c in _rect_cells(r):
                out[c] = ("objects", r["type"])
    for r in layers.get("walls") or []:
        for c in _rect_cells(r):
            out[c] = ("walls", r["type"])
    return out


GERMAN = {
    "wall": "Wand", "bars": "Gitter", "palisade": "Palisade", "fence": "Zaun", "hedge": "Hecke",
    "water": "Wasser", "deep-water": "tiefes Wasser", "cliff": "Klippe", "chasm": "Abgrund",
    "lava": "Lava", "table": "Tisch", "chair": "Stuhl", "bed": "Bett", "tree": "Baum",
    "rock": "Felsen", "pillar": "Säule", "well": "Brunnen", "bar": "Theke", "fireplace": "Kamin",
    "fire": "Feuer", "stall": "Marktstand", "tent": "Zelt", "wagon": "Wagen", "log": "Baumstamm",
    "chest": "Truhe", "altar": "Altar", "statue": "Statue", "shrine": "Schrein", "barrel": "Fass",
    "crate": "Kiste", "bush": "Busch", "bench": "Bank", "stool": "Hocker", "shelf": "Regal",
    "cupboard": "Schrank", "wardrobe": "Kleiderschrank", "counter": "Tresen", "sack": "Sack",
    "plant": "Pflanze", "pot": "Topf", "anvil": "Amboss", "forge": "Esse", "cage": "Käfig",
    "throne": "Thron",
}


def german(kind: str) -> str:
    return GERMAN.get(kind, kind)


def nearest_free(m: dict, x: int, y: int, size: int = 1, occupied=frozenset(), blocked=None):
    """The free square closest to (x, y) a figure of `size` can stand on, or None."""
    blocked = blockers(m) if blocked is None else blocked
    best = None
    for yy in range(m["rows"] - size + 1):
        for xx in range(m["cols"] - size + 1):
            cells = {(xx + dx, yy + dy) for dx in range(size) for dy in range(size)}
            if any(c in blocked for c in cells) or cells & set(occupied):
                continue
            d = max(abs(xx - x), abs(yy - y)) * 100 + abs(xx - x) + abs(yy - y)
            if best is None or d < best[0]:
                best = (d, (xx, yy))
    return best[1] if best else None


def find_path(m: dict, start, goal, blocked=None, limit: int = 6000):
    """Shortest walkable way from start to goal (8 directions, no cutting past a
    blocked corner) as a list of cells including both ends; None when there is
    none. The start may itself be blocked (a figure on a table steps down)."""
    blocked = blockers(m) if blocked is None else blocked
    cols, rows = m["cols"], m["rows"]
    start, goal = tuple(start), tuple(goal)
    if start == goal:
        return [start]
    if goal in blocked:
        return None

    def h(c):
        dx, dy = abs(c[0] - goal[0]), abs(c[1] - goal[1])
        return 10 * max(dx, dy) + 4 * min(dx, dy)

    open_heap = [(h(start), 0, start)]
    came = {start: None}
    cost = {start: 0}
    steps = 0
    while open_heap and steps < limit:
        _, g, cur = heapq.heappop(open_heap)
        steps += 1
        if cur == goal:
            out = [cur]
            while came[out[-1]] is not None:
                out.append(came[out[-1]])
            return out[::-1]
        if g > cost.get(cur, 1 << 30):
            continue
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                if not dx and not dy:
                    continue
                nx, ny = cur[0] + dx, cur[1] + dy
                if not (0 <= nx < cols and 0 <= ny < rows) or (nx, ny) in blocked:
                    continue
                if dx and dy and ((cur[0] + dx, cur[1]) in blocked or (cur[0], cur[1] + dy) in blocked):
                    continue
                ng = g + (14 if dx and dy else 10)
                if ng < cost.get((nx, ny), 1 << 30):
                    cost[(nx, ny)] = ng
                    came[(nx, ny)] = cur
                    heapq.heappush(open_heap, (ng + h((nx, ny)), ng, (nx, ny)))
    return None


def reach(m: dict, start, squares: int, blocked=None) -> set:
    """Cells reachable within `squares` steps (a diagonal counts as one square,
    as in the 2024 rules), walking around obstacles."""
    blocked = blockers(m) if blocked is None else blocked
    start = tuple(start)
    seen = {start}
    frontier = [start]
    for _ in range(squares):
        nxt = []
        for cx, cy in frontier:
            for dx in (-1, 0, 1):
                for dy in (-1, 0, 1):
                    c = (cx + dx, cy + dy)
                    if c in seen or not (0 <= c[0] < m["cols"] and 0 <= c[1] < m["rows"]) or c in blocked:
                        continue
                    if dx and dy and ((cx + dx, cy) in blocked or (cx, cy + dy) in blocked):
                        continue
                    seen.add(c)
                    nxt.append(c)
        frontier = nxt
    return seen
