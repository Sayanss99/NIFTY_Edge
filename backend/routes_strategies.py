"""Strategy Builder API routes — market analytics, strategy rankings, and multi-leg strategies."""

from datetime import datetime, timezone, timedelta
from typing import List, Optional
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session
from sqlalchemy import desc as _desc
from pydantic import BaseModel
from database import get_db
from models import Trade, TradeStatus, User, Strategy, IVDailyRecord, IVSkewDaily, MarketSnapshot
from auth import get_current_user
from config import LOT_SIZE, RISK_FREE_RATE

_IST = timezone(timedelta(hours=5, minutes=30))

router = APIRouter(prefix="/api/strategies", tags=["strategies"])


# ── Pydantic schemas ──────────────────────────────────────────────────────────

class StrategyLeg(BaseModel):
    strike: int
    option_type: str   # CE or PE
    action: str        # BUY or SELL
    lots: int = 1
    expiry: Optional[str] = None   # per-leg expiry (calendars); falls back to request expiry

class CreateStrategyRequest(BaseModel):
    strategy_type: str
    name: Optional[str] = None
    expiry_date: str
    legs: List[StrategyLeg]


# ── Helpers ───────────────────────────────────────────────────────────────────

def _get_iv_rank_data(db: Session, days: int = 252):
    """Fetch IV rank metrics from DB. Matches IVRankAnalyzer.compute() logic."""
    import statistics
    rows = db.query(IVDailyRecord).order_by(_desc(IVDailyRecord.date)).limit(days).all()
    if len(rows) < 5:
        return {
            "iv_rank": 50.0, "iv_percentile": 50.0, "current_atm_iv": None,
            "iv_52wk_high": None, "iv_52wk_low": None, "iv_mean": None,
            "iv_median": None, "data_days": len(rows),
        }
    vals = [r.atm_iv for r in rows]
    cur = vals[0]
    iv_max, iv_min = max(vals), min(vals)
    n = len(vals)
    iv_mean = sum(vals) / n
    iv_median = statistics.median(vals)
    iv_rank = (cur - iv_min) / (iv_max - iv_min) * 100 if iv_max > iv_min else 50.0
    iv_pct = sum(1 for v in vals if v < cur) / n * 100
    return {
        "iv_rank": round(iv_rank, 1),
        "iv_percentile": round(iv_pct, 1),
        "current_atm_iv": round(cur, 2),
        "iv_52wk_high": round(iv_max, 2),
        "iv_52wk_low": round(iv_min, 2),
        "iv_mean": round(iv_mean, 2),
        "iv_median": round(iv_median, 2),
        "data_days": n,
    }


def _compute_pcr(chain_df):
    """Put-Call Ratio by OI."""
    ce_oi = chain_df.loc[chain_df["Type"] == "CE", "OI"].sum()
    pe_oi = chain_df.loc[chain_df["Type"] == "PE", "OI"].sum()
    return round(pe_oi / ce_oi, 3) if ce_oi > 0 else 1.0


def _compute_vol_pcr(chain_df, spot):
    """Volume PCR near ATM."""
    atm = round(spot / 50) * 50
    near = chain_df[(chain_df["Strike"] >= atm - 200) & (chain_df["Strike"] <= atm + 200)]
    ce_vol = near.loc[near["Type"] == "CE", "Volume"].sum()
    pe_vol = near.loc[near["Type"] == "PE", "Volume"].sum()
    return round(pe_vol / ce_vol, 3) if ce_vol > 0 else 1.0


def _compute_gex(chain_df, spot):
    """Full GEX analysis: per-strike breakdown, gamma flip, regime.

    Formula (matching module5_analytics.py GammaExposure.compute()):
      call_gex = ce_gamma × ce_oi × LOT_SIZE × spot × 0.01
      put_gex  = pe_gamma × pe_oi × LOT_SIZE × spot × 0.01
      net_gex  = call_gex - put_gex  (positive = dealers long gamma = mean-reverting)
    Returns total_gex, gamma_flip strike, regime label, top ±3 strikes.
    """
    atm = round(spot / 50) * 50
    near = chain_df[(chain_df["Strike"] >= atm - 500) & (chain_df["Strike"] <= atm + 500)]

    gex_by_strike: dict[int, float] = {}
    for _, row in near.iterrows():
        strike = int(row["Strike"])
        g = float(row.get("gamma", 0))
        oi = float(row.get("OI", 0))
        contrib = g * oi * LOT_SIZE * spot * 0.01
        if row["Type"] == "CE":
            gex_by_strike[strike] = gex_by_strike.get(strike, 0.0) + contrib
        else:
            gex_by_strike[strike] = gex_by_strike.get(strike, 0.0) - contrib

    total_gex = sum(gex_by_strike.values())

    # Gamma flip: lowest strike where cumulative GEX sign flips
    strikes_sorted = sorted(gex_by_strike.keys())
    gamma_flip = None
    for i in range(len(strikes_sorted) - 1):
        g_cur = gex_by_strike[strikes_sorted[i]]
        g_nxt = gex_by_strike[strikes_sorted[i + 1]]
        if (g_cur >= 0 and g_nxt < 0) or (g_cur < 0 and g_nxt >= 0):
            gamma_flip = strikes_sorted[i]
            break

    sorted_by_gex = sorted(gex_by_strike.items(), key=lambda x: x[1], reverse=True)
    top_pos = [{"strike": k, "gex": round(v, 2)} for k, v in sorted_by_gex if v > 0][:3]
    top_neg = [{"strike": k, "gex": round(v, 2)} for k, v in reversed(sorted_by_gex) if v < 0][:3]

    if total_gex > 0:
        regime = "Mean-Reverting"
        regime_desc = "Dealers long gamma — buy dips, sell rallies (range-bound expected)"
    else:
        regime = "Trending"
        regime_desc = "Dealers short gamma — amplify moves (breakout/breakdown risk)"

    return {
        "total_gex": round(total_gex, 2),
        "gamma_flip": gamma_flip,
        "regime": regime,
        "regime_desc": regime_desc,
        "top_positive": top_pos,
        "top_negative": top_neg,
    }


def _compute_iv_skew(chain_df, spot):
    """OTM PE avg IV - OTM CE avg IV. Positive = puts expensive = fear."""
    atm = round(spot / 50) * 50
    otm_ce = chain_df[(chain_df["Type"] == "CE") & (chain_df["Strike"] >= atm + 100) & (chain_df["Strike"] <= atm + 500)]
    otm_pe = chain_df[(chain_df["Type"] == "PE") & (chain_df["Strike"] <= atm - 100) & (chain_df["Strike"] >= atm - 500)]
    ce_avg = float(otm_ce["IV"].mean()) if not otm_ce.empty else 0.0
    pe_avg = float(otm_pe["IV"].mean()) if not otm_pe.empty else 0.0
    return round(pe_avg - ce_avg, 2)


def _compute_oi_sr(chain_df):
    """Max CE OI strike = resistance, Max PE OI strike = support."""
    ce = chain_df[chain_df["Type"] == "CE"]
    pe = chain_df[chain_df["Type"] == "PE"]
    resistance = int(ce.loc[ce["OI"].idxmax(), "Strike"]) if not ce.empty else 0
    support = int(pe.loc[pe["OI"].idxmax(), "Strike"]) if not pe.empty else 0
    return resistance, support


def _compute_rr_zscore(db: Session, current_rr: float, lookback: int = 60) -> dict:
    """Compute z-score of current RR vs historical 60-day distribution from IVSkewDaily."""
    import math
    rows = (
        db.query(IVSkewDaily)
        .filter(IVSkewDaily.risk_reversal.isnot(None))
        .order_by(_desc(IVSkewDaily.date))
        .limit(lookback)
        .all()
    )
    if len(rows) < 5:
        return {"z_score": None, "hist_mean": None, "hist_std": None, "classification": "Insufficient history"}

    rr_vals = [r.risk_reversal for r in rows]
    hist_mean = sum(rr_vals) / len(rr_vals)
    variance = sum((v - hist_mean) ** 2 for v in rr_vals) / len(rr_vals)
    hist_std = math.sqrt(variance) if variance > 0 else 0.0

    z = round((current_rr - hist_mean) / hist_std, 2) if hist_std > 0 else 0.0

    if z < -2.0:
        classification = "Extreme bearish skew (puts very expensive)"
    elif z < -1.0:
        classification = "Elevated bearish skew"
    elif z > 2.0:
        classification = "Extreme bullish skew (calls very expensive)"
    elif z > 1.0:
        classification = "Elevated bullish skew"
    else:
        classification = "Normal skew range"

    return {
        "z_score": z,
        "hist_mean": round(hist_mean, 2),
        "hist_std": round(hist_std, 2),
        "days_used": len(rows),
        "classification": classification,
    }


def compute_market_verdict(d: dict) -> dict:
    """Centralized "Market Verdict / Outlook" scoring — single source of truth
    consumed by both the Dashboard's Market Verdict card and the Strategy
    Builder's Market Outlook panel.  The two pages MUST never disagree, so
    the logic lives here on the backend rather than being duplicated in JS.

    Each factor is a fixed mathematical comparison against a threshold derived
    from option-market mechanics — there are no situational hard-codes, no
    look-up tables, and no learned weights.  Factors fire only when the data
    is *materially* on one side of the boundary (no zero-noise triggers), and
    every comparison defends against missing inputs so an absent field can
    never silently fire as a one-sided factor.

    ── Bull factors (each fires only when data is present) ──────────────────
      1. (bullish_count − bearish_count) ≥ 2  — dominance in the 8-factor
         directional sub-signal (mathematically equivalent to "the underlying
         8-factor system has at least two more bullish votes than bearish").
      2. pcr_oi > 1.10  — put OI ≥ 110% of call OI; symmetric ±0.1 around 1.0
         where 1.0 is the no-bias point.
      3. vol_pcr > 1.20 — put volume ≥ 120% of call volume; tightened from
         the legacy 1.0 to give a properly symmetric ±0.2 band around the
         empirical 1.0 mean (NIFTY structural retail-call bias means most
         days vol_pcr > 0.8, so 1.0 was too easy a threshold).
      4. gex > +5e5    — positive net gamma above 500K (matches the existing
         narrative "+GEX (mean-reverting)" threshold; eliminates noise hits
         around zero).
      5. rr_z > +1.0   — RR ≥ 1σ above its own 60-day mean.  Z-score is
         already normalised so structural NIFTY put-skew is absorbed into
         the mean — ±1σ is the symmetric, statistically defensible band.
      6. vix_change_pct < -3%  — material VIX drop (fear subsiding).
      7. term_structure = Contango — front IV ≤ next-month IV (normal curve).
      8. (spot − max_pain) < -50  — spot ≥ 50 pts below max-pain → mechanical
         upward pull.  -50/+100 asymmetry below encodes NIFTY's documented
         "drift up to max pain" tendency (writers' floor effect).

    ── Bear factors (each fires only when data is present) ──────────────────
      1. (bearish_count − bullish_count) ≥ 2  — mirror dominance.
      2. pcr_oi < 0.90  — symmetric mirror of #2 above.
      3. vol_pcr < 0.80 — symmetric mirror of #3 above.
      4. gex < -5e5     — symmetric mirror of #4 above.
      5. rr_z < -1.0    — symmetric (was −1.5; corrected to remove the
         hidden bull-bias of the legacy asymmetric thresholds).
      6. vix_change_pct > +5%  — VIX surge (asymmetric vs −3% above
         because empirical vol asymmetry: VIX rises faster than it falls,
         so a higher threshold for "material spike" is justified).
      7. iv_skew > 3    — elevated put-wing IV.  Bull has no mirror because
         elevated call skew is already captured by rr_z > 1 (#5).  This is
         the only deliberate factor-count asymmetry (9 bear vs 8 bull).
      8. term_structure = Backwardation — front IV > next-month IV (stress).
      9. (spot − max_pain) > +100 — spot well above max pain → downward
         pull.  +100 vs the bull's −50 reflects NIFTY's asymmetric drift.

    ── Verdict tiers ────────────────────────────────────────────────────────
    Both an *absolute floor* and a *dominance gap* are required, so a 3-vs-2
    near-tie cannot masquerade as a directional call:

      Mildly Bullish/Bearish (strong) : score ≥ 3 AND |diff| ≥ 2
      Slight lean (soft)              : score ≥ 2 AND |diff| ≥ 2
      Range-Bound, High IV            : iv_rank > 60 AND |diff| ≤ 1
      Mixed Signals                   : everything else (truly indecisive)

    ── Sanity walk: representative scenarios ────────────────────────────────
       8 bull / 0 bear  → diff +8 → Mildly Bullish      (clear)
       3 bull / 1 bear  → diff +2 → Mildly Bullish      (dominant + floor)
       3 bull / 2 bear  → diff +1 → Mixed               (near-tie; honest)
       2 bull / 0 bear  → diff +2 → Slight bullish lean (quiet but one-sided)
       2 bull / 1 bear  → diff +1 → Mixed               (1-vote contradiction)
       1 bull / 0 bear  → diff +1 → Mixed               (single factor too thin)
       0 bull / 0 bear  → diff  0 → Mixed               (no signal)
       4 bull / 4 bear  → diff  0 → Mixed (or Range if iv_rank > 60)

    Mirror cases for bears resolve identically.  The system is symmetric,
    transparent, and produces "Mixed" exactly when signals truly are.
    """
    # ── Defensive extraction.  Use explicit None checks for any field whose
    # missing value would FALSELY fire a one-sided factor (PCRs in particular:
    # `pcr_oi or 0` would make `0 < 0.9` True, spuriously firing a bear).
    rr_z    = (d.get("rr_zscore") or {}).get("z_score")
    vix_c   = d.get("vix_change_pct")
    spot    = d.get("spot")
    mp      = d.get("max_pain")
    mp_dist = (spot - mp) if (spot and mp) else None
    ts      = (d.get("term_structure") or {}).get("shape")
    iv_skew = d.get("iv_skew")
    pcr_oi  = d.get("pcr_oi")
    vol_pcr = d.get("vol_pcr")
    gex     = d.get("gex")
    iv_rank = d.get("iv_rank") or 0
    bull_n  = d.get("bullish_count") or 0
    bear_n  = d.get("bearish_count") or 0

    GEX_THRESH = 5e5  # 500K — same threshold the narrative uses for "+GEX/−GEX"

    # Each factor is a (name, fired?) tuple for full audit traceability in
    # the response.  Every comparison is None-safe.
    bull_factors = [
        ("dir_dominance",   (bull_n - bear_n) >= 2),
        ("pcr_oi",          pcr_oi  is not None and pcr_oi  > 1.10),
        ("vol_pcr",         vol_pcr is not None and vol_pcr > 1.20),
        ("gex",             gex     is not None and gex     >  GEX_THRESH),
        ("rr_zscore",       rr_z    is not None and rr_z    >  1.0),
        ("vix_falling",     vix_c   is not None and vix_c   < -3),
        ("term_contango",   ts == "Contango"),
        ("max_pain_below",  mp_dist is not None and mp_dist < -50),
    ]
    bear_factors = [
        ("dir_dominance",   (bear_n - bull_n) >= 2),
        ("pcr_oi",          pcr_oi  is not None and pcr_oi  < 0.90),
        ("vol_pcr",         vol_pcr is not None and vol_pcr < 0.80),
        ("gex",             gex     is not None and gex     < -GEX_THRESH),
        ("rr_zscore",       rr_z    is not None and rr_z    < -1.0),
        ("vix_surging",     vix_c   is not None and vix_c   >  5),
        ("iv_skew",         iv_skew is not None and iv_skew >  3),
        ("term_backwardn",  ts == "Backwardation"),
        ("max_pain_above",  mp_dist is not None and mp_dist > 100),
    ]
    bull = sum(1 for _, fired in bull_factors if fired)
    bear = sum(1 for _, fired in bear_factors if fired)
    diff = bull - bear

    if   bull >= 3 and diff  >= 2:
        text, tone = "Mildly Bullish — favour PE selling / bull spreads", "BULLISH"
    elif bear >= 3 and -diff >= 2:
        text, tone = "Mildly Bearish — favour CE selling / bear spreads", "BEARISH"
    elif bull >= 2 and diff  >= 2:
        text, tone = "Slight bullish lean — early signs of upward bias", "BULLISH_SOFT"
    elif bear >= 2 and -diff >= 2:
        text, tone = "Slight bearish lean — early signs of downward bias", "BEARISH_SOFT"
    elif iv_rank > 60 and abs(diff) <= 1:
        text, tone = "Range-Bound, High IV — premium collection", "RANGE"
    else:
        text, tone = "Mixed Signals — no strong edge", "NEUTRAL"

    return {
        "text": text,
        "tone": tone,
        "bull": bull,
        "bull_total": len(bull_factors),
        "bear": bear,
        "bear_total": len(bear_factors),
        "sub": f"Bull {bull}/{len(bull_factors)} · Bear {bear}/{len(bear_factors)}",
        # Per-factor breakdown so the verdict is fully auditable from the API
        # response — no hidden state.  The dashboard/builder don't render
        # this today, but having it on the wire makes future debugging and
        # any "explain this verdict" UI trivial.
        "factor_details": {
            "bull": [{"name": n, "fired": bool(f)} for n, f in bull_factors],
            "bear": [{"name": n, "fired": bool(f)} for n, f in bear_factors],
        },
    }


# Strategy universe — directional bias, preferred vol regime, risk type, structure.
# All defined-risk except SHORT_STRANGLE, which is kept but always demoted so it
# can never outrank a comparable defined-risk pick (safety-first).
_STRAT_META = {
    "BULL_PUT_SPREAD":  {"label": "Bull Put Spread",  "bias": "BULLISH", "vol": "HIGH",     "risk": "defined",   "kind": "credit"},
    "BULL_CALL_SPREAD": {"label": "Bull Call Spread", "bias": "BULLISH", "vol": "LOW",      "risk": "defined",   "kind": "debit"},
    "BEAR_CALL_SPREAD": {"label": "Bear Call Spread", "bias": "BEARISH", "vol": "HIGH",     "risk": "defined",   "kind": "credit"},
    "BEAR_PUT_SPREAD":  {"label": "Bear Put Spread",  "bias": "BEARISH", "vol": "LOW",      "risk": "defined",   "kind": "debit"},
    "IRON_CONDOR":      {"label": "Iron Condor",      "bias": "NEUTRAL", "vol": "HIGH",     "risk": "defined",   "kind": "credit"},
    "CALENDAR_SPREAD":  {"label": "Calendar Spread",  "bias": "NEUTRAL", "vol": "LOW",      "risk": "defined",   "kind": "debit"},
    "LONG_STRANGLE":    {"label": "Long Strangle",    "bias": "MOVE",    "vol": "VERY_LOW", "risk": "defined",   "kind": "debit"},
    "SHORT_STRANGLE":   {"label": "Short Strangle",   "bias": "NEUTRAL", "vol": "HIGH",     "risk": "unlimited", "kind": "credit"},
}


def _snap(x: float) -> int:
    return int(round(x / 50.0) * 50)


def _vol_regime(iv_tile: dict) -> str:
    """Map the dashboard IV tile verdict → HIGH / MID / LOW / VERY_LOW. Falls back
    to the IV percentile if the verdict is missing."""
    v = (iv_tile or {}).get("iv_verdict") or ""
    if "Elevated" in v:           # "Elevated IV" / "Very Elevated IV"
        return "HIGH"
    if "Very Low" in v:
        return "VERY_LOW"
    if "Low" in v:
        return "LOW"
    if "Neutral" in v:
        return "MID"
    ivp = (iv_tile or {}).get("iv_percentile")
    if ivp is None:
        return "MID"
    return "HIGH" if ivp >= 60 else ("VERY_LOW" if ivp <= 20 else ("LOW" if ivp <= 40 else "MID"))


# Vol-fit by (preferred regime, current regime): 1.0 = ideal, lower = mismatch.
# LOW-pref strategies (debit spreads, calendars) fade at rock-bottom IV — the near
# leg's premium is too thin and vol is more likely to expand — so VERY_LOW is a poor
# fit for them, leaving room for the Long Strangle (VERY_LOW-pref) to win there.
_VOL_FIT = {
    "HIGH":     {"HIGH": 1.0, "MID": 0.60, "LOW": 0.25, "VERY_LOW": 0.10},
    "LOW":      {"HIGH": 0.25, "MID": 0.60, "LOW": 1.0, "VERY_LOW": 0.45},
    "VERY_LOW": {"HIGH": 0.05, "MID": 0.25, "LOW": 0.60, "VERY_LOW": 1.0},
}


def _default_legs(strategy_type: str, spot: float, support: int, resistance: int,
                  expiries: list) -> list:
    """OI-anchored default legs (strikes snapped to the 50-pt grid). Spreads anchor
    the SHORT strike to the OI support (bull) / resistance (bear) wall; calendars
    SELL the current expiry and BUY the next (two expiries)."""
    atm = _snap(spot)
    wing = 100  # 2 strikes
    near = str(expiries[0]) if expiries else None
    far = str(expiries[1]) if expiries and len(expiries) > 1 else None
    # Keep anchors on the correct, OTM side of spot.
    sup = _snap(min(support or atm, atm))      # support at/below spot
    res = _snap(max(resistance or atm, atm))   # resistance at/above spot
    move = max(100, _snap(spot * 0.02))         # ~1-SD-ish strangle offset

    if strategy_type == "BULL_PUT_SPREAD":
        return [{"option_type": "PE", "action": "SELL", "strike": sup, "lots": 1},
                {"option_type": "PE", "action": "BUY",  "strike": sup - wing, "lots": 1}]
    if strategy_type == "BEAR_CALL_SPREAD":
        return [{"option_type": "CE", "action": "SELL", "strike": res, "lots": 1},
                {"option_type": "CE", "action": "BUY",  "strike": res + wing, "lots": 1}]
    if strategy_type == "BULL_CALL_SPREAD":
        return [{"option_type": "CE", "action": "BUY",  "strike": atm, "lots": 1},
                {"option_type": "CE", "action": "SELL", "strike": max(res, atm + wing), "lots": 1}]
    if strategy_type == "BEAR_PUT_SPREAD":
        return [{"option_type": "PE", "action": "BUY",  "strike": atm, "lots": 1},
                {"option_type": "PE", "action": "SELL", "strike": min(sup, atm - wing), "lots": 1}]
    if strategy_type == "IRON_CONDOR":
        return [{"option_type": "PE", "action": "SELL", "strike": sup, "lots": 1},
                {"option_type": "PE", "action": "BUY",  "strike": sup - wing, "lots": 1},
                {"option_type": "CE", "action": "SELL", "strike": res, "lots": 1},
                {"option_type": "CE", "action": "BUY",  "strike": res + wing, "lots": 1}]
    if strategy_type == "SHORT_STRANGLE":
        return [{"option_type": "PE", "action": "SELL", "strike": atm - move, "lots": 1},
                {"option_type": "CE", "action": "SELL", "strike": atm + move, "lots": 1}]
    if strategy_type == "LONG_STRANGLE":
        return [{"option_type": "PE", "action": "BUY", "strike": atm - move, "lots": 1},
                {"option_type": "CE", "action": "BUY", "strike": atm + move, "lots": 1}]
    if strategy_type == "CALENDAR_SPREAD":
        # SELL current expiry, BUY next expiry — same ATM strike (two expiries).
        return [{"option_type": "CE", "action": "SELL", "strike": atm, "lots": 1, "expiry": near},
                {"option_type": "CE", "action": "BUY",  "strike": atm, "lots": 1, "expiry": far}]
    return []


def _market_staleness(iv_tile, fii_dii, oi_totals, rr_value) -> list:
    """Stale/missing inputs → each applies a flat confidence penalty in scoring so
    the builder never silently ranks on outdated data."""
    stale = []
    if not iv_tile or iv_tile.get("status") != "ok" or (iv_tile.get("data_days") or 0) < 30:
        stale.append("IV history")
    if not fii_dii or (fii_dii.get("fii_fut_net") is None and fii_dii.get("fii_cash_net") is None):
        stale.append("FII/DII flows")
    if not oi_totals or oi_totals.get("pcr_chg") is None:
        stale.append("PCR change")
    if rr_value is None:
        stale.append("Risk reversal")
    return stale


def _score_strategies(direction, agreement_pct, iv_tile, term_structure,
                      oi_support, oi_resistance, pcr_chg, rr_value,
                      spot, expiries, staleness=None):
    """Rank strategies 0-100 from the DASHBOARD's regime (direction = Market Tone,
    volatility = IV tile) + OI structure. Five weighted priorities:
    Directional Agreement(35) · Volatility Fit(25) · OI-Structure Fit(15) ·
    Defined-Risk Safety(15) · Execution Quality(10). Defined-risk always beats a
    comparable unlimited-risk pick; stale inputs apply a flat confidence penalty."""
    staleness = staleness or []
    regime = _vol_regime(iv_tile)
    ivp = (iv_tile or {}).get("iv_percentile")
    iv_trend = (iv_tile or {}).get("iv_5d_change_pct") or 0.0
    contango = ((term_structure or {}).get("shape") == "Contango")
    conv = max(0.0, min(1.0, (agreement_pct or 0) / 100.0))   # tone conviction 0..1
    has_sup, has_res = bool(oi_support), bool(oi_resistance)
    pcr_chg = pcr_chg or 0.0
    penalty = 7.0 * len(staleness)        # confidence haircut per stale/missing input

    only_one_expiry = not (expiries and len(expiries) > 1)
    rr_txt = f"{rr_value:+.1f}" if rr_value is not None else "n/a"

    rows = []
    for k, m in _STRAT_META.items():
        bias, vol, risk, kind = m["bias"], m["vol"], m["risk"], m["kind"]

        # 1. Directional agreement (vs dashboard Market Tone)
        if bias in ("BULLISH", "BEARISH"):
            if direction == bias:
                d_fit = 0.55 + 0.45 * conv
            elif direction == "NEUTRAL":
                d_fit = 0.40
            else:                       # opposite the tone → strongly avoid
                d_fit = 0.05
        elif bias == "MOVE":            # long strangle — direction-agnostic move play
            d_fit = 0.55 + 0.25 * conv
        else:                           # NEUTRAL strategies
            d_fit = 1.0 if direction == "NEUTRAL" else (1.0 - 0.60 * conv)

        # 2. Volatility fit (vs dashboard IV tile regime)
        v_fit = _VOL_FIT[vol][regime]
        if k == "LONG_STRANGLE" and (iv_trend >= 0 or contango):
            v_fit = min(1.0, v_fit + 0.15)   # expansion expected → buy vol

        # 3. OI-structure fit (clear wall to anchor the short strike)
        if bias == "BULLISH":
            o_fit = (1.0 if has_sup else 0.5) + (0.1 if pcr_chg > 0 else 0.0)
        elif bias == "BEARISH":
            o_fit = (1.0 if has_res else 0.5) + (0.1 if pcr_chg < 0 else 0.0)
        elif k in ("IRON_CONDOR", "SHORT_STRANGLE"):
            o_fit = 1.0 if (has_sup and has_res) else 0.6
        else:                           # calendar / long strangle — ATM-anchored
            o_fit = 0.7
        o_fit = min(1.0, o_fit)

        # 4. Defined-risk safety   5. Execution quality
        safety = 1.0 if risk == "defined" else 0.30
        exe = 0.85
        if k == "CALENDAR_SPREAD" and only_one_expiry:
            exe = 0.0                   # cannot build a real (2-expiry) calendar now

        score = 35 * d_fit + 25 * v_fit + 15 * o_fit + 15 * safety + 10 * exe - penalty
        score = max(0.0, min(100.0, score))

        regime_word = {"HIGH": "high IV", "MID": "moderate IV", "LOW": "low IV",
                       "VERY_LOW": "very low IV"}[regime]
        rationale = (
            f"Market Tone {direction.title()} ({int(round(conv*100))}% agree) · {regime_word}"
            f"{f' (IVP {ivp:.0f})' if ivp is not None else ''}. "
            + (("Short strike anchored to OI " + ("support" if bias == "BULLISH" else "resistance")
                + ".") if bias in ("BULLISH", "BEARISH")
               else ("Both wings at OI support/resistance." if k in ("IRON_CONDOR", "SHORT_STRANGLE")
                     else ("Buy next / sell current expiry." if k == "CALENDAR_SPREAD"
                           else "Direction-agnostic move play.")))
            + (" Defined risk." if risk == "defined" else " Unlimited risk — demoted.")
            + (f" RR {rr_txt}." if bias in ("BULLISH", "BEARISH") else "")
        )

        rows.append({
            "strategy_type": k,
            "label": m["label"],
            "confidence_score": round(score, 1),
            "risk_type": risk,
            "rationale": rationale,
            "legs": _default_legs(k, spot, oi_support, oi_resistance, expiries),
        })

    rows.sort(key=lambda r: r["confidence_score"], reverse=True)
    return rows


# ── Endpoints ─────────────────────────────────────────────────────────────────

