"""
risk_monitor.py — Live P&L + Risk Severity + OI/Fibonacci Hedge Engine

Multi-speed refresh architecture:
  • Every 15 s  — Spot, VIX (B3–B6), current LTP & unrealized P&L, hedge responses
  • Every ~3 min — Risk scores, hedge trigger / suggestion
  • At 10:00 / 12:30 / 15:30 — Daily IV & delta snapshots
  • At 15:15 (weekdays) — OI + Fibonacci S/R levels

Sheet layout:
  ── Inputs (user fills once per trade) ──────────────────────────
  B2   EXPIRY          e.g.  17-Mar-2026
  B13  NIFTY_MAX_LIMIT worst-case CE level
  B14  NIFTY_MIN_LIMIT worst-case PE level
  D7   LOT_SIZE        e.g.  75
  B16  TRADED_DATE     date you entered the trade  (DD-Mon-YYYY)

  ── Market Data (script writes every 15s) ───────────────────────
  B3   NIFTY CMP       (live spot from option chain)
  B4   INDIA VIX       (live from NSE)
  B5   VIX Change      (absolute, from previous close)
  B6   VIX Change %    (percentage)

  ── Hedge Suggestions (rows 16–17) ─────────────────────────────
  H16:J16  CE hedge: STRIKE / QTY / COST  (script writes ONCE when triggered)
  H17:J17  PE hedge: same
  K16:L16  Worst / Best case P&L  (script computes)
  K17:L17  same for PE
  M16/M17  User response: ✅ Done / ❌ Rejected / ✅ Logged

  ── Risk Scores ─────────────────────────────────────────────────
  H6 / I6        CE / PE risk (0–100)
  H9–H12         CE breakdown labels
  I9–I12         PE breakdown labels

  ── Support / Resistance ────────────────────────────────────────
  K4–K6   Supports    (refreshed at scheduled times + startup)
  L4–L6   Resistances

  ── Directional Signal (K12:L12) ────────────────────────────────
  K12  Direction + factor count  (e.g. "🟢 BULLISH 6/8")
  L12  Trade suggestion + score  (e.g. "BUY 24500CE @120 | +3.2")
  8-factor engine; fires when ≥5 agree. "(prev)" tag outside windows.
  J8:K11 = 25-delta Risk Reversal table (manually managed, NOT touched by script).
"""

import time
import math
import traceback
from datetime import datetime, date, timedelta

import gspread
from google.oauth2.service_account import Credentials

from option_chain_greeks import build_chain

# ── CONFIG ────────────────────────────────────────────────────────────────────

SERVICE_ACCOUNT_FILE = "credentials.json"
SHEET_ID             = "1evL0PFhsHY4Yo7nlV6oRa0DYyPM_ykJkdbBlrYnuYr0"
WORKSHEET_NAME       = "Sheet1"
WORKSHEET2_NAME      = "Sheet2"
RISK_FREE_RATE       = 0.0525

# ── Refresh timing ────────────────────────────────────────────────────────────

FAST_INTERVAL    = 15        # seconds — prices, P&L, VIX
OC_INTERVAL      = 30        # seconds — option chain on Sheet2
SLOW_MULTIPLIER  = 12        # every 12 fast cycles (~3 min) — risk, hedge
SR_REFRESH_TIMES  = ["10:00", "12:00", "14:00", "15:25"]  # S/R refresh times

# ── Input cells (user fills) ─────────────────────────────────────────────────

CELL_TIMESTAMP   = "C1"
CELL_EXPIRY      = "B3"
CELL_WORST_CE    = "B13"     # NIFTY_MAX_LIMIT
CELL_WORST_PE    = "B14"     # NIFTY_MIN_LIMIT
LOT_SIZE         = 65
CELL_TRADED_DATE = "B16"

# ── Market data outputs (script writes every 15s) ────────────────────────────

CELL_SPOT_OUT     = "B4"
CELL_VIX_OUT      = "B5"
CELL_VIX_CHG      = "B6"
CELL_VIX_CHG_PCT  = "B7"

# ── Hedge cells (Preventive Hedge Trades table, rows 19–20) ─────────────────

HEDGE_CE_STRIKE     = "C19"
HEDGE_CE_QTY        = "D19"
HEDGE_CE_COST       = "E19"
HEDGE_CE_WORST_PNL  = "F19"
HEDGE_CE_BEST_PNL   = "G19"
HEDGE_CE_CONFIDENCE = "H19"
CELL_CE_HEDGE_RESP  = "I19"

HEDGE_PE_STRIKE     = "C20"
HEDGE_PE_QTY        = "D20"
HEDGE_PE_COST       = "E20"
HEDGE_PE_WORST_PNL  = "F20"
HEDGE_PE_BEST_PNL   = "G20"
HEDGE_PE_CONFIDENCE = "H20"
CELL_PE_HEDGE_RESP  = "I20"

# ── Risk Meter output cells (rows 14–20) ─────────────────────────────────────

CELL_CE_RISK_SCORE  = "K16"
CELL_PE_RISK_SCORE  = "L16"
CELL_CE_DELTA_LABEL = "K17"
CELL_CE_IV_LABEL    = "K18"
CELL_CE_PREM_LABEL  = "K19"
CELL_CE_PROX_LABEL  = "K20"
CELL_PE_DELTA_LABEL = "L17"
CELL_PE_IV_LABEL    = "L18"
CELL_PE_PREM_LABEL  = "L19"
CELL_PE_PROX_LABEL  = "L20"

# ── S/R output cells ─────────────────────────────────────────────────────────

CELL_SR_SUPPORTS = ["K4", "K5", "K6"]
CELL_SR_RESISTS  = ["L4", "L5", "L6"]

# ── Pivot+OI S/R output cells (I4:J6) ───────────────────────────────────────

CELL_PIVOT_SUPPORTS  = ["I4", "I5", "I6"]
CELL_PIVOT_RESISTS   = ["J4", "J5", "J6"]

# ── Directional Signal output cells (K12:L12 only) ──────────────────────────
# J8:K11 = 25-delta Risk Reversal table (manually managed in sheet — do NOT touch).

CELL_DIR_SIGNAL = "L9"     # Direction + conviction (e.g. "BEARISH (moderate)")
CELL_DIR_TRADE  = "L10"    # Trade suggestion (e.g. "BUY 24500CE @120")

# ── Trade Details (rows 13–15) ───────────────────────────────────────────────

CELL_CE_STRIKE_IN   = "C14"    # user fills CE strike
CELL_PE_STRIKE_IN   = "C15"    # user fills PE strike
CELL_CE_ENTRY_IV    = "D14"    # script writes once (from Sheet2 col B)
CELL_PE_ENTRY_IV    = "D15"    # script writes once (from Sheet2 col H)
CELL_CE_ENTRY_DELTA = "E14"    # script writes once (from Sheet2 col D)
CELL_PE_ENTRY_DELTA = "E15"    # script writes once (from Sheet2 col F)
CELL_CE_ENTRY_PREM  = "F14"    # user fills CE entry premium
CELL_PE_ENTRY_PREM  = "F15"    # user fills PE entry premium
CELL_CE_HEDGING     = "I14"    # script writes: Required / Not Required
CELL_PE_HEDGING     = "I15"

# ── Sheet2 Option Chain ──────────────────────────────────────────────────────

OC_EXPIRY_CELL   = "E2"      # user input: expiry for option chain
OC_ERROR_CELL    = "F2"      # script writes error if bad expiry
OC_DATA_RANGE    = "A4:I86"  # header row 4 + up to 82 data rows (A5:I86)
OC_HEADER_ROW    = 4
OC_DATA_START    = 5
OC_DATA_END      = 86
OC_STRIKES       = 40        # strikes each side of ATM

# ── Sheet2 Trade Summary (K4:O6) ────────────────────────────────────────────
# Row 4 = header, Row 5 = CE side, Row 6 = PE side
# Columns: K=Label, L=Strike, M=LTP, N=Delta, O=IV
OC_SUMMARY_RANGE = "K4:O6"

# ── Chart Configuration ──────────────────────────────────────────────────────

CHART_TITLE       = "NIFTY Range & Spot Monitor"
CHART_CELL        = "B22"          # cell where =IMAGE() formula is written
GCS_BUCKET        = "nifty-risk-chart-megho"
GCS_OBJECT_NAME   = "nifty_range_chart.png"
# Cells to read for chart data (sheet formula outputs)
CHART_CALC_CELLS  = ["E3", "E4", "E7", "E8"]  # calc_top, calc_bottom, vix_top, vix_bottom

# ── Other ─────────────────────────────────────────────────────────────────────

CONFLUENCE_WINDOW = 75    # pts — OI and Fib levels within this band are merged


# ── Google Sheets connection ──────────────────────────────────────────────────

def connect_sheets() -> tuple[gspread.Worksheet, gspread.Worksheet]:
    creds = Credentials.from_service_account_file(
        SERVICE_ACCOUNT_FILE,
        scopes=["https://www.googleapis.com/auth/spreadsheets"]
    )
    book = gspread.authorize(creds).open_by_key(SHEET_ID)
    ws1 = book.worksheet(WORKSHEET_NAME)
    ws2 = book.worksheet(WORKSHEET2_NAME)
    return ws1, ws2


# ── Helpers ───────────────────────────────────────────────────────────────────

def _float(val, default=0.0) -> float:
    try:
        return float(str(val).replace(",", "").replace("%", "").strip())
    except (ValueError, TypeError):
        return default

def _int(val, default=0) -> int:
    return int(_float(val, default))

# ── VIX fetch ─────────────────────────────────────────────────────────────────

def fetch_vix(nse) -> dict | None:
    """Fetch India VIX from NSE.  Returns {last, change, pChange} or None."""
    try:
        resp = nse.endpoint_tester("https://www.nseindia.com/api/allIndices")
        data = resp.json()
        for idx in data.get("data", []):
            name = str(idx.get("indexSymbol") or idx.get("index") or "")
            if "VIX" in name.upper():
                return {
                    "last":    _float(idx.get("last")),
                    "change":  _float(idx.get("variation")),
                    "pChange": _float(idx.get("percentChange")),
                }
    except Exception as e:
        print(f"  [WARN] VIX fetch failed: {e}")
    return None


# ── Sheet reading ─────────────────────────────────────────────────────────────

def read_config(ws: gspread.Worksheet) -> dict:
    """Read trade configuration from fixed input cells (rarely changes)."""
    cells = [CELL_EXPIRY, CELL_WORST_CE, CELL_WORST_PE, CELL_TRADED_DATE]
    values = ws.batch_get(cells)
    def v(i):
        try:
            return values[i][0][0]
        except (IndexError, TypeError):
            return ""
    return {
        "expiry":        v(0),
        "worst_case_ce": _int(v(1)),
        "worst_case_pe": _int(v(2)),
        "lot_size":      LOT_SIZE,
        "traded_date":   v(3),
    }


def load_sr_from_sheet(ws) -> tuple[list, list]:
    """Load cached S/R levels from sheet cells on startup."""
    raw = ws.batch_get(CELL_SR_SUPPORTS + CELL_SR_RESISTS)
    supports, resistances = [], []
    for i in range(3):
        try:
            val = str(raw[i][0][0]).strip()
        except (IndexError, TypeError):
            val = "-"
        if val not in ("-", ""):
            supports.append({"strike": _int(val), "oi": None, "source": "cached"})
    for i in range(3, 6):
        try:
            val = str(raw[i][0][0]).strip()
        except (IndexError, TypeError):
            val = "-"
        if val not in ("-", ""):
            resistances.append({"strike": _int(val), "oi": None, "source": "cached"})
    return supports, resistances


