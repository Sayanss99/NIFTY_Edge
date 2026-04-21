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


def _score_strategies(iv_rank, direction, dir_score, rr_value, pcr, vix):
    """Score 5 strategy types 0-100 for current market. Safety-first bias —
    defined-risk strategies (Iron Condor, spreads, Calendar) get a head start
    over unlimited-risk Short Strangle. Strangle is penalised harder when vol
    or directional conviction is elevated, since tail risk is asymmetric.
    """
    scores = {}
    rationales = {}

    # Convenience: strong directional conviction (|dir_score| tracks the
    # 8-factor composite). High |score| means one-sided flow → bad for
    # unlimited-risk short-vol plays.
    strong_dir = abs(dir_score or 0) >= 4

    # ── IRON CONDOR ─ safer premium-sell default; both wings defined ────────
    ic = 55.0   # slight head-start: defined risk, neutral bias preferred
    if iv_rank > 70: ic += 25
    elif iv_rank > 50: ic += 14
    elif iv_rank < 30: ic -= 20
    if direction == "NEUTRAL": ic += 18
    else: ic -= (15 if strong_dir else 8)
    if abs(rr_value) < 2: ic += 12
    if vix > 22: ic -= 6     # even defined risk hurts in chaos
    rationales["IRON_CONDOR"] = (
        f"IV Rank {iv_rank:.0f}% ({'elevated – premium selling favoured' if iv_rank > 60 else 'moderate'}). "
        f"Market {'neutral – ideal for range-bound sell' if direction == 'NEUTRAL' else 'directional – defined wings cap risk'}. "
        f"RR {rr_value:+.1f} ({'balanced' if abs(rr_value) < 2 else 'skewed'}). Defined-risk both sides."
    )
    scores["IRON_CONDOR"] = max(0, min(100, ic))

    # ── SHORT STRANGLE ─ unlimited risk, safety-penalised ───────────────────
    st = 42.0   # lower base — unlimited risk demands stronger justification
    if iv_rank > 75: st += 22
    elif iv_rank > 60: st += 12
    elif iv_rank < 40: st -= 22
    if direction == "NEUTRAL": st += 10
    else: st -= (18 if strong_dir else 10)   # directional markets → tail risk
    if vix > 22: st -= 15                     # high-vol regime → blow-up risk
    elif vix > 18: st -= 6
    elif vix < 12: st -= 8                    # complacent vol, thin premium
    if abs(rr_value) > 3: st -= 8             # skew stressed
    rationales["STRANGLE"] = (
        f"IV Rank {iv_rank:.0f}% ({'very high – strong premium-sell case' if iv_rank > 70 else 'moderate'}). "
        f"VIX {vix:.1f} ({'elevated – tail risk heightened' if vix > 18 else 'calm'}). "
        f"Unlimited risk on both wings — prefer only when vol is rich and flow is neutral."
    )
    scores["STRANGLE"] = max(0, min(100, st))

    # ── BULL PUT SPREAD ─ defined-risk directional ──────────────────────────
    bp = 50.0
    if direction == "BULLISH": bp += (30 if strong_dir else 22)
    elif direction == "BEARISH": bp -= 25
    if rr_value < -1: bp += 10
    if pcr > 1.1: bp += 10
    if iv_rank > 40: bp += 10
    if vix > 25: bp -= 6
    rationales["BULL_PUT_SPREAD"] = (
        f"8-factor signal: {direction} (score {dir_score:+.1f}). "
        f"PCR {pcr:.2f} ({'supports bullish bias' if pcr > 1.0 else 'no strong support'}). "
        f"RR {rr_value:+.1f}. Defined risk — max loss capped at spread width."
    )
    scores["BULL_PUT_SPREAD"] = max(0, min(100, bp))

    # ── BEAR CALL SPREAD ─ defined-risk directional ─────────────────────────
    bc = 50.0
    if direction == "BEARISH": bc += (30 if strong_dir else 22)
    elif direction == "BULLISH": bc -= 25
    if rr_value > 1: bc += 10
    if pcr < 0.9: bc += 10
    if iv_rank > 40: bc += 10
    if vix > 25: bc -= 6
    rationales["BEAR_CALL_SPREAD"] = (
        f"8-factor signal: {direction} (score {dir_score:+.1f}). "
        f"PCR {pcr:.2f} ({'heavy call resistance' if pcr < 0.9 else 'neutral'}). "
        f"RR {rr_value:+.1f}. Defined risk — max loss capped at spread width."
    )
    scores["BEAR_CALL_SPREAD"] = max(0, min(100, bc))

    # ── CALENDAR SPREAD ─ defined-risk, long theta curve ────────────────────
    cs = 50.0
    if iv_rank < 30: cs += 30
    elif iv_rank < 45: cs += 15
    elif iv_rank > 70: cs -= 20
    if vix < 15: cs += 15
    if direction == "NEUTRAL": cs += 12
    elif strong_dir: cs -= 6
    rationales["CALENDAR_SPREAD"] = (
        f"IV Rank {iv_rank:.0f}% ({'depressed – buy near, sell far works well' if iv_rank < 30 else 'elevated – calendar spread less attractive'}). "
        f"VIX {vix:.1f} ({'low-vol environment' if vix < 15 else 'moderate'}). "
        f"Debit capped — max loss is net premium paid."
    )
    scores["CALENDAR_SPREAD"] = max(0, min(100, cs))

    result = sorted(scores.items(), key=lambda x: x[1], reverse=True)
    return [
        {
            "strategy_type": k,
            "confidence_score": round(v, 1),
            "rationale": rationales[k],
            "label": {
                "IRON_CONDOR": "Iron Condor",
                "STRANGLE": "Short Strangle",
                "BULL_PUT_SPREAD": "Bull Put Spread",
                "BEAR_CALL_SPREAD": "Bear Call Spread",
                "CALENDAR_SPREAD": "Calendar Spread",
            }.get(k, k),
        }
        for k, v in result
    ]


