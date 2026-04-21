/**
 * NiftyEdge — Shared UI utilities
 */

// Apply saved theme immediately (before render to prevent flash)
(function() {
    const theme = localStorage.getItem('niftyedge_theme');
    if (theme === 'dark') document.documentElement.setAttribute('data-theme', 'dark');
})();

const Utils = {
    /** Format number with commas */
    formatNum(n, decimals = 0) {
        if (n == null || isNaN(n)) return '-';
        return Number(n).toLocaleString('en-IN', {
            minimumFractionDigits: decimals,
            maximumFractionDigits: decimals,
        });
    },

    /** Format currency (INR) */
    formatINR(n) {
        if (n == null || isNaN(n)) return '-';
        const sign = n < 0 ? '-' : n > 0 ? '+' : '';
        return sign + '₹' + Math.abs(n).toLocaleString('en-IN', {
            minimumFractionDigits: 0,
            maximumFractionDigits: 0,
        });
    },

    /** Format P&L with color class */
    pnlHtml(value) {
        if (value == null || isNaN(value)) return '<span class="text-muted">-</span>';
        const cls = value > 0 ? 'profit' : value < 0 ? 'loss' : '';
        return `<span class="pnl-value ${cls}">${this.formatINR(value)}</span>`;
    },

    /** Format percentage change */
    formatPct(n) {
        if (n == null || isNaN(n)) return '-';
        const sign = n > 0 ? '+' : '';
        return sign + n.toFixed(1) + '%';
    },

    /** Escape HTML to prevent XSS (use on all user/RSS-sourced text) */
    escapeHtml(str) {
        return String(str)
            .replace(/&/g, '&amp;').replace(/</g, '&lt;')
            .replace(/>/g, '&gt;').replace(/"/g, '&quot;');
    },

    /** Get CSS class for change direction */
    changeClass(value) {
        if (value > 0) return 'positive';
        if (value < 0) return 'negative';
        return 'neutral';
    },

    /** Get risk color CSS */
    riskColor(score) {
        if (score == null) return '#9CA3AF';
        if (score < 35) return '#16A34A';
        if (score < 60) return '#D97706';
        if (score < 80) return '#DC2626';
        return '#991B1B';
    },

    /** Get risk CSS class */
    riskClass(label) {
        if (!label) return 'badge-neutral';
        const l = label.toLowerCase();
        if (l === 'safe' || l === 'ok') return 'risk-safe';
        if (l === 'warning') return 'risk-warning';
        return 'risk-danger';
    },

    /** Get trade type badge class */
    tradeTypeClass(type) {
        const map = {
            'CE_SELL': 'badge-ce-sell',
            'PE_SELL': 'badge-pe-sell',
            'CE_BUY': 'badge-ce-buy',
            'PE_BUY': 'badge-pe-buy',
        };
        return map[type] || 'badge-neutral';
    },

    /** Get premium ranking badge class */
    premiumRankClass(rank) {
        const map = {
            'Very Low': 'badge-very-low',
            'Sweet': 'badge-sweet',
            'Slightly Higher': 'badge-high',
            'High': 'badge-very-high',
        };
        return map[rank] || 'badge-neutral';
    },

    /** Tendency badge */
    tendencyBadge(tendency) {
        if (!tendency) return '<span class="badge badge-neutral">-</span>';
        const map = {
            'Positive': 'badge-safe',
            'Neutral': 'badge-neutral',
            'Negative': 'badge-danger',
        };
        return `<span class="badge ${map[tendency] || 'badge-neutral'}">${tendency}</span>`;
    },

    /** Show toast notification */
    toast(message, type = 'info') {
        let container = document.getElementById('toast-container');
        if (!container) {
            container = document.createElement('div');
            container.id = 'toast-container';
            container.className = 'toast-container';
            document.body.appendChild(container);
        }

        const toast = document.createElement('div');
        toast.className = `toast toast-${type}`;
        toast.textContent = message;
        container.appendChild(toast);

        setTimeout(() => {
            toast.style.opacity = '0';
            toast.style.transform = 'translateX(100%)';
            setTimeout(() => toast.remove(), 300);
        }, 3500);
    },

    /** Smooth number tween animation */
    tweenNumber(element, newValue, decimals = 0, duration = 400) {
        const current = parseFloat(element.textContent.replace(/[₹,+]/g, '')) || 0;
        if (Math.abs(current - newValue) < 0.01) return;

        const start = performance.now();
        const animate = (now) => {
            const elapsed = now - start;
            const progress = Math.min(elapsed / duration, 1);
            const eased = 1 - Math.pow(1 - progress, 3); // easeOutCubic
            const val = current + (newValue - current) * eased;
            element.textContent = this.formatNum(val, decimals);
            if (progress < 1) requestAnimationFrame(animate);
        };
        requestAnimationFrame(animate);
    },

    /** Build sidebar HTML */
    buildSidebar(activePage) {
        const pages = [
            { id: 'dashboard', icon: '◉', label: 'Dashboard', href: '/dashboard' },
            { id: 'option-chain', icon: '⊞', label: 'Option Chain', href: '/option-chain' },
            { id: 'positions', icon: '◫', label: 'Positions', href: '/positions' },
            { id: 'analytics', icon: '◔', label: 'Analytics', href: '/analytics' },
            { id: 'strategy-builder', icon: '◈', label: 'Strategy', href: '/strategy-builder' },
            { id: 'journal', icon: '☰', label: 'Journal', href: '/journal' },
            { id: 'alerts', icon: '⚡', label: 'Alerts', href: '/alerts' },
            { id: 'news-desk', icon: '📰', label: 'News Desk', href: '/news-desk' },
        ];

        return `
            <div class="sidebar-logo">N<span>iftyEdge</span></div>
            <nav class="sidebar-nav">
                ${pages.map(p => `
                    <a href="${p.href}" class="nav-item ${p.id === activePage ? 'active' : ''}">
                        <span class="icon">${p.icon}</span>
                        <span class="label">${p.label}</span>
                    </a>
                `).join('')}
            </nav>
        `;
    },

    /** Page icon/color config */
    pageConfig: {
        'Dashboard':    { icon: '&#9678;',  color: '#2563EB' },
        'Option Chain': { icon: '&#9638;',  color: '#DC2626' },
        'Positions':    { icon: '&#9635;',  color: '#16A34A' },
        'Analytics':    { icon: '&#9685;',  color: '#7C3AED' },
        'Strategy Builder': { icon: '&#9672;', color: '#7C3AED' },
        'Journal':      { icon: '&#9776;',  color: '#6366F1' },
        'Alerts':       { icon: '&#9889;',  color: '#D97706' },
        'News Desk':    { icon: '&#128240;', color: '#0891B2' },
    },

    /** Inject profile card CSS once (idempotent) */
    _injectProfileCardStyles() {
        if (document.getElementById('ne-profile-style')) return;
        const s = document.createElement('style');
        s.id = 'ne-profile-style';
        s.textContent = `
            .ne-profile-card {
                position: absolute;
                top: 54px;
                right: 16px;
                width: 248px;
                background: var(--bg-card, #1E293B);
                border: 1px solid rgba(59,130,246,0.28);
                border-radius: 14px;
                overflow: hidden;
                box-shadow: 0 12px 40px rgba(0,0,0,0.38), 0 0 0 1px rgba(59,130,246,0.08);
                z-index: 200;
            }
            .ne-profile-card.hidden { display: none; }
            .ne-profile-card-header {
                display: flex;
                align-items: center;
                gap: 12px;
                padding: 16px;
                background: linear-gradient(135deg, rgba(37,99,235,0.14) 0%, rgba(124,58,237,0.09) 100%);
            }
            .ne-profile-avatar {
                width: 46px; height: 46px;
                border-radius: 50%;
                background: linear-gradient(135deg, #2563EB 0%, #7C3AED 100%);
                display: flex; align-items: center; justify-content: center;
                font-size: 19px; font-weight: 700; color: #fff; flex-shrink: 0;
                box-shadow: 0 3px 10px rgba(37,99,235,0.40);
            }
            .ne-profile-meta { min-width: 0; }
            .ne-profile-name {
                font-size: 13px; font-weight: 700;
                color: var(--text-primary, #F1F5F9);
                white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
            }
            .ne-profile-email {
                font-size: 11px; color: var(--text-muted, #64748B);
                margin-top: 2px;
                white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
            }
            .ne-profile-divider { height: 1px; background: var(--border, rgba(148,163,184,0.15)); }
            .ne-profile-row {
                display: flex; align-items: center; justify-content: space-between;
                padding: 11px 16px; cursor: pointer; transition: background 0.15s;
            }
            .ne-profile-row:hover { background: rgba(59,130,246,0.07); }
            .ne-profile-row-label { font-size: 12px; color: var(--text-secondary, #94A3B8); }
            .ne-theme-toggle {
                width: 36px; height: 20px; border-radius: 10px;
                background: rgba(148,163,184,0.18);
                position: relative; transition: background 0.2s;
                border: 1px solid rgba(148,163,184,0.22);
                flex-shrink: 0;
            }
            .ne-theme-toggle.on { background: #2563EB; border-color: #2563EB; }
            .ne-theme-thumb {
                position: absolute; top: 2px; left: 2px;
                width: 14px; height: 14px; border-radius: 50%;
                background: #fff; transition: transform 0.2s;
                box-shadow: 0 1px 3px rgba(0,0,0,0.2);
            }
            .ne-theme-toggle.on .ne-theme-thumb { transform: translateX(16px); }
            .ne-profile-logout {
                width: 100%; padding: 11px 16px;
                text-align: left; background: none; border: none;
                font-size: 12px; font-weight: 600; color: #EF4444;
                cursor: pointer; font-family: inherit;
                display: flex; align-items: center; gap: 8px;
                transition: background 0.15s;
            }
            .ne-profile-logout:hover { background: rgba(239,68,68,0.07); }
        `;
        document.head.appendChild(s);
    },

    /** Toggle profile card open/closed */
    toggleProfileCard() {
        const card = document.getElementById('ne-profile-card');
        if (!card) return;
        const opening = card.classList.contains('hidden');
        card.classList.toggle('hidden', !opening);
        if (!opening) return;
        // Sync theme toggle thumb
        const toggle = document.getElementById('ne-theme-toggle');
        if (toggle) toggle.classList.toggle('on', document.documentElement.getAttribute('data-theme') === 'dark');
        // Close on outside click or Escape
        setTimeout(() => {
            const away = (e) => {
                const trigger = document.getElementById('ne-profile-trigger');
                if (!card.contains(e.target) && (!trigger || !trigger.contains(e.target))) {
                    card.classList.add('hidden');
                    document.removeEventListener('click', away);
                }
            };
            document.addEventListener('click', away);
            document.addEventListener('keydown', (e) => {
                if (e.key === 'Escape') card.classList.add('hidden');
            }, { once: true });
        }, 0);
    },

    /** Build header HTML */
    buildHeader(title, expiries = []) {
        const user = API.getUser();
        const initial = user ? user.name.charAt(0).toUpperCase() : '?';
        const cfg = this.pageConfig[title] || { icon: '&#9679;', color: '#6B7280' };

        this._injectProfileCardStyles();

        return `
            <div class="header-left">
                <div class="header-icon" style="background:${cfg.color}">${cfg.icon}</div>
                <span class="header-title">${title}</span>
            </div>
            <div class="header-right">
                <select id="expiry-selector" class="expiry-selector">
                    ${expiries.length ? expiries.map(e =>
                        `<option value="${e}">${e}</option>`
                    ).join('') : '<option value="">Loading...</option>'}
                </select>
                <span class="refresh-info" id="refresh-info">Connecting...</span>
                <a href="/alerts" class="notification-bell" id="notification-bell" title="Alerts" style="position:relative;text-decoration:none;font-size:18px;color:#6B7280;margin:0 8px;cursor:pointer">
                    &#128276;
                    <span id="alert-badge" style="display:none;position:absolute;top:-4px;right:-6px;background:#DC2626;color:#fff;font-size:9px;font-weight:700;border-radius:50%;min-width:16px;height:16px;line-height:16px;text-align:center;padding:0 3px"></span>
                </a>
                <div class="user-menu" id="ne-profile-trigger" onclick="Utils.toggleProfileCard()">
                    <div class="user-avatar">${initial}</div>
                    <span style="font-size:12px">${user ? user.name : ''}</span>
                </div>
                <div id="ne-profile-card" class="ne-profile-card hidden">
                    <div class="ne-profile-card-header">
                        <div class="ne-profile-avatar">${initial}</div>
                        <div class="ne-profile-meta">
                            <div class="ne-profile-name">${user ? user.name : ''}</div>
                            <div class="ne-profile-email">${user ? (user.email || '') : ''}</div>
                        </div>
                    </div>
                    <div class="ne-profile-divider"></div>
                    <div class="ne-profile-row" onclick="Utils.toggleDarkMode()">
                        <span class="ne-profile-row-label">Dark Mode</span>
                        <div class="ne-theme-toggle" id="ne-theme-toggle">
                            <div class="ne-theme-thumb"></div>
                        </div>
                    </div>
                    <div class="ne-profile-divider"></div>
                    <button class="ne-profile-logout" onclick="API.logout()">
                        <span style="font-size:14px">&#x2192;</span> Sign Out
                    </button>
                </div>
            </div>
        `;
    },

    /** Market closed banner HTML */
    marketClosedBanner(reason, nextOpen) {
        return `
            <div id="market-closed-banner" style="background:var(--amber-bg);border:1px solid var(--amber);border-radius:8px;padding:12px 20px;margin-bottom:16px;display:flex;align-items:center;justify-content:space-between">
                <div>
                    <strong style="color:var(--amber)">Market Closed</strong>
                    <span style="color:var(--text-secondary);margin-left:8px">${reason || ''}</span>
                </div>
                <span style="color:var(--amber);font-size:12px">Next open: ${nextOpen || '-'}</span>
            </div>
        `;
    },

    /** Update refresh timestamp.
     * Backend stores snapshot timestamps as naive UTC (datetime.utcnow()), which
     * serialise to an ISO string without a timezone marker. A naive ISO string
     * is parsed by JS as *local* time, so IST users saw a 5h30m shift (e.g.
     * 06:55 AM instead of 12:25 PM). We detect the missing tz and pin it to UTC.
     */
    updateRefreshInfo(timestamp) {
        const el = document.getElementById('refresh-info');
        if (!el || !timestamp) return;
        let ts = timestamp;
        if (typeof ts === 'string' && !/Z|[+-]\d{2}:?\d{2}$/.test(ts)) ts = ts + 'Z';
        const d = new Date(ts);
        if (isNaN(d.getTime())) return;
        el.textContent = `Last: ${d.toLocaleTimeString('en-IN', {
            hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: true,
            timeZone: 'Asia/Kolkata',
        })} IST`;
    },

    /** localStorage key for persisted expiry selection */
    EXPIRY_KEY: 'niftyedge_selected_expiry',

    /**
     * Parse an NSE expiry string "DD-Mon-YYYY" into a plain Date (year/month/day).
     * Returns null if the string is malformed.
     */
    parseExpiryDate(expiry) {
        const MONTHS = { Jan:0, Feb:1, Mar:2, Apr:3, May:4, Jun:5, Jul:6, Aug:7, Sep:8, Oct:9, Nov:10, Dec:11 };
        const parts = (expiry || '').split('-');
        if (parts.length !== 3) return null;
        const d = parseInt(parts[0], 10);
        const m = MONTHS[parts[1]];
        const y = parseInt(parts[2], 10);
        if (isNaN(d) || m == null || isNaN(y)) return null;
        return new Date(y, m, d);
    },

    /**
     * Return today's calendar date in IST (UTC+5:30), with time stripped.
     * NSE expiry dates are always IST calendar dates, so comparisons must use IST.
     */
    todayIST() {
        const now = new Date();
        const ist = new Date(now.getTime() + 5.5 * 60 * 60 * 1000);
        return new Date(ist.getUTCFullYear(), ist.getUTCMonth(), ist.getUTCDate());
    },

    /**
     * Returns true if the given expiry string is today or in the future (IST).
     * Today's expiry is still valid — it only expires at EOD IST.
     */
    isExpiryCurrent(expiry) {
        const d = this.parseExpiryDate(expiry);
        if (!d) return false;
        return d >= this.todayIST();
    },

    /**
     * Return only future/current expiries from the supplied list (IST comparison).
     * If the filter would leave nothing (e.g. bad data), the original list is
     * returned as a safe fallback so the UI never goes blank.
     */
    filterFutureExpiries(expiries) {
        if (!expiries || !expiries.length) return expiries || [];
        const future = expiries.filter(e => this.isExpiryCurrent(e));
        return future.length ? future : expiries;
    },

    /**
     * Nearest *tradeable* expiry — strictly after today (DTE > 0). On expiry
     * day, today's expiry has DTE=0 which makes range math collapse, so the
     * default rolls to next week. Falls back to the first item if all expiries
     * are today or past (degenerate data).
     */
    nearestFutureExpiry(expiries) {
        if (!expiries || !expiries.length) return '';
        const today = this.todayIST();
        for (const e of expiries) {
            const d = this.parseExpiryDate(e);
            if (d && d > today) return e;
        }
        return expiries[0];
    },

    /**
     * Save the user's expiry selection to localStorage.
     * @param {string} expiry  e.g. "17-Apr-2026"
     */
    saveExpiry(expiry) {
        if (expiry) localStorage.setItem(this.EXPIRY_KEY, expiry);
    },

    /**
     * Given a list of available expiries (ordered nearest-first), return the
     * expiry that should be pre-selected in the dropdown:
     *   1. Filter the list to future/current expiries only (IST).
     *   2. If localStorage has a saved expiry AND it's in the future list → use it.
     *   3. Otherwise → nearest upcoming expiry (future[0]).
     * @param {string[]} expiries  Raw list from the backend (may include past expiries)
     * @returns {string}
     */
    resolveExpiry(expiries) {
        if (!expiries || !expiries.length) return '';
        const future = this.filterFutureExpiries(expiries);
        const saved = localStorage.getItem(this.EXPIRY_KEY);
        // Only restore saved expiry if it's still in the future
        if (saved && future.includes(saved)) return saved;
        return this.nearestFutureExpiry(future);
    },

    /**
     * Initialize common header state — loads market status, populates expiry
     * selector, and sets refresh-info text. Call on every page after buildHeader().
     */
    async initHeader() {
        try {
            const status = await API.getMarketStatus();
            const el = document.getElementById('refresh-info');
            if (el) {
                if (status.open) {
                    el.textContent = 'Market Open';
                    el.style.color = '#16A34A';
                } else {
                    el.textContent = status.next_open
                        ? `Market Closed · Next: ${status.next_open}`
                        : 'Market Closed';
                    el.style.color = '#D97706';
                }
            }
        } catch (e) {}

        // Load LIVE expiries from NSE (works regardless of market hours)
        try {
            const expResp = await API.getExpiries();
            const sel = document.getElementById('expiry-selector');
            if (sel && expResp && expResp.expiries && expResp.expiries.length) {
                // Strip any past expiries before populating the dropdown
                const futureExpiries = this.filterFutureExpiries(expResp.expiries);
                sel.innerHTML = futureExpiries.map(e =>
                    `<option value="${e}">${e}</option>`
                ).join('');
                // Restore user's last selection, but never a past expiry
                sel.value = this.resolveExpiry(futureExpiries);
                // Persist future changes from these pages
                sel.addEventListener('change', () => this.saveExpiry(sel.value));
            }
        } catch (e) {}

        // Use snapshot only for refresh-info timestamp
        try {
            const snap = await API.getSnapshot();
            if (snap && snap.data && snap.data.timestamp) {
                this.updateRefreshInfo(snap.data.timestamp);
            }
        } catch (e) {}

        // Load unread alert count for bell badge
        try {
            const alertResp = await API.fetch('/api/alerts/unread-count');
            if (alertResp && alertResp.count > 0) {
                this.updateAlertBadge(alertResp.count);
            }
        } catch (e) {}

        // Wire a global alert-triggered listener so any page pops the alert
        // prominently and flashes the bell. Safe to call multiple times —
        // WS.on pushes handlers; we guard with a single-instance flag.
        this._wireGlobalAlertListener();

        // Show first-time tour (no-op if already completed)
        this.initTour();
    },

    /** Register a global WS listener that pops a rich toast + pulses the bell
     *  whenever an alert fires. Idempotent — only binds once per page. */
    _wireGlobalAlertListener() {
        if (typeof WS === 'undefined' || this._alertListenerBound) return;
        this._alertListenerBound = true;
        this._injectAlertPopupStyles();

        // If no page has opened a socket yet, open one so we still receive events.
        if (!WS.socket || WS.socket.readyState > 1) {
            try { WS.connect(); } catch (e) {}
        }

        WS.on('alert_triggered', (data) => {
            this.showAlertPopup(data || {});
            this.flashBell();
            // Bump the badge locally; authoritative refresh happens next poll.
            const badge = document.getElementById('alert-badge');
            const prev = parseInt((badge && badge.textContent) || '0', 10) || 0;
            this.updateAlertBadge(prev + 1);
        });
    },

    _injectAlertPopupStyles() {
        if (document.getElementById('ne-alert-popup-style')) return;
        const s = document.createElement('style');
        s.id = 'ne-alert-popup-style';
        s.textContent = `
            @keyframes ne-bell-shake {
                0%,100% { transform: rotate(0); }
                15% { transform: rotate(-18deg); }
                30% { transform: rotate(14deg); }
                45% { transform: rotate(-10deg); }
                60% { transform: rotate(6deg); }
                75% { transform: rotate(-3deg); }
            }
            .ne-bell-shake { animation: ne-bell-shake 0.9s ease-in-out; color: #DC2626 !important; }
            .ne-alert-popup {
                position: fixed; right: 20px; top: 72px; z-index: 9500;
                background: var(--bg-card); border: 1px solid #DC2626;
                border-left: 4px solid #DC2626;
                border-radius: 8px; padding: 12px 14px 12px 14px;
                box-shadow: 0 12px 28px rgba(0,0,0,0.22);
                max-width: 360px; font-size: 12px; line-height: 1.45;
                animation: ne-alert-in 0.25s ease-out;
                cursor: pointer;
            }
            @keyframes ne-alert-in {
                from { transform: translateY(-8px); opacity: 0; }
                to   { transform: translateY(0);    opacity: 1; }
            }
            .ne-alert-popup .na-title {
                font-weight: 700; color: #DC2626; font-size: 12.5px;
                letter-spacing: 0.3px; margin-bottom: 4px;
                display: flex; align-items: center; gap: 6px;
            }
            .ne-alert-popup .na-msg { color: var(--text-primary); margin-bottom: 6px; }
            .ne-alert-popup .na-meta {
                color: var(--text-muted); font-size: 10.5px;
                font-family: var(--font-mono);
            }
            .ne-alert-popup .na-close {
                position: absolute; top: 6px; right: 8px;
                color: var(--text-muted); font-size: 14px; cursor: pointer;
                border: none; background: none; padding: 0 4px;
            }
        `;
        document.head.appendChild(s);
    },

    /** Show a prominent, clickable alert pop-up anchored to the bell. */
    showAlertPopup(data) {
        this._injectAlertPopupStyles();
        // Stack successive popups vertically
        const existing = document.querySelectorAll('.ne-alert-popup');
        const offset = 72 + existing.length * 12;

        const metric = (data.metric || '').toUpperCase().replace(/_/g, ' ');
        const cond   = data.condition || '';
        const thr    = data.threshold != null ? data.threshold : '';
        const cur    = data.current_value != null ? data.current_value : '';
        const msg    = data.message || 'Alert condition met.';

        const popup = document.createElement('div');
        popup.className = 'ne-alert-popup';
        popup.style.top = offset + 'px';
        popup.innerHTML = `
            <button class="na-close" aria-label="Dismiss">&times;</button>
            <div class="na-title">&#128276; Alert Triggered${metric ? ' — ' + metric : ''}</div>
            <div class="na-msg">${msg}</div>
            <div class="na-meta">${cond && thr !== '' ? `${cond} ${thr}` : ''}${cur !== '' ? `  ·  now ${cur}` : ''}</div>
        `;
        popup.querySelector('.na-close').addEventListener('click', (ev) => {
            ev.stopPropagation();
            popup.remove();
        });
        popup.addEventListener('click', () => { window.location.href = '/alerts'; });
        document.body.appendChild(popup);

        setTimeout(() => { if (popup.isConnected) popup.remove(); }, 5000);
    },

    /** Briefly shake + tint the bell icon to grab attention. */
    flashBell() {
        const bell = document.getElementById('notification-bell');
        if (!bell) return;
        bell.classList.remove('ne-bell-shake');
        // Force reflow so the animation restarts if fired repeatedly
        void bell.offsetWidth;
        bell.classList.add('ne-bell-shake');
        setTimeout(() => bell.classList.remove('ne-bell-shake'), 1000);
    },

    /** Toggle dark mode */
    toggleDarkMode() {
        const html = document.documentElement;
        const isDark = html.getAttribute('data-theme') === 'dark';
        if (isDark) {
            html.removeAttribute('data-theme');
            localStorage.setItem('niftyedge_theme', 'light');
        } else {
            html.setAttribute('data-theme', 'dark');
            localStorage.setItem('niftyedge_theme', 'dark');
        }
        const toggle = document.getElementById('ne-theme-toggle');
        if (toggle) toggle.classList.toggle('on', !isDark);
        window.dispatchEvent(new CustomEvent('themechange', {
            detail: { theme: isDark ? 'light' : 'dark' }
        }));
    },

    /** Update the alert bell badge count */
    updateAlertBadge(count) {
        const badge = document.getElementById('alert-badge');
        if (!badge) return;
        if (count > 0) {
            badge.textContent = count > 9 ? '9+' : count;
            badge.style.display = 'inline-block';
        } else {
            badge.style.display = 'none';
        }
    },

    /* ── First-time onboarding tour ───────────────────────────────────────
       Shows once per account (keyed by username) and never again.
       Each step anchors to a real DOM element via querySelector.
       Call after buildHeader() + initHeader() so all elements exist.    */
    TOUR_KEY: 'niftyedge_tour_v1',

    isTourDone() {
        try {
            const user = API.getUser();
            const key  = this.TOUR_KEY + (user ? '_' + user.email : '');
            return !!localStorage.getItem(key);
        } catch (e) { return true; }
    },

    markTourDone() {
        const user = API.getUser();
        const key  = this.TOUR_KEY + (user ? '_' + user.email : '');
        localStorage.setItem(key, '1');
    },

    /**
     * Inject tour CSS once, idempotent.
     */
    _injectTourStyles() {
        if (document.getElementById('ne-tour-style')) return;
        const s = document.createElement('style');
        s.id = 'ne-tour-style';
        s.textContent = `
            .ne-tour-backdrop {
                position: fixed; inset: 0; z-index: 8000;
                pointer-events: none;
            }
            .ne-tour-tip {
                position: fixed; z-index: 8010;
                background: #0F172A;
                border: 1px solid rgba(59,130,246,0.55);
                border-radius: 10px;
                padding: 14px 16px 12px;
                width: 230px;
                box-shadow: 0 8px 32px rgba(0,0,0,0.45), 0 0 0 1px rgba(59,130,246,0.15);
                font-family: var(--font-sans, sans-serif);
                pointer-events: all;
            }
            .ne-tour-tip::before {
                content: '';
                position: absolute;
                width: 10px; height: 10px;
                background: #0F172A;
                border: 1px solid rgba(59,130,246,0.55);
                transform: rotate(45deg);
            }
            .ne-tour-tip[data-arrow="top"]::before    { top:-6px; left:20px; border-bottom:none; border-right:none; }
            .ne-tour-tip[data-arrow="bottom"]::before { bottom:-6px; left:20px; border-top:none; border-left:none; }
            .ne-tour-tip[data-arrow="right"]::before  { right:-6px; top:14px; border-left:none; border-bottom:none; }
            .ne-tour-tip[data-arrow="left"]::before   { left:-6px; top:14px; border-right:none; border-top:none; }
            .ne-tour-label {
                font-size: 9px; font-weight: 700; letter-spacing: 0.8px;
                text-transform: uppercase; color: #3B82F6; margin-bottom: 5px;
            }
            .ne-tour-title {
                font-size: 13px; font-weight: 700; color: #F1F5F9; margin-bottom: 4px;
            }
            .ne-tour-body {
                font-size: 11.5px; color: #94A3B8; line-height: 1.55; margin-bottom: 12px;
            }
            .ne-tour-footer {
                display: flex; justify-content: space-between; align-items: center;
            }
            .ne-tour-dots { display:flex; gap:4px; }
            .ne-tour-dot {
                width:6px; height:6px; border-radius:50%;
                background: #1E3A5F;
                transition: background 0.2s;
            }
            .ne-tour-dot.active { background: #3B82F6; }
            .ne-tour-skip {
                font-size: 11px; color: #475569; background: none; border: none;
                cursor: pointer; font-family: inherit; padding: 0;
            }
            .ne-tour-skip:hover { color: #94A3B8; }
            .ne-tour-next {
                font-size: 11.5px; font-weight: 600; color: #fff;
                background: #2563EB; border: none; border-radius: 6px;
                padding: 5px 14px; cursor: pointer; font-family: inherit;
                transition: background 0.15s;
            }
            .ne-tour-next:hover { background: #1D4ED8; }
            .ne-tour-ring {
                position: fixed; z-index: 8005; pointer-events: none;
                border: 2px solid rgba(59,130,246,0.7);
                border-radius: 8px;
                box-shadow: 0 0 0 4px rgba(59,130,246,0.15);
                transition: all 0.25s ease;
            }
        `;
        document.head.appendChild(s);
    },

    /**
     * Define tour steps.
     * Each step: { selector, title, body, arrow, offsetX, offsetY }
     * selector: CSS selector for the highlighted element (null = center screen)
     * arrow:    which side of the tooltip the pointer arrow appears on
     * place:    where to position tooltip relative to element ('below','above','right','left')
     */
    _tourSteps() {
        return [
            {
                selector: '#expiry-selector',
                title:    'Select Expiry',
                body:     'Choose which expiry to analyse. All analytics, charts, and the option chain update to match your selection.',
                arrow:    'top',
                place:    'below',
            },
            {
                selector: '.sidebar-nav',
                title:    'Navigate Modules',
                body:     'Hover the sidebar to expand labels. Each module — Chain, Positions, Strategy, Journal — serves a distinct purpose.',
                arrow:    'left',
                place:    'right',
            },
            {
                selector: '.metrics-strip',
                title:    'Live Market Analytics',
                body:     'Seven real-time NIFTY metrics: spot, VIX, PCR, max pain, and more. These drive all strategy recommendations.',
                arrow:    'top',
                place:    'below',
            },
            {
                selector: '#notification-bell',
                title:    'Price & IV Alerts',
                body:     'Set triggers on price or implied volatility. The bell lights up when any alert fires.',
                arrow:    'top',
                place:    'below',
            },
        ];
    },

    /**
     * Launch the tour. Safe to call unconditionally — returns immediately if done.
     */
    initTour() {
        if (this.isTourDone()) return;
        this.markTourDone(); // mark immediately — navigating away won't replay it
        this._injectTourStyles();

        const steps    = this._tourSteps();
        let   current  = 0;

        // Backdrop (non-blocking)
        const backdrop = document.createElement('div');
        backdrop.className = 'ne-tour-backdrop';
        document.body.appendChild(backdrop);

        // Ring highlighter
        const ring = document.createElement('div');
        ring.className = 'ne-tour-ring';
        document.body.appendChild(ring);

        // Tooltip
        const tip = document.createElement('div');
        tip.className = 'ne-tour-tip';
        document.body.appendChild(tip);

        const finish = () => {
            this.markTourDone();
            [backdrop, ring, tip].forEach(el => el.remove());
        };

        const show = (idx) => {
            const step = steps[idx];
            const total = steps.length;

            // Build tooltip HTML
            const dots = Array.from({ length: total }, (_, i) =>
                `<span class="ne-tour-dot ${i === idx ? 'active' : ''}"></span>`
            ).join('');

            const isLast = idx === total - 1;
            tip.setAttribute('data-arrow', step.arrow || 'top');
            tip.innerHTML = `
                <div class="ne-tour-label">Quick tour · ${idx + 1} of ${total}</div>
                <div class="ne-tour-title">${step.title}</div>
                <div class="ne-tour-body">${step.body}</div>
                <div class="ne-tour-footer">
                    <div class="ne-tour-dots">${dots}</div>
                    <div style="display:flex;align-items:center;gap:10px">
                        <button class="ne-tour-skip">Skip all</button>
                        <button class="ne-tour-next">${isLast ? 'Done ✓' : 'Next →'}</button>
                    </div>
                </div>
            `;

            tip.querySelector('.ne-tour-skip').onclick = finish;
            tip.querySelector('.ne-tour-next').onclick = () => {
                if (isLast) { finish(); } else { show(idx + 1); }
            };

            // Position ring + tooltip relative to target element
            const el = step.selector ? document.querySelector(step.selector) : null;
            if (el) {
                const r = el.getBoundingClientRect();
                const PAD = 6;
                ring.style.cssText = `
                    position:fixed; z-index:8005; pointer-events:none;
                    border:2px solid rgba(59,130,246,0.7);
                    border-radius:8px;
                    box-shadow:0 0 0 4px rgba(59,130,246,0.15);
                    top:${r.top - PAD}px; left:${r.left - PAD}px;
                    width:${r.width + PAD * 2}px; height:${r.height + PAD * 2}px;
                `;

                // Place tooltip
                const tipW = 230, tipH = 140;
                const vw = window.innerWidth, vh = window.innerHeight;
                let tx, ty;

                if (step.place === 'below') {
                    tx = Math.min(r.left, vw - tipW - 12);
                    ty = r.bottom + 14;
                    if (ty + tipH > vh) { ty = r.top - tipH - 14; tip.setAttribute('data-arrow','bottom'); }
                } else if (step.place === 'above') {
                    tx = Math.min(r.left, vw - tipW - 12);
                    ty = r.top - tipH - 14;
                    tip.setAttribute('data-arrow','bottom');
                } else if (step.place === 'right') {
                    tx = r.right + 14;
                    ty = r.top;
                    if (tx + tipW > vw) { tx = r.left - tipW - 14; tip.setAttribute('data-arrow','right'); }
                } else {
                    tx = r.left - tipW - 14;
                    ty = r.top;
                    tip.setAttribute('data-arrow','right');
                }

                tip.style.cssText = `top:${ty}px; left:${tx}px; width:${tipW}px;`;
            } else {
                // Center fallback
                ring.style.cssText = 'display:none';
                tip.style.cssText  = 'top:50%;left:50%;transform:translate(-50%,-50%);width:260px;';
            }
        };

        // Slight delay so page fully renders first
        setTimeout(() => show(0), 600);
    },
};