# ── Risk scoring ──────────────────────────────────────────────────────────────

def _label(score: float) -> str:
    if score < 35:  return "✅ OK"
    if score < 60:  return "⚠️ WARNING"
    if score < 80:  return "🔴 DANGER"
    return "🚨 CRITICAL"


# ── Sub-scores (0-100 each) ──────────────────────────────────────────────────

def _delta_score(delta: float) -> float:
    d = abs(delta)
    if d <= 0.08: return 0
    if d <= 0.15: return 20
    if d <= 0.20: return 40
    if d <= 0.30: return 65
    if d <= 0.40: return 85
    return 100


def _gamma_dte_score(gamma: float, delta: float, dte: int) -> float:
    """Gamma/DTE acceleration — captures convexity risk near expiry."""
    d = abs(delta)
    gamma_impact = abs(gamma) * LOT_SIZE * 50   # delta shift per 50-pt NIFTY move

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
    # DTE <= 7  — gamma explosion zone
    if d < 0.10: return 50
    return 100


def _iv_score(entry_iv: float, current_iv: float,
              vix_abs: float = 0.0) -> float:
    if entry_iv <= 0:
        return 0
    pct = (current_iv - entry_iv) / entry_iv * 100
    if pct < 10:   base = 0
    elif pct < 25:  base = 20
    elif pct < 40:  base = 45
    elif pct < 60:  base = 70
    else:           base = 90
    # VIX regime penalty — elevated absolute VIX amplifies IV risk
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


# ── Hard circuit breakers ────────────────────────────────────────────────────

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


# ── Composite risk score ─────────────────────────────────────────────────────

# Weights: delta(0.25) + gamma/DTE(0.15) + IV(0.15) + premium(0.15)
#         + proximity(0.20) + theta_ratio(0.10)

