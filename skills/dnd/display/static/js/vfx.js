/* vfx.js — action overlays on the main display.
 *
 * Reacts to {"vfx": {"effect", "actor", "source"}} from the server (see
 * display/vfx.py). What an effect looks like comes from GET /vfx/iconset,
 * the merged vfx-iconset.json — this file knows no effect names.
 *
 * Main display only (displayOnly): phones are controllers and stay quiet.
 * The "Action Overlays" toggle in the settings column is per browser.
 *
 * Extension point for the grid view: VfxOverlay.anchorFor is a function
 * (actor) → {x, y[, size]} in viewport pixels (or null). grid.js sets it, so
 * an overlay with an actor appears — smaller — over that actor's token while
 * the battle grid is on screen, and centred otherwise.
 */
(function () {
  'use strict';

  const STORAGE_KEY = 'dnd-vfx';
  const ICONSET_TTL_MS = 30000;   // re-read the iconset so config edits apply live
  const QUEUE_MAX = 2;
  const ANIMATIONS = ['pop', 'slash', 'shoot', 'pulse', 'rise', 'swipe', 'fade'];

  let iconset = null;
  let iconsetAt = 0;
  let iconsetLoading = null;
  let enabled = true;
  let layer = null;
  let playing = false;
  const queue = [];

  // ── Pure helpers (exported for tests) ────────────────────────────────────

  /* Effect name → look, falling back to the iconset's fallback effect.
     Returns null when neither exists. */
  function resolveEffect(set, effect) {
    const effects = (set && set.effects) || {};
    const spec = effects[effect] || effects[set && set.fallback];
    if (!spec || !spec.icon) return null;
    const duration = Math.min(10000, Math.max(100, Number(spec.duration_ms) || 1100));
    return {
      icon: '/vfx/icons/' + encodeURIComponent(spec.icon),
      animation: ANIMATIONS.includes(spec.animation) ? spec.animation : 'pop',
      duration: duration,
      tint: /^#[0-9a-fA-F]{3,8}$/.test(spec.tint || '') ? spec.tint : '#e8c05a',
      label: typeof spec.label === 'string' ? spec.label : '',
    };
  }

  function caption(look, actor) {
    return [actor, look.label].filter(Boolean).join(' · ');
  }

  /* Queue policy: keep at most QUEUE_MAX pending; newest wins. */
  function enqueue(q, item, max) {
    q.push(item);
    while (q.length > max) q.shift();
    return q;
  }

  // ── Iconset loading ──────────────────────────────────────────────────────

  function loadIconset() {
    if (iconsetLoading) return iconsetLoading;
    iconsetLoading = fetch('/vfx/iconset', { cache: 'no-store' })
      .then(r => (r.ok ? r.json() : null))
      .then(data => {
        if (data && data.effects) { iconset = data; iconsetAt = Date.now(); }
      })
      .catch(() => {})
      .finally(() => { iconsetLoading = null; });
    return iconsetLoading;
  }

  function ensureIconset() {
    if (!iconset) return loadIconset();
    if (Date.now() - iconsetAt > ICONSET_TTL_MS) loadIconset();   // refresh in background
    return Promise.resolve();
  }

  // ── Rendering ────────────────────────────────────────────────────────────

  function ensureLayer() {
    if (layer) return layer;
    const css = document.createElement('link');
    css.rel = 'stylesheet';
    css.href = '/static/css/vfx.css';
    document.head.appendChild(css);
    layer = document.createElement('div');
    layer.id = 'vfx-layer';
    layer.setAttribute('aria-hidden', 'true');
    document.body.appendChild(layer);
    return layer;
  }

  function play(event) {
    const look = resolveEffect(iconset, event.effect);
    if (!look) { next(); return; }

    const card = document.createElement('div');
    card.className = 'vfx-card vfx-anim-' + look.animation;
    card.style.setProperty('--vfx-tint', look.tint);
    card.style.setProperty('--vfx-dur', look.duration + 'ms');

    const anchor = event.actor && typeof api.anchorFor === 'function' ? api.anchorFor(event.actor) : null;
    if (anchor && isFinite(anchor.x) && isFinite(anchor.y)) {
      card.classList.add('vfx-anchored');
      card.style.position = 'fixed';
      card.style.left = anchor.x + 'px';
      card.style.top = anchor.y + 'px';
      card.style.translate = '-50% -100%';  // card ends at the token's centre: caption on top, icon over the token
      if (isFinite(anchor.size) && anchor.size > 0) {
        card.style.setProperty('--vfx-anchor', Math.round(Math.min(160, Math.max(56, anchor.size * 2.2))) + 'px');
      }
    }

    const img = document.createElement('img');
    img.className = 'vfx-icon';
    img.alt = '';
    img.src = look.icon;
    card.appendChild(img);

    const text = caption(look, event.actor);
    if (text) {
      const cap = document.createElement('div');
      cap.className = 'vfx-caption';
      cap.textContent = text;
      card.appendChild(cap);
    }

    ensureLayer().appendChild(card);
    playing = true;
    let done = false;
    const finish = () => {
      if (done) return;
      done = true;
      card.remove();
      next();
    };
    card.addEventListener('animationend', finish);
    setTimeout(finish, look.duration + 250);   // safety net if animationend never fires
  }

  function next() {
    playing = false;
    if (queue.length) play(queue.shift());
  }

  function show(event) {
    if (!enabled || !event || !event.effect) return;
    ensureIconset().then(() => {
      if (playing) enqueue(queue, event, QUEUE_MAX);
      else play(event);
    });
  }

  // ── Settings toggle ──────────────────────────────────────────────────────

  function initToggle() {
    try { enabled = localStorage.getItem(STORAGE_KEY) !== '0'; } catch (_) {}
    const row = document.getElementById('vfx-row');
    const track = document.getElementById('vfx-track');
    if (!row || !track) return;
    track.classList.toggle('on', enabled);
    row.addEventListener('click', () => {
      enabled = !enabled;
      track.classList.toggle('on', enabled);
      try { localStorage.setItem(STORAGE_KEY, enabled ? '1' : '0'); } catch (_) {}
      if (!enabled) { queue.length = 0; if (layer) layer.textContent = ''; playing = false; }
    });
  }

  // ── Wiring ───────────────────────────────────────────────────────────────

  const api = {
    show: show,
    anchorFor: null,
    // exposed for tests
    _resolveEffect: resolveEffect,
    _caption: caption,
    _enqueue: enqueue,
  };
  window.VfxOverlay = api;

  if (window.DisplayModules) {
    DisplayModules.register('vfx', payload => { if (payload.vfx) show(payload.vfx); },
                            { displayOnly: true });
  }

  if (typeof document !== 'undefined' && !(window.DisplayModules && DisplayModules.context.isPhone)) {
    if (document.readyState === 'loading') {
      document.addEventListener('DOMContentLoaded', () => { initToggle(); loadIconset(); });
    } else {
      initToggle();
      loadIconset();
    }
  }
})();
