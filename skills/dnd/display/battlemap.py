#!/usr/bin/env python3
"""battlemap.py - painted battle maps from the logic map (Phase 1, "painted map from sketch").

The logic map (grid_map.py / map_v2.py) stays the authority on where walls,
water, doors and furniture are. From it:

  1. sketch(m)   a plain floor plan, one flat colour per kind of square
                 (floor, wall, water, door, furniture …), exactly on the grid;
  2. prompt      config/map-style.json -> painted_map: Gemini repaints the
                 sketch as one finished battle map, keeping the layout;
  3. verify()    the painting is checked square by square against the logic
                 map: wall, water and floor squares must be told apart by
                 their colour (nearest-centre test) - a wall painted where
                 there is none, or water moved, fails;
  4. up to 2 new attempts, then give up: the display keeps the tile drawing
     and the reason is stored (shown to Claude as a map warning);
  5. stored per place in <assets>/battlemaps/<map_id>.jpg (+ .json with the
     layout hash), reused until the layout, style or prompt changes.

The painting itself never decides anything: figures still stand on the grid
squares of the logic map, and every check is the server's.

CLI (tests and manual runs):
    python3 battlemap.py sketch MAP_ID --out FILE.png
    python3 battlemap.py paint MAP_ID [--force] [--max-eur 0.5]
    python3 battlemap.py status [MAP_ID]
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import sys
import threading
import time
from typing import Callable, Optional

_HERE = os.path.dirname(os.path.abspath(__file__))
for _p in (_HERE, os.path.join(_HERE, os.pardir, "scripts")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import map_v2  # noqa: E402

SKETCH_CELL = 48

# Flat sketch colours. Keep them far apart: the painter reads them, and the
# legend in the prompt names them.
FLOOR = {"wood": (154, 107, 60), "floor": (154, 107, 60), "stone": (150, 146, 138), "tiles": (165, 160, 150),
         "cobble": (128, 122, 110), "grass": (104, 150, 70), "moss": (90, 130, 70), "earth": (118, 90, 60),
         "dirt": (138, 106, 72), "mud": (100, 80, 55), "sand": (216, 192, 138), "gravel": (140, 136, 128),
         "snow": (235, 238, 240)}
TERRAIN = {"water": (40, 105, 185), "deep-water": (25, 70, 140), "shallow-water": (95, 160, 205),
           "swamp": (70, 100, 70), "road": (180, 148, 104), "path": (168, 141, 102), "difficult": (125, 105, 80),
           "rubble": (120, 112, 100), "ice": (190, 220, 235), "cliff": (85, 70, 55), "chasm": (60, 30, 75),
           "lava": (210, 70, 26)}
WALL = (20, 20, 20)
BUILDING = (120, 45, 35)          # large wall blocks: whole buildings, painted as roofs
HATCH = (60, 45, 30)              # rough ground: hatching over the floor colour
WELL = (95, 95, 100)
BARS = (60, 60, 64)
DOOR = (245, 205, 60)
STAIRS = (230, 230, 230)
RUG = (160, 40, 40)
BRIDGE = (200, 154, 90)
OBJECT = {"table": (200, 120, 40), "bar": (170, 95, 40), "counter": (170, 95, 40), "chair": (235, 165, 80),
          "stool": (235, 165, 80), "bench": (215, 140, 60), "bed": (225, 220, 205), "chest": (180, 100, 30),
          "barrel": (140, 90, 45), "crate": (205, 165, 95), "shelf": (120, 70, 35), "cupboard": (120, 70, 35),
          "wardrobe": (120, 70, 35), "tree": (35, 105, 40), "bush": (75, 140, 60), "plant": (75, 140, 60),
          "rock": (165, 165, 165), "pillar": (190, 190, 190), "statue": (205, 205, 210), "altar": (210, 205, 190),
          "shrine": (210, 205, 190), "well": (120, 120, 125), "fireplace": (215, 95, 40), "fire": (255, 120, 25),
          "stall": (190, 70, 60), "tent": (200, 185, 150), "wagon": (150, 100, 55), "log": (120, 80, 45),
          "anvil": (70, 70, 75), "forge": (215, 95, 40), "cage": (60, 60, 64), "throne": (175, 140, 50)}
OTHER_OBJECT = (220, 140, 200)
PAD = (255, 255, 255)

LEGEND = ("black = thick stone walls; dark red-brown blocks = solid buildings, paint them as roofs seen from above; dark grey stripes = iron bars; yellow = doors and door openings; "
          "light grey with stripes = stairs; blue = water (darker = deeper); brown = wooden floor; "
          "grey = stone floor or flagstones; green = grass; orange and brown blocks = furniture "
          "(tables, chairs, benches, beds, chests, barrels, crates, shelves); dark green circles = trees; "
          "light grey round = rocks or pillars; dark red = a rug or carpet; light brown planks = a bridge; "
          "dark diagonal hatching = rough ground, rubble or debris (walkable); grey ring with blue centre = a well; "
          "purple = a chasm; white margin outside the map = continue the surroundings naturally")


def _style_cfg() -> dict:
    from config_loader import ConfigFile
    return ConfigFile("map-style.json").get()


def is_building(r: dict) -> bool:
    """A wall block at least 2x2 squares is a whole building, not a wall."""
    return r.get("type", "wall") == "wall" and r.get("w", 1) >= 2 and r.get("h", 1) >= 2


def floor_kind(m: dict) -> "dict[tuple[int, int], str]":
    """The surface of every square: base floor, ground patch or walkable terrain (road, rubble)."""
    out = {(x, y): m.get("floor") or "stone" for x in range(m["cols"]) for y in range(m["rows"])}
    L = m.get("layers") or {}
    for name in ("ground", "terrain"):
        for r in L.get(name) or []:
            for i in range(r.get("w", 1)):
                for j in range(r.get("h", 1)):
                    out[(r["x"] + i, r["y"] + j)] = r["type"]
    for p in L.get("paths") or []:
        for c in map_v2.path_cells(p, m["cols"], m["rows"]):
            out[c] = map_v2.PATH_TYPES.get(p["type"], "road")
    for r in L.get("objects") or []:
        if r["type"] in ("rug", "carpet", "bridge", "plank", "ford", "door", "gate", "stairs", "ladder", "trapdoor", "exit"):
            for i in range(r.get("w", 1)):
                for j in range(r.get("h", 1)):
                    out[(r["x"] + i, r["y"] + j)] = r["type"]
    return out

def layout_hash(m: dict, style_id: str, version: int) -> str:
    key = json.dumps({"cols": m["cols"], "rows": m["rows"], "floor": m.get("floor"), "layers": m.get("layers"),
                      "style": style_id, "v": version}, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:24]


def classify(m: dict) -> "dict[tuple[int, int], str]":
    """Expected kind of every square: wall | building | water | object | floor."""
    layers = m.get("layers") or {}
    out = {(x, y): "floor" for x in range(m["cols"]) for y in range(m["rows"])}
    buildings = {(r["x"] + i, r["y"] + j) for r in layers.get("walls") or [] if is_building(r)
                 for i in range(r.get("w", 1)) for j in range(r.get("h", 1))}
    for (x, y), (layer, kind) in map_v2.blockers(m).items():
        if layer == "walls":
            out[(x, y)] = "building" if (x, y) in buildings else "wall"
        elif layer == "terrain" and kind in ("water", "deep-water"):
            out[(x, y)] = "water"
        elif layer == "objects":
            out[(x, y)] = "object"
    return out


def canvas_for(m: dict, cell: int = SKETCH_CELL) -> "tuple[int, int, tuple[int, int, int, int], str]":
    """Sketch size padded to the painter's nearest aspect ratio; returns (W, H, map box, ratio)."""
    from image_providers.gemini import ASPECT_RATIOS, aspect_ratio
    mw, mh = m["cols"] * cell, m["rows"] * cell
    ratio = aspect_ratio(mw, mh)
    r = ASPECT_RATIOS[ratio]
    if mw / mh >= r:
        w, h = mw, int(round(mw / r))
    else:
        w, h = int(round(mh * r)), mh
    x0, y0 = (w - mw) // 2, (h - mh) // 2
    return w, h, (x0, y0, x0 + mw, y0 + mh), ratio