def compute_risk_score(
    delta: float, gamma: float, theta: float,
    entry_iv: float, current_iv: float,
    entry_premium: float, current_ltp: float,
    current_spot: float, strike: float,
    dte: int, vix_abs: float = 0.0, vix_chg_pct: float = 0.0,
) -> tuple[float, dict, bool]:
    """
    Returns (score, breakdown_dict, hedge_required).
    hedge_required is True if hard trigger fires OR composite >= 65.
    If entry_premium is 0 (blank), premium sub-score is excluded and
    weights are redistributed.
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
        # Redistribute premium weight (0.15) across other factors
        total = round(
            0.30 * ds + 0.17 * gs + 0.17 * ivs +
            0.24 * prx + 0.12 * trs, 1)

    # Hard circuit breakers
    hard_triggered = _check_hard_triggers(
        delta, entry_premium, current_ltp, current_spot,
        strike, dte, vix_abs, vix_chg_pct) if has_premium else False

    hedge_required = hard_triggered or total >= 65

    # Breakdown labels for sheet
    iv_chg_pct = ((current_iv - entry_iv) / entry_iv * 100) if entry_iv > 0 else 0
    dist_pts   = abs(current_spot - strike)

    bd = {
        "delta":     (ds,  f"{_label(ds)} | Δ = {abs(delta):.3f}"),
        "iv":        (ivs, f"{_label(ivs)} | IV {iv_chg_pct:+.0f}% from entry"),
        "proximity": (prx, f"{_label(prx)} | {dist_pts:.0f} pts from strike"),
    }
    if has_premium:
        prem_ratio = current_ltp / entry_premium
        bd["premium"] = (ps, f"{_label(ps)} | {prem_ratio:.1f}× entry premium")
    else:
        bd["premium"] = (0, "")

    bd["overall"] = _label(total)

    return total, bd, hedge_required


# ── OI-based Support / Resistance ────────────────────────────────────────────

def find_oi_sr_levels(chain_df, current_spot: float,
                      worst_case_ce: int, worst_case_pe: int) -> dict:
    ce_zone = chain_df[
        (chain_df["Type"] == "CE") &
        (chain_df["Strike"] > current_spot) &
        (chain_df["Strike"] <= worst_case_ce)
    ].copy()
    pe_zone = chain_df[
        (chain_df["Type"] == "PE") &
        (chain_df["Strike"] < current_spot) &
        (chain_df["Strike"] >= worst_case_pe)
    ].copy()

    top_resist = (ce_zone.nlargest(3, "OI")[["Strike", "OI"]]
                  .rename(columns={"Strike": "strike", "OI": "oi"})
                  .to_dict("records"))
    top_support = (pe_zone.nlargest(3, "OI")[["Strike", "OI"]]
                   .rename(columns={"Strike": "strike", "OI": "oi"})
                   .to_dict("records"))

    for row in top_resist + top_support:
        row["strike"] = int(round(row["strike"]))

    return {"oi_supports": top_support, "oi_resistances": top_resist}


# ── Fibonacci Retracement ─────────────────────────────────────────────────────

def fetch_fibonacci_levels(lookback_days: int = 20) -> list[int]:
    """Fetch NIFTY 50 OHLC via yfinance and compute Fibonacci retracement levels."""
    try:
        import yfinance as yf

        nifty = yf.download("^NSEI", period=f"{lookback_days + 5}d",
                            progress=False, auto_adjust=True)
        # Flatten multi-level columns from yfinance
        if hasattr(nifty.columns, "levels"):
            nifty.columns = nifty.columns.get_level_values(0)
        if nifty.empty or len(nifty) < 5:
            print("  [WARN] Fibonacci: insufficient NIFTY data from yfinance.")
            return []

        # Use last `lookback_days` trading sessions
        nifty = nifty.tail(lookback_days)

        swing_high = float(nifty["High"].max())
        swing_low  = float(nifty["Low"].min())
        diff = swing_high - swing_low
        if diff <= 0:
            return []

        fib_ratios = [0.236, 0.382, 0.500, 0.618, 0.786]
        levels = sorted(int(round(swing_low + diff * r)) for r in fib_ratios)
        print(f"  Fib: swing {swing_low:.0f}–{swing_high:.0f}  levels: {levels}")
        return levels

    except Exception as e:
        print(f"  [WARN] Fibonacci fetch failed: {e} — using OI-only S/R.")
        return []


# ── Merge OI + Fibonacci into top-3 S/R ──────────────────────────────────────

def merge_sr_levels(
    oi_supports: list, oi_resistances: list,
    fib_levels: list[int], current_spot: float,
) -> tuple[list, list]:
    """
    Pick the 3 best S/R levels prioritising OI+Fib confluence.

    Tier 1 — OI strike within CONFLUENCE_WINDOW of a Fib level (highest
             conviction: both the market's positioning and the mathematical
             retracement agree).  Sorted by OI weight so the strongest
             confluence zones come first.
    Tier 2 — Remaining OI-only strikes (market positioning without Fib
             confirmation).  Used as backfill if fewer than 3 confluences.
    Tier 3 — Remaining Fib-only levels (mathematical levels without OI
             backing).  Lowest conviction; only used when OI data is sparse.
    """
    fib_below = [f for f in fib_levels if f < current_spot]
    fib_above = [f for f in fib_levels if f > current_spot]

    def _merge(oi_list: list, fib_list: list, descending: bool) -> list:
        confluence = []   # Tier 1: OI strike confirmed by nearby Fib
        oi_only    = []   # Tier 2: OI strike without Fib match
        used_fibs  = set()

        for oi_entry in oi_list:
            strike = oi_entry["strike"]
            oi_val = oi_entry["oi"] or 0
            # Find the closest Fib level to this OI strike
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
                    "strike": strike, "oi": oi_entry["oi"],
                    "source": f"OI+Fib ✓ (fib {best_fib})",
                    "_oi_val": oi_val, "_dist": best_dist,
                })
            else:
                oi_only.append({
                    "strike": strike, "oi": oi_entry["oi"],
                    "source": "OI", "_oi_val": oi_val, "_dist": 9999,
                })

        # Fib levels that had no nearby OI strike
        fib_only = []
        for fib in fib_list:
            if fib not in used_fibs:
                fib_only.append({
                    "strike": fib, "oi": None,
                    "source": "Fib", "_oi_val": 0, "_dist": 9999,
                })

        # Sort each tier: confluence by proximity then OI, others by OI
        confluence.sort(key=lambda x: (x["_dist"], -x["_oi_val"]))
        oi_only.sort(key=lambda x: -x["_oi_val"])
        fib_only.sort(key=lambda x: x["strike"], reverse=descending)

        # Merge tiers: confluence first, then OI-only backfill, then Fib-only
        result = (confluence + oi_only + fib_only)[:3]

        # Clean internal keys and sort by strike for display
        for r in result:
            r.pop("_oi_val", None)
            r.pop("_dist", None)
        result.sort(key=lambda x: x["strike"], reverse=descending)
        return result

    top3_supports    = _merge(oi_supports,    fib_below, descending=True)
    top3_resistances = _merge(oi_resistances, fib_above, descending=False)
    return top3_supports, top3_resistances


# ── Pivot Point + OI based S/R (I4:J6) ──────────────────────────────────────

def fetch_pivot_levels(lookback_days: int = 1) -> dict:
    """Compute standard pivot points from previous trading day OHLC via yfinance.

    Standard pivots:
      Pivot = (H + L + C) / 3
      S1 = 2×P - H,  S2 = P - (H - L),  S3 = L - 2×(H - P)
      R1 = 2×P - L,  R2 = P + (H - L),  R3 = H + 2×(P - L)

    Returns {"pivot": P, "supports": [S1,S2,S3], "resistances": [R1,R2,R3]}
    or empty dict on failure.
    """
    try:
        import yfinance as yf
        nifty = yf.download("^NSEI", period=f"{lookback_days + 5}d",
                            progress=False, auto_adjust=True)
        if hasattr(nifty.columns, "levels"):
            nifty.columns = nifty.columns.get_level_values(0)
        if nifty.empty or len(nifty) < 2:
            print("  [WARN] Pivot: insufficient NIFTY data from yfinance.")
            return {}

        prev = nifty.iloc[-2]  # previous completed session
        H = float(prev["High"])
        L = float(prev["Low"])
        C = float(prev["Close"])
        P = (H + L + C) / 3

        supports = [
            int(round(2 * P - H)),              # S1
            int(round(P - (H - L))),             # S2
            int(round(L - 2 * (H - P))),         # S3
        ]
        resistances = [
            int(round(2 * P - L)),               # R1
            int(round(P + (H - L))),             # R2
            int(round(H + 2 * (P - L))),         # R3
        ]

        print(f"  Pivot: P={P:.0f}  S={supports}  R={resistances}")
        return {"pivot": int(round(P)),
                "supports": supports, "resistances": resistances}

    except Exception as e:
        print(f"  [WARN] Pivot fetch failed: {e}")
        return {}


def compute_pivot_oi_sr(
    chain_df, current_spot: float,
    pivot_data: dict,
) -> tuple[list, list]:
    """
    Pick top-3 supports and resistances from pivot levels, ranked by OI.

    Logic:
      1. All pivot supports below CMP, all pivot resistances above CMP.
      2. For each pivot level, find the nearest option chain strike and
         look up its OI.
      3. Rank by OI descending — top 3 with highest OI are picked.

    Returns (supports_list, resistances_list) — each is a list of
    {"strike": int, "oi": int, "source": str}.
    """
    if not pivot_data:
        return [], []

    pivot_sups = sorted(pivot_data["supports"], reverse=True)   # highest first
    pivot_ress = sorted(pivot_data["resistances"])               # lowest first

    # Available option chain strikes
    all_strikes = sorted(chain_df["Strike"].unique())
    if not all_strikes:
        return [], []

    def _nearest_strike(level):
        return min(all_strikes, key=lambda s: abs(s - level))

    def _get_oi(strike, opt_type):
        row = chain_df[(chain_df["Strike"] == strike) &
                       (chain_df["Type"] == opt_type)]
        if not row.empty:
            return int(row.iloc[0]["OI"])
        return 0

    # ── Supports: any pivot level below CMP ──
    scored_sups = []
    for level in pivot_sups:
        ns = _nearest_strike(level)
        if ns < current_spot:
            oi = _get_oi(ns, "PE")  # PE OI = support
            scored_sups.append({"strike": ns, "oi": oi,
                                "source": f"Pivot+OI"})

    # Deduplicate by strike, keep highest OI
    seen = {}
    for s in scored_sups:
        k = s["strike"]
        if k not in seen or s["oi"] > seen[k]["oi"]:
            seen[k] = s
    scored_sups = sorted(seen.values(), key=lambda x: -x["oi"])[:3]

    # ── Resistances: any pivot level above CMP ──
    scored_ress = []
    for level in pivot_ress:
        ns = _nearest_strike(level)
        if ns > current_spot:
            oi = _get_oi(ns, "CE")  # CE OI = resistance
            scored_ress.append({"strike": ns, "oi": oi,
                                "source": f"Pivot+OI"})

    seen = {}
    for r in scored_ress:
        k = r["strike"]
        if k not in seen or r["oi"] > seen[k]["oi"]:
            seen[k] = r
    scored_ress = sorted(seen.values(), key=lambda x: -x["oi"])[:3]

    # Sort for display: supports descending, resistances ascending
    scored_sups.sort(key=lambda x: x["strike"], reverse=True)
    scored_ress.sort(key=lambda x: x["strike"])

    return scored_sups, scored_ress


def write_pivot_sr(ws, supports: list, resistances: list):
    """Write Pivot+OI S/R levels to I4:J6."""
    updates = []
    for i, s in enumerate(supports):
        updates.append({"range": CELL_PIVOT_SUPPORTS[i],
                        "values": [[int(s["strike"])]]})
    for i in range(len(supports), 3):
        updates.append({"range": CELL_PIVOT_SUPPORTS[i], "values": [["-"]]})
    for i, r in enumerate(resistances):
        updates.append({"range": CELL_PIVOT_RESISTS[i],
                        "values": [[int(r["strike"])]]})
    for i in range(len(resistances), 3):
        updates.append({"range": CELL_PIVOT_RESISTS[i], "values": [["-"]]})
    if updates:
        ws.batch_update(updates)
        print(f"  Pivot+OI S/R written: S={[int(s['strike']) for s in supports]} R={[int(r['strike']) for r in resistances]}")


# ── 25-delta IV helper ──────────────────────────────────────────────────────

def _find_25delta_iv(chain_df, opt_type: str) -> tuple[int, float]:
    """Return (strike, IV_%) for the strike closest to |delta| = 0.25."""
    sub = chain_df[chain_df["Type"] == opt_type].copy()
    if sub.empty:
        return 0, 0.0
    sub["_d25"] = (sub["delta"].abs() - 0.25).abs()
    best = sub.loc[sub["_d25"].idxmin()]
    return int(best["Strike"]), float(best["IV_%"])


# ── Directional Signal: 8-factor scoring engine ─────────────────────────────
#
# Each factor returns a float in [-1, +1]:
#   +1 = strongly bullish,  -1 = strongly bearish,  0 = neutral.
# A signal fires when ≥5 of 8 factors agree on a direction.
# Only evaluated during signal windows: 09:30-10:00 and 14:45-15:15 IST.


# Signal window definitions (24h format)
SIGNAL_WINDOWS = [("09:30", "10:00"), ("14:45", "15:15")]


def _in_signal_window(now: datetime) -> bool:
    """Return True if current time falls inside a signal window."""
    t = now.strftime("%H:%M")
    for start, end in SIGNAL_WINDOWS:
        if start <= t <= end:
            return True
    return False


def _rr_score(chain_df) -> float:
    """Factor 1: 25-delta Risk Reversal (CE 25d IV - PE 25d IV).
    Positive RR → calls expensive → market expects upside → bullish."""
    _, ce_iv = _find_25delta_iv(chain_df, "CE")
    _, pe_iv = _find_25delta_iv(chain_df, "PE")
    if ce_iv <= 0 or pe_iv <= 0:
        return 0.0
    rr = ce_iv - pe_iv
    # Scale: ±4 IV points maps to ±1.0
    return max(min(rr / 4.0, 1.0), -1.0)


def _pcr_score(chain_df) -> float:
    """Factor 2: Put-Call Ratio by OI.
    High PCR (>1.2) = more puts written as support → bullish.
    Low PCR (<0.8)  = more calls written as resistance → bearish."""
    ce_oi = chain_df.loc[chain_df["Type"] == "CE", "OI"].sum()
    pe_oi = chain_df.loc[chain_df["Type"] == "PE", "OI"].sum()
    if ce_oi <= 0:
        return 0.0
    pcr = pe_oi / ce_oi
    # Neutral band: 0.8-1.2 → 0.  Beyond that, scale to ±1
    if pcr > 1.2:
        return min((pcr - 1.2) / 0.6, 1.0)
    elif pcr < 0.8:
        return max((pcr - 0.8) / 0.6, -1.0)
    return 0.0


def _oi_buildup_score(chain_df, prev_oi: dict | None, spot: float) -> float:
    """Factor 3: OI buildup direction.
    Rising CE OI above spot = resistance building = bearish.
    Rising PE OI below spot = support building = bullish.
    Returns 0.0 on first call (no previous snapshot)."""
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

    # Net: PE buildup is bullish, CE buildup is bearish
    net = pe_delta - ce_delta
    denom = max(abs(pe_delta) + abs(ce_delta), 1)
    return max(min(net / denom, 1.0), -1.0)


def _iv_skew_slope_score(chain_df, spot: float) -> float:
    """Factor 4: IV skew slope across strikes.
    If OTM puts have steeper IV rise than OTM calls → market pricing
    downside → bearish.  Vice versa → bullish."""
    atm = round(spot / 50) * 50  # nearest 50

    otm_ce = chain_df[(chain_df["Type"] == "CE") &
                       (chain_df["Strike"] >= atm + 100) &
                       (chain_df["Strike"] <= atm + 500)]
    otm_pe = chain_df[(chain_df["Type"] == "PE") &
                       (chain_df["Strike"] <= atm - 100) &
                       (chain_df["Strike"] >= atm - 500)]

    ce_avg_iv = otm_ce["IV_%"].mean() if not otm_ce.empty else 0
    pe_avg_iv = otm_pe["IV_%"].mean() if not otm_pe.empty else 0

    atm_row = chain_df[(chain_df["Strike"] == atm) & (chain_df["Type"] == "CE")]
    atm_iv = float(atm_row["IV_%"].iloc[0]) if not atm_row.empty else 0

    if atm_iv <= 0:
        return 0.0

    # Normalised skew: how much more expensive each wing is vs ATM
    ce_skew = (ce_avg_iv - atm_iv) / atm_iv
    pe_skew = (pe_avg_iv - atm_iv) / atm_iv

    # If PE wing more expensive → downside fear → bearish
    diff = ce_skew - pe_skew  # positive = CE wing steeper = bullish fear
    return max(min(-diff * 5, 1.0), -1.0)


def _volume_pressure_score(chain_df, spot: float) -> float:
    """Factor 5: Volume pressure — CE vs PE volume near the money.
    Heavy CE buying near ATM → bullish demand.  Heavy PE buying → bearish."""
    atm = round(spot / 50) * 50
    near = chain_df[(chain_df["Strike"] >= atm - 200) &
                     (chain_df["Strike"] <= atm + 200)]

    ce_vol = near.loc[near["Type"] == "CE", "Volume"].sum()
    pe_vol = near.loc[near["Type"] == "PE", "Volume"].sum()
    total = ce_vol + pe_vol
    if total <= 0:
        return 0.0

    # CE-heavy volume = bullish, PE-heavy = bearish
    ratio = (ce_vol - pe_vol) / total
    return max(min(ratio * 2, 1.0), -1.0)


def _vix_score(vix_data: dict | None) -> float:
    """Factor 6: VIX regime.
    Low & falling VIX = bullish.  High & rising = bearish."""
    if not vix_data:
        return 0.0
    vix = vix_data["last"]
    chg_pct = vix_data["pChange"]  # already in whole number form

    score = 0.0
    # Level component
    if vix < 13:     score += 0.4
    elif vix < 16:   score += 0.2
    elif vix < 20:   score += 0.0
    elif vix < 25:   score -= 0.3
    else:            score -= 0.5

    # Change component
    if chg_pct < -5:    score += 0.5
    elif chg_pct < -2:  score += 0.3
    elif chg_pct > 5:   score -= 0.5
    elif chg_pct > 2:   score -= 0.3

    return max(min(score, 1.0), -1.0)


def compute_max_pain(chain_df) -> float:
    """Compute max-pain strike (strike where total buyer loss is maximised).
    Returns the strike value, or 0 if cannot compute."""
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
            # CE buyers lose when settle < strike
            ce_oi = ce_map.get(s, 0)
            pain += max(settle - s, 0) * ce_oi
            # PE buyers lose when settle > strike
            pe_oi = pe_map.get(s, 0)
            pain += max(s - settle, 0) * pe_oi
        if pain < min_pain:
            min_pain = pain
            best_strike = settle

    return float(best_strike)


def _max_pain_score(chain_df, spot: float) -> float:
    """Factor 7: Max pain position relative to spot.
    Spot below max pain → gravitational pull upward → bullish.
    Spot above max pain → pull downward → bearish."""
    mp = compute_max_pain(chain_df)
    if mp <= 0 or spot <= 0:
        return 0.0
    diff_pct = (mp - spot) / spot * 100
    # Scale: ±2% maps to ±1.0
    return max(min(diff_pct / 2.0, 1.0), -1.0)


def _gex_score(chain_df, spot: float) -> float:
    """Factor 8: Gamma Exposure (GEX).
    Net positive gamma near spot → market makers will buy dips / sell rallies
    → mean-reverting → bullish bias (pins toward current level).
    Net negative gamma → trend-amplifying → bearish bias (breakout risk)."""
    atm = round(spot / 50) * 50
    near = chain_df[(chain_df["Strike"] >= atm - 300) &
                     (chain_df["Strike"] <= atm + 300)]

    # GEX per strike = gamma × OI × contractMultiplier × spot²
    # For relative scoring we can skip constant multipliers
    gex = 0.0
    for _, row in near.iterrows():
        g = float(row.get("gamma", 0))
        oi = float(row.get("OI", 0))
        sign = 1 if row["Type"] == "CE" else -1  # MMs are short puts, long gamma on puts => negative GEX from puts
        # Actually: MMs sell to buyers. If buyer buys CE, MM is short CE → negative gamma.
        # Convention: CE OI adds positive GEX, PE OI adds negative GEX
        gex += sign * g * oi

    if gex > 0:
        # Positive GEX → mean-reverting → bullish (supports current level)
        return min(0.6, 1.0)
    elif gex < 0:
        # Negative GEX → trend-following → could break either way, slight bearish
        return max(-0.4, -1.0)
    return 0.0


def _snapshot_oi(chain_df, spot: float) -> dict:
    """Capture current OI snapshot for next-cycle delta comparison."""
    ce_above = chain_df[(chain_df["Type"] == "CE") & (chain_df["Strike"] > spot)]
    pe_below = chain_df[(chain_df["Type"] == "PE") & (chain_df["Strike"] < spot)]
    return {
        "ce_above_oi": ce_above["OI"].sum(),
        "pe_below_oi": pe_below["OI"].sum(),
    }


def compute_directional_signal(
    chain_df, spot: float, vix_data: dict | None,
    prev_oi: dict | None,
) -> dict:
    """
    Run all 8 factors and produce a directional signal.

    Returns dict with keys:
      direction: "BULLISH" / "BEARISH" / "NEUTRAL"
      score: float composite (-8 to +8)
      factors_agree: int (how many agree on the winning direction)
      factor_details: list of (name, value) tuples
      max_pain: float
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
        "factor_details": factors,
        "max_pain": compute_max_pain(chain_df),
    }


