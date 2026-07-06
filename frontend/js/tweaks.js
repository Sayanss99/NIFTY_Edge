/**
 * Stratovex — Tweaks panel
 * Injects a refinement controller on every page. State persists in localStorage.
 * Drives data attributes on <html> consumed by the v3 refinement layer in styles.css.
 */
(function () {
    const KEY = 'niftyedge_tweaks_v1';
    // Baked defaults: dark + cobalt + grid + round + living, per user preference.
    // First visit on any device gets this look; user can still change via Tweaks panel.
    const DEFAULTS = {
        refine: 'on',          // on | off  — toggles the v3 refinement layer
        theme: 'dark',         // auto | light | dark
        hue: 220,              // accent hue (oklch) — cobalt
        density: 'normal',     // compact | normal | spacious
        texture: 'grid',       // grid | scan | none
        radius: 20,            // card radius in px — round
        vibe: 'living'         // calm | living | pulse  — v4 living-terminal layer
    };

    function load() {
        try {
            const raw = localStorage.getItem(KEY);
            if (!raw) return { ...DEFAULTS };
            return { ...DEFAULTS, ...JSON.parse(raw) };
        } catch (e) { return { ...DEFAULTS }; }
    }
    function save(state) { localStorage.setItem(KEY, JSON.stringify(state)); }

    const HUES = [
        { h: 220, name: 'Cobalt' },
        { h: 195, name: 'Cyan'   },
        { h: 160, name: 'Mint'   },
        { h: 80,  name: 'Lime'   },
        { h: 30,  name: 'Amber'  },
        { h: 350, name: 'Magma'  },
    ];

    function applyTheme(mode) {
        const root = document.documentElement;
        if (mode === 'dark') {
            root.setAttribute('data-theme', 'dark');
        } else if (mode === 'light') {
            root.removeAttribute('data-theme');
        } else {
            // auto — respect existing localStorage 'niftyedge_theme' if present
            const saved = localStorage.getItem('niftyedge_theme');
            if (saved === 'dark') root.setAttribute('data-theme', 'dark');
            else root.removeAttribute('data-theme');
        }
    }

    function apply(state) {
        const root = document.documentElement;
        root.setAttribute('data-ne-refine', state.refine);
        root.setAttribute('data-ne-density', state.density);
        root.setAttribute('data-ne-texture', state.texture);
        root.setAttribute('data-ne-vibe', state.vibe || 'living');
        root.style.setProperty('--ne-accent-h', String(state.hue));
        root.style.setProperty('--ne-radius', state.radius + 'px');
        applyTheme(state.theme);
    }

    let state = load();
    apply(state);   // apply ASAP so first paint reflects user prefs

    // ── UI ─────────────────────────────────────────────────────────────
    function el(tag, attrs = {}, html = '') {
        const e = document.createElement(tag);
        for (const k in attrs) {
            if (k === 'class') e.className = attrs[k];
            else if (k.startsWith('on')) e.addEventListener(k.slice(2), attrs[k]);
            else e.setAttribute(k, attrs[k]);
        }
        if (html) e.innerHTML = html;
        return e;
    }

    function buildPanel() {
        const panel = el('div', { class: 'ne-tweaks-panel', id: 'ne-tweaks-panel' });

        // Head
        const head = el('div', { class: 'ne-tweaks-head' });
        head.appendChild(el('div', { class: 'ne-tweaks-title' }, 'Tweaks'));
        const close = el('button', {
            class: 'ne-tweaks-close',
            'aria-label': 'Close',
            onclick: () => togglePanel(false)
        }, '×');
        head.appendChild(close);
        panel.appendChild(head);

        // Body
        const body = el('div', { class: 'ne-tweaks-body' });

        body.appendChild(buildSegRow(
            'Refinement', 'Stratovex v3 visual layer',
            [['on', 'On'], ['off', 'Off']],
            state.refine, v => updateOne('refine', v)
        ));

        body.appendChild(buildSegRow(
            'Theme', 'Light / dark surface',
            [['auto', 'Auto'], ['light', 'Light'], ['dark', 'Dark']],
            state.theme, v => updateOne('theme', v)
        ));

        body.appendChild(buildHueRow());

        body.appendChild(buildSegRow(
            'Density', 'Compact for traders',
            [['compact', 'Tight'], ['normal', 'Normal'], ['spacious', 'Roomy']],
            state.density, v => updateOne('density', v)
        ));

        body.appendChild(buildSegRow(
            'Texture', 'Page background',
            [['grid', 'Grid'], ['scan', 'Scan'], ['none', 'None']],
            state.texture, v => updateOne('texture', v)
        ));

        body.appendChild(buildSegRow(
            'Corners', 'Card radius',
            [['8', 'Sharp'], ['14', 'Soft'], ['20', 'Round']],
            String(state.radius), v => updateOne('radius', parseInt(v, 10))
        ));

        body.appendChild(buildSegRow(
            'Vibe', 'Motion & live visuals',
            [['calm', 'Calm'], ['living', 'Living'], ['pulse', 'Pulse']],
            state.vibe, v => updateOne('vibe', v)
        ));

        panel.appendChild(body);

        // Foot
        const foot = el('div', { class: 'ne-tweaks-foot' });
        const reset = el('button', {
            class: 'ne-reset',
            onclick: () => { state = { ...DEFAULTS }; save(state); apply(state); rebuild(); }
        }, 'Reset');
        foot.appendChild(reset);
        foot.appendChild(el('span', { class: 'ne-stamp' }, 'NE • v3.0'));
        panel.appendChild(foot);

        return panel;
    }

    function buildSegRow(label, hint, options, current, onChange) {
        const row = el('div', { class: 'ne-tweak-row' });
        const lbl = el('div', { class: 'ne-tweak-label' });
        lbl.appendChild(el('span', {}, label));
        lbl.appendChild(el('span', { class: 'ne-tweak-hint' }, hint));
        row.appendChild(lbl);

        const seg = el('div', { class: 'ne-seg' });
        options.forEach(([val, txt]) => {
            const b = el('button', {
                class: String(current) === String(val) ? 'is-on' : '',
                'data-val': val,
                onclick: () => {
                    seg.querySelectorAll('button').forEach(x => x.classList.remove('is-on'));
                    b.classList.add('is-on');
                    onChange(val);
                }
            }, txt);
            seg.appendChild(b);
        });
        row.appendChild(seg);
        return row;
    }

    function buildHueRow() {
        const row = el('div', { class: 'ne-tweak-row', style: 'flex-direction:column;align-items:stretch;gap:8px' });
        const lbl = el('div', { class: 'ne-tweak-label' });
        lbl.appendChild(el('span', {}, 'Accent'));
        lbl.appendChild(el('span', { class: 'ne-tweak-hint' }, 'OKLCH-tuned hue'));
        row.appendChild(lbl);

        const grid = el('div', { class: 'ne-hue-row' });
        HUES.forEach(({ h, name }) => {
            const b = el('button', {
                'data-hue': h,
                'title': name,
                class: state.hue === h ? 'is-on' : '',
                style: `background: oklch(0.62 0.16 ${h});`,
                onclick: () => {
                    grid.querySelectorAll('button').forEach(x => x.classList.remove('is-on'));
                    b.classList.add('is-on');
                    updateOne('hue', h);
                }
            });
            grid.appendChild(b);
        });
        row.appendChild(grid);
        return row;
    }

    function updateOne(k, v) {
        state = { ...state, [k]: v };
        save(state);
        apply(state);
        // Keep the legacy theme store + header toggle in sync so the profile
        // dark-mode switch and this panel never disagree.
        if (k === 'theme' && (v === 'light' || v === 'dark')) {
            localStorage.setItem('niftyedge_theme', v);
            const toggle = document.getElementById('ne-theme-toggle');
            if (toggle) toggle.classList.toggle('on', v === 'dark');
            window.dispatchEvent(new CustomEvent('themechange', { detail: { theme: v } }));
        }
    }

    function rebuild() {
        const old = document.getElementById('ne-tweaks-panel');
        if (!old) return;
        const fresh = buildPanel();
        fresh.classList.toggle('is-open', old.classList.contains('is-open'));
        old.replaceWith(fresh);
    }

    // ── Launcher + protocol ────────────────────────────────────────────
    let launcher, panel;
    function ensureMounted() {
        if (panel) return;
        panel = buildPanel();
        document.body.appendChild(panel);
        launcher = el('button', {
            class: 'ne-tweaks-launcher',
            'aria-label': 'Open Tweaks',
            title: 'Tweaks',
            onclick: () => togglePanel()
        }, '◐');
        document.body.appendChild(launcher);
    }
    function togglePanel(force) {
        ensureMounted();
        const open = force === undefined ? !panel.classList.contains('is-open') : !!force;
        panel.classList.toggle('is-open', open);
    }

    // Tweak host protocol
    window.addEventListener('message', (e) => {
        const d = e && e.data;
        if (!d || !d.type) return;
        if (d.type === '__activate_edit_mode') {
            ensureMounted();
            launcher.classList.add('is-active');
            togglePanel(true);
        } else if (d.type === '__deactivate_edit_mode') {
            if (launcher) launcher.classList.remove('is-active');
            if (panel) panel.classList.remove('is-open');
        }
    });

    // Defer the available announcement until DOM is ready (so launcher mounts cleanly)
    function announce() {
        try { window.parent.postMessage({ type: '__edit_mode_available' }, '*'); } catch (e) {}
    }
    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', announce);
    } else {
        announce();
    }

    // Expose for debugging
    window.NETweaks = { state: () => ({ ...state }), apply, open: () => togglePanel(true) };
})();