def sketch(m: dict, cell: int = SKETCH_CELL) -> "tuple[bytes, tuple[int, int, int, int], str]":
    """PNG floor plan of the logic map, padded to an aspect ratio the painter supports.
    Returns (png, map box inside the picture, aspect ratio)."""
    from PIL import Image, ImageDraw
    w, h, box, ratio = canvas_for(m, cell)
    img = Image.new("RGB", (w, h), PAD)
    d = ImageDraw.Draw(img)
    ox, oy = box[0], box[1]
    L = m.get("layers") or {}

    def rect(r, col, inset=0):
        x0 = ox + r["x"] * cell + inset
        y0 = oy + r["y"] * cell + inset
        d.rectangle([x0, y0, x0 + r.get("w", 1) * cell - 1 - 2 * inset, y0 + r.get("h", 1) * cell - 1 - 2 * inset], fill=col)

    d.rectangle([ox, oy, box[2] - 1, box[3] - 1], fill=FLOOR.get(m.get("floor"), FLOOR["stone"]))
    for r in L.get("ground") or []:
        rect(r, FLOOR.get(r["type"], FLOOR["stone"]))
    for r in L.get("terrain") or []:
        if r["type"] in ("difficult", "rubble"):
            for (cx, cy) in [(r["x"] + i, r["y"] + j) for i in range(r.get("w", 1)) for j in range(r.get("h", 1))]:
                x0, y0 = ox + cx * cell, oy + cy * cell
                for k in range(-cell, cell, cell // 4):
                    d.line([x0 + k, y0 + cell, x0 + k + cell, y0], fill=HATCH, width=3)
            continue
        rect(r, TERRAIN.get(r["type"], (125, 105, 80)))
    for p in L.get("paths") or []:
        col = TERRAIN.get(map_v2.PATH_TYPES.get(p["type"], "road"), TERRAIN["road"])
        for x, y in map_v2.path_cells(p, m["cols"], m["rows"]):
            rect({"x": x, "y": y}, col)
    for r in L.get("objects") or []:
        t = r["type"]
        x0, y0 = ox + r["x"] * cell, oy + r["y"] * cell
        x1, y1 = x0 + r.get("w", 1) * cell - 1, y0 + r.get("h", 1) * cell - 1
        if t == "exit":
            continue                      # a way out is the open edge itself: nothing to paint
        if t in ("door", "gate", "trapdoor"):
            rect(r, DOOR)
        elif t in ("stairs", "ladder"):
            rect(r, STAIRS)
            horizontal = r.get("w", 1) >= r.get("h", 1)
            for i in range(1, 4 * max(r.get("w", 1), r.get("h", 1))):
                if horizontal:
                    xx = x0 + i * cell // 4
                    d.line([xx, y0, xx, y1], fill=(120, 120, 120), width=2)
                else:
                    yy = y0 + i * cell // 4
                    d.line([x0, yy, x1, yy], fill=(120, 120, 120), width=2)
        elif t in ("rug", "carpet"):
            rect(r, RUG, inset=cell // 10)
        elif t in ("bridge", "plank", "ford"):
            rect(r, BRIDGE)
        elif t == "well":
            d.ellipse([x0 + cell // 10, y0 + cell // 10, x1 - cell // 10, y1 - cell // 10], fill=WELL)
            d.ellipse([x0 + cell // 3, y0 + cell // 3, x1 - cell // 3, y1 - cell // 3], fill=TERRAIN["water"])
        elif t in ("tree", "bush", "plant", "rock", "pillar", "barrel", "statue"):
            d.ellipse([x0 + cell // 10, y0 + cell // 10, x1 - cell // 10, y1 - cell // 10], fill=OBJECT.get(t, OTHER_OBJECT))
        else:
            inset = cell // 4 if t in ("chair", "stool") else cell // 12
            d.rectangle([x0 + inset, y0 + inset, x1 - inset, y1 - inset], fill=OBJECT.get(t, OTHER_OBJECT))
    for r in L.get("walls") or []:
        if r["type"] == "bars":
            x0, y0 = ox + r["x"] * cell, oy + r["y"] * cell
            for i in range(r.get("w", 1) * 4):
                xx = x0 + i * cell // 4 + cell // 8
                d.line([xx, y0, xx, y0 + r.get("h", 1) * cell - 1], fill=BARS, width=4)
        else:
            rect(r, BUILDING if is_building(r) else WALL)
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue(), box, ratio


def build_prompt(m: dict, cfg: Optional[dict] = None) -> str:
    cfg = cfg or _style_cfg()
    pm = cfg.get("painted_map") or {}
    styles = cfg.get("styles") or {}
    active = cfg.get("active", "neutral")
    st = styles.get(active) or {}
    style = st.get("prompt") if st.get("candidate") else pm.get("default_style")
    rules = cfg.get("rules") or {}
    style = f"{style}; {rules.get('light', '')}".strip("; ")
    text = pm.get("prompt", "").format(style=style, legend=LEGEND, cols=m["cols"], rows=m["rows"],
                                       place=m.get("name") or m["id"])
    return text + " " + inventory(m)


def inventory(m: dict) -> str:
    """Every object by its squares (columns A.., rows 1.. from the top left), so the painter
    neither drops nor invents furniture."""
    from grid_map import format_coord
    groups: dict = {}
    for r in (m.get("layers") or {}).get("objects") or []:
        if r["type"] == "exit":
            continue
        a = format_coord(r["x"], r["y"])
        b = format_coord(r["x"] + r.get("w", 1) - 1, r["y"] + r.get("h", 1) - 1)
        groups.setdefault(r["type"], []).append(a if a == b else f"{a}-{b}")
    if not groups:
        return "There is no furniture: leave the floor free of objects."
    parts = [f"{kind} ({', '.join(where)})" for kind, where in sorted(groups.items())]
    return ("The squares are numbered like a chessboard: columns A, B, C ... from the left, rows 1, 2, 3 ... "
            "from the top. Paint exactly these objects on exactly these squares and nothing else: "
            + "; ".join(parts) + ". Every other floor square stays empty floor (no extra furniture, "
            "no extra benches, rugs or crates).")

# -- checking a painting against the logic map ---------------------------------

def crop_to_map(data: bytes, canvas: "tuple[int, int]", box: "tuple[int, int, int, int]"):
    """The painting's part that covers the map (the sketch had a margin); a PIL image."""
    from PIL import Image
    img = Image.open(io.BytesIO(data)).convert("RGB")
    sx, sy = img.width / canvas[0], img.height / canvas[1]
    return img.crop((round(box[0] * sx), round(box[1] * sy), round(box[2] * sx), round(box[3] * sy)))


def _cell_colours(img, cols: int, rows: int) -> dict:
    """Mean RGB of the inner part of every square (edges blur into neighbours)."""
    px = 12
    small = img.resize((cols * px, rows * px))
    data = small.load()
    out = {}
    for y in range(rows):
        for x in range(cols):
            acc = [0, 0, 0]
            n = 0
            for yy in range(y * px + 3, y * px + px - 3):
                for xx in range(x * px + 3, x * px + px - 3):
                    p = data[xx, yy]
                    acc[0] += p[0]; acc[1] += p[1]; acc[2] += p[2]
                    n += 1
            out[(x, y)] = (acc[0] / n, acc[1] / n, acc[2] / n)
    return out


def _dist(a, b) -> float:
    return ((a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2 + (a[2] - b[2]) ** 2) ** 0.5


def _centre(values) -> tuple:
    vs = list(values)
    return tuple(sorted(v[i] for v in vs)[len(vs) // 2] for i in range(3))   # median: robust to details


def verify(m: dict, img, limits: Optional[dict] = None) -> dict:
    """Do walls and water sit where the logic map has them?

    For each kind (wall, water) against the floor: the median colour of its
    squares and of the floor squares; every square must be nearer to its own
    kind's centre. Returns {"ok", "checks": {kind: {...}}, "reason"}."""
    limits = dict({"min_wall_accuracy": 0.8, "min_water_accuracy": 0.75, "min_separation": 18}, **(limits or {}))
    kinds = classify(m)
    cols = _cell_colours(img, m["cols"], m["rows"])
    floor = [cols[c] for c, k in kinds.items() if k == "floor"]
    checks, reasons = {}, []
    if len(floor) < 3:
        return {"ok": True, "checks": {}, "reason": "too little floor to compare"}
    fc = _centre(floor)
    for kind, need in (("wall", limits["min_wall_accuracy"]), ("building", limits["min_wall_accuracy"]),
                       ("water", limits["min_water_accuracy"])):
        cells = [cols[c] for c, k in kinds.items() if k == kind]
        if len(cells) < 3:
            continue
        kc = _centre(cells)
        sep = _dist(kc, fc)
        own = sum(1 for v in cells if _dist(v, kc) < _dist(v, fc)) / len(cells)
        flo = sum(1 for v in floor if _dist(v, fc) <= _dist(v, kc)) / len(floor)
        ok = sep >= limits["min_separation"] and own >= need and flo >= float(limits.get("min_floor_accuracy", 0.75))
        checks[kind] = {"squares": len(cells), "accuracy": round(own, 2), "floor_accuracy": round(flo, 2),
                        "separation": round(sep, 1), "ok": ok}
        if not ok:
            reasons.append(f"{kind}: {own:.0%} of {len(cells)} squares in place, floor {flo:.0%}, "
                           f"contrast {sep:.0f}")
    objs = [cols[c] for c, k in kinds.items() if k == "object"]
    if objs:
        spread = sum(_dist(v, fc) for v in floor) / len(floor)
        stand_out = sum(_dist(v, fc) for v in objs) / len(objs)
        checks["object"] = {"squares": len(objs), "stand_out": round(stand_out / max(1.0, spread), 2)}
    # floor squares must look like the rest of their surface: an obstacle the painter
    # invented (a wall piece, a crate) on a walkable square shows up as an outlier
    surface = floor_kind(m)
    groups: dict = {}
    for c, k in kinds.items():
        if k == "floor":
            groups.setdefault(surface[c], []).append(c)
    outliers, counted = [], 0
    for name, cells in groups.items():
        if len(cells) < 4:
            continue
        gc = _centre(cols[c] for c in cells)
        spread = sorted(_dist(cols[c], gc) for c in cells)[len(cells) // 2]
        limit = max(float(limits.get("min_outlier_distance", 55)), 3.0 * spread)
        counted += len(cells)
        outliers += [c for c in cells if _dist(cols[c], gc) > limit]
    share = len(outliers) / counted if counted else 0.0
    max_share = float(limits.get("max_invented_share", 0.06))
    checks["walkable"] = {"squares": counted, "invented": len(outliers), "share": round(share, 3),
                          "ok": share <= max_share}
    if share > max_share:
        from grid_map import format_coord
        where = ", ".join(format_coord(*c) for c in sorted(outliers)[:8])
        reasons.append(f"walkable squares look blocked or changed: {len(outliers)} ({where})")
    # the white sketch margin must never show inside the map: then the painter
    # shrank or shifted the layout (rooms smaller than their squares)
    leak = [c for c, v in cols.items() if min(v) > 225 and kinds[c] != "floor" or
            (min(v) > 235 and surface.get(c) not in ("snow", "sand", "ice"))]
    checks["margin"] = {"white_squares": len(leak), "ok": len(leak) <= max(1, len(cols) // 100)}
    if not checks["margin"]["ok"]:
        from grid_map import format_coord
        reasons.append("the layout was shrunk or shifted (margin colour inside the map at "
                       + ", ".join(format_coord(*c) for c in sorted(leak)[:6]) + ")")
    # large furniture (2+ squares: tables, counters, fireplaces) must stand out from the
    # floor (else it was moved or left out); a single chair on a wooden floor often does not
    big = {(r["x"] + i, r["y"] + j) for r in (m.get("layers") or {}).get("objects") or []
           if r.get("w", 1) * r.get("h", 1) >= 2 and map_v2.object_props(r)[0]
           for i in range(r.get("w", 1)) for j in range(r.get("h", 1))}
    obj = [c for c, k in kinds.items() if k == "object" and c in big]
    if len(obj) >= 4 and groups:
        far = 0
        for c in obj:
            g = groups.get(surface.get(c)) or max(groups.values(), key=len)
            gc = _centre(cols[x] for x in g)
            spread = sorted(_dist(cols[x], gc) for x in g)[len(g) // 2]
            if _dist(cols[c], gc) > max(18.0, 1.6 * spread):
                far += 1
        share_obj = far / len(obj)
        need = float(limits.get("min_object_share", 0.6))
        checks["object"]["visible"] = round(share_obj, 2)
        checks["object"]["ok"] = share_obj >= need
        if share_obj < need:
            reasons.append(f"large furniture: only {share_obj:.0%} of {len(obj)} squares look like furniture")
    ok = not reasons
    return {"ok": ok, "checks": checks, "reason": "; ".join(reasons) if reasons else ""}


# -- storage --------------------------------------------------------------------

def _root() -> str:
    from asset_store import AssetStore
    return os.path.join(AssetStore().global_root, "battlemaps")


def _paths(map_id: str, root: Optional[str] = None) -> "tuple[str, str]":
    r = root or _root()
    return os.path.join(r, f"{map_id}.jpg"), os.path.join(r, f"{map_id}.json")


def read_meta(map_id: str, root: Optional[str] = None) -> Optional[dict]:
    try:
        with open(_paths(map_id, root)[1], encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def current(m: dict, root: Optional[str] = None, cfg: Optional[dict] = None) -> "tuple[Optional[str], Optional[dict]]":
    """(URL of the painted map for this layout, its meta) - URL None while there is
    no fitting painting (not made yet, layout changed, or failed)."""
    if not m or m.get("temporary"):
        return None, None
    cfg = cfg or _style_cfg()
    meta = read_meta(m["id"], root)
    want = layout_hash(m, cfg.get("active", "neutral"), (cfg.get("painted_map") or {}).get("prompt_version", 1))
    if not meta or meta.get("hash") != want:
        return None, None
    img = _paths(m["id"], root)[0]
    if meta.get("ok") and os.path.isfile(img):
        return f"/assets/file/global/battlemaps/{m['id']}.jpg?v={int(os.path.getmtime(img))}", meta
    return None, meta


def recheck(m: dict, root: Optional[str] = None) -> dict:
    """Check the stored, turned-down picture again with the current rules (costs nothing);
    if it fits now, it becomes the place's painted map."""
    img_path, meta_path = _paths(m["id"], root)
    rejected = img_path.replace(".jpg", ".rejected.jpg")
    meta = read_meta(m["id"], root)
    if not meta or not os.path.isfile(rejected):
        raise ValueError(f"no turned-down picture for '{m['id']}'")
    from PIL import Image
    cfg = _style_cfg()
    check = verify(m, Image.open(rejected).convert("RGB"), (cfg.get("painted_map") or {}).get("verify"))
    meta["checks"] = check["checks"]
    if check["ok"]:
        os.replace(rejected, img_path)
        meta.update(ok=True, rechecked=time.strftime("%Y-%m-%dT%H:%M:%S"))
        meta.pop("reason", None)
    else:
        meta["reason"] = check["reason"]
    _write(meta_path, json.dumps(meta, ensure_ascii=False, indent=1).encode("utf-8"))
    return meta


def _write(path: str, data: bytes) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "wb") as f:
        f.write(data)
    os.replace(tmp, path)


def paint(m: dict, provider=None, budget=None, force: bool = False, root: Optional[str] = None,
          log: Callable[[str], None] = print, max_eur: Optional[float] = None) -> dict:
    """Paint one map (sketch -> Gemini -> check, up to max_attempts). Returns the stored meta."""
    cfg = _style_cfg()
    pm = cfg.get("painted_map") or {}
    limits = pm.get("verify") or {}
    attempts = int(limits.get("max_attempts", 3))
    h = layout_hash(m, cfg.get("active", "neutral"), pm.get("prompt_version", 1))
    old = read_meta(m["id"], root)
    if old and old.get("hash") == h and not force:
        return old
    if provider is None or budget is None:
        from image_providers import load_provider
        from asset_pipeline import provider_config
        from gemini_budget import Budget
        pcfg = provider_config().get()
        if pm.get("model"):                     # painted maps use their own (stronger) model
            pcfg.setdefault("providers", {}).setdefault("gemini", {})["model"] = pm["model"]
        provider = provider or load_provider("gemini", pcfg)
        budget = budget or Budget()
    ok_av, why = provider.available()
    if not ok_av:
        raise RuntimeError(f"no painter: {why}")
    png, box, ratio = sketch(m)
    from PIL import Image
    canvas = Image.open(io.BytesIO(png)).size
    prompt = build_prompt(m, cfg)
    cost = budget.image_cost(getattr(provider, "model", None)) if getattr(provider, "paid", False) else 0.0
    from image_providers import ImageRequest
    spent, best, tries = 0.0, None, 0
    meta = {"map_id": m["id"], "hash": h, "style": cfg.get("active", "neutral"), "ok": False,
            "model": getattr(provider, "model", None)}
    for attempt in range(1, attempts + 1):
        if max_eur is not None and spent + cost > max_eur + 1e-9:
            meta["reason"] = f"stopped at the cost limit of {max_eur:.2f} EUR"
            break
        if cost:
            budget.check(cost)
        extra = "" if attempt == 1 else (" IMPORTANT: the previous painting moved walls or water. Follow the "
                                         "sketch's floor plan exactly, square for square.")
        req = ImageRequest(prompt=prompt + extra, kind="map", width=canvas[0], height=canvas[1],
                           reference=png, reference_mime="image/png")
        tries = attempt
        try:
            result = provider.generate(req)
        except Exception as e:                                   # noqa: BLE001
            log(f"  attempt {attempt}: painter failed - {e}")
            continue
        if cost:
            budget.charge("image", cost, detail=f"battlemap:{m['id']}")
            spent += cost
        try:
            crop = crop_to_map(result.data, canvas, box)
        except Exception as e:                                   # noqa: BLE001
            log(f"  attempt {attempt}: unreadable picture - {e}")
            continue
        check = verify(m, crop, limits)
        log(f"  attempt {attempt}: {'fits' if check['ok'] else 'does not fit'} {json.dumps(check['checks'])}")
        if best is None or check["ok"]:
            best = (crop, check)
        if check["ok"]:
            break
    img_path, meta_path = _paths(m["id"], root)
    meta.update(attempts=tries, cost_eur=round(spent, 4), created=time.strftime("%Y-%m-%dT%H:%M:%S"))
    if best is not None:
        crop, check = best
        meta["checks"] = check["checks"]
        buf = io.BytesIO()
        crop.save(buf, "JPEG", quality=90)
        if check["ok"]:
            meta["ok"] = True
            meta.pop("reason", None)
            _write(img_path, buf.getvalue())
        else:
            meta["reason"] = meta.get("reason") or check["reason"]
            _write(img_path.replace(".jpg", ".rejected.jpg"), buf.getvalue())
    else:
        meta["reason"] = meta.get("reason") or "no usable picture"
    _write(meta_path, json.dumps(meta, ensure_ascii=False, indent=1).encode("utf-8"))
    return meta


class Painter:
    """Background painting, one map at a time (like the other pictures).
    `on_done(map_id, meta)` runs after every finished map."""

    def __init__(self, on_done: Callable[[str, dict], None], enabled: Callable[[], bool], log=print):
        self._on_done, self._enabled, self._log = on_done, enabled, log
        self._todo: "list[dict]" = []
        self._busy: "set[str]" = set()
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def request(self, m: dict) -> bool:
        """Queue `m` unless it is painted for this layout, failed for it, or already queued."""
        if not m or m.get("temporary") or not self._enabled():
            return False
        url, meta = current(m)
        if url or meta:
            return False
        with self._lock:
            if m["id"] in self._busy:
                return False
            self._busy.add(m["id"])
            self._todo.append(m)
        self._wake.set()
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, name="battlemap-painter", daemon=True)
            self._thread.start()
        return True

    def _run(self) -> None:
        while True:
            self._wake.wait()
            with self._lock:
                job = self._todo.pop(0) if self._todo else None
                if not self._todo:
                    self._wake.clear()
            if job is None:
                continue
            try:
                meta = paint(job, log=lambda s: self._log(f"[battlemap {job['id']}] {s}"))
            except Exception as e:                               # noqa: BLE001
                meta = {"map_id": job["id"], "ok": False, "reason": str(e)}
            with self._lock:
                self._busy.discard(job["id"])
            try:
                self._on_done(job["id"], meta)
            except Exception:                                    # noqa: BLE001
                pass


# -- command line ---------------------------------------------------------------

def _load(map_id: str) -> dict:
    import tempfile
    import grid_map
    from paths import maps_library_dir
    m = grid_map.MapStore(str(maps_library_dir()), tempfile.mkdtemp()).load(map_id)
    if m is None:
        raise SystemExit(f"no map '{map_id}' in the library")
    return m


def main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description="Painted battle maps from the logic map.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("sketch"); s.add_argument("map_id"); s.add_argument("--out", required=True)
    p = sub.add_parser("paint"); p.add_argument("map_id"); p.add_argument("--force", action="store_true")
    p.add_argument("--max-eur", type=float, default=0.25)
    st = sub.add_parser("status"); st.add_argument("map_id", nargs="?")
    rc = sub.add_parser("recheck", help="check the turned-down picture again (free)"); rc.add_argument("map_id")
    args = ap.parse_args(argv)
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    if args.cmd == "sketch":
        png, box, ratio = sketch(_load(args.map_id))
        with open(args.out, "wb") as f:
            f.write(png)
        print(f"sketch {args.out} (aspect {ratio}, map box {box})")
        return 0
    if args.cmd == "paint":
        meta = paint(_load(args.map_id), force=args.force, max_eur=args.max_eur)
        print(json.dumps(meta, ensure_ascii=False, indent=1))
        return 0 if meta.get("ok") else 1
    if args.cmd == "recheck":
        meta = recheck(_load(args.map_id))
        print(f"{args.map_id}: {'fits now - shown painted' if meta.get('ok') else 'still does not fit: ' + meta.get('reason', '')}")
        return 0 if meta.get("ok") else 1
    root = _root()
    ids = [args.map_id] if args.map_id else sorted(f[:-5] for f in os.listdir(root) if f.endswith(".json")) \
        if os.path.isdir(root) else []
    for i in ids:
        meta = read_meta(i) or {}
        print(f"{i}: {'painted' if meta.get('ok') else 'not painted'} {meta.get('reason', '')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())