@router.get("/market-analytics")
def get_market_analytics(
    expiry: Optional[str] = Query(None),
    db: Session = Depends(get_db),
    _=Depends(get_current_user),
):
    """Full analytics bar data for the Strategy Builder page.

    Direction comes from the dashboard's Market Tone and volatility from the
    dashboard's IV tile — the builder does NOT compute its own directional model,
    so the two pages can never disagree."""
    from market_data import (
        build_chain, compute_risk_reversal, compute_oi_totals, compute_market_tone,
        compute_tendency, compute_max_pain, days_to_expiry,
        fetch_vix, pick_active_expiry,
    )
    from routes_market import compute_iv_tile
    from fii_dii import get_fii_dii

    # If the caller didn't pin a specific expiry, default to the *active* one
    # (≥ 2 trading days out).  This makes analytics/tendency/direction read
    # off a chain that actually has signal — on expiry day or the day before,
    # the front-month IV/skew is too compressed to be informative.
    if not expiry:
        try:
            _, _exp_list, _ = build_chain("NIFTY")
            expiry = pick_active_expiry(_exp_list) or expiry
        except Exception:
            pass

    # Fetch chain
    try:
        chain_df, expiries, spot = build_chain("NIFTY", expiry)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Chain fetch failed: {e}")

    # Use the *active* expiry from the live list rather than blindly picking
    # expiries[0] — keeps analytics consistent with the dashboard rollover.
    near_expiry = expiry or pick_active_expiry(expiries) or (str(expiries[0]) if expiries else "")
    dte = days_to_expiry(near_expiry)

    # VIX
    vix_data = fetch_vix()
    vix = vix_data["last"] if vix_data else 0.0
    vix_change_pct = vix_data.get("pChange", 0) if vix_data else 0.0

    # ATM IV
    atm_strike = round(spot / 50) * 50
    ce_atm = chain_df[(chain_df["Strike"] == atm_strike) & (chain_df["Type"] == "CE")]
    pe_atm = chain_df[(chain_df["Strike"] == atm_strike) & (chain_df["Type"] == "PE")]
    ce_atm_iv = float(ce_atm["IV"].iloc[0]) if not ce_atm.empty else 0.0
    pe_atm_iv = float(pe_atm["IV"].iloc[0]) if not pe_atm.empty else 0.0
    atm_iv = round((ce_atm_iv + pe_atm_iv) / 2, 2) if (ce_atm_iv + pe_atm_iv) > 0 else 0.0

    # Straddle price + implied move
    ce_atm_ltp = float(ce_atm["LTP"].iloc[0]) if not ce_atm.empty else 0.0
    pe_atm_ltp = float(pe_atm["LTP"].iloc[0]) if not pe_atm.empty else 0.0
    straddle_price = round(ce_atm_ltp + pe_atm_ltp, 2)
    implied_move_pct = round(straddle_price / spot * 100, 2) if spot else 0.0

    # Term structure: compare near vs next expiry ATM IV
    term_structure = {"near_atm_iv": atm_iv, "far_atm_iv": None, "shape": "N/A"}
    if len(expiries) > 1:
        try:
            chain_far, _, _ = build_chain("NIFTY", expiries[1])
            far_atm = chain_far[(chain_far["Strike"] == atm_strike) & (chain_far["Type"] == "CE")]
            if not far_atm.empty:
                far_iv = round(float(far_atm["IV"].iloc[0]), 2)
                term_structure["far_atm_iv"] = far_iv
                term_structure["shape"] = "Backwardation" if atm_iv > far_iv else "Contango"
        except Exception:
            pass

    # IV skew
    iv_skew = _compute_iv_skew(chain_df, spot)

    # Risk reversal — compute z-score first so tendency can use it
    rr = compute_risk_reversal(chain_df)
    rr_value = rr.get("rr_value", 0)
    rr_zscore = _compute_rr_zscore(db, rr_value)
    tendency_data = compute_tendency(rr_value, vix, vix_change_pct, rr_zscore=rr_zscore)

    # ── DIRECTION = dashboard Market Tone (the heatmap) — single source ──────
    oi_totals = compute_oi_totals(chain_df)
    fd = get_fii_dii()
    tone = compute_market_tone(oi_totals, fd, rr_value)
    # ── VOLATILITY = dashboard IV tile — single source ──────────────────────
    iv_tile = compute_iv_tile(db)
    staleness = _market_staleness(iv_tile, fd, oi_totals, rr_value)

    # Max pain
    max_pain = compute_max_pain(chain_df)

    # PCR, Vol PCR, GEX (full breakdown)
    pcr_oi = oi_totals.get("pcr")
    pcr_chg = oi_totals.get("pcr_chg")
    vol_pcr = _compute_vol_pcr(chain_df, spot)
    gex_data = _compute_gex(chain_df, spot)

    # OI S/R
    oi_resistance, oi_support = _compute_oi_sr(chain_df)

    # Butterfly from current chain (25-delta)
    butterfly = None
    try:
        from market_data import find_25delta_iv
        ce25_strike, ce25_iv = find_25delta_iv(chain_df, "CE")
        pe25_strike, pe25_iv = find_25delta_iv(chain_df, "PE")
        if ce25_iv > 0 and pe25_iv > 0 and atm_iv > 0:
            butterfly = round((ce25_iv + pe25_iv) / 2.0 - atm_iv, 2)
    except Exception:
        pass

    response = {
        "spot": round(spot, 2),
        "expiry": near_expiry,
        "dte": dte,
        # ── The 8 consumed dashboard reads (single source of truth) ─────────
        "verdict": tone.get("label"),               # dashboard Market Tone label
        "direction": tone.get("tone"),              # BULLISH / BEARISH / NEUTRAL
        "agreement_pct": tone.get("agree_pct"),     # "NN% of weight agrees"
        "oi_support": oi_support,
        "oi_resistance": oi_resistance,
        "pcr_chg": pcr_chg,
        "rr_value": rr_value,
        "iv_percentile": iv_tile.get("iv_percentile"),
        "iv_5d_change_pct": iv_tile.get("iv_5d_change_pct"),
        "term_structure": term_structure,
        "staleness": staleness,
        # ── Supporting info tiles (display only; not a second direction model) ─
        "iv_rank": iv_tile.get("iv_rank"),
        "iv_score": iv_tile.get("iv_score"),
        "iv_verdict": iv_tile.get("iv_verdict"),
        "iv_premium": iv_tile.get("iv_premium"),
        "atm_iv": atm_iv,
        "iv_52wk_high": iv_tile.get("iv_52wk_high"),
        "iv_52wk_low": iv_tile.get("iv_52wk_low"),
        "iv_mean": iv_tile.get("iv_mean"),
        "iv_skew": iv_skew,
        "butterfly": butterfly,
        "straddle_price": straddle_price,
        "implied_move_pct": implied_move_pct,
        "rr_verdict": rr.get("verdict"),
        "rr_zscore": rr_zscore,
        "tendency": tendency_data.get("tendency"),
        "tendency_confidence": (
            "Strong"   if tendency_data.get("confidence", 0) >= 0.7
            else "Moderate" if tendency_data.get("confidence", 0) >= 0.4
            else "Mild"    if tendency_data.get("confidence", 0) > 0
            else ""
        ),
        "pcr_oi": pcr_oi,
        "vol_pcr": vol_pcr,
        "max_pain": max_pain,
        "gex": gex_data["total_gex"],
        "gex_regime": gex_data["regime"],
        "gex_regime_desc": gex_data["regime_desc"],
        "gamma_flip": gex_data["gamma_flip"],
        "gex_top_positive": gex_data["top_positive"],
        "gex_top_negative": gex_data["top_negative"],
        "vix": vix,
        "vix_change_pct": vix_change_pct,
        "data_days": iv_tile.get("data_days"),
        "low_data_warning": (iv_tile.get("data_days") or 0) < 30,
    }
    return response


