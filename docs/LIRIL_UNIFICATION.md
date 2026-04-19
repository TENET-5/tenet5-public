# LIRIL Unification — Canonical Page Template

**Status:** Active migration, started 2026-04-19.
**Scope:** All 308 HTML pages on `tenet-5.github.io`.

This document defines the single canonical way every page loads its theme,
scripts, and the LIRIL walkthrough. If a page deviates from this pattern,
it's a migration candidate — *not* a reason to invent a new pattern.

---

## Rule #1 — Two link tags. That's it.

Every page `<head>` should end with **exactly these two tags**:

```html
<link rel="stylesheet" href="/css/liril-unified.css?v=1">
<script src="/js/liril-bootstrap.js?v=1" defer></script>
```

Everything else (nav, footer, voice, walkthrough, presentation pill,
read-next, ux progress bar, i18n strings, integrity gate, cinema
background) is loaded by `liril-bootstrap.js` in a deterministic
order with double-init guards.

---

## What the unified stack provides

| Feature                               | Source                   |
|---------------------------------------|--------------------------|
| Red Ensign Royal Canadian theme       | `css/liril-unified.css`  |
| CSS variables (`--liril-ensign-red`,  |                          |
| `--liril-gold`, `--liril-navy`, etc.) | `css/liril-unified.css`  |
| Two-tier header with crest            | `nav.js`                 |
| Shared footer                         | `footer.js`              |
| Reading progress / back-to-top / mobile nav | `js/ux.js`         |
| i18n strings (en/fr/es)               | `js/i18n.js`             |
| Page slide-in motion                  | `js/slate.js`            |
| Scroll-reveal flow                    | `js/flow.js`             |
| Hallucination-gate / source verifier  | `js/integrity.js`        |
| Cinema background layer               | `js/cinema.js`           |
| LIRIL voice (Clara, speechSynthesis)  | `js/liril-voice.js`      |
| Walkthrough engine (200 sections max) | `js/liril-walkthrough.js`|
| Page-indicator pill                   | `js/presentation.js`     |
| "Next page" suggester                 | `readnext.js`            |

---

## Public JS API

After `document.addEventListener('liril:ready', fn)` fires, the following
is reliably available on every page:

```js
window.LIRIL.version                 // "1.0"
window.LIRIL.ready                   // Promise<{version, modules}>
window.LIRIL.modules                 // {nav:"loaded", voice:"loaded", ...}
window.LIRIL.isAvailable("voice")    // true | false
window.LIRIL.startWalkthrough()      // fire narration from JS
window.LIRIL.stopWalkthrough()
window.LIRIL.speak("One sentence.")  // push to voice engine
```

Event listeners:

```js
document.addEventListener('liril:ready', function(e) {
  console.log('LIRIL v' + e.detail.version + ' modules:', e.detail.modules);
});
```

---

## CSS custom properties (don't hard-code colors again)

```css
/* Brand */
--liril-ensign-red        /* #c41e3a */
--liril-ensign-red-deep   /* #8f1228 */
--liril-gold              /* #d4af37 */
--liril-gold-bright       /* #f0cc5c */
--liril-navy              /* #0b1220 */
--liril-navy-deep         /* #07101d */
--liril-navy-raised       /* #111a2b */
--liril-navy-border       /* rgba(212,175,55,0.18) */

/* Text */
--liril-text-primary      /* #ecf0f7 */
--liril-text-secondary    /* #aeb7c8 */
--liril-text-dim          /* #6b7489 */

/* LIRIL AI accents */
--liril-ai-cyan           /* #22d3ee */
--liril-ai-quantum        /* #a855f7 */
--liril-ai-amber          /* #f59e0b */
--liril-ai-emerald        /* #10b981 */

/* Type, spacing, radii, shadows, motion, z-index */
--liril-font-sans / --liril-font-mono / --liril-font-serif
--liril-radius-sm / --liril-radius-md / --liril-radius-lg / --liril-radius-pill
--liril-pad-xs / --liril-pad-sm / --liril-pad-md / --liril-pad-lg / --liril-pad-xl
--liril-shadow-sm / --liril-shadow-md / --liril-shadow-lg / --liril-shadow-glow
--liril-ease-out / --liril-ease-in-out
--liril-duration-fast / --liril-duration-base / --liril-duration-slow
--liril-z-nav / --liril-z-overlay / --liril-z-walkthrough / --liril-z-toast
```

---

## Shared component classes

