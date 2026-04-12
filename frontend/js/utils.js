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
            { id: 'journal', icon: '☰', label: 'Journal', href: '/journal' },
            { id: 'alerts', icon: '⚡', label: 'Alerts', href: '/alerts' },
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
        'Journal':      { icon: '&#9776;',  color: '#6366F1' },
        'Alerts':       { icon: '&#9889;',  color: '#D97706' },
    },

    /** Build header HTML */
    buildHeader(title, expiries = []) {
        const user = API.getUser();
        const initial = user ? user.name.charAt(0).toUpperCase() : '?';
        const cfg = this.pageConfig[title] || { icon: '&#9679;', color: '#6B7280' };

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
                <div class="user-menu" onclick="document.getElementById('user-dropdown').classList.toggle('hidden')">
                    <div class="user-avatar">${initial}</div>
                    <span style="font-size:12px">${user ? user.name : ''}</span>
                </div>
                <div id="user-dropdown" class="hidden" style="position:absolute;top:48px;right:16px;background:var(--bg-card);border:1px solid var(--border);border-radius:8px;padding:8px;box-shadow:0 4px 12px rgba(0,0,0,0.15);z-index:60;min-width:140px">
                    <button class="btn btn-sm btn-secondary" onclick="Utils.toggleDarkMode()" style="width:100%;margin-bottom:6px;font-size:11px" id="dark-mode-btn">${document.documentElement.getAttribute('data-theme') === 'dark' ? 'Light Mode' : 'Dark Mode'}</button>
                    <button class="btn btn-sm btn-secondary" onclick="API.logout()" style="width:100%;font-size:11px">Logout</button>
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

    /** Update refresh timestamp */
    updateRefreshInfo(timestamp) {
        const el = document.getElementById('refresh-info');
        if (!el) return;
        if (!timestamp) return;
        const d = new Date(timestamp);
        el.textContent = `Last: ${d.toLocaleTimeString('en-IN', { hour: '2-digit', minute: '2-digit', second: '2-digit' })}`;
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
                    el.textContent = 'Market Closed';
                    el.style.color = '#D97706';
                }
            }
        } catch (e) {}

        // Load LIVE expiries from NSE (works regardless of market hours)
        try {
            const expResp = await API.getExpiries();
            const sel = document.getElementById('expiry-selector');
            if (sel && expResp && expResp.expiries && expResp.expiries.length) {
                sel.innerHTML = expResp.expiries.map(e =>
                    `<option value="${e}">${e}</option>`
                ).join('');
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
        const btn = document.getElementById('dark-mode-btn');
        if (btn) btn.textContent = isDark ? 'Dark Mode' : 'Light Mode';
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
};