# ── Endpoints ─────────────────────────────────────────────────────────────────

@router.get("/market-analytics")
def get_market_analytics(
    expiry: Optional[str] = Query(None),
    db: Session = Depends(get_db),
    _=Depends(get_current_user),
):
    """Full analytics bar data for the Strategy Builder page."""
    from market_data import (
        build_chain, compute_risk_reversal, compute_directional_signal,
        compute_tendency, compute_max_pain, days_to_expiry,
        fetch_vix,
    )

    # Fetch chain
    try:
        chain_df, expiries, spot = build_chain("NIFTY", expiry)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Chain fetch failed: {e}")

    near_expiry = expiries[0] if expiries else expiry or ""
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

    # 8-factor directional signal
    dir_signal = compute_directional_signal(chain_df, spot, vix_data, None)

    # Max pain
    max_pain = compute_max_pain(chain_df)

    # PCR, Vol PCR, GEX (full breakdown)
    pcr_oi = _compute_pcr(chain_df)
    vol_pcr = _compute_vol_pcr(chain_df, spot)
    gex_data = _compute_gex(chain_df, spot)

    # OI S/R
    oi_resistance, oi_support = _compute_oi_sr(chain_df)

    # IV rank from DB
    iv_data = _get_iv_rank_data(db)

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

    return {
        "spot": round(spot, 2),
        "expiry": near_expiry,
        "dte": dte,
        # IV & Volatility
        "iv_rank": iv_data["iv_rank"],
        "iv_percentile": iv_data["iv_percentile"],
        "atm_iv": atm_iv,
        "iv_52wk_high": iv_data.get("iv_52wk_high"),
        "iv_52wk_low": iv_data.get("iv_52wk_low"),
        "iv_mean": iv_data.get("iv_mean"),
        "iv_skew": iv_skew,
        "butterfly": butterfly,
        "term_structure": term_structure,
        "straddle_price": straddle_price,
        "implied_move_pct": implied_move_pct,
        # Market Structure
        "rr_value": rr_value,
        "rr_verdict": rr.get("verdict"),
        "rr_zscore": rr_zscore,
        "direction": dir_signal.get("direction"),
        "direction_score": dir_signal.get("score"),
        "factors_agree": dir_signal.get("factors_agree"),
        "bullish_count": dir_signal.get("bullish_count"),
        "bearish_count": dir_signal.get("bearish_count"),
        "conviction": dir_signal.get("conviction"),
        "factor_details": dir_signal.get("factor_details", []),
        "tendency": tendency_data.get("tendency"),   # direction only: "Flat" / "Bearish Skew" / "Bullish Skew"
        "tendency_confidence": (
            "Strong"   if tendency_data.get("confidence", 0) >= 0.7
            else "Moderate" if tendency_data.get("confidence", 0) >= 0.4
            else "Mild"    if tendency_data.get("confidence", 0) > 0
            else ""
        ),
        "pcr_oi": pcr_oi,
        "vol_pcr": vol_pcr,
        "max_pain": max_pain,
        # GEX: full breakdown
        "gex": gex_data["total_gex"],
        "gex_regime": gex_data["regime"],
        "gex_regime_desc": gex_data["regime_desc"],
        "gamma_flip": gex_data["gamma_flip"],
        "gex_top_positive": gex_data["top_positive"],
        "gex_top_negative": gex_data["top_negative"],
        "vix": vix,
        "vix_change_pct": vix_change_pct,
        "oi_resistance": oi_resistance,
        "oi_support": oi_support,
        "data_days": iv_data["data_days"],
        "low_data_warning": iv_data["data_days"] < 30,
    }


