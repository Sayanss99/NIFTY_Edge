"""
Market data engine — fetches live NSE data via PNSEA, computes Greeks,
ranges, S/R levels, risk scores, and directional signals.

Ported from Sheet_dasboard_codes/option_chain_greeks.py and risk_monitor.py.
"""

import math
import numpy as np
import pandas as pd
from scipy.stats import norm
from datetime import datetime, date
from pnsea import NSE
from config import RISK_FREE_RATE, LOT_SIZE

CONFLUENCE_WINDOW = 75  # pts — OI and Fib levels within this band are merged


# ── Black-Scholes Greeks ────────────────────────────────────────────────────

def _d1_d2(S, K, T, r, sigma):
    if T <= 0 or sigma <= 0 or S <= 0 or K <= 0:
        return None, None
    d1 = (np.log(S / K) + (r + 0.5 * sigma ** 2) * T) / (sigma * np.sqrt(T))
    d2 = d1 - sigma * np.sqrt(T)
    return d1, d2


def greeks(S, K, T, r, sigma, option_type="CE"):
    d1, d2 = _d1_d2(S, K, T, r, sigma)
    if d1 is None:
        return {g: 0.0 for g in ("delta", "gamma", "theta", "vega", "rho")}

    n_d1 = norm.pdf(d1)
    disc = np.exp(-r * T)
    gamma = n_d1 / (S * sigma * np.sqrt(T))
    vega = S * n_d1 * np.sqrt(T) / 100

    if option_type == "CE":
        delta = norm.cdf(d1)
        theta = ((-S * n_d1 * sigma / (2 * np.sqrt(T)))
                 - r * K * disc * norm.cdf(d2)) / 365
        rho = K * T * disc * norm.cdf(d2) / 100
    else:
        delta = norm.cdf(d1) - 1
        theta = ((-S * n_d1 * sigma / (2 * np.sqrt(T)))
                 + r * K * disc * norm.cdf(-d2)) / 365
        rho = -K * T * disc * norm.cdf(-d2) / 100

    return {
        "delta": round(float(delta), 4),
        "gamma": round(float(gamma), 6),
        "theta": round(float(theta), 4),
        "vega": round(float(vega), 4),
        "rho": round(float(rho), 4),
    }


def time_to_expiry(expiry_str: str) -> float:
    expiry_dt = datetime.strptime(expiry_str, "%d-%b-%Y").date()
    today = date.today()
    days_left = (expiry_dt - today).days
    return max(days_left, 0) / 365.0


def days_to_expiry(expiry_str: str) -> int:
    expiry_dt = datetime.strptime(expiry_str, "%d-%b-%Y").date()
    return max((expiry_dt - date.today()).days, 0)


# ── Option Chain Fetching ───────────────────────────────────────────────────

def _extract_option_row(strike, opt_type, ltp, iv_pct, oi, vol, spot, expiry, r):
    T = time_to_expiry(expiry)
    sigma = iv_pct / 100.0
    g = greeks(spot, strike, T, r, sigma, option_type=opt_type)
    return {
        "Strike": int(round(strike)),
        "Type": opt_type,
        "LTP": round(float(ltp), 2),
        "IV": round(float(iv_pct), 2),
        "OI": int(oi) if not pd.isna(oi) else 0,
        "Volume": int(vol) if not pd.isna(vol) else 0,
        **g,
    }


def build_chain(symbol="NIFTY", expiry_date=None, risk_free_rate=None):
    """Fetch option chain from NSE. Returns (DataFrame, expiry_list, spot_price)."""
    if risk_free_rate is None:
        risk_free_rate = RISK_FREE_RATE

    nse = NSE()
    kwargs = {}
    if expiry_date:
        kwargs["expiry_date"] = expiry_date

    chain_df, expiries, spot = nse.options.option_chain(symbol, **kwargs)

    if "strikePrice" in chain_df.columns:
        rows = _process_wide(chain_df, spot, expiry_date or expiries[0], risk_free_rate)
    else:
        rows = _process_long(chain_df, spot, expiry_date or expiries[0], risk_free_rate)

    result = pd.DataFrame(rows)
    return result, expiries, float(spot)


def _process_wide(df, spot, expiry, r):
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
            rows.append(_extract_option_row(
                strike, opt_type, ltp, iv_pct or 0, oi, vol, spot, expiry, r
            ))
    return rows


