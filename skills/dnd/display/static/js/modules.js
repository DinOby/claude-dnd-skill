/* modules.js — plug-in point for display feature modules.
 *
 * index.html owns the SSE connection and its built-in rendering. After it has
 * handled a message it hands the same parsed payload to every registered
 * module, so features (action overlays, grid view, …) live in their own files
 * and never touch the core handler:
 *
 *   DisplayModules.register('vfx', (payload, ctx) => {
 *     if (payload.vfx) showOverlay(payload.vfx);
 *   }, { displayOnly: true });
 *
 * Contract:
 *   - payload is the server's JSON message, unchanged. Treat it as read-only.
 *   - ctx is DisplayModules.context: { isPhone, character }. isPhone is true
 *     for the player controller (?view=input, ?char=, ?character=), using the
 *     same rule as index.html's input-only mode.
 *   - displayOnly: true skips the handler on phones.
 *   - A throwing handler is logged and skipped; it cannot break the display
 *     or other modules.
 *
 * Load order: modules.js first, then feature modules, all as plain (non-defer)
 * <script> tags before the main inline script. The SSE connection opens in
 * that script, so every module is registered before the first message.
 */
(function () {
  'use strict';

  const params = new URLSearchParams(location.search);
  const character = (params.get('char') || params.get('character') || '').trim();
  const context = Object.freeze({
    isPhone: params.get('view') === 'input' || params.has('char') || params.has('character'),
    character: character,
  });

  const handlers = [];

  function register(name, handler, opts) {
    if (typeof name !== 'string' || !name) throw new TypeError('module name required');
    if (typeof handler !== 'function') throw new TypeError('handler must be a function');
    unregister(name);   // re-registering replaces, so reloading a module is idempotent
    handlers.push({ name: name, handler: handler, displayOnly: !!(opts && opts.displayOnly) });
  }

  function unregister(name) {
    const i = handlers.findIndex(h => h.name === name);
    if (i !== -1) handlers.splice(i, 1);
  }

  function dispatch(payload) {
    for (const h of handlers.slice()) {
      if (h.displayOnly && context.isPhone) continue;
      try {
        h.handler(payload, context);
      } catch (err) {
        console.error('[DisplayModules] ' + h.name + ':', err);
      }
    }
  }

  window.DisplayModules = Object.freeze({
    context: context,
    register: register,
    unregister: unregister,
    dispatch: dispatch,
    list: () => handlers.map(h => h.name),
  });
})();
