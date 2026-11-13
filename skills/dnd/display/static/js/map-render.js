/* map-render.js — the battle map drawn with PixiJS (Phase 1).
 *
 * Draws a format-2 map (grid_map.py / map_v2.py) layer by layer:
 *   ground   base floor + ground patches   (textured, soft rounded edges)
 *   terrain  water, roads, chasms …         (autotiled: one continuous shape per
 *                                            type, banks/edges along the outline,
 *                                            rounded outer and inner corners)
 *   walls    autotiled blocks: lit top-left edges, shadow to the lower right
 *   objects  pictures from the asset system, else a drawn symbol; exits,
 *            doors and stairs get a marker and a German label
 *   figures  shape by kind (circle = player, square = NPC, diamond = enemy,
 *            hexagon = object) + colour + letter badge: readable without colour
 * plus the grid, coordinates (A1 top left), the reach of the active figure,
 * the trail of the last move and the turn marker.
 *
 * Pure helpers are exported on MapRenderer._helpers for node tests; the
 * renderer itself needs PIXI (vendored, static/vendor/pixi.min.js) and a DOM.
 * Nothing here talks to the network.
 */
(function () {
  'use strict';

  const KINDS = {
    pc:     { shape: 'circle',  glyph: 'S', label: 'Spielerfigur',      key: 'pc' },
    npc:    { shape: 'square',  glyph: 'N', label: 'Nichtspielerfigur', key: 'npc' },
    enemy:  { shape: 'diamond', glyph: 'G', label: 'Gegner',            key: 'enemy' },
    object: { shape: 'hexagon', glyph: 'O', label: 'Gegenstand',        key: 'object_token' },
  };

  // German names and what you can do with a thing (tooltips, legend).
  const THINGS = {
    wall: ['Wand', 'Undurchdringlich. Volle Deckung dahinter.'],
    bars: ['Gitter', 'Man sieht hindurch, kommt aber nicht durch.'],
    palisade: ['Palisade', 'Holzwall. Undurchdringlich.'],
    fence: ['Zaun', 'Blockiert. Kann überklettert werden (Athletik).'],
    hedge: ['Hecke', 'Blockiert die Sicht und den Weg.'],
    water: ['Wasser', 'Nicht begehbar. Schwimmen kostet doppelte Bewegung.'],
    'deep-water': ['Tiefes Wasser', 'Nicht begehbar. Nur schwimmend.'],
    'shallow-water': ['Seichtes Wasser', 'Begehbar, schwieriges Gelände.'],
    road: ['Straße', 'Begehbar.'],
    path: ['Weg', 'Begehbar.'],
    difficult: ['Schwieriges Gelände', 'Kostet doppelte Bewegung.'],
    cliff: ['Klippe', 'Nicht begehbar. Klettern nötig.'],
    chasm: ['Abgrund', 'Nicht begehbar. Wer hineinfällt, stürzt.'],
    lava: ['Lava', 'Nicht begehbar. Feuerschaden.'],
    ice: ['Eis', 'Rutschig: schwieriges Gelände.'],
    rubble: ['Geröll', 'Schwieriges Gelände.'],
    swamp: ['Sumpf', 'Schwieriges Gelände.'],
    table: ['Tisch', 'Hindernis. Halbe Deckung, kann umgestoßen werden.'],
    chair: ['Stuhl', 'Hindernis. Kann man wegschieben.'],
    bed: ['Bett', 'Hindernis.'],
    bar: ['Theke', 'Hindernis. Halbe Deckung.'],
    door: ['Tür', 'Durchgang. Öffnen kostet keine Aktion.'],
    gate: ['Tor', 'Durchgang.'],
    stairs: ['Treppe', 'Durchgang nach oben oder unten.'],
    ladder: ['Leiter', 'Klettern: halbe Geschwindigkeit.'],
    trapdoor: ['Falltür', 'Durchgang nach unten.'],
    exit: ['Ausgang', 'Hier geht es hinaus.'],
    bridge: ['Brücke', 'Begehbar, führt über Wasser oder Abgrund.'],
    ford: ['Furt', 'Begehbar, seichte Stelle.'],
    plank: ['Planke', 'Schmal: Geschicklichkeit bei Gefahr.'],
    rug: ['Teppich', 'Begehbar.'],
    carpet: ['Teppich', 'Begehbar.'],
    tree: ['Baum', 'Hindernis. Deckung, kann erklettert werden.'],
    bush: ['Busch', 'Hindernis, Sichtschutz.'],
    rock: ['Felsen', 'Hindernis. Deckung.'],
    pillar: ['Säule', 'Hindernis. Deckung.'],
    well: ['Brunnen', 'Hindernis. Tief.'],
    fireplace: ['Kamin', 'Hindernis. Heiß.'],
    fire: ['Feuer', 'Nicht betreten: Feuerschaden.'],
    stall: ['Marktstand', 'Hindernis. Halbe Deckung.'],
    tent: ['Zelt', 'Hindernis.'],
    wagon: ['Wagen', 'Hindernis. Deckung.'],
    log: ['Baumstamm', 'Hindernis. Halbe Deckung.'],
    chest: ['Truhe', 'Kann geöffnet werden.'],
    barrel: ['Fass', 'Hindernis. Halbe Deckung.'],
    crate: ['Kiste', 'Hindernis. Halbe Deckung.'],
    altar: ['Altar', 'Hindernis.'],
    statue: ['Statue', 'Hindernis. Deckung.'],
    shrine: ['Schrein', 'Hindernis.'],
  };
  const EXITS = ['exit', 'door', 'stairs', 'ladder', 'trapdoor', 'gate'];
  const TERRAIN_ORDER = ['mud', 'swamp', 'rubble', 'path', 'road', 'shallow-water', 'water', 'deep-water',
                         'ice', 'lava', 'chasm', 'cliff', 'difficult'];
  const WATERY = ['water', 'deep-water', 'shallow-water', 'swamp'];
  const PATH_KIND = { river: 'water', stream: 'shallow-water', creek: 'shallow-water', road: 'road',
                      path: 'path', trail: 'path', lava: 'lava', chasm: 'chasm' };

  // ── pure helpers (node-tested) ─────────────────────────────────────────

  function key(x, y) { return x + ',' + y; }

  function rectCells(r) {
    const out = [];
    for (let dy = 0; dy < (r.h || 1); dy++) for (let dx = 0; dx < (r.w || 1); dx++) out.push([r.x + dx, r.y + dy]);
    return out;
  }

  /* 4-connected line between two cells (same rule as map_v2._line). */
  function line(a, b) {
    let x = a[0], y = a[1];
    const x1 = b[0], y1 = b[1];
    const dx = Math.abs(x1 - x), dy = Math.abs(y1 - y), sx = x1 > x ? 1 : -1, sy = y1 > y ? 1 : -1;
    let err = dx - dy;
    const out = [[x, y]];
    while (x !== x1 || y !== y1) {
      const e2 = 2 * err;
      if (x !== x1 && (e2 > -dy || y === y1)) { err -= dy; x += sx; } else { err += dx; y += sy; }
      out.push([x, y]);
    }
    return out;
  }

  /* Cells of a path (same rule as map_v2.path_cells). */
  function pathCells(path, cols, rows) {
    const pts = path.points || [];
    const width = Math.max(1, Math.min(4, path.width || 1));
    let centre = [];
    for (let i = 0; i + 1 < pts.length; i++) {
      for (const c of line(pts[i], pts[i + 1])) {
        const last = centre[centre.length - 1];
        if (!last || last[0] !== c[0] || last[1] !== c[1]) centre.push(c);
      }
    }
    if (pts.length === 1) centre = [pts[0]];
    const lo = -Math.floor((width - 1) / 2);
    const seen = new Set(), out = [];
    centre.forEach((c, i) => {
      const ref = centre[i + 1] || centre[i - 1];
      const horizontal = !!ref && ref[1] === c[1];
      for (let k = lo; k < lo + width; k++) {
        let cells = horizontal ? [[c[0], c[1] + k]] : [[c[0] + k, c[1]]];
        if (!ref) { cells = []; for (let j = lo; j < lo + width; j++) cells.push([c[0] + k, c[1] + j]); }
        for (const q of cells) {
          if (q[0] < 0 || q[1] < 0 || q[0] >= cols || q[1] >= rows || seen.has(key(q[0], q[1]))) continue;
          seen.add(key(q[0], q[1]));
          out.push(q);
        }
      }
    });
    return out;
  }

  /* {type: Set("x,y")} for ground patches and terrain incl. paths. */
  function typeCells(map) {
    const L = map.layers || {};
    const out = {};
    const add = (type, x, y) => { (out[type] = out[type] || new Set()).add(key(x, y)); };
    for (const r of L.ground || []) for (const [x, y] of rectCells(r)) add(r.type, x, y);
    for (const r of L.terrain || []) for (const [x, y] of rectCells(r)) add(r.type, x, y);
    for (const p of L.paths || []) {
      const kind = PATH_KIND[p.type] || 'road';
      for (const [x, y] of pathCells(p, map.cols, map.rows)) add(kind, x, y);
    }
    return out;
  }

  /* Rounded outer corners of a cell in a shape: [tl, tr, br, bl]. */
  function outerCorners(set, x, y) {
    const has = (a, b) => set.has(key(a, b));
    return [
      !has(x - 1, y) && !has(x, y - 1),
      !has(x + 1, y) && !has(x, y - 1),
      !has(x + 1, y) && !has(x, y + 1),
      !has(x - 1, y) && !has(x, y + 1),
    ];
  }

  /* Inner (concave) corners of an EMPTY cell next to a shape: [tl, tr, br, bl]. */
  function innerCorners(set, x, y) {
    const has = (a, b) => set.has(key(a, b));
    return [
      has(x - 1, y) && has(x, y - 1),
      has(x + 1, y) && has(x, y - 1),
      has(x + 1, y) && has(x, y + 1),
      has(x - 1, y) && has(x, y + 1),
    ];
  }

  /* Wall neighbours as a 4-bit mask N=1 E=2 S=4 W=8 (exposed edges are the zeros). */
  function wallMask(set, x, y) {
    return (set.has(key(x, y - 1)) ? 1 : 0) | (set.has(key(x + 1, y)) ? 2 : 0)
         | (set.has(key(x, y + 1)) ? 4 : 0) | (set.has(key(x - 1, y)) ? 8 : 0);
  }

  /* Cells reachable within n squares (diagonal = 1, no corner cutting) — as map_v2.reach. */
  function reachCells(cols, rows, blocked, start, n) {
    const seen = new Set([key(start[0], start[1])]);
    let frontier = [start];
    for (let step = 0; step < n; step++) {
      const next = [];
      for (const [cx, cy] of frontier) {
        for (let dx = -1; dx <= 1; dx++) for (let dy = -1; dy <= 1; dy++) {
          if (!dx && !dy) continue;
          const x = cx + dx, y = cy + dy, k = key(x, y);
          if (x < 0 || y < 0 || x >= cols || y >= rows || seen.has(k) || blocked.has(k)) continue;
          if (dx && dy && (blocked.has(key(cx + dx, cy)) || blocked.has(key(cx, cy + dy)))) continue;
          seen.add(k);
          next.push([x, y]);
        }
      }
      frontier = next;
    }
    return seen;
  }

  /* Squares a figure may move: speed in feet / cell size (default 30 ft, 5-ft cells). */
  function speedSquares(speed, cellFt) {
    const ft = parseInt(String(speed == null ? '' : speed).replace(/[^0-9]/g, ''), 10);
    return Math.max(0, Math.floor((isNaN(ft) ? 30 : ft) / (cellFt || 5)));
  }
  /* German tooltip lines for a figure or an object. */
  function describe(target, players) {
    if (!target) return null;
    if (target.what === 'object') {
      const t = THINGS[target.type] || [target.type, ''];
      const lines = [];
      if (target.label) lines.push(t[0]);
      if (t[1]) lines.push(t[1]);
      return { title: target.label || t[0], lines: lines };
    }
    const tok = target.tok || {};
    const kind = KINDS[tok.kind] || KINDS.npc;
    const low = String(tok.name || '').toLowerCase();
    const p = (players || []).find(pl => String(pl.name || '').toLowerCase() === low);
    const st = tok.stats || {};
    const lines = [kind.label];
    const hp = p && p.hp && p.hp.current != null ? p.hp : st.hp;
    if (hp && hp.current != null) lines.push('Trefferpunkte ' + hp.current + (hp.max != null ? ' / ' + hp.max : ''));
    const conds = (p && p.conditions && p.conditions.length) ? p.conditions : (st.conditions || []);
    lines.push(conds.length ? 'Zustand: ' + conds.join(', ') : 'Zustand: unversehrt');
    const what = { pc: 'Klick: Werte ansehen', npc: 'Ansprechen, handeln, folgen', enemy: 'Angreifen, ausweichen, verhandeln',
                   object: 'Untersuchen, benutzen' }[tok.kind] || '';
    if (what) lines.push(what);
    return { title: tok.name, lines: lines };
  }

  function legendItems() {
    return [
      { shape: 'circle', key: 'pc', text: 'Spielerfigur (S)' },
      { shape: 'square', key: 'npc', text: 'Nichtspielerfigur (N)' },
      { shape: 'diamond', key: 'enemy', text: 'Gegner (G)' },
      { shape: 'hexagon', key: 'object_token', text: 'Gegenstand (O)' },
      { mark: 'turn', text: 'Goldener Ring: ist am Zug' },
      { mark: 'exit', text: 'Pfeil: Ausgang, Tür, Treppe' },
      { mark: 'wall', text: 'Dunkle Blöcke: Wände' },
      { mark: 'water', text: 'Blau mit Ufer: Wasser (nicht begehbar)' },
      { mark: 'range', text: 'Blaue Felder: Reichweite der aktiven Figur' },
    ];
  }

  function hex(c, fallback) {
    if (typeof c === 'number') return c;
    const m = /^#?([0-9a-f]{6})/i.exec(String(c || ''));
    return m ? parseInt(m[1], 16) : (fallback == null ? 0x777777 : fallback);
  }

  function shade(c, f) {           // f > 0 lighter, f < 0 darker
    const r = (c >> 16) & 255, g = (c >> 8) & 255, b = c & 255;
    const t = f < 0 ? 0 : 255, p = Math.abs(f);
    return (Math.round((t - r) * p) + r) << 16 | (Math.round((t - g) * p) + g) << 8 | (Math.round((t - b) * p) + b);
  }

  function rng(seed) {             // small deterministic generator for textures
    let s = seed >>> 0 || 1;
    return () => { s ^= s << 13; s ^= s >>> 17; s ^= s << 5; return ((s >>> 0) % 10000) / 10000; };
  }

  function hashStr(s) {
    let h = 2166136261;
    for (let i = 0; i < s.length; i++) { h ^= s.charCodeAt(i); h = Math.imul(h, 16777619); }
    return h >>> 0;
  }

  function colLabel(x) {
    let label = '', n = x + 1;
    while (n > 0) { const rem = (n - 1) % 26; label = String.fromCharCode(65 + rem) + label; n = Math.floor((n - 1) / 26); }
    return label;
  }

  function initials(name) {
    const words = String(name || '').trim().split(/[\s\-_]+/).filter(Boolean);
    if (!words.length) return '?';
    const first = words[0], last = words[words.length - 1];
    if (words.length > 1 && /^\d+$/.test(last)) return first[0].toUpperCase() + last;
    if (words.length > 1) return (first[0] + words[1][0]).toUpperCase();
    return first[0].toUpperCase() + (first[1] || '').toLowerCase();
  }

  /* How an exit is marked: {arrow: [dx, dy]} for a way out at the map's edge
     (and every "exit"), else {icon: "door" | "stairs"}. */
  function exitKind(r, cols, rows) {
    const w = r.w || 1, h = r.h || 1;
    let arrow = null;
    if (r.x === 0) arrow = [-1, 0]; else if (r.x + w >= cols) arrow = [1, 0];
    else if (r.y === 0) arrow = [0, -1]; else if (r.y + h >= rows) arrow = [0, 1];
    if (arrow) return { arrow: arrow };
    if (r.type === 'exit') return { arrow: [0, -1] };
    return { icon: (r.type === 'stairs' || r.type === 'ladder' || r.type === 'trapdoor') ? 'stairs' : 'door' };
  }

  /* Short name under a figure: "Schleusenwärter 2" stays, "Ingenieurin Vell" → "Ingenieurin". */
  function shortName(name) {
    const words = String(name || '').trim().split(/\s+/).filter(Boolean);
    if (words.length > 1 && /^\d+$/.test(words[words.length - 1])) return words[0] + ' ' + words[words.length - 1];
    const first = words[0] || '';
    return first.length > 12 ? first.slice(0, 11) + '…' : first;
  }

  /* Ids of figures whose name stays visible: players, the active figure and anyone with
     no other figure on the neighbouring squares (crowds show names on hover only). */
  function labelled(tokens, turnName) {
    const low = String(turnName || '').toLowerCase();
    const at = {};
    for (const t of tokens) {             // objects never crowd a figure's name away
      if (!t.hidden && t.x != null && t.kind !== 'object') at[key(t.x, t.y)] = (at[key(t.x, t.y)] || 0) + 1;
    }
    const out = new Set();
    for (const t of tokens) {
      if (t.hidden || t.x == null) continue;
      if (t.kind === 'pc' || String(t.name).toLowerCase() === low) { out.add(t.id); continue; }
      let crowded = false;
      for (let dx = -1; dx <= 1 && !crowded; dx++) for (let dy = -1; dy <= 1; dy++) {
        const n = at[key(t.x + dx, t.y + dy)] || 0;
        if ((dx || dy) ? n > 0 : n > 1) { crowded = true; break; }
      }
      if (!crowded) out.add(t.id);
    }
    return out;
  }

  function shapePoints(shape, r) {
    if (shape === 'diamond') return [0, -r, r, 0, 0, r, -r, 0];
    if (shape === 'hexagon') { const out = []; for (let i = 0; i < 6; i++) { const a = Math.PI / 6 + i * Math.PI / 3; out.push(Math.cos(a) * r, Math.sin(a) * r); } return out; }
    return null;
  }

  const helpers = { key, rectCells, line, pathCells, typeCells, outerCorners, innerCorners, wallMask,
                    reachCells, speedSquares, describe, legendItems, hex, shade, colLabel, shapePoints,
                    exitKind, shortName, labelled,
                    THINGS, KINDS, EXITS };

  // ── procedural textures (neutral style) ────────────────────────────────

  function proceduralCanvas(material, base, size) {
    const cv = document.createElement('canvas');
    cv.width = cv.height = size;
    const g = cv.getContext('2d');
    const r = rng(hashStr(material));
    const col = c => '#' + ('000000' + (c >>> 0).toString(16)).slice(-6);
    g.fillStyle = col(base);
    g.fillRect(0, 0, size, size);
    for (let i = 0; i < size * size / 9; i++) {          // fine grain
      g.fillStyle = col(shade(base, (r() - 0.5) * 0.18));
      g.fillRect(Math.floor(r() * size), Math.floor(r() * size), 2, 2);
    }
    g.lineWidth = 2;
    if (material === 'wood' || material === 'floor') {
      g.strokeStyle = col(shade(base, -0.28));
      for (let y = 0; y < size; y += size / 4) { g.beginPath(); g.moveTo(0, y + 0.5); g.lineTo(size, y + 0.5); g.stroke(); }
      for (let i = 0; i < 6; i++) {
        const y = Math.floor(r() * 4) * size / 4, x = Math.floor(r() * size);
        g.beginPath(); g.moveTo(x + 0.5, y); g.lineTo(x + 0.5, y + size / 4); g.stroke();
      }
    } else if (material === 'stone' || material === 'tiles') {
      g.strokeStyle = col(shade(base, -0.25));
      const s = size / 4;
      for (let row = 0; row < 4; row++) {
        g.beginPath(); g.moveTo(0, row * s + 0.5); g.lineTo(size, row * s + 0.5); g.stroke();
        for (let x = (row % 2) * s / 2; x < size; x += s) { g.beginPath(); g.moveTo(x + 0.5, row * s); g.lineTo(x + 0.5, row * s + s); g.stroke(); }
      }
    } else if (material === 'cobble' || material === 'gravel' || material === 'road' || material === 'rubble') {
      const n = material === 'cobble' ? 22 : 40;
      for (let i = 0; i < n; i++) {
        g.fillStyle = col(shade(base, (r() - 0.4) * 0.3));
        g.strokeStyle = col(shade(base, -0.3));
        const rad = (material === 'cobble' ? 6 : 2.5) + r() * (material === 'cobble' ? 5 : 3);
        g.beginPath(); g.ellipse(r() * size, r() * size, rad, rad * (0.7 + r() * 0.3), r() * 3, 0, Math.PI * 2);
        g.fill(); if (material === 'cobble') g.stroke();
      }
    } else if (material === 'grass' || material === 'moss') {
      for (let i = 0; i < 160; i++) {
        g.strokeStyle = col(shade(base, (r() - 0.3) * 0.35));
        g.lineWidth = 1.2;
        const x = r() * size, y = r() * size;
        g.beginPath(); g.moveTo(x, y); g.lineTo(x + (r() - 0.5) * 4, y - 3 - r() * 4); g.stroke();
      }
    } else if (WATERY.includes(material)) {
      g.strokeStyle = col(shade(base, 0.22));
      g.lineWidth = 1.5;
      for (let i = 0; i < 9; i++) {
        const x = r() * size, y = r() * size, w = 10 + r() * 16;
        g.beginPath(); g.arc(x, y, w, Math.PI * 1.15, Math.PI * 1.85); g.stroke();
      }
    }
    return cv;
  }

  // ── the renderer ───────────────────────────────────────────────────────

  function MapRenderer(host, opts) {
    this.host = host;
    this.opts = opts || {};
    this.app = new PIXI.Application({ backgroundAlpha: 0, antialias: true,
                                      resolution: Math.min(2, window.devicePixelRatio || 1), autoDensity: true });
    this.app.view.className = 'grid-canvas';
    host.appendChild(this.app.view);
    const C = () => new PIXI.Container();
    this.root = C();
    this.layers = { ground: C(), terrain: C(), shadow: C(), walls: C(), objects: C(), grid: C(),
                    range: C(), trail: C(), tokens: C(), labels: C() };
    for (const k of Object.keys(this.layers)) this.root.addChild(this.layers[k]);
    this.app.stage.addChild(this.root);
    this.textures = {};
    this.tokens = new Map();
    this.staticKey = null;
    this.anims = [];
    this.app.ticker.add(() => this._tick());
  }

  MapRenderer.prototype.destroy = function () {
    try { this.app.destroy(true, { children: true, texture: true }); } catch (_) {}
  };

  MapRenderer.prototype._pal = function (name, fallback) {
    const pal = (this.style && this.style.palette) || {};
    return hex(pal[name], fallback);
  };

  MapRenderer.prototype._texture = function (material) {
    const style = this.style || {};
    const k = (style.id || 'neutral') + ':' + material;
    if (this.textures[k]) return this.textures[k];
    const url = style.textures && style.textures[material];
    const tex = url ? PIXI.Texture.from(url)
                    : PIXI.Texture.from(proceduralCanvas(material, this._pal(material, 0x777777), 128));
    this.textures[k] = tex;
    return tex;
  };
  /* Filled shape of a cell set with rounded outer corners and concave fillets. */
  MapRenderer.prototype._blob = function (g, set, cs, r, grow) {
    const pad = grow || 0;
    for (const k of set) {
      const [x, y] = k.split(',').map(Number);
      const c = outerCorners(set, x, y);
      const has = (a, b) => set.has(key(a, b));
      const x0 = x * cs - (has(x - 1, y) ? 0 : pad), y0 = y * cs - (has(x, y - 1) ? 0 : pad);
      const x1 = (x + 1) * cs + (has(x + 1, y) ? 0 : pad), y1 = (y + 1) * cs + (has(x, y + 1) ? 0 : pad);
      const rr = Math.min(r + pad, (x1 - x0) / 2, (y1 - y0) / 2);
      g.moveTo(x0 + (c[0] ? rr : 0), y0);
      g.lineTo(x1 - (c[1] ? rr : 0), y0);
      if (c[1]) g.quadraticCurveTo(x1, y0, x1, y0 + rr);
      g.lineTo(x1, y1 - (c[2] ? rr : 0));
      if (c[2]) g.quadraticCurveTo(x1, y1, x1 - rr, y1);
      g.lineTo(x0 + (c[3] ? rr : 0), y1);
      if (c[3]) g.quadraticCurveTo(x0, y1, x0, y1 - rr);
      g.lineTo(x0, y0 + (c[0] ? rr : 0));
      if (c[0]) g.quadraticCurveTo(x0, y0, x0 + rr, y0);
      g.closePath();
    }
    const seen = new Set();           // concave corners: fill the inner angle of empty cells
    for (const k of set) {
      const [x, y] = k.split(',').map(Number);
      for (let dx = -1; dx <= 1; dx++) for (let dy = -1; dy <= 1; dy++) {
        const ex = x + dx, ey = y + dy, ek = key(ex, ey);
        if (set.has(ek) || seen.has(ek)) continue;
        seen.add(ek);
        const ic = innerCorners(set, ex, ey);
        const corners = [[ex * cs, ey * cs, 1, 1], [(ex + 1) * cs, ey * cs, -1, 1],
                         [(ex + 1) * cs, (ey + 1) * cs, -1, -1], [ex * cs, (ey + 1) * cs, 1, -1]];
        corners.forEach(([cx, cy, sx, sy], i) => {
          if (!ic[i]) return;
          const rr = r * 0.8 + pad;
          g.moveTo(cx, cy);
          g.lineTo(cx + sx * rr, cy);
          g.quadraticCurveTo(cx, cy, cx, cy + sy * rr);
          g.closePath();
        });
      }
    }
  };

  MapRenderer.prototype._texturedBlob = function (container, set, material, cs, r, edgeColor, edgeWidth) {
    if (!set || !set.size) return;
    if (edgeColor != null) {
      const edge = new PIXI.Graphics();
      edge.beginFill(edgeColor, 1);
      this._blob(edge, set, cs, r, edgeWidth);
      edge.endFill();
      container.addChild(edge);
    }
    const mask = new PIXI.Graphics();
    mask.beginFill(0xffffff, 1);
    this._blob(mask, set, cs, r, 0);
    mask.endFill();
    const tile = new PIXI.TilingSprite(this._texture(material), this.map.cols * cs, this.map.rows * cs);
    const tex = tile.texture;
    const w = tex.baseTexture && tex.baseTexture.valid ? tex.width : 128;
    tile.tileScale.set((cs * 2) / (w || 128));
    tile.mask = mask;
    container.addChild(mask);
    container.addChild(tile);
  };

  MapRenderer.prototype._buildStatic = function () {
    const m = this.map, cs = this.cell, L = m.layers || {};
    for (const k of ['ground', 'terrain', 'shadow', 'walls', 'objects', 'grid', 'labels']) {
      this.layers[k].removeChildren().forEach(c => c.destroy({ children: true }));
    }
    const W = m.cols * cs, H = m.rows * cs;
    const floor = new PIXI.TilingSprite(this._texture(m.floor || 'stone'), W, H);
    const ft = floor.texture, fw = ft.baseTexture && ft.baseTexture.valid ? ft.width : 128;
    floor.tileScale.set((cs * 2) / (fw || 128));
    this.layers.ground.addChild(floor);
    const types = typeCells(m);
    for (const t of Object.keys(types)) {
      if (!TERRAIN_ORDER.includes(t)) this._texturedBlob(this.layers.ground, types[t], t, cs, cs * 0.45, null, 0);
    }
    for (const t of TERRAIN_ORDER) {
      if (!types[t]) continue;
      const watery = WATERY.includes(t);
      const edge = watery ? this._pal('bank', 0xc9bb8b) : (t === 'road' || t === 'path') ? this._pal('road_edge', 0x6d5a42)
        : (t === 'chasm' || t === 'cliff') ? shade(this._pal(t, 0x222222), -0.5) : null;
      this._texturedBlob(this.layers.terrain, types[t], t, cs, cs * 0.42, edge, Math.max(2, cs * (watery ? 0.12 : 0.06)));
    }
    if (types.difficult) {     // hatch marks: "slow ground" readable without colour
      const g = new PIXI.Graphics();
      g.lineStyle(Math.max(1, cs * 0.05), 0x000000, 0.25);
      for (const k of types.difficult) {
        const [x, y] = k.split(',').map(Number);
        for (let i = 1; i < 4; i++) { g.moveTo(x * cs + i * cs / 4, y * cs + cs * 0.2); g.lineTo(x * cs + i * cs / 4 - cs * 0.15, y * cs + cs * 0.8); }
      }
      this.layers.terrain.addChild(g);
    }
    const wallSet = new Set(), barSet = new Set();
    for (const r of L.walls || []) for (const [x, y] of rectCells(r)) (r.type === 'bars' ? barSet : wallSet).add(key(x, y));
    const shadowAlpha = (this.style && this.style.shadow) || 0.32;
    const sh = new PIXI.Graphics();
    sh.beginFill(0x000000, shadowAlpha);
    const off = cs * 0.22;
    for (const k of wallSet) {
      const [x, y] = k.split(',').map(Number);
      const mask = wallMask(wallSet, x, y);
      if (!(mask & 4)) sh.drawRect(x * cs + off, (y + 1) * cs, cs, off);
      if (!(mask & 2)) sh.drawRect((x + 1) * cs, y * cs + off, off, cs);
    }
    sh.endFill();
    this.layers.shadow.addChild(sh);
    const wallCol = this._pal('wall', 0x4b4641), top = this._pal('wall_top', 0x6f685f);
    const wg = new PIXI.Graphics();
    wg.beginFill(wallCol, 1);
    for (const k of wallSet) { const [x, y] = k.split(',').map(Number); wg.drawRect(x * cs, y * cs, cs, cs); }
    wg.endFill();
    wg.lineStyle(1, shade(wallCol, -0.22), 0.7);         // masonry
    for (const k of wallSet) {
      const [x, y] = k.split(',').map(Number);
      wg.moveTo(x * cs + 2, y * cs + cs / 2); wg.lineTo((x + 1) * cs - 2, y * cs + cs / 2);
      const bx = x * cs + ((y % 2) ? cs / 2 : cs / 4);
      wg.moveTo(bx, y * cs + 2); wg.lineTo(bx, y * cs + cs / 2 - 1);
      const bx2 = x * cs + ((y % 2) ? cs / 4 : (3 * cs) / 4);
      wg.moveTo(bx2, y * cs + cs / 2 + 1); wg.lineTo(bx2, (y + 1) * cs - 2);
    }
    const lw = Math.max(1.5, cs * 0.07);
    for (const k of wallSet) {
      const [x, y] = k.split(',').map(Number);
      const mask = wallMask(wallSet, x, y);
      wg.lineStyle(lw, top, 1);
      if (!(mask & 1)) { wg.moveTo(x * cs, y * cs + lw / 2); wg.lineTo((x + 1) * cs, y * cs + lw / 2); }
      if (!(mask & 8)) { wg.moveTo(x * cs + lw / 2, y * cs); wg.lineTo(x * cs + lw / 2, (y + 1) * cs); }
      wg.lineStyle(lw, shade(wallCol, -0.45), 1);
      if (!(mask & 4)) { wg.moveTo(x * cs, (y + 1) * cs - lw / 2); wg.lineTo((x + 1) * cs, (y + 1) * cs - lw / 2); }
      if (!(mask & 2)) { wg.moveTo((x + 1) * cs - lw / 2, y * cs); wg.lineTo((x + 1) * cs - lw / 2, (y + 1) * cs); }
    }
    wg.lineStyle(0);
    this.layers.walls.addChild(wg);
    const bg = new PIXI.Graphics(), barCol = this._pal('bars', 0x2f2f31);
    for (const k of barSet) {
      const [x, y] = k.split(',').map(Number);
      bg.lineStyle(Math.max(1.5, cs * 0.06), barCol, 1);
      for (let i = 1; i < 5; i++) { bg.moveTo(x * cs + i * cs / 5, y * cs); bg.lineTo(x * cs + i * cs / 5, (y + 1) * cs); }
      bg.lineStyle(Math.max(2, cs * 0.08), shade(barCol, 0.25), 1);
      bg.moveTo(x * cs, y * cs + cs * 0.15); bg.lineTo((x + 1) * cs, y * cs + cs * 0.15);
      bg.moveTo(x * cs, (y + 1) * cs - cs * 0.15); bg.lineTo((x + 1) * cs, (y + 1) * cs - cs * 0.15);
    }
    bg.lineStyle(0);
    this.layers.walls.addChild(bg);
    for (const r of L.objects || []) this._object(r);
    const grid = new PIXI.Graphics();
    grid.lineStyle(1, 0x000000, (this.style && this.style.grid_alpha) || 0.16);
    for (let x = 0; x <= m.cols; x++) { grid.moveTo(x * cs, 0); grid.lineTo(x * cs, H); }
    for (let y = 0; y <= m.rows; y++) { grid.moveTo(0, y * cs); grid.lineTo(W, y * cs); }
    this.layers.grid.addChild(grid);
    const st = { fontFamily: 'Segoe UI, Arial, sans-serif', fontSize: Math.max(9, Math.round(this.gutter * 0.42)), fill: '#e8e0cc' };
    for (let x = 0; x < m.cols; x++) {
      const t = new PIXI.Text(colLabel(x), st);
      t.anchor.set(0.5, 0.5); t.x = x * cs + cs / 2; t.y = -this.gutter / 2;
      this.layers.labels.addChild(t);
    }
    for (let y = 0; y < m.rows; y++) {
      const t = new PIXI.Text(String(y + 1), st);
      t.anchor.set(0.5, 0.5); t.x = -this.gutter / 2; t.y = y * cs + cs / 2;
      this.layers.labels.addChild(t);
    }
  };

  MapRenderer.prototype._hover = function (obj, target) {
    obj.eventMode = 'static';
    obj.on('pointerover', e => this.opts.onHover && this.opts.onHover(target, e.global));
    obj.on('pointerout', () => this.opts.onLeave && this.opts.onLeave());
    obj.on('pointertap', e => this.opts.onTap && this.opts.onTap(target, e.global));
  };

  MapRenderer.prototype._object = function (r) {
    const cs = this.cell, x = r.x * cs, y = r.y * cs, w = (r.w || 1) * cs, h = (r.h || 1) * cs;
    const c = new PIXI.Container();
    const url = this.images && this.images.terrain && this.images.terrain[r.type];
    if (url && r.type !== 'exit') {
      const sp = PIXI.Sprite.from(url);
      sp.x = x; sp.y = y; sp.width = w; sp.height = h;
      c.addChild(sp);
    } else {
      c.addChild(this._symbol(r.type, x, y, w, h));
    }
    if (EXITS.includes(r.type)) c.addChild(this._exitMark(r, x, y, w, h));
    c.cursor = 'help';
    c.hitArea = new PIXI.Rectangle(x, y, w, h);
    this._hover(c, { what: 'object', type: r.type, label: r.label || '' });
    this.layers.objects.addChild(c);
  };
  MapRenderer.prototype._symbol = function (type, x, y, w, h) {
    const g = new PIXI.Graphics(), cs = this.cell;
    const wood = this._pal('object', 0x8a6a44), dark = this._pal('object_dark', 0x4d3a25);
    const leaf = this._pal('foliage', 0x4f7a3c), leafD = this._pal('foliage_dark', 0x365a2a);
    const rock = this._pal('rock', 0x8b877f), metal = this._pal('metal', 0x8f939a);
    const sha = (this.style && this.style.shadow) || 0.32, o = cs * 0.12;
    const shadow = fn => { g.beginFill(0x000000, sha * 0.8); fn(o); g.endFill(); };
    const p = Math.max(2, cs * 0.12);
    const cx = x + w / 2, cy = y + h / 2, rad = Math.min(w, h) / 2;
    const line = Math.max(1, cs * 0.05);
    switch (type) {
      case 'tree': case 'bush': {
        const k = type === 'bush' ? 0.7 : 0.95;
        shadow(d => g.drawCircle(cx + d, cy + d, rad * k));
        g.beginFill(leafD); g.drawCircle(cx, cy, rad * k); g.endFill();
        g.beginFill(leaf); g.drawCircle(cx - rad * 0.18, cy - rad * 0.18, rad * k * 0.72); g.endFill();
        g.beginFill(shade(leaf, 0.22)); g.drawCircle(cx - rad * 0.34, cy - rad * 0.34, rad * k * 0.32); g.endFill();
        break;
      }
      case 'rock': case 'pillar': case 'statue': {
        shadow(d => g.drawCircle(cx + d, cy + d, rad * 0.8));
        g.beginFill(rock); g.lineStyle(line, shade(rock, -0.4));
        if (type === 'rock') g.drawPolygon([cx - rad * 0.8, cy, cx - rad * 0.3, cy - rad * 0.8, cx + rad * 0.6, cy - rad * 0.6,
                                            cx + rad * 0.85, cy + rad * 0.2, cx + rad * 0.2, cy + rad * 0.8, cx - rad * 0.6, cy + rad * 0.6]);
        else g.drawCircle(cx, cy, rad * 0.72);
        g.endFill(); g.lineStyle(0);
        g.beginFill(shade(rock, 0.3), 0.8); g.drawCircle(cx - rad * 0.25, cy - rad * 0.25, rad * 0.24); g.endFill();
        break;
      }
      case 'well': case 'fireplace': case 'fire': {
        if (type !== 'fire') { g.beginFill(type === 'well' ? rock : shade(rock, -0.2)); g.lineStyle(line, shade(rock, -0.45)); g.drawCircle(cx, cy, rad * 0.85); g.endFill(); g.lineStyle(0); }
        g.beginFill(type === 'well' ? this._pal('water', 0x3d6b8a) : this._pal('fire', 0xe0832f));
        g.drawCircle(cx, cy, rad * (type === 'fire' ? 0.7 : 0.5)); g.endFill();
        if (type !== 'well') { g.beginFill(0xffd27a, 0.9); g.drawCircle(cx, cy, rad * 0.28); g.endFill(); }
        break;
      }
      case 'door': case 'gate': {
        g.beginFill(wood); g.lineStyle(line, dark);
        if (w >= h) g.drawRect(x + p * 0.5, cy - cs * 0.15, w - p, cs * 0.3);
        else g.drawRect(cx - cs * 0.15, y + p * 0.5, cs * 0.3, h - p);
        g.endFill(); g.lineStyle(0);
        break;
      }
      case 'stairs': case 'ladder': {
        g.beginFill(shade(wood, 0.1)); g.drawRect(x + p * 0.4, y + p * 0.4, w - p * 0.8, h - p * 0.8); g.endFill();
        g.lineStyle(Math.max(1.5, cs * 0.06), dark);
        const steps = Math.max(3, Math.round(Math.max(w, h) / cs * 3));
        for (let i = 1; i < steps; i++) {
          if (h >= w) { const yy = y + i * h / steps; g.moveTo(x + p * 0.4, yy); g.lineTo(x + w - p * 0.4, yy); }
          else { const xx = x + i * w / steps; g.moveTo(xx, y + p * 0.4); g.lineTo(xx, y + h - p * 0.4); }
        }
        g.lineStyle(0);
        break;
      }
      case 'bridge': case 'plank': case 'ford': {
        const horiz = w >= h;
        g.beginFill(shade(wood, 0.05)); g.drawRect(x, y + (horiz ? p * 0.3 : 0), w, h - (horiz ? p * 0.6 : 0)); g.endFill();
        g.lineStyle(line, dark, 0.8);
        const n = Math.max(2, Math.round((horiz ? w : h) / (cs * 0.3)));
        for (let i = 1; i < n; i++) {
          if (horiz) { g.moveTo(x + i * w / n, y + p * 0.3); g.lineTo(x + i * w / n, y + h - p * 0.3); }
          else { g.moveTo(x, y + i * h / n); g.lineTo(x + w, y + i * h / n); }
        }
        g.lineStyle(Math.max(2, cs * 0.08), dark);
        if (horiz) { g.moveTo(x, y + p * 0.3); g.lineTo(x + w, y + p * 0.3); g.moveTo(x, y + h - p * 0.3); g.lineTo(x + w, y + h - p * 0.3); }
        else { g.moveTo(x, y); g.lineTo(x, y + h); g.moveTo(x + w, y); g.lineTo(x + w, y + h); }
        g.lineStyle(0);
        break;
      }
      case 'rug': case 'carpet': {
        g.beginFill(this._pal('cloth', 0x9a4b3c), 0.85); g.drawRect(x + p * 0.3, y + p * 0.3, w - p * 0.6, h - p * 0.6); g.endFill();
        g.lineStyle(line, 0xe8d9a8, 0.6); g.drawRect(x + p, y + p, w - 2 * p, h - 2 * p); g.lineStyle(0);
        break;
      }
      case 'exit': break;
      case 'trapdoor': { g.beginFill(dark); g.lineStyle(line, wood); g.drawRect(x + p, y + p, w - 2 * p, h - 2 * p); g.endFill(); g.lineStyle(0); break; }
      case 'barrel': {
        shadow(d => g.drawCircle(cx + d, cy + d, rad * 0.7));
        g.beginFill(wood); g.lineStyle(line, dark); g.drawCircle(cx, cy, rad * 0.7); g.endFill();
        g.drawCircle(cx, cy, rad * 0.45); g.lineStyle(0);
        break;
      }
      case 'crate': case 'chest': {
        shadow(d => g.drawRect(x + p + d, y + p + d, w - 2 * p, h - 2 * p));
        g.beginFill(wood); g.lineStyle(line, dark); g.drawRect(x + p, y + p, w - 2 * p, h - 2 * p); g.endFill();
        if (type === 'crate') { g.moveTo(x + p, y + p); g.lineTo(x + w - p, y + h - p); g.moveTo(x + w - p, y + p); g.lineTo(x + p, y + h - p); }
        else { g.lineStyle(0); g.beginFill(metal); g.drawRect(cx - p * 0.4, y + p, p * 0.8, h - 2 * p); g.endFill(); }
        g.lineStyle(0);
        break;
      }
      case 'stall': case 'tent': {
        shadow(d => g.drawRect(x + p + d, y + p + d, w - 2 * p, h - 2 * p));
        const n = Math.max(3, Math.round(w / (cs * 0.4)));
        for (let i = 0; i < n; i++) {
          g.beginFill(i % 2 ? 0xe8dcc0 : this._pal('cloth', 0x9a4b3c));
          g.drawRect(x + p + i * (w - 2 * p) / n, y + p, (w - 2 * p) / n, h - 2 * p); g.endFill();
        }
        break;
      }
      case 'wagon': case 'log': {
        shadow(d => g.drawRoundedRect(x + p + d, y + p + d, w - 2 * p, h - 2 * p, cs * 0.2));
        g.beginFill(wood); g.lineStyle(line, dark);
        g.drawRoundedRect(x + p, y + p, w - 2 * p, h - 2 * p, type === 'log' ? Math.min(w, h) / 2 - p : cs * 0.1); g.endFill();
        g.lineStyle(0);
        if (type === 'wagon') { g.beginFill(dark); for (const [ax, ay] of [[x + p, y + p], [x + w - p, y + p], [x + p, y + h - p], [x + w - p, y + h - p]]) g.drawCircle(ax, ay, cs * 0.14); g.endFill(); }
        break;
      }
      default: {   // table, chair, bed, bar, altar, shrine and anything unknown
        const round = type === 'table' ? cs * 0.18 : cs * 0.1;
        const pp = type === 'chair' ? cs * 0.24 : p;
        const stone = type === 'altar' || type === 'shrine';
        const col = stone ? rock : wood;
        shadow(d => g.drawRoundedRect(x + pp + d, y + pp + d, w - 2 * pp, h - 2 * pp, round));
        g.beginFill(col); g.lineStyle(line, stone ? shade(rock, -0.4) : dark);
        g.drawRoundedRect(x + pp, y + pp, w - 2 * pp, h - 2 * pp, round); g.endFill(); g.lineStyle(0);
        if (type === 'bed') { g.beginFill(0xe8e0cc); g.drawRoundedRect(x + pp * 1.4, y + pp * 1.4, w - 2.8 * pp, cs * 0.3, cs * 0.08); g.endFill(); }
        else if (type === 'table' || type === 'bar') { g.beginFill(shade(col, 0.15), 0.7); g.drawRoundedRect(x + pp * 1.6, y + pp * 1.6, Math.max(1, w - 3.2 * pp), Math.max(1, h - 3.2 * pp), round * 0.6); g.endFill(); }
      }
    }
    return g;
  };

  MapRenderer.prototype._exitMark = function (r, x, y, w, h) {
    const cs = this.cell, m = this.map, c = new PIXI.Container();
    const col = this._pal('exit', 0xf2e3b3);
    const kind = exitKind(r, m.cols, m.rows);
    const cx = x + w / 2, cy = y + h / 2, s = cs * 0.24;
    const g = new PIXI.Graphics();
    g.beginFill(0x1b1712, 0.78); g.lineStyle(1.5, col, 0.9); g.drawCircle(cx, cy, s * 1.35); g.endFill(); g.lineStyle(0);
    if (kind.arrow) {          // a way out of the map: arrow pointing out
      g.beginFill(col);
      const dx = kind.arrow[0], dy = kind.arrow[1], nx = -dy, ny = dx;
      g.drawPolygon([cx + dx * s, cy + dy * s, cx - dx * s * 0.6 + nx * s * 0.75, cy - dy * s * 0.6 + ny * s * 0.75,
                     cx - dx * s * 0.2, cy - dy * s * 0.2, cx - dx * s * 0.6 - nx * s * 0.75, cy - dy * s * 0.6 - ny * s * 0.75]);
      g.endFill();
    } else if (kind.icon === 'stairs') {   // three steps
      g.lineStyle(Math.max(1.5, cs * 0.05), col, 1);
      g.moveTo(cx - s * 0.7, cy + s * 0.6); g.lineTo(cx - s * 0.7, cy + s * 0.2); g.lineTo(cx - s * 0.2, cy + s * 0.2);
      g.lineTo(cx - s * 0.2, cy - s * 0.2); g.lineTo(cx + s * 0.3, cy - s * 0.2); g.lineTo(cx + s * 0.3, cy - s * 0.6);
      g.lineTo(cx + s * 0.75, cy - s * 0.6); g.lineStyle(0);
    } else {                   // a door: the leaf with its knob
      g.lineStyle(Math.max(1.5, cs * 0.05), col, 1);
      g.drawRect(cx - s * 0.45, cy - s * 0.7, s * 0.9, s * 1.4);
      g.lineStyle(0); g.beginFill(col); g.drawCircle(cx + s * 0.22, cy, Math.max(1.2, s * 0.13)); g.endFill();
    }
    c.addChild(g);
    const name = r.label || (THINGS[r.type] || [r.type])[0];
    const t = new PIXI.Text(name, { fontFamily: 'Segoe UI, Arial, sans-serif', fontSize: Math.max(10, Math.round(cs * 0.28)),
                                    fill: '#f6eed8', stroke: '#1b1712', strokeThickness: 3 });
    t.anchor.set(0.5, 0); t.x = cx; t.y = y + h + 1;
    if (y + h + t.height > m.rows * cs) { t.anchor.set(0.5, 1); t.y = y - 1; }
    t.x = Math.max(t.width / 2, Math.min(m.cols * cs - t.width / 2, t.x));
    c.addChild(t);
    return c;
  };

  function drawShape(g, shape, r) {
    if (shape === 'circle') g.drawCircle(0, 0, r);
    else if (shape === 'square') g.drawRoundedRect(-r * 0.9, -r * 0.9, r * 1.8, r * 1.8, r * 0.35);
    else g.drawPolygon(shapePoints(shape, shape === 'diamond' ? r * 1.15 : r * 1.05));
  }

  MapRenderer.prototype._tokenView = function (tok) {
    const cs = this.cell, size = (tok.size || 1) * cs;
    const kind = KINDS[tok.kind] || KINDS.npc;
    const col = this._pal(kind.key, 0x888888);
    const c = new PIXI.Container();
    const r = size * 0.4;
    const turn = new PIXI.Graphics();
    turn.visible = false;
    c.addChild(turn);
    const shadow = new PIXI.Graphics();
    shadow.beginFill(0x000000, 0.35); shadow.drawEllipse(size * 0.06, size * 0.1, r, r * 0.9); shadow.endFill();
    c.addChild(shadow);
    const ring = new PIXI.Graphics();
    ring.beginFill(col); drawShape(ring, kind.shape, r); ring.endFill();
    c.addChild(ring);
    const inner = new PIXI.Graphics();
    inner.beginFill(shade(col, -0.45)); drawShape(inner, kind.shape, r * 0.8); inner.endFill();
    c.addChild(inner);
    const url = this.images && this.images.tokens && this.images.tokens[tok.id];
    if (url) {
      const sp = PIXI.Sprite.from(url);
      sp.anchor.set(0.5); sp.width = sp.height = r * 1.7;
      const mask = new PIXI.Graphics(); mask.beginFill(0xffffff); drawShape(mask, kind.shape, r * 0.8); mask.endFill();
      sp.mask = mask; c.addChild(mask); c.addChild(sp);
    } else {
      const t = new PIXI.Text(initials(tok.name), { fontFamily: 'Segoe UI, Arial, sans-serif', fontWeight: '700',
                                                    fontSize: Math.max(9, Math.round(r * 0.8)), fill: '#ffffff' });
      t.anchor.set(0.5); c.addChild(t);
    }
    const br = Math.max(6, r * 0.32);            // letter badge: the kind without colour
    const b = new PIXI.Graphics();
    b.beginFill(0x1b1712, 0.92); b.lineStyle(1.5, col); b.drawCircle(r * 0.78, r * 0.78, br); b.endFill();
    c.addChild(b);
    const bt = new PIXI.Text(kind.glyph, { fontFamily: 'Segoe UI, Arial, sans-serif', fontWeight: '700',
                                           fontSize: Math.max(8, Math.round(br * 1.2)), fill: '#ffffff' });
    bt.anchor.set(0.5); bt.x = r * 0.78; bt.y = r * 0.78; c.addChild(bt);
    const nameT = new PIXI.Text(shortName(tok.name), { fontFamily: 'Segoe UI, Arial, sans-serif', fontSize: Math.max(9, Math.round(cs * 0.26)),
                                            fill: '#f6eed8', stroke: '#141110', strokeThickness: 3 });
    nameT.anchor.set(0.5, 0); nameT.y = r + 2;
    nameT.visible = cs >= 30;
    c.addChild(nameT);
    c.on('pointerover', () => { nameT.visible = cs >= 22; });
    c.on('pointerout', () => { nameT.visible = cs >= 30 && v.labelled !== false; });
    c.cursor = 'pointer';
    c.hitArea = new PIXI.Circle(0, 0, r * 1.1);
    const target = { what: 'token', tok: tok };
    this._hover(c, target);
    const v = { c: c, turn: turn, target: target, r: r, shape: kind.shape, size: size, tok: tok, nameT: nameT };
    return v;
  };

  MapRenderer.prototype._centre = function (tok, x, y) {
    const s = (tok.size || 1) * this.cell;
    return [x * this.cell + s / 2, y * this.cell + s / 2];
  };

  MapRenderer.prototype._syncTokens = function (moves) {
    const seen = new Set(), byId = {};
    for (const mv of moves || []) byId[mv.id] = mv;
    for (const tok of this.map.tokens || []) {
      if (tok.hidden || tok.x == null) continue;
      seen.add(tok.id);
      let v = this.tokens.get(tok.id);
      const sig = [tok.name, tok.kind, tok.size, this.images && this.images.tokens && this.images.tokens[tok.id], this.cell].join('|');
      if (v && v.sig !== sig) { v.c.destroy({ children: true }); this.tokens.delete(tok.id); v = null; }
      const [tx, ty] = this._centre(tok, tok.x, tok.y);
      if (!v) {
        v = this._tokenView(tok);
        v.sig = sig;
        v.c.x = tx; v.c.y = ty; v.c.alpha = 0;
        this.anims.push({ kind: 'fade', obj: v.c, from: 0, to: 1, t: 0, dur: 300 });
        this.layers.tokens.addChild(v.c);
        this.tokens.set(tok.id, v);
      } else if (Math.abs(v.c.x - tx) > 0.5 || Math.abs(v.c.y - ty) > 0.5) {
        const mv = byId[tok.id];
        this.anims = this.anims.filter(a => a.obj !== v.c || a.kind === 'fade');
        if (mv && mv.path && mv.path.length > 1) this._walk(v, tok, mv.path);
        else if (mv && mv.jump) this.anims.push({ kind: 'jump', obj: v.c, to: [tx, ty], t: 0, dur: 500 });
        else this.anims.push({ kind: 'move', obj: v.c, from: [v.c.x, v.c.y], to: [tx, ty], t: 0, dur: 420 });
      }
      v.tok = tok;
      v.target.tok = tok;
    }
    for (const [id, v] of this.tokens) {
      if (seen.has(id)) continue;
      this.tokens.delete(id);
      this.anims.push({ kind: 'fade', obj: v.c, from: v.c.alpha, to: 0, t: 0, dur: 300, destroy: true });
    }
  };

  MapRenderer.prototype._walk = function (v, tok, path) {
    const pts = path.map(p => this._centre(tok, p[0], p[1]));
    const per = Math.max(70, Math.min(150, 1600 / pts.length));
    this.anims.push({ kind: 'path', obj: v.c, pts: pts, t: 0, dur: per * (pts.length - 1) });
    const g = new PIXI.Graphics(), col = this._pal('trail', 0xf2e3b3);     // where it came from
    g.lineStyle(Math.max(2, this.cell * 0.08), col, 0.75);
    g.moveTo(pts[0][0], pts[0][1]);
    for (const q of pts.slice(1)) g.lineTo(q[0], q[1]);
    g.lineStyle(0);
    g.beginFill(col, 0.9);
    for (const q of pts.slice(0, -1)) g.drawCircle(q[0], q[1], Math.max(2, this.cell * 0.07));
    g.endFill();
    this.layers.trail.addChild(g);
    this.anims.push({ kind: 'fade', obj: g, from: 1, to: 0, t: -per * pts.length - 1200, dur: 2200, destroy: true });
  };

  MapRenderer.prototype._tick = function () {
    const dt = this.app.ticker.deltaMS;
    this.clock = (this.clock || 0) + dt;
    const keep = [];
    for (const a of this.anims) {
      if (a.obj.destroyed) continue;
      a.t += dt;
      if (a.t < 0) { keep.push(a); continue; }
      const k = Math.min(1, a.t / a.dur);
      const e = k < 0.5 ? 2 * k * k : 1 - Math.pow(-2 * k + 2, 2) / 2;
      if (a.kind === 'fade') a.obj.alpha = a.from + (a.to - a.from) * k;
      else if (a.kind === 'move') { a.obj.x = a.from[0] + (a.to[0] - a.from[0]) * e; a.obj.y = a.from[1] + (a.to[1] - a.from[1]) * e; }
      else if (a.kind === 'jump') {
        a.obj.alpha = k < 0.5 ? 1 - k * 2 : (k - 0.5) * 2;
        if (k >= 0.5) { a.obj.x = a.to[0]; a.obj.y = a.to[1]; }
      } else if (a.kind === 'path') {
        const n = a.pts.length - 1, f = k * n, i = Math.min(n - 1, Math.floor(f)), rr = f - i;
        a.obj.x = a.pts[i][0] + (a.pts[i + 1][0] - a.pts[i][0]) * rr;
        a.obj.y = a.pts[i][1] + (a.pts[i + 1][1] - a.pts[i][1]) * rr;
      }
      if (k < 1) keep.push(a);
      else if (a.destroy) a.obj.destroy({ children: true });
    }
    this.anims = keep;
    for (const v of this.tokens.values()) {
      if (v.turn.visible) v.turn.scale.set(1 + 0.07 * Math.sin(this.clock / 260));
    }
  };

  MapRenderer.prototype._marks = function (turnName, rangeOn, players) {
    const low = String(turnName || '').toLowerCase();
    const named = labelled(this.map.tokens || [], turnName);
    for (const [id, v] of this.tokens) {
      v.labelled = named.has(id);
      v.nameT.visible = this.cell >= 30 && v.labelled;
    }
    let active = null;
    for (const v of this.tokens.values()) {
      const on = !!low && String(v.tok.name).toLowerCase() === low;
      v.turn.clear();
      v.turn.visible = on;
      if (!on) continue;
      active = v;
      const col = this._pal('turn', 0xf2c14e);
      v.turn.lineStyle(Math.max(3, this.cell * 0.1), col, 1);
      drawShape(v.turn, v.shape, v.r * 1.3);
      v.turn.lineStyle(0);
      v.turn.beginFill(col);
      v.turn.drawPolygon([-v.r * 0.38, -v.r * 1.85, v.r * 0.38, -v.r * 1.85, 0, -v.r * 1.42]);
      v.turn.endFill();
      this.layers.tokens.addChild(v.c);          // the active figure on top
    }
    const rg = this.layers.range;
    rg.removeChildren().forEach(c => c.destroy());
    this.activeName = active ? active.tok.name : null;
    if (!rangeOn || !active) return;
    const tok = active.tok;
    const p = (players || []).find(pl => String(pl.name || '').toLowerCase() === String(tok.name).toLowerCase());
    const speed = (p && p.speed) || (tok.stats && tok.stats.speed) || 30;
    const cells = reachCells(this.map.cols, this.map.rows, this.blocked, [tok.x, tok.y], speedSquares(speed, this.map.cell_ft));
    const g = new PIXI.Graphics(), cs = this.cell, col = this._pal('range', 0x5fb3ff);
    g.beginFill(col, 0.2);
    g.lineStyle(1, col, 0.55);
    for (const k of cells) { const [x, y] = k.split(',').map(Number); g.drawRect(x * cs + 1, y * cs + 1, cs - 2, cs - 2); }
    g.endFill();
    rg.addChild(g);
  };

  /* Draw or update. state: {cell, gutter, images, style, blocked, turn, players, moves, range} */
  MapRenderer.prototype.render = function (map, state) {
    this.map = map;
    this.images = state.images || {};
    this.style = state.style || {};
    this.cell = state.cell;
    this.gutter = state.gutter;
    this.blocked = new Set((state.blocked || []).map(c => key(c[0], c[1])));
    const W = this.gutter + map.cols * this.cell, H = this.gutter + map.rows * this.cell;
    if (this.app.screen.width !== W || this.app.screen.height !== H) this.app.renderer.resize(W, H);
    this.root.x = this.gutter; this.root.y = this.gutter;
    const sk = [map.id, map.cols, map.rows, map.floor, JSON.stringify(map.layers || {}), this.cell, this.gutter,
                JSON.stringify(this.images.terrain || {}), this.style.id || '', JSON.stringify(this.style.palette || {})].join('|');
    if (sk !== this.staticKey) {
      if (!this.staticKey || this.staticKey.split('|')[0] !== map.id) {
        for (const v of this.tokens.values()) v.c.destroy({ children: true });
        this.tokens.clear();
        this.layers.trail.removeChildren();
        this.anims = [];
      }
      this.staticKey = sk;
      this._buildStatic();
    }
    this._syncTokens(state.moves);
    this._marks(state.turn, state.range, state.players);
  };

  MapRenderer._helpers = helpers;
  MapRenderer.describe = describe;
  MapRenderer.legendItems = legendItems;
  if (typeof window !== 'undefined') window.MapRenderer = MapRenderer;
})();