def select_trade_strike(chain_df, spot: float, direction: str,
                        dte: int) -> dict | None:
    """
    Pick the best strike for a directional trade suggestion.

    BULLISH → buy CE (slightly OTM, good delta/cost).
    BEARISH → buy PE (slightly OTM, good delta/cost).
    NEUTRAL → no suggestion.

    Returns dict: {strike, type, ltp, delta, rationale} or None.
    """
    if direction == "NEUTRAL":
        return None

    opt_type = "CE" if direction == "BULLISH" else "PE"
    sub = chain_df[(chain_df["Type"] == opt_type) & (chain_df["LTP"] > 0)].copy()
    if sub.empty:
        return None

    # Target: |delta| between 0.20 and 0.40 (sweet spot for directional)
    sub["_d_dist"] = (sub["delta"].abs() - 0.30).abs()
    sub = sub[(sub["delta"].abs() >= 0.15) & (sub["delta"].abs() <= 0.45)]
    if sub.empty:
        return None

    # Score: prefer delta near 0.30, good OI, reasonable cost
    sub = sub.copy()
    sub["_score"] = (
        (1.0 - sub["_d_dist"] / 0.15) * 40 +               # delta proximity
        sub["OI"].rank(pct=True) * 30 +                       # liquidity
        (1.0 - sub["LTP"].rank(pct=True)) * 30                # cost efficiency
    )

    best = sub.loc[sub["_score"].idxmax()]
    strike = int(best["Strike"])
    ltp = float(best["LTP"])
    delta = float(best["delta"])

    # DTE context in rationale
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
        "rationale": f"BUY {strike}{opt_type} @{ltp:.0f} |Δ|={abs(delta):.2f} {dte_note}",
    }


def _conviction_label(score: float) -> str:
    """Map composite score magnitude to conviction text."""
    mag = abs(score)
    if mag >= 5:   return "high conviction"
    if mag >= 3:   return "moderate conviction"
    if mag >= 1.5: return "mild conviction"
    return "borderline conviction"


def _format_l9_l10(ws, bg9, fg9, bg10=None, fg10=None):
    """Apply background + text formatting to L9 and L10."""
    sid = ws.id
    if bg10 is None:
        bg10 = {"red": 0.96, "green": 0.96, "blue": 0.96}
    if fg10 is None:
        fg10 = {"red": 0.15, "green": 0.15, "blue": 0.15}
    requests = [
        {"repeatCell": {
            "range": {"sheetId": sid,
                      "startRowIndex": 8, "endRowIndex": 9,
                      "startColumnIndex": 11, "endColumnIndex": 12},
            "cell": {"userEnteredFormat": {
                "backgroundColor": bg9,
                "textFormat": {"bold": True, "fontSize": 12,
                               "foregroundColor": fg9},
                "horizontalAlignment": "CENTER",
            }},
            "fields": "userEnteredFormat(backgroundColor,textFormat,horizontalAlignment)",
        }},
        {"repeatCell": {
            "range": {"sheetId": sid,
                      "startRowIndex": 9, "endRowIndex": 10,
                      "startColumnIndex": 11, "endColumnIndex": 12},
            "cell": {"userEnteredFormat": {
                "backgroundColor": bg10,
                "textFormat": {"bold": False, "fontSize": 12,
                               "foregroundColor": fg10},
                "horizontalAlignment": "CENTER",
            }},
            "fields": "userEnteredFormat(backgroundColor,textFormat,horizontalAlignment)",
        }},
    ]
    try:
        ws.spreadsheet.batch_update({"requests": requests})
    except Exception:
        pass


def _direction_colors(direction: str):
    """Return (bg, fg) dicts for a direction."""
    if direction == "BULLISH":
        return ({"red": 0.85, "green": 0.95, "blue": 0.85},
                {"red": 0.1,  "green": 0.4,  "blue": 0.1})
    elif direction == "BEARISH":
        return ({"red": 0.97, "green": 0.85, "blue": 0.85},
                {"red": 0.6,  "green": 0.1,  "blue": 0.1})
    return ({"red": 0.93, "green": 0.93, "blue": 0.93},
            {"red": 0.3,  "green": 0.3,  "blue": 0.3})


def write_signal_entry(ws, signal: dict, trade: dict,
                       entry_time: str, sl_price: float):
    """Write a new trade entry to L9:L10 (frozen until exit).

    L9  = direction + conviction
    L10 = BUY strike@price | SL:xx | HH:MM AM
    """
    conv = _conviction_label(signal["score"])
    m9 = f"{signal['direction']} ({conv})"
    m10 = (f"BUY {trade['strike']}{trade['type']} @{trade['ltp']:.0f}"
           f" | SL:{sl_price:.0f} | {entry_time}")

    ws.batch_update([
        {"range": CELL_DIR_SIGNAL, "values": [[m9]]},
        {"range": CELL_DIR_TRADE,  "values": [[m10]]},
    ])
    bg, fg = _direction_colors(signal["direction"])
    _format_l9_l10(ws, bg, fg)


def write_signal_exit(ws, active: dict, current_ltp: float,
                      exit_time: str, reason: str):
    """Write EXIT to L9:L10 (stays until new signal replaces it).

    L9  = EXIT
    L10 = EXIT strike@ltp | exitTime (entry: entryTime) | reason
    """
    m9 = "EXIT"
    m10 = (f"EXIT {active['strike']}{active['type']} @{current_ltp:.0f}"
           f" | {exit_time} (entry: {active['entry_time']})"
           f" | {reason}")

    ws.batch_update([
        {"range": CELL_DIR_SIGNAL, "values": [[m9]]},
        {"range": CELL_DIR_TRADE,  "values": [[m10]]},
    ])
    # Red bg for EXIT on both cells
    bg = {"red": 0.95, "green": 0.80, "blue": 0.80}
    fg = {"red": 0.6,  "green": 0.1,  "blue": 0.1}
    _format_l9_l10(ws, bg, fg, bg10=bg, fg10=fg)


def write_signal_neutral(ws, signal: dict):
    """Write neutral/idle state to L9:L10 (no active trade)."""
    conv = _conviction_label(signal["score"])
    m9 = f"{signal['direction']} ({conv})"

    ws.batch_update([
        {"range": CELL_DIR_SIGNAL, "values": [[m9]]},
        {"range": CELL_DIR_TRADE,  "values": [["--"]]},
    ])
    bg, fg = _direction_colors(signal["direction"])
    _format_l9_l10(ws, bg, fg)


# ── Hedge engine v2: 6-factor scored strike selection ───────────────────────

def compute_hedge_v2(
    side: str,                     # "CE" or "PE"
    sold_strike: int,
    entry_premium: float,
    current_spot: float,
    chain_df,                      # full option chain DataFrame
    dte: int,                      # days to expiry
    supports: list,                # [{strike, oi, source}, ...]
    resistances: list,
    lot_size: int,
) -> dict | None:
    """
    Intelligent hedge suggestion with 6-factor strike scoring,
    DTE-adaptive weighting, quantity optimisation, and confidence scoring.

    Returns dict with keys: strike, ltp, qty_lots, cost, worst_pnl,
    best_pnl, confidence, rationale.  Or None if no viable hedge found.
    """
    import pandas as pd

    opt_type = side  # "CE" or "PE"

    # ── Build candidate list ────────────────────────────────────────────
    sub = chain_df[chain_df["Type"] == opt_type].copy()
    sub = sub[sub["LTP"] > 0].copy()
    if sub.empty:
        return None

    # Range: from slightly ITM to moderately OTM beyond sold strike
    if side == "CE":
        lo = current_spot - 100
        hi = sold_strike + 300
    else:
        lo = sold_strike - 300
        hi = current_spot + 100

    cands = sub[(sub["Strike"] >= lo) & (sub["Strike"] <= hi)].copy()
    if cands.empty:
        return None

    # ── 25-delta risk reversal ──────────────────────────────────────────
    _, ce_25d_iv = _find_25delta_iv(chain_df, "CE")
    _, pe_25d_iv = _find_25delta_iv(chain_df, "PE")
    risk_reversal = ce_25d_iv - pe_25d_iv  # positive = call skew

    # ── S/R strike set for proximity scoring ────────────────────────────
    sr_strikes = []
    for s in (supports or []):
        sr_strikes.append(s["strike"])
    for r in (resistances or []):
        sr_strikes.append(r["strike"])

    # ── Scenario spot for P&L (200-pt gap from sold strike) ─────────────
    if side == "CE":
        scenario_spot = sold_strike + 200
    else:
        scenario_spot = sold_strike - 200

    # ── Factor scoring per candidate ────────────────────────────────────
    scores = []
    for _, row in cands.iterrows():
        c_strike = int(row["Strike"])
        c_ltp    = float(row["LTP"])
        c_delta  = float(row["delta"])
        c_gamma  = float(row.get("gamma", 0))
        c_oi     = float(row.get("OI", 0))
        c_vol    = float(row.get("Volume", 0))
        c_iv     = float(row.get("IV_%", 0))

        # Factor 1: Cost efficiency
        if side == "CE":
            protection = max(scenario_spot - c_strike, 0)
        else:
            protection = max(c_strike - scenario_spot, 0)
        cost_per_pt = c_ltp / max(protection, 1) if protection > 0 else 99

        # Factor 2: Delta coverage
        delta_cov = min(abs(c_delta) / 0.50 * 100, 100)

        # Factor 3: Gamma advantage
        gamma_val = c_gamma * lot_size * 50  # gamma impact for 50-pt move

        # Factor 4: OI / liquidity
        oi_val = c_oi

        # Factor 5: IV skew alignment
        dist_from_atm = c_strike - current_spot
        if side == "CE":
            if risk_reversal > 2:
                # Calls expensive → prefer OTM (further from spot = less skew premium)
                skew_score = min(max(dist_from_atm / 50, 0) * 20, 100)
            else:
                # Calls cheap → prefer closer to ATM
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

    # ── Normalise factors to 0-100 using rank-percentile ────────────────
    def _rank_norm(series, ascending=True):
        """Rank-percentile normalisation to 0-100."""
        if series.nunique() <= 1:
            return pd.Series(50.0, index=series.index)
        ranked = series.rank(ascending=ascending, method="min")
        return (ranked - 1) / (ranked.max() - 1) * 100

    sdf["f_cost"]  = _rank_norm(sdf["cost_per_pt"], ascending=True)   # lower = better
    sdf["f_delta"] = sdf["delta_cov"].clip(0, 100)
    sdf["f_gamma"] = _rank_norm(sdf["gamma_impact"], ascending=False)
    sdf["f_oi"]    = _rank_norm(sdf["oi_val"], ascending=False)
    sdf["f_skew"]  = sdf["skew_score"].clip(0, 100)
    sdf["f_sr"]    = sdf["sr_score"]

    # ── DTE-adaptive weights ────────────────────────────────────────────
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

    # ── Select best strike ──────────────────────────────────────────────
    sdf = sdf.sort_values("composite", ascending=False)
    best = sdf.iloc[0]
    h_strike = int(best["strike"])
    h_ltp    = float(best["ltp"])

    if h_ltp <= 0:
        return None

    # ── Quantity calculation ─────────────────────────────────────────────
    sold_qty = lot_size   # 1 lot sold (standard)
    if side == "CE":
        short_loss = max(scenario_spot - sold_strike, 0) * sold_qty
        hedge_payoff_per_lot = max(scenario_spot - h_strike, 0) * lot_size
    else:
        short_loss = max(sold_strike - scenario_spot, 0) * sold_qty
        hedge_payoff_per_lot = max(h_strike - scenario_spot, 0) * lot_size

    net_payoff_per_lot = hedge_payoff_per_lot - h_ltp * lot_size
    if net_payoff_per_lot <= 0:
        # Strike can't profit at scenario — use 1 lot minimum as insurance
        raw_lots = 1
    else:
        premium_collected = entry_premium * sold_qty
        loss_after_premium = max(short_loss - premium_collected, 0)
        raw_lots = max(math.ceil(loss_after_premium / net_payoff_per_lot), 1)

    # DTE safety multiplier
    if dte == 0:     dte_mult = 1.3
    elif dte == 1:   dte_mult = 1.2
    elif dte == 2:   dte_mult = 1.1
    else:            dte_mult = 1.0
    hedge_lots = math.ceil(raw_lots * dte_mult)

    # Cap at 3× sold lots
    hedge_lots = min(hedge_lots, 3)

    cost = round(h_ltp * hedge_lots * lot_size, 2)

    # ── P&L at 200-point scenario ───────────────────────────────────────
    if side == "CE":
        short_pnl_worst = (entry_premium - max(scenario_spot - sold_strike, 0)) * sold_qty
        hedge_pnl_worst = (max(scenario_spot - h_strike, 0) - h_ltp) * hedge_lots * lot_size
    else:
        short_pnl_worst = (entry_premium - max(sold_strike - scenario_spot, 0)) * sold_qty
        hedge_pnl_worst = (max(h_strike - scenario_spot, 0) - h_ltp) * hedge_lots * lot_size
    worst_pnl = round(short_pnl_worst + hedge_pnl_worst, 2)

    # Best case: spot stays, short keeps premium, hedge decays
    if dte >= 3:    decay = 0.85
    elif dte >= 1:  decay = 0.90
    else:           decay = 1.00
    best_pnl = round(entry_premium * sold_qty - cost * decay, 2)

    # ── Confidence scoring (0-100) ──────────────────────────────────────
    conf = 0

    # 1. Composite strike quality (max 35)
    conf += (float(best["composite"]) / 100) * 35

    # 2. Cost-to-loss ratio (max 20)
    max_loss_unhedged = max(short_loss - entry_premium * sold_qty, 1)
    cost_ratio = cost / max_loss_unhedged
    if cost_ratio < 0.05:     conf += 20
    elif cost_ratio < 0.10:   conf += 15
    elif cost_ratio < 0.20:   conf += 10
    elif cost_ratio < 0.30:   conf += 5

    # 3. Liquidity (max 15)
    h_oi = float(best["oi"]) or 1
    h_vol = float(best["volume"]) or 0
    vol_oi = h_vol / h_oi
    if vol_oi > 0.5:     conf += 15
    elif vol_oi > 0.2:   conf += 10
    elif vol_oi > 0.05:  conf += 5

    # 4. Worst-case P&L effectiveness (max 15)
    if worst_pnl > 0:         conf += 15
    elif worst_pnl > -cost:   conf += 10

    # 5. DTE appropriateness (max 15)
    if dte >= 3:     conf += 15
    elif dte == 2:   conf += 12
    elif dte == 1:   conf += 7
    else:            conf += 3   # 0-DTE hedge is last resort

    confidence = min(round(conf), 100)

    # ── Rationale string ────────────────────────────────────────────────
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


