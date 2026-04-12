/**
 * NiftyEdge — API client & Auth utilities
 */

const API = {
    BASE: '',

    /** Get stored JWT token */
    getToken() {
        return localStorage.getItem('niftyedge_token');
    },

    /** Get stored user info */
    getUser() {
        const u = localStorage.getItem('niftyedge_user');
        return u ? JSON.parse(u) : null;
    },

    /** Save auth data after login/signup */
    saveAuth(data) {
        localStorage.setItem('niftyedge_token', data.access_token);
        localStorage.setItem('niftyedge_user', JSON.stringify(data.user));
    },

    /** Clear auth and redirect to login */
    logout() {
        localStorage.removeItem('niftyedge_token');
        localStorage.removeItem('niftyedge_user');
        window.location.href = '/login';
    },

    /** Check if user is authenticated, redirect if not */
    requireAuth() {
        if (!this.getToken()) {
            window.location.href = '/login';
            return false;
        }
        return true;
    },

    /** Make an authenticated API request */
    async fetch(path, options = {}) {
        const token = this.getToken();
        const headers = {
            'Content-Type': 'application/json',
            ...(token ? { 'Authorization': `Bearer ${token}` } : {}),
            ...(options.headers || {}),
        };

        const resp = await fetch(`${this.BASE}${path}`, {
            ...options,
            cache: 'no-store',
            headers,
        });

        if (resp.status === 401) {
            this.logout();
            return null;
        }

        return resp.json();
    },

    /** POST helper */
    async post(path, body) {
        return this.fetch(path, {
            method: 'POST',
            body: JSON.stringify(body),
        });
    },

    /** PUT helper */
    async put(path, body) {
        return this.fetch(path, {
            method: 'PUT',
            body: JSON.stringify(body),
        });
    },

    // ── Auth endpoints ────────────────────────────────────────────────

    async signup(email, name, password) {
        return this.post('/api/auth/signup', { email, name, password });
    },

    async login(email, password) {
        return this.post('/api/auth/login', { email, password });
    },

    async getMe() {
        return this.fetch('/api/auth/me');
    },

    // ── Market endpoints ──────────────────────────────────────────────

    async getMarketStatus() {
        const resp = await fetch('/api/market/status');
        return resp.json();
    },

    async getSnapshot() {
        return this.fetch('/api/market/snapshot');
    },

    async getSRLevels(source) {
        const q = source ? `?source=${source}` : '';
        return this.fetch(`/api/market/sr-levels${q}`);
    },

    async getOptionChain(expiry) {
        const q = expiry ? `?expiry=${encodeURIComponent(expiry)}` : '';
        return this.fetch(`/api/market/option-chain${q}`);
    },

    async strikeLookup(strike, optionType, expiry) {
        let q = `?strike=${strike}&option_type=${optionType}`;
        if (expiry) q += `&expiry=${encodeURIComponent(expiry)}`;
        return this.fetch(`/api/market/strike-lookup${q}`);
    },

    async getExpiries() {
        return this.fetch('/api/market/expiries');
    },

    async computeForExpiry(expiry, vixOffset = 50) {
        return this.fetch(`/api/market/compute-for-expiry?expiry=${encodeURIComponent(expiry)}&vix_offset_pct=${vixOffset}`);
    },

    // ── Trade endpoints ───────────────────────────────────────────────

    async getTrades(status, expiry) {
        let q = [];
        if (status) q.push(`status=${status}`);
        if (expiry) q.push(`expiry=${encodeURIComponent(expiry)}`);
        const qs = q.length ? '?' + q.join('&') : '';
        return this.fetch(`/api/trades/${qs}`);
    },

    async addTrade(trade) {
        return this.post('/api/trades/', trade);
    },

    async closeTrade(tradeId, exitPrice, closeQuantity) {
        return this.post(`/api/trades/${tradeId}/close`, {
            exit_price: exitPrice,
            close_quantity: closeQuantity,
        });
    },

    async getRiskScores() {
        return this.fetch('/api/trades/risk');
    },
};