def _process_long(df, spot, expiry, r):
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
        rows.append(_extract_option_row(
            strike, opt_type, ltp, iv_pct or 0, oi, vol, spot, expiry, r
        ))
    return rows


# ── VIX Fetch ───────────────────────────────────────────────────────────────

def fetch_vix(nse=None) -> dict | None:
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
    """Compute Fibonacci retracement levels from recent NIFTY OHLC."""
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
    """Compute standard pivot points from previous trading day."""
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

def compute_tendency(rr_value: float, vix: float = 0, vix_change_pct: float = 0) -> dict:
    """Compute market tendency based on Risk Reversal and supporting factors.

    Primary signal: RR value (CE 25d IV - PE 25d IV)
      - Positive RR → puts more expensive → bearish skew → Negative tendency
      - Negative RR → calls more expensive → bullish skew → Positive tendency

    Supporting factors add confidence:
      - VIX level (high VIX = fear = adds bearish confidence)
      - VIX change direction (rising VIX = bearish, falling = bullish)

    Returns dict with tendency, confidence, and detail.
    """
    # Primary: RR = CE_IV - PE_IV
    #   Negative RR → PE IV higher → puts expensive → bearish skew → Negative tendency
    #   Positive RR → CE IV higher → calls expensive → bullish skew → Positive tendency
    if rr_value < -1:
        direction = "Negative"
        base_confidence = min(abs(rr_value) / 6, 1.0)  # 6+ IV pts = full confidence
    elif rr_value > 1:
        direction = "Positive"
        base_confidence = min(abs(rr_value) / 6, 1.0)
    else:
        direction = "Neutral"
        base_confidence = 0.0

    # Supporting factors (each adds up to 0.15 confidence)
    support_score = 0
    factors = []

    # VIX level
    if vix >= 25:
        support_score -= 0.15  # high fear
        factors.append("High VIX")
    elif vix >= 20:
        support_score -= 0.08
    elif vix < 14:
        support_score += 0.10
        factors.append("Low VIX")

    # VIX change
    if vix_change_pct > 5:
        support_score -= 0.15
        factors.append("VIX surging")
    elif vix_change_pct > 2:
        support_score -= 0.08
    elif vix_change_pct < -3:
        support_score += 0.10
        factors.append("VIX falling")

    # Adjust confidence based on whether support aligns with direction
    if direction == "Negative":
        confidence = base_confidence + (-support_score if support_score < 0 else support_score * 0.5)
    elif direction == "Positive":
        confidence = base_confidence + (support_score if support_score > 0 else -support_score * 0.5)
    else:
        confidence = 0

    confidence = max(min(confidence, 1.0), 0.0)

    # Confidence label
    if confidence >= 0.7:
        conf_label = "strong"
    elif confidence >= 0.4:
        conf_label = "moderate"
    elif confidence > 0:
        conf_label = "mild"
    else:
        conf_label = ""

    label = direction
    if conf_label and direction != "Neutral":
        label = f"{direction} ({conf_label})"

    return {
        "tendency": direction,
        "confidence": round(confidence, 2),
        "label": label,
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

    return {
        "direction": direction,
        "score": round(composite, 2),
        "factors_agree": agree,
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

    if side == "CE":
        scenario_spot = sold_strike + 200
    else:
        scenario_spot = sold_strike - 200

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

    # DTE-adaptive weights
    if dte >= 4:
        w = {"cost": 0.30, "delta": 0.15, "gamma": 0.10,
             "oi": 0.20, "skew": 0.15, "sr": 0.10}
    elif dte >= 2:
        w = {"cost": 0.20, "delta": 0.25, "gamma": 0.15,
             "oi": 0.15, "skew": 0.10, "sr": 0.15}
    else:  # 0-1 DTE — gamma-dominant
        w = {"cost": 0.10, "delta": 0.30, "gamma": 0.25,
             "oi": 0.15, "skew": 0.05, "sr": 0.15}

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

    if dte == 0:     dte_mult = 1.3
    elif dte == 1:   dte_mult = 1.2
    elif dte == 2:   dte_mult = 1.1
    else:            dte_mult = 1.0
    hedge_lots = min(math.ceil(raw_lots * dte_mult), 3)

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
