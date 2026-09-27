/* grid.js — the battle grid on the main display.
 *
 * Keeps the current map (grid_map.py model) and draws it: a square grid
 * with column letters and row numbers (A1 = top-left, the notation the DM
 * uses), terrain rectangles, and tokens as discs with initials. Tokens are
 * keyed by id, so a move animates from the old square to the new one.
 *
 * Pictures: the server sends image URLs next to the map ("map_images":
 * floor, terrain type → sprite, token id → portrait). The floor is tiled
 * under the grid, furniture sprites are stretched over their rectangle,
 * natural features and surfaces (trees, walls, water) are tiled per square,
 * tokens show their portrait in a ring coloured by kind. Anything without a
 * picture keeps the drawn look (terrain colours, initials).
 *
 * Messages (main display only — the server never sends them to phones):
 *   {"map": {...} | null, "map_images": {...}}   full map, or none
 *   {"map_patch": {map_id, base, rev, move, add, remove}, "map_images": {tokens}}
 *                                  applied when base matches the map's rev,
 *                                  otherwise the map is re-read (GET /map)
 *   {"stats": {turn_order: {current}}}           highlights whose turn it is
 *   {"assets_changed": true}       new pictures were generated → re-read
 *   {"clear": true}                forget the map
 *
 * Whether the grid is on screen is decided by scene-mode.js through
 * GridView.setVisible(); this file only keeps and draws the map.
 * Tokens with "hidden": true are the DM's secret and are not drawn.
 */
(function () {
  'use strict';

  const MOVE_MS = 450;
  // Sprites stretched over their whole rectangle; every other type is tiled per square.
  const STRETCH = ['table', 'chair', 'bar', 'fireplace', 'door', 'well', 'log', 'stall', 'tent', 'wagon',
                   'fire', 'stairs', 'bridge', 'rug', 'bed', 'altar', 'chest', 'statue', 'shrine'];

  let map = null;
  let images = { floor: null, terrain: {}, tokens: {} };
  let turn = null;                 // name of the combatant whose turn it is
  let visible = false;
  let panel = null, board = null, title = null;
  const tokenEls = new Map();      // token id → element
  let resyncing = false;
  const listeners = [];

  // ── Pure helpers (exported for tests) ────────────────────────────────────

  function colLabel(x) {
    let label = '';
    let n = x + 1;
    while (n > 0) {
      const rem = (n - 1) % 26;
      label = String.fromCharCode(65 + rem) + label;
      n = Math.floor((n - 1) / 26);
    }
    return label;
  }

  /* "Flerb" → "Fl", "Wirtin Hilde" → "WH", "Goblin 2" → "G2", "goblin-3" → "G3". */
  function initials(name) {
    const words = String(name || '').trim().split(/[\s\-_]+/).filter(Boolean);
    if (!words.length) return '?';
    const first = words[0];
    const last = words[words.length - 1];
    if (words.length > 1 && /^\d+$/.test(last)) return first[0].toUpperCase() + last;
    if (words.length > 1) return (first[0] + words[1][0]).toUpperCase();
    return first[0].toUpperCase() + (first[1] || '').toLowerCase();
  }

  /* New map after a patch, or null when the patch does not fit (→ re-read). */
  function applyPatch(current, patch) {
    if (!current || !patch || patch.map_id !== current.id || patch.base !== current.rev) return null;
    const next = JSON.parse(JSON.stringify(current));
    const removed = new Set(patch.remove || []);
    next.tokens = next.tokens.filter(t => !removed.has(t.id));
    for (const mv of patch.move || []) {
      const tok = next.tokens.find(t => t.id === mv.id);
      if (!tok) return null;
      tok.x = mv.x; tok.y = mv.y;
    }
    for (const tok of patch.add || []) next.tokens.push(tok);
    next.rev = patch.rev;
    return next;
  }

  function spriteMode(type) {
    return STRETCH.includes(String(type).toLowerCase()) ? 'stretch' : 'tile';
  }

  /* CSS background for a sprite in its rectangle. */
  function spriteBackground(url, type) {
    const u = 'url("' + String(url).replace(/"/g, '%22') + '")';
    return spriteMode(type) === 'stretch'
      ? u + ' center / 100% 100% no-repeat'
      : u + ' 0 0 / var(--cell) var(--cell) repeat';
  }

  function mergeImages(base, extra) {
    const out = { floor: base.floor, terrain: Object.assign({}, base.terrain), tokens: Object.assign({}, base.tokens) };
    if (!extra) return out;
    if ('floor' in extra) out.floor = extra.floor;
    if (extra.terrain) out.terrain = Object.assign({}, extra.terrain);
    Object.assign(out.tokens, extra.tokens || {});
    return out;
  }

  /* Cell size (px) so cols×rows plus one label row/column fit into w×h. */
  function cellSize(cols, rows, w, h) {
    if (!cols || !rows || w <= 0 || h <= 0) return 0;
    return Math.max(8, Math.floor(Math.min(w / (cols + 0.8), h / (rows + 0.8))));
  }

  // ── DOM ──────────────────────────────────────────────────────────────────

  function ensurePanel() {
    if (panel) return panel;
    const css = document.createElement('link');
    css.rel = 'stylesheet';
    css.href = '/static/css/grid.css';
    css.addEventListener('load', () => render());   // first measurement needs the styles
    document.head.appendChild(css);
    panel = document.createElement('div');
    panel.id = 'grid-panel';
    panel.setAttribute('aria-label', 'Battle map');
    title = document.createElement('div');
    title.className = 'grid-title';
    board = document.createElement('div');
    board.className = 'grid-board';
    panel.appendChild(title);
    panel.appendChild(board);
    document.body.appendChild(panel);
    window.addEventListener('resize', () => render());
    // follow the text column when the sidebar or settings column is toggled
    const scroll = document.getElementById('text-scroll');
    if (scroll && typeof MutationObserver !== 'undefined') {
      new MutationObserver(() => { placePanel(); setTimeout(render, 420); })
        .observe(scroll, { attributes: true, attributeFilter: ['class'] });
    }
    return panel;
  }

  function placePanel() {
    const scroll = document.getElementById('text-scroll');
    if (!panel || !scroll) return;
    const cs = getComputedStyle(scroll);
    panel.style.left = cs.paddingLeft;
    panel.style.right = cs.paddingRight;
  }

  function el(tag, cls, text) {
    const e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text != null) e.textContent = text;
    return e;
  }

  function terrainClass(type) {
    return 'grid-t grid-t-' + String(type).toLowerCase().replace(/[^a-z0-9-]/g, '');
  }

  function render() {
    if (!panel || !map || !document.body.classList.contains('grid-on')) return;
    placePanel();
    title.textContent = map.name || map.id;
    const cell = cellSize(map.cols, map.rows, panel.clientWidth - 8, panel.clientHeight - title.offsetHeight - 8);
    if (!cell) return;
    const gutter = Math.round(cell * 0.8);
    board.style.setProperty('--cell', cell + 'px');
    board.style.setProperty('--gutter', gutter + 'px');
    board.style.width = (gutter + map.cols * cell) + 'px';
    board.style.height = (gutter + map.rows * cell) + 'px';

    // static layer: labels, grid lines, terrain (rebuilt — cheap and rare)
    let field = board.querySelector('.grid-field');
    const fieldKey = map.id + ':' + map.cols + 'x' + map.rows + ':' + cell + ':' + JSON.stringify(map.terrain)
      + ':' + images.floor + ':' + JSON.stringify(images.terrain);
    if (!field || field.dataset.key !== fieldKey) {
      board.querySelectorAll('.grid-labels, .grid-field').forEach(n => n.remove());
      const cols = el('div', 'grid-labels grid-labels-cols');
      for (let x = 0; x < map.cols; x++) cols.appendChild(el('span', null, colLabel(x)));
      const rows = el('div', 'grid-labels grid-labels-rows');
      for (let y = 0; y < map.rows; y++) rows.appendChild(el('span', null, String(y + 1)));
      field = el('div', 'grid-field');
      field.dataset.key = fieldKey;
      const floor = el('div', 'grid-floor');
      if (images.floor) {
        floor.classList.add('has-img');
        floor.style.setProperty('--floor', 'url("' + String(images.floor).replace(/"/g, '%22') + '")');
      }
      field.appendChild(floor);
      for (const t of map.terrain || []) {
        const tEl = el('div', terrainClass(t.type));
        tEl.style.left = (t.x * cell) + 'px';
        tEl.style.top = (t.y * cell) + 'px';
        tEl.style.width = (t.w * cell) + 'px';
        tEl.style.height = (t.h * cell) + 'px';
        tEl.title = t.label || t.type;
        const url = images.terrain[t.type];
        if (url) {
          tEl.classList.add('has-img');
          tEl.style.background = spriteBackground(url, t.type);
        }
        field.appendChild(tEl);
      }
      board.appendChild(cols);
      board.appendChild(rows);
      board.appendChild(field);
    }

    // tokens: keep elements by id so moves animate
    const seen = new Set();
    for (const tok of map.tokens || []) {
      if (tok.hidden) continue;
      seen.add(tok.id);
      let tEl = tokenEls.get(tok.id);
      if (!tEl) {
        tEl = el('div', 'grid-token entering');
        tEl.appendChild(el('div', 'grid-token-disc'));
        tEl.appendChild(el('div', 'grid-token-name'));
        field.appendChild(tEl);
        tokenEls.set(tok.id, tEl);
        requestAnimationFrame(() => tEl.classList.remove('entering'));
      } else if (tEl.parentNode !== field) {
        field.appendChild(tEl);
      }
      const portrait = images.tokens[tok.id];
      const isTurn = !!turn && String(tok.name).toLowerCase() === turn;
      tEl.className = 'grid-token grid-kind-' + (tok.kind || 'npc') + (tEl.classList.contains('entering') ? ' entering' : '')
        + (portrait ? ' has-img' : '') + (isTurn ? ' grid-token-turn' : '');
      tEl.dataset.id = tok.id;
      tEl.style.width = tEl.style.height = (tok.size * cell) + 'px';
      tEl.style.transform = 'translate(' + (tok.x * cell) + 'px,' + (tok.y * cell) + 'px)';
      tEl.firstChild.textContent = portrait ? '' : initials(tok.name);
      tEl.firstChild.style.backgroundImage = portrait ? 'url("' + String(portrait).replace(/"/g, '%22') + '")' : '';
      tEl.lastChild.textContent = tok.name;
      tEl.title = tok.name;
      tEl.classList.toggle('grid-token-small', cell < 34);
    }
    for (const [id, tEl] of tokenEls) {
      if (seen.has(id)) continue;
      tokenEls.delete(id);
      tEl.classList.add('leaving');
      setTimeout(() => tEl.remove(), MOVE_MS);
    }
  }

  function clearBoard() {
    tokenEls.clear();
    if (board) board.textContent = '';
    if (title) title.textContent = '';
  }

  // ── State ────────────────────────────────────────────────────────────────

  function notify() {
    for (const cb of listeners.slice()) {
      try { cb(map); } catch (err) { console.error('[GridView] listener:', err); }
    }
  }

  function setMap(next, nextImages) {
    const sameMap = map && next && map.id === next.id;
    map = next || null;
    if (nextImages !== undefined) images = mergeImages({ floor: null, terrain: {}, tokens: {} }, nextImages || {});
    if (!map) images = { floor: null, terrain: {}, tokens: {} };
    if (typeof document === 'undefined') { notify(); return; }
    if (!map) { clearBoard(); notify(); return; }
    ensurePanel();
    if (!sameMap) clearBoard();
    render();
    notify();
  }

  function resync() {
    if (resyncing || typeof fetch === 'undefined') return;
    resyncing = true;
    fetch('/map', { cache: 'no-store' })
      .then(r => (r.ok ? r.json() : null))
      .then(data => { if (data) setMap(data.map, data.images || {}); })
      .catch(() => {})
      .finally(() => { resyncing = false; });
  }

  function onPatch(patch, extra) {
    const next = applyPatch(map, patch);
    if (!next) { resync(); return; }
    images = mergeImages(images, extra ? { tokens: extra.tokens || {} } : null);
    setMap(next);
  }

  function onTurn(stats) {
    const to = stats && stats.turn_order;
    const current = to && to.current ? String(to.current).toLowerCase() : null;
    if (current === turn) return;
    turn = current;
    if (map && typeof document !== 'undefined') render();
  }

  function setVisible(on) {
    visible = !!on;
    if (typeof document === 'undefined') return;
    if (visible) ensurePanel();
    const shown = visible && !!map;
    const was = document.body.classList.contains('grid-on');
    document.body.classList.toggle('grid-on', shown);
    if (shown && !was) render();   // measure only once the panel takes up space
  }

  /* Viewport centre of a token (by id or name) — for overlays over a token. */
  function tokenCenter(ref) {
    if (!map || !visible) return null;
    const low = String(ref || '').toLowerCase();
    const tok = (map.tokens || []).find(t => t.id === ref || String(t.name).toLowerCase() === low);
    const tEl = tok && tokenEls.get(tok.id);
    if (!tEl) return null;
    const r = tEl.getBoundingClientRect();
    return { x: r.left + r.width / 2, y: r.top + r.height / 2 };
  }

  const api = {
    current: () => map,
    setMap: setMap,
    setVisible: setVisible,
    isVisible: () => visible,
    onChange: cb => { if (typeof cb === 'function') listeners.push(cb); },
    tokenCenter: tokenCenter,
    // exposed for tests
    _initials: initials,
    _applyPatch: applyPatch,
    _colLabel: colLabel,
    _cellSize: cellSize,
    _spriteMode: spriteMode,
    _spriteBackground: spriteBackground,
    _mergeImages: mergeImages,
    _images: () => images,
    _turn: () => turn,
  };
  window.GridView = api;

  if (window.DisplayModules) {
    DisplayModules.register('grid', payload => {
      if ('map' in payload) setMap(payload.map, payload.map_images || {});
      if (payload.map_patch) onPatch(payload.map_patch, payload.map_images);
      if (payload.stats) onTurn(payload.stats);
      if (payload.assets_changed && map) resync();
      if (payload.clear) setMap(null);
    }, { displayOnly: true });
  }
})();
