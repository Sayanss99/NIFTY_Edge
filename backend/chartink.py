"""Chartink live screener client — runs the Step-1 multi-timeframe RSI scan
across the whole market in a single live request (no specific dashboard needed).

Chartink computes daily/weekly/hourly RSI server-side, so this replaces ~200×3
yfinance calls with one call and acts as a second live source.  Returns the list
of matching NSE symbols; the screener then intersects with the F&O universe and
verifies/enriches each survivor with yfinance (DeMark + display RSI).
"""

import re
from curl_cffi import requests as _cr

from market_data import _memo

_SCREENER_URL = "https://chartink.com/screener/"
_PROCESS_URL = "https://chartink.com/screener/process"
_TTL = 300.0   # 5 min

# Step-1 PE (bullish) RSI rule:  (40<dRSI<50 & wRSI>60)  OR  (40<hRSI<50 & dRSI>60)
STEP1_CLAUSE = (
    "( {cash} ( "
    "( latest rsi( 14 ) > 40 and latest rsi( 14 ) < 50 and weekly rsi( 14 ) > 60 ) "
    "or ( [0] 1 hour rsi( 14 ) > 40 and [0] 1 hour rsi( 14 ) < 50 and latest rsi( 14 ) > 60 ) "
    ") )"
)

# Step-1 CE (bearish) RSI rule:  50<dRSI<60 & wRSI<40
# Mirror of the bullish rule: daily is in a mild bear pullback (50–60 = still elevated
# but rolling over) while the weekly is already weak (<40), confirming the downtrend.
STEP1_CE_CLAUSE = (
    "( {cash} ( "
    "latest rsi( 14 ) > 50 and latest rsi( 14 ) < 60 and weekly rsi( 14 ) < 40 "
    ") )"
)


def _scan(clause: str) -> list | None:
    """Run a Chartink scan; returns list of NSE symbols, or None on failure."""
    def _producer():
        try:
            s = _cr.Session(impersonate="chrome")
            page = s.get(_SCREENER_URL, timeout=20)
            m = re.search(r'name="csrf-token"\s+content="([^"]+)"', page.text)
            if not m:
                return None
            headers = {
                "x-csrf-token": m.group(1),
                "x-requested-with": "XMLHttpRequest",
                "referer": _SCREENER_URL,
            }
            resp = s.post(_PROCESS_URL, data={"scan_clause": clause},
                          headers=headers, timeout=30)
            rows = resp.json().get("data", [])
            syms = [str(d["nsecode"]).upper() for d in rows if d.get("nsecode")]
            return sorted(set(syms)) or None
        except Exception as e:
            print(f"  [Chartink] scan failed: {e}")
            return None
    # None (failure) is not cached, so a transient error retries next call.
    return _memo(f"chartink:{hash(clause)}", _TTL, _producer)


def step1_rsi_candidates() -> list | None:
    """Live Step-1 PE (bullish) candidates. None if Chartink is unreachable."""
    return _scan(STEP1_CLAUSE)


def step1_ce_rsi_candidates() -> list | None:
    """Live Step-1 CE (bearish) candidates. None if Chartink is unreachable."""
    return _scan(STEP1_CE_CLAUSE)