@router.get("/rankings")
def get_strategy_rankings(
    expiry: Optional[str] = Query(None),
    db: Session = Depends(get_db),
    _=Depends(get_current_user),
):
    """Rank 5 strategy types by confidence score for current market conditions."""
    from market_data import (
        build_chain, compute_risk_reversal, compute_directional_signal,
        fetch_vix,
    )

    try:
        chain_df, expiries, spot = build_chain("NIFTY", expiry)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Chain fetch failed: {e}")

    vix_data = fetch_vix()
    vix = vix_data["last"] if vix_data else 0.0

    rr = compute_risk_reversal(chain_df)
    dir_signal = compute_directional_signal(chain_df, spot, vix_data, None)
    iv_data = _get_iv_rank_data(db)
    pcr = _compute_pcr(chain_df)

    rankings = _score_strategies(
        iv_rank=iv_data["iv_rank"],
        direction=dir_signal.get("direction", "NEUTRAL"),
        dir_score=dir_signal.get("score", 0),
        rr_value=rr.get("rr_value", 0),
        pcr=pcr,
        vix=vix,
    )
    return {"rankings": rankings, "spot": round(spot, 2), "vix": vix}


@router.post("/")
def create_strategy(
    req: CreateStrategyRequest,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Create a multi-leg strategy and add all legs as Trade records."""
    from market_data import build_chain, greeks, time_to_expiry

    try:
        chain_df, _, spot = build_chain("NIFTY", req.expiry_date)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Chain fetch failed: {e}")

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
    T = time_to_expiry(req.expiry_date)
    atm_strike = round(spot / 50) * 50
    ce_atm = chain_df[(chain_df["Strike"] == atm_strike) & (chain_df["Type"] == "CE")]
    atm_iv_frac = float(ce_atm["IV"].iloc[0]) / 100 if not ce_atm.empty else 0.15

    created_trades = []
    for leg in req.legs:
        opt_type = leg.option_type
        row = chain_df[(chain_df["Strike"] == leg.strike) & (chain_df["Type"] == opt_type)]
        ltp = float(row["LTP"].iloc[0]) if not row.empty else 0.0
        iv_frac = float(row["IV"].iloc[0]) / 100 if not row.empty else atm_iv_frac
        leg_greeks = greeks(spot, leg.strike, T or 0.001, RISK_FREE_RATE, iv_frac, opt_type)

        trade_type = f"{opt_type}_{leg.action}"  # CE_SELL, CE_BUY, PE_SELL, PE_BUY
        trade = Trade(
            user_id=user.id,
            trade_type=trade_type,
            strike=leg.strike,
            expiry_date=req.expiry_date,
            entry_premium=round(ltp, 2),
            entry_iv=round(iv_frac * 100, 2),
            entry_delta=round(leg_greeks["delta"], 4),
            quantity=leg.lots,
            status=TradeStatus.OPEN.value,
            strategy_id=strategy.id,
            entry_date=now,
        )
        db.add(trade)
        created_trades.append({"strike": leg.strike, "type": trade_type, "ltp": ltp})

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
    """List active strategies with combined unrealized P&L."""
    from market_data import build_chain, days_to_expiry

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

    strategy.status = "CLOSED"
    db.commit()

    return {
        "strategy_id": strategy_id,
        "legs_closed": len(legs),
        "total_realized_pnl": round(total_realized, 2),
    }