# ── Hedge response handling ───────────────────────────────────────────────────

def handle_hedge_responses(ws: gspread.Worksheet) -> dict:
    """
    Read I19/I20 (CONFIRM column) and act on user's response.

    ✅ Done     → mark ✅ Logged, keep C-H intact, lock
    ❌ Rejected → clear C-H and I, unlock for new suggestion
    ✅ Logged   → lock (already handled)

    Returns {"ce_locked": bool, "pe_locked": bool}.
    """
    raw = ws.batch_get([CELL_CE_HEDGE_RESP, CELL_PE_HEDGE_RESP,
                        HEDGE_CE_STRIKE, HEDGE_PE_STRIKE])
    def rv(i):
        try:    return raw[i][0][0]
        except (IndexError, TypeError): return ""

    ce_resp   = str(rv(0)).strip()
    pe_resp   = str(rv(1)).strip()
    ce_strike = _int(rv(2))
    pe_strike = _int(rv(3))

    locked = {"ce_locked": False, "pe_locked": False}

    # Clear range covers strike through confidence (C:H on the row)
    ce_data_range = f"{HEDGE_CE_STRIKE}:{HEDGE_CE_CONFIDENCE}"  # C19:H19
    pe_data_range = f"{HEDGE_PE_STRIKE}:{HEDGE_PE_CONFIDENCE}"  # C20:H20

    for side, resp, strike, resp_cell, data_range in [
        ("CE", ce_resp, ce_strike, CELL_CE_HEDGE_RESP, ce_data_range),
        ("PE", pe_resp, pe_strike, CELL_PE_HEDGE_RESP, pe_data_range),
    ]:
        key = f"{side.lower()}_locked"

        if resp == "✅ Done" and strike > 0:
            ws.update([["\u2705 Logged"]], resp_cell)
            locked[key] = True

        elif resp == "❌ Rejected":
            ws.update([["-", "-", "-", "-", "-", "-"]], data_range)
            ws.update([[""]], resp_cell)

        elif resp == "✅ Logged":
            locked[key] = True

    return locked


# ── S/R label formatting ─────────────────────────────────────────────────────

def _sr_label(entry: dict, for_sheet: bool = True) -> str:
    if for_sheet:
        return str(entry["strike"])
    base = f"{entry['strike']}  {entry['source']}"
    if entry.get("oi"):
        return f"{base}  OI:{entry['oi']/1e7:.1f}Cr"
    return base


# ── Write functions ───────────────────────────────────────────────────────────

def write_fast(ws: gspread.Worksheet, spot: float, vix_data: dict | None):
    """Fast-cycle write (every 15s): timestamp, B3–B6 spot and VIX."""
    now_str = datetime.now().strftime("%d-%b-%Y %H:%M:%S")
    updates = [
        {"range": CELL_TIMESTAMP, "values": [[f"Last refreshed: {now_str}"]]},
        {"range": CELL_SPOT_OUT,  "values": [[round(spot, 2)]]},
    ]

    if vix_data:
        updates += [
            {"range": CELL_VIX_OUT,     "values": [[round(vix_data["last"], 2)]]},
            {"range": CELL_VIX_CHG,     "values": [[round(vix_data["change"], 2)]]},
            {"range": CELL_VIX_CHG_PCT, "values": [[vix_data["pChange"] / 100]]},
        ]

    ws.batch_update(updates)


def write_risk(ws: gspread.Worksheet, ce_data: dict | None, pe_data: dict | None):
    """Slow-cycle write: risk scores, breakdown labels, and hedging status."""
    updates = []
    if ce_data:
        updates += [
            {"range": CELL_CE_RISK_SCORE,  "values": [[ce_data["score"]]]},
            {"range": CELL_CE_DELTA_LABEL, "values": [[ce_data["bd"]["delta"][1]]]},
            {"range": CELL_CE_IV_LABEL,    "values": [[ce_data["bd"]["iv"][1]]]},
            {"range": CELL_CE_PREM_LABEL,  "values": [[ce_data["bd"]["premium"][1]]]},
            {"range": CELL_CE_PROX_LABEL,  "values": [[ce_data["bd"]["proximity"][1]]]},
            {"range": CELL_CE_HEDGING,
             "values": [["Required" if ce_data["hedge"] else "Not Required"]]},
        ]
    if pe_data:
        updates += [
            {"range": CELL_PE_RISK_SCORE,  "values": [[pe_data["score"]]]},
            {"range": CELL_PE_DELTA_LABEL, "values": [[pe_data["bd"]["delta"][1]]]},
            {"range": CELL_PE_IV_LABEL,    "values": [[pe_data["bd"]["iv"][1]]]},
            {"range": CELL_PE_PREM_LABEL,  "values": [[pe_data["bd"]["premium"][1]]]},
            {"range": CELL_PE_PROX_LABEL,  "values": [[pe_data["bd"]["proximity"][1]]]},
            {"range": CELL_PE_HEDGING,
             "values": [["Required" if pe_data["hedge"] else "Not Required"]]},
        ]
    if updates:
        ws.batch_update(updates)


def write_hedge(ws: gspread.Worksheet, ce_hedge: dict | None,
                pe_hedge: dict | None, locked: dict):
    """Write hedge suggestion from compute_hedge_v2 output — only when not locked."""
    updates = []

    def _add(h, strike_c, qty_c, cost_c, worst_c, best_c, conf_c):
        if h:
            updates.extend([
                {"range": strike_c, "values": [[h["strike"]]]},
                {"range": qty_c,    "values": [[h["qty_lots"]]]},
                {"range": cost_c,   "values": [[h["cost"]]]},
                {"range": worst_c,  "values": [[h["worst_pnl"]]]},
                {"range": best_c,   "values": [[h["best_pnl"]]]},
                {"range": conf_c,   "values": [[f"{h['confidence']}%"]]},
            ])
        else:
            # Clear cells when no hedge needed
            for cell in (strike_c, qty_c, cost_c, worst_c, best_c, conf_c):
                updates.append({"range": cell, "values": [["-"]]})

    if not locked["ce_locked"]:
        _add(ce_hedge,
             HEDGE_CE_STRIKE, HEDGE_CE_QTY, HEDGE_CE_COST,
             HEDGE_CE_WORST_PNL, HEDGE_CE_BEST_PNL, HEDGE_CE_CONFIDENCE)

    if not locked["pe_locked"]:
        _add(pe_hedge,
             HEDGE_PE_STRIKE, HEDGE_PE_QTY, HEDGE_PE_COST,
             HEDGE_PE_WORST_PNL, HEDGE_PE_BEST_PNL, HEDGE_PE_CONFIDENCE)

    if updates:
        ws.batch_update(updates)


def write_sr(ws: gspread.Worksheet, supports: list, resistances: list):
    """Write S/R levels to sheet."""
    updates = []
    for i, s in enumerate(supports):
        updates.append({"range": CELL_SR_SUPPORTS[i], "values": [[_sr_label(s)]]})
    for i in range(len(supports), 3):
        updates.append({"range": CELL_SR_SUPPORTS[i], "values": [["-"]]})
    for i, r in enumerate(resistances):
        updates.append({"range": CELL_SR_RESISTS[i], "values": [[_sr_label(r)]]})
    for i in range(len(resistances), 3):
        updates.append({"range": CELL_SR_RESISTS[i], "values": [["-"]]})
    if updates:
        ws.batch_update(updates)


# ── Chart: matplotlib image generation ───────────────────────────────────────

