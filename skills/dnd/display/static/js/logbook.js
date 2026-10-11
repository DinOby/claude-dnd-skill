/* logbook.js — the players' logbook, one click away in the character sidebar.
 *
 * The DM keeps <campaign>/logbuch.md: what the characters know, sorted by
 * quest (never the DM's secrets). GET /logbook returns it; this module adds a
 * "📜 Logbook" button at the top of the sidebar that opens it in a reading
 * window, and marks the button when the DM announces a change
 * ({"logbook_updated": true}, sent by push_stats.py --logbook-updated).
 *
 * The markdown subset the logbook uses is rendered here: headings, paragraphs,
 * bullet and numbered lists, task lists, pipe tables, bold, italic, links.
 * Everything is escaped first, so the file can never inject markup.
 */
(function () {
  'use strict';

  const SEEN_KEY = 'dnd-logbook-seen';
  let button = null, modal = null, body = null;

  // ── Markdown (pure: tested in node) ──────────────────────────────────────

  function escapeHtml(s) {
    return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;');
  }

  function inline(s) {
    let out = escapeHtml(s);
    out = out.replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>');
    out = out.replace(/(^|[^*])\*([^*\s][^*]*)\*/g, '$1<em>$2</em>');
    out = out.replace(/\[([^\]]+)\]\((https?:\/\/[^)\s]+)\)/g,
      '<a href="$2" target="_blank" rel="noopener noreferrer">$1</a>');
    return out;
  }

  function splitRow(line) {
    return line.trim().replace(/^\|/, '').replace(/\|$/, '').split('|').map(c => c.trim());
  }

  function renderMarkdown(md) {
    const lines = String(md || '').replace(/\r\n?/g, '\n').split('\n');
    const html = [];
    let i = 0;
    while (i < lines.length) {
      const line = lines[i];
      if (!line.trim()) { i++; continue; }
      const h = line.match(/^(#{1,4})\s+(.*)$/);
      if (h) {
        const level = Math.min(4, h[1].length + 1);   // the page title is the window's, so # → h2
        html.push('<h' + level + '>' + inline(h[2]) + '</h' + level + '>');
        i++;
        continue;
      }
      if (/^\s*\|/.test(line) && i + 1 < lines.length && /^\s*\|?\s*:?-{2,}/.test(lines[i + 1])) {
        const head = splitRow(line);
        i += 2;
        const rows = [];
        while (i < lines.length && /^\s*\|/.test(lines[i])) rows.push(splitRow(lines[i++]));
        html.push('<table><thead><tr>' + head.map(c => '<th>' + inline(c) + '</th>').join('')
          + '</tr></thead><tbody>' + rows.map(r => '<tr>' + r.map(c => '<td>' + inline(c) + '</td>').join('')
          + '</tr>').join('') + '</tbody></table>');
        continue;
      }
      if (/^\s*([-*•]|\d+\.)\s+/.test(line)) {
        const ordered = /^\s*\d+\./.test(line);
        const items = [];
        while (i < lines.length && /^\s*([-*•]|\d+\.)\s+/.test(lines[i])) {
          let item = lines[i].replace(/^\s*([-*•]|\d+\.)\s+/, '');
          const task = item.match(/^\[([ xX])\]\s*(.*)$/);
          if (task) {
            const done = task[1] !== ' ';
            item = '<span class="lb-check' + (done ? ' done' : '') + '">' + (done ? '☑' : '☐') + '</span> ' + inline(task[2]);
          } else {
            item = inline(item);
          }
          items.push('<li>' + item + '</li>');
          i++;
        }
        html.push(ordered ? '<ol>' + items.join('') + '</ol>' : '<ul>' + items.join('') + '</ul>');
        continue;
      }
      const para = [];
      while (i < lines.length && lines[i].trim() && !/^(#{1,4}\s|\s*\||\s*([-*•]|\d+\.)\s+)/.test(lines[i])) {
        para.push(lines[i++].trim());
      }
      if (para.length) html.push('<p>' + inline(para.join(' ')) + '</p>');
      else i++;
    }
    return html.join('\n');
  }

  // ── UI ───────────────────────────────────────────────────────────────────

  function el(tag, cls, text) {
    const e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text != null) e.textContent = text;
    return e;
  }

  function seen() { try { return parseFloat(localStorage.getItem(SEEN_KEY) || '0') || 0; } catch (_) { return 0; } }
  function markSeen(ts) { try { localStorage.setItem(SEEN_KEY, String(ts || Date.now() / 1000)); } catch (_) {} }

  function setUnread(on) { if (button) button.classList.toggle('lb-new', !!on); }

  function ensureUi() {
    if (button) return;
    const css = document.createElement('link');
    css.rel = 'stylesheet';
    css.href = '/static/css/logbook.css';
    document.head.appendChild(css);

    const sidebar = document.getElementById('sidebar');
    button = el('button', null, '📜 Logbook');
    button.id = 'logbook-btn';
    button.type = 'button';
    button.title = 'What your characters know, sorted by quest';
    button.addEventListener('click', open);
    if (sidebar) sidebar.insertBefore(button, sidebar.firstChild);
    else document.body.appendChild(button);

    modal = el('div', 'lb-modal');
    modal.hidden = true;
    const panel = el('div', 'lb-panel');
    const head = el('div', 'lb-head');
    head.appendChild(el('div', 'lb-title', 'Logbook'));
    const close = el('button', 'lb-close', '✕');
    close.type = 'button';
    close.addEventListener('click', () => { modal.hidden = true; });
    head.appendChild(close);
    body = el('div', 'lb-body');
    panel.appendChild(head);
    panel.appendChild(body);
    modal.appendChild(panel);
    modal.addEventListener('click', e => { if (e.target === modal) modal.hidden = true; });
    document.addEventListener('keydown', e => { if (e.key === 'Escape' && modal) modal.hidden = true; });
    document.body.appendChild(modal);
  }

  function fetchLogbook() {
    return fetch('/logbook', { cache: 'no-store' }).then(r => (r.ok ? r.json() : { markdown: '' }));
  }

  function open() {
    ensureUi();
    body.textContent = 'Loading…';
    modal.hidden = false;
    fetchLogbook().then(data => {
      if (!data.markdown) {
        body.innerHTML = '<p class="lb-empty">The logbook is still empty.</p>';
      } else {
        body.innerHTML = renderMarkdown(data.markdown);
      }
      markSeen(data.updated);
      setUnread(false);
    }).catch(() => { body.textContent = 'The logbook could not be loaded.'; });
  }

  function checkUnread() {
    fetchLogbook().then(data => setUnread(!!data.updated && data.updated > seen())).catch(() => {});
  }

  window.Logbook = Object.freeze({ open: open, _render: renderMarkdown });

  if (typeof document !== 'undefined' && window.DisplayModules && !window.DisplayModules.context.isPhone) {
    const init = () => { ensureUi(); checkUnread(); };
    if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
    else init();
  }

  if (window.DisplayModules) {
    window.DisplayModules.register('logbook', payload => {
      if (payload.logbook_updated) {
        if (modal && !modal.hidden) open();
        else setUnread(true);
      }
    }, { displayOnly: true });
  }
})();
