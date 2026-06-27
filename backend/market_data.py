"""
Market data engine — fetches live NSE data via PNSEA, computes Greeks,
ranges, S/R levels, risk scores, and directional signals.

Ported from Sheet_dasboard_codes/option_chain_greeks.py and risk_monitor.py.
"""

import math
import threading
import time as _time
import numpy as np
import pandas as pd
from scipy.stats import norm
from datetime import datetime, date, timedelta
from pnsea import NSE
from config import RISK_FREE_RATE, LOT_SIZE, FAST_REFRESH_SECONDS

CONFLUENCE_WINDOW = 75  # pts — OI and Fib levels within this band are merged


# ── Shared market-data cache ────────────────────────────────────────────────
#
# Everything fetched here (option chains, VIX, Fibonacci, pivots) is *common*
# to every user — it does not depend on who is logged in.  Without a cache,
# each API request from each user triggers its own NSE / yfinance round-trip,
# so the same chain gets scraped N times per refresh for N users.
#
# This module-level TTL cache makes the fetch happen *once* per refresh window
# and serves every subsequent caller (any route, any user, the scheduler) from
# memory.  Only user-specific data (positions, journal, alerts) is still
# computed per user.  The cache is thread-safe because FastAPI runs sync
# endpoints in a worker thread-pool and the scheduler fetches via
# run_in_executor.
#
# TTLs:
#   • Chain / VIX  — short while the market is open (data ticks), long when
#     closed (NSE just returns the last frozen snapshot, so re-fetching is
#     wasted work).  The open-TTL is set slightly above the scheduler's refresh
#     interval so the cache stays continuously warm between scheduler cycles.
#   • Fibonacci / pivots — derived from daily OHLC; they only change once a day,
#     so a long TTL is fine even intraday.

_CACHE_LOCK = threading.RLock()
_chain_cache: dict = {}   # (symbol, expiry) -> (expires_at_monotonic, (df, expiries, spot))
_misc_cache: dict = {}    # key -> (expires_at_monotonic, value)

_CHAIN_TTL_OPEN = float(FAST_REFRESH_SECONDS) + 5.0   # ~20s — covers the gap between 15s cycles
_CHAIN_TTL_CLOSED = 600.0                              # 10 min when market closed
_VIX_TTL_OPEN = float(FAST_REFRESH_SECONDS) + 5.0
_VIX_TTL_CLOSED = 600.0
_DAILY_TTL = 600.0                                     # Fib / pivot — daily OHLC derived


def _now() -> float:
    return _time.monotonic()


def _market_is_open() -> bool:
    """Best-effort market-open check (lazy import to avoid an import cycle)."""
    try:
        from scheduler import is_market_open
        return bool(is_market_open().get("open"))
    except Exception:
        return True   # assume open → use the shorter, safer TTL


def _chain_cache_get(symbol, expiry):
    with _CACHE_LOCK:
        entry = _chain_cache.get((symbol, expiry))
        if entry and entry[0] > _now():
            df, expiries, spot = entry[1]
            # Return copies so a caller mutating the frame can never corrupt
            # the shared cache entry.
            return df.copy(), list(expiries), spot
    return None


def _chain_cache_put(symbol, expiry, payload, resolved_expiry):
    ttl = _CHAIN_TTL_OPEN if _market_is_open() else _CHAIN_TTL_CLOSED
    expires_at = _now() + ttl
    with _CACHE_LOCK:
        _chain_cache[(symbol, expiry)] = (expires_at, payload)
        # Mirror an expiry=None ("nearest") fetch under its resolved expiry too,
        # so an explicit request for that same expiry hits the cache instead of
        # re-scraping.
        if resolved_expiry and resolved_expiry != expiry:
            _chain_cache[(symbol, resolved_expiry)] = (expires_at, payload)


def _memo(key, ttl, producer, cache_falsy=False):
    """Generic TTL memo for the non-chain common fetches.

    A failed fetch (falsy result) is not cached unless ``cache_falsy`` is set,
    so a transient NSE/yfinance error doesn't pin an empty value for the whole
    TTL — the next caller retries.
    """
    with _CACHE_LOCK:
        entry = _misc_cache.get(key)
        if entry and entry[0] > _now():
            return entry[1]
    value = producer()
    if value or cache_falsy:
        with _CACHE_LOCK:
            _misc_cache[key] = (_now() + ttl, value)
    return value


def clear_market_cache():
    """Drop all cached common data (used by tests / manual refresh)."""
    with _CACHE_LOCK:
        _chain_cache.clear()
        _misc_cache.clear()


# ── Symbol universe & lot sizes ─────────────────────────────────────────────
#
# Index symbols use the Indices option-chain endpoint; every other F&O symbol
# is an individual stock and uses the Equity endpoint.  Lot sizes vary per stock
# and are sourced from NSE's canonical F&O market-lots master CSV (cached daily),
# with a hard fallback to the NIFTY lot.