def generate_range_chart(spot, calc_top, calc_bottom, vix_top, vix_bottom,
                         supports, resistances,
                         ce_strike, pe_strike,
                         ce_risk_data, pe_risk_data):
    """Generate a trade-risk-centric NIFTY chart.

    Left panel  — price axis with range bands, S/R, CMP, and trade strikes.
    Right panel — horizontal risk gauges for CE and PE with action labels.
    """
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from io import BytesIO

    # Need at least spot to draw anything meaningful
    if not spot or spot <= 0:
        return None

    ce_score = ce_risk_data["score"] if ce_risk_data else 0
    pe_score = pe_risk_data["score"] if pe_risk_data else 0
    ce_hedge = ce_risk_data["hedge"] if ce_risk_data else False
    pe_hedge = pe_risk_data["hedge"] if pe_risk_data else False

    # ── Figure: two panels ──
    fig = plt.figure(figsize=(10, 5), facecolor="white")
    ax_price = fig.add_axes([0.06, 0.08, 0.58, 0.84])   # left 60%
    ax_risk  = fig.add_axes([0.70, 0.08, 0.27, 0.84])   # right 30%

    # ── Price panel: auto-scale Y ──
    price_vals = [spot]
    if calc_top > 0:    price_vals.append(calc_top)
    if calc_bottom > 0: price_vals.append(calc_bottom)
    if vix_top > 0:     price_vals.append(vix_top)
    if vix_bottom > 0:  price_vals.append(vix_bottom)
    if ce_strike > 0:   price_vals.append(ce_strike)
    if pe_strike > 0:   price_vals.append(pe_strike)
    price_vals += [v for v in supports + resistances if v and v > 0]

    padding = (max(price_vals) - min(price_vals)) * 0.12 or 200
    y_min = min(price_vals) - padding
    y_max = max(price_vals) + padding
    ax_price.set_ylim(y_min, y_max)
    ax_price.set_xlim(0, 10)
    ax_price.xaxis.set_visible(False)

    # VIX offset zone (subtle amber background)
    if vix_bottom > 0 and vix_top > 0:
        ax_price.axhspan(vix_bottom, vix_top, alpha=0.15, color="#FF9800",
                         label="VIX Zone")
        ax_price.axhline(vix_top, color="#EF6C00", lw=0.8, ls=":", alpha=0.4)
        ax_price.axhline(vix_bottom, color="#EF6C00", lw=0.8, ls=":", alpha=0.4)

    # Calculated range (subtle grey band)
    if calc_bottom > 0 and calc_top > 0:
        ax_price.axhspan(calc_bottom, calc_top, alpha=0.18, color="#9E9E9E",
                         label="Calc Range")
        ax_price.axhline(calc_top, color="#757575", lw=0.8, ls=":", alpha=0.4)
        ax_price.axhline(calc_bottom, color="#757575", lw=0.8, ls=":", alpha=0.4)

    # Traded range (blue band between CE and PE strikes)
    if ce_strike > 0 and pe_strike > 0:
        band_top = max(ce_strike, pe_strike)
        band_bot = min(ce_strike, pe_strike)
        ax_price.axhspan(band_bot, band_top, alpha=0.14, color="#2196F3",
                         label="Traded Range")

    # Support levels (green dashed)
    for i, s in enumerate(supports):
        if s and s > 0:
            ax_price.axhline(s, color="#2E7D32", lw=1, ls="--", alpha=0.5)
            ax_price.text(0.2, s, f"S{i+1}: {s:.0f}", va="bottom",
                          ha="left", fontsize=10, color="#1B5E20",
                          fontweight="bold")

    # Resistance levels (red dashed)
    for i, r in enumerate(resistances):
        if r and r > 0:
            ax_price.axhline(r, color="#C62828", lw=1, ls="--", alpha=0.5)
            ax_price.text(0.2, r, f"R{i+1}: {r:.0f}", va="bottom",
                          ha="left", fontsize=10, color="#B71C1C",
                          fontweight="bold")

    # ── CE strike line (coral, prominent) ──
    if ce_strike > 0:
        ce_color = "#E53935" if ce_hedge else "#FF8A65"
        ax_price.axhline(ce_strike, color=ce_color, lw=2.2, alpha=0.85,
                         zorder=3)
        ce_tag = f"CE {ce_strike}  [{ce_score:.0f}]"
        if ce_hedge:
            ce_tag += "  EXIT!"
        ax_price.text(9.8, ce_strike, ce_tag, va="bottom", ha="right",
                      fontsize=11, fontweight="bold", color=ce_color,
                      bbox=dict(boxstyle="round,pad=0.3",
                                fc="#FFF3E0" if not ce_hedge else "#FFCDD2",
                                ec=ce_color, alpha=0.95), zorder=4)

    # ── PE strike line (teal, prominent) ──
    if pe_strike > 0:
        pe_color = "#00897B" if not pe_hedge else "#E53935"
        ax_price.axhline(pe_strike, color=pe_color, lw=2.2, alpha=0.85,
                         zorder=3)
        pe_tag = f"PE {pe_strike}  [{pe_score:.0f}]"
        if pe_hedge:
            pe_tag += "  EXIT!"
        ax_price.text(9.8, pe_strike, pe_tag, va="top", ha="right",
                      fontsize=11, fontweight="bold", color=pe_color,
                      bbox=dict(boxstyle="round,pad=0.3",
                                fc="#E0F2F1" if not pe_hedge else "#FFCDD2",
                                ec=pe_color, alpha=0.95), zorder=4)

    # ── CMP marker (bold dark line + diamond) ──
    ax_price.axhline(spot, color="#212121", lw=2.8, alpha=0.9, zorder=5)
    ax_price.plot(5, spot, marker="D", ms=10, color="#D32F2F",
                  markeredgecolor="#212121", markeredgewidth=1.2, zorder=6)
    ax_price.text(5.5, spot, f"CMP: {spot:.0f}", va="center", ha="left",
                  fontsize=12, fontweight="bold", color="#212121",
                  bbox=dict(boxstyle="round,pad=0.3", fc="white",
                            ec="#212121", lw=1.5, alpha=0.95), zorder=6)

    # ── Direction arrow between CMP and danger strike ──
    if ce_strike > 0 and pe_strike > 0:
        if ce_score > pe_score and ce_score > 30:
            ax_price.annotate("", xy=(3, ce_strike), xytext=(3, spot),
                              arrowprops=dict(arrowstyle="-|>", color="#E53935",
                                              lw=2.5, mutation_scale=20),
                              zorder=4)
            ax_price.text(3.4, (spot + ce_strike) / 2, "RISK",
                          fontsize=10, fontweight="bold", color="#E53935",
                          rotation=90, va="center", ha="left")
        elif pe_score > ce_score and pe_score > 30:
            ax_price.annotate("", xy=(3, pe_strike), xytext=(3, spot),
                              arrowprops=dict(arrowstyle="-|>", color="#E53935",
                                              lw=2.5, mutation_scale=20),
                              zorder=4)
            ax_price.text(3.4, (spot + pe_strike) / 2, "RISK",
                          fontsize=10, fontweight="bold", color="#E53935",
                          rotation=90, va="center", ha="left")

    # Price panel styling
    ax_price.set_title(CHART_TITLE, fontsize=14, fontweight="bold",
                       pad=10, color="#212121")
    ax_price.set_ylabel("NIFTY Level", fontsize=11, color="#424242")
    ax_price.tick_params(axis="y", labelsize=9)
    ax_price.grid(axis="y", alpha=0.15, ls=":")
    ax_price.spines["top"].set_visible(False)
    ax_price.spines["right"].set_visible(False)
    ax_price.spines["bottom"].set_visible(False)
    ax_price.legend(loc="upper center", bbox_to_anchor=(0.5, -0.02),
                    ncol=3, fontsize=9, framealpha=0.85,
                    edgecolor="#BDBDBD")

    # ══════════════════════════════════════════════════════════════════════
    # ── Right panel: Risk gauges ──
    # ══════════════════════════════════════════════════════════════════════
    ax_risk.set_xlim(0, 100)
    ax_risk.set_ylim(0, 10)
    ax_risk.axis("off")

    def _bar_color(score):
        if score < 35:  return "#43A047"   # green
        if score < 60:  return "#FB8C00"   # amber
        if score < 80:  return "#E53935"   # red
        return "#B71C1C"                    # dark red

    def _action_label(score, hedge):
        if hedge:       return "SQUARE OFF NOW", "#B71C1C"
        if score >= 65: return "EXIT / HEDGE", "#E53935"
        if score >= 40: return "MONITOR CLOSELY", "#FB8C00"
        return "SAFE", "#43A047"

    def _draw_gauge(y_center, label, score, hedge):
        bar_y = y_center
        bar_h = 0.7

        # Background track
        ax_risk.barh(bar_y, 100, height=bar_h, color="#EEEEEE",
                     edgecolor="#BDBDBD", linewidth=0.5)
        # Score fill
        ax_risk.barh(bar_y, score, height=bar_h, color=_bar_color(score),
                     edgecolor="none")

        # Label above bar
        ax_risk.text(0, bar_y + bar_h / 2 + 0.3, f"{label} RISK",
                     fontsize=13, fontweight="bold", color="#212121",
                     va="bottom")
        # Score value on bar
        ax_risk.text(max(score, 10), bar_y, f" {score:.0f}/100",
                     fontsize=12, fontweight="bold", va="center",
                     color="white" if score > 25 else "#212121")

        # Action label below bar
        action, act_color = _action_label(score, hedge)
        ax_risk.text(0, bar_y - bar_h / 2 - 0.2,
                     action, fontsize=11, fontweight="bold",
                     color=act_color, va="top")

    # CE gauge (upper half)
    if ce_strike > 0 and ce_risk_data:
        _draw_gauge(7.0, "CE", ce_score, ce_hedge)
    else:
        ax_risk.text(50, 7.0, "CE — No position", ha="center", va="center",
                     fontsize=12, color="#9E9E9E", style="italic")

    # PE gauge (lower half)
    if pe_strike > 0 and pe_risk_data:
        _draw_gauge(3.5, "PE", pe_score, pe_hedge)
    else:
        ax_risk.text(50, 3.5, "PE — No position", ha="center", va="center",
                     fontsize=12, color="#9E9E9E", style="italic")

    # Direction verdict at the bottom
    if ce_score > 0 or pe_score > 0:
        if ce_hedge and pe_hedge:
            verdict = "BOTH LEGS AT RISK"
            v_color = "#B71C1C"
        elif ce_hedge:
            verdict = "CE UNSAFE — EXIT"
            v_color = "#E53935"
        elif pe_hedge:
            verdict = "PE UNSAFE — EXIT"
            v_color = "#E53935"
        elif ce_score > pe_score + 15:
            verdict = "BEARISH — WATCH CE"
            v_color = "#E65100"
        elif pe_score > ce_score + 15:
            verdict = "BULLISH — WATCH PE"
            v_color = "#E65100"
        else:
            verdict = "BALANCED — OK"
            v_color = "#2E7D32"
        ax_risk.text(50, 0.8, verdict, ha="center", va="center",
                     fontsize=12, fontweight="bold", color="white",
                     bbox=dict(boxstyle="round,pad=0.5", fc=v_color,
                               ec=v_color, alpha=0.95))

    ax_risk.set_title("Risk Dashboard", fontsize=14, fontweight="bold",
                      pad=10, color="#212121")

    buf = BytesIO()
    fig.savefig(buf, format="png", dpi=96, bbox_inches="tight",
                facecolor="white")
    plt.close(fig)
    buf.seek(0)
    return buf.read()


# ── Chart: Google Cloud Storage upload ────────────────────────────────────────