@router.get("/rankings")
def get_strategy_rankings(
    expiry: Optional[str] = Query(None),
    db: Session = Depends(get_db),
    _=Depends(get_current_user),
):
    """Rank strategies for the current regime — DIRECTION from the dashboard's
    Market Tone, VOLATILITY from the dashboard's IV tile, STRUCTURE from OI. No
    independent directional model is computed here."""
    from market_data import build_chain, compute_risk_reversal, compute_oi_totals, compute_market_tone
    from routes_market import compute_iv_tile
    from fii_dii import get_fii_dii

    try:
        chain_df, expiries, spot = build_chain("NIFTY", expiry)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Chain fetch failed: {e}")

    rr = compute_risk_reversal(chain_df)
    rr_value = rr.get("rr_value")
    oi_totals = compute_oi_totals(chain_df)
    fd = get_fii_dii()
    tone = compute_market_tone(oi_totals, fd, rr_value)          # ← single source of direction
    iv_tile = compute_iv_tile(db)                                 # ← single source of volatility
    oi_resistance, oi_support = _compute_oi_sr(chain_df)

    staleness = _market_staleness(iv_tile, fd, oi_totals, rr_value)
    rankings = _score_strategies(
        direction=tone.get("tone", "NEUTRAL"),
        agreement_pct=tone.get("agree_pct", 0),
        iv_tile=iv_tile,
        term_structure=None,            # iv_5d_change covers the expansion check here
        oi_support=oi_support,
        oi_resistance=oi_resistance,
        pcr_chg=oi_totals.get("pcr_chg"),
        rr_value=rr_value,
        spot=spot,
        expiries=expiries,
        staleness=staleness,
    )
    return {
        "rankings": rankings, "spot": round(spot, 2),
        "verdict": tone.get("label"), "direction": tone.get("tone"),
        "agreement_pct": tone.get("agree_pct"),
        "staleness": staleness,
    }


