/* play-as.js — "Playing as": a remote player's own main display is their device.
 *
 * Online groups watch the full display in their own browsers (not the phone
 * controller). Without a character binding the server sent every prescribed
 * roll to the shared screen, and players typed results into the party panel.
 *
 * With "Playing as <character>" (Settings, stored per browser):
 *   - the stream connects with ?as=<character>, so the server routes that
 *     character's dice requests here (and no longer opens them on the host);
 *   - a prescribed roll opens a roll dialog on this screen, prefilled with the
 *     DM's die, modifier, advantage and label; the result resolves the request
 *     and reaches the DM's next turn by itself;
 *   - a 🎲 button opens the same dialog for a free roll at any time;
 *   - the party input panel starts on this character instead of "Everybody".
 *
 * Phones (?char=…) are already bound and are left alone.
 */
(function () {
  'use strict';

  const KEY = 'dnd-play-as';
  const ctx = window.DisplayModules && window.DisplayModules.context;
  const DICE = ['1d20', '1d4', '1d6', '1d8', '1d10', '1d12', '1d100', '2d6'];

  function stored() {
    try { return (localStorage.getItem(KEY) || '').trim(); } catch (_) { return ''; }
  }
  function store(name) {
    try { if (name) localStorage.setItem(KEY, name); else localStorage.removeItem(KEY); } catch (_) {}
  }

  const playAs = ctx && !ctx.isPhone ? stored() : '';
  let party = [];
  let dialog = null;
  let pendingReq = null;

  /* Query string for /stream — read by index.html's connect(). */
  function streamQuery() {
    return playAs ? '?as=' + encodeURIComponent(playAs) : '';
  }

  /* True when a dice request names this screen's character. Pure: tested in node. */
  function targetsMe(req, me) {
    if (!req || !me) return false;
    const list = Array.isArray(req.characters) && req.characters.length ? req.characters : [req.character];
    const low = String(me).toLowerCase();
    return list.some(c => String(c || '').toLowerCase() === low);
  }

  function headers() {
    /* global _authHeaders */
    return typeof _authHeaders === 'function' ? _authHeaders() : { 'Content-Type': 'application/json' };
  }

  function el(tag, cls, text) {
    const e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text != null) e.textContent = text;
    return e;
  }

  function ensureStyles() {
    if (document.getElementById('play-as-css')) return;
    const css = document.createElement('link');
    css.id = 'play-as-css';
    css.rel = 'stylesheet';
    css.href = '/static/css/play-as.css';
    document.head.appendChild(css);
  }

  // ── Settings row ─────────────────────────────────────────────────────────

  function initSettingsRow() {
    const ctrls = document.getElementById('audio-controls');
    if (!ctrls || document.getElementById('playas-row')) return;
    const row = el('div', 'audio-row');
    row.id = 'playas-row';
    row.title = 'The character you play on this screen: the DM\'s rolls for them open here, '
      + 'and party input starts on them. Click to cycle; the page reloads.';
    row.appendChild(el('span', 'audio-label', 'Playing as'));
    const label = el('span', 'speed-label', playAs || 'Nobody');
    label.id = 'playas-label';
    row.appendChild(label);
    const anchor = document.getElementById('sfx-row');
    ctrls.insertBefore(row, anchor || null);
    let timer = null;
    row.addEventListener('click', () => {
      const names = [''].concat(party);
      const current = (label.dataset.value != null ? label.dataset.value : playAs).toLowerCase();
      const i = names.findIndex(n => n.toLowerCase() === current);
      const next = names[(i + 1) % names.length] || '';
      label.dataset.value = next;
      label.textContent = next || 'Nobody';
      clearTimeout(timer);
      timer = setTimeout(() => { store(next); location.reload(); }, 900);
    });
  }

  // ── Roll dialog ──────────────────────────────────────────────────────────

  function closeDialog() {
    if (dialog) dialog.hidden = true;
    pendingReq = null;
  }

  function openDialog(req) {
    ensureStyles();
    if (!dialog) {
      dialog = el('div', 'pa-dialog');
      dialog.setAttribute('role', 'dialog');
      document.body.appendChild(dialog);
    }
    pendingReq = req || null;
    dialog.textContent = '';
    dialog.hidden = false;

    const head = el('div', 'pa-head');
    head.appendChild(el('div', 'pa-title', req ? 'The DM asks for a roll' : 'Roll'));
    const x = el('button', 'pa-close', '✕');
    x.type = 'button';
    x.addEventListener('click', closeDialog);
    head.appendChild(x);
    dialog.appendChild(head);
    dialog.appendChild(el('div', 'pa-who', playAs));

    const label = el('input', 'pa-label');
    label.type = 'text';
    label.maxLength = 60;
    label.placeholder = 'What for? (e.g. Stealth)';
    label.value = req && req.label ? req.label : '';
    if (req && req.dc != null) dialog.appendChild(el('div', 'pa-dc', 'DC ' + req.dc));

    const dieRow = el('div', 'pa-row');
    let spec = req && DICE.includes(req.spec) ? req.spec : (req && req.spec) || '1d20';
    const dieBtns = [];
    for (const d of DICE) {
      const b = el('button', 'pa-die' + (d === spec ? ' active' : ''), d.replace(/^1d/, 'd'));
      b.type = 'button';
      b.addEventListener('click', () => { spec = d; dieBtns.forEach(o => o.classList.toggle('active', o === b)); });
      dieBtns.push(b);
      dieRow.appendChild(b);
    }

    const advRow = el('div', 'pa-row');
    let adv = req && req.advantage ? req.advantage : 'normal';
    const advBtns = [];
    for (const [val, text] of [['normal', 'Normal'], ['advantage', 'Advantage'], ['disadvantage', 'Disadvantage']]) {
      const b = el('button', 'pa-adv' + (val === adv ? ' active' : ''), text);
      b.type = 'button';
      b.addEventListener('click', () => { adv = val; advBtns.forEach(o => o.classList.toggle('active', o === b)); });
      advBtns.push(b);
      advRow.appendChild(b);
    }

    const modRow = el('label', 'pa-row pa-mod');
    modRow.appendChild(el('span', null, 'Modifier'));
    const mod = el('input');
    mod.type = 'number';
    mod.min = '-20';
    mod.max = '20';
    mod.value = String(req && req.modifier ? req.modifier : 0);
    modRow.appendChild(mod);

    const result = el('div', 'pa-result');
    const roll = el('button', 'pa-roll', 'Roll');
    roll.type = 'button';
    roll.addEventListener('click', () => {
      roll.disabled = true;
      const body = {
        character: playAs, spec: spec, modifier: parseInt(mod.value, 10) || 0,
        advantage: spec === '1d20' ? adv : 'normal', label: label.value.trim(),
      };
      if (pendingReq && pendingReq.request_id) body.request_id = pendingReq.request_id;
      fetch('/player-input/dice', { method: 'POST', headers: headers(), body: JSON.stringify(body) })
        .then(r => (r.ok ? r.json() : Promise.reject(new Error('HTTP ' + r.status))))
        .then(res => {
          const dice = (res.both || res.rolls || []).join(', ');
          result.textContent = 'Total ' + res.total + '  [' + dice + ']' + (res.modifier ? ' ' + (res.modifier > 0 ? '+' : '') + res.modifier : '');
          result.classList.add('done');
          pendingReq = null;
          roll.textContent = 'Sent to the DM ✓';
          setTimeout(closeDialog, 2600);
        })
        .catch(err => { result.textContent = 'Could not roll (' + err.message + ')'; roll.disabled = false; });
    });

    dialog.appendChild(label);
    dialog.appendChild(dieRow);
    dialog.appendChild(advRow);
    dialog.appendChild(modRow);
    dialog.appendChild(roll);
    dialog.appendChild(result);
    roll.focus();
  }

  function initRollButton() {
    if (!playAs || document.getElementById('pa-fab')) return;
    ensureStyles();
    const b = el('button', null, '🎲');
    b.id = 'pa-fab';
    b.type = 'button';
    b.title = 'Roll dice as ' + playAs;
    b.addEventListener('click', () => openDialog(null));
    document.body.appendChild(b);
  }

  // ── Secret actions and whispers ──────────────────────────────────────────
  //
  // 🤫 sends an action only the DM sees (POST /player-input/secret). The DM's
  // answer (send.py --private NAME) reaches only this character's devices; it
  // shows up here as a card and stays in a small list until dismissed.

  let whisperBox = null;

  function initSecretButton() {
    if (!playAs || document.getElementById('pa-secret-fab')) return;
    ensureStyles();
    const b = el('button', null, '🤫');
    b.id = 'pa-secret-fab';
    b.type = 'button';
    b.title = 'Secret action — only the DM sees it';
    b.addEventListener('click', openSecretDialog);
    document.body.appendChild(b);
  }

  function openSecretDialog() {
    ensureStyles();
    if (!dialog) {
      dialog = el('div', 'pa-dialog');
      dialog.setAttribute('role', 'dialog');
      document.body.appendChild(dialog);
    }
    pendingReq = null;
    dialog.textContent = '';
    dialog.hidden = false;
    const head = el('div', 'pa-head');
    head.appendChild(el('div', 'pa-title', '🤫 Secret action'));
    const x = el('button', 'pa-close', '✕');
    x.type = 'button';
    x.addEventListener('click', closeDialog);
    head.appendChild(x);
    dialog.appendChild(head);
    dialog.appendChild(el('div', 'pa-who', 'Only the DM sees this — not the other players.'));
    const text = el('textarea', 'pa-label pa-secret-text');
    text.rows = 4;
    text.maxLength = 500;
    text.placeholder = 'What does ' + playAs + ' do or say in secret?';
    dialog.appendChild(text);
    const send = el('button', 'pa-roll', 'Send to the DM');
    send.type = 'button';
    const result = el('div', 'pa-result');
    send.addEventListener('click', () => {
      const value = text.value.trim();
      if (!value) return;
      send.disabled = true;
      fetch('/player-input/secret', { method: 'POST', headers: headers(),
        body: JSON.stringify({ character: playAs, text: value }) })
        .then(r => {
          if (r.status === 202) throw new Error('waiting for the DM to approve this device');
          if (!r.ok) throw new Error('HTTP ' + r.status);
          result.textContent = 'Sent ✓';
          result.classList.add('done');
          setTimeout(closeDialog, 1400);
        })
        .catch(err => { result.textContent = 'Not sent (' + err.message + ')'; send.disabled = false; });
    });
    dialog.appendChild(send);
    dialog.appendChild(result);
    text.focus();
  }

  function showWhisper(msg) {
    if (!msg || !msg.text) return;
    ensureStyles();
    if (!whisperBox) {
      whisperBox = el('div', 'pa-whispers');
      whisperBox.setAttribute('aria-live', 'polite');
      document.body.appendChild(whisperBox);
    }
    const card = el('div', 'pa-whisper' + (msg.own ? ' own' : ''));
    const head = el('div', 'pa-whisper-head',
      msg.own ? 'You, in secret, to the DM' : 'The DM — only you see this');
    const x = el('button', 'pa-close', '✕');
    x.type = 'button';
    x.addEventListener('click', () => card.remove());
    head.appendChild(x);
    card.appendChild(head);
    card.appendChild(el('div', 'pa-whisper-text', msg.text));
    whisperBox.appendChild(card);
    while (whisperBox.children.length > 6) whisperBox.firstChild.remove();
  }

  // ── Party input starts on this character ─────────────────────────────────

  function initPartyTab() {
    const tabs = document.getElementById('char-tabs');
    if (!playAs || !tabs || typeof MutationObserver === 'undefined') return;
    const pick = () => {
      const want = Array.from(tabs.querySelectorAll('.char-tab'))
        .find(b => String(b.dataset.char || '').toLowerCase() === playAs.toLowerCase());
      if (want && !want.classList.contains('active')) want.click();
    };
    new MutationObserver(pick).observe(tabs, { childList: true });
    pick();
  }

  function init() {
    if (!ctx || ctx.isPhone) return;
    initSettingsRow();
    initRollButton();
    initSecretButton();
    initPartyTab();
  }

  window.PlayAs = Object.freeze({
    name: () => playAs,
    streamQuery: streamQuery,
    _targetsMe: targetsMe,
  });

  if (typeof document !== 'undefined' && ctx && !ctx.isPhone) {
    if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
    else init();
  }

  if (window.DisplayModules) {
    window.DisplayModules.register('play-as', payload => {
      if (payload.stats && Array.isArray(payload.stats.players)) {
        party = payload.stats.players.map(p => p && p.name).filter(Boolean);
      }
      if (payload.dice_request && targetsMe(payload.dice_request, playAs)) openDialog(payload.dice_request);
      if (payload.dice_request_cancelled && pendingReq && pendingReq.request_id === payload.dice_request_cancelled) {
        closeDialog();
      }
    }, { displayOnly: true });
    // Whispers also reach phones (?char=), so this one is not displayOnly. The
    // server only sends them to the addressee's devices anyway.
    window.DisplayModules.register('whispers', payload => {
      if (payload.private) showWhisper(payload.private);
    });
  }
})();
