/**
 * NiftyEdge — Position Sizing Calculator
 *
 *  Self-contained modal that computes "how many lots can I afford?" from a
 *  ₹-at-risk budget, the option premium, and a stop-loss percentage.
 *
 *  Surfaces:
 *    • Option chain rows (Sizing button next to Buy/Sell)
 *    • Add-Trade modal in positions.html (Quick-fill button next to Qty)
 *
 *  No backend dependency — pure arithmetic against the NIFTY options lot
 *  size (65 as of 04/06/2026, mirrors backend/config.py LOT_SIZE).
 *  Callers can override via `options.lot` when the lot size has changed.
 *
 *  Public API:
 *    Sizing.open({ tradeType, strike, premium, lot, onApply })
 *      tradeType : 'CE_SELL' | 'PE_SELL' | 'CE_BUY' | 'PE_BUY' (or undefined)
 *      strike    : number (optional, used for breakeven hint)
 *      premium   : number (optional, prefills the field)
 *      lot       : number (defaults to 65 — current NIFTY options lot)
 *      onApply   : function(lots: number)  — called when user clicks Apply
 *
 *  The "Apply" button is shown only when onApply is provided; otherwise the
 *  modal is read-only (calculator-only mode).
 */
(function () {
    'use strict';

    const DEFAULT_LOT = 65;            // NIFTY options lot — mirrors backend/config.py LOT_SIZE

    // Conservative defaults: short option positions hit a 70% stop (lose 70%
    // of collected premium); long options hit a 100% stop (premium goes to 0).
    const DEFAULT_SL_FOR = (tradeType) => {
        if (!tradeType) return 70;
        return /BUY/.test(tradeType) ? 100 : 70;
    };

    let _state = null;
    let _root  = null;

    function _ensureRoot() {
        if (_root) return _root;
        const wrap = document.createElement('div');
        wrap.id = 'sizing-modal-bg';
        wrap.className = 'sizing-modal-bg';
        wrap.addEventListener('click', (e) => { if (e.target === wrap) close(); });
        wrap.innerHTML = `
            <div class="sizing-modal" role="dialog" aria-label="Position sizing calculator">
                <div class="sizing-head">
                    <div>
                        <div class="sizing-title">Position Sizing</div>
                        <div class="sizing-sub" id="sizing-sub">Lots that keep your max loss inside the budget.</div>
                    </div>
                    <button class="sizing-close" aria-label="Close">&times;</button>
                </div>

                <div class="sizing-grid">
                    <label class="sizing-field">
                        <span class="sizing-lbl">Max ₹ at risk</span>
                        <input type="number" id="sz-risk" min="0" step="100" placeholder="e.g. 5000" inputmode="numeric">
                    </label>
                    <label class="sizing-field">
                        <span class="sizing-lbl">Premium (₹)</span>
                        <input type="number" id="sz-prem" min="0" step="0.05" placeholder="e.g. 42.50" inputmode="decimal">
                    </label>
                    <label class="sizing-field">
                        <span class="sizing-lbl">Stop-loss <span class="sizing-hint">(% of premium)</span></span>
                        <input type="number" id="sz-sl" min="1" max="100" step="5" inputmode="numeric">
                    </label>
                    <label class="sizing-field">
                        <span class="sizing-lbl">Lot size</span>
                        <input type="number" id="sz-lot" min="1" step="1" readonly>
                    </label>
                </div>

                <div class="sizing-out" id="sizing-out">
                    <div class="sizing-out-row">
                        <span class="sizing-out-lbl">Suggested lots</span>
                        <span class="sizing-out-val" id="sz-lots">—</span>
                    </div>
                    <div class="sizing-out-row">
                        <span class="sizing-out-lbl">Total premium</span>
                        <span class="sizing-out-val" id="sz-total">—</span>
                    </div>
                    <div class="sizing-out-row">
                        <span class="sizing-out-lbl">Max loss at SL</span>
                        <span class="sizing-out-val" id="sz-maxloss">—</span>
                    </div>
                    <div class="sizing-out-row" id="sz-be-row" style="display:none">
                        <span class="sizing-out-lbl">Breakeven (approx.)</span>
                        <span class="sizing-out-val" id="sz-be">—</span>
                    </div>
                </div>

                <div class="sizing-foot">
                    <button class="btn btn-sm btn-secondary" id="sz-cancel">Close</button>
                    <button class="btn btn-sm btn-primary" id="sz-apply" style="display:none">Apply</button>
                </div>
            </div>
        `;
        document.body.appendChild(wrap);
        _root = wrap;

        wrap.querySelector('.sizing-close').addEventListener('click', close);
        wrap.querySelector('#sz-cancel').addEventListener('click', close);
        wrap.querySelector('#sz-apply').addEventListener('click', _onApply);
        ['sz-risk', 'sz-prem', 'sz-sl', 'sz-lot'].forEach(id => {
            wrap.querySelector('#' + id).addEventListener('input', _recalc);
        });

        // Inject styles once.
        if (!document.getElementById('sizing-styles')) {
            const s = document.createElement('style');
            s.id = 'sizing-styles';
            s.textContent = `
                .sizing-modal-bg {
                    position: fixed; inset: 0;
                    background: rgba(0,0,0,0.55);
                    backdrop-filter: blur(4px);
                    display: none;
                    align-items: center; justify-content: center;
                    z-index: 1100;
                }
                .sizing-modal-bg.open { display: flex; }
                .sizing-modal {
                    background: var(--bg-card);
                    border: 1px solid var(--border-light);
                    border-radius: 14px;
                    box-shadow: 0 20px 60px rgba(0,0,0,0.4);
                    width: min(460px, 92vw);
                    padding: 22px 24px 18px;
                    animation: szIn 180ms cubic-bezier(0.22, 1, 0.36, 1);
                }
                @keyframes szIn {
                    from { opacity: 0; transform: translateY(8px) scale(0.97); }
                    to   { opacity: 1; transform: translateY(0)   scale(1); }
                }
                .sizing-head {
                    display: flex; align-items: flex-start; justify-content: space-between;
                    gap: 10px;
                    margin-bottom: 14px;
                }
                .sizing-title { font-size: 16px; font-weight: 700; }
                .sizing-sub { font-size: 11.5px; color: var(--text-muted); margin-top: 2px; }
                .sizing-close {
                    background: none; border: none;
                    font-size: 22px; line-height: 1;
                    color: var(--text-muted); cursor: pointer;
                    padding: 0 4px;
                }
                .sizing-close:hover { color: var(--text-primary); }
                .sizing-grid {
                    display: grid;
                    grid-template-columns: 1fr 1fr;
                    gap: 10px 12px;
                    margin-bottom: 14px;
                }
                .sizing-field { display: flex; flex-direction: column; gap: 4px; }
                .sizing-lbl {
                    font-size: 10px; text-transform: uppercase; letter-spacing: 0.5px;
                    color: var(--text-muted); font-weight: 700;
                }
                .sizing-hint { font-weight: 500; text-transform: none; letter-spacing: 0; }
                .sizing-field input {
                    background: var(--bg-secondary, rgba(255,255,255,0.03));
                    border: 1px solid var(--border-light);
                    border-radius: 7px;
                    padding: 8px 10px;
                    font: inherit;
                    font-family: var(--font-mono);
                    font-weight: 700;
                    color: var(--text-primary);
                }
                .sizing-field input:focus {
                    outline: none;
                    border-color: var(--primary, #2563EB);
                    box-shadow: 0 0 0 3px color-mix(in srgb, var(--primary, #2563EB) 18%, transparent);
                }
                .sizing-field input[readonly] { opacity: 0.7; cursor: not-allowed; }
                .sizing-out {
                    background: var(--bg-secondary, rgba(255,255,255,0.03));
                    border: 1px solid var(--border-light);
                    border-radius: 8px;
                    padding: 10px 14px;
                }
                .sizing-out-row {
                    display: flex; align-items: center; justify-content: space-between;
                    font-size: 12.5px;
                    padding: 4px 0;
                }
                .sizing-out-lbl { color: var(--text-muted); }
                .sizing-out-val { font-family: var(--font-mono); font-weight: 700; }
                #sz-lots.has-result { font-size: 18px; color: var(--primary, #2563EB); }
                #sz-lots.zero { color: var(--red); }
                .sizing-foot {
                    display: flex; justify-content: flex-end; gap: 8px;
                    margin-top: 16px;
                }
            `;
            document.head.appendChild(s);
        }
        return wrap;
    }

    function _recalc() {
        if (!_state || !_root) return;
        const get = (id) => parseFloat(_root.querySelector('#' + id).value);
        const risk    = get('sz-risk');
        const premium = get('sz-prem');
        const slPct   = get('sz-sl');
        const lot     = get('sz-lot') || _state.lot || DEFAULT_LOT;

        const lotsEl = _root.querySelector('#sz-lots');
        const totEl  = _root.querySelector('#sz-total');
        const mlEl   = _root.querySelector('#sz-maxloss');
        const beRow  = _root.querySelector('#sz-be-row');
        const beEl   = _root.querySelector('#sz-be');
        const apply  = _root.querySelector('#sz-apply');

        if (!isFinite(risk) || !isFinite(premium) || !isFinite(slPct) || risk <= 0 || premium <= 0 || slPct <= 0) {
            lotsEl.textContent = '—';
            lotsEl.className = 'sizing-out-val';
            totEl.textContent = '—';
            mlEl.textContent  = '—';
            beRow.style.display = 'none';
            apply.disabled = true;
            return;
        }

        // Per-lot risk budget at the stop-loss point.
        const slFraction   = slPct / 100;
        const perLotRisk   = premium * lot * slFraction;
        const lots         = Math.max(0, Math.floor(risk / perLotRisk));
        const totalPremium = premium * lot * lots;
        const maxLoss      = perLotRisk * lots;

        lotsEl.textContent = String(lots);
        lotsEl.className = 'sizing-out-val has-result' + (lots === 0 ? ' zero' : '');
        totEl.textContent = '₹' + Math.round(totalPremium).toLocaleString('en-IN');
        mlEl.textContent  = '−₹' + Math.round(maxLoss).toLocaleString('en-IN');
        apply.disabled = (lots === 0);

        // Approx. breakeven for short options at expiry: strike ± net premium per share.
        // (For longs, the move required to break even — also strike ± premium.)
        if (_state.strike && _state.tradeType) {
            const isCE = /CE/.test(_state.tradeType);
            const isShort = /SELL/.test(_state.tradeType);
            // Short CE: spot stays below strike+premium → profit
            // Short PE: spot stays above strike-premium → profit
            // Long  CE: spot must clear strike+premium to profit
            // Long  PE: spot must drop below strike-premium to profit
            const bePoint = isCE
                ? _state.strike + premium
                : _state.strike - premium;
            const verb = isShort ? 'Profit if spot ' : 'Profit if spot ';
            const dir  = isCE
                ? (isShort ? '< ' : '> ')
                : (isShort ? '> ' : '< ');
            beEl.textContent = verb + dir + Math.round(bePoint).toLocaleString('en-IN');
            beRow.style.display = '';
        } else {
            beRow.style.display = 'none';
        }
    }

    function _onApply() {
        if (!_state || !_state.onApply) return close();
        const lots = parseInt(_root.querySelector('#sz-lots').textContent, 10);
        if (!isFinite(lots) || lots <= 0) return;
        try { _state.onApply(lots); } catch (e) { /* host bug — don't block close */ }
        close();
    }

    function open(opts) {
        opts = opts || {};
        _ensureRoot();
        _state = {
            tradeType: opts.tradeType || null,
            strike:    opts.strike != null ? Number(opts.strike) : null,
            lot:       opts.lot || DEFAULT_LOT,
            onApply:   typeof opts.onApply === 'function' ? opts.onApply : null,
        };

        const sub = _root.querySelector('#sizing-sub');
        if (_state.tradeType) {
            const pretty = _state.tradeType.replace('_', ' ');
            sub.textContent = `${pretty}${_state.strike ? ' · ' + _state.strike : ''} — sized to keep loss inside budget.`;
        } else {
            sub.textContent = 'Lots that keep your max loss inside the budget.';
        }
        _root.querySelector('#sz-prem').value  = opts.premium != null && opts.premium > 0 ? Number(opts.premium).toFixed(2) : '';
        _root.querySelector('#sz-risk').value  = '';
        _root.querySelector('#sz-sl').value    = String(DEFAULT_SL_FOR(_state.tradeType));
        _root.querySelector('#sz-lot').value   = String(_state.lot);

        _root.querySelector('#sz-apply').style.display = _state.onApply ? '' : 'none';

        _root.classList.add('open');
        // Focus the first empty field for fast entry.
        const firstField = _root.querySelector('#sz-risk');
        if (firstField) setTimeout(() => firstField.focus(), 50);

        _recalc();
    }

    function close() {
        if (_root) _root.classList.remove('open');
        _state = null;
    }

    document.addEventListener('keydown', (e) => {
        if (e.key === 'Escape' && _root && _root.classList.contains('open')) close();
    });

    window.Sizing = { open, close };
})();
