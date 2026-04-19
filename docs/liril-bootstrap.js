/* ═══════════════════════════════════════════════════════════════════════
   LIRIL BOOTSTRAP — Canonical per-page script loader
   Modified: 2026-04-19 | claude_code | Initial unification pass

   ONE script tag a page needs to include (other than its own content):

       <script src="/js/liril-bootstrap.js?v=1" defer></script>

   Everything TENET5 considers "core" is loaded from here, in a
   deterministic order, with double-init guards. A page author never
   has to remember whether nav.js goes before or after footer.js, or
   whether liril-voice.js must precede liril-walkthrough.js.

   What it loads (in order)
   ------------------------
     1. /nav.js              — shared two-tier header
     2. /footer.js           — shared footer
     3. /js/ux.js            — reading progress bar, back-to-top, mobile nav
     4. /js/i18n.js          — language strings shared across pages
     5. /js/slate.js         — motion + page polish
     6. /js/flow.js          — scroll flow animations
     7. /js/integrity.js     — hallucination-gate + source verifier
     8. /js/cinema.js        — background cinema layer
     9. /js/liril-voice.js   — voice resolver (must precede walkthrough)
    10. /js/liril-walkthrough.js  — sectional narration engine
    11. /js/presentation.js  — page indicator pill
    12. /js/readnext.js      — next-page suggester

   Pages that don't want everything can set this BEFORE the script tag:
     <script>window.LIRIL_BOOTSTRAP_SKIP = {
       cinema: true,         // skip cinema.js
       walkthrough: true,    // skip the narration stack
     };</script>

   Public API exposed on window.LIRIL (after DOMContentLoaded)
   -----------------------------------------------------------
     window.LIRIL.ready           Promise that resolves when all scripts loaded
     window.LIRIL.version         "1.0"
     window.LIRIL.startWalkthrough()   Trigger the narration from JS
     window.LIRIL.stopWalkthrough()    Stop an in-progress narration
     window.LIRIL.speak(text)          Push a sentence to the voice engine
     window.LIRIL.isAvailable(name)    Did a particular dep successfully load?

   The bootstrap emits a custom event when ready:
     document.dispatchEvent(new CustomEvent('liril:ready', {detail:{...}}))
   ═══════════════════════════════════════════════════════════════════════ */

(function () {
  'use strict';

  if (window.__LIRIL_BOOTSTRAP_LOADED) return;
  window.__LIRIL_BOOTSTRAP_LOADED = true;

  var VERSION = '1.0';
  var skip = (typeof window.LIRIL_BOOTSTRAP_SKIP === 'object' &&
              window.LIRIL_BOOTSTRAP_SKIP) || {};

  /* Canonical script manifest. The order of this array IS the load order. */
  var MANIFEST = [
    { key: 'nav',          src: '/nav.js?v=17' },
    { key: 'footer',       src: '/footer.js?v=2' },
    { key: 'ux',           src: '/js/ux.js?v=1' },
    { key: 'i18n',         src: '/js/i18n.js?v=4' },
    { key: 'slate',        src: '/js/slate.js' },
    { key: 'flow',         src: '/js/flow.js' },
    { key: 'integrity',    src: '/js/integrity.js' },
    { key: 'cinema',       src: '/js/cinema.js?v=2' },
    { key: 'voice',        src: '/js/liril-voice.js' },
    { key: 'walkthrough',  src: '/js/liril-walkthrough.js' },
    { key: 'presentation', src: '/js/presentation.js' },
    { key: 'readnext',     src: '/readnext.js' }
  ];

  var loaded = {};

  function alreadyPresent(src) {
    /* Any <script> whose src starts with the same path (ignoring query) */
    var href = src.split('?')[0];
    var all  = document.getElementsByTagName('script');
    for (var i = 0; i < all.length; i++) {
      var s = all[i].getAttribute('src') || '';
      if (s && s.split('?')[0].indexOf(href) !== -1) return true;
    }
    return false;
  }

  function loadOne(entry) {
    return new Promise(function (resolve) {
      if (skip[entry.key]) {
        loaded[entry.key] = 'skipped';
        return resolve();
      }
      if (alreadyPresent(entry.src)) {
        loaded[entry.key] = 'already';
        return resolve();
      }
      var s = document.createElement('script');
      s.src = entry.src;
      s.defer = true;
      s.setAttribute('data-liril-bootstrap', entry.key);
      s.onload  = function () { loaded[entry.key] = 'loaded'; resolve(); };
      s.onerror = function () { loaded[entry.key] = 'error';  resolve(); };
      document.head.appendChild(s);
    });
  }

  /* Sequential load so later scripts can assume earlier ones are ready. */
  function loadAll() {
    return MANIFEST.reduce(function (p, entry) {
      return p.then(function () { return loadOne(entry); });
    }, Promise.resolve());
  }

  /* ── Public API on window.LIRIL ─────────────────────────────────── */

  var readyResolve;
  var readyPromise = new Promise(function (r) { readyResolve = r; });

  window.LIRIL = {
    version: VERSION,
    ready: readyPromise,
    modules: loaded,

    isAvailable: function (name) {
      return loaded[name] === 'loaded' ||
             loaded[name] === 'already';
    },

    startWalkthrough: function () {
      if (typeof window.__LIRIL_WALKTHROUGH_START === 'function') {
        return window.__LIRIL_WALKTHROUGH_START();
      }
      /* Fallback: synthesize a click on the canonical CTA if present */
      var cta = document.querySelector('.liril-walkthrough-cta');
      if (cta) { cta.click(); return true; }
      return false;
    },

    stopWalkthrough: function () {
      if (typeof window.__LIRIL_WALKTHROUGH_STOP === 'function') {
        return window.__LIRIL_WALKTHROUGH_STOP();
      }
      return false;
    },

    speak: function (text) {
      if (window.LIRIL_VOICE && typeof window.LIRIL_VOICE.speak === 'function') {
        return window.LIRIL_VOICE.speak(text);
      }
      /* Last-resort: fall through to the native speech synth if available */
      if (typeof window.speechSynthesis !== 'undefined') {
        var u = new SpeechSynthesisUtterance(String(text || ''));
        window.speechSynthesis.speak(u);
        return true;
      }
      return false;
    }
  };

  /* Kick off loading. */
  function boot() {
    loadAll().then(function () {
      /* Mark document so CSS / other scripts can react */
      try { document.documentElement.setAttribute('data-liril-ready', '1'); } catch (e) {}
      /* Dispatch the ready event */
      try {
        document.dispatchEvent(new CustomEvent('liril:ready', {
          detail: { version: VERSION, modules: Object.assign({}, loaded) }
        }));
      } catch (e) {}
      if (readyResolve) readyResolve({ version: VERSION, modules: loaded });
    });
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', boot, { once: true });
  } else {
    boot();
  }
})();