```html
<!-- Canonical "Start LIRIL walkthrough" CTA -->
<button class="liril-walkthrough-cta">LIRIL Walkthrough</button>

<!-- Status pill -->
<span class="liril-badge" data-tone="ai">LIRIL narration</span>
<span class="liril-badge" data-tone="live">Live OSINT</span>
<span class="liril-badge" data-tone="quantum">NV-quantum</span>

<!-- Heartbeat dot -->
<span class="liril-status-dot" aria-label="live"></span>
```

`liril-walkthrough.js` automatically applies `.liril-section-active`
to the section it is currently narrating, and renders the
`.liril-walkthrough-subtitle` band at the bottom of the viewport.

---

## Opt-outs (rare)

Some pages (landing, narrow embed frames, static PDFs) don't need the
full stack. Declare skip flags BEFORE the bootstrap tag:

```html
<script>
  window.LIRIL_BOOTSTRAP_SKIP = {
    cinema:       true,   // no cinema background
    walkthrough:  true,   // no narration / subtitle
    readnext:     true    // no "next page" suggester
  };
</script>
<script src="/js/liril-bootstrap.js?v=1" defer></script>
```

Valid keys: `nav`, `footer`, `ux`, `i18n`, `slate`, `flow`,
`integrity`, `cinema`, `voice`, `walkthrough`, `presentation`,
`readnext`.

---

## Migration recipe for an existing page

1. Remove all `<link rel="stylesheet">` tags that point at `style.css`,
   `css/polish.css`, `css/inline_generated.css`, `style-slate.css`,
   `style-slate-motion.css` — those are now re-imported inside
   `liril-unified.css` OR are legitimate page-specific extensions that
   should be loaded AFTER the unified stylesheet.
2. Remove `<script>` tags that point at any of: `nav.js`, `footer.js`,
   `js/ux.js`, `js/i18n.js`, `js/slate.js`, `js/flow.js`,
   `js/integrity.js`, `js/cinema.js`, `js/liril-voice.js`,
   `js/liril-walkthrough.js`, `js/presentation.js`, `readnext.js`.
   `liril-bootstrap.js` loads these.
3. Add the two canonical tags (see Rule #1).
4. If the page had custom CSS that referenced hard-coded colors, try
   to replace them with the `--liril-*` tokens — grep is your friend.
5. Load the page locally and verify:
     * The heraldic crest renders at the top.
     * The LIRIL walkthrough CTA appears bottom-right.
     * Clicking the CTA starts narration and highlights sections.
     * No console errors.

---

## What does NOT belong here

- Page-specific colours, one-off typography, unique layout — those
  stay in inline `<style>` blocks or page-specific CSS.
- Per-page presentation variants — those are page concerns, not
  unified-stack concerns.
- Duplicate walkthrough implementations — there is ONE walkthrough
  engine (`liril-walkthrough.js`). Anything else should be neutralized
  to a no-op shim and linked back to it, the same way
  `walkthrough-enhancements.js` already was.

---

## FAQ

**Q. My page loads `style-slate.css` for a specific look. Where does that go?**
A. AFTER the unified stylesheet, so its rules override. Example:
```html
<link rel="stylesheet" href="/css/liril-unified.css?v=1">
<link rel="stylesheet" href="/style-slate.css?v=2">
<link rel="stylesheet" href="/style-slate-motion.css?v=1">
```

**Q. The bootstrap loads `cinema.js` and my page doesn't want it.**
A. Opt out via `LIRIL_BOOTSTRAP_SKIP` (see above).

**Q. I need an extra script that isn't in the manifest.**
A. Add it as a normal `<script>` tag after the bootstrap. The
bootstrap doesn't fight with pages that load their own extras.

**Q. I want to start the walkthrough automatically on load.**
A. Listen for `liril:ready`, then call `LIRIL.startWalkthrough()`.
Do NOT auto-start on first landing (Daniel's directive: walkthrough
runs only when the user explicitly clicks).

---

## Next migration targets (tracking)

In order of traffic / visibility:

- [ ] `index.html` — iframe frame, migration has a shell.js consideration (see below)
- [x] `about.html` — first reference implementation (2026-04-19, +4 lines)
- [ ] `accountability.html`
- [ ] `records.html`
- [ ] `search.html`
- [ ] Remaining 303 pages (batched; ~60 per pass)

### index.html caveat

`index.html` is the iframe FRAME (loads content pages via `shell.js`).
Its relationship to the unified stack is different: the content pages
inside the iframe use the bootstrap; the frame itself only needs the
unified CSS so the nav/footer rendered by `shell.js` can reference
`--liril-*` tokens. Migration of `index.html` is one CSS swap, no JS.

---

*Every new page should be migrated AT BIRTH. Old patterns don't get a
grace period.*
