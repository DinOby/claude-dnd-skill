/* gemini-budget.js — this month's Gemini spending in Settings, and the free
 * fallback voice once the budget is used up.
 *
 * The server caps Gemini (images + narration) per month (gemini_budget.py).
 * When narration is refused with 402, index.html's _ttsPlayBlock reads the
 * block with the browser's own speech synthesis instead: free, German voice
 * if the system has one, a little more mechanical.
 *
 *   DndBudget.speak(text) → Promise, resolves when reading ends or is stopped
 *   DndBudget.stop()      → stop the fallback voice
 *
 * The Settings row ("Gemini (Monat)") updates from GET /gemini-budget on load,
 * every two minutes (scripts/assets.py books images from another process),
 * and from the server's `gemini_budget` SSE message after each narration.
 */
(function () {
  'use strict';

  const POLL_MS = 120000;
  let row = null;
  let label = null;
  let speaking = null;   // {utterance, resolve} while the fallback voice reads

  function euro(n) {
    return Number(n || 0).toFixed(2).replace('.', ',') + ' €';
  }

  function ensureRow() {
    if (row) return row;
    const anchor = document.getElementById('narrate-row') || document.getElementById('speed-row');
    if (!anchor || !anchor.parentNode) return null;
    row = document.createElement('div');
    row.className = 'audio-row';
    row.id = 'gemini-budget-row';
    row.title = 'Gemini-Kosten in diesem Monat (Bilder + Vorlesen). Ist das Budget erreicht, ' +
      'entstehen keine neuen Bilder und das Vorlesen nutzt die Browser-Stimme.';
    const name = document.createElement('span');
    name.className = 'audio-label';
    name.textContent = 'Gemini (Monat)';
    label = document.createElement('span');
    label.className = 'theme-label';
    label.id = 'gemini-budget-label';
    label.textContent = '…';
    row.appendChild(name);
    row.appendChild(label);
    anchor.parentNode.insertBefore(row, anchor.nextSibling);
    return row;
  }

  function show(status) {
    if (!status || !ensureRow()) return;
    label.textContent = euro(status.spent_eur) + ' / ' + euro(status.limit_eur).replace(',00 €', ' €');
    label.style.color = status.exhausted ? '#d9534f' : '';
    row.dataset.exhausted = status.exhausted ? 'true' : 'false';
  }

  function refresh() {
    fetch('/gemini-budget', { cache: 'no-store' })
      .then(r => (r.ok ? r.json() : null))
      .then(show)
      .catch(() => {});
  }

  function germanVoice() {
    const voices = (window.speechSynthesis && speechSynthesis.getVoices()) || [];
    return voices.find(v => /^de(-|_|$)/i.test(v.lang)) || null;
  }

  function stop() {
    if (!speaking) return;
    const done = speaking.resolve;
    speaking = null;
    try { speechSynthesis.cancel(); } catch (_) {}
    done();
  }

  function speak(text) {
    stop();
    if (!('speechSynthesis' in window) || !text) return Promise.resolve();
    return new Promise(resolve => {
      const u = new SpeechSynthesisUtterance(text);
      u.lang = 'de-DE';
      const voice = germanVoice();
      if (voice) u.voice = voice;
      const finish = () => {
        if (speaking && speaking.utterance === u) speaking = null;
        resolve();
      };
      u.onend = finish;
      u.onerror = finish;
      speaking = { utterance: u, resolve: resolve };
      speechSynthesis.speak(u);
    });
  }

  window.DndBudget = Object.freeze({ speak: speak, stop: stop, refresh: refresh, show: show });

  if (window.DisplayModules) {
    DisplayModules.register('gemini-budget', payload => {
      if (payload && payload.gemini_budget) show(payload.gemini_budget);
    });
  }
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', refresh);
  } else {
    refresh();
  }
  setInterval(refresh, POLL_MS);
  if ('speechSynthesis' in window) speechSynthesis.getVoices();   // some browsers load voices lazily
})();