@router.post("/")
def create_strategy(
    req: CreateStrategyRequest,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Create a multi-leg strategy and add all legs as Trade records.

    Each leg uses its own expiry when given (calendars span two expiries) — the
    Trade row stores the per-leg expiry, so the long/short legs land on the correct
    expiries. The Strategy.expiry_date is the near (request) expiry."""
    from market_data import build_chain, greeks, time_to_expiry

    # Calendars must span two expiries.
    leg_exps = [(leg.expiry or req.expiry_date) for leg in req.legs]
    if req.strategy_type.upper().startswith("CALENDAR") and len(set(leg_exps)) < 2:
        raise HTTPException(status_code=400,
                            detail="Calendar spread requires two expiries (sell current, buy next).")

    # One chain per distinct leg expiry.
    chains, spot = {}, None
    for e in sorted(set(leg_exps), key=lambda x: time_to_expiry(x)):
        try:
            cdf, _, sp = build_chain("NIFTY", e)
        except Exception as ex:
            raise HTTPException(status_code=500, detail=f"Chain fetch failed for {e}: {ex}")
        chains[e] = cdf
        if spot is None:
            spot = sp

    name = req.name or f"{req.strategy_type.replace('_', ' ').title()} – {req.expiry_date[:6]}"
    strategy = Strategy(
        user_id=user.id,
        name=name,
        strategy_type=req.strategy_type,
        expiry_date=req.expiry_date,
        status="ACTIVE",
    )
    db.add(strategy)
    db.flush()  # get strategy.id

    now = datetime.now(_IST).replace(tzinfo=None)
    atm_strike = round(spot / 50) * 50

    created_trades = []
    for leg, e in zip(req.legs, leg_exps):
        opt_type = leg.option_type
        cdf = chains[e]
        T = time_to_expiry(e)
        ce_atm = cdf[(cdf["Strike"] == atm_strike) & (cdf["Type"] == "CE")]
        atm_iv_frac = float(ce_atm["IV"].iloc[0]) / 100 if not ce_atm.empty else 0.15
        row = cdf[(cdf["Strike"] == leg.strike) & (cdf["Type"] == opt_type)]
        ltp = float(row["LTP"].iloc[0]) if not row.empty else 0.0
        iv_frac = float(row["IV"].iloc[0]) / 100 if not row.empty else atm_iv_frac
        leg_greeks = greeks(spot, leg.strike, T or 0.001, RISK_FREE_RATE, iv_frac, opt_type)

        trade_type = f"{opt_type}_{leg.action}"  # CE_SELL, CE_BUY, PE_SELL, PE_BUY
        trade = Trade(
            user_id=user.id,
            trade_type=trade_type,
            strike=leg.strike,
            expiry_date=e,                      # per-leg expiry
            entry_premium=round(ltp, 2),
            entry_iv=round(iv_frac * 100, 2),
            entry_delta=round(leg_greeks["delta"], 4),
            quantity=leg.lots,
            status=TradeStatus.OPEN.value,
            strategy_id=strategy.id,
            entry_date=now,
        )
        db.add(trade)
        created_trades.append({"strike": leg.strike, "type": trade_type, "ltp": ltp, "expiry": e})

    db.commit()
    db.refresh(strategy)
    return {
        "strategy_id": strategy.id,
        "name": strategy.name,
        "strategy_type": strategy.strategy_type,
        "expiry_date": strategy.expiry_date,
        "legs_created": len(created_trades),
        "legs": created_trades,
    }


@router.get("/")
def list_strategies(
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """List ACTIVE strategies with combined unrealized P&L.

    Auto-cleanup: any strategy whose legs have all been closed elsewhere
    (e.g. closed individually from the Positions page) is transitioned to
    CLOSED here so it stops showing up under Active.  Without this, the UI
    would render zombie strategies — same name, 0 legs, 0 P&L — which is
    exactly what the user reported.
    """
    from market_data import build_chain, days_to_expiry

    # Auto-close strategies that have no remaining OPEN legs.
    candidate = db.query(Strategy).filter(
        Strategy.user_id == user.id,
        Strategy.status == "ACTIVE",
    ).all()
    cleanup_dirty = False
    for s in candidate:
        open_count = db.query(Trade).filter(
            Trade.strategy_id == s.id,
            Trade.status == TradeStatus.OPEN.value,
        ).count()
        if open_count == 0:
            s.status = "CLOSED"
            cleanup_dirty = True
    if cleanup_dirty:
        db.commit()

    strategies = db.query(Strategy).filter(
        Strategy.user_id == user.id,
        Strategy.status == "ACTIVE",
    ).order_by(_desc(Strategy.created_at)).all()

    result = []
    chain_cache = {}

    for s in strategies:
        legs = db.query(Trade).filter(
            Trade.strategy_id == s.id,
            Trade.status == TradeStatus.OPEN.value,
        ).all()

        # Defensive: if a concurrent close drained the legs between the
        # cleanup pass above and this loop, skip rendering the empty card.
        if not legs:
            s.status = "CLOSED"
            db.commit()
            continue

        total_pnl = 0.0
        legs_data = []

        for leg in legs:
            opt_type = "CE" if "CE" in leg.trade_type else "PE"
            cache_key = leg.expiry_date
            if cache_key not in chain_cache:
                try:
                    df, _, sp = build_chain("NIFTY", leg.expiry_date)
                    chain_cache[cache_key] = (df, sp)
                except Exception:
                    chain_cache[cache_key] = (None, 0)

            chain_df, spot = chain_cache[cache_key]
            current_ltp = 0.0
            if chain_df is not None:
                row = chain_df[(chain_df["Strike"] == leg.strike) & (chain_df["Type"] == opt_type)]
                current_ltp = float(row["LTP"].iloc[0]) if not row.empty else 0.0

            if "SELL" in leg.trade_type:
                leg_pnl = (leg.entry_premium - current_ltp) * leg.quantity * LOT_SIZE
            else:
                leg_pnl = (current_ltp - leg.entry_premium) * leg.quantity * LOT_SIZE
            total_pnl += leg_pnl

            legs_data.append({
                "trade_id": leg.id,
                "strike": leg.strike,
                "trade_type": leg.trade_type,
                "quantity": leg.quantity,
                "entry_premium": leg.entry_premium,
                "current_ltp": round(current_ltp, 2),
                "pnl": round(leg_pnl, 2),
            })

        dte = days_to_expiry(s.expiry_date)
        result.append({
            "id": s.id,
            "name": s.name,
            "strategy_type": s.strategy_type,
            "expiry_date": s.expiry_date,
            "status": s.status,
            "created_at": s.created_at.isoformat() if s.created_at else None,
            "dte": dte,
            "total_pnl": round(total_pnl, 2),
            "legs": legs_data,
        })

    return {"strategies": result}


@router.get("/closed")
def list_closed_strategies(
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """List CLOSED strategies with combined *realized* P&L.

    Powers the Strategy Builder's "Past Strategies" tab.  P&L sums the
    realized_pnl of every leg, so the user sees the actual settled outcome
    instead of a stale unrealized snapshot.
    """
    from market_data import days_to_expiry

    # Promote any orphaned ACTIVE rows (no remaining open legs) to CLOSED so
    # they show up here instead of disappearing.
    orphans = db.query(Strategy).filter(
        Strategy.user_id == user.id,
        Strategy.status == "ACTIVE",
    ).all()
    dirty = False
    for s in orphans:
        oc = db.query(Trade).filter(
            Trade.strategy_id == s.id,
            Trade.status == TradeStatus.OPEN.value,
        ).count()
        if oc == 0:
            s.status = "CLOSED"
            dirty = True
    if dirty:
        db.commit()

    strategies = db.query(Strategy).filter(
        Strategy.user_id == user.id,
        Strategy.status == "CLOSED",
    ).order_by(_desc(Strategy.created_at)).all()

    result = []
    for s in strategies:
        legs = db.query(Trade).filter(Trade.strategy_id == s.id).all()
        total_realized = 0.0
        legs_data = []
        latest_exit = None
        for leg in legs:
            r = leg.realized_pnl or 0.0
            total_realized += r
            if leg.exit_date and (latest_exit is None or leg.exit_date > latest_exit):
                latest_exit = leg.exit_date
            legs_data.append({
                "trade_id": leg.id,
                "strike": leg.strike,
                "trade_type": leg.trade_type,
                "quantity": leg.quantity,
                "entry_premium": leg.entry_premium,
                "exit_price": leg.exit_price,
                "realized_pnl": round(r, 2),
            })

        result.append({
            "id": s.id,
            "name": s.name,
            "strategy_type": s.strategy_type,
            "expiry_date": s.expiry_date,
            "status": s.status,
            "created_at": s.created_at.isoformat() if s.created_at else None,
            "closed_at": latest_exit.isoformat() if latest_exit else None,
            "total_realized_pnl": round(total_realized, 2),
            "legs": legs_data,
        })

    return {"strategies": result}


@router.get("/{strategy_id}/payoff")
def get_strategy_payoff(
    strategy_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Compute payoff curve for a strategy at expiry."""
    from market_data import build_chain, days_to_expiry

    strategy = db.query(Strategy).filter(
        Strategy.id == strategy_id, Strategy.user_id == user.id
    ).first()
    if not strategy:
        raise HTTPException(status_code=404, detail="Strategy not found")

    legs = db.query(Trade).filter(
        Trade.strategy_id == strategy_id,
        Trade.status == TradeStatus.OPEN.value,
    ).all()

    try:
        _, _, spot = build_chain("NIFTY", strategy.expiry_date)
    except Exception:
        snap = db.query(MarketSnapshot).order_by(_desc(MarketSnapshot.timestamp)).first()
        spot = snap.nifty_spot if snap else 22000.0

    # Payoff at expiry: intrinsic value
    spot_range = range(int(spot) - 500, int(spot) + 550, 50)
    payoff_curve = []
    net_premium = 0.0

    for leg in legs:
        opt_type = "CE" if "CE" in leg.trade_type else "PE"
        if "SELL" in leg.trade_type:
            net_premium += leg.entry_premium * leg.quantity * LOT_SIZE
        else:
            net_premium -= leg.entry_premium * leg.quantity * LOT_SIZE

    for s in spot_range:
        pnl = net_premium  # start with premium received/paid
        for leg in legs:
            opt_type = "CE" if "CE" in leg.trade_type else "PE"
            intrinsic = max(s - leg.strike, 0) if opt_type == "CE" else max(leg.strike - s, 0)
            qty = leg.quantity * LOT_SIZE
            if "SELL" in leg.trade_type:
                pnl -= intrinsic * qty
            else:
                pnl += intrinsic * qty
        payoff_curve.append({"spot": s, "pnl": round(pnl, 2)})

    return {
        "strategy_id": strategy_id,
        "name": strategy.name,
        "current_spot": round(spot, 2),
        "net_premium": round(net_premium, 2),
        "payoff": payoff_curve,
    }


class PayoffPreviewLeg(BaseModel):
    option_type: str   # "CE" or "PE"
    action: str        # "BUY" or "SELL"
    strike: int
    lots: int = 1
    expiry: Optional[str] = None   # per-leg expiry (calendars); falls back to request expiry


class PayoffPreviewRequest(BaseModel):
    expiry: str
    legs: list[PayoffPreviewLeg]
    strategy_type: Optional[str] = None   # used to validate calendars span two expiries


@router.post("/payoff-preview")
def payoff_preview(req: PayoffPreviewRequest, _=Depends(get_current_user)):
    """Compute true payoff-at-expiry curve from live LTPs + leg config.

    Used by the Strategy Builder wizard to render an accurate payoff diagram
    *before* the strategy is executed.  Unlike the client-side intrinsic-only
    preview, this folds in actual mid-quotes so max profit / max loss / break-
    evens reflect the real net premium credit or debit.
    """
    from market_data import build_chain, black76_price, time_to_expiry

    if not req.legs:
        raise HTTPException(status_code=400, detail="At least one leg required.")

    # Each leg's expiry defaults to the request expiry; the payoff is evaluated at
    # the NEAR (earliest) expiry — for a calendar that's when the short leg settles.
    leg_exps = [(leg.expiry or req.expiry) for leg in req.legs]
    distinct_exps = sorted(set(leg_exps), key=lambda e: time_to_expiry(e))
    near_expiry = distinct_exps[0]

    # A calendar MUST span two expiries (SELL current, BUY next) — never one.
    if (req.strategy_type or "").upper().startswith("CALENDAR") and len(distinct_exps) < 2:
        raise HTTPException(status_code=400,
                            detail="Calendar spread requires two expiries (sell current, buy next).")

    # One chain per distinct expiry; spot from the near chain.
    chains, spot = {}, None
    for e in distinct_exps:
        try:
            cdf, _, sp = build_chain("NIFTY", e)
        except Exception as ex:
            raise HTTPException(status_code=500, detail=f"Chain fetch failed for {e}: {ex}")
        chains[e] = cdf
        if spot is None:
            spot = sp
    T_near = time_to_expiry(near_expiry)

    leg_records = []
    net_premium = 0.0          # ₹ across all legs (positive = credit received)
    portfolio_theta = 0.0      # ₹/day at portfolio level (short legs flip sign)
    margin_estimate = 0.0      # SPAN+exposure approximation
    for leg, e in zip(req.legs, leg_exps):
        cdf = chains[e]
        row = cdf[(cdf["Strike"] == leg.strike) & (cdf["Type"] == leg.option_type)]
        if row.empty:
            raise HTTPException(status_code=400,
                                detail=f"Strike {leg.strike}{leg.option_type} not found for {e}.")
        ltp = float(row["LTP"].iloc[0])
        iv = float(row["IV"].iloc[0]) if "IV" in row.columns else 0.0
        qty = leg.lots * LOT_SIZE
        is_sell = leg.action.upper() == "SELL"
        net_premium += (+1 if is_sell else -1) * ltp * qty
        leg_theta_share = float(row["theta"].iloc[0]) if "theta" in row.columns else 0.0
        portfolio_theta += leg_theta_share * qty * (-1 if is_sell else +1)
        if is_sell:
            margin_estimate += 0.12 * float(leg.strike) * qty
        leg_records.append({
            "option_type": leg.option_type, "action": leg.action.upper(),
            "strike": leg.strike, "lots": leg.lots, "qty": qty,
            "ltp": round(ltp, 2), "expiry": e, "iv": round(iv, 2),
            "is_far": time_to_expiry(e) > T_near + 1e-9,
        })

    # Curve at the NEAR expiry. Near-expiry legs settle to intrinsic; FAR-expiry
    # legs (the long side of a calendar) still hold time value → priced via
    # Black-76 at the residual time, so the calendar shows its real tent shape.
    span = max(800, int(spot * 0.10))
    lo, hi, step = int(spot) - span, int(spot) + span, 50
    curve = []
    for s in range(lo, hi + step, step):
        pnl = net_premium
        for leg in leg_records:
            sgn = -1 if leg["action"] == "SELL" else +1
            if leg["is_far"]:
                rem = max(time_to_expiry(leg["expiry"]) - T_near, 1.0 / 525600)
                val = black76_price(float(s), leg["strike"], rem, RISK_FREE_RATE,
                                    max(leg["iv"], 1.0) / 100.0, leg["option_type"])
            else:
                val = (max(s - leg["strike"], 0) if leg["option_type"] == "CE"
                       else max(leg["strike"] - s, 0))
            pnl += sgn * val * leg["qty"]
        curve.append({"spot": s, "pnl": round(pnl, 2)})

    pnls = [p["pnl"] for p in curve]
    max_profit = max(pnls)
    max_loss = min(pnls)

    # Break-evens: linear-interpolate where curve crosses zero.
    breakevens = []
    for i in range(1, len(curve)):
        a, b = curve[i - 1], curve[i]
        if (a["pnl"] == 0):
            breakevens.append(a["spot"])
        elif (a["pnl"] < 0) != (b["pnl"] < 0):
            # zero crossing between a and b
            t = -a["pnl"] / (b["pnl"] - a["pnl"])
            breakevens.append(round(a["spot"] + t * (b["spot"] - a["spot"]), 1))
    # de-dupe within 1 pt
    dedup_be = []
    for be in breakevens:
        if not dedup_be or abs(be - dedup_be[-1]) > 1:
            dedup_be.append(be)

    # If the curve is unbounded on one side (long call: profit grows linearly
    # with spot, short call: loss grows linearly), surface that to the client
    # so the UI can render "Unlimited" instead of a misleading finite number.
    profit_unbounded = (curve[-1]["pnl"] > curve[-2]["pnl"] > 0) or (curve[0]["pnl"] > curve[1]["pnl"] > 0)
    loss_unbounded   = (curve[-1]["pnl"] < curve[-2]["pnl"] < 0) or (curve[0]["pnl"] < curve[1]["pnl"] < 0)

    return {
        "spot": round(spot, 2),
        "expiry": near_expiry,
        "expiries": distinct_exps,
        "net_premium": round(net_premium, 2),
        "max_profit": round(max_profit, 2),
        "max_loss": round(max_loss, 2),
        "profit_unbounded": profit_unbounded,
        "loss_unbounded": loss_unbounded,
        "breakevens": dedup_be,
        # Batch C.3 additions — surfaced by the Strategy Builder execute
        # panel so the trader sees what the strategy locks up before they
        # commit. Margin uses the same approximation as the dashboard's
        # margin-summary endpoint so the two agree.
        "theta_per_day": round(portfolio_theta, 2),
        "margin_estimate": round(margin_estimate, 2),
        "legs": leg_records,
        "curve": curve,
    }


@router.post("/{strategy_id}/close")
def close_strategy(
    strategy_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Close all open legs of a strategy."""
    from market_data import build_chain
    from config import LOT_SIZE as LOT

    strategy = db.query(Strategy).filter(
        Strategy.id == strategy_id, Strategy.user_id == user.id
    ).first()
    if not strategy:
        raise HTTPException(status_code=404, detail="Strategy not found")

    legs = db.query(Trade).filter(
        Trade.strategy_id == strategy_id,
        Trade.status == TradeStatus.OPEN.value,
    ).all()

    now = datetime.now(_IST).replace(tzinfo=None)
    total_realized = 0.0
    chain_cache = {}

    for leg in legs:
        cache_key = leg.expiry_date
        if cache_key not in chain_cache:
            try:
                df, _, sp = build_chain("NIFTY", leg.expiry_date)
                chain_cache[cache_key] = (df, sp)
            except Exception:
                chain_cache[cache_key] = (None, 0)

        chain_df, _ = chain_cache[cache_key]
        opt_type = "CE" if "CE" in leg.trade_type else "PE"
        exit_price = leg.entry_premium  # fallback
        if chain_df is not None:
            row = chain_df[(chain_df["Strike"] == leg.strike) & (chain_df["Type"] == opt_type)]
            if not row.empty:
                exit_price = float(row["LTP"].iloc[0])

        if "SELL" in leg.trade_type:
            realized = (leg.entry_premium - exit_price) * leg.quantity * LOT
        else:
            realized = (exit_price - leg.entry_premium) * leg.quantity * LOT

        leg.exit_price = exit_price
        leg.exit_date = now
        leg.status = TradeStatus.CLOSED.value
        leg.realized_pnl = round(realized, 2)
        total_realized += realized

        # Detach any open child trades attached to this leg so they keep
        # showing in Open Positions as standalone trades after the strategy
        # collapses. Same convention as the option/futures close paths.
        open_children = db.query(Trade).filter(
            Trade.parent_trade_id == leg.id,
            Trade.status == TradeStatus.OPEN.value,
        ).all()
        for child in open_children:
            child.parent_trade_id = None

    strategy.status = "CLOSED"
    db.commit()

    return {
        "strategy_id": strategy_id,
        "legs_closed": len(legs),
        "total_realized_pnl": round(total_realized, 2),
    }
