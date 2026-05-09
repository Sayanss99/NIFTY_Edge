/**
 * NiftyEdge — Living Terminal layer (ne-live.js)
 *
 *  Read-only enhancement over whatever your existing JS pushes into the DOM:
 *   • Detects when .metric-value / .pnl-value / .price text changes and
 *     pulses the element green (rising) or red (falling).
 *   • Builds a 30-tick history per element and renders a tiny inline-SVG
 *     sparkline into the parent .metric-card / .card so each tile gains a
 *     "what's been happening" trace — same data, visualized over time.
 *   • Wraps each .metric-card's value in a radial dial decoration: a
 *     conic-gradient ring whose fill normalizes to that card's own min/max.
 *   • Tracks cursor over data-table rows and writes --mx / --my CSS vars so
 *     the v4 stylesheet can paint a soft cobalt spotlight under the pointer.
 *   • Adds a "ticking" pulse to any element with [data-live] or .live-dot.
 *
 *  Backend untouched. Markup the existing app reads/writes is untouched.
 *  Activated by data-ne-vibe="living" (or "pulse") on <html>; "calm" disables.
 */
(function () {
    'use strict';

    const root = document.documentElement;
    const vibeOf = () => root.getAttribute('data-ne-vibe') || 'living';

    // ── Helpers ─────────────────────────────────────────────────────────
    const PULSE_SEL = '.metric-value, .pnl-value, .price, [data-live-num]';
    const histories = new WeakMap();
    const HISTORY_MAX = 30;
    // Persist history to localStorage so the conic-gradient ring on each
    // metric card has context to normalise against even when the WS is
    // quiet (off-market hours, network blip, page just opened). Without
    // this, history on a fresh load is a single sample → norm = 0.5
    // default → ring shows a stuck half-fill that looks broken.
    const STORE_PREFIX = 'ne-hist:';
    const STORE_VER    = 'v1';

    function parseNum(s) {
        if (s == null) return NaN;
        // Strip everything except digits, sign, dot, and percent — also handle commas
        const cleaned = String(s).replace(/[, ]/g, '').match(/-?\d+(?:\.\d+)?/);
        return cleaned ? parseFloat(cleaned[0]) : NaN;
    }

    // Stable per-element key — page path + the nearest label text. Multiple
    // .metric-value on the same page need different keys, and the same metric
    // on the same page needs the SAME key across reloads. The metric-label is
    // a content-anchored identifier that survives DOM rebuilds.
    function elKey(el) {
        const card = el.closest('.metric-card, .card, .summary-card, .position-card');
        let label = '';
        if (card) {
            const lbl = card.querySelector('.metric-label, .card-title, .lbl');
            if (lbl) label = (lbl.textContent || '').trim().toLowerCase().replace(/\s+/g, '-');
        }
        if (!label) label = (el.id || el.className || '').toString().slice(0, 40);
        return STORE_PREFIX + STORE_VER + ':' + location.pathname + ':' + label;
    }

    function loadHistory(el) {
        try {
            const k = elKey(el);
            if (!k.endsWith(':')) {
                const raw = localStorage.getItem(k);
                if (raw) {
                    const arr = JSON.parse(raw);
                    if (Array.isArray(arr)) return arr.filter(v => Number.isFinite(v)).slice(-HISTORY_MAX);
                }
            }
        } catch (e) { /* quota / private mode — degrade silently */ }
        return [];
    }

    function saveHistory(el, h) {
        try {
            const k = elKey(el);
            if (k.endsWith(':')) return;
            localStorage.setItem(k, JSON.stringify(h.slice(-HISTORY_MAX)));
        } catch (e) { /* quota — ignore */ }
    }

    function pulse(el, dir) {
        if (vibeOf() === 'calm') return;
        el.classList.remove('ne-pulse-up', 'ne-pulse-down');
        // force reflow so animation can replay
        void el.offsetWidth;
        el.classList.add(dir > 0 ? 'ne-pulse-up' : 'ne-pulse-down');
        clearTimeout(el._neTimer);
        el._neTimer = setTimeout(() => {
            el.classList.remove('ne-pulse-up', 'ne-pulse-down');
        }, 1300);
    }

    function buildSparkPath(h, w, hgt) {
        if (h.length < 2) return '';
        const min = Math.min.apply(null, h);
        const max = Math.max.apply(null, h);
        const span = (max - min) || 1;
        let d = '';
        for (let i = 0; i < h.length; i++) {
            const x = (i / (h.length - 1)) * w;
            const y = hgt - ((h[i] - min) / span) * hgt;
            d += (i === 0 ? 'M' : 'L') + x.toFixed(2) + ',' + y.toFixed(2) + ' ';
        }
        return d.trim();
    }

    function ensureSparkSlot(card) {
        if (card.__neSpark) return card.__neSpark;
        const slot = document.createElement('div');
        slot.className = 'ne-spark-slot';
        slot.setAttribute('aria-hidden', 'true');
        slot.innerHTML =
            '<svg viewBox="0 0 100 24" preserveAspectRatio="none">' +
            '<defs><linearGradient id="ne-spark-grad" x1="0" y1="0" x2="0" y2="1">' +
            '<stop offset="0%" stop-color="currentColor" stop-opacity="0.55"/>' +
            '<stop offset="100%" stop-color="currentColor" stop-opacity="0"/>' +
            '</linearGradient></defs>' +
            '<path class="ne-spark-area" d="" fill="url(#ne-spark-grad)" stroke="none"/>' +
            '<path class="ne-spark-line" d="" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linejoin="round" stroke-linecap="round"/>' +
            '</svg>';
        card.appendChild(slot);
        card.__neSpark = slot;
        return slot;
    }

    // The conic-gradient dial ring was removed — it added visual noise
    // without communicating anything actionable.  We keep --ne-norm being
    // written to the card (cheap, harmless) so any future visual layer can
    // pick it up, but we no longer mount the ring element itself.
    function ensureDial(_metricCard, _valueEl) { /* no-op */ }

    function track(el) {
        const val = parseNum(el.textContent);
        if (!isFinite(val)) return;

        // Hydrate from localStorage on first touch. This is what makes the
        // conic-gradient ring active out-of-market: even without a single
        // WS push, the ring renders against the previous session's history.
        let h = histories.get(el);
        if (!h) {
            h = loadHistory(el);
            histories.set(el, h);
        }
        const last = h.length ? h[h.length - 1] : null;
        if (last !== null && Math.abs(val - last) > 1e-9) {
            pulse(el, val - last);
        }
        // Only append on actual change so a stale page that re-renders the
        // same value 100× doesn't fill history with duplicates.
        if (last === null || Math.abs(val - last) > 1e-9) {
            h.push(val);
            if (h.length > HISTORY_MAX) h.shift();
            saveHistory(el, h);
        }

        // Mirror to parent card for CSS-driven visuals
        const card = el.closest('.metric-card, .card, .summary-card, .position-card, .future-card, .futures-tile');
        if (!card) return;

        if (card.classList.contains('metric-card')) {
            ensureDial(card, el);
        }

        const min = Math.min.apply(null, h);
        const max = Math.max.apply(null, h);
        const span = (max - min) || 1;
        // With persisted history, h.length is almost always ≥ 2 immediately
        // — so the ring shows real positional context rather than the 0.5
        // default fallback that made it look frozen out-of-market.
        const norm = h.length >= 2 ? (val - min) / span : 0.5;
        card.style.setProperty('--ne-norm', norm.toFixed(3));

        // Direction class — used by spark stroke colour
        if (last !== null) {
            card.classList.toggle('ne-trend-up',   val > last);
            card.classList.toggle('ne-trend-down', val < last);
        }

        // Sparkline path (only when we have enough samples)
        if (h.length >= 4) {
            const slot = ensureSparkSlot(card);
            const linePath = buildSparkPath(h, 100, 24);
            const areaPath = linePath + ' L100,24 L0,24 Z';
            const lineEl = slot.querySelector('.ne-spark-line');
            const areaEl = slot.querySelector('.ne-spark-area');
            if (lineEl) lineEl.setAttribute('d', linePath);
            if (areaEl) areaEl.setAttribute('d', areaPath);
            card.classList.add('ne-has-spark');
        }
    }

    // ── Initial scan + observation ──────────────────────────────────────
    function scanAll() {
        document.querySelectorAll(PULSE_SEL).forEach(track);
    }

    const observer = new MutationObserver((mutations) => {
        if (vibeOf() === 'calm') return;
        const seen = new Set();
        for (const m of mutations) {
            // Walk up to find an element matching PULSE_SEL
            let n = m.target;
            // Text nodes report parent on characterData mutations
            if (n && n.nodeType === 3) n = n.parentNode;
            while (n && n !== document.body) {
                if (n.matches && n.matches(PULSE_SEL) && !seen.has(n)) {
                    seen.add(n);
                    track(n);
                    break;
                }
                n = n.parentNode;
            }
            // Also catch newly-added nodes
            if (m.addedNodes) {
                m.addedNodes.forEach(node => {
                    if (node.nodeType !== 1) return;
                    if (node.matches && node.matches(PULSE_SEL)) track(node);
                    if (node.querySelectorAll) {
                        node.querySelectorAll(PULSE_SEL).forEach(track);
                    }
                });
            }
        }
    });

    function start() {
        try {
            scanAll();
            observer.observe(document.body, {
                childList: true,
                subtree: true,
                characterData: true
            });
        } catch (e) { /* defensive */ }

        // Cursor spotlight on data-table rows (CSS reads --mx/--my)
        const ROW_SEL = '.data-table tbody tr, .journal-table tbody tr, .history-table tbody tr, .option-chain-table tbody tr';
        document.addEventListener('mousemove', (e) => {
            if (vibeOf() === 'calm') return;
            const tgt = e.target;
            if (!tgt || !tgt.closest) return;
            const tr = tgt.closest(ROW_SEL);
            if (!tr) return;
            const r = tr.getBoundingClientRect();
            tr.style.setProperty('--mx', (e.clientX - r.left) + 'px');
            tr.style.setProperty('--my', (e.clientY - r.top) + 'px');
        }, { passive: true });

        // Periodic re-scan for late-mounted DOM (some pages render async)
        let rescans = 0;
        const rescanTimer = setInterval(() => {
            scanAll();
            if (++rescans > 12) clearInterval(rescanTimer);
        }, 1500);
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', start);
    } else {
        start();
    }

    window.NELive = { rescan: scanAll };
})();
