/**
 * Stratovex — WebSocket client for real-time updates
 */

const WS = {
    socket: null,
    handlers: {},
    reconnectAttempts: 0,
    maxReconnectAttempts: 10,
    reconnectDelay: 2000,

    /** Register a handler for a message type */
    on(type, callback) {
        if (!this.handlers[type]) this.handlers[type] = [];
        this.handlers[type].push(callback);
    },

    /** Remove all handlers for a type */
    off(type) {
        delete this.handlers[type];
    },

    /** Connect to WebSocket */
    connect() {
        const token = API.getToken();
        if (!token) return;

        const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
        const url = `${protocol}//${window.location.host}/ws?token=${token}`;

        this.socket = new WebSocket(url);

        this.socket.onopen = () => {
            console.log('[WS] Connected');
            this.reconnectAttempts = 0;
            this._dispatch('connected', {});
        };

        this.socket.onmessage = (event) => {
            try {
                const msg = JSON.parse(event.data);
                this._dispatch(msg.type, msg.data);
            } catch (e) {
                console.error('[WS] Parse error:', e);
            }
        };

        this.socket.onclose = (event) => {
            console.log('[WS] Disconnected:', event.code);
            this._dispatch('disconnected', {});
            this._tryReconnect();
        };

        this.socket.onerror = (error) => {
            console.error('[WS] Error:', error);
        };
    },

    /** Disconnect */
    disconnect() {
        if (this.socket) {
            this.socket.close();
            this.socket = null;
        }
    },

    /** Dispatch message to registered handlers */
    _dispatch(type, data) {
        const fns = this.handlers[type] || [];
        for (const fn of fns) {
            try {
                fn(data);
            } catch (e) {
                console.error(`[WS] Handler error for ${type}:`, e);
            }
        }
    },

    /** Reconnect with exponential backoff */
    _tryReconnect() {
        if (this.reconnectAttempts >= this.maxReconnectAttempts) {
            console.log('[WS] Max reconnect attempts reached');
            return;
        }
        this.reconnectAttempts++;
        const delay = this.reconnectDelay * Math.pow(1.5, this.reconnectAttempts - 1);
        console.log(`[WS] Reconnecting in ${Math.round(delay / 1000)}s...`);
        setTimeout(() => this.connect(), delay);
    },
};
