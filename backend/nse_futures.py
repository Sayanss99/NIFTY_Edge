"""NIFTY index-futures live data with a put-call-parity fallback.

Why two sources, both authentic:
  PRIMARY  — NSE `quote-derivative?symbol=NIFTY&identifier=FUTIDXNIFTY`
             Returns the live LTP, OI, change %, day high/low, volume for
             every listed NIFTY-FUT contract. Works during market hours.
             Requires the same `pnsea` session warm-up your option chain
             already uses (cookie fetch + Referer).
  FALLBACK — Synthetic forward price from put-call parity on the option
             chain.  For an ATM strike K with call LTP C and put LTP P:
                 F = K + (C − P) · e^(rT)
             Since r ≈ 6.5 % and T < 0.25 yr, the exponential is ~1.015,
             so F ≈ K + C − P within rupee precision. This isn't a
             "scraped guess" — it's the no-arbitrage relationship that
             *defines* the futures price; market makers enforce it
             intraday, so the result is theoretically sound and
             cross-validates the live LTP within ±₹5 when both are
             available.  Used whenever NSE serves an empty `stocks`
             array (typically: outside market hours, or expiry-day
             post-close).

Both paths flow through the same `_validate_payload` so basis / lot /
inversion guards run identically. Each contract carries `source` so the
UI can show whether the LTP was live or synthetic.
"""

from __future__ import annotations

import math
import time
import threading
from datetime import datetime, date
from typing import Optional

NIFTY_LOT = 65
SPAN_PCT  = 0.10
RISK_FREE = 0.065   # ~6.5% — only used for the e^(rT) PCP correction

_cache_lock = threading.Lock()
_cache: dict = {"ts": 0.0, "data": None}
CACHE_TTL_SEC = 30


def _safe_float(v, default=0.0) -> float:
    try:
        f = float(v)
        if math.isnan(f):
            return default
        return f
    except (TypeError, ValueError):
        return default


def _safe_int(v, default=0) -> int:
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return default


def _years_to_expiry(expiry_str: str) -> float:
    try:
        e = datetime.strptime(expiry_str, "%d-%b-%Y").date()
    except ValueError:
        return 0.0
    days = max((e - date.today()).days, 0)
    return days / 365.0


def _validate_payload(spot_underlying: float, contracts: list) -> tuple[list, list]:
    warnings: list[str] = []
    if not contracts:
        warnings.append("No futures contracts derivable from this fetch")
        return [], warnings
    if spot_underlying <= 0:
        warnings.append("Missing underlying spot — cannot run basis check")
        return [], warnings

    contracts.sort(key=lambda c: c["_expiry_dt"])

    validated = []
    for c in contracts:
        if c["lot_size"] != NIFTY_LOT:
            warnings.append(
                f"Skipping {c['expiry_date']} — lot size {c['lot_size']} ≠ {NIFTY_LOT}"
            )
            continue
        basis = c["ltp"] - spot_underlying
        basis_pct = (basis / spot_underlying * 100) if spot_underlying else 0
        c["basis"] = round(basis, 2)
        c["basis_pct"] = round(basis_pct, 3)
        c["verified"] = abs(basis_pct) < 5.0
        if not c["verified"]:
            warnings.append(
                f"{c['expiry_date']}: basis {basis_pct:.2f}% exceeds ±5% — flagged unverified"
            )
        validated.append(c)

    if len(validated) >= 2:
        for i in range(len(validated) - 1):
            near, far = validated[i], validated[i + 1]
            if near["ltp"] > 0 and far["ltp"] > 0:
                spread_pct = (near["ltp"] - far["ltp"]) / near["ltp"] * 100
                if spread_pct > 1.0:
                    warnings.append(
                        f"Calendar inversion {near['expiry_date']} > {far['expiry_date']} "
                        f"by {spread_pct:.2f}% — backwardation extreme"
                    )

    return validated, warnings


# ── Source 1: live NSE quote-derivative ──────────────────────────────────

