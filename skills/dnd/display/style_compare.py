#!/usr/bin/env python3
"""style_compare.py - map style comparison (Phase 1, B3).

For every candidate style in config/map-style.json (keys with "candidate":
true) this makes a small test set with Gemini - four ground textures (grass,
forest floor, water, road) and two map objects (tree, rock, cut out from a
magenta background) - and builds one comparison page: the same test map
(a river through a forest with a road and a bridge) drawn by the display's
own renderer in every style, side by side, with the test set below.

    python3 style_compare.py --dry-run            # what would be made, and the cost
    python3 style_compare.py                       # make missing pictures (<= --max-eur), build the page
    python3 style_compare.py --page-only           # only rebuild the page

Pictures go to <data-root>/assets/styles/<style>/, the page to
<data-root>/stilvergleich/stilvergleich.html. The page is one self-contained
file (PixiJS, the renderer and the pictures are inside it): open it in any
browser, nothing is loaded from anywhere.

Costs: every picture goes through gemini_budget (the monthly cap and the
ledger); this run additionally stops at --max-eur (default 1.50 EUR).
Pictures that already exist are never made again.
"""
from __future__ import annotations

import argparse
import base64
import io
import json
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
for _p in (_HERE, os.path.join(_HERE, os.pardir, "scripts")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

from config_loader import ConfigFile                                    # noqa: E402

TEXTURES = {
    "grass": "short meadow grass with a few tiny wild flowers",
    "earth": "dark forest floor with fallen leaves, moss and pine needles",
    "water": "a calm river surface with gentle ripples and a hint of depth",
    "road": "a packed dirt road with small stones and faint wheel ruts",
}
SPRITES = {
    "tree": "the round crown of a single leafy deciduous tree",
    "rock": "a single grey boulder",
}


def data_root() -> str:
    from paths import _root as root   # the same data root the display uses
    return str(root())


def texture_prompt(style: dict, rules: dict, subject: str) -> str:
    return (f"Seamless tileable texture of {subject}, seen straight from above (top-down), "
            f"filling the whole square edge to edge, even lighting. Style: {style['prompt']}. "
            f"{rules.get('light', '')}. No objects, no people, no text, no letters, no grid lines, "
            f"no border, no frame, no vignette.")


def sprite_prompt(style: dict, rules: dict, subject: str) -> str:
    return (f"{subject}, seen straight from above (top-down) for a tabletop battle map, centred, "
            f"filling most of the frame. Style: {style['prompt']}. {rules.get('light', '')}. "
            f"On a flat solid pure magenta (#FF00FF) background, no shadow on the background, "
            f"no magenta on the object, no text, no border.")


def make_seamless(data: bytes, size: int = 256) -> bytes:
    """Downscale and blend with a half-offset copy so the texture tiles without seams."""
    from PIL import Image, ImageChops
    with Image.open(io.BytesIO(data)) as src:
        img = src.convert("RGB").resize((size, size), Image.LANCZOS)
    shifted = ImageChops.offset(img, size // 2, size // 2)
    mask = Image.new("L", (size, size))
    half = size / 2.0
    px = mask.load()
    for y in range(size):
        for x in range(size):
            d = min(x, y, size - 1 - x, size - 1 - y) / half      # 0 at the edge, 1 in the middle
            px[x, y] = int(255 * min(1.0, d * 2.2))
    out = Image.composite(img, shifted, mask)
    buf = io.BytesIO()
    out.save(buf, "JPEG", quality=88)
    return buf.getvalue()


def plan(styles: dict, out_dir: str) -> "list[dict]":
    jobs = []
    for sid, st in styles.items():
        for name, subject in TEXTURES.items():
            path = os.path.join(out_dir, sid, f"{name}.jpg")
            if not os.path.exists(path):
                jobs.append({"style": sid, "name": name, "kind": "texture", "subject": subject, "path": path})
        for name, subject in SPRITES.items():
            path = os.path.join(out_dir, sid, f"{name}.png")
            if not os.path.exists(path):
                jobs.append({"style": sid, "name": name, "kind": "sprite", "subject": subject, "path": path})
    return jobs


def generate(jobs, styles, rules, max_eur: float, log=print) -> "tuple[int, float]":
    from image_providers import ImageRequest, load_provider
    from asset_pipeline import chroma_key, provider_config
    from gemini_budget import Budget, BudgetExceeded
    cfg = provider_config().get()
    prov = load_provider("gemini", cfg)
    ok, why = prov.available()
    if not ok:
        log(f"Gemini not available: {why}")
        return 0, 0.0
    budget = Budget()
    cost = budget.image_cost(getattr(prov, "model", None))
    made, spent = 0, 0.0
    for job in jobs:
        if spent + cost > max_eur + 1e-9:
            log(f"stopped: this run's limit of {max_eur:.2f} EUR reached ({made} pictures made)")
            break
        try:
            budget.check(cost)
        except BudgetExceeded as e:
            log(f"stopped by the monthly Gemini cap: {e}")
            break
        st = styles[job["style"]]
        prompt = (texture_prompt if job["kind"] == "texture" else sprite_prompt)(st, rules, job["subject"])
        try:
            result = prov.generate(ImageRequest(prompt=prompt, kind="map" if job["kind"] == "texture" else "sprite",
                                                width=1024, height=1024))
        except Exception as e:                                  # noqa: BLE001 - one failure, go on
            log(f"  {job['style']}/{job['name']}: failed - {e}")
            continue
        budget.charge("image", cost, detail=f"style:{job['style']}:{job['name']}")
        spent += cost
        try:
            if job["kind"] == "texture":
                data = make_seamless(result.data)
            else:
                data, _ = chroma_key(result.data)
                from PIL import Image
                with Image.open(io.BytesIO(data)) as im:
                    im.thumbnail((256, 256), Image.LANCZOS)
                    buf = io.BytesIO()
                    im.save(buf, "PNG", optimize=True)
                    data = buf.getvalue()
        except Exception as e:                                  # noqa: BLE001
            log(f"  {job['style']}/{job['name']}: picture unusable - {e}")
            continue
        os.makedirs(os.path.dirname(job["path"]), exist_ok=True)
        with open(job["path"], "wb") as f:
            f.write(data)
        made += 1
        log(f"  {job['style']}/{job['name']}: ok")
    return made, spent


def test_map() -> dict:
    """A river through a forest with a road and a bridge (the comparison map)."""
    import grid_map
    trees = ["B2", "D1", "F3", "C5", "E10", "B12", "D13", "N2", "Q1", "S3", "P10", "R12", "T9", "Q13", "M12", "G12"]
    return grid_map.normalize_map({
        "format": 2, "id": "stilvergleich", "name": "Furt im Wald", "cols": 22, "rows": 14, "floor": "grass",
        "layers": {
            "ground": [{"at": "A1", "w": 7, "h": 6, "type": "earth"}, {"at": "A10", "w": 8, "h": 5, "type": "earth"},
                       {"at": "M1", "w": 10, "h": 4, "type": "earth"}, {"at": "O9", "w": 8, "h": 6, "type": "earth"}],
            "terrain": [], "walls": [],
            "objects": [{"at": t, "w": 2, "h": 2, "type": "tree"} for t in trees]
                       + [{"at": "H4", "type": "rock"}, {"at": "P6", "type": "rock"}, {"at": "F9", "type": "rock"},
                          {"at": "J7", "w": 3, "h": 1, "type": "bridge"},
                          {"at": "A7", "type": "exit", "label": "Zum Dorf"}, {"at": "V7", "type": "exit", "label": "Zur Burg"}],
            "paths": [{"type": "river", "points": ["K1", "K5", "J8", "L11", "L14"], "width": 2},
                      {"type": "road", "points": ["A7", "V7"]}]},
        "tokens": [{"name": "Grosch", "kind": "pc", "at": "E7"}, {"name": "Mordos", "kind": "pc", "at": "F8"},
                   {"name": "Zarith", "kind": "pc", "at": "D8"}, {"name": "Wolf", "kind": "enemy", "at": "O6"},
                   {"name": "Wolf", "kind": "enemy", "at": "P8"}, {"name": "Fuhrmann", "kind": "npc", "at": "S7"},
                   {"name": "Kiste", "kind": "object", "at": "H6"}]})


def _data_uri(path: str) -> str:
    mime = "image/png" if path.endswith(".png") else "image/jpeg"
    with open(path, "rb") as f:
        return f"data:{mime};base64," + base64.b64encode(f.read()).decode("ascii")


def build_page(styles: dict, all_styles: dict, out_dir: str, page: str) -> str:
    import map_v2
    m = test_map()
    blocked = sorted([x, y] for x, y in map_v2.blockers(m))
    shown = []
    for sid in list(styles) + ["neutral"]:
        st = dict(all_styles[sid], id=sid)
        tex, spr = {}, {}
        for name in TEXTURES:
            p = os.path.join(out_dir, sid, f"{name}.jpg")
            if os.path.exists(p):
                tex[name] = _data_uri(p)
        for name in SPRITES:
            p = os.path.join(out_dir, sid, f"{name}.png")
            if os.path.exists(p):
                spr[name] = _data_uri(p)
        if "earth" in tex:
            tex.setdefault("dirt", tex["earth"])
        if "road" in tex:
            tex.setdefault("path", tex["road"])
        st["textures"], st["sprites"] = tex, spr
        shown.append(st)
    vendor = os.path.join(_HERE, "static", "vendor", "pixi.min.js")
    render = os.path.join(_HERE, "static", "js", "map-render.js")
    with open(vendor, encoding="utf-8") as f:
        pixi_js = f.read()
    with open(render, encoding="utf-8") as f:
        render_js = f.read()
    html = PAGE.replace("/*PIXI*/", pixi_js.replace("</script", "<\\/script")) \
               .replace("/*RENDER*/", render_js.replace("</script", "<\\/script")) \
               .replace("/*DATA*/", json.dumps({"map": m, "blocked": blocked, "styles": shown},
                                                ensure_ascii=False).replace("</", "<\\/"))
    os.makedirs(os.path.dirname(page), exist_ok=True)
    with open(page, "w", encoding="utf-8") as f:
        f.write(html)
    return page


PAGE = """<!doctype html>
<html lang="de"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Stilvergleich Karte</title>
<style>
:root { --bg:#17130f; --card:#221c16; --ink:#efe6cf; --muted:#b9ab8c; --line:rgba(232,200,130,.25); }
@media (prefers-color-scheme: light) { :root:not([data-theme="dark"]) { --bg:#f3ecdc; --card:#fffaf0; --ink:#2b2117; --muted:#6b5a43; --line:rgba(70,50,20,.2); } }
body { margin:0; background:var(--bg); color:var(--ink); font:15px/1.5 "Segoe UI",Arial,sans-serif; }
main { max-width:1500px; margin:0 auto; padding:24px 16px 48px; }
h1 { font-size:26px; margin:0 0 6px; } .lead { color:var(--muted); margin:0 0 20px; max-width:900px; }
.grid { display:grid; grid-template-columns:repeat(auto-fit,minmax(640px,1fr)); gap:18px; }
.card { background:var(--card); border:1px solid var(--line); border-radius:12px; padding:14px; }
.card h2 { font-size:19px; margin:0 0 2px; } .card .id { color:var(--muted); font-size:13px; }
.card p { margin:4px 0 10px; color:var(--muted); }
.map { overflow:auto; } .map canvas { display:block; max-width:100%; height:auto; border-radius:6px; }
.set { display:flex; gap:10px; flex-wrap:wrap; margin-top:10px; }
.set figure { margin:0; text-align:center; font-size:12px; color:var(--muted); }
.set img { width:84px; height:84px; object-fit:cover; border-radius:6px; border:1px solid var(--line); display:block; background:#888; }
.set img.sprite { object-fit:contain; background:repeating-conic-gradient(#8885 0 25%, #0000 0 50%) 50%/16px 16px; }
.missing { color:#d9806b; font-size:13px; }
.how { margin-top:22px; padding:12px 14px; border:1px dashed var(--line); border-radius:10px; }
</style></head><body><main>
<h1>Stilvergleich: Furt im Wald</h1>
<p class="lead">Dieselbe Karte in vier Malstilen, gezeichnet vom Display selbst. Ein Fluss läuft durch einen Wald, eine Straße kreuzt ihn über eine Brücke. Darunter der kleine Testsatz jedes Stils (Böden, Baum, Fels). Ganz unten zum Vergleich der neutrale Platzhalter, der bis zur Entscheidung benutzt wird. Achte auf zwei Dinge: Gefällt der Stil? Und sind Wasser, Wege, Hindernisse und Figuren auf einen Blick zu unterscheiden?</p>
<div class="grid" id="grid"></div>
<div class="how"><b>So wählst du:</b> Sag Claude zum Beispiel „Stil: aquarell“. Dann wird in <code>config/map-style.json</code> der Eintrag <code>active</code> umgestellt, und die übrigen Bodenarten und Objekte werden in diesem Stil erzeugt.</div>
</main>
<script>/*PIXI*/</script>
<script>/*RENDER*/</script>
<script>
const DATA = /*DATA*/;
const grid = document.getElementById('grid');
const LABELS = { grass: 'Wiese', earth: 'Waldboden', water: 'Wasser', road: 'Straße', tree: 'Baum', rock: 'Fels' };
for (const st of DATA.styles) {
  const card = document.createElement('section');
  card.className = 'card';
  card.innerHTML = '<h2></h2><div class="id"></div><p></p><div class="map"></div><div class="set"></div>';
  card.querySelector('h2').textContent = st.name;
  card.querySelector('.id').textContent = st.id === 'neutral' ? 'Platzhalter (ohne Bilder)' : 'Stil-Kennung: ' + st.id;
  card.querySelector('p').textContent = st.description || '';
  const set = card.querySelector('.set');
  const pics = Object.assign({}, st.textures, st.sprites);
  let count = 0;
  for (const k of ['grass', 'earth', 'water', 'road', 'tree', 'rock']) {
    if (!pics[k]) continue;
    count++;
    const f = document.createElement('figure');
    const img = document.createElement('img');
    img.src = pics[k]; img.alt = LABELS[k];
    if (k === 'tree' || k === 'rock') img.className = 'sprite';
    f.appendChild(img);
    const cap = document.createElement('figcaption'); cap.textContent = LABELS[k]; f.appendChild(cap);
    set.appendChild(f);
  }
  if (!count && st.id !== 'neutral') { const s = document.createElement('div'); s.className = 'missing'; s.textContent = 'Testsatz noch nicht erzeugt.'; set.appendChild(s); }
  grid.appendChild(card);
  const r = new MapRenderer(card.querySelector('.map'), {});
  const draw = () => r.render(DATA.map, { cell: 30, gutter: 24, images: {}, style: st, blocked: DATA.blocked,
                                          turn: 'Grosch', players: [], moves: [], range: false });
  draw();
  setTimeout(draw, 400);     // textures from data URIs are ready a moment later
}
</script></body></html>
"""


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Map style comparison: test sets and one comparison page.")
    ap.add_argument("--styles", help="comma-separated style ids (default: every candidate)")
    ap.add_argument("--dry-run", action="store_true", help="show what would be made and the cost")
    ap.add_argument("--page-only", action="store_true", help="only rebuild the page")
    ap.add_argument("--max-eur", type=float, default=1.5, help="stop this run at this cost (default 1.50)")
    ap.add_argument("--out-dir", help="default <data-root>/assets/styles")
    ap.add_argument("--page", help="default <data-root>/stilvergleich/stilvergleich.html")
    args = ap.parse_args(argv)
    cfg = ConfigFile("map-style.json").get()
    all_styles = cfg.get("styles") or {}
    rules = cfg.get("rules") or {}
    wanted = [s.strip() for s in args.styles.split(",")] if args.styles else \
        [k for k, v in all_styles.items() if v.get("candidate")]
    unknown = [s for s in wanted if s not in all_styles]
    if unknown:
        print(f"unknown style(s): {', '.join(unknown)}", file=sys.stderr)
        return 2
    styles = {k: all_styles[k] for k in wanted}
    root = data_root()
    out_dir = args.out_dir or os.path.join(root, "assets", "styles")
    page = args.page or os.path.join(root, "stilvergleich", "stilvergleich.html")
    if not args.page_only:
        jobs = plan(styles, out_dir)
        if args.dry_run:
            from gemini_budget import Budget
            from asset_pipeline import provider_config
            model = ((provider_config().get().get("providers") or {}).get("gemini") or {}).get("model")
            cost = Budget().image_cost(model)
            for j in jobs:
                print(f"  {j['style']}/{j['name']} ({j['kind']})")
            print(f"{len(jobs)} pictures, about {len(jobs) * cost:.2f} EUR (cap for this run {args.max_eur:.2f} EUR)")
            return 0
        made, spent = generate(jobs, styles, rules, args.max_eur)
        print(f"{made} pictures made for {spent:.2f} EUR.")
    print("page:", build_page(styles, all_styles, out_dir, page))
    return 0


if __name__ == "__main__":
    sys.exit(main())