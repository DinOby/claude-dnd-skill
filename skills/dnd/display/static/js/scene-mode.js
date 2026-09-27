/* scene-mode.js — what the main display shows for the current scene state.
 *
 * Display rule (setting "Map View", per browser: Auto | Scene | Grid):
 *
 *   Auto:  stationary + map  → grid with tokens
 *          stationary, no map → the scene backdrop as before
 *          travel             → backdrop by terrain + travel banner, no map
 *          travel_event       → grid with the event map + event banner
 *   Scene: never the grid (banners still show)
 *   Grid:  the grid whenever there is a map
 *
 * Inputs: {"scene_state": …} from the server (scene_state.py) and the map
 * kept by grid.js (GridView.onChange). The terrain backdrop itself comes from
 * the server as an ordinary {"scene": …} message. Main display only; phones
 * never receive scene_state and this module is displayOnly.
 */
(function () {
  'use strict';

  const STORAGE_KEY = 'dnd-map-view';
  const MODES = ['auto', 'scene', 'grid'];
  const LABELS = { auto: 'Auto', scene: 'Scene', grid: 'Grid' };

  let state = null;
  let mode = 'auto';
  let banner = null;

  // ── Pure helpers (exported for tests) ────────────────────────────────────

  function showGrid(sceneState, hasMap, viewMode) {
    if (!hasMap || viewMode === 'scene') return false;
    if (viewMode === 'grid') return true;
    const m = sceneState && sceneState.mode;
    return m !== 'travel';   // stationary with a map, travel_event, or no state yet
  }

  /* Banner content for the state, or null. UI text is English; place names
     and event titles are game content and shown as given. */
  function bannerFor(sceneState) {
    if (!sceneState || !sceneState.travel) return null;
    const t = sceneState.travel;
    const day = t.days_total ? 'Day ' + Math.max(1, t.day || 0) + ' of ' + t.days_total : '';
    if (sceneState.mode === 'travel') {
      return {
        kind: 'travel',
        kicker: 'On the road',
        title: (t.from ? t.from + ' → ' : '') + t.to,
        sub: [day, t.via ? 'via ' + t.via : '', t.terrain || ''].filter(Boolean).join(' · '),
      };
    }
    if (sceneState.mode === 'travel_event') {
      const ev = sceneState.event || {};
      return {
        kind: 'event',
        kicker: 'Travel event',
        title: ev.title || 'Something happens',
        sub: [day, 'on the way to ' + t.to].filter(Boolean).join(' · '),
      };
    }
    return null;
  }

  function nextMode(m) {
    return MODES[(MODES.indexOf(m) + 1) % MODES.length] || 'auto';
  }

  // ── DOM ──────────────────────────────────────────────────────────────────

  function ensureBanner() {
    if (banner) return banner;
    const css = document.createElement('link');
    css.rel = 'stylesheet';
    css.href = '/static/css/scene-mode.css';
    document.head.appendChild(css);
    banner = document.createElement('div');
    banner.id = 'scene-banner';
    banner.setAttribute('role', 'status');
    banner.innerHTML = '<div class="sb-kicker"></div><div class="sb-title"></div><div class="sb-sub"></div>';
    document.body.appendChild(banner);
    return banner;
  }

  function apply() {
    const grid = window.GridView;
    const hasMap = !!(grid && grid.current());
    if (grid) grid.setVisible(showGrid(state, hasMap, mode));
    const info = bannerFor(state);
    const b = ensureBanner();
    b.className = info ? 'visible sb-' + info.kind : '';
    document.body.classList.toggle('has-scene-banner', !!info);
    if (info) {
      b.children[0].textContent = info.kicker;
      b.children[1].textContent = info.title;
      b.children[2].textContent = info.sub;
    }
  }

  function initSetting() {
    try { mode = MODES.includes(localStorage.getItem(STORAGE_KEY)) ? localStorage.getItem(STORAGE_KEY) : 'auto'; }
    catch (_) { mode = 'auto'; }
    const row = document.getElementById('mapview-row');
    const label = document.getElementById('mapview-label');
    if (!row || !label) return;
    label.textContent = LABELS[mode];
    row.addEventListener('click', () => {
      mode = nextMode(mode);
      label.textContent = LABELS[mode];
      try { localStorage.setItem(STORAGE_KEY, mode); } catch (_) {}
      apply();
    });
  }

  // ── Wiring ───────────────────────────────────────────────────────────────

  const api = {
    state: () => state,
    mode: () => mode,
    // exposed for tests
    _showGrid: showGrid,
    _bannerFor: bannerFor,
    _nextMode: nextMode,
  };
  window.SceneMode = api;

  const isPhone = window.DisplayModules && DisplayModules.context.isPhone;
  if (window.DisplayModules) {
    DisplayModules.register('scene-mode', payload => {
      if ('scene_state' in payload) { state = payload.scene_state; apply(); }
      if (payload.clear) { state = null; apply(); }
    }, { displayOnly: true });
  }
  if (typeof document !== 'undefined' && !isPhone) {
    if (window.GridView) GridView.onChange(() => apply());
    if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', initSetting);
    else initSetting();
  }
})();