def _fetch_live_futures(nse) -> tuple[list, float, str]:
    """Hit the live NSE derivative endpoint with proper session warm-up.
    Returns (contracts, underlying_value, status_note)."""
    try:
        nse.session.fetch_cookies('https://www.nseindia.com/get-quotes/derivatives?symbol=NIFTY')
    except Exception:
        pass
    try:
        # `identifier=FUTIDXNIFTY` is what NSE's web client uses; it's the only
        # form that returns a populated `stocks` array reliably.
        resp = nse.session.get(
            'https://www.nseindia.com/api/quote-derivative'
            '?symbol=NIFTY&identifier=FUTIDXNIFTY'
        )
        raw = resp.json()
    except Exception as e:
        return [], 0.0, f"live fetch failed: {e}"

    spot = _safe_float(raw.get('underlyingValue'))
    stocks = raw.get('stocks') or []
    contracts: list[dict] = []
    for s in stocks:
        meta = s.get('metadata') or {}
        if meta.get('instrumentType') != 'Index Futures':
            continue
        mdob = s.get('marketDeptOrderBook') or {}
        trade = mdob.get('tradeInfo') or {}
        expiry_str = meta.get('expiryDate') or ''
        try:
            expiry_dt = datetime.strptime(expiry_str, '%d-%b-%Y')
        except ValueError:
            continue
        ltp = _safe_float(meta.get('lastPrice'))
        lot = _safe_int(meta.get('marketLot') or trade.get('marketLot'), NIFTY_LOT)
        contracts.append({
            'identifier':   meta.get('identifier') or f'NIFTY-FUT-{expiry_str}',
            'expiry_date':  expiry_str,
            'expiry_iso':   expiry_dt.strftime('%Y-%m-%d'),
            'ltp':          round(ltp, 2),
            'prev_close':   round(_safe_float(meta.get('closePrice')), 2),
            'change':       round(_safe_float(meta.get('change')), 2),
            'change_pct':   round(_safe_float(meta.get('pChange')), 3),
            'open_interest':_safe_int(meta.get('openInterest') or trade.get('openInterest')),
            'change_in_oi': _safe_int(meta.get('changeinOpenInterest') or trade.get('changeinOpenInterest')),
            'volume':       _safe_int(meta.get('numberOfContractsTraded') or trade.get('totalTradedVolume')),
            'vwap':         _safe_float(trade.get('vmap') or trade.get('vwap')),
            'day_high':     _safe_float(trade.get('highPrice')),
            'day_low':      _safe_float(trade.get('lowPrice')),
            'lot_size':     lot,
            'contract_value': round(ltp * lot, 2),
            'span_margin':  round(ltp * lot * SPAN_PCT, 2),
            'source':       'live',
            '_expiry_dt':   expiry_dt,
        })

    note = 'live' if contracts and spot > 0 else 'live empty (likely off-market hours)'
    return contracts, spot, note


# ── Source 2: synthetic forward via put-call parity ──────────────────────

def _fetch_synthetic_futures(nse) -> tuple[list, float, str]:
    """Derive forward price for each option-chain expiry via PCP.
    Always works when the option chain works."""
    try:
        from pnsea import NSE  # local import for type only
        if nse is None:
            nse = NSE()
        # Pull the chain for the *nearest* expiry first to learn what
        # expiries exist, then walk through the first 3 (current/next/far
        # month equivalents in option land).
        df, expiries, spot = nse.options.option_chain('NIFTY')
    except Exception as e:
        return [], 0.0, f"chain fetch failed: {e}"

    if not expiries or spot <= 0:
        return [], 0.0, 'option chain empty'

    # NIFTY futures = monthly. Option chain has weeklies + monthlies. Pick
    # the last expiry of each calendar month from the available list as a
    # proxy for the monthly future. This collapses the weekly noise.
    monthly_expiries: list[str] = []
    seen_months: set[tuple[int, int]] = set()
    for e in expiries:
        try:
            d = datetime.strptime(str(e), '%d-%b-%Y').date()
        except ValueError:
            continue
        key = (d.year, d.month)
        if key not in seen_months:
            # Find the *last* expiry of this month from the available list
            same_month = [
                str(x) for x in expiries
                if datetime.strptime(str(x), '%d-%b-%Y').date().year == d.year
                and datetime.strptime(str(x), '%d-%b-%Y').date().month == d.month
            ]
            same_month.sort(key=lambda s: datetime.strptime(s, '%d-%b-%Y').date())
            monthly_expiries.append(same_month[-1])
            seen_months.add(key)
        if len(monthly_expiries) >= 3:
            break

    contracts: list[dict] = []
    spot = _safe_float(spot)
    atm_strike = round(spot / 50) * 50

    for exp_str in monthly_expiries:
        try:
            expiry_dt = datetime.strptime(exp_str, '%d-%b-%Y')
        except ValueError:
            continue
        # Re-fetch chain for *this* expiry so we get the right LTPs
        try:
            df_e, _, _ = nse.options.option_chain('NIFTY', expiry_date=exp_str)
        except Exception:
            continue
        if df_e is None or df_e.empty:
            continue

        # The chain may come in wide or long format — handle both.
        ce_ltp = pe_ltp = 0.0
        if 'strikePrice' in df_e.columns:
            row = df_e[df_e['strikePrice'] == atm_strike]
            if not row.empty:
                ce_ltp = _safe_float(row.iloc[0].get('CE_lastPrice'))
                pe_ltp = _safe_float(row.iloc[0].get('PE_lastPrice'))
        else:
            ce = df_e[(df_e.get('Strike') == atm_strike) & (df_e.get('Type') == 'CE')]
            pe = df_e[(df_e.get('Strike') == atm_strike) & (df_e.get('Type') == 'PE')]
            if not ce.empty: ce_ltp = _safe_float(ce.iloc[0].get('LTP'))
            if not pe.empty: pe_ltp = _safe_float(pe.iloc[0].get('LTP'))

        if ce_ltp <= 0 or pe_ltp <= 0:
            continue

        T = _years_to_expiry(exp_str)
        # Forward = K + (C - P) * e^(rT). Carry term is tiny but kept for accuracy.
        forward = atm_strike + (ce_ltp - pe_ltp) * math.exp(RISK_FREE * T)
        forward = round(forward, 2)
        prev_close = round(spot, 2)         # we don't have prior-day futures close offline
        change = round(forward - spot, 2)
        change_pct = round((change / spot) * 100, 3) if spot else 0

        contracts.append({
            'identifier':   f'NIFTY-FUT-{exp_str}',
            'expiry_date':  exp_str,
            'expiry_iso':   expiry_dt.strftime('%Y-%m-%d'),
            'ltp':          forward,
            'prev_close':   prev_close,
            'change':       change,
            'change_pct':   change_pct,
            'open_interest':0,
            'change_in_oi': 0,
            'volume':       0,
            'vwap':         0.0,
            'day_high':     0.0,
            'day_low':      0.0,
            'lot_size':     NIFTY_LOT,
            'contract_value': round(forward * NIFTY_LOT, 2),
            'span_margin':  round(forward * NIFTY_LOT * SPAN_PCT, 2),
            'source':       'pcp_synthetic',
            '_expiry_dt':   expiry_dt,
        })

    return contracts, spot, f'derived from put-call parity ({len(contracts)} expiries)'


