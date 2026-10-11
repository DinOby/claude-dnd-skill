/* combat-music.js — music while a fight is on.
 *
 * A fight is on while the stats carry an initiative order (push_stats.py
 * --turn-order … until --turn-clear). The track fades in when the order
 * appears and out when it is cleared. Source: GET /audio/music/combat — the
 * table's own file at <data-root>/audio/combat.mp3 (or .ogg/.m4a/.wav) or a
 * synthesised drum-and-drone loop.
 *
 * Settings (per browser): "Combat Music" on/off and a volume slider.
 * Browsers refuse sound before the first click on the page; if playback is
 * refused, it starts on the next click.
 */
(function () {
  'use strict';

  const ON_KEY = 'dnd-combat-music';
  const VOL_KEY = 'dnd-combat-music-vol';
  const FADE_MS = 1800;

  let audio = null;
  let inCombat = false;
  let enabled = true;
  let volume = 0.45;
  let fadeTimer = null;
  let waitingForClick = false;

  try {
    enabled = localStorage.getItem(ON_KEY) !== '0';
    const v = parseFloat(localStorage.getItem(VOL_KEY));
    if (!isNaN(v)) volume = Math.min(1, Math.max(0, v));
  } catch (_) {}

  /* Is a fight on? Pure: tested in node. */
  function fightOn(stats) {
    const to = stats && stats.turn_order;
    return !!(to && Array.isArray(to.order) && to.order.length);
  }

  function ensureAudio() {
    if (audio) return audio;
    audio = new Audio('/audio/music/combat');
    audio.loop = true;
    audio.preload = 'auto';
    audio.volume = 0;
    return audio;
  }

  function fadeTo(target, then) {
    clearInterval(fadeTimer);
    const a = ensureAudio();
    const start = a.volume;
    const t0 = Date.now();
    fadeTimer = setInterval(() => {
      const k = Math.min(1, (Date.now() - t0) / FADE_MS);
      a.volume = Math.max(0, Math.min(1, start + (target - start) * k));
      if (k >= 1) {
        clearInterval(fadeTimer);
        if (then) then();
      }
    }, 50);
  }

  function start() {
    if (!enabled) return;
    const a = ensureAudio();
    const p = a.play();
    if (p && typeof p.catch === 'function') {
      p.then(() => fadeTo(volume)).catch(() => {
        if (waitingForClick) return;
        waitingForClick = true;
        document.addEventListener('click', function retry() {
          document.removeEventListener('click', retry);
          waitingForClick = false;
          if (inCombat && enabled) start();
        });
      });
    } else {
      fadeTo(volume);
    }
  }

  function stop() {
    if (!audio || audio.paused) return;
    fadeTo(0, () => { audio.pause(); audio.currentTime = 0; });
  }

  function apply() {
    if (inCombat && enabled) start();
    else stop();
  }

  function initSettings() {
    const ctrls = document.getElementById('audio-controls');
    if (!ctrls || document.getElementById('music-row')) return;
    const row = document.createElement('div');
    row.className = 'audio-row';
    row.id = 'music-row';
    row.title = 'Music while a fight is on (fades in at initiative, out when it ends)';
    row.innerHTML = '<span class="audio-label">Combat Music</span>'
      + '<span class="vb-wrap"><input id="music-vol" type="range" min="0" max="100" step="5" aria-label="Combat music volume">'
      + '<div class="toggle-track" id="music-track"><div class="toggle-thumb"></div></div></span>';
    const anchor = document.getElementById('vfx-row');
    ctrls.insertBefore(row, anchor || null);
    const track = row.querySelector('#music-track');
    const slider = row.querySelector('#music-vol');
    slider.value = String(Math.round(volume * 100));
    track.classList.toggle('on', enabled);
    track.addEventListener('click', e => {
      e.stopPropagation();
      enabled = !enabled;
      track.classList.toggle('on', enabled);
      try { localStorage.setItem(ON_KEY, enabled ? '1' : '0'); } catch (_) {}
      apply();
    });
    slider.addEventListener('click', e => e.stopPropagation());
    slider.addEventListener('input', () => {
      volume = parseInt(slider.value, 10) / 100;
      try { localStorage.setItem(VOL_KEY, String(volume)); } catch (_) {}
      if (audio && !audio.paused) { clearInterval(fadeTimer); audio.volume = volume; }
    });
  }

  window.CombatMusic = Object.freeze({ _fightOn: fightOn });

  if (typeof document !== 'undefined' && window.DisplayModules && !window.DisplayModules.context.isPhone) {
    if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', initSettings);
    else initSettings();
  }

  if (window.DisplayModules) {
    window.DisplayModules.register('combat-music', payload => {
      if (payload.clear) { inCombat = false; apply(); return; }
      if (!payload.stats || !('turn_order' in payload.stats)) return;
      const now = fightOn(payload.stats);
      if (now === inCombat) return;
      inCombat = now;
      apply();
    }, { displayOnly: true });
  }
})();