def _upload_chart_image(image_bytes: bytes):
    """Upload (or overwrite) chart PNG to GCS. Always the same object name."""
    import requests as req
    from google.auth.transport.requests import Request as AuthRequest

    creds = Credentials.from_service_account_file(
        SERVICE_ACCOUNT_FILE,
        scopes=["https://www.googleapis.com/auth/devstorage.read_write"],
    )
    creds.refresh(AuthRequest())

    resp = req.post(
        f"https://storage.googleapis.com/upload/storage/v1/b/"
        f"{GCS_BUCKET}/o",
        params={"uploadType": "media", "name": GCS_OBJECT_NAME},
        headers={"Authorization": f"Bearer {creds.token}",
                 "Content-Type": "image/png"},
        data=image_bytes,
    )
    resp.raise_for_status()


# ── Chart: orchestrator ──────────────────────────────────────────────────────

def setup_chart_cell(ws: gspread.Worksheet):
    """Prepare the chart display cell (merge + row height). Runs once."""
    sid = ws.id
    ws.spreadsheet.batch_update({"requests": [
        # Set rows 22-26 height (100px each = 500px total for the chart)
        {"updateDimensionProperties": {
            "range": {"sheetId": sid, "dimension": "ROWS",
                      "startIndex": 21, "endIndex": 26},
            "properties": {"pixelSize": 100},
            "fields": "pixelSize",
        }},
        # Merge B22:I26 so the image has a proper aspect-ratio cell
        {"mergeCells": {
            "range": {"sheetId": sid,
                      "startRowIndex": 21, "endRowIndex": 26,
                      "startColumnIndex": 1, "endColumnIndex": 9},
            "mergeType": "MERGE_ALL",
        }},
    ]})
    print("  Chart cell prepared (B22:I26 merged, row heights set)")


def update_chart(ws: gspread.Worksheet, spot: float,
                 supports: list, resistances: list,
                 ce_strike: int, pe_strike: int,
                 ce_risk_data: dict | None, pe_risk_data: dict | None,
                 chart_file_id: str | None) -> str | None:
    """Read range data, generate chart image, upload, write =IMAGE() formula.
    Returns the Drive file_id for subsequent updates."""
    # Read calculated range + VIX values from sheet (E3, E4, E7, E8)
    raw = ws.batch_get(CHART_CALC_CELLS)
    def _rv(i):
        try: return _float(raw[i][0][0])
        except (IndexError, TypeError): return 0.0
    calc_top    = _rv(0)
    calc_bottom = _rv(1)
    vix_top     = _rv(2)
    vix_bottom  = _rv(3)

    sup_vals = [s["strike"] for s in supports] if supports else []
    res_vals = [r["strike"] for r in resistances] if resistances else []

    image_bytes = generate_range_chart(
        spot, calc_top, calc_bottom, vix_top, vix_bottom,
        sup_vals, res_vals,
        ce_strike, pe_strike,
        ce_risk_data, pe_risk_data,
    )
    if not image_bytes:
        return chart_file_id

    try:
        _upload_chart_image(image_bytes)
    except Exception as e:
        print(f"  [WARN] Chart upload failed: {e}")
        return chart_file_id

    # GCS public URL — serves raw PNG bytes, no redirects/interstitials.
    # Cache-buster param forces Sheets to re-fetch on each cycle.
    img_url = (f"https://storage.googleapis.com/{GCS_BUCKET}/"
               f"{GCS_OBJECT_NAME}?t={int(time.time())}")

    ws.update([[f'=IMAGE("{img_url}",4,500,800)']], CHART_CELL,
              value_input_option="USER_ENTERED")

    print(f"  Chart updated -> {img_url}")
    return True


# ── Sheet2: Option Chain ─────────────────────────────────────────────────────

def populate_trade_entry(ws: gspread.Worksheet, ws2: gspread.Worksheet,
                         prev_ce_strike: int, prev_pe_strike: int) -> tuple[int, int]:
    """
    Check C14/C15 for strike changes.  When a strike changes, look it up in
    Sheet2 column E and populate entry IV & delta from Sheet2 (one-time snapshot).
    Returns (current_ce_strike, current_pe_strike) for next-cycle comparison.
    """
    raw = ws.batch_get([CELL_CE_STRIKE_IN, CELL_PE_STRIKE_IN])
    ce_strike = _int(raw[0][0][0]) if raw[0] and raw[0][0] else 0
    pe_strike = _int(raw[1][0][0]) if raw[1] and raw[1][0] else 0

    if ce_strike == prev_ce_strike and pe_strike == prev_pe_strike:
        return ce_strike, pe_strike  # nothing changed

    # Read Sheet2 option chain data (A:I layout)
    # A=CE OI, B=CE IV, C=LTP, D=CE Delta, E=Strike, F=PE Delta, G=LTP, H=PE IV, I=PE OI
    oc_data = ws2.get(f"A{OC_DATA_START}:I{OC_DATA_END}")
    # Build strike lookup: strike -> {ce_iv, ce_delta, pe_delta, pe_iv}
    oc_lookup = {}
    for row in (oc_data or []):
        if len(row) >= 8:
            s = _int(row[4])  # col E = Strike (index 4 in A:I)
            if s:
                oc_lookup[s] = {
                    "ce_iv":    row[1],  # col B
                    "ce_delta": row[3],  # col D
                    "pe_delta": row[5],  # col F
                    "pe_iv":    row[7],  # col H
                }

    updates = []

    if ce_strike and ce_strike != prev_ce_strike:
        entry = oc_lookup.get(ce_strike)
        if entry:
            updates.append({"range": CELL_CE_ENTRY_IV,    "values": [[entry["ce_iv"]]]})
            updates.append({"range": CELL_CE_ENTRY_DELTA, "values": [[entry["ce_delta"]]]})
            print(f"  CE entry snapshot: strike {ce_strike} -> IV={entry['ce_iv']}, delta={entry['ce_delta']}")

    if pe_strike and pe_strike != prev_pe_strike:
        entry = oc_lookup.get(pe_strike)
        if entry:
            updates.append({"range": CELL_PE_ENTRY_IV,    "values": [[entry["pe_iv"]]]})
            updates.append({"range": CELL_PE_ENTRY_DELTA, "values": [[entry["pe_delta"]]]})
            print(f"  PE entry snapshot: strike {pe_strike} -> IV={entry['pe_iv']}, delta={entry['pe_delta']}")

    if updates:
        ws.batch_update(updates)

    return ce_strike, pe_strike


def write_option_chain(ws2: gspread.Worksheet, last_oc_expiry: str):
    """
    Read expiry from E2 of Sheet2, fetch chain, pivot CE/PE side-by-side,
    and write to A4:I86.  Returns (new_expiry_str, spot) so caller can
    track whether the expiry changed.
    """
    expiry_val = str(ws2.acell(OC_EXPIRY_CELL).value or "").strip()

    if not expiry_val:
        return last_oc_expiry, 0.0

    # Fetch chain — catch bad expiry
    try:
        chain_df, _, spot = build_chain("NIFTY", expiry_val, RISK_FREE_RATE)
        # Clear any previous error
        ws2.update_acell(OC_ERROR_CELL, "")
    except Exception as e:
        ws2.update_acell(OC_ERROR_CELL, f"Invalid expiry: {expiry_val}")
        print(f"  [WARN] Sheet2 bad expiry '{expiry_val}': {e}")
        return expiry_val, 0.0

    # Find ATM strike (closest to spot)
    strikes_all = sorted(chain_df["Strike"].unique())
    atm = min(strikes_all, key=lambda s: abs(s - spot))
    atm_idx = strikes_all.index(atm)

    # Select OC_STRIKES strikes each side of ATM (inclusive)
    lo = max(0, atm_idx - OC_STRIKES)
    hi = min(len(strikes_all), atm_idx + OC_STRIKES + 1)
    selected = strikes_all[lo:hi]

    # Build CE and PE lookup dicts keyed by strike
    ce_map, pe_map = {}, {}
    for _, r in chain_df.iterrows():
        s = r["Strike"]
        if s not in selected:
            continue
        if r["Type"] == "CE":
            ce_map[s] = r
        else:
            pe_map[s] = r

    # Build rows: CE OI | CE IV | LTP | CE DELTA | Strike | PE DELTA | LTP | PE IV | PE OI
    def _safe(val, decimals=2):
        """Convert to rounded float, replace NaN with empty string."""
        import math
        v = float(val)
        if math.isnan(v):
            return ""
        return round(v, decimals)

    def _safe_int(val):
        """Convert OI to int, replace NaN/0 with empty string."""
        import math
        v = float(val)
        if math.isnan(v) or v == 0:
            return ""
        return int(v)

    max_rows = OC_DATA_END - OC_DATA_START + 1  # 82
    grid = []
    for strike in selected[:max_rows]:
        ce = ce_map.get(strike)
        pe = pe_map.get(strike)
        grid.append([
            _safe_int(ce["OI"])    if ce is not None else "",
            _safe(ce["IV_%"], 2)   if ce is not None else "",
            _safe(ce["LTP"], 2)    if ce is not None else "",
            _safe(ce["delta"], 2)  if ce is not None else "",
            int(strike),
            _safe(pe["delta"], 2)  if pe is not None else "",
            _safe(pe["LTP"], 2)    if pe is not None else "",
            _safe(pe["IV_%"], 2)   if pe is not None else "",
            _safe_int(pe["OI"])    if pe is not None else "",
        ])

    # Pad remaining rows with blanks so stale data is cleared
    while len(grid) < max_rows:
        grid.append(["", "", "", "", "", "", "", "", ""])

    # Header + data in one batch
    header = [["CE OI", "CE IV", "LTP", "CE DELTA", "Strike", "PE DELTA", "LTP", "PE IV", "PE OI"]]
    ws2.update(header + grid, f"A{OC_HEADER_ROW}:I{OC_DATA_END}",
               value_input_option="RAW")

    print(f"  Sheet2 option chain updated ({len(selected)} strikes)")
    return expiry_val, spot


# ── Console summary ───────────────────────────────────────────────────────────

def print_summary(spot: float, vix_data: dict | None,
                  sr_supports: list | None = None,
                  sr_resistances: list | None = None):
    ts = datetime.now().strftime("%H:%M:%S")
    vix_str = (f"  VIX: {vix_data['last']:.2f} ({vix_data['change']:+.2f})"
               if vix_data else "")
    print(f"\n[{ts}]  Spot: {spot:.2f}{vix_str}")

    if sr_supports:
        sup_str = "  |  ".join(_sr_label(s, for_sheet=False) for s in sr_supports)
        print(f"  Supports   : {sup_str}")
    if sr_resistances:
        res_str = "  |  ".join(_sr_label(r, for_sheet=False) for r in sr_resistances)
        print(f"  Resistances: {res_str}")


# ── Main loop ─────────────────────────────────────────────────────────────────

