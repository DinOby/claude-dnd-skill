/* assets.js — images for game content (items, tokens, maps).
 *
 * The server owns the name → image logic (display/asset_store.py); this
 * module asks it in batches and caches the answers:
 *
 *   AssetResolver.resolve('item', ['Longsword', 'Heiltrank (2)'])
 *     → Promise<Map name → {name, key, url, category, placeholder}>
 *   AssetResolver.imageFor(info)  → url, or the category placeholder
 *   AssetResolver.decorate(listEl, 'item')
 *     → prepends a thumbnail to every <li data-asset-name|data-srd-name>
 *
 * {"assets_changed": true} over SSE (sent after `assets.py generate`)
 * clears the cache and re-decorates everything decorated so far.
 */
(function () {
  'use strict';

  const cache = new Map();            // "kind\u0000name" → info
  const decorated = new Set();        // [listEl, kind] pairs still in the DOM
  let cssLoaded = false;

  function cacheKey(kind, name) { return kind + '\u0000' + name; }

  function imageFor(info) {
    return (info && (info.url || info.placeholder)) || '/assets/placeholder/gear.svg';
  }

  function resolve(kind, names, category) {
    const out = new Map();
    const missing = [];
    for (const n of names) {
      const hit = cache.get(cacheKey(kind, n));
      if (hit) out.set(n, hit); else if (!missing.includes(n)) missing.push(n);
    }
    if (!missing.length) return Promise.resolve(out);

    const qs = new URLSearchParams({ kind: kind });
    if (category) qs.set('category', category);
    missing.slice(0, 100).forEach(n => qs.append('name', n));
    return fetch('/assets/resolve?' + qs.toString())
      .then(r => (r.ok ? r.json() : { items: [] }))
      .catch(() => ({ items: [] }))
      .then(data => {
        for (const info of data.items || []) {
          cache.set(cacheKey(kind, info.name), info);
          out.set(info.name, info);
        }
        return out;
      });
  }

  function ensureCss() {
    if (cssLoaded || typeof document === 'undefined') return;
    const css = document.createElement('link');
    css.rel = 'stylesheet';
    css.href = '/static/css/assets.css';
    document.head.appendChild(css);
    cssLoaded = true;
  }

  function nameOf(li) {
    return (li.dataset.assetName || li.dataset.srdName || li.textContent || '').trim();
  }

  function decorate(listEl, kind) {
    if (!listEl) return Promise.resolve();
    ensureCss();
    decorated.add([listEl, kind]);
    const items = Array.from(listEl.querySelectorAll('li')).filter(li => nameOf(li));
    return resolve(kind, items.map(nameOf)).then(infos => {
      for (const li of items) {
        const info = infos.get(nameOf(li));
        if (!info) continue;
        let img = li.querySelector(':scope > img.asset-thumb');
        if (!img) {
          img = document.createElement('img');
          img.className = 'asset-thumb';
          img.alt = '';
          img.loading = 'lazy';
          li.prepend(img);
          li.classList.add('has-asset-thumb');
        }
        img.dataset.category = info.category;
        img.classList.toggle('is-placeholder', !info.url);
        img.onerror = () => { img.onerror = null; img.src = info.placeholder; img.classList.add('is-placeholder'); };
        img.src = imageFor(info);
      }
    });
  }

  function refresh() {
    cache.clear();
    for (const pair of Array.from(decorated)) {
      if (!pair[0].isConnected) { decorated.delete(pair); continue; }
      decorate(pair[0], pair[1]);
    }
  }

  window.AssetResolver = Object.freeze({
    resolve: resolve,
    imageFor: imageFor,
    decorate: decorate,
    refresh: refresh,
  });

  if (window.DisplayModules) {
    DisplayModules.register('assets', payload => { if (payload.assets_changed) refresh(); });
  }
})();