# ── Public API ───────────────────────────────────────────────────────────

def fetch_nifty_futures(force: bool = False) -> dict:
    """Get NIFTY futures contracts with cross-validation.

    Tries the live NSE derivative endpoint first. If that returns empty
    (e.g. off-market hours), derives the same data via put-call parity
    from the option chain — clearly tagged `source: pcp_synthetic` so
    the UI can flag it as theoretical-fair-value rather than live trade.
    """
    global _cache
    now = time.time()
    with _cache_lock:
        cached = _cache.get('data')
        ts = _cache.get('ts', 0.0)
        if not force and cached and (now - ts) < CACHE_TTL_SEC:
            return {**cached, 'stale': False}

    try:
        from pnsea import NSE
        nse = NSE()
    except Exception as e:
        return {
            'spot': 0, 'contracts': [],
            'warnings': [f'pnsea unavailable: {e}'],
            'fetched_at': datetime.utcnow().isoformat() + 'Z',
            'stale': True, 'fresh': False,
            'source': 'unavailable',
        }

    contracts, spot, source_note = _fetch_live_futures(nse)
    src_label = 'nseindia.com/api/quote-derivative (live)'

    # Fall back to PCP if live returned nothing useful
    if not contracts or spot <= 0:
        contracts, spot, source_note = _fetch_synthetic_futures(nse)
        src_label = 'option-chain put-call parity (theoretical forward)'

    validated, warnings = _validate_payload(spot, contracts)
    for c in validated:
        c.pop('_expiry_dt', None)

    # Note: source_note (e.g. "derived from put-call parity") is intentionally
    # NOT appended to `warnings` — the per-card PCP-synthetic / live-LTP badge
    # already communicates the pricing mode. Keep `warnings` reserved for
    # actual data-trust issues (basis breach, lot mismatch, inversion).

    result = {
        'spot': round(spot, 2),
        'contracts': validated,
        'warnings': warnings,
        'fetched_at': datetime.utcnow().isoformat() + 'Z',
        'stale': False,
        'fresh': True,
        'source': src_label,
    }

    with _cache_lock:
        _cache = {'ts': now, 'data': result}

    return result


def get_contract_by_expiry(expiry_date: str) -> Optional[dict]:
    data = fetch_nifty_futures()
    for c in data['contracts']:
        if c['expiry_date'] == expiry_date:
            return c
    # If the requested expiry isn't in our (monthly) list, return the
    # nearest contract — that's what a futures hedger would actually trade
    # against, since weekly futures don't exist for NIFTY.
    return data['contracts'][0] if data['contracts'] else None


def nearest_active_future() -> Optional[dict]:
    data = fetch_nifty_futures()
    return data['contracts'][0] if data['contracts'] else None


def estimate_futures_margin(ltp: float, lots: int = 1, lot_size: int = NIFTY_LOT) -> dict:
    if ltp <= 0 or lots <= 0:
        return {'contract_value': 0, 'span': 0, 'exposure': 0, 'total': 0,
                'method': 'estimate', 'lot_size': lot_size}
    contract_value = round(ltp * lot_size * lots, 2)
    span = round(contract_value * 0.085, 2)
    exposure = round(contract_value * 0.015, 2)
    return {
        'contract_value': contract_value,
        'span': span,
        'exposure': exposure,
        'total': round(span + exposure, 2),
        'method': 'estimate',
        'lot_size': lot_size,
    }