def main():
    from pnsea import NSE

    print("Connecting to Google Sheets …")
    ws, ws2 = connect_sheets()

    print("Setting up chart cell …")
    setup_chart_cell(ws)
    chart_file_id = None

    print("Warming NSE session …")
    nse = NSE()
    nse.equity.market_status()

    cycle         = 0
    config        = None
    cached_sr     = load_sr_from_sheet(ws)
    sr_written_times = set()
    last_date     = date.today()
    last_oc_time  = 0.0         # timestamp of last Sheet2 option chain write
    last_oc_expiry = ""
    prev_ce_strike = 0          # track C14/C15 changes for entry snapshots
    prev_pe_strike = 0

    prev_oi_snapshot = None       # for directional signal OI buildup factor
    cached_pivot_sr = ([], [])    # Pivot+OI S/R for chart
    active_trade    = None        # active directional trade suggestion (frozen)
    trade_exited    = False       # True while EXIT is showing on L9:L10

    print(f"Loaded {len(cached_sr[0])} supports, {len(cached_sr[1])} resistances from sheet.")
    print(f"Fast interval: {FAST_INTERVAL}s | Slow cycle: every {SLOW_MULTIPLIER} fast cycles")
    print("-" * 60)

    while True:
        try:
            cycle += 1
            now        = datetime.now()
            now_time   = now.strftime("%H:%M")
            today      = now.date()
            is_weekday = today.weekday() < 5
            is_slow    = (cycle == 1 or cycle % SLOW_MULTIPLIER == 0)

            # Reset daily tracking on new day
            if today != last_date:
                sr_written_times.clear()
                active_trade = None
                trade_exited = False
                last_date = today

            # ── 1. Read config (slow cycle or first run) ────────────────
            if is_slow or config is None:
                config = read_config(ws)
                if not config["expiry"]:
                    print("[WARN] No expiry set — waiting 60s …")
                    time.sleep(60)
                    continue

            # ── 2. Fetch option chain ───────────────────────────────────
            print(f"Fetching {config['expiry']} chain …", end=" ", flush=True)
            chain_df, _, spot = build_chain(
                "NIFTY", config["expiry"], RISK_FREE_RATE)
            print("done")

            # ── 3. Fetch VIX ─────────────────────────────────────────────
            vix_data = fetch_vix(nse)
            if vix_data is None:
                try:
                    nse = NSE()
                    nse.equity.market_status()
                    vix_data = fetch_vix(nse)
                except Exception:
                    pass

            # ── 4. Write fast updates (every cycle) ──────────────────────
            write_fast(ws, spot, vix_data)

            # ── 5b. Trade entry snapshot (check C14/C15 changes) ─────────
            prev_ce_strike, prev_pe_strike = populate_trade_entry(
                ws, ws2, prev_ce_strike, prev_pe_strike)

            # ── 5c. Compute DTE (needed by directional signal & risk) ────
            try:
                exp_dt = datetime.strptime(config["expiry"], "%d-%b-%Y").date()
                dte = max((exp_dt - today).days, 0)
            except Exception:
                dte = 30

            # ── 5d. Directional signal with trade lifecycle ──────────────
            # Compute signal on same schedule as before:
            #   In signal window → every cycle
            #   Outside window   → slow cycles only
            should_compute = (_in_signal_window(now)
                              or is_slow)

            if should_compute:
                try:
                    signal = compute_directional_signal(
                        chain_df, spot, vix_data, prev_oi_snapshot)
                    prev_oi_snapshot = _snapshot_oi(chain_df, spot)

                    if active_trade and not trade_exited:
                        # ── Active trade: check exit conditions ─────
                        tr_row = chain_df[
                            (chain_df["Strike"] == active_trade["strike"]) &
                            (chain_df["Type"] == active_trade["type"])]
                        cur_ltp = (float(tr_row.iloc[0]["LTP"])
                                   if not tr_row.empty else 0.0)

                        sl_hit = (cur_ltp > 0
                                  and cur_ltp <= active_trade["sl_price"])
                        scoring_drop = signal["factors_agree"] < 5

                        if sl_hit or scoring_drop:
                            reason = ("SL hit" if sl_hit
                                      else "scoring drop")
                            exit_time = now.strftime("%I:%M %p").lstrip("0")
                            write_signal_exit(ws, active_trade,
                                              cur_ltp, exit_time, reason)
                            print(f"  Trade EXIT: {active_trade['strike']}"
                                  f"{active_trade['type']} @{cur_ltp:.0f}"
                                  f" | {reason}")
                            trade_exited = True
                            active_trade = None

                    elif trade_exited or active_trade is None:
                        # ── No active trade: look for new entry ─────
                        if (signal["factors_agree"] >= 5
                                and signal["direction"] != "NEUTRAL"):
                            trade_sug = select_trade_strike(
                                chain_df, spot, signal["direction"], dte)
                            if trade_sug:
                                entry_time = now.strftime(
                                    "%I:%M %p").lstrip("0")
                                sl_price = round(
                                    trade_sug["ltp"] * 0.75, 2)
                                active_trade = {
                                    "direction": signal["direction"],
                                    "strike": trade_sug["strike"],
                                    "type": trade_sug["type"],
                                    "entry_ltp": trade_sug["ltp"],
                                    "sl_price": sl_price,
                                    "entry_time": entry_time,
                                }
                                trade_exited = False
                                write_signal_entry(
                                    ws, signal, trade_sug,
                                    entry_time, sl_price)
                                print(
                                    f"  Signal ENTRY: "
                                    f"{signal['direction']} "
                                    f"({signal['factors_agree']}/8) "
                                    f"BUY {trade_sug['strike']}"
                                    f"{trade_sug['type']} "
                                    f"@{trade_sug['ltp']:.0f} "
                                    f"SL:{sl_price:.0f}")
                        elif not trade_exited:
                            # No qualifying signal and no EXIT showing
                            write_signal_neutral(ws, signal)

                except Exception as e:
                    print(f"  [WARN] Directional signal failed: {e}")
            else:
                prev_oi_snapshot = _snapshot_oi(chain_df, spot)

            # ── 6. S/R refresh at scheduled times on weekdays ────────────
            first_run_needs_sr = (cycle == 1)
            sr_time_match = (now_time in SR_REFRESH_TIMES
                             and is_weekday
                             and now_time not in sr_written_times)

            if first_run_needs_sr or sr_time_match:
                if sr_time_match:
                    sr_written_times.add(now_time)
                print("  Refreshing S/R levels …")
                oi_levels = find_oi_sr_levels(
                    chain_df, spot,
                    config["worst_case_ce"], config["worst_case_pe"])
                fib_levels = fetch_fibonacci_levels()
                top3_sup, top3_res = merge_sr_levels(
                    oi_levels["oi_supports"], oi_levels["oi_resistances"],
                    fib_levels, spot,
                )
                cached_sr = (top3_sup, top3_res)
                write_sr(ws, top3_sup, top3_res)

                # Pivot+OI S/R for I4:J6
                print("  Refreshing Pivot+OI S/R …")
                pivot_data = fetch_pivot_levels()
                if pivot_data:
                    pivot_sup, pivot_res = compute_pivot_oi_sr(
                        chain_df, spot, pivot_data)
                    write_pivot_sr(ws, pivot_sup, pivot_res)
                    cached_pivot_sr = (pivot_sup, pivot_res)

            # ── 7. Risk meter (slow cycle) ────────────────────────────────
            ce_risk_data = pe_risk_data = None
            if is_slow and (prev_ce_strike or prev_pe_strike):
                trade_cells = ws.batch_get([
                    CELL_CE_STRIKE_IN, CELL_PE_STRIKE_IN,
                    CELL_CE_ENTRY_IV, CELL_PE_ENTRY_IV,
                    CELL_CE_ENTRY_PREM, CELL_PE_ENTRY_PREM,
                ])
                def _tv(i):
                    try: return trade_cells[i][0][0]
                    except (IndexError, TypeError): return ""

                ce_strike = _int(_tv(0))
                pe_strike = _int(_tv(1))
                ce_entry_iv = _float(_tv(2))
                pe_entry_iv = _float(_tv(3))
                ce_entry_prem = _float(_tv(4))
                pe_entry_prem = _float(_tv(5))

                vix_abs = vix_data["last"] if vix_data else 0.0
                vix_chg = abs(vix_data["pChange"]) if vix_data else 0.0

                # dte already computed in step 5c

                ce_risk_data = pe_risk_data = None

                if ce_strike:
                    ce_row = chain_df[
                        (chain_df["Strike"] == ce_strike) &
                        (chain_df["Type"] == "CE")]
                    if not ce_row.empty:
                        r = ce_row.iloc[0]
                        score, bd, hedge = compute_risk_score(
                            delta=float(r["delta"]),
                            gamma=float(r.get("gamma", 0)),
                            theta=float(r.get("theta", 0)),
                            entry_iv=ce_entry_iv,
                            current_iv=float(r["IV_%"]),
                            entry_premium=ce_entry_prem,
                            current_ltp=float(r["LTP"]),
                            current_spot=spot,
                            strike=ce_strike,
                            dte=dte,
                            vix_abs=vix_abs,
                            vix_chg_pct=vix_chg,
                        )
                        ce_risk_data = {"score": score, "bd": bd, "hedge": hedge}

                if pe_strike:
                    pe_row = chain_df[
                        (chain_df["Strike"] == pe_strike) &
                        (chain_df["Type"] == "PE")]
                    if not pe_row.empty:
                        r = pe_row.iloc[0]
                        score, bd, hedge = compute_risk_score(
                            delta=float(r["delta"]),
                            gamma=float(r.get("gamma", 0)),
                            theta=float(r.get("theta", 0)),
                            entry_iv=pe_entry_iv,
                            current_iv=float(r["IV_%"]),
                            entry_premium=pe_entry_prem,
                            current_ltp=float(r["LTP"]),
                            current_spot=spot,
                            strike=pe_strike,
                            dte=dte,
                            vix_abs=vix_abs,
                            vix_chg_pct=vix_chg,
                        )
                        pe_risk_data = {"score": score, "bd": bd, "hedge": hedge}

                write_risk(ws, ce_risk_data, pe_risk_data)

                # ── 7a. Hedge suggestions (when risk = Required) ─────────
                locked = handle_hedge_responses(ws)
                ce_hedge = pe_hedge = None

                if ce_risk_data and ce_risk_data["hedge"] and not locked["ce_locked"]:
                    ce_hedge = compute_hedge_v2(
                        side="CE", sold_strike=ce_strike,
                        entry_premium=ce_entry_prem,
                        current_spot=spot, chain_df=chain_df,
                        dte=dte, supports=cached_sr[0],
                        resistances=cached_sr[1], lot_size=LOT_SIZE,
                    )
                    if ce_hedge:
                        print(f"  CE hedge: BUY {ce_hedge['strike']} "
                              f"×{ce_hedge['qty_lots']}L "
                              f"conf={ce_hedge['confidence']}%")

                if pe_risk_data and pe_risk_data["hedge"] and not locked["pe_locked"]:
                    pe_hedge = compute_hedge_v2(
                        side="PE", sold_strike=pe_strike,
                        entry_premium=pe_entry_prem,
                        current_spot=spot, chain_df=chain_df,
                        dte=dte, supports=cached_sr[0],
                        resistances=cached_sr[1], lot_size=LOT_SIZE,
                    )
                    if pe_hedge:
                        print(f"  PE hedge: BUY {pe_hedge['strike']} "
                              f"×{pe_hedge['qty_lots']}L "
                              f"conf={pe_hedge['confidence']}%")

                write_hedge(ws, ce_hedge, pe_hedge, locked)

            # ── 7b. Chart image update (slow cycle) ──────────────────────
            if is_slow:
                chart_file_id = update_chart(
                    ws, spot, cached_pivot_sr[0], cached_pivot_sr[1],
                    prev_ce_strike, prev_pe_strike,
                    ce_risk_data, pe_risk_data, chart_file_id)

            # ── 8. Sheet2 option chain (every 30s) ───────────────────────
            now_ts = time.time()
            if now_ts - last_oc_time >= OC_INTERVAL:
                last_oc_expiry, _ = write_option_chain(ws2, last_oc_expiry)
                last_oc_time = now_ts

            # ── 9. Console output ─────────────────────────────────────────
            print_summary(spot, vix_data, cached_sr[0], cached_sr[1])

        except Exception as e:
            print(f"[ERROR] {e}")
            traceback.print_exc()

        time.sleep(FAST_INTERVAL)


if __name__ == "__main__":
    main()
