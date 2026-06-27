"""Stock-options screener — layered filter that picks tradable F&O stocks.

Pipeline (serial, layered — cheap filters first):
  Step 1  Multi-timeframe RSI (Wilder):
            (40<dRSI<50 & wRSI>60)  OR  (40<hRSI<50 & dRSI>60)
  Step 2  DeMark TD **perfected** buy-setup, checked on daily / weekly / hourly:
            a fresh "9" on ANY timeframe (the priority signal) -> candidate pool.
            (matches the user's TradingView buyPerfected logic)
  Step 3  On pooled names, with user-adjustable thresholds:
            i.   forward(synthetic-futures) vs spot proximity (growth potential)
            ii.  IVP within [ivp_min, ivp_max]   (provisional HV-proxy until history builds)
            iii. strike with delta_min < |Δ| < delta_max, highest OI, on the chosen side
            iv.  premium yield = (prem/(strike-prem))*(365/DTE)*100 >= min_yield

Data: NSE option chains via market_data.build_chain (equity branch) + yfinance OHLC
for RSI/DeMark/HV.  Every fetch is sanity-checked; bad data is skipped, not faked.

RSI uses the standard Wilder definition (RS = avg gain / avg loss,
RSI = 100 - 100/(1+RS)).
"""

import math
import threading
import time as _time
import numpy as np
import pandas as pd
from datetime import date, datetime, timedelta

from database import SessionLocal
from models import StockIVHistory, StockConfirmedLog
from market_data import (
    build_chain, forward_from_chain, time_to_expiry, days_to_expiry,
    get_lot_size, _memo, _market_is_open,
)
from pnsea import NSE


# ── Tunable defaults (Step 3 sliders) ───────────────────────────────────────
DEFAULT_THRESHOLDS = {
    "ivp_min": 45.0,
    "ivp_max": 70.0,
    "min_yield": 12.0,     # % annualised premium yield
    "delta_min": 0.10,
    "delta_max": 0.25,
}

# Fallback label for the DeMark timeframe badge when no specific firing
# timeframe is available (the screener now reports the actual fired TFs, e.g.
# "D, W", from multi_timeframe_demark).
DEMARK_TIMEFRAME = "Daily"

_OHLC_TTL_DAILY = 3600.0     # daily/weekly bars change slowly
_OHLC_TTL_HOURLY = 1800.0    # ~30 min
_UNIVERSE_TTL = 6 * 3600.0
_IVP_MIN_HISTORY = 40        # records needed before IVP is "real" (else HV proxy)

# Confirmed-suggestion lifecycle: a confirmation episode stays in the live
# "Confirmed" tab while it is < 2 days old (day 1 + day 2); from day 3 it
# graduates to the "Track Record" log. A gap of > 2 days without re-confirmation
# ends an episode (a later re-confirmation starts a fresh one).
GRADUATE_AFTER_DAYS = 2
EPISODE_GAP_DAYS = 2
_HISTORY_TTL = 45.0          # cache the live re-evaluation of the log briefly

# Background prefilter state — the DeMark-9 candidate pool (RSI-screened names
# that print a fresh TD buy-setup 9 on any timeframe), refreshed by scheduler.
# Confirmed vs radar is decided per-request in get_screen_results once the user's
# Step-3 thresholds are applied.
_SCREEN_STATE = {"pool": [], "ts": 0.0, "universe": 0, "running": False}


# ── Indicators (pure, testable) ─────────────────────────────────────────────

