/**
 * Stratovex — API client & Auth utilities
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

        if (resp.status >= 500) {
            // Server failures throw a typed error so pages can show an error
            // state instead of a frozen "Loading…". 4xx responses keep the
            // existing contract of returning the parsed body (callers check
            // resp.detail / resp.error for validation messages).
            let detail = '';
            try {
                const body = await resp.json();
                detail = body && (body.detail || body.error) || '';
            } catch (e) { /* non-JSON error body */ }
            const err = new Error(detail || `Server error (${resp.status})`);
            err.status = resp.status;
            err.path = path;
            throw err;
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

    async getOptionChain(expiry, symbol) {
        let q = [];
        if (expiry) q.push(`expiry=${encodeURIComponent(expiry)}`);
        if (symbol) q.push(`symbol=${encodeURIComponent(symbol)}`);
        const qs = q.length ? '?' + q.join('&') : '';
        return this.fetch(`/api/market/option-chain${qs}`);
    },

    async strikeLookup(strike, optionType, expiry, symbol) {
        let q = `?strike=${strike}&option_type=${optionType}`;
        if (expiry) q += `&expiry=${encodeURIComponent(expiry)}`;
        if (symbol) q += `&symbol=${encodeURIComponent(symbol)}`;
        return this.fetch(`/api/market/strike-lookup${q}`);
    },

    async getExpiries(symbol) {
        const q = symbol ? `?symbol=${encodeURIComponent(symbol)}` : '';
        return this.fetch(`/api/market/expiries${q}`);
    },

    async computeForExpiry(expiry, vixOffset = 50) {
        return this.fetch(`/api/market/compute-for-expiry?expiry=${encodeURIComponent(expiry)}&vix_offset_pct=${vixOffset}`);
    },

    async computeForSymbol(symbol, expiry) {
        let q = `?symbol=${encodeURIComponent(symbol)}`;
        if (expiry) q += `&expiry=${encodeURIComponent(expiry)}`;
        return this.fetch(`/api/market/compute-for-symbol${q}`);
    },

    // ── Stock-options screener ────────────────────────────────────────
    async getStockScreener(params = {}) {
        const q = new URLSearchParams(params).toString();
        return this.fetch(`/api/stocks/screener${q ? '?' + q : ''}`);
    },

    async getFnoList() {
        return this.fetch('/api/stocks/fno-list');
    },

    async getMostActive(limit = 8) {
        return this.fetch(`/api/stocks/most-active?limit=${limit}`);
    },

    async getStockSuggestion(symbol, side, params = {}) {
        const q = new URLSearchParams({ symbol, side, ...params }).toString();
        return this.fetch(`/api/stocks/suggest?${q}`);
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

    // ── Strategy Builder endpoints ────────────────────────────────────

    async getMarketAnalytics(expiry) {
        const q = expiry ? `?expiry=${encodeURIComponent(expiry)}` : '';
        return this.fetch(`/api/strategies/market-analytics${q}`);
    },

    async getStrategyRankings(expiry) {
        const q = expiry ? `?expiry=${encodeURIComponent(expiry)}` : '';
        return this.fetch(`/api/strategies/rankings${q}`);
    },

    async createStrategy(body) {
        return this.post('/api/strategies/', body);
    },

    async getStrategies() {
        return this.fetch('/api/strategies/');
    },

    async getStrategyPayoff(id) {
        return this.fetch(`/api/strategies/${id}/payoff`);
    },

    async closeStrategy(id) {
        return this.post(`/api/strategies/${id}/close`, {});
    },

    async getIVRank(days = 252) {
        return this.fetch(`/api/market/iv-rank?days=${days}`);
    },

    async calculateMargin(body) {
        return this.post('/api/market/margin', body);
    },

    async getRollScenarios(tradeId) {
        return this.fetch(`/api/trades/${tradeId}/roll-scenarios`);
    },

    async getHedgeSuggestions(tradeId) {
        return this.fetch(`/api/trades/${tradeId}/hedge-suggestions`);
    },

    async executeRoll(tradeId, body) {
        return this.post(`/api/trades/${tradeId}/roll`, body);
    },
};
