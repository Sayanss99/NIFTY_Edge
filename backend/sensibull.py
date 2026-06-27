"""Sensibull live data client — real-time IV percentile per stock.

Sensibull's public instrument-details endpoint returns, per expiry, the live
implied volatility and the **IV percentile** (the same IVP shown on their option
chain).  We use it as the authoritative live IVP source for the stock-options
screener instead of computing a proxy.  Results are cached briefly and
sanity-checked.
"""

from datetime import datetime
from curl_cffi import requests as _cr

from market_data import _memo

_BASE = "https://api.sensibull.com/v1/instrument_details/"
_IV_TTL = 600.0   # 10 min — IVP moves slowly intraday


def _session():
    return _cr.Session(impersonate="chrome")


def _fetch_instrument(symbol: str) -> dict:
    """Live instrument snapshot: per-expiry IV map + last/close price.

    Returns {"expiries": {iso: {iv, ivp, prev_iv}}, "last_price", "close_price",
    "updated_at"}.  Cached briefly and shared.
    """
    def _producer():
        try:
            r = _session().get(f"{_BASE}{symbol.upper()}", timeout=15)
            j = r.json()
            exp = {}
            for exp_iso, d in (j.get("per_expiry_data") or {}).items():
                exp[exp_iso] = {
                    "iv": d.get("impliedVolatility"),
                    "ivp": d.get("iv_percentile"),
                    "prev_iv": d.get("prev_iv"),
                }
            return {
                "expiries": exp,
                "last_price": j.get("last_price"),
                "close_price": j.get("close_price"),
                "updated_at": j.get("last_updated_at"),
            }
        except Exception as e:
            print(f"  [Sensibull] fetch failed {symbol}: {e}")
            return {}
    return _memo(f"sensibull_iv:{symbol.upper()}", _IV_TTL, _producer)


def get_last_price(symbol: str) -> float | None:
    """Live last-traded price from Sensibull (falls back to close)."""
    d = _fetch_instrument(symbol)
    lp = (d or {}).get("last_price") or (d or {}).get("close_price")
    try:
        return float(lp) if lp and float(lp) > 0 else None
    except (TypeError, ValueError):
        return None


def _valid(d: dict) -> bool:
    iv, ivp = d.get("iv"), d.get("ivp")
    return (iv is not None and iv > 0 and ivp is not None and 0 <= ivp <= 100)


def get_iv_percentile(symbol: str, expiry_ddmon: str | None = None) -> dict | None:
    """Live IVP for a symbol. Prefers the requested expiry (DD-Mon-YYYY), else
    the nearest expiry with valid data. Returns {ivp, iv, expiry, source} or None.
    """
    snap = _fetch_instrument(symbol)
    data = (snap or {}).get("expiries") or {}
    if not data:
        return None

    target_iso = None
    if expiry_ddmon:
        try:
            target_iso = datetime.strptime(expiry_ddmon, "%d-%b-%Y").strftime("%Y-%m-%d")
        except Exception:
            target_iso = None

    if target_iso and target_iso in data and _valid(data[target_iso]):
        d = data[target_iso]
        return {"ivp": round(d["ivp"], 1), "iv": round(d["iv"], 2),
                "expiry": target_iso, "source": "sensibull"}

    # Fall back to the nearest (earliest) expiry that has valid live data.
    for iso in sorted(data.keys()):
        if _valid(data[iso]):
            d = data[iso]
            return {"ivp": round(d["ivp"], 1), "iv": round(d["iv"], 2),
                    "expiry": iso, "source": "sensibull"}
    return None