def compute_rsi(closes, period: int = 14):
    """Standard Wilder RSI of a close series. Returns the latest value or None."""
    closes = [float(c) for c in closes if c is not None and not pd.isna(c)]
    if len(closes) < period + 1:
        return None
    deltas = np.diff(closes)
    gains = np.where(deltas > 0, deltas, 0.0)
    losses = np.where(deltas < 0, -deltas, 0.0)
    avg_gain = gains[:period].mean()
    avg_loss = losses[:period].mean()
    for i in range(period, len(deltas)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return round(100.0 - 100.0 / (1.0 + rs), 2)


def compute_td_buy_setup(closes) -> int:
    """TD Sequential buy-setup count = trailing run of bars whose close is below
    the close 4 bars earlier (bearish/exhaustion sequence that completes at 9).
    Returns the current consecutive count."""
    closes = [float(c) for c in closes if c is not None and not pd.isna(c)]
    n = len(closes)
    if n < 5:
        return 0
    count = 0
    i = n - 1
    while i >= 4 and closes[i] < closes[i - 4]:
        count += 1
        i -= 1
        if count >= 9:
            break
    return count


def compute_td_buy_setup_perfected(closes, lows=None) -> tuple[int, bool, bool]:
    """TD buy-setup on the CURRENT bar, matching the TradingView Pine the user runs:

        TS := close < close[4] ? TS[1] + 1 : 0             # buy-setup down-count
        buyPerfected = (TS == 9) and
            (min(low, low[1]) <= low[2] and min(low, low[1]) <= low[3])

    (Pine's ``TDDn = TS - valuewhen(TS<TS[1], TS, 1)`` reduces to ``TS`` because TS
    is 0 at every reset bar, so the buy-setup count is just the trailing run of
    closes below close[4] and a run of 9 is exactly buyPerfected's trigger.)

    A "9" is only authentic while it sits on the most-recent bar: the bullish
    reversal it calls plays out within the next bars and the count resets to 0, so
    a 9 that printed days ago is NOT a live setup (the move is already gone). We
    therefore fire strictly on the current bar — exactly what TradingView shows now.

    Returns ``(count, fired, perfected)``:
      • count     — current trailing run of closes below close[4] (capped 9; drives
                    the 7-8 radar),
      • fired     — count is exactly 9 on the current bar (a fresh, live "9"),
      • perfected — fired AND min(low_9, low_8) ≤ low_7 and ≤ low_6.

    ``closes``/``lows`` are row-aligned (same bars, newest last)."""
    if closes is None:
        return 0, False, False
    try:
        cl = [float(c) for c in closes]
    except (TypeError, ValueError):
        return 0, False, False
    n = len(cl)
    if n < 5:
        return 0, False, False

    raw, i = 0, n - 1
    while i >= 4 and cl[i] < cl[i - 4]:
        raw += 1
        i -= 1
    count = min(raw, 9)
    fired = (raw == 9)
    perfected = False
    if fired and lows is not None and len(lows) == n:
        try:
            low9, low8 = float(lows[n - 1]), float(lows[n - 2])
            low7, low6 = float(lows[n - 3]), float(lows[n - 4])
            m = min(low9, low8)
            perfected = (m <= low7) and (m <= low6)
        except (TypeError, ValueError):
            perfected = False
    return count, fired, perfected


def compute_td_sell_setup_perfected(closes, highs=None) -> tuple[int, bool, bool]:
    """TD SELL-setup on the CURRENT bar — the exact mirror of the buy setup (Pine
    ``sellPerfected``):

        TS := close > close[4] ? TS[1] + 1 : 0             # sell-setup up-count
        sellPerfected = (TS == 9) and
            (max(high, high[1]) >= high[2] and max(high, high[1]) >= high[3])

    A completed sell setup is a BEARISH reversal (buying exhausted, a pullback is
    likely). Same live-candle semantics as the buy setup.

    Returns ``(count, fired, perfected)``:
      • count     — current trailing run of closes ABOVE close[4] (capped 9; radar),
      • fired     — count is exactly 9 on the current bar,
      • perfected — fired AND max(high_9, high_8) ≥ high_7 and ≥ high_6.

    ``closes``/``highs`` are row-aligned (same bars, newest last)."""
    if closes is None:
        return 0, False, False
    try:
        cl = [float(c) for c in closes]
    except (TypeError, ValueError):
        return 0, False, False
    n = len(cl)
    if n < 5:
        return 0, False, False

    raw, i = 0, n - 1
    while i >= 4 and cl[i] > cl[i - 4]:
        raw += 1
        i -= 1
    count = min(raw, 9)
    fired = (raw == 9)
    perfected = False
    if fired and highs is not None and len(highs) == n:
        try:
            high9, high8 = float(highs[n - 1]), float(highs[n - 2])
            high7, high6 = float(highs[n - 3]), float(highs[n - 4])
            m = max(high9, high8)
            perfected = (m >= high7) and (m >= high6)
        except (TypeError, ValueError):
            perfected = False
    return count, fired, perfected


def verify_td_count(closes, count: int) -> bool:
    """Confirm a daily TD buy-setup is trustworthy — kept deliberately LENIENT so
    genuine setups are never bottlenecked. It rejects only the clearly-broken case
    that produced wrong readings (a "2" showing as an "8"): an unadjusted split /
    bonus inside the run, where a single-day plunge far beyond NSE's price bands
    (≥ ~45%) spuriously inflates the count. Normal volatility (≤ ~20%), gaps and
    ordinary data noise all pass; only an outright corporate-action jump fails.
    Also needs a minimum of clean history to count at all."""
    cl = [float(c) for c in closes if c is not None and not pd.isna(c) and float(c) > 0]
    if len(cl) < 20:
        return False
    window = cl[-(count + 4):] if count else cl[-9:]
    for a, b in zip(window, window[1:]):
        if a > 0 and b / a < 0.55:        # ≥45% single-day drop ⇒ split/bonus, not a real run
            return False
    return True


def historical_volatility(closes, window: int = 20):
    """Annualised historical volatility (%) over the last `window` returns."""
    closes = [float(c) for c in closes if c is not None and not pd.isna(c)]
    if len(closes) < window + 1:
        return None
    rets = np.diff(np.log(closes))[-window:]
    if len(rets) < 2:
        return None
    return round(float(np.std(rets, ddof=1) * math.sqrt(252) * 100), 2)


def hv_percentile(closes, window: int = 20):
    """Percentile rank (0-100) of the latest rolling HV within its own history."""
    closes = [float(c) for c in closes if c is not None and not pd.isna(c)]
    if len(closes) < window + 5:
        return None
    logret = np.diff(np.log(closes))
    s = pd.Series(logret).rolling(window).std(ddof=1) * math.sqrt(252) * 100
    s = s.dropna()
    if len(s) < 5:
        return None
    cur = s.iloc[-1]
    return round(float((s < cur).mean() * 100), 1)


def _anchored_vwap(highs, lows, closes, volumes, anchor_idx) -> float | None:
    """Volume-weighted average of typical price ((H+L+C)/3) accumulated from
    ``anchor_idx`` to the latest bar. Returns None when volume is missing/all-zero
    (VWAP is undefined without traded volume) or the inputs aren't row-aligned."""
    try:
        n = len(closes)
        if not (len(highs) == len(lows) == len(volumes) == n):
            return None
        i0 = max(0, min(int(anchor_idx), n - 1))
        num = den = 0.0
        for i in range(i0, n):
            h, l, c, v = float(highs[i]), float(lows[i]), float(closes[i]), float(volumes[i])
            if v <= 0 or h <= 0 or l <= 0 or c <= 0:
                continue
            tp = (h + l + c) / 3.0
            num += tp * v
            den += v
        if den <= 0:
            return None
        return round(num / den, 2)
    except (TypeError, ValueError):
        return None


def _setup_anchor_idx(closes, side: str) -> int:
    """Start index of the active DeMark setup run on ``closes`` — the first bar of
    the trailing run. For a PE/buy pick this is the buy-setup down-count; for a
    CE/sell pick the sell-setup up-count. Anchor = n - count (clamped ≥ 0)."""
    n = len(closes or [])
    if n == 0:
        return 0
    if side == "CE":
        count, _, _ = compute_td_sell_setup_perfected(closes)
    else:
        count, _, _ = compute_td_buy_setup_perfected(closes)
    return max(0, n - count) if count else max(0, n - 1)


# ── yfinance OHLC (batched, cached, sanity-checked) ─────────────────────────

def _yf_ticker(symbol: str) -> str:
    return f"{symbol.upper()}.NS"


# Batch-warmed bars per (interval, period) -> {symbol: (expires_monotonic, bars)}.
# The prefilter fills these in ONE multi-ticker download per timeframe so the scan
# doesn't make ~190 sequential calls (the main cold-start slowdown).
_BARS_PREFETCH: dict = {}


def _prefetch_bars(symbols, interval: str, period: str) -> int:
    """Download (interval, period) bars for the whole universe in a single
    multi-ticker request and warm the prefetch cache, so per-symbol reads are
    cache hits. Best-effort: anything the batch misses falls back to per-symbol."""
    import yfinance as yf
    tickers = sorted({_yf_ticker(s) for s in symbols})
    if not tickers:
        return 0
    try:
        df = yf.download(tickers, period=period, interval=interval, group_by="ticker",
                         progress=False, auto_adjust=False, threads=True, timeout=30)
    except Exception as e:
        print(f"  [Screener] batch prefetch failed {interval}/{period}: {e}")
        return 0
    if df is None or getattr(df, "empty", True):
        return 0
    lvl0 = set(df.columns.get_level_values(0)) if hasattr(df.columns, "levels") else set()
    store = _BARS_PREFETCH.setdefault(f"{interval}:{period}", {})
    ttl = _OHLC_TTL_HOURLY if interval == "1h" else _OHLC_TTL_DAILY
    exp = _time.monotonic() + ttl
    n = 0
    for s in symbols:
        t = _yf_ticker(s)
        if t not in lvl0:
            continue
        try:
            sub = df[t].dropna(subset=["Close"])
            closes = sub["Close"].tolist()
            lows = sub["Low"].tolist() if "Low" in sub.columns else []
            highs = sub["High"].tolist() if "High" in sub.columns else []
            volumes = sub["Volume"].tolist() if "Volume" in sub.columns else []
            if len(closes) >= 20 and not any(c <= 0 for c in closes[-20:]):
                store[s] = (exp, {"closes": closes, "lows": lows, "highs": highs,
                                  "volumes": volumes})
                n += 1
        except Exception:
            continue
    print(f"[Screener] batch prefetch {interval}/{period}: warmed {n}/{len(symbols)} symbols")
    return n


def _fetch_bars(symbol: str, interval: str, period: str):
    """OHLC bars for one symbol/timeframe, cached. Returns
    ``{"closes": [...], "lows": [...]}`` (row-aligned, newest last) or None on
    bad/empty data. Shared by both the RSI and DeMark reads so the download
    happens once per (symbol, timeframe)."""
    ttl = _OHLC_TTL_HOURLY if interval == "1h" else _OHLC_TTL_DAILY

    # Serve the batch-prefetched series (the common case during the scan).
    store = _BARS_PREFETCH.get(f"{interval}:{period}")
    if store:
        hit = store.get(symbol)
        if hit and hit[0] > _time.monotonic():
            return hit[1]

    def _producer():
        import yfinance as yf
        # The full-universe scan hammers yfinance, so a single fetch intermittently
        # returns empty/raises (rate-limit / network). Retry a couple of times with
        # a short back-off so a transient miss doesn't silently drop a real setup
        # (e.g. OIL with a daily DeMark 8). A *present-but-insufficient* series is
        # genuine (new listing) and isn't retried.
        last_err = None
        for attempt in range(3):
            try:
                # auto_adjust=False → raw traded prices, matching NSE / TradingView /
                # Chartink so the displayed RSI/HV/DeMark line up with those sources.
                df = yf.download(_yf_ticker(symbol), period=period, interval=interval,
                                 progress=False, auto_adjust=False, timeout=15)
            except Exception as e:
                last_err = e
                df = None
            if df is not None and not df.empty:
                if hasattr(df.columns, "levels"):
                    df.columns = df.columns.get_level_values(0)
                df = df.dropna(subset=["Close"])
                closes = df["Close"].tolist()
                lows = df["Low"].tolist() if "Low" in df.columns else []
                highs = df["High"].tolist() if "High" in df.columns else []
                volumes = df["Volume"].tolist() if "Volume" in df.columns else []
                # Sanity: enough bars and positive prices
                if len(closes) < 20 or any(c <= 0 for c in closes[-20:]):
                    return None
                return {"closes": closes, "lows": lows, "highs": highs,
                        "volumes": volumes}
            if attempt < 2:
                _time.sleep(0.6)   # transient empty/failure — back off and retry
        if last_err:
            print(f"  [Screener] OHLC fetch failed {symbol} {interval} after retries: {last_err}")
        return None

    return _memo(f"bars:{symbol}:{interval}:{period}", ttl, _producer)


def _fetch_ohlc(symbol: str, interval: str, period: str):
    """Closes (list) for one symbol/timeframe, cached. None on bad/empty data."""
    b = _fetch_bars(symbol, interval, period)
    return b["closes"] if b else None


def _fetch_intraday_5m(symbol: str):
    """5-minute OHLCV bars for one symbol over ~60 days, cached, with each bar's
    calendar date retained for setup-date slicing. Returns
    ``{"highs","lows","closes","volumes","dates"}`` (row-aligned, newest last) or
    None on bad/empty data. 5m is the finest interval with a lookback long enough
    to cover a DeMark setup window (yfinance keeps ~60d of 5m; 1m only ~7d)."""
    def _producer():
        import yfinance as yf
        try:
            df = yf.download(_yf_ticker(symbol), period="60d", interval="5m",
                             progress=False, auto_adjust=False, timeout=20)
        except Exception as e:
            print(f"  [Screener] 5m fetch failed {symbol}: {e}")
            return None
        if df is None or df.empty:
            return None
        if hasattr(df.columns, "levels"):
            df.columns = df.columns.get_level_values(0)
        df = df.dropna(subset=["Close"])
        if df.empty or "Volume" not in df.columns:
            return None
        return {
            "highs": df["High"].tolist(),
            "lows": df["Low"].tolist(),
            "closes": df["Close"].tolist(),
            "volumes": df["Volume"].tolist(),
            "dates": [ts.date() for ts in df.index],
        }
    return _memo(f"bars5m:{symbol}", _OHLC_TTL_HOURLY, _producer)


def anchored_vwap_for_symbol(symbol: str, side: str) -> float | None:
    """Anchored VWAP for a symbol's active DeMark setup. Primary path anchors on
    5m bars from the setup-start *date* (precise); falls back to a daily-bar
    anchored VWAP when 5m is unavailable or doesn't reach the setup window."""
    def _producer():
        daily = _fetch_bars(symbol, "1d", "6mo")
        if not daily or not daily.get("closes"):
            return None
        dcloses = daily["closes"]
        anchor_d = _setup_anchor_idx(dcloses, side)
        setup_len = len(dcloses) - anchor_d        # setup length in trading days

        # Primary: 5m, sliced to the last `setup_len` distinct trading dates.
        five = _fetch_intraday_5m(symbol)
        if five and five.get("dates") and setup_len > 0:
            dates = five["dates"]
            uniq = sorted(set(dates))
            if len(uniq) >= setup_len:
                start_date = uniq[-setup_len]
                # First 5m bar on/after the setup-start date.
                i0 = next((i for i, d in enumerate(dates) if d >= start_date), None)
                if i0 is not None:
                    v = _anchored_vwap(five["highs"], five["lows"], five["closes"],
                                       five["volumes"], i0)
                    if v is not None:
                        return v

        # Fallback: daily-bar anchored VWAP from the setup start.
        return _anchored_vwap(daily.get("highs", []), daily.get("lows", []),
                              dcloses, daily.get("volumes", []), anchor_d)

    return _memo(f"avwap:{symbol}:{side}", _OHLC_TTL_HOURLY, _producer,
                 cache_falsy=False)


# DeMark is computed on DAILY primarily; WEEKLY is a small fallback (top-3) when
# the daily radar+picks are sparse (see run_prefilter). Hourly is intentionally
# excluded — hourly setups churn too fast to be reliable buy-setup signals.
_DEMARK_TFS = {"daily": ("1d", "6mo"), "weekly": ("1wk", "2y")}
_TF_ABBR = {"daily": "D", "weekly": "W"}


def _td_for_tf(symbol: str, tf: str) -> dict:
    """Active TD setup ({count, fired, perfected, dir}) for one timeframe on the
    LATEST candle (the current bar, matching what TradingView shows live).

    Both directions are checked and the ACTIVE one is returned (they're mutually
    exclusive — the latest bar's close is either above or below close[4]):
      • ``dir='buy'``  — bullish reversal (down-count) → expressed as a PE sell,
      • ``dir='sell'`` — bearish reversal (up-count)   → expressed as a CE sell.

    No verification gate (0 only when the data shows no setup)."""
    b = _fetch_bars(symbol, *_DEMARK_TFS[tf])
    if not b:
        return {"count": 0, "fired": False, "perfected": False, "dir": "buy"}
    buy = compute_td_buy_setup_perfected(b["closes"], b.get("lows"))
    sell = compute_td_sell_setup_perfected(b["closes"], b.get("highs"))
    # Whichever run is live (one is 0 when the other is positive); buy wins a 0-0 tie
    # (no setup either way — count 0, won't pool).
    cnt, fired, perf, direction = (
        (*buy, "buy") if buy[0] >= sell[0] else (*sell, "sell"))
    return {"count": cnt, "fired": fired, "perfected": perf, "dir": direction}


def _demark_dict(td: dict, tf: str) -> dict:
    """Wrap a single-timeframe TD result in the multi-TF shape downstream expects
    (fired_tfs / approaching_tfs / max_count / has9 / has78), tagged with ``tf``.
    ``dir`` (buy/sell) flows through so routing can pick the sell side."""
    out = {"fired_tfs": [], "perfected_tfs": [], "approaching_tfs": [],
           "max_count": td["count"], "has9": td["fired"],
           "has78": td["count"] in (7, 8), "verified": True,
           "dir": td.get("dir", "buy"),
           tf: {"count": td["count"], "fired": td["fired"], "perfected": td["perfected"]}}
    if td["fired"]:
        out["fired_tfs"].append(tf)
    elif td["count"] in (7, 8):
        out["approaching_tfs"].append(tf)
    if td["perfected"]:
        out["perfected_tfs"].append(tf)
    return out


def multi_timeframe_demark(symbol: str) -> dict:
    """DAILY perfected TD buy-setup for one symbol — the direct calculation (no
    verification gate). Used by the Track-Record re-evaluation, which is daily."""
    return _demark_dict(_td_for_tf(symbol, "daily"), "daily")


def _daily_low_close(symbol: str) -> tuple[float | None, float | None]:
    """Latest daily (low, close) for a symbol, from the shared bars cache."""
    b = _fetch_bars(symbol, "1d", "6mo")
    if not b:
        return None, None
    lows, closes = b.get("lows") or [], b.get("closes") or []
    low = float(lows[-1]) if lows else None
    close = float(closes[-1]) if closes else None
    return low, close


def _setup_stop(symbol: str, side: str | None) -> tuple[float | None, float | None]:
    """(stop_level, close): the level whose breach invalidates the setup — the
    setup-candle LOW for a bullish PE (buy) pick, the setup-candle HIGH for a
    bearish CE (sell) pick — paired with the latest daily close."""
    b = _fetch_bars(symbol, "1d", "6mo")
    if not b:
        return None, None
    closes = b.get("closes") or []
    close = float(closes[-1]) if closes else None
    if side == "CE":
        highs = b.get("highs") or []
        return (float(highs[-1]) if highs else None), close
    lows = b.get("lows") or []
    return (float(lows[-1]) if lows else None), close


def _stop_broken(side: str | None, stop: float | None, close: float | None) -> bool:
    """A bullish PE setup breaks when price closes BELOW its low; a bearish CE setup
    breaks when price closes ABOVE its high."""
    if stop is None or close is None:
        return False
    return close > stop if side == "CE" else close < stop


def multi_timeframe_rsi(symbol: str) -> dict:
    """Daily/weekly/hourly RSI + a daily SMA trend read for one symbol."""
    daily = _fetch_ohlc(symbol, "1d", "6mo")
    out = {"daily": None, "weekly": None, "hourly": None,
           "sma_fast": None, "sma_slow": None, "daily_closes": daily}
    if daily:
        out["daily"] = compute_rsi(daily)
        if len(daily) >= 50:
            out["sma_fast"] = float(np.mean(daily[-20:]))
            out["sma_slow"] = float(np.mean(daily[-50:]))
    weekly = _fetch_ohlc(symbol, "1wk", "2y")
    if weekly:
        out["weekly"] = compute_rsi(weekly)
    hourly = _fetch_ohlc(symbol, "1h", "3mo")
    if hourly:
        out["hourly"] = compute_rsi(hourly)
    return out


# ── Universe ────────────────────────────────────────────────────────────────

def fno_universe() -> list:
    """List of F&O stock symbols (cached). Excludes index symbols."""
    def _producer():
        try:
            syms = NSE().equityOptions.fno_stocks_list()
            return [str(s).upper() for s in syms if isinstance(s, str)]
        except Exception as e:
            print(f"  [Screener] F&O universe fetch failed: {e}")
            return []
    return _memo("fno_universe", _UNIVERSE_TTL, _producer) or []


# ── Most-active stock options (live, ranked by traded volume) ───────────────
#
# "Most traded" = highest **traded volume in shares** (Volume_contracts × lot),
# the exact metric the NSE/Sensibull "most active by volume" lists use. The
# volume leaders are dominated by high-lot (low-priced) F&O names like IDEA
# (lot 71,475), which NSE's value-ranked "most active" feed omits — so we scan
# the highest-lot-size stocks' live chains and globally rank every strike by
# traded volume×lot (verified to match the source list to the decimal). No
# per-symbol cap: the genuine list is intentionally faithful (often IDEA-heavy).
# The scan runs in the scheduler and is cached here; the API just reads cache.

_MOST_ACTIVE_SCAN = 40     # scan the N highest-lot F&O stocks (volume×lot lives here)
_MOST_ACTIVE = {"rows": [], "ts": 0.0}


def _oi_buildup_signal(chg_pct: float, chg_oi: int) -> tuple[str, str, str]:
    """Classic price-vs-OI buildup read for one option strike (its own LTP move
    and its own OI move over the day):

        price ↑ & OI ↑ → Long build   (fresh buyers dominant)   → green
        price ↓ & OI ↑ → Short build  (fresh writers dominant)  → red
        price ↑ & OI ↓ → Short cover  (writers/bears exiting)    → light green
        price ↓ & OI ↓ → Long unwind  (buyers/bulls exiting)     → orange

    Returns (code, label, tone). "Flat" is intentionally narrow — only when
    *nothing* moved (both price and OI exactly zero). If just one axis moved we
    still classify off it (OI is the primary buildup driver), so the signal stays
    meaningful even on quiet/illiquid strikes instead of collapsing to flat."""
    up_p, dn_p = chg_pct > 0, chg_pct < 0
    up_oi, dn_oi = chg_oi > 0, chg_oi < 0
    # Both axes moved → the textbook price×OI matrix.
    if up_p and up_oi:
        return "long_build", "Long build", "green"
    if dn_p and up_oi:
        return "short_build", "Short build", "red"
    if up_p and dn_oi:
        return "short_cover", "Short cover", "lightgreen"
    if dn_p and dn_oi:
        return "long_unwind", "Long unwind", "orange"
    # Only one axis moved → infer from it.
    if up_oi:                       # OI added at a flat price → fresh longs building
        return "long_build", "Long build", "green"
    if dn_oi:                       # OI cut at a flat price → positions being closed
        return "long_unwind", "Long unwind", "orange"
    if up_p:                        # price up with no OI add → shorts covering
        return "short_cover", "Short cover", "lightgreen"
    if dn_p:                        # price down with no OI → longs liquidating
        return "long_unwind", "Long unwind", "orange"
    return "neutral", "Flat", "muted"   # genuinely nothing moved


def refresh_most_active(scan: int = _MOST_ACTIVE_SCAN, top: int = 12) -> dict:
    """Scan the highest-lot F&O stocks and rank every option strike by traded
    volume×lot. Populates the shared cache. Heavy → call from the scheduler."""
    from market_data import build_chain, _fetch_lot_size_map, _INDEX_SYMBOLS
    lot_map = _fetch_lot_size_map() or {}
    cands = [s for s, _ in sorted(lot_map.items(), key=lambda kv: kv[1], reverse=True)
             if s not in _INDEX_SYMBOLS][:scan]
    rows = []
    for sym in cands:
        try:
            df, exps, spot = build_chain(sym)
            if df is None or df.empty:
                continue
            lot = lot_map.get(sym) or LOT_SIZE
            expiry = str(exps[0]) if exps else ""
            # Direction axis for the OI-buildup signal = the underlying's move on
            # the day (live spot vs previous close). pnsea's chain has no per-option
            # price change, and the underlying's day move is the standard input for
            # an OI-buildup read. This is the day's net read — live intraday and the
            # final EOD value once the market closes.
            day_chg_pct = 0.0
            try:
                closes = _fetch_ohlc(sym, "1d", "6mo")
                if closes and len(closes) >= 2 and spot and closes[-2] > 0:
                    day_chg_pct = round((spot - closes[-2]) / closes[-2] * 100, 2)
            except Exception:
                day_chg_pct = 0.0
            sub = df[df["Volume"] > 0]
            for _, r in sub.iterrows():
                vol_c = int(r.get("Volume", 0) or 0)
                if vol_c <= 0:
                    continue
                # Filter: only contracts with a strike > 100 — drops low-priced names
                # (IDEA, YESBANK, etc. at ~20) so the ranking fills with the next
                # eligible high-strike contracts. Ranking/dedup/top-N are unchanged.
                if int(r["Strike"]) <= 100:
                    continue
                oi_c = int(r["OI"]); ltp = float(r["LTP"])
                chg_oi = int(r.get("ChgOI", 0) or 0)
                chg_pct = day_chg_pct
                sig_code, sig_label, sig_tone = _oi_buildup_signal(chg_pct, chg_oi)
                rows.append({
                    "symbol": sym,
                    "side": r["Type"],
                    "strike": int(r["Strike"]),
                    "ltp": round(ltp, 2),
                    "volume_shares": vol_c * lot,                  # for ranking
                    "volume_lakh": round(vol_c * lot / 1e5, 2),    # traded volume, lakhs (matches source)
                    "oi_cr": round(oi_c * lot / 1e7, 2),           # OI in crores of shares
                    "turnover_cr": round(vol_c * lot * ltp / 1e7, 1),  # ₹cr traded value
                    "chg_oi": chg_oi, "chg_pct": chg_pct,
                    "signal": sig_code, "signal_label": sig_label, "signal_tone": sig_tone,
                    "lot": lot,
                    "expiry": expiry,
                })
        except Exception:
            continue
    rows.sort(key=lambda x: x["volume_shares"], reverse=True)
    # Keep only the single highest-volume strike per (symbol, side): a stock can
    # appear at most twice — its top CE and its top PE — and never repeats a side
    # on a lower strike. Because rows are already volume-sorted, the first time we
    # see a (symbol, side) is its highest-traded strike; later ones are dropped.
    seen, deduped = set(), []
    for r in rows:
        key = (r["symbol"], r["side"])
        if key in seen:
            continue
        seen.add(key)
        deduped.append(r)
    rows = deduped[: max(top, 0)]
    for r in rows:
        r.pop("volume_shares", None)   # internal sort key, not for the client
    _MOST_ACTIVE["rows"] = rows
    _MOST_ACTIVE["ts"] = _time.time()
    print(f"[Screener] most-active refreshed: {len(rows)} strikes "
          f"(top: {rows[0]['symbol']} {rows[0]['strike']}{rows[0]['side']} "
          f"{rows[0]['volume_lakh']}L)" if rows else "[Screener] most-active: no data")
    return _MOST_ACTIVE


def most_active_stock_options(limit: int = 8) -> list:
    """Return the cached most-traded-by-volume strikes (populated by the scheduler)."""
    return _MOST_ACTIVE["rows"][: max(limit, 0)]


# ── Step 1 / Step 2 ─────────────────────────────────────────────────────────

def screen_step1(rsi: dict, side: str = "PE") -> tuple[bool, str]:
    """RSI momentum gate. Returns (pass, reason).

    PE (bullish): daily pulling back (40–50) while weekly still strong (>60),
                  OR hourly pulling back (40–50) while daily still strong (>60).
    CE (bearish): daily in a mild rollover (50–60) while weekly already weak (<40).
    """
    d, w, h = rsi.get("daily"), rsi.get("weekly"), rsi.get("hourly")
    if side == "CE":
        if d is not None and w is not None and 50 < d < 60 and w < 40:
            return True, f"dRSI {d} (bear pullback) + wRSI {w}<40"
        return False, ""
    # PE (bullish)
    if d is not None and w is not None and 40 < d < 50 and w > 60:
        return True, f"dRSI {d} (pullback) + wRSI {w}>60"
    if h is not None and d is not None and 40 < h < 50 and d > 60:
        return True, f"hRSI {h} (pullback) + dRSI {d}>60"
    return False, ""


def screen_step2(daily_closes) -> tuple[str, int]:
    """DeMark gate. Returns (status, count): CONFIRM / RADAR / DROP."""
    count = compute_td_buy_setup(daily_closes or [])
    if count >= 9:
        return "CONFIRM", count
    if count in (7, 8):
        return "RADAR", count
    return "DROP", count


# ── IV percentile (accumulated history + HV proxy fallback) ──────────────────

def compute_ivp(symbol, current_atm_iv, hv_pct, db) -> tuple[float | None, bool]:
    """Returns (ivp, provisional). Uses real ATM-IV history once enough exists,
    otherwise falls back to the HV percentile (provisional)."""
    try:
        rows = (db.query(StockIVHistory)
                .filter(StockIVHistory.symbol == symbol,
                        StockIVHistory.atm_iv.isnot(None))
                .order_by(StockIVHistory.date.desc())
                .limit(252).all())
        vals = [r.atm_iv for r in rows if r.atm_iv]
        if len(vals) >= _IVP_MIN_HISTORY and current_atm_iv:
            pct = round(sum(1 for v in vals if v < current_atm_iv) / len(vals) * 100, 1)
            return pct, False
    except Exception as e:
        print(f"  [Screener] IVP history error {symbol}: {e}")
    return (hv_pct, True) if hv_pct is not None else (None, True)


def _record_iv_history(symbol, atm_iv, hv, spot, dte, db):
    """Upsert today's ATM IV / HV for a symbol (for IVP accumulation)."""
    try:
        today = date.today()
        row = (db.query(StockIVHistory)
               .filter(StockIVHistory.symbol == symbol,
                       StockIVHistory.date == today).first())
        if row:
            row.atm_iv = atm_iv; row.hv = hv; row.spot = spot; row.dte = dte
        else:
            db.add(StockIVHistory(symbol=symbol, date=today, atm_iv=atm_iv,
                                  hv=hv, spot=spot, dte=dte))
        db.commit()
    except Exception as e:
        db.rollback()
        print(f"  [Screener] IV history write failed {symbol}: {e}")


# ── Step 3 ──────────────────────────────────────────────────────────────────

def _atm_iv(chain_df, spot) -> float | None:
    atm = chain_df.iloc[(chain_df["Strike"] - spot).abs().argmin()]["Strike"]
    ce = chain_df[(chain_df["Strike"] == atm) & (chain_df["Type"] == "CE")]
    pe = chain_df[(chain_df["Strike"] == atm) & (chain_df["Type"] == "PE")]
    ivs = [float(x["IV"].iloc[0]) for x in (ce, pe) if not x.empty and float(x["IV"].iloc[0]) > 0]
    return round(sum(ivs) / len(ivs), 2) if ivs else None


def _side_bias(rsi, forward, spot) -> tuple[str, str]:
    """Pick CE vs PE from a bullish/bearish read. Returns (side, reason)."""
    score = 1  # DeMark buy setup is bullish baseline
    bits = ["TD buy-setup"]
    if forward >= spot:
        score += 1; bits.append("F≥spot")
    else:
        score -= 1; bits.append("F<spot")
    sf, ss = rsi.get("sma_fast"), rsi.get("sma_slow")
    if sf and ss:
        if spot > ss:
            score += 1; bits.append("spot>SMA50")
        else:
            score -= 1
        if sf > ss:
            score += 1; bits.append("SMA20>SMA50")
    side = "PE" if score >= 0 else "CE"
    return side, ("bullish: " if side == "PE" else "bearish: ") + ", ".join(bits)


def _live_cmp(symbol, daily=None):
    """Live CMP for a radar tile — Sensibull last price, else last daily close."""
    try:
        from sensibull import get_last_price
        lp = get_last_price(symbol)
        if lp:
            return round(lp, 2)
    except Exception:
        pass
    return round(daily[-1], 2) if daily else None


def quick_bias(rsi: dict) -> str:
    """Lightweight bullish/bearish read from RSI + SMA (no chain needed) —
    used to highlight the preferred sell side on radar cards. Bullish → PE."""
    score = 0
    sf, ss = rsi.get("sma_fast"), rsi.get("sma_slow")
    if sf and ss:
        score += 1 if sf >= ss else -1
    d = rsi.get("daily")
    if d is not None:
        score += 1 if d >= 50 else -1
    return "PE" if score >= 0 else "CE"


def build_stock_suggestion(symbol, thresholds=None, db=None, rsi=None,
                           force_side=None, enforce_gates=True) -> dict | None:
    """Build a single sell suggestion for a symbol.

    When ``force_side`` is given the strike is picked on that side; otherwise the
    directional bias chooses it.  With ``enforce_gates`` the four Step-3 gates
    (proximity / IVP band / delta / yield) must pass or None is returned; with it
    off (the confirmation flow), the best strike is returned regardless plus
    per-gate flags so the UI can warn.  Returns None only if no strike exists.
    """
    t = {**DEFAULT_THRESHOLDS, **(thresholds or {})}
    own_db = db is None
    if own_db:
        db = SessionLocal()
    try:
        try:
            chain_df, expiries, spot = build_chain(symbol)
        except Exception as e:
            print(f"  [Screener] chain fetch failed {symbol}: {e}")
            return None
        if chain_df is None or chain_df.empty or not expiries or spot <= 0:
            return None

        expiry = str(expiries[0])
        T = time_to_expiry(expiry)
        dte = max(days_to_expiry(expiry), 1)
        forward = forward_from_chain(chain_df, spot, T)
        if not forward or abs(forward - spot) / spot > 0.15:
            forward = spot

        if rsi is None:
            rsi = multi_timeframe_rsi(symbol)
        preferred_side, bias_reason = _side_bias(rsi, forward, spot)
        side = force_side or preferred_side

        basis_pct = round((forward - spot) / spot * 100, 2)
        # (i) growth-potential proximity gate (direction-consistent forward)
        prox_ok = not ((side == "PE" and forward < spot * 0.985) or
                       (side == "CE" and forward > spot * 1.015))

        # (ii) IVP — Sensibull live first, then local history / HV proxy
        atm_iv = _atm_iv(chain_df, spot)
        daily = rsi.get("daily_closes")
        hv = historical_volatility(daily) if daily else None
        _record_iv_history(symbol, atm_iv, hv, spot, dte, db)
        ivp, provisional, ivp_source = None, False, "sensibull"
        try:
            from sensibull import get_iv_percentile
            live = get_iv_percentile(symbol, expiry)
            if live:
                ivp = live["ivp"]
        except Exception as e:
            print(f"  [Screener] Sensibull IVP failed {symbol}: {e}")
        if ivp is None:
            hvp = hv_percentile(daily) if daily else None
            ivp, provisional = compute_ivp(symbol, atm_iv, hvp, db)
            ivp_source = "history" if (ivp is not None and not provisional) else "hv-proxy"
        ivp_ok = ivp is not None and (t["ivp_min"] <= ivp <= t["ivp_max"])

        # (iii) strike: chosen side, OTM, delta band, highest OI
        sub = chain_df[(chain_df["Type"] == side) & (chain_df["LTP"] > 0)].copy()
        sub = sub[sub["Strike"] < spot] if side == "PE" else sub[sub["Strike"] > spot]
        sub["_ad"] = sub["delta"].abs()
        in_band = sub[(sub["_ad"] > t["delta_min"]) & (sub["_ad"] < t["delta_max"])]
        pool = in_band if not in_band.empty else sub
        if pool.empty:
            return None
        best = pool.loc[pool["OI"].idxmax()]
        strike = int(best["Strike"]); ltp = float(best["LTP"])
        if strike - ltp <= 0:
            return None
        delta_ok = not in_band.empty

        # (iv) premium yield
        prem_yield = round(ltp / (strike - ltp) * (365 / dte) * 100, 2)
        yield_ok = prem_yield >= t["min_yield"]

        # (v) setup confirmation — anchored VWAP from the start of the DeMark setup.
        # Bullish (PE): spot ≥ VWAP means demand absorbed the setup; bearish (CE):
        # spot ≤ VWAP means supply stayed in control. None (no volume) → not held
        # against the name. Scoring-only: NOT part of `passes` (the hard filter).
        anchored_vwap = anchored_vwap_for_symbol(symbol, side)
        if anchored_vwap is None:
            vwap_ok = True
        else:
            vwap_ok = (spot >= anchored_vwap) if side == "PE" else (spot <= anchored_vwap)

        passes = prox_ok and ivp_ok and delta_ok and yield_ok
        if enforce_gates and not passes:
            return None

        return {
            "symbol": symbol,
            "side": side,
            "preferred_side": preferred_side,
            "trade_type": f"{side}_SELL",
            "side_reason": bias_reason,
            "strike": strike,
            "expiry": expiry,
            "dte": dte,
            "ltp": round(ltp, 2),
            "delta": round(float(best["delta"]), 4),
            "oi": int(best["OI"]),
            "iv": round(float(best["IV"]), 2),
            "spot": round(spot, 2),
            "forward": round(forward, 2),
            "basis_pct": basis_pct,
            "ivp": ivp,
            "ivp_provisional": provisional,
            "ivp_source": ivp_source,
            "hv": hv,
            "premium_yield": prem_yield,
            "anchored_vwap": anchored_vwap,
            "lot_size": get_lot_size(symbol),
            "gates": {"proximity": prox_ok, "ivp": ivp_ok,
                      "delta": delta_ok, "yield": yield_ok,
                      "vwap": vwap_ok, "all": passes},
            "rsi": {"daily": rsi.get("daily"), "weekly": rsi.get("weekly"),
                    "hourly": rsi.get("hourly")},
        }
    finally:
        if own_db:
            db.close()


def screen_step3(symbol, rsi, thresholds, db) -> dict | None:
    """Step-3 gate for a screener-confirmed symbol (side chosen by bias)."""
    return build_stock_suggestion(symbol, thresholds, db, rsi=rsi,
                                  force_side=None, enforce_gates=True)


# ── Orchestration ───────────────────────────────────────────────────────────

_DAILY_FALLBACK_MIN = 5     # show weekly/hourly only when daily cards < this
_TF_FALLBACK_TOP = 3        # …and then only the top-3 weekly + top-3 hourly


def _pool_entry(sym: str, dm: dict, tf: str,
                pe_set, ce_set, pe_up, ce_up) -> dict:
    """Build a pool entry for a symbol whose DeMark (on timeframe ``tf``) qualifies.
    RSI + Step-3 gates are evaluated downstream as card exceptions, never a gate."""
    setup_side = "CE" if dm.get("dir") == "sell" else "PE"
    chartink_set = ce_set if setup_side == "CE" else pe_set
    chartink_up = ce_up if setup_side == "CE" else pe_up
    rsi = multi_timeframe_rsi(sym)
    ok, reason = screen_step1(rsi, side=setup_side)
    return {"symbol": sym, "rsi": rsi, "rsi_reason": reason, "rsi_ok": ok,
            "demark": dm, "tf": tf,
            "in_chartink": sym in chartink_set, "chartink_up": chartink_up}


def run_prefilter(limit: int | None = None) -> dict:
    """Scan the F&O universe for TD buy setups and cache the pool. Heavy — runs in
    the background scheduler, not on requests.

    DAILY is the primary trigger: a name pools on a fresh daily **9** (→ Picks) or
    forming **7-8** (→ Radar). Only when the daily pool is sparse (< 5) do we add a
    small fallback — the **top-3 weekly** setups — routed the same way by their
    count. The whole universe is checked (no RSI pre-filter), so a real DeMark is
    never overlooked; RSI + gates become per-card exceptions."""
    # Skip only if a *recent* run is genuinely in flight; a flag left stuck by a
    # hung/killed run (older than 3 min) self-heals so the pool can't stay empty.
    if _SCREEN_STATE.get("running") and (_time.monotonic() - _SCREEN_STATE.get("run_started", 0)) < 180:
        return _SCREEN_STATE
    _SCREEN_STATE["running"] = True
    _SCREEN_STATE["run_started"] = _time.monotonic()
    try:
        universe = fno_universe()
        if limit:
            universe = universe[:limit]
        # One batched daily download warms the cache so the scan reads from memory.
        _prefetch_bars(universe, "1d", "6mo")
        # Chartink live RSI pools (scoring inputs, not gates); fetch PE and CE
        # separately so each side is judged against its own directional rule.
        try:
            from chartink import step1_rsi_candidates, step1_ce_rsi_candidates
            pe_chartink_list = step1_rsi_candidates()
            ce_chartink_list = step1_ce_rsi_candidates()
        except Exception as e:
            print(f"  [Screener] chartink fetch failed: {e}")
            pe_chartink_list = None
            ce_chartink_list = None
        pe_set = set(pe_chartink_list or [])
        ce_set = set(ce_chartink_list or [])
        pe_up = pe_chartink_list is not None
        ce_up = ce_chartink_list is not None

        # Phase 1 — DAILY (primary).
        pool = []
        for sym in universe:
            try:
                d = _td_for_tf(sym, "daily")
                if d["fired"] or d["count"] in (7, 8):
                    pool.append(_pool_entry(sym, _demark_dict(d, "daily"), "daily",
                                            pe_set, ce_set, pe_up, ce_up))
            except Exception as e:
                print(f"  [Screener] prefilter error {sym}: {e}")
                continue

        # Phase 2 — WEEKLY fallback, only when daily is sparse; top-3.
        if len(pool) < _DAILY_FALLBACK_MIN:
            for tf in ("weekly",):
                _prefetch_bars(universe, *_DEMARK_TFS[tf])
                cands = []
                for sym in universe:
                    try:
                        t = _td_for_tf(sym, tf)
                        if t["fired"] or t["count"] in (7, 8):
                            cands.append((sym, t))
                    except Exception:
                        continue
                cands.sort(key=lambda x: (x[1]["fired"], x[1]["count"]), reverse=True)
                for sym, t in cands[:_TF_FALLBACK_TOP]:
                    pool.append(_pool_entry(sym, _demark_dict(t, tf), tf,
                                            pe_set, ce_set, pe_up, ce_up))

        _SCREEN_STATE.update({"pool": pool, "ts": _time.time(), "universe": len(universe)})
        n9 = sum(1 for p in pool if p["demark"].get("has9"))
        tfs = {}
        for p in pool:
            tfs[p["tf"]] = tfs.get(p["tf"], 0) + 1
        print(f"[Screener] prefilter: scanned={len(universe)} pool={len(pool)} "
              f"(9s={n9}, 7-8s={len(pool) - n9}) by-tf={tfs}")
        return _SCREEN_STATE
    finally:
        _SCREEN_STATE["running"] = False


def _unmet_constraints(sug: dict, t: dict, rsi_ok: bool = True) -> list:
    """Plain-language list of which picking filters a name hasn't matched (RSI +
    the four Step-3 gates) — shown on the card as exceptions."""
    g = sug.get("gates", {})
    u = []
    if not rsi_ok:
        u.append("RSI pullback structure not met")
    if not g.get("ivp"):
        ivp = sug.get("ivp")
        u.append(f"IVP {ivp if ivp is not None else '—'} not in "
                 f"{t['ivp_min']:.0f}–{t['ivp_max']:.0f}")
    if not g.get("delta"):
        u.append(f"|Δ| {abs(sug.get('delta', 0)):.2f} not in "
                 f"{t['delta_min']:.2f}–{t['delta_max']:.2f}")
    if not g.get("yield"):
        u.append(f"Yield {sug.get('premium_yield')}% < {t['min_yield']:.0f}%")
    if not g.get("proximity"):
        u.append("Forward vs spot proximity")
    if not g.get("vwap"):
        av = sug.get("anchored_vwap")
        u.append(f"Price vs setup VWAP {av if av is not None else '—'} "
                 "(reversal not confirmed)")
    return u


def _screen_pool(thresholds: dict | None, db) -> tuple[list, list]:
    """Split the cached setup pool by DeMark count alone (no lifecycle here):

      • confirmed (Ready-to-Trade Picks) — a fresh DeMark 9 on any timeframe.
      • radar (On the Radar)             — a forming DeMark 7-8 on any timeframe.

    Every name's RSI + Step-3 gates are evaluated and attached as ``unmet``
    (filter exceptions) so the card can show exactly what it doesn't pass — but
    a failing gate no longer demotes/drops a real 9. ``fully_ok`` marks the names
    that pass everything (used by the Track-Record log)."""
    t = {**DEFAULT_THRESHOLDS, **(thresholds or {})}
    confirmed, radar = [], []
    for p in _SCREEN_STATE["pool"]:
        dm = p["demark"]
        # Side is the setup's DIRECTION, not a momentum guess: a bullish TD buy
        # setup is a PE (put) sell; a bearish TD sell setup is a CE (call) sell.
        setup_dir = dm.get("dir", "buy")
        setup_side = "PE" if setup_dir == "buy" else "CE"
        try:
            # enforce_gates=False → always get the best strike + per-gate flags.
            sug = build_stock_suggestion(p["symbol"], thresholds, db,
                                         rsi=p["rsi"], force_side=setup_side,
                                         enforce_gates=False)
        except Exception as e:
            print(f"  [Screener] suggestion error {p['symbol']}: {e}")
            continue
        if not sug:
            continue
        sug["preferred_side"] = setup_side
        sug["side_reason"] = ("bullish: TD buy-setup → sell PE" if setup_dir == "buy"
                              else "bearish: TD sell-setup → sell CE")
        has9 = bool(dm.get("has9"))
        tfs = dm.get("fired_tfs") if has9 else dm.get("approaching_tfs")
        sug["demark"] = 9 if has9 else dm.get("max_count", 0)
        sug["demark_tf"] = ", ".join(_TF_ABBR[x] for x in (tfs or [])) or DEMARK_TIMEFRAME
        sug["demark_perfected"] = bool(dm.get("perfected_tfs")) and has9
        sug["tf"] = p.get("tf", "daily")     # daily | weekly | hourly (for logging)
        sug["rsi_reason"] = p.get("rsi_reason", "")
        # RSI prefilter = Chartink's pullback pool (authoritative); fall back to our
        # own RSI gate only when Chartink is unavailable.
        chartink_up = bool(p.get("chartink_up"))
        rsi_pass = bool(p.get("in_chartink")) if chartink_up else bool(p.get("rsi_ok", True))
        # Latest daily close — carried so the DeMark-9-day-low invalidation can be
        # checked live without an extra fetch.
        dcs = (p.get("rsi") or {}).get("daily_closes") or []
        sug["daily_close"] = float(dcs[-1]) if dcs else None
        # Tradability score = how many of the 6 picking filters pass (RSI + the four
        # Step-3 gates + setup-VWAP), DeMark excluded. Shortfalls are the exceptions.
        sug["unmet"] = _unmet_constraints(sug, t, rsi_ok=rsi_pass)
        sug["score_total"] = 6
        sug["score"] = 6 - len(sug["unmet"])
        sug["rsi_pass"] = rsi_pass          # for radar ranking (RSI emphasis)
        sug["fully_ok"] = (sug["score"] >= 6)
        # Section routing (the only place picks/radar are split). Thresholds scaled
        # for 6 filters to keep the prior selectivity (~⅔ / ~⅘):
        #   • a DeMark 9 is a Ready-to-Trade Pick ONLY with safety score ≥ 4,
        #   • a forming setup is UPGRADED from Radar to Picks once it reaches
        #     DeMark 8+ with a strong (5/6 or 6/6) safety score.
        # Everything else stays On the Radar. (has9 → demark==9, so the 8+ clause
        # also covers a 9; a sub-4 nine falls through to Radar as required.)
        is_pick = (has9 and sug["score"] >= 4) or (sug["demark"] >= 8 and sug["score"] >= 5)
        (confirmed if is_pick else radar).append(sug)
    return confirmed, radar


def _confirmed_log_map(db) -> dict:
    """{symbol: latest-episode StockConfirmedLog row}."""
    rows = (db.query(StockConfirmedLog)
            .order_by(StockConfirmedLog.first_confirmed.desc()).all())
    out = {}
    for r in rows:
        out.setdefault(r.symbol, r)   # first seen = latest (desc order)
    return out


def _maybe_kick_prefilter() -> None:
    """Self-heal: if the setup pool was never built (cold start, or a server just
    restarted onto new code) and nothing is in flight, kick a background prefilter
    so the page fills on the first load instead of waiting for the ~10-min
    scheduler tick. Non-blocking — the triggering request still returns the
    current (possibly empty) pool; the page's 2-min poll picks up the fresh one."""
    if _SCREEN_STATE.get("ts"):          # already built at least once
        return
    if _SCREEN_STATE.get("running") and (_time.monotonic() - _SCREEN_STATE.get("run_started", 0)) < 180:
        return                            # a build is genuinely in flight
    threading.Thread(target=run_prefilter_and_log, name="screen-selfheal",
                     daemon=True).start()


def get_screen_results(thresholds: dict | None = None) -> dict:
    """Live screen for the page. Confirmed = pool names passing all Step-3 gates
    whose confirmation episode is still < 2 days old (newest first); names that
    aged out have graduated to the Track Record log. Radar = a 9 is in but a gate
    is unmet. Fast — reuses cached chains. Safe to call per request."""
    _maybe_kick_prefilter()
    t = {**DEFAULT_THRESHOLDS, **(thresholds or {})}
    db = SessionLocal()
    try:
        confirmed, radar = _screen_pool(thresholds, db)
        now = datetime.utcnow()
        logmap = _confirmed_log_map(db)
        live_confirmed = []
        for c in confirmed:
            row = logmap.get(c["symbol"])
            fc = row.first_confirmed if row else now
            age_days = (now.date() - fc.date()).days
            c["first_confirmed"] = fc.isoformat()
            c["age_days"] = age_days
            # Invalidation: a daily close beyond the setup's stop breaks it (below
            # the 9-bar low for a bullish PE pick, above the 9-bar high for a bearish
            # CE pick) → drop from Confirmed. From the persisted flag or live close.
            broke = bool(row and row.invalidated)
            if (not broke and row and row.demark9_low
                    and _stop_broken(row.side, row.demark9_low, c.get("daily_close"))):
                broke = True
            if broke:
                continue
            if age_days < GRADUATE_AFTER_DAYS:   # still fresh → live Confirmed tab
                live_confirmed.append(c)
        # Most recently added first; premium yield breaks same-day ties.
        live_confirmed.sort(key=lambda x: (x["first_confirmed"], x.get("premium_yield", 0) or 0),
                            reverse=True)
        # Rank radar by safety score (filters passed), emphasising RSI then IVP,
        # with premium yield as the final tiebreak — best-conditioned setups on top.
        radar.sort(key=lambda x: (
            x.get("score", 0),
            1 if x.get("rsi_pass") else 0,
            1 if (x.get("gates") or {}).get("ivp") else 0,
            x.get("premium_yield", 0) or 0,
        ), reverse=True)
        any_provisional = any(c.get("ivp_provisional") for c in live_confirmed)
        return {
            "confirmed": live_confirmed,
            "radar": radar,
            "meta": {
                "universe": _SCREEN_STATE["universe"],
                "survivors": len(_SCREEN_STATE["pool"]),
                "last_prefilter": _SCREEN_STATE["ts"],
                "ivp_provisional": any_provisional,
                "thresholds": t,
            },
        }
    finally:
        db.close()


# ── Confirmed-suggestion lifecycle log + Track Record ────────────────────────

def _purge_log_to_current_week(db) -> None:
    """Weekly reset: drop log entries from before the current ISO week (Monday
    00:00 UTC ≈ 05:30 IST, i.e. before Monday's open). Stateless and idempotent —
    each Monday last week's Track Record is cleared automatically."""
    now = datetime.utcnow()
    week_start = (now - timedelta(days=now.weekday())).replace(
        hour=0, minute=0, second=0, microsecond=0)
    try:
        n = (db.query(StockConfirmedLog)
             .filter(StockConfirmedLog.first_confirmed < week_start).delete())
        if n:
            db.commit()
            print(f"[Screener] weekly track-record reset: cleared {n} log rows")
    except Exception as e:
        db.rollback()
        print(f"  [Screener] log purge failed: {e}")


def log_confirmed_suggestions() -> None:
    """Upsert EVERY ready-to-pick (DeMark 9) into the lifecycle log so nothing is
    lost when it later leaves the Picks section. Runs in the background after each
    prefilter so first_confirmed is stable and slider-independent. Also purges the
    log to the current week (a weekly reset)."""
    db = SessionLocal()
    try:
        _purge_log_to_current_week(db)
        confirmed, _ = _screen_pool(None, db)
        now = datetime.utcnow()
        for c in confirmed:
            # Track only DAILY picks — weekly/hourly are a transient fallback and
            # the Track-Record re-evaluation is daily, so logging them would be
            # incoherent. Every daily DeMark-9 is captured (exceptions or not).
            if c.get("tf", "daily") != "daily":
                continue
            sym = c["symbol"]
            row = (db.query(StockConfirmedLog)
                   .filter(StockConfirmedLog.symbol == sym)
                   .order_by(StockConfirmedLog.first_confirmed.desc()).first())
            recent = bool(row and row.last_confirmed and
                          (now - row.last_confirmed) <= timedelta(days=EPISODE_GAP_DAYS))
            if recent and not row.invalidated:
                row.last_confirmed = now          # same live episode
            elif recent and row.invalidated:
                continue                          # already broke its low — stay logged
            else:
                rsi = c.get("rsi") or {}
                # Setup stop level: 9-bar low for a PE (buy) pick, high for a CE
                # (sell) pick. Stored in demark9_low as the generic stop reference.
                stop, _close = _setup_stop(sym, c.get("side"))
                db.add(StockConfirmedLog(
                    symbol=sym, side=c.get("side"), strike=c.get("strike"),
                    expiry=c.get("expiry"), trade_type=c.get("trade_type"),
                    entry_rsi_d=rsi.get("daily"), entry_rsi_w=rsi.get("weekly"),
                    entry_rsi_h=rsi.get("hourly"),
                    entry_ivp=c.get("ivp"), entry_iv=c.get("iv"),
                    entry_delta=c.get("delta"), entry_oi=c.get("oi"),
                    entry_premium_yield=c.get("premium_yield"),
                    entry_demark=c.get("demark"), entry_demark_tf=c.get("demark_tf"),
                    entry_demark_perfected=c.get("demark_perfected"),
                    entry_spot=c.get("spot"), entry_forward=c.get("forward"),
                    demark9_low=stop, invalidated=False,
                    first_confirmed=now, last_confirmed=now))
        db.commit()

        # Invalidation pass: any active (not-yet-invalidated) episode whose latest
        # daily close has fallen below its DeMark-9-day low is broken → mark it so
        # it leaves Confirmed and is shown as invalidated in the Track Record.
        active = (db.query(StockConfirmedLog)
                  .filter(StockConfirmedLog.invalidated.isnot(True),
                          StockConfirmedLog.demark9_low.isnot(None))
                  .order_by(StockConfirmedLog.last_confirmed.desc()).limit(80).all())
        for r in active:
            if r.last_confirmed and (now - r.last_confirmed) > timedelta(days=10):
                continue   # stale episode, skip the live check
            _stop, close = _setup_stop(r.symbol, r.side)
            if r.demark9_low is not None and _stop_broken(r.side, r.demark9_low, close):
                r.invalidated = True
                r.invalidated_at = now
                r.invalidated_close = round(close, 2)
        db.commit()
    except Exception as e:
        db.rollback()
        print(f"  [Screener] confirmed-log update failed: {e}")
    finally:
        db.close()


def _reevaluate_symbol(symbol: str, side: str | None, db,
                       demark9_low: float | None = None) -> dict:
    """Live re-evaluation of a logged name: current RSI / IVP / Δ / DeMark, whether
    its latest daily close has broken the DeMark-9-day low, and whether it STILL
    passes the full confirmed criterion (DeMark 9 + RSI + all Step-3 gates AND the
    low not broken). Drives the Track Record 'tradable?' column."""
    try:
        dm = multi_timeframe_demark(symbol)
        rsi = multi_timeframe_rsi(symbol)
        rsi_ok, _ = screen_step1(rsi, side=side or "PE")
        sug = build_stock_suggestion(symbol, None, db, rsi=rsi,
                                     force_side=side, enforce_gates=False)
        gates_all = bool(sug and sug.get("gates", {}).get("all"))
        fired = dm.get("fired_tfs") or []
        dcs = (rsi or {}).get("daily_closes") or []
        cur_close = float(dcs[-1]) if dcs else None
        low_broken = bool(demark9_low) and _stop_broken(side, demark9_low, cur_close)
        tradable = bool(dm.get("has9") and rsi_ok and gates_all and not low_broken)
        return {
            "rsi": {"daily": rsi.get("daily"), "weekly": rsi.get("weekly"),
                    "hourly": rsi.get("hourly")},
            "rsi_ok": rsi_ok,
            "ivp": sug.get("ivp") if sug else None,
            "iv": sug.get("iv") if sug else None,
            "delta": sug.get("delta") if sug else None,
            "strike": sug.get("strike") if sug else None,
            "ltp": sug.get("ltp") if sug else None,
            "premium_yield": sug.get("premium_yield") if sug else None,
            "demark": 9 if fired else dm.get("max_count", 0),
            "demark_tf": ", ".join(_TF_ABBR[x] for x in fired) or "—",
            "demark_perfected": bool(dm.get("perfected_tfs")),
            "close": cur_close,
            "low_broken": low_broken,
            "gates": sug.get("gates") if sug else {},
            "tradable": tradable,
        }
    except Exception as e:
        print(f"  [Screener] re-eval {symbol} failed: {e}")
        return {"tradable": False, "demark": 0, "demark_tf": "—", "rsi": {},
                "low_broken": False, "error": True}


def _build_confirmed_history() -> list:
    """Graduated confirmation episodes (≥ 2 days old) with their entry snapshot +
    a live re-evaluation of current tradability."""
    db = SessionLocal()
    try:
        now = datetime.utcnow()
        rows = (db.query(StockConfirmedLog)
                .order_by(StockConfirmedLog.first_confirmed.desc()).all())
        out = []
        for r in rows:
            age_days = (now.date() - r.first_confirmed.date()).days
            live = _reevaluate_symbol(r.symbol, r.side, db, demark9_low=r.demark9_low)
            invalidated = bool(r.invalidated) or bool(live.get("low_broken"))
            # A row belongs in the Track Record once it has either aged out of the
            # live Confirmed tab (≥ 2 days) or been invalidated (broke its low).
            if age_days < GRADUATE_AFTER_DAYS and not invalidated:
                continue
            if invalidated:
                live["tradable"] = False     # broke the low → not re-tradable
            out.append({
                "symbol": r.symbol, "side": r.side, "strike": r.strike,
                "expiry": r.expiry, "trade_type": r.trade_type,
                "first_confirmed": r.first_confirmed.isoformat(),
                "last_confirmed": r.last_confirmed.isoformat() if r.last_confirmed else None,
                "age_days": age_days,
                "demark9_low": r.demark9_low,
                "invalidated": invalidated,
                "invalidated_at": r.invalidated_at.isoformat() if r.invalidated_at else None,
                "invalidated_close": r.invalidated_close if r.invalidated_close is not None else live.get("close"),
                "entry": {
                    "rsi": {"daily": r.entry_rsi_d, "weekly": r.entry_rsi_w,
                            "hourly": r.entry_rsi_h},
                    "ivp": r.entry_ivp, "iv": r.entry_iv, "delta": r.entry_delta,
                    "oi": r.entry_oi, "premium_yield": r.entry_premium_yield,
                    "demark": r.entry_demark, "demark_tf": r.entry_demark_tf,
                    "demark_perfected": r.entry_demark_perfected,
                },
                "live": live,
            })
        return out
    finally:
        db.close()


def get_confirmed_history() -> list:
    """Track Record list (cached briefly so rapid reloads don't re-scrape)."""
    return _memo("confirmed_history", _HISTORY_TTL, _build_confirmed_history,
                 cache_falsy=True) or []


def run_prefilter_and_log(limit: int | None = None) -> dict:
    """Background entry point: rebuild the DeMark-9 pool, then update the
    confirmed lifecycle log from the canonical (default-threshold) screen, and
    persist a snapshot of the canonical screen so it can be shown off-hours."""
    state = run_prefilter(limit=limit)
    try:
        log_confirmed_suggestions()
    except Exception as e:
        print(f"  [Screener] confirmed-log step failed: {e}")
    # Persist the canonical (default-threshold) screen as the off-hours snapshot.
    # Only when the pool actually built, so a transient empty scan never overwrites
    # a good close snapshot.
    try:
        if state.get("universe"):
            from snapshots import save_snapshot
            save_snapshot("screener", get_screen_results(None))
    except Exception as e:
        print(f"  [Screener] snapshot save failed: {e}")
    return state