_INDEX_SYMBOLS = {"NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "NIFTYNXT50"}

_FO_MKTLOTS_URL = "https://nsearchives.nseindia.com/content/fo/fo_mktlots.csv"
_LOTSIZE_TTL = 6 * 3600.0   # refresh the lot-size master a few times a day


def _fetch_lot_size_map() -> dict:
    """Parse NSE's F&O market-lots master into {SYMBOL: lot_size}.

    The CSV has UNDERLYING, SYMBOL, then one lot column per expiry month; the
    first numeric month column is the current lot.  Returns {} on failure so the
    caller falls back to the default lot.
    """
    try:
        resp = NSE().endpoint_tester(_FO_MKTLOTS_URL)
        text = resp.text if hasattr(resp, "text") else ""
        if not text:
            return {}
        out = {}
        lines = text.splitlines()
        for line in lines[1:]:
            parts = [p.strip() for p in line.split(",")]
            if len(parts) < 3:
                continue
            sym = parts[1].upper()
            if not sym or sym == "SYMBOL":
                continue
            lot = None
            for cell in parts[2:]:
                if cell.isdigit() and int(cell) > 0:
                    lot = int(cell)
                    break
            if lot:
                out[sym] = lot
        return out
    except Exception as e:
        print(f"  [WARN] lot-size master fetch failed: {e}")
        return {}


def get_lot_size(symbol: str) -> int:
    """Return the F&O lot size for a symbol (cached). Falls back to LOT_SIZE."""
    if not symbol:
        return LOT_SIZE
    sym = symbol.upper()
    lot_map = _memo("fo_lotsizes", _LOTSIZE_TTL, _fetch_lot_size_map)
    if lot_map and sym in lot_map:
        return lot_map[sym]
    return LOT_SIZE


# ── Black-76 Greeks (forward-based) ─────────────────────────────────────────
#
# NIFTY options are European index options and must be priced/greeked off the
# *forward* (synthetic-futures) price, NOT the spot.  The forward sits above
# spot by the cost-of-carry minus dividends, so using spot systematically
# under-states call deltas and over-states put deltas — which is exactly why the
# chain's deltas were "off" vs Sensibull/NSE (e.g. ATM CE read 0.47 instead of
# the correct 0.51).
#
# We pass the synthetic forward (derived from put-call parity, see
# `synthetic_forward`) into `greeks` and use the Black-76 model.  When no
# forward is supplied, we fall back to the pure-carry forward F = S·e^(rT),
# which reduces Black-76 to the previous spot model (deltas differ only by the
# tiny discount factor) — so callers that only have spot are unaffected.

def greeks(S, K, T, r, sigma, option_type="CE", forward=None):
    # Forward price: prefer the supplied (parity-derived) forward; otherwise
    # use the cost-of-carry forward off spot.
    if forward and forward > 0:
        F = forward
    elif S > 0 and T > 0:
        F = S * math.exp(r * T)
    else:
        F = S

    if T <= 0 or sigma <= 0 or F <= 0 or K <= 0:
        return {g: 0.0 for g in ("delta", "gamma", "theta", "vega", "rho")}

    sqrtT = np.sqrt(T)
    d1 = (np.log(F / K) + 0.5 * sigma ** 2 * T) / (sigma * sqrtT)
    d2 = d1 - sigma * sqrtT

    n_d1 = norm.pdf(d1)
    disc = np.exp(-r * T)

    # Black-76 gamma/vega (w.r.t. the forward).
    gamma = disc * n_d1 / (F * sigma * sqrtT)
    vega = F * disc * n_d1 * sqrtT / 100

    decay = F * disc * n_d1 * sigma / (2 * sqrtT)  # time-decay term (shared)

    if option_type == "CE":
        delta = disc * norm.cdf(d1)
        price = disc * (F * norm.cdf(d1) - K * norm.cdf(d2))
        theta = (r * disc * (F * norm.cdf(d1) - K * norm.cdf(d2)) - decay) / 365
        rho = -T * price / 100
    else:
        delta = -disc * norm.cdf(-d1)
        price = disc * (K * norm.cdf(-d2) - F * norm.cdf(-d1))
        theta = (r * disc * (K * norm.cdf(-d2) - F * norm.cdf(-d1)) - decay) / 365
        rho = -T * price / 100

    return {
        "delta": round(float(delta), 4),
        "gamma": round(float(gamma), 6),
        "theta": round(float(theta), 4),
        "vega": round(float(vega), 4),
        "rho": round(float(rho), 4),
    }


def synthetic_forward(spot, atm_pairs, T=0.0, r=RISK_FREE_RATE):
    """Derive the synthetic-futures (forward) price from put-call parity.

    `atm_pairs` is an iterable of (strike, ce_ltp, pe_ltp) for strikes near the
    money.  Parity gives F = K + (CE − PE); we average the nearest few strikes
    for stability against a single stale quote.  Falls back to the cost-of-carry
    forward F = S·e^(rT) when no valid pairs are available.

    This matches the "Synth Fut Price" that vendors (e.g. Sensibull) use as the
    underlying for index-option Greeks.
    """
    vals = []
    for k, ce, pe in atm_pairs:
        try:
            if ce and pe and ce > 0 and pe > 0:
                vals.append(float(k) + float(ce) - float(pe))
        except (TypeError, ValueError):
            continue
    if vals:
        return sum(vals) / len(vals)
    if spot > 0 and T > 0:
        return spot * math.exp(r * T)
    return spot


def time_to_expiry(expiry_str: str) -> float:
    expiry_dt = datetime.strptime(expiry_str, "%d-%b-%Y").date()
    today = date.today()
    days_left = (expiry_dt - today).days
    return max(days_left, 0) / 365.0


def days_to_expiry(expiry_str: str) -> int:
    expiry_dt = datetime.strptime(expiry_str, "%d-%b-%Y").date()
    return max((expiry_dt - date.today()).days, 0)


def trading_days_to_expiry(expiry_str: str) -> int:
    """Count *trading* days remaining until an expiry.

    Excludes weekends and NSE holidays.  Returns 0 if expiry is today or past.
    The current day is not counted (consistent with `days_to_expiry`).

    This is the unit used by the "should we roll to next expiry" rule — a
    Friday with Monday holiday + Tuesday expiry shows DTE=4 calendar days but
    only 1 trading day, and option metrics on the chain become unreliable
    just like they do on a normal Monday before a Tuesday expiry.
    """
    try:
        expiry_dt = datetime.strptime(expiry_str, "%d-%b-%Y").date()
    except Exception:
        return 0
    today = date.today()
    if expiry_dt <= today:
        return 0
    # Lazy import keeps market_data module-level free of scheduler dependency.
    try:
        from scheduler import NSE_HOLIDAYS_2026 as _HOLIDAYS  # noqa
    except Exception:
        _HOLIDAYS = set()
    count = 0
    cur = today
    while cur < expiry_dt:
        cur = cur + timedelta(days=1)
        if cur.weekday() >= 5:
            continue
        if cur.strftime("%Y-%m-%d") in _HOLIDAYS:
            continue
        count += 1
    return count


# Single source of truth for the "active default" expiry decision.
# Rule (matches user-stated requirement):
#   • If trading-days-to-expiry <= 1 → that expiry is too close to read
#     reliable IV/skew/RR off the chain; roll to the next.
#   • Falls back to the last expiry if every entry would otherwise be skipped.
#
# Used by scheduler, /compute-for-expiry callers, market-analytics, and
# anywhere a "default" expiry is picked.  Keep this function the only place
# that encodes the threshold so dashboard, analytics, and strategy builder
# never disagree on what "this expiry" means.
ACTIVE_EXPIRY_MIN_TDTE = 2   # need ≥ 2 trading days to call an expiry "active"


def pick_active_expiry(expiries) -> str | None:
    """Return the nearest expiry whose trading-days remaining is ≥ threshold.

    `expiries` is the list returned by build_chain — strings or date-likes.
    """
    if not expiries:
        return None
    candidates = [str(e) for e in expiries]
    for e in candidates:
        try:
            if trading_days_to_expiry(e) >= ACTIVE_EXPIRY_MIN_TDTE:
                return e
        except Exception:
            continue
    # Every expiry is either past or too close — fall back to the last one
    # (latest available) so callers always get *something* to compute against.
    return candidates[-1]


# ── Option Chain Fetching ───────────────────────────────────────────────────

def _chg_int(v) -> int:
    """Parse NSE change-in-OI (may be NaN/None/str) → int, 0 on failure."""
    try:
        if v is None or pd.isna(v):
            return 0
        return int(float(str(v).replace(",", "")))
    except (TypeError, ValueError):
        return 0


def _extract_option_row(strike, opt_type, ltp, iv_pct, oi, vol, spot, expiry, r,
                        forward=None):
    T = time_to_expiry(expiry)
    sigma = iv_pct / 100.0
    g = greeks(spot, strike, T, r, sigma, option_type=opt_type, forward=forward)
    return {
        "Strike": int(round(strike)),
        "Type": opt_type,
        "LTP": round(float(ltp), 2),
        "IV": round(float(iv_pct), 2),
        "OI": int(oi) if not pd.isna(oi) else 0,
        "Volume": int(vol) if not pd.isna(vol) else 0,
        **g,
    }


def build_chain(symbol="NIFTY", expiry_date=None, risk_free_rate=None,
                use_cache=True, refresh=False):
    """Fetch option chain from NSE. Returns (DataFrame, expiry_list, spot_price).

    Results are cached and shared across all callers/users (see the shared
    market-data cache above) so the same chain is scraped only once per refresh
    window regardless of how many users are connected.

    ``use_cache``  — read from / write to the shared cache (default True).
    ``refresh``    — bypass the *read* but still write-through (the scheduler
                     uses this so it always broadcasts fresh data while keeping
                     the cache warm for the REST endpoints in between cycles).
    """
    if risk_free_rate is None:
        risk_free_rate = RISK_FREE_RATE

    # Only the default risk-free rate participates in the shared cache; a custom
    # rate is an off-cache, caller-specific computation.
    cacheable = use_cache and risk_free_rate == RISK_FREE_RATE

    if cacheable and not refresh:
        hit = _chain_cache_get(symbol, expiry_date)
        if hit is not None:
            return hit

    nse = NSE()
    kwargs = {}
    if expiry_date:
        kwargs["expiry_date"] = expiry_date

    # Index symbols (NIFTY, BANKNIFTY, …) use the Indices option-chain endpoint;
    # everything else is treated as an individual F&O stock and uses the Equity
    # endpoint.  Both return the identical wide format, so the rest of the
    # pipeline (Greeks, forward, cache) is unchanged.
    #
    # NSE intermittently returns an empty payload (the equity expiry lookup is
    # especially flaky under concurrent load — "No expiry dates found"), so retry
    # a few times with a fresh session before giving up.
    is_index = symbol.upper() in _INDEX_SYMBOLS
    chain_df, expiries, spot, last_err = None, [], 0.0, None
    for attempt in range(3):
        try:
            if is_index:
                chain_df, expiries, spot = nse.options.option_chain(symbol, **kwargs)
            else:
                chain_df, expiries, spot = nse.equityOptions.option_chain(symbol, **kwargs)
            if chain_df is not None and not chain_df.empty:
                break
        except Exception as e:
            last_err = e
        if attempt < 2:
            _time.sleep(0.7)
            nse = NSE()   # fresh session/cookies for the retry

    if chain_df is None or chain_df.empty:
        # Last resort: serve a previously cached chain (even if just expired)
        # rather than failing the user's action.
        with _CACHE_LOCK:
            stale = _chain_cache.get((symbol, expiry_date))
        if stale is not None:
            df0, exp0, spot0 = stale[1]
            return df0.copy(), list(exp0), spot0
        if last_err:
            raise last_err
        raise ValueError(f"No option chain data returned for {symbol}")

    if "strikePrice" in chain_df.columns:
        rows = _process_wide(chain_df, spot, expiry_date or expiries[0], risk_free_rate)
    else:
        rows = _process_long(chain_df, spot, expiry_date or expiries[0], risk_free_rate)

    result = pd.DataFrame(rows)

    if cacheable:
        resolved = expiry_date or (str(expiries[0]) if len(expiries) else None)
        _chain_cache_put(symbol, expiry_date,
                         (result, list(expiries), float(spot)), resolved)
        # Hand the caller copies so it can mutate freely.
        return result.copy(), list(expiries), float(spot)

    return result, expiries, float(spot)


def _forward_from_wide(df, spot, T, r):
    """Synthetic forward from the wide chain via parity on near-ATM strikes."""
    try:
        tmp = df.copy()
        tmp["_dist"] = (tmp["strikePrice"].astype(float) - spot).abs()
        pairs = [
            (row.get("strikePrice"), row.get("CE_lastPrice"), row.get("PE_lastPrice"))
            for _, row in tmp.nsmallest(3, "_dist").iterrows()
        ]
        return synthetic_forward(spot, pairs, T, r)
    except Exception:
        return synthetic_forward(spot, [], T, r)


def _process_wide(df, spot, expiry, r):
    T = time_to_expiry(expiry)
    forward = _forward_from_wide(df, spot, T, r)
    rows = []
    for _, row in df.iterrows():
        strike = row.get("strikePrice")
        for opt_type, prefix in (("CE", "CE_"), ("PE", "PE_")):
            ltp = row.get(f"{prefix}lastPrice", np.nan)
            iv_pct = row.get(f"{prefix}impliedVolatility", 0)
            oi = row.get(f"{prefix}openInterest", np.nan)
            vol = row.get(f"{prefix}totalTradedVolume", np.nan)
            if pd.isna(ltp) or ltp == 0:
                continue
            d = _extract_option_row(
                strike, opt_type, ltp, iv_pct or 0, oi, vol, spot, expiry, r,
                forward=forward,
            )
            # Additive: day's OI change (for the OI-buildup signal). Extra column —
            # existing consumers read by name, so this is unaffecting.
            d["ChgOI"] = _chg_int(row.get(f"{prefix}changeinOpenInterest"))
            rows.append(d)
    return rows


def _process_long(df, spot, expiry, r):
    T = time_to_expiry(expiry)

    # Pair CE/PE LTPs by strike so we can derive the synthetic forward via parity.
    ltp_by_strike: dict = {}
    for _, row in df.iterrows():
        strike = row.get("strikePrice") or row.get("Strike")
        opt_type = row.get("optionType") or row.get("Type", "CE")
        ltp = row.get("lastPrice") or row.get("LTP", np.nan)
        if strike is None or pd.isna(ltp):
            continue
        ltp_by_strike.setdefault(float(strike), {})[opt_type] = float(ltp)
    pairs = sorted(
        ((k, v.get("CE"), v.get("PE")) for k, v in ltp_by_strike.items()
         if v.get("CE") and v.get("PE")),
        key=lambda kv: abs(kv[0] - spot),
    )[:3]
    forward = synthetic_forward(spot, pairs, T, r)

    rows = []
    for _, row in df.iterrows():
        strike = row.get("strikePrice") or row.get("Strike")
        opt_type = row.get("optionType") or row.get("Type", "CE")
        ltp = row.get("lastPrice") or row.get("LTP", np.nan)
        iv_pct = row.get("impliedVolatility") or row.get("IV", 0)
        oi = row.get("openInterest") or row.get("OI", np.nan)
        vol = row.get("totalTradedVolume") or row.get("Volume", np.nan)
        if pd.isna(ltp) or ltp == 0:
            continue
        d = _extract_option_row(
            strike, opt_type, ltp, iv_pct or 0, oi, vol, spot, expiry, r,
            forward=forward,
        )
        d["ChgOI"] = _chg_int(row.get("changeinOpenInterest"))
        rows.append(d)
    return rows


# ── VIX Fetch ───────────────────────────────────────────────────────────────

def fetch_vix(nse=None, use_cache=True, refresh=False) -> dict | None:
    """Fetch India VIX + NIFTY 50 change. Cached/shared across users.

    ``refresh`` bypasses the read but still writes through, so the scheduler
    always broadcasts a fresh value while keeping the cache warm for the REST
    endpoints between cycles.
    """
    if not use_cache:
        return _fetch_vix_raw(nse)

    ttl = _VIX_TTL_OPEN if _market_is_open() else _VIX_TTL_CLOSED
    if refresh:
        value = _fetch_vix_raw(nse)
        if value:
            with _CACHE_LOCK:
                _misc_cache["vix"] = (_now() + ttl, value)
        return dict(value) if value else None

    cached = _memo("vix", ttl, lambda: _fetch_vix_raw(nse))
    # Return a copy so callers can't mutate the shared dict.
    return dict(cached) if cached else None


def _fetch_vix_raw(nse=None) -> dict | None:
    try:
        if nse is None:
            nse = NSE()
        resp = nse.endpoint_tester("https://www.nseindia.com/api/allIndices")
        data = resp.json()
        result = {}
        for idx in data.get("data", []):
            name = str(idx.get("indexSymbol") or idx.get("index") or "")
            if "VIX" in name.upper():
                result["last"] = float(idx.get("last", 0))
                result["change"] = float(idx.get("variation", 0))
                result["pChange"] = float(idx.get("percentChange", 0))
            if name == "NIFTY 50":
                result["nifty_change"] = float(idx.get("variation", 0))
                result["nifty_change_pct"] = float(idx.get("percentChange", 0))
        return result if "last" in result else None
    except Exception as e:
        print(f"  [WARN] VIX fetch failed: {e}")
    return None


# ── Range Calculation ───────────────────────────────────────────────────────

def calculate_ranges(spot: float, vix: float, dte: int, vix_offset_pct: float = 0) -> dict:
    """Calculate range bands based on VIX and DTE.

    Formulas (matching the Google Sheet):
      move = spot * VIX / sqrt(252 / DTE) / 100
      Calc Range Top    = CEILING(spot + move, 100)
      Calc Range Bottom = FLOOR(spot - move, 100)

    With offset (e.g. 50 → 1.5 SD):
      offset_move = spot * VIX * (1 + offset/100) / sqrt(252 / DTE) / 100
      VIX Range Top    = CEILING(spot + offset_move, 100)
      VIX Range Bottom = FLOOR(spot - offset_move, 100)
    """
    if vix <= 0 or dte <= 0:
        return {
            "calc_range_top": spot,
            "calc_range_bottom": spot,
            "vix_range_top": spot,
            "vix_range_bottom": spot,
        }

    # Base move (1 SD)
    move = spot * vix / math.sqrt(252 / dte) / 100

    # Calculated range: ceiling/floor to nearest 100
    calc_top = math.ceil((spot + move) / 100) * 100
    calc_bottom = math.floor((spot - move) / 100) * 100

    # VIX range with offset (wider SD)
    if vix_offset_pct > 0:
        offset_move = spot * vix * (1 + vix_offset_pct / 100) / math.sqrt(252 / dte) / 100
    else:
        offset_move = move

    vix_top = math.ceil((spot + offset_move) / 100) * 100
    vix_bottom = math.floor((spot - offset_move) / 100) * 100

    return {
        "calc_range_top": calc_top,
        "calc_range_bottom": calc_bottom,
        "vix_range_top": vix_top,
        "vix_range_bottom": vix_bottom,
    }


# ── Risk Scoring (ported from risk_monitor.py) ──────────────────────────────

def _risk_label(score: float) -> str:
    if score < 35:
        return "OK"
    if score < 60:
        return "WARNING"
    if score < 80:
        return "DANGER"
    return "CRITICAL"


def _delta_score(delta: float) -> float:
    d = abs(delta)
    if d <= 0.08: return 0
    if d <= 0.15: return 20
    if d <= 0.20: return 40
    if d <= 0.30: return 65
    if d <= 0.40: return 85
    return 100


def _iv_score(entry_iv: float, current_iv: float, vix_abs: float = 0.0) -> float:
    if entry_iv <= 0:
        return 0
    pct = (current_iv - entry_iv) / entry_iv * 100
    if pct < 10: base = 0
    elif pct < 25: base = 20
    elif pct < 40: base = 45
    elif pct < 60: base = 70
    else: base = 90
    if vix_abs >= 25:
        base = min(base + 20, 100)
    return base


def _premium_score(entry_premium: float, current_ltp: float) -> float:
    if entry_premium <= 0:
        return 0
    ratio = current_ltp / entry_premium
    if ratio < 1.5: return 0
    if ratio < 2.0: return 25
    if ratio < 2.5: return 50
    if ratio < 3.0: return 70
    if ratio < 5.0: return 85
    return 100


def _proximity_score(current_spot: float, strike: float) -> float:
    if current_spot <= 0:
        return 100
    dist_pct = abs(current_spot - strike) / current_spot * 100
    if dist_pct > 4.0: return 0
    if dist_pct > 3.0: return 15
    if dist_pct > 2.0: return 35
    if dist_pct > 1.5: return 50
    if dist_pct > 1.0: return 65
    if dist_pct > 0.5: return 80
    return 100


def _gamma_dte_score(gamma: float, delta: float, dte: int) -> float:
    """Gamma/DTE acceleration — captures convexity risk near expiry."""
    d = abs(delta)
    gamma_impact = abs(gamma) * LOT_SIZE * 50  # delta shift per 50-pt NIFTY move

    if dte > 21:
        if gamma_impact < 1.0: return 0
        if gamma_impact < 2.0: return 20
        return 35
    if dte > 14:
        if d < 0.20: return 30
        return 55
    if dte > 7:
        if d < 0.15: return 40
        return 75
    # DTE <= 7 — gamma explosion zone
    if d < 0.10: return 50
    return 100


def _theta_ratio_score(delta: float, theta: float) -> float:
    """Delta-theta ratio: is the position being compensated for its risk?"""
    if theta >= 0 or delta == 0:
        return 0
    dt_ratio = abs(delta) / abs(theta)
    if dt_ratio < 0.20: return 0
    if dt_ratio < 0.30: return 20
    if dt_ratio < 0.50: return 45
    if dt_ratio < 0.75: return 70
    return 100


def _check_hard_triggers(delta: float, entry_premium: float,
                         current_ltp: float, current_spot: float,
                         strike: float, dte: int,
                         vix_abs: float, vix_chg_pct: float) -> bool:
    """Return True if ANY single hard trigger fires → immediate hedge."""
    d = abs(delta)
    dist_pct = abs(current_spot - strike) / current_spot * 100 if current_spot > 0 else 0
    prem_ratio = current_ltp / entry_premium if entry_premium > 0 else 0

    if d >= 0.35:                                       return True
    if prem_ratio >= 3.0:                               return True
    if dist_pct <= 0.5:                                 return True
    if vix_abs >= 28 and vix_chg_pct >= 25:             return True
    if dte <= 5 and d >= 0.15:                          return True
    return False


def compute_risk_score(
    delta, entry_iv, current_iv, entry_premium, current_ltp,
    current_spot, strike, vix_abs=0.0,
    gamma=0.0, theta=0.0, dte=30, vix_chg_pct=0.0,
) -> dict:
    """Compute 6-factor composite risk score (0-100) with breakdown.

    Weights (with premium):
      delta(0.25) + gamma/DTE(0.15) + IV(0.15) + premium(0.15)
      + proximity(0.20) + theta_ratio(0.10)
    Without premium: redistribute 0.15 across other factors.

    Returns dict with score, label, breakdown, and hedge_required flag.
    hedge_required is True if hard trigger fires OR composite >= 65.
    """
    ds  = _delta_score(delta)
    gs  = _gamma_dte_score(gamma, delta, dte)
    ivs = _iv_score(entry_iv, current_iv, vix_abs)
    prx = _proximity_score(current_spot, strike)
    trs = _theta_ratio_score(delta, theta)

    has_premium = entry_premium > 0
    ps = _premium_score(entry_premium, current_ltp) if has_premium else 0

    if has_premium:
        total = round(
            0.25 * ds + 0.15 * gs + 0.15 * ivs +
            0.15 * ps + 0.20 * prx + 0.10 * trs, 1)
    else:
        total = round(
            0.30 * ds + 0.17 * gs + 0.17 * ivs +
            0.24 * prx + 0.12 * trs, 1)

    # Hard circuit breakers
    hard_triggered = _check_hard_triggers(
        delta, entry_premium, current_ltp, current_spot,
        strike, dte, vix_abs, vix_chg_pct) if has_premium else False

    hedge_required = hard_triggered or total >= 65

    iv_chg_pct = ((current_iv - entry_iv) / entry_iv * 100) if entry_iv > 0 else 0
    dist_pts = abs(current_spot - strike)

    return {
        "score": total,
        "label": _risk_label(total),
        "hedge_required": hedge_required,
        "hard_triggered": hard_triggered,
        "breakdown": {
            "delta": {"score": ds, "value": abs(delta), "label": _risk_label(ds)},
            "gamma_dte": {"score": gs, "label": _risk_label(gs)},
            "iv": {"score": ivs, "change_pct": round(iv_chg_pct, 1), "label": _risk_label(ivs)},
            "premium": {"score": ps, "ratio": round(current_ltp / entry_premium, 2) if entry_premium > 0 else 0, "label": _risk_label(ps)},
            "proximity": {"score": prx, "distance_pts": round(dist_pts), "label": _risk_label(prx)},
            "theta_ratio": {"score": trs, "label": _risk_label(trs)},
        }
    }


# ── S/R Levels ──────────────────────────────────────────────────────────────

def find_oi_sr_levels(chain_df, current_spot: float) -> dict:
    """Find top OI-based support and resistance levels."""
    ce_zone = chain_df[
        (chain_df["Type"] == "CE") & (chain_df["Strike"] > current_spot)
    ].copy()
    pe_zone = chain_df[
        (chain_df["Type"] == "PE") & (chain_df["Strike"] < current_spot)
    ].copy()

    resistances = (ce_zone.nlargest(3, "OI")[["Strike", "OI"]]
                   .rename(columns={"Strike": "strike", "OI": "oi"})
                   .to_dict("records"))
    supports = (pe_zone.nlargest(3, "OI")[["Strike", "OI"]]
                .rename(columns={"Strike": "strike", "OI": "oi"})
                .to_dict("records"))

    for row in resistances + supports:
        row["strike"] = int(round(row["strike"]))
        row["oi"] = int(row["oi"]) if row["oi"] else 0
        row["source"] = "OI"

    return {"supports": supports, "resistances": resistances}


def fetch_fibonacci_levels(lookback_days: int = 20) -> list[int]:
    """Compute Fibonacci retracement levels from recent NIFTY OHLC.

    Derived from daily OHLC (changes once a day) so it is cached/shared with a
    long TTL across all users.
    """
    return _memo(f"fib:{lookback_days}", _DAILY_TTL,
                 lambda: _fetch_fibonacci_levels_raw(lookback_days))


def _fetch_fibonacci_levels_raw(lookback_days: int = 20) -> list[int]:
    try:
        import yfinance as yf
        nifty = yf.download("^NSEI", period=f"{lookback_days + 5}d",
                            progress=False, auto_adjust=True)
        if hasattr(nifty.columns, "levels"):
            nifty.columns = nifty.columns.get_level_values(0)
        if nifty.empty or len(nifty) < 5:
            return []

        nifty = nifty.tail(lookback_days)
        swing_high = float(nifty["High"].max())
        swing_low = float(nifty["Low"].min())
        diff = swing_high - swing_low
        if diff <= 0:
            return []

        fib_ratios = [0.236, 0.382, 0.500, 0.618, 0.786]
        return sorted(int(round(swing_low + diff * r)) for r in fib_ratios)
    except Exception as e:
        print(f"  [WARN] Fibonacci fetch failed: {e}")
        return []


def fetch_pivot_levels(lookback_days: int = 1) -> dict:
    """Compute standard pivot points from previous trading day.

    Derived from daily OHLC (changes once a day) so it is cached/shared with a
    long TTL across all users.
    """
    return _memo(f"pivot:{lookback_days}", _DAILY_TTL,
                 lambda: _fetch_pivot_levels_raw(lookback_days))


def _fetch_pivot_levels_raw(lookback_days: int = 1) -> dict:
    try:
        import yfinance as yf
        nifty = yf.download("^NSEI", period=f"{lookback_days + 5}d",
                            progress=False, auto_adjust=True)
        if hasattr(nifty.columns, "levels"):
            nifty.columns = nifty.columns.get_level_values(0)
        if nifty.empty or len(nifty) < 2:
            return {}

        prev = nifty.iloc[-2]
        H, L, C = float(prev["High"]), float(prev["Low"]), float(prev["Close"])
        P = (H + L + C) / 3

        return {
            "pivot": int(round(P)),
            "supports": [int(round(2*P - H)), int(round(P - (H-L))), int(round(L - 2*(H-P)))],
            "resistances": [int(round(2*P - L)), int(round(P + (H-L))), int(round(H + 2*(P-L)))],
        }
    except Exception as e:
        print(f"  [WARN] Pivot fetch failed: {e}")
        return {}


# ── Merge OI + Fibonacci into top-3 S/R ────────────────────────────────────

def merge_sr_levels(
    oi_supports: list, oi_resistances: list,
    fib_levels: list[int], current_spot: float,
) -> tuple[list, list]:
    """Pick the 3 best S/R levels prioritising OI+Fib confluence.

    Tier 1 — OI strike within CONFLUENCE_WINDOW of a Fib level
    Tier 2 — Remaining OI-only strikes
    Tier 3 — Remaining Fib-only levels
    """
    fib_below = [f for f in fib_levels if f < current_spot]
    fib_above = [f for f in fib_levels if f > current_spot]

    def _merge(oi_list: list, fib_list: list, descending: bool) -> list:
        confluence = []
        oi_only = []
        used_fibs = set()

        for oi_entry in oi_list:
            strike = oi_entry["strike"]
            oi_val = oi_entry.get("oi") or 0
            best_fib = None
            best_dist = CONFLUENCE_WINDOW + 1
            for fib in fib_list:
                d = abs(strike - fib)
                if d <= CONFLUENCE_WINDOW and d < best_dist:
                    best_dist = d
                    best_fib = fib
            if best_fib is not None:
                used_fibs.add(best_fib)
                confluence.append({
                    "strike": strike, "oi": oi_entry.get("oi"),
                    "source": f"OI+Fib (fib {best_fib})",
                    "_oi_val": oi_val, "_dist": best_dist,
                })
            else:
                oi_only.append({
                    "strike": strike, "oi": oi_entry.get("oi"),
                    "source": "OI", "_oi_val": oi_val, "_dist": 9999,
                })

        fib_only = []
        for fib in fib_list:
            if fib not in used_fibs:
                fib_only.append({
                    "strike": fib, "oi": None,
                    "source": "Fib", "_oi_val": 0, "_dist": 9999,
                })

        confluence.sort(key=lambda x: (x["_dist"], -x["_oi_val"]))
        oi_only.sort(key=lambda x: -x["_oi_val"])
        fib_only.sort(key=lambda x: x["strike"], reverse=descending)

        result = (confluence + oi_only + fib_only)[:3]
        for r in result:
            r.pop("_oi_val", None)
            r.pop("_dist", None)
        result.sort(key=lambda x: x["strike"], reverse=descending)
        return result

    top3_supports = _merge(oi_supports, fib_below, descending=True)
    top3_resistances = _merge(oi_resistances, fib_above, descending=False)
    return top3_supports, top3_resistances


def compute_pivot_oi_sr(
    chain_df, current_spot: float, pivot_data: dict,
) -> tuple[list, list]:
    """Pick top-3 supports and resistances from pivot levels, ranked by OI."""
    if not pivot_data:
        return [], []

    pivot_sups = sorted(pivot_data["supports"], reverse=True)
    pivot_ress = sorted(pivot_data["resistances"])

    all_strikes = sorted(chain_df["Strike"].unique())
    if not all_strikes:
        return [], []

    def _nearest_strike(level):
        return min(all_strikes, key=lambda s: abs(s - level))

    def _get_oi(strike, opt_type):
        row = chain_df[(chain_df["Strike"] == strike) & (chain_df["Type"] == opt_type)]
        if not row.empty:
            return int(row.iloc[0]["OI"])
        return 0

    scored_sups = []
    for level in pivot_sups:
        ns = _nearest_strike(level)
        if ns < current_spot:
            oi = _get_oi(ns, "PE")
            scored_sups.append({"strike": ns, "oi": oi, "source": "Pivot+OI"})

    seen = {}
    for s in scored_sups:
        k = s["strike"]
        if k not in seen or s["oi"] > seen[k]["oi"]:
            seen[k] = s
    scored_sups = sorted(seen.values(), key=lambda x: -x["oi"])[:3]

    scored_ress = []
    for level in pivot_ress:
        ns = _nearest_strike(level)
        if ns > current_spot:
            oi = _get_oi(ns, "CE")
            scored_ress.append({"strike": ns, "oi": oi, "source": "Pivot+OI"})

    seen = {}
    for r in scored_ress:
        k = r["strike"]
        if k not in seen or r["oi"] > seen[k]["oi"]:
            seen[k] = r
    scored_ress = sorted(seen.values(), key=lambda x: -x["oi"])[:3]

    scored_sups.sort(key=lambda x: x["strike"], reverse=True)
    scored_ress.sort(key=lambda x: x["strike"])

    return scored_sups, scored_ress


# ── 25-delta Risk Reversal ──────────────────────────────────────────────────

def find_25delta_iv(chain_df, opt_type: str) -> tuple[int, float]:
    sub = chain_df[chain_df["Type"] == opt_type].copy()
    if sub.empty:
        return 0, 0.0
    sub["_d25"] = (sub["delta"].abs() - 0.25).abs()
    best = sub.loc[sub["_d25"].idxmin()]
    return int(best["Strike"]), float(best["IV"])


def compute_risk_reversal(chain_df) -> dict:
    """Compute 25-delta risk reversal."""
    ce_strike, ce_iv = find_25delta_iv(chain_df, "CE")
    pe_strike, pe_iv = find_25delta_iv(chain_df, "PE")
    rr = ce_iv - pe_iv if ce_iv > 0 and pe_iv > 0 else 0

    # RR = CE_IV - PE_IV: negative means puts expensive (bearish skew)
    if rr < -2:
        verdict = "Bearish"
    elif rr > 2:
        verdict = "Bullish"
    else:
        verdict = "Neutral"

    return {
        "ce_strike": ce_strike, "ce_iv": round(ce_iv, 2),
        "pe_strike": pe_strike, "pe_iv": round(pe_iv, 2),
        "rr_value": round(rr, 2),
        "verdict": verdict,
    }


def compute_oi_totals(chain_df, lot_size: int = LOT_SIZE) -> dict:
    """Aggregate Call vs Put open interest — the dashboard's movement monitor.

    Sums OI across all strikes per side (same basis as ``_pcr_score``).  NSE /
    pnsea report OI in **contracts**, but Sensibull/NSE display aggregate OI in
    **shares** (contracts × lot size), e.g. 11.45 Cr — so we multiply by the lot
    before converting to crore (/1e7).  PCR is unaffected (the lot cancels in the
    ratio).  High PCR (puts >> calls) reads bullish (puts written as support);
    low PCR (calls >> puts) reads bearish (calls written as resistance)."""
    call_contracts = float(chain_df.loc[chain_df["Type"] == "CE", "OI"].sum())
    put_contracts = float(chain_df.loc[chain_df["Type"] == "PE", "OI"].sum())
    call_oi = call_contracts * lot_size
    put_oi = put_contracts * lot_size
    pcr = round(put_contracts / call_contracts, 2) if call_contracts > 0 else 0.0

    # Previous-day PCR derived from the day's change-in-OI (prev-close OI =
    # current OI − ChgOI), so the day-over-day PCR change needs no storage and
    # stays realtime. lot cancels in the ratio, so we work in contracts.
    pcr_chg = None
    ce_chg = pe_chg = None
    if "ChgOI" in chain_df.columns:
        ce_chg = float(chain_df.loc[chain_df["Type"] == "CE", "ChgOI"].sum())
        pe_chg = float(chain_df.loc[chain_df["Type"] == "PE", "ChgOI"].sum())
        prev_call = call_contracts - ce_chg
        prev_put = put_contracts - pe_chg
        if prev_call > 0 and prev_put >= 0:
            prev_pcr = prev_put / prev_call
            pcr_chg = round(pcr - prev_pcr, 2)

    # Directional conviction: PCR level decides it, with a MINOR nudge from the
    # day-over-day PCR direction (rising PCR = more put-writing = bullish lean).
    adj_pcr = pcr + (0.1 * pcr_chg if pcr_chg is not None else 0.0)
    if adj_pcr >= 1.2:
        tone, verdict = "BULLISH", "Bullish bias"
        blurb = "More puts written than calls — sellers expect support to hold."
    elif 0 < adj_pcr <= 0.8:
        tone, verdict = "BEARISH", "Bearish bias"
        blurb = "More calls written than puts — sellers expect upside to stall."
    else:
        tone, verdict = "NEUTRAL", "Rangebound"
        # PCR near 1 = calls and puts converging — an unstable balance that can
        # break either way (the frontend colours "reversal" by which side of 1).
        blurb = "Calls and puts nearly balanced — watch for a breakout or reversal either way."
    return {
        "call_oi": call_oi, "put_oi": put_oi,
        "call_oi_cr": round(call_oi / 1e7, 2),
        "put_oi_cr": round(put_oi / 1e7, 2),
        "pcr": pcr, "pcr_chg": pcr_chg,
        # Today's OI writing flow (change-in-OI per side, in contracts) — None when
        # the chain carries no ChgOI column. Feeds the market-tone "OI Flow" read.
        "call_chg": ce_chg, "put_chg": pe_chg,
        "tone": tone, "verdict": verdict, "blurb": blurb,
    }


# NIFTY market-tone weighting — fixed "practical weights" (sum to 100). Each
# factor casts a bullish (+1) / neutral (0) / bearish (−1) vote; the verdict is the
# weighted vote. Order here is also the heatmap's row-major 4×2 layout:
#   row 1 (institutional money) · row 2 (options market)
_TONE_FACTORS = [
    {"k": "fii_fut",     "name": "FII Fut",   "weight": 18},
    {"k": "fii_fut_chg", "name": "FII Fut Δ", "weight": 26},
    {"k": "fii_cash",    "name": "FII Cash",  "weight": 7},
    {"k": "dii_cash",    "name": "DII Cash",  "weight": 2},
    {"k": "oi",          "name": "OI Flow",   "weight": 22},
    {"k": "pcr",         "name": "PCR ≷1",    "weight": 6},
    {"k": "pcr_chg",     "name": "PCR Δ",     "weight": 13},
    {"k": "rr",          "name": "Risk Rev",  "weight": 6},
]


def _sign(x) -> int:
    """+1 / 0 / −1 for a numeric value (0/None → 0)."""
    try:
        v = float(x)
    except (TypeError, ValueError):
        return 0
    return 1 if v > 0 else (-1 if v < 0 else 0)


def compute_market_tone(oi_totals: dict | None, fii_dii: dict | None, rr_value,
                        nifty_change_pct=None) -> dict:
    """Composite NIFTY market tone — a fixed 7-factor *weighted* verdict.

    Each factor votes bullish (+1) / neutral (0) / bearish (−1); the verdict is
    ``score = Σ(weight × vote) / 100`` ∈ [−1, 1], mapped to a tone label by bands.
    Weights are fixed (see ``_TONE_FACTORS``), not scaled by the realized move.

    Votes:
      • FII Fut (20) — sign of FII index-futures net position (net long → bullish).
      • FII Fut Δ (15) — sign of the day's change in that net (adding longs /
        covering shorts → bullish).
      • OI Flow (25) — today's option writing: more put-OI added than call-OI
        (``put_chg > call_chg``) → support building → bullish; else bearish.
      • PCR ≷1 (10) — PCR above 1 (puts dominate) → bullish, below → bearish.
      • PCR Δ (10) — sign of the day-over-day PCR change (rising → more put
        writing → bullish).
      • Risk Rev (10) — 25Δ RR bands (≥ −1.0 bullish, ≤ −6.0 bearish).
      • FII Cash (7) / DII Cash (3) — sign of net cash flow.

    ``nifty_change_pct`` is accepted for call-site stability but unused (the
    heatmap shades by fixed weight + direction, not realized alignment).

    Returns ``tone`` (BULLISH/BEARISH/NEUTRAL), ``label``, ``score``,
    ``agree_pct`` (% of total weight pointing the verdict's way), ``votes``
    (per-factor), and ``factors`` (grid-ordered meta + vote, for the heatmap)."""
    oi = oi_totals or {}
    fd = fii_dii or {}

    # ── per-factor votes ────────────────────────────────────────────────
    # FII index-futures position + its CHANGE. The change uses a 3-day EMA (falls
    # back to the raw 1-day Δ) so a single expiry-day spike can't swing the verdict.
    fii_fut_vote = _sign(fd.get("fii_fut_net"))
    fut_chg_val = fd.get("fii_fut_chg_ema3")
    if fut_chg_val is None:
        fut_chg_val = fd.get("fii_fut_chg")
    fii_fut_chg_vote = _sign(fut_chg_val)

    # OI writing flow: more put-OI added than call-OI today → bullish support.
    call_chg, put_chg = oi.get("call_chg"), oi.get("put_chg")
    oi_vote = 0
    if call_chg is not None and put_chg is not None:
        oi_vote = _sign(put_chg - call_chg)

    # PCR level (≷ 1) with a ±0.1 deadband — middling PCR (≈1) carries little
    # information, so only a meaningful tilt votes; and its day-over-day change.
    try:
        p = float(oi.get("pcr"))
    except (TypeError, ValueError):
        p = 0.0
    pcr_vote = 1 if p >= 1.1 else (-1 if 0 < p <= 0.9 else 0)
    pcr_chg_vote = _sign(oi.get("pcr_chg"))

    # 25Δ risk reversal — same skew bands as compute_risk_reversal / the RR card.
    try:
        rr = float(rr_value) if rr_value is not None else None
    except (TypeError, ValueError):
        rr = None
    rr_vote = 0 if rr is None else (1 if rr >= -1.0 else (-1 if rr <= -6.0 else 0))

    # Cash flows.
    fii_cash_vote = _sign(fd.get("fii_cash_net"))
    dii_cash_vote = _sign(fd.get("dii_cash_net"))

    votes = {
        "fii_fut": fii_fut_vote, "fii_fut_chg": fii_fut_chg_vote,
        "fii_cash": fii_cash_vote, "dii_cash": dii_cash_vote,
        "oi": oi_vote, "pcr": pcr_vote, "pcr_chg": pcr_chg_vote, "rr": rr_vote,
    }

    # ── weighted verdict ────────────────────────────────────────────────
    score = sum(f["weight"] * votes[f["k"]] for f in _TONE_FACTORS) / 100.0
    if score >= 0.40:
        tone, label = "BULLISH", "Bullish"
    elif score >= 0.12:
        tone, label = "BULLISH", "Mild Bullish"
    elif score > -0.12:
        tone, label = "NEUTRAL", "Neutral / Mixed"
    elif score > -0.40:
        tone, label = "BEARISH", "Mild Bearish"
    else:
        tone, label = "BEARISH", "Bearish"

    # % of total weight (sums to 100) pointing the verdict's way.
    sign = 1 if score > 0 else (-1 if score < 0 else 0)
    agree_pct = (sum(f["weight"] for f in _TONE_FACTORS if votes[f["k"]] == sign)
                 if sign else 0)

    # Heatmap shade depth — a per-factor `intensity` ∈ [0,1] driven purely by how
    # extreme the metric's value is within its realistic range (NOT the weight, so
    # the colour reflects the value). The frontend interpolates light→full by it;
    # `vote == 0` renders grey regardless. Visual only — does not affect the verdict.
    def _clamp01(x):
        return 0.0 if x < 0 else (1.0 if x > 1 else x)

    def _i_sym(v, sat):                       # symmetric magnitude / saturation
        return _clamp01(abs(v) / sat) if (v is not None and sat) else 0.0

    def _i_asym(v, buy_sat, sell_sat):        # asymmetric buy/sell saturations
        if v is None:
            return 0.0
        return _clamp01(v / buy_sat) if v >= 0 else _clamp01(abs(v) / sell_sat)

    # OI flow: how one-sided today's writing is (self-normalising 0..1).
    oi_int = 0.0
    if call_chg is not None and put_chg is not None:
        denom = abs(put_chg) + abs(call_chg)
        oi_int = _clamp01(abs(put_chg - call_chg) / denom) if denom else 0.0
    # PCR level: convex so extremes pop and middling stays faint.
    pcr_int = _clamp01((abs(p - 1.0) / 0.6) ** 1.5) if p else 0.0
    # RR: deepens with skew beyond the band edge (±4 IV-pts ≈ full).
    if rr is None:
        rr_int = 0.0
    elif rr_vote > 0:
        rr_int = _clamp01((rr + 1.0) / 4.0)
    elif rr_vote < 0:
        rr_int = _clamp01((-6.0 - rr) / 4.0)
    else:
        rr_int = 0.0
    intensity_map = {
        "fii_fut":     _i_sym(fd.get("fii_fut_net"), 300000),
        "fii_fut_chg": _i_sym(fut_chg_val, 17500),          # EMA-smoothed Δ
        "fii_cash":    _i_asym(fd.get("fii_cash_net"), 2000, 2500),
        "dii_cash":    _i_asym(fd.get("dii_cash_net"), 6000, 3000),  # softened
        "oi":          oi_int,
        "pcr":         pcr_int,
        "pcr_chg":     _i_sym(oi.get("pcr_chg"), 0.35),
        "rr":          rr_int,
    }
    factors = [{"k": f["k"], "name": f["name"], "weight": f["weight"],
                "vote": votes[f["k"]],
                "intensity": round(intensity_map.get(f["k"], 0.0), 3)}
               for f in _TONE_FACTORS]

    return {
        "tone": tone, "label": label,
        "score": round(score, 3), "agree_pct": agree_pct,
        "votes": votes, "factors": factors,
    }


# ── Premium Ranking ─────────────────────────────────────────────────────────

def premium_ranking(ltp: float, strike: int, opt_type: str) -> str:
    """Rank a premium based on LTP/Strike ratio.

    Formula: ratio = (LTP / Strike) * 100
      < 0.05  → Very Low
      <= 0.08 → Sweet
      <= 1.05 → Slightly Higher
      > 1.05  → High
    Same formula for both CE and PE sides.
    """
    if strike <= 0 or ltp <= 0:
        return "N/A"

    ratio = (ltp / strike) * 100
    if ratio < 0.05:
        return "Very Low"
    if ratio <= 0.08:
        return "Sweet"
    if ratio <= 1.05:
        return "Slightly Higher"
    return "High"


# ── Market Tendency ─────────────────────────────────────────────────────────

def compute_tendency(rr_value: float, vix: float = 0, vix_change_pct: float = 0, rr_zscore: dict = None) -> dict:
    """Compute IV skew tendency from 25-delta Risk Reversal and supporting factors.

    Primary: RR = CE_25d_IV - PE_25d_IV (negative = put skew, normal for Nifty index)

    Nifty has structural put skew — absolute RR < 0 is almost always true.
    When rr_zscore is provided (strategy builder), uses z-score vs history:
      z < -1.5 → Bearish Skew (elevated put demand vs norm)
      z > +1.5 → Bullish Skew (unusual call demand)
    Absolute fallback (no z-score): wider bands to avoid false "Bearish Skew".
      rr < -4 → Bearish Skew, rr > 1 → Bullish Skew, else Flat.
    """
    if rr_zscore and rr_zscore.get("z_score") is not None:
        z = rr_zscore["z_score"]
        if z < -1.5:
            direction = "Bearish Skew"
            base_confidence = min(abs(z) / 3.0, 1.0)
        elif z > 1.5:
            direction = "Bullish Skew"
            base_confidence = min(z / 3.0, 1.0)
        else:
            direction = "Flat"
            base_confidence = 0.0
    elif rr_value < -4:
        direction = "Bearish Skew"
        base_confidence = min(abs(rr_value) / 8, 1.0)
    elif rr_value > 1:
        direction = "Bullish Skew"
        base_confidence = min(abs(rr_value) / 6, 1.0)
    else:
        direction = "Flat"
        base_confidence = 0.0

    support_score = 0
    factors = []

    if vix >= 25:
        support_score -= 0.15
        factors.append("High VIX")
    elif vix >= 20:
        support_score -= 0.08
    elif vix < 14:
        support_score += 0.10
        factors.append("Low VIX")

    if vix_change_pct > 5:
        support_score -= 0.15
        factors.append("VIX surging")
    elif vix_change_pct > 2:
        support_score -= 0.08
    elif vix_change_pct < -3:
        support_score += 0.10
        factors.append("VIX falling")

    if direction == "Bearish Skew":
        confidence = base_confidence + (-support_score if support_score < 0 else support_score * 0.5)
    elif direction == "Bullish Skew":
        confidence = base_confidence + (support_score if support_score > 0 else -support_score * 0.5)
    else:
        confidence = 0

    confidence = max(min(confidence, 1.0), 0.0)

    if confidence >= 0.7:
        conf_label = "strong"
    elif confidence >= 0.4:
        conf_label = "moderate"
    elif confidence > 0:
        conf_label = "mild"
    else:
        conf_label = ""

    label = direction
    if conf_label and direction != "Flat":
        label = f"{direction} ({conf_label})"

    return {
        "tendency": direction,
        "confidence": round(confidence, 2),
        "label": label,
        "rr_value": round(rr_value, 2),
        "factors": factors,
    }


# ── 8-Factor Directional Signal Engine ─────────────────────────────────────
#
# Each factor returns a float in [-1, +1]:
#   +1 = strongly bullish,  -1 = strongly bearish,  0 = neutral.
# A signal fires when ≥5 of 8 factors agree on a direction.

def _rr_score(chain_df) -> float:
    """Factor 1: 25-delta Risk Reversal."""
    _, ce_iv = find_25delta_iv(chain_df, "CE")
    _, pe_iv = find_25delta_iv(chain_df, "PE")
    if ce_iv <= 0 or pe_iv <= 0:
        return 0.0
    rr = ce_iv - pe_iv
    return max(min(rr / 4.0, 1.0), -1.0)


def _pcr_score(chain_df) -> float:
    """Factor 2: Put-Call Ratio by OI.
    High PCR (>1.2) = more puts written as support = bullish.
    Low PCR (<0.8) = more calls written as resistance = bearish."""
    ce_oi = chain_df.loc[chain_df["Type"] == "CE", "OI"].sum()
    pe_oi = chain_df.loc[chain_df["Type"] == "PE", "OI"].sum()
    if ce_oi <= 0:
        return 0.0
    pcr = pe_oi / ce_oi
    if pcr > 1.2:
        return min((pcr - 1.2) / 0.6, 1.0)
    elif pcr < 0.8:
        return max((pcr - 0.8) / 0.6, -1.0)
    return 0.0


def _oi_buildup_score(chain_df, prev_oi: dict | None, spot: float) -> float:
    """Factor 3: OI buildup direction.
    Rising CE OI above spot = resistance building = bearish.
    Rising PE OI below spot = support building = bullish."""
    if prev_oi is None:
        return 0.0

    ce_above = chain_df[(chain_df["Type"] == "CE") & (chain_df["Strike"] > spot)]
    pe_below = chain_df[(chain_df["Type"] == "PE") & (chain_df["Strike"] < spot)]

    ce_oi_now = ce_above["OI"].sum()
    pe_oi_now = pe_below["OI"].sum()
    ce_oi_prev = prev_oi.get("ce_above_oi", ce_oi_now)
    pe_oi_prev = prev_oi.get("pe_below_oi", pe_oi_now)

    ce_delta = ce_oi_now - ce_oi_prev
    pe_delta = pe_oi_now - pe_oi_prev

    net = pe_delta - ce_delta
    denom = max(abs(pe_delta) + abs(ce_delta), 1)
    return max(min(net / denom, 1.0), -1.0)


def _iv_skew_slope_score(chain_df, spot: float) -> float:
    """Factor 4: IV skew slope across strikes.
    OTM puts steeper IV rise than OTM calls = bearish."""
    atm = round(spot / 50) * 50

    otm_ce = chain_df[(chain_df["Type"] == "CE") &
                       (chain_df["Strike"] >= atm + 100) &
                       (chain_df["Strike"] <= atm + 500)]
    otm_pe = chain_df[(chain_df["Type"] == "PE") &
                       (chain_df["Strike"] <= atm - 100) &
                       (chain_df["Strike"] >= atm - 500)]

    ce_avg_iv = otm_ce["IV"].mean() if not otm_ce.empty else 0
    pe_avg_iv = otm_pe["IV"].mean() if not otm_pe.empty else 0

    atm_row = chain_df[(chain_df["Strike"] == atm) & (chain_df["Type"] == "CE")]
    atm_iv = float(atm_row["IV"].iloc[0]) if not atm_row.empty else 0

    if atm_iv <= 0:
        return 0.0

    ce_skew = (ce_avg_iv - atm_iv) / atm_iv
    pe_skew = (pe_avg_iv - atm_iv) / atm_iv

    diff = ce_skew - pe_skew
    return max(min(-diff * 5, 1.0), -1.0)


def _volume_pressure_score(chain_df, spot: float) -> float:
    """Factor 5: Volume pressure — CE vs PE volume near the money."""
    atm = round(spot / 50) * 50
    near = chain_df[(chain_df["Strike"] >= atm - 200) &
                     (chain_df["Strike"] <= atm + 200)]

    ce_vol = near.loc[near["Type"] == "CE", "Volume"].sum()
    pe_vol = near.loc[near["Type"] == "PE", "Volume"].sum()
    total = ce_vol + pe_vol
    if total <= 0:
        return 0.0

    ratio = (ce_vol - pe_vol) / total
    return max(min(ratio * 2, 1.0), -1.0)


def _vix_score(vix_data: dict | None) -> float:
    """Factor 6: VIX regime.
    Low & falling VIX = bullish. High & rising = bearish."""
    if not vix_data:
        return 0.0
    vix = vix_data.get("last", 0)
    chg_pct = vix_data.get("pChange", 0)

    score = 0.0
    if vix < 13:     score += 0.4
    elif vix < 16:   score += 0.2
    elif vix < 20:   score += 0.0
    elif vix < 25:   score -= 0.3
    else:            score -= 0.5

    if chg_pct < -5:    score += 0.5
    elif chg_pct < -2:  score += 0.3
    elif chg_pct > 5:   score -= 0.5
    elif chg_pct > 2:   score -= 0.3

    return max(min(score, 1.0), -1.0)


def compute_max_pain(chain_df) -> float:
    """Compute max-pain strike (where total buyer loss is maximised)."""
    strikes = sorted(chain_df["Strike"].unique())
    if not strikes:
        return 0.0

    ce_map = {}
    pe_map = {}
    for _, row in chain_df.iterrows():
        s = row["Strike"]
        oi = row["OI"]
        if row["Type"] == "CE":
            ce_map[s] = oi
        else:
            pe_map[s] = oi

    best_strike, min_pain = 0, float("inf")
    for settle in strikes:
        pain = 0
        for s in strikes:
            ce_oi = ce_map.get(s, 0)
            pain += max(settle - s, 0) * ce_oi
            pe_oi = pe_map.get(s, 0)
            pain += max(s - settle, 0) * pe_oi
        if pain < min_pain:
            min_pain = pain
            best_strike = settle

    return float(best_strike)


def _max_pain_score(chain_df, spot: float) -> float:
    """Factor 7: Max pain position relative to spot.
    Spot below max pain = gravitational pull upward = bullish."""
    mp = compute_max_pain(chain_df)
    if mp <= 0 or spot <= 0:
        return 0.0
    diff_pct = (mp - spot) / spot * 100
    return max(min(diff_pct / 2.0, 1.0), -1.0)


def _gex_score(chain_df, spot: float) -> float:
    """Factor 8: Gamma Exposure (GEX).
    Net positive gamma near spot = mean-reverting = bullish bias.
    Net negative gamma = trend-amplifying = bearish bias."""
    atm = round(spot / 50) * 50
    near = chain_df[(chain_df["Strike"] >= atm - 300) &
                     (chain_df["Strike"] <= atm + 300)]

    gex = 0.0
    for _, row in near.iterrows():
        g = float(row.get("gamma", 0))
        oi = float(row.get("OI", 0))
        sign = 1 if row["Type"] == "CE" else -1
        gex += sign * g * oi

    if gex > 0:
        return min(0.6, 1.0)
    elif gex < 0:
        return max(-0.4, -1.0)
    return 0.0


def snapshot_oi(chain_df, spot: float) -> dict:
    """Capture current OI snapshot for next-cycle delta comparison."""
    ce_above = chain_df[(chain_df["Type"] == "CE") & (chain_df["Strike"] > spot)]
    pe_below = chain_df[(chain_df["Type"] == "PE") & (chain_df["Strike"] < spot)]
    return {
        "ce_above_oi": int(ce_above["OI"].sum()),
        "pe_below_oi": int(pe_below["OI"].sum()),
    }


def compute_directional_signal(
    chain_df, spot: float, vix_data: dict | None,
    prev_oi: dict | None,
) -> dict:
    """Run all 8 factors and produce a directional signal.

    Returns dict with direction, score, factors_agree, factor_details, max_pain.
    """
    factors = [
        ("RR",      _rr_score(chain_df)),
        ("PCR",     _pcr_score(chain_df)),
        ("OI Bld",  _oi_buildup_score(chain_df, prev_oi, spot)),
        ("IV Skew", _iv_skew_slope_score(chain_df, spot)),
        ("Vol",     _volume_pressure_score(chain_df, spot)),
        ("VIX",     _vix_score(vix_data)),
        ("MaxPain", _max_pain_score(chain_df, spot)),
        ("GEX",     _gex_score(chain_df, spot)),
    ]

    bullish = sum(1 for _, v in factors if v > 0.1)
    bearish = sum(1 for _, v in factors if v < -0.1)
    composite = sum(v for _, v in factors)

    if bullish >= 5:
        direction = "BULLISH"
        agree = bullish
    elif bearish >= 5:
        direction = "BEARISH"
        agree = bearish
    else:
        direction = "NEUTRAL"
        agree = max(bullish, bearish)

    # Conviction: based on how many factors agree on the dominant side
    dominant = max(bullish, bearish)
    if dominant >= 7:
        conviction = "Strong"
    elif dominant >= 5:
        conviction = "Moderate"
    elif dominant >= 3:
        conviction = "Weak"
    else:
        conviction = "Conflicted"

    return {
        "direction": direction,
        "score": round(composite, 2),
        "factors_agree": agree,
        "bullish_count": bullish,
        "bearish_count": bearish,
        "conviction": conviction,
        "factor_details": [(n, round(v, 2)) for n, v in factors],
        "max_pain": compute_max_pain(chain_df),
    }


def select_trade_strike(chain_df, spot: float, direction: str,
                        dte: int) -> dict | None:
    """Pick the best strike for a directional trade suggestion.

    BULLISH = buy CE, BEARISH = buy PE, NEUTRAL = no suggestion.
    """
    if direction == "NEUTRAL":
        return None

    opt_type = "CE" if direction == "BULLISH" else "PE"
    sub = chain_df[(chain_df["Type"] == opt_type) & (chain_df["LTP"] > 0)].copy()
    if sub.empty:
        return None

    sub["_d_dist"] = (sub["delta"].abs() - 0.30).abs()
    sub = sub[(sub["delta"].abs() >= 0.15) & (sub["delta"].abs() <= 0.45)]
    if sub.empty:
        return None

    sub = sub.copy()
    sub["_score"] = (
        (1.0 - sub["_d_dist"] / 0.15) * 40 +
        sub["OI"].rank(pct=True) * 30 +
        (1.0 - sub["LTP"].rank(pct=True)) * 30
    )

    best = sub.loc[sub["_score"].idxmax()]
    strike = int(best["Strike"])
    ltp = float(best["LTP"])
    delta = float(best["delta"])

    if dte <= 2:
        dte_note = "0-2DTE aggressive"
    elif dte <= 5:
        dte_note = "weekly"
    else:
        dte_note = f"{dte}DTE"

    return {
        "strike": strike,
        "type": opt_type,
        "ltp": ltp,
        "delta": delta,
        "rationale": f"BUY {strike}{opt_type} @{ltp:.0f} |D|={abs(delta):.2f} {dte_note}",
    }


# ── Hedge Engine v2: 6-factor scored strike selection ──────────────────────

def compute_hedge_v2(
    side: str,
    sold_strike: int,
    entry_premium: float,
    current_spot: float,
    chain_df,
    dte: int,
    supports: list,
    resistances: list,
    lot_size: int = LOT_SIZE,
) -> dict | None:
    """Intelligent hedge suggestion with 6-factor strike scoring,
    DTE-adaptive weighting, quantity optimisation, and confidence scoring.

    Returns dict with strike, ltp, qty_lots, cost, worst_pnl,
    best_pnl, confidence, rationale. Or None if no viable hedge found.
    """
    opt_type = side  # "CE" or "PE"

    sub = chain_df[chain_df["Type"] == opt_type].copy()
    sub = sub[sub["LTP"] > 0].copy()
    if sub.empty:
        return None

    if side == "CE":
        lo = current_spot - 100
        hi = sold_strike + 300
    else:
        lo = sold_strike - 300
        hi = current_spot + 100

    cands = sub[(sub["Strike"] >= lo) & (sub["Strike"] <= hi)].copy()
    if cands.empty:
        return None

    # 25-delta risk reversal for IV skew alignment
    _, ce_25d_iv = find_25delta_iv(chain_df, "CE")
    _, pe_25d_iv = find_25delta_iv(chain_df, "PE")
    risk_reversal = ce_25d_iv - pe_25d_iv

    sr_strikes = []
    for s in (supports or []):
        sr_strikes.append(s["strike"])
    for r in (resistances or []):
        sr_strikes.append(r["strike"])

    # Stress scenario: 250-pt breach of the short strike (safer side).
    # A deeper shock forces the optimiser to pick more protective hedges and
    # size them for a harsher move than the minimum textbook 200-pt case.
    if side == "CE":
        scenario_spot = sold_strike + 250
    else:
        scenario_spot = sold_strike - 250

    scores = []
    for _, row in cands.iterrows():
        c_strike = int(row["Strike"])
        c_ltp    = float(row["LTP"])
        c_delta  = float(row["delta"])
        c_gamma  = float(row.get("gamma", 0))
        c_oi     = float(row.get("OI", 0))
        c_vol    = float(row.get("Volume", 0))
        c_iv     = float(row.get("IV", 0))

        # Factor 1: Cost efficiency
        if side == "CE":
            protection = max(scenario_spot - c_strike, 0)
        else:
            protection = max(c_strike - scenario_spot, 0)
        cost_per_pt = c_ltp / max(protection, 1) if protection > 0 else 99

        # Factor 2: Delta coverage
        delta_cov = min(abs(c_delta) / 0.50 * 100, 100)

        # Factor 3: Gamma advantage
        gamma_val = c_gamma * lot_size * 50

        # Factor 4: OI / liquidity
        oi_val = c_oi

        # Factor 5: IV skew alignment
        dist_from_atm = c_strike - current_spot
        if side == "CE":
            if risk_reversal > 2:
                skew_score = min(max(dist_from_atm / 50, 0) * 20, 100)
            else:
                skew_score = max(100 - abs(dist_from_atm) / 5, 0)
        else:
            if risk_reversal < -2:
                skew_score = min(max(-dist_from_atm / 50, 0) * 20, 100)
            else:
                skew_score = max(100 - abs(dist_from_atm) / 5, 0)

        # Factor 6: S/R proximity
        if sr_strikes:
            min_sr_dist = min(abs(c_strike - s) for s in sr_strikes)
        else:
            min_sr_dist = 9999
        if min_sr_dist <= 25:     sr_score = 100
        elif min_sr_dist <= 75:   sr_score = 60
        elif min_sr_dist <= 150:  sr_score = 30
        else:                     sr_score = 0

        scores.append({
            "strike": c_strike, "ltp": c_ltp, "delta": c_delta,
            "gamma": c_gamma, "oi": c_oi, "volume": c_vol, "iv": c_iv,
            "cost_per_pt": cost_per_pt, "delta_cov": delta_cov,
            "gamma_impact": gamma_val, "oi_val": oi_val,
            "skew_score": skew_score, "sr_score": sr_score,
            "protection": protection,
        })

    if not scores:
        return None

    sdf = pd.DataFrame(scores)

    def _rank_norm(series, ascending=True):
        if series.nunique() <= 1:
            return pd.Series(50.0, index=series.index)
        ranked = series.rank(ascending=ascending, method="min")
        return (ranked - 1) / (ranked.max() - 1) * 100

    sdf["f_cost"]  = _rank_norm(sdf["cost_per_pt"], ascending=True)
    sdf["f_delta"] = sdf["delta_cov"].clip(0, 100)
    sdf["f_gamma"] = _rank_norm(sdf["gamma_impact"], ascending=False)
    sdf["f_oi"]    = _rank_norm(sdf["oi_val"], ascending=False)
    sdf["f_skew"]  = sdf["skew_score"].clip(0, 100)
    sdf["f_sr"]    = sdf["sr_score"]

    # DTE-adaptive weights — re-tuned to favour protection over cheapness.
    # We want hedges that actually cover the breach, so delta-coverage and
    # gamma weight are raised while raw cost-efficiency is de-emphasised.
    if dte >= 4:
        w = {"cost": 0.22, "delta": 0.22, "gamma": 0.11,
             "oi": 0.18, "skew": 0.15, "sr": 0.12}
    elif dte >= 2:
        w = {"cost": 0.15, "delta": 0.30, "gamma": 0.17,
             "oi": 0.13, "skew": 0.10, "sr": 0.15}
    else:  # 0-1 DTE — gamma-dominant
        w = {"cost": 0.08, "delta": 0.32, "gamma": 0.27,
             "oi": 0.13, "skew": 0.05, "sr": 0.15}

    sdf["composite"] = (
        w["cost"]  * sdf["f_cost"]  +
        w["delta"] * sdf["f_delta"] +
        w["gamma"] * sdf["f_gamma"] +
        w["oi"]    * sdf["f_oi"]    +
        w["skew"]  * sdf["f_skew"]  +
        w["sr"]    * sdf["f_sr"]
    )

    sdf = sdf.sort_values("composite", ascending=False)
    best = sdf.iloc[0]
    h_strike = int(best["strike"])
    h_ltp    = float(best["ltp"])

    if h_ltp <= 0:
        return None

    # Quantity calculation
    sold_qty = lot_size
    if side == "CE":
        short_loss = max(scenario_spot - sold_strike, 0) * sold_qty
        hedge_payoff_per_lot = max(scenario_spot - h_strike, 0) * lot_size
    else:
        short_loss = max(sold_strike - scenario_spot, 0) * sold_qty
        hedge_payoff_per_lot = max(h_strike - scenario_spot, 0) * lot_size

    net_payoff_per_lot = hedge_payoff_per_lot - h_ltp * lot_size
    if net_payoff_per_lot <= 0:
        raw_lots = 1
    else:
        premium_collected = entry_premium * sold_qty
        loss_after_premium = max(short_loss - premium_collected, 0)
        raw_lots = max(math.ceil(loss_after_premium / net_payoff_per_lot), 1)

    # Size a bit heavier on the safer side, with a slightly higher ceiling.
    if dte == 0:     dte_mult = 1.4
    elif dte == 1:   dte_mult = 1.3
    elif dte == 2:   dte_mult = 1.2
    else:            dte_mult = 1.1
    hedge_lots = min(math.ceil(raw_lots * dte_mult), 4)

    cost = round(h_ltp * hedge_lots * lot_size, 2)

    # P&L at 200-point scenario
    if side == "CE":
        short_pnl_worst = (entry_premium - max(scenario_spot - sold_strike, 0)) * sold_qty
        hedge_pnl_worst = (max(scenario_spot - h_strike, 0) - h_ltp) * hedge_lots * lot_size
    else:
        short_pnl_worst = (entry_premium - max(sold_strike - scenario_spot, 0)) * sold_qty
        hedge_pnl_worst = (max(h_strike - scenario_spot, 0) - h_ltp) * hedge_lots * lot_size
    worst_pnl = round(short_pnl_worst + hedge_pnl_worst, 2)

    if dte >= 3:    decay = 0.85
    elif dte >= 1:  decay = 0.90
    else:           decay = 1.00
    best_pnl = round(entry_premium * sold_qty - cost * decay, 2)

    # Confidence scoring (0-100)
    conf = 0

    conf += (float(best["composite"]) / 100) * 35

    max_loss_unhedged = max(short_loss - entry_premium * sold_qty, 1)
    cost_ratio = cost / max_loss_unhedged
    if cost_ratio < 0.05:     conf += 20
    elif cost_ratio < 0.10:   conf += 15
    elif cost_ratio < 0.20:   conf += 10
    elif cost_ratio < 0.30:   conf += 5

    h_oi = float(best["oi"]) or 1
    h_vol = float(best["volume"]) or 0
    vol_oi = h_vol / h_oi
    if vol_oi > 0.5:     conf += 15
    elif vol_oi > 0.2:   conf += 10
    elif vol_oi > 0.05:  conf += 5

    if worst_pnl > 0:         conf += 15
    elif worst_pnl > -cost:   conf += 10

    if dte >= 3:     conf += 15
    elif dte == 2:   conf += 12
    elif dte == 1:   conf += 7
    else:            conf += 3

    confidence = min(round(conf), 100)

    # Rationale
    dist_from_atm = h_strike - round(current_spot / 50) * 50
    if abs(dist_from_atm) <= 50:
        strike_desc = "ATM"
    elif (side == "CE" and dist_from_atm > 0) or (side == "PE" and dist_from_atm < 0):
        strike_desc = f"OTM {abs(dist_from_atm)}pts"
    else:
        strike_desc = f"ITM {abs(dist_from_atm)}pts"

    parts = [f"{strike_desc} @{h_ltp:.1f}"]
    if dte <= 1:
        parts.append("gamma-weighted")
    if float(best["sr_score"]) >= 60:
        parts.append("near S/R")
    if abs(risk_reversal) > 2:
        parts.append(f"RR={risk_reversal:.1f}")

    rationale = " | ".join(parts)

    return {
        "strike": h_strike,
        "ltp": h_ltp,
        "qty_lots": hedge_lots,
        "cost": cost,
        "worst_pnl": worst_pnl,
        "best_pnl": best_pnl,
        "confidence": confidence,
        "rationale": rationale,
    }


# ── Margin: scenario-based SPAN + Exposure ──────────────────────────────────
#
# Implements the standard NSE SPAN methodology rather than the old flat-percent
# proxies.  SPAN is the worst-case loss of the *whole portfolio* revalued across
# a grid of price + volatility scenarios (the exchange's "risk array"); Exposure
# is the exchange's flat % of notional buffer that sits on top.  This is exactly
# the reference formula:
#
#   TOTAL_MARGIN = SPAN + EXPOSURE
#   SPAN         = max over scenarios of  −Σ(qty × Δvalueₛcₑₙₐᵣᵢₒ × multiplier)
#   EXPOSURE     = exposure_rate × short notional
#
# Because every leg is repriced with Black-76, hedges/spreads/strangles net out
# automatically: a long leg's gain in a scenario offsets the short leg's loss in
# that same scenario, so a defined-risk spread is charged far less than a naked
# short — without any per-strategy special-casing.  The two extreme ±2×PSR
# scenarios (counted at 35%) embed the extreme-loss component inside SPAN.

# Tunable SPAN parameters (approximate NSE NIFTY settings; centralised here so
# they can be adjusted in one place as the exchange revises them).
SPAN_PRICE_SCAN_PCT  = 0.06   # price scan range as a fraction of the forward
SPAN_VOL_SCAN_PCT    = 0.10   # ± relative shift applied to each leg's IV
SPAN_EXTREME_PCT     = 2.0    # extreme move = ±this × price-scan-range
SPAN_EXTREME_WEIGHT  = 0.35   # extreme scenarios counted at 35% of their loss
EXPOSURE_MARGIN_RATE = 0.02   # 2% of short notional (NSE index exposure margin)


def black76_price(F, K, T, r, sigma, option_type="CE") -> float:
    """Black-76 (forward-based) European option price.  Falls back to intrinsic
    value when there is no time/vol left."""
    if F <= 0 or K <= 0:
        return 0.0
    if T <= 0 or sigma <= 0:
        return max(F - K, 0.0) if option_type == "CE" else max(K - F, 0.0)
    sqrtT = np.sqrt(T)
    d1 = (np.log(F / K) + 0.5 * sigma ** 2 * T) / (sigma * sqrtT)
    d2 = d1 - sigma * sqrtT
    disc = np.exp(-r * T)
    if option_type == "CE":
        return float(disc * (F * norm.cdf(d1) - K * norm.cdf(d2)))
    return float(disc * (K * norm.cdf(-d2) - F * norm.cdf(-d1)))


def forward_from_chain(chain_df, spot, T=0.0, r=RISK_FREE_RATE) -> float:
    """Synthetic forward (futures) price derived from put-call parity on the
    near-ATM strikes of a *processed* chain (Strike/Type/LTP columns)."""
    try:
        strikes = sorted(chain_df["Strike"].unique(), key=lambda k: abs(k - spot))[:3]
        pairs = []
        for k in strikes:
            ce = chain_df[(chain_df["Strike"] == k) & (chain_df["Type"] == "CE")]
            pe = chain_df[(chain_df["Strike"] == k) & (chain_df["Type"] == "PE")]
            ce_ltp = float(ce["LTP"].iloc[0]) if not ce.empty else None
            pe_ltp = float(pe["LTP"].iloc[0]) if not pe.empty else None
            pairs.append((k, ce_ltp, pe_ltp))
        return synthetic_forward(spot, pairs, T, r)
    except Exception:
        return synthetic_forward(spot, [], T, r)


def compute_span_margin(
    legs, forward, T, r=RISK_FREE_RATE, lot_size=LOT_SIZE,
    price_scan_pct=SPAN_PRICE_SCAN_PCT, vol_scan_pct=SPAN_VOL_SCAN_PCT,
    extreme_mult=SPAN_EXTREME_PCT, extreme_weight=SPAN_EXTREME_WEIGHT,
) -> tuple[float, dict]:
    """Scenario-based SPAN margin for an options portfolio.

    ``legs`` — list of dicts: {strike, option_type 'CE'/'PE', action 'BUY'/'SELL',
    lots, iv (as a fraction, e.g. 0.14)}.

    Returns (span_margin, worst_scenario_info).
    """
    if forward <= 0 or not legs:
        return 0.0, {}

    psr = forward * price_scan_pct

    # The 16-scenario SPAN risk array: 7 price moves × {vol up, vol down}, plus
    # two extreme moves weighted at `extreme_weight`.
    fractions = [0.0, 1 / 3, -1 / 3, 2 / 3, -2 / 3, 1.0, -1.0]
    scenarios = []
    for f in fractions:
        scenarios.append((f, +vol_scan_pct, 1.0))
        scenarios.append((f, -vol_scan_pct, 1.0))
    scenarios.append((+extreme_mult, 0.0, extreme_weight))
    scenarios.append((-extreme_mult, 0.0, extreme_weight))

    # Base (current) theoretical value of each leg.
    base_vals = [
        black76_price(forward, leg["strike"], T, r, max(leg.get("iv", 0.0), 0.0),
                      leg["option_type"])
        for leg in legs
    ]

    worst_loss = 0.0
    worst_info = {}
    for frac, dvol, weight in scenarios:
        f_scen = max(forward + frac * psr, 0.01)
        pnl = 0.0
        for i, leg in enumerate(legs):
            sigma = max(leg.get("iv", 0.0) * (1 + dvol), 0.0)
            v = black76_price(f_scen, leg["strike"], T, r, sigma, leg["option_type"])
            sign = 1.0 if leg["action"] == "BUY" else -1.0
            qty = leg["lots"] * lot_size
            pnl += sign * (v - base_vals[i]) * qty
        loss = max(-pnl, 0.0) * weight
        if loss > worst_loss:
            worst_loss = loss
            worst_info = {
                "price_move_pct": round(frac * price_scan_pct * 100, 2),
                "vol_shift_pct": round(dvol * 100, 1),
                "scenario_forward": round(f_scen, 2),
            }
    return round(worst_loss, 2), worst_info


# ── Alternative hedge: protective short in the NEXT expiry ───────────────────

def compute_protective_short_hedge(
    side, sold_strike, current_spot, next_chain_df,
    qty_lots=1, max_delta=0.10, lot_size=LOT_SIZE,
):
    """Suggest a further-OTM short in the *next* expiry as an alternative hedge.

    For a risky short (e.g. a 23000 PE going in-the-money), this finds a strike
    further out-of-the-money in the next expiry — lower for PE, higher for CE —
    that has a low delta (|Δ| < ``max_delta``, default 0.10) and good open
    interest.  Selling it collects additional premium that cushions the existing
    position while staying far from the money.

    Returns a dict (strike, ltp, delta, oi, iv, qty_lots, premium_collected,
    rationale) or None when no suitable strike exists.
    """
    if next_chain_df is None or next_chain_df.empty:
        return None

    sub = next_chain_df[(next_chain_df["Type"] == side) & (next_chain_df["LTP"] > 0)].copy()
    if sub.empty:
        return None

    # Only strikes further OTM than the sold strike, in the safe direction.
    if side == "PE":
        sub = sub[sub["Strike"] < sold_strike]
    else:
        sub = sub[sub["Strike"] > sold_strike]
    if sub.empty:
        return None

    sub["_absdelta"] = sub["delta"].abs()

    # Primary filter: delta under the cap.  Relax once if nothing qualifies.
    qual = sub[sub["_absdelta"] <= max_delta]
    relaxed = False
    if qual.empty:
        qual = sub[sub["_absdelta"] <= max_delta + 0.05]
        relaxed = True
    if qual.empty:
        return None

    # Require decent liquidity — OI at or above the median of the qualifying set.
    oi_floor = qual["OI"].median()
    liquid = qual[qual["OI"] >= oi_floor]
    if liquid.empty:
        liquid = qual

    # Among the low-delta, liquid, OTM candidates, pick the one closest to the
    # money (highest premium → most cushion) while still under the delta cap.
    best = liquid.loc[liquid["LTP"].idxmax()]
    strike = int(best["Strike"])
    ltp = float(best["LTP"])
    delta = float(best["delta"])
    oi = int(best["OI"])
    iv = float(best["IV"])

    premium_collected = round(ltp * qty_lots * lot_size, 2)
    dist = abs(strike - sold_strike)

    rationale = (
        f"Sell {strike}{side} (next expiry) | Δ={abs(delta):.2f}, "
        f"OI={oi:,}, {dist}pt further OTM — collect ₹{premium_collected:,.0f} cushion"
    )
    if relaxed:
        rationale += " (no strike under 0.10Δ — relaxed)"

    return {
        "strike": strike,
        "ltp": round(ltp, 2),
        "delta": round(delta, 4),
        "oi": oi,
        "iv": round(iv, 2),
        "qty_lots": qty_lots,
        "premium_collected": premium_collected,
        "rationale": rationale,
    }
