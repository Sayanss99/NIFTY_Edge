/**
 * Stratovex — Scroll Reveal layer (ne-scroll.js)
 *
 *  Pure progressive enhancement: content animates in on load AND re-animates each
 *  time it scrolls back into view, with a treatment matched to how the data is
 *  presented. Read-only over whatever the existing JS renders.
 *
 *  Treatments:
 *   • cards / tiles / panels  → rise + fade
 *   • visualizations (charts) → fade + scale-up (container only; canvas undistorted)
 *   • log tables (journal / option chain) → row cascade on re-entry
 *
 *  Behaviour & safety:
 *   • Blocks: hidden then revealed by the observer, so above-the-fold blocks get an
 *     entrance animation on load and below-the-fold ones reveal as you reach them;
 *     scrolling away and back re-animates them.
 *   • A card that WRAPS a log table is never hidden — its rows stay visible (logs
 *     are never invisible-until-scroll); the rows themselves cascade on re-entry.
 *   • Animations use the independent `translate`/`scale` CSS properties, so they
 *     compose with the existing transform-based hover lifts instead of fighting them.
 *   • Live row swaps (option chain) inherit the shown state with no transition →
 *     they appear instantly, no flicker, no replay.
 *   • Bails out entirely (everything visible) under prefers-reduced-motion, the
 *     Tweaks "Calm" vibe, or when IntersectionObserver is unavailable. The hidden
 *     classes are ONLY added by JS, so if it never runs nothing is hidden.
 */
(function () {
    'use strict';

    const root = document.documentElement;

    function motionOff() {
        try {
            if (root.getAttribute('data-ne-vibe') === 'calm') return true;
            if (window.matchMedia &&
                window.matchMedia('(prefers-reduced-motion: reduce)').matches) return true;
            if (!('IntersectionObserver' in window)) return true;
        } catch (e) { return true; }
        return false;
    }

    const SEL_BLOCK = [
        '.card', '.metric-card', '.summary-card', '.position-card',
        '.future-card', '.futures-tile', '.chart-card', '.chart-container',
        '.panel', '[data-ne-reveal]'
    ].join(',');

    // Log tables whose ROWS cascade. Narrow on purpose — not generic .data-table.
    const SEL_ROW_TABLE = '.journal-table, .chain-table';
    const SKIP_ANCESTOR = '.modal, .modal-overlay, .ne-tweaks-panel, [role="dialog"]';

    let io = null;

    // ── Blocks: cards & visualizations ──────────────────────────────────────
    function blockKind(el) {
        if (el.matches('.chart-container, .chart-card') || el.querySelector('canvas, svg.chart'))
            return 'ne-reveal-viz';
        return 'ne-reveal';
    }
    function isTopLevelBlock(el) {
        const parent = el.parentElement;
        if (parent && parent.closest(SEL_BLOCK)) return false;
        if (el.closest(SKIP_ANCESTOR)) return false;
        // Never hide a card that wraps a log table — its rows must stay visible.
        if (el.querySelector(SEL_ROW_TABLE)) return false;
        return true;
    }
    function armBlock(el) {
        if (!el || el.__neArmed || !el.matches || !el.matches(SEL_BLOCK)) return;
        if (!isTopLevelBlock(el)) return;
        el.__neArmed = true;
        el.classList.add(blockKind(el));   // hidden until the observer reveals it
        io.observe(el);
    }

    // ── Log tables: rows visible on load, cascade on re-entry ───────────────
    function armTbody(tbody) {
        if (!tbody || tbody.__neArmed) return;
        if (tbody.closest && tbody.closest(SKIP_ANCESTOR)) return;
        tbody.__neArmed = true;
        tbody.classList.add('ne-rows');
        if (tbody.rows && tbody.rows.length) {       // reveal immediately (never invisible)
            tbody.classList.add('ne-rows-in');
            tbody.__neRevealed = true;
        }
        io.observe(tbody);
    }
    function armRowTable(tableEl) {
        const tbody = tableEl && tableEl.tBodies && tableEl.tBodies[0];
        if (tbody) armTbody(tbody);
    }
    // Rows that arrive after arm (async load) → reveal once so logs never hide.
    function refreshTbodies() {
        document.querySelectorAll(SEL_ROW_TABLE).forEach((t) => {
            const tb = t.tBodies && t.tBodies[0];
            if (tb && tb.classList.contains('ne-rows') && !tb.__neRevealed &&
                tb.rows && tb.rows.length) {
                tb.classList.add('ne-rows-in');
                tb.__neRevealed = true;
            }
        });
    }

    function scan(scope) {
        const s = scope || document;
        try {
            s.querySelectorAll(SEL_BLOCK).forEach(armBlock);
            s.querySelectorAll(SEL_ROW_TABLE).forEach(armRowTable);
        } catch (e) { /* defensive */ }
    }

    function start() {
        if (motionOff()) return;

        io = new IntersectionObserver((entries) => {
            let idx = 0;
            for (const entry of entries) {
                const el = entry.target;
                if (el.tagName === 'TBODY') {
                    if (entry.isIntersecting) {
                        el.__neSeen = true;
                        if (el.rows && el.rows.length) {
                            el.classList.add('ne-rows-in');
                            el.__neRevealed = true;
                        }
                    } else if (el.__neSeen) {
                        el.classList.remove('ne-rows-in');   // reset only after first sighting
                    }
                    continue;
                }
                if (entry.isIntersecting) {
                    el.style.transitionDelay = (Math.min(idx++, 6) * 60) + 'ms';
                    el.classList.add('ne-in');
                } else {
                    el.style.transitionDelay = '';
                    el.classList.remove('ne-in');
                }
            }
        }, { root: null, rootMargin: '0px 0px -8% 0px', threshold: 0.06 });

        scan(document);   // script is at end of body → static content already parsed

        // Re-render hook: arm new nodes synchronously (no flash on JS-rendered cards).
        try {
            new MutationObserver((muts) => {
                for (const m of muts) {
                    if (!m.addedNodes) continue;
                    m.addedNodes.forEach((n) => {
                        if (n.nodeType !== 1) return;
                        if (n.matches && n.matches(SEL_BLOCK)) armBlock(n);
                        if (n.matches && n.matches(SEL_ROW_TABLE)) armRowTable(n);
                        if (n.querySelectorAll) {
                            n.querySelectorAll(SEL_BLOCK).forEach(armBlock);
                            n.querySelectorAll(SEL_ROW_TABLE).forEach(armRowTable);
                        }
                    });
                }
                refreshTbodies();
            }).observe(document.body, { childList: true, subtree: true });
        } catch (e) {}

        // A few timed passes catch late mounts (pages that render after load).
        let n = 0;
        const t = setInterval(() => {
            scan(document); refreshTbodies();
            if (++n > 8) clearInterval(t);
        }, 700);
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', start);
    } else {
        start();
    }

    window.NEScroll = { rescan: () => { scan(document); refreshTbodies(); } };
})();
