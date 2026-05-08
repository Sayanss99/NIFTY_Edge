"""Market data API routes — live data, option chain, S/R levels."""

import numpy as np
from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session
from database import get_db
from models import MarketSnapshot, SRLevel, IVDailyRecord
from auth import get_current_user


def _py(val):
    """Convert numpy types to native Python types for JSON serialization."""
    if isinstance(val, (np.integer,)):
        return int(val)
    if isinstance(val, (np.floating,)):
        return float(val)
    if isinstance(val, np.ndarray):
        return val.tolist()
    if isinstance(val, memoryview):
        val = val.tobytes()
    if isinstance(val, (bytes, bytearray)):
        raw = bytes(val)
        if len(raw) in (2, 4, 8):
            return int.from_bytes(raw, byteorder="little", signed=True)
        try:
            return raw.decode("utf-8")
        except UnicodeDecodeError:
            return raw.hex()
    return val

router = APIRouter(prefix="/api/market", tags=["market"])


@router.get("/status")
def get_market_status():
    """Check if the market is currently open (no auth required for this)."""
    from scheduler import is_market_open
    return is_market_open()


@router.get("/snapshot")
def get_latest_snapshot(db: Session = Depends(get_db), _=Depends(get_current_user)):
    """Get the most recent market snapshot."""
    snap = db.query(MarketSnapshot).order_by(MarketSnapshot.timestamp.desc()).first()
    if not snap:
        return {"data": None}
    return {
        "data": {
            "timestamp": snap.timestamp.isoformat() if snap.timestamp else None,
            "nifty_spot": snap.nifty_spot,
            "nifty_change": snap.nifty_change,
            "nifty_change_pct": snap.nifty_change_pct,
            "india_vix": snap.india_vix,
            "vix_change": snap.vix_change,
            "vix_change_pct": snap.vix_change_pct,
            "calc_range_top": snap.calculated_range_top,
            "calc_range_bottom": snap.calculated_range_bottom,
            "vix_range_top": snap.vix_range_top,
            "vix_range_bottom": snap.vix_range_bottom,
            "expected_move": snap.expected_move,
            "days_to_expiry": snap.days_to_expiry,
            "tendency": snap.tendency,
            "expiry_date": snap.expiry_date,
            "expiries": (snap.expiries_json if snap.expiries_json else ([snap.expiry_date] if snap.expiry_date else [])),
            "max_pain": snap.max_pain,
            "directional_signal": {
                "direction": snap.directional_direction,
                "score": snap.directional_score,
                "factors_agree": snap.directional_factors_agree,
            } if snap.directional_direction else None,
            "risk_reversal": {
                "ce_strike": snap.rr_ce_strike,
                "ce_iv": snap.rr_ce_iv,
                "pe_strike": snap.rr_pe_strike,
                "pe_iv": snap.rr_pe_iv,
                "rr_value": snap.rr_value,
                "verdict": snap.rr_verdict,
            },
        }
    }


@router.get("/expiries")
def list_expiries(_=Depends(get_current_user)):
    """Live list of available NIFTY expiries from NSE — works in market-closed hours too.

    Also returns `default_expiry` — the nearest expiry with ≥ 2 trading days
    remaining, computed by the central `pick_active_expiry` helper.  The
    frontend (dashboard, strategy builder, analytics) uses this field as the
    single source of truth for "which expiry to default to", so all surfaces
    agree on rollover (e.g. on Monday EOD with Tuesday expiry, or any Friday
    EOD when Monday is an NSE holiday).
    """
    from market_data import build_chain, pick_active_expiry
    try:
        _, expiries, spot = build_chain("NIFTY")
        ex_strs = [str(e) for e in expiries]
        return {
            "expiries": ex_strs,
            "default_expiry": pick_active_expiry(ex_strs),
            "spot": _py(spot),
        }
    except Exception as e:
        return {"error": str(e), "expiries": [], "default_expiry": None}


@router.get("/compute-for-expiry")
def compute_for_expiry(
    expiry: str = Query(..., description="Expiry in DD-Mon-YYYY format"),
    vix_offset_pct: float = Query(50),
    _=Depends(get_current_user),
):
    """On-demand computation of all dashboard metrics for a specific expiry."""
    from market_data import (
        build_chain, calculate_ranges, compute_risk_reversal,
        compute_tendency, compute_directional_signal, compute_max_pain,
        days_to_expiry, find_oi_sr_levels, fetch_fibonacci_levels,
        fetch_pivot_levels, merge_sr_levels, compute_pivot_oi_sr,
    )

    try:
        chain_df, expiries, spot = build_chain("NIFTY", expiry)
    except Exception as e:
        return {"error": f"Cannot fetch chain: {e}", "market_closed": True}

    if chain_df is None or len(chain_df) == 0:
        return {"error": f"No option chain data for expiry {expiry}. Valid expiries: {[str(e) for e in expiries]}"}

    try:
        dte = days_to_expiry(expiry)
    except Exception:
        dte = 1

    # Get VIX from latest snapshot
    from database import SessionLocal
    db_local = SessionLocal()
    try:
        latest_snap = db_local.query(MarketSnapshot).order_by(
            MarketSnapshot.timestamp.desc()
        ).first()
        vix = latest_snap.india_vix if latest_snap and latest_snap.india_vix else 15.0
        vix_change = latest_snap.vix_change if latest_snap else 0
        vix_change_pct = latest_snap.vix_change_pct if latest_snap else 0
        nifty_change = latest_snap.nifty_change if latest_snap else 0
        nifty_change_pct = latest_snap.nifty_change_pct if latest_snap else 0
    finally:
        db_local.close()

    try:
        ranges = calculate_ranges(spot, vix, dte, vix_offset_pct)
    except Exception:
        ranges = {"calc_range_top": 0, "calc_range_bottom": 0, "vix_range_top": 0, "vix_range_bottom": 0}
    try:
        rr = compute_risk_reversal(chain_df) or {}
    except Exception:
        rr = {}
    # Compute RR z-score so tendency labels match the Strategy Builder.  Without
    # this the dashboard fell through to the absolute fallback (rr < -4 / > 1)
    # while the builder used z-score thresholds, producing contradictory
    # "Flat" vs "Bullish Skew" reads on the same RR.
    rr_zscore = None
    try:
        from routes_strategies import _compute_rr_zscore
        db_z = SessionLocal()
        try:
            rr_zscore = _compute_rr_zscore(db_z, rr.get("rr_value", 0) or 0)
        finally:
            db_z.close()
    except Exception:
        rr_zscore = None
    try:
        tendency_data = compute_tendency(
            rr.get("rr_value", 0) or 0, vix, vix_change_pct or 0,
            rr_zscore=rr_zscore,
        )
    except Exception:
        tendency_data = {"label": "N/A"}
    try:
        dir_signal = compute_directional_signal(
            chain_df, spot,
            {"vix": vix, "vix_change": vix_change, "vix_change_pct": vix_change_pct},
            None
        ) or {}
    except Exception:
        dir_signal = {}
    try:
        max_pain = compute_max_pain(chain_df)
    except Exception:
        max_pain = 0

    # S/R levels for selected expiry
    oi_fib_sup, oi_fib_res = [], []
    try:
        oi_sr = find_oi_sr_levels(chain_df, spot)
        fib_levels = fetch_fibonacci_levels()
        merged = merge_sr_levels(oi_sr["supports"], oi_sr["resistances"], fib_levels, spot)
        if isinstance(merged, tuple):
            oi_fib_sup, oi_fib_res = merged
        elif isinstance(merged, dict):
            oi_fib_sup = merged.get("supports", [])
            oi_fib_res = merged.get("resistances", [])
    except Exception:
        pass

    pivot_sup, pivot_res = [], []
    try:
        pivot_data = fetch_pivot_levels()
        ps = compute_pivot_oi_sr(chain_df, spot, pivot_data)
        if isinstance(ps, tuple):
            pivot_sup, pivot_res = ps
        elif isinstance(ps, dict):
            pivot_sup = ps.get("supports", [])
            pivot_res = ps.get("resistances", [])
    except Exception:
        pass

    def _sr(items, kind):
        return [
            {
                "strike": _py(x.get("strike")),
                "oi": _py(x.get("oi")),
                "source": kind,
                "label": x.get("source") or x.get("label"),
            }
            for x in items
        ]

    sr_supports = _sr(oi_fib_sup, "OI_FIB") + _sr(pivot_sup, "PIVOT_OI")
    sr_resistances = _sr(oi_fib_res, "OI_FIB") + _sr(pivot_res, "PIVOT_OI")

    return {
        "nifty_spot": _py(spot),
        "nifty_change": _py(nifty_change),
        "nifty_change_pct": _py(nifty_change_pct),
        "india_vix": _py(vix),
        "vix_change": _py(vix_change),
        "vix_change_pct": _py(vix_change_pct),
        "calc_range_top": _py(ranges["calc_range_top"]),
        "calc_range_bottom": _py(ranges["calc_range_bottom"]),
        "vix_range_top": _py(ranges["vix_range_top"]),
        "vix_range_bottom": _py(ranges["vix_range_bottom"]),
        "days_to_expiry": dte,
        "tendency": tendency_data["label"],
        "tendency_detail": tendency_data,
        "directional_signal": dir_signal,
        "max_pain": _py(max_pain),
        "expiry_date": expiry,
        "expiries": [str(e) for e in expiries],
        "risk_reversal": rr,
        "rr_zscore": rr_zscore,
        "sr_levels": {"supports": sr_supports, "resistances": sr_resistances},
    }


@router.get("/sr-levels")
def get_sr_levels(
    source: str = Query(None),
    db: Session = Depends(get_db),
    _=Depends(get_current_user),
):
    """Get latest S/R levels, optionally filtered by source."""
    query = db.query(SRLevel).order_by(SRLevel.timestamp.desc())
    if source:
        query = query.filter(SRLevel.source == source)

    # Get the latest batch (same timestamp)
    latest = query.first()
    if not latest:
        return {"supports": [], "resistances": []}

    levels = query.filter(SRLevel.timestamp == latest.timestamp).all()

    supports = [
        {"strike": _py(l.strike), "oi": _py(l.oi), "source": _py(l.source), "label": _py(l.label)}
        for l in levels if l.level_type == "SUPPORT"
    ]
    resistances = [
        {"strike": _py(l.strike), "oi": _py(l.oi), "source": _py(l.source), "label": _py(l.label)}
        for l in levels if l.level_type == "RESISTANCE"
    ]
    
    return {"supports": supports, "resistances": resistances}


@router.get("/option-chain")
def get_option_chain(
    expiry: str = Query(None, description="Expiry in DD-Mon-YYYY format"),
    _=Depends(get_current_user),
):
    """Fetch live option chain from NSE."""
    from market_data import build_chain, compute_risk_reversal, premium_ranking

    try:
        chain_df, expiries, spot = build_chain("NIFTY", expiry)
    except Exception as e:
        return {"error": str(e), "data": [], "expiries": [], "spot": 0}

    rr = compute_risk_reversal(chain_df)

    # Build mirrored chain: group by strike, CE on left, PE on right
    strikes = sorted(chain_df["Strike"].unique())
    chain = []
    for strike in strikes:
        row = {"strike": _py(strike)}
        ce = chain_df[(chain_df["Strike"] == strike) & (chain_df["Type"] == "CE")]
        pe = chain_df[(chain_df["Strike"] == strike) & (chain_df["Type"] == "PE")]

        if not ce.empty:
            c = ce.iloc[0]
            row["ce_ltp"] = _py(c["LTP"])
            row["ce_iv"] = _py(c["IV"])
            row["ce_delta"] = _py(c["delta"])
            row["ce_oi"] = _py(c["OI"])
            row["ce_volume"] = _py(c["Volume"])
            row["ce_premium_rank"] = premium_ranking(float(c["LTP"]), int(strike), "CE")
        else:
            row["ce_ltp"] = row["ce_iv"] = row["ce_delta"] = 0
            row["ce_oi"] = row["ce_volume"] = 0
            row["ce_premium_rank"] = "N/A"

        if not pe.empty:
            p = pe.iloc[0]
            row["pe_ltp"] = _py(p["LTP"])
            row["pe_iv"] = _py(p["IV"])
            row["pe_delta"] = _py(p["delta"])
            row["pe_oi"] = _py(p["OI"])
            row["pe_volume"] = _py(p["Volume"])
            row["pe_premium_rank"] = premium_ranking(float(p["LTP"]), int(strike), "PE")
        else:
            row["pe_ltp"] = row["pe_iv"] = row["pe_delta"] = 0
            row["pe_oi"] = row["pe_volume"] = 0
            row["pe_premium_rank"] = "N/A"

        chain.append(row)

    return {
        "data": chain,
        "spot": _py(spot),
        "expiries": [str(e) for e in expiries],
        "risk_reversal": rr,
        "selected_expiry": expiry or (str(expiries[0]) if expiries else ""),
    }


@router.get("/strike-lookup")
def strike_lookup(
    strike: int = Query(...),
    option_type: str = Query(..., regex="^(CE|PE)$"),
    expiry: str = Query(None),
    _=Depends(get_current_user),
):
    """Look up details for a specific strike."""
    from market_data import build_chain, premium_ranking

    try:
        chain_df, expiries, spot = build_chain("NIFTY", expiry)
    except Exception as e:
        return {"error": str(e)}

    match = chain_df[(chain_df["Strike"] == strike) & (chain_df["Type"] == option_type)]
    if match.empty:
        return {"error": f"No data for {strike} {option_type}"}

    row = match.iloc[0]
    return {
        "strike": strike,
        "type": option_type,
        "ltp": _py(row["LTP"]),
        "iv": _py(row["IV"]),
        "delta": _py(row["delta"]),
        "gamma": _py(row["gamma"]),
        "theta": _py(row["theta"]),
        "oi": _py(row["OI"]),
        "volume": _py(row["Volume"]),
        "premium_rank": premium_ranking(float(row["LTP"]), strike, option_type),
        "spot": _py(spot),
    }


# ── IV Rank / IV Percentile ───────────────────────────────────────────────────

@router.get("/iv-rank")
def get_iv_rank(
    days: int = Query(252, ge=5, le=1000),
    db: Session = Depends(get_db),
    _=Depends(get_current_user),
):
    """Return IV Rank and IV Percentile from historical IVDailyRecord data.

    IV Rank   = (current - min) / (max - min) × 100
    IV %ile   = % of historical days where ATM IV < current
    Signal    = dual-threshold: BOTH rank AND percentile must exceed cutoffs.
    Data quality warning shown when history < 30 days.
    """
    import statistics
    from sqlalchemy import desc as _desc

    rows = (
        db.query(IVDailyRecord)
        .order_by(_desc(IVDailyRecord.date))
        .limit(days)
        .all()
    )

    if len(rows) < 5:
        return {
            "status": "no_data",
            "current_atm_iv": None,
            "iv_rank": None,
            "iv_percentile": None,
            "iv_52wk_high": None,
            "iv_52wk_low": None,
            "iv_mean": None,
            "iv_median": None,
            "data_days": len(rows),
            "signal": "Insufficient data (need ≥5 records)",
            "is_proxy_data": True,
            "low_data_warning": True,
        }

    iv_values = [r.atm_iv for r in rows]
    current_iv = iv_values[0]  # most recent (DESC order)
    iv_max = max(iv_values)
    iv_min = min(iv_values)
    n = len(iv_values)
    iv_mean = sum(iv_values) / n
    iv_median = statistics.median(iv_values)

    iv_rank = round((current_iv - iv_min) / (iv_max - iv_min) * 100, 1) if iv_max > iv_min else 50.0
    iv_pct = round(sum(1 for v in iv_values if v < current_iv) / n * 100, 1)

    # Dual-threshold signal — both rank AND percentile must agree
    if iv_rank >= 70 and iv_pct >= 70:
        signal = "Elevated – Selling favoured"
    elif iv_rank <= 30 and iv_pct <= 30:
        signal = "Depressed – Buying favoured"
    elif iv_rank >= 60 or iv_pct >= 60:
        signal = "Slightly elevated"
    elif iv_rank <= 40 or iv_pct <= 40:
        signal = "Slightly depressed"
    else:
        signal = "Neutral"

    # Data quality flags
    low_data = n < 30
    data_quality = f"{n}d{'  — low data' if low_data else ''}"
    any_proxy = any(r.is_proxy for r in rows[:5])

    return {
        "status": "ok",
        "current_atm_iv": round(current_iv, 2),
        "iv_rank": iv_rank,
        "iv_percentile": iv_pct,
        "iv_52wk_high": round(iv_max, 2),
        "iv_52wk_low": round(iv_min, 2),
        "iv_mean": round(iv_mean, 2),
        "iv_median": round(iv_median, 2),
        "data_days": n,
        "data_quality": data_quality,
        "signal": signal,
        "is_proxy_data": any_proxy,
        "low_data_warning": low_data,
    }


# ── Margin Calculator ─────────────────────────────────────────────────────────

from pydantic import BaseModel
from typing import List

class MarginLeg(BaseModel):
    strike: int
    option_type: str   # CE or PE
    action: str        # BUY or SELL
    lots: int = 1

class MarginRequest(BaseModel):
    strategy_type: str = "CUSTOM"
    expiry: str = ""
    legs: List[MarginLeg] = []


@router.post("/margin")
def calculate_margin(
    req: MarginRequest,
    db: Session = Depends(get_db),
    _=Depends(get_current_user),
):
    """Estimate SPAN + Exposure margin for a multi-leg options strategy.

    SPAN logic:
    - Spread = |buy_strike - sell_strike| × qty × 0.85
    - IRON_CONDOR / paired spreads: take max of CE spread and PE spread
    - Standalone short: spread_width = 200pts (proxy)
    Exposure = spot × qty_max_short × 0.03
    Total = SPAN + Exposure − net_premium_credit
    Expiry day = Total × 1.75
    """
    from market_data import build_chain
    from config import LOT_SIZE

    # Get current spot
    try:
        expiry = req.expiry or None
        chain_df, expiries, spot = build_chain("NIFTY", expiry)
    except Exception:
        snap = db.query(MarketSnapshot).order_by(MarketSnapshot.timestamp.desc()).first()
        spot = snap.nifty_spot if snap else 22000.0
        chain_df = None
        expiries = []

    # Get current LTP for each leg from chain
    def get_ltp(strike, opt_type):
        if chain_df is None or chain_df.empty:
            return 0.0
        row = chain_df[(chain_df["Strike"] == strike) & (chain_df["Type"] == opt_type)]
        return float(row["LTP"].iloc[0]) if not row.empty else 0.0

    qty_per_lot = LOT_SIZE
    legs = req.legs
    strategy_type = req.strategy_type.upper()

    # Pair up SELL legs with their protective BUY legs by option_type
    ce_sells = [l for l in legs if l.option_type == "CE" and l.action == "SELL"]
    ce_buys  = [l for l in legs if l.option_type == "CE" and l.action == "BUY"]
    pe_sells = [l for l in legs if l.option_type == "PE" and l.action == "SELL"]
    pe_buys  = [l for l in legs if l.option_type == "PE" and l.action == "BUY"]

    # Long-only configurations do not block exchange margin.  Max risk is the
    # premium paid; surface that explicitly so the user isn't left wondering
    # whether the response is missing data.
    has_any_sell = bool(ce_sells or pe_sells)
    if not has_any_sell:
        debit_breakdown = []
        total_debit = 0.0
        for leg in legs:
            qty = leg.lots * qty_per_lot
            ltp = get_ltp(leg.strike, leg.option_type)
            cost = round(ltp * qty, 2)
            total_debit += cost
            debit_breakdown.append({
                "label": f"{leg.option_type} BUY @ {leg.strike}",
                "span": 0,
                "buy_ltp": round(ltp, 2),
                "sell_ltp": 0,
                "premium_credit": -cost,   # negative = debit
                "qty": qty,
                "spread_width": 0,
            })
        return {
            "spot": round(spot, 2),
            "span": 0,
            "span_note": "No SPAN/Exposure block — long options have no margin requirement.",
            "exposure": 0,
            "net_premium_credit": -round(total_debit, 2),
            "total_margin": 0,
            "effective_cost": round(total_debit, 2),  # cash outlay = premium paid
            "expiry_day_margin": 0,
            "margin_per_lot": 0,
            "roi_on_margin": 0,
            "breakdown": debit_breakdown,
            "long_only": True,
            "max_loss": round(total_debit, 2),
            "note": (
                f"Long-only position: max loss = premium paid (₹{round(total_debit, 2):,.0f}). "
                f"No exchange margin block; profits are unbounded on calls / capped at strike on puts."
            ),
        }

    breakdown = []
    span_amounts = []
    net_premium = 0.0

    def _compute_spread(sell_leg, buy_leg, label):
        qty = sell_leg.lots * qty_per_lot
        spread_w = abs(sell_leg.strike - buy_leg.strike)
        span = spread_w * qty * 0.85
        sell_ltp = get_ltp(sell_leg.strike, sell_leg.option_type)
        buy_ltp  = get_ltp(buy_leg.strike,  buy_leg.option_type)
        premium_credit = (sell_ltp - buy_ltp) * qty
        return {"label": label, "span": round(span, 2),
                "sell_ltp": round(sell_ltp, 2), "buy_ltp": round(buy_ltp, 2),
                "premium_credit": round(premium_credit, 2), "qty": qty,
                "spread_width": spread_w}

    if ce_sells and ce_buys:
        for s, b in zip(ce_sells, ce_buys):
            entry = _compute_spread(s, b, "CE Spread (Bear Call)")
            breakdown.append(entry)
            span_amounts.append(("CE", entry["span"]))
            net_premium += entry["premium_credit"]

    if pe_sells and pe_buys:
        for s, b in zip(pe_sells, pe_buys):
            entry = _compute_spread(s, b, "PE Spread (Bull Put)")
            breakdown.append(entry)
            span_amounts.append(("PE", entry["span"]))
            net_premium += entry["premium_credit"]

    # Handle naked shorts (no paired buy)
    # NSE charges ~5% of notional as SPAN for naked index short options
    # (no spread/hedge discount — the 0.85 factor only applies to paired spreads)
    for sell_leg in ce_sells[len(ce_buys):]:
        qty = sell_leg.lots * qty_per_lot
        span = spot * qty * 0.05
        sell_ltp = get_ltp(sell_leg.strike, sell_leg.option_type)
        entry = {"label": "CE Naked Short (est.)", "span": round(span, 2),
                 "sell_ltp": round(sell_ltp, 2), "buy_ltp": 0,
                 "premium_credit": round(sell_ltp * qty, 2), "qty": qty, "spread_width": 0}
        breakdown.append(entry)
        span_amounts.append(("CE", span))
        net_premium += sell_ltp * qty

    for sell_leg in pe_sells[len(pe_buys):]:
        qty = sell_leg.lots * qty_per_lot
        span = spot * qty * 0.05
        sell_ltp = get_ltp(sell_leg.strike, sell_leg.option_type)
        entry = {"label": "PE Naked Short (est.)", "span": round(span, 2),
                 "sell_ltp": round(sell_ltp, 2), "buy_ltp": 0,
                 "premium_credit": round(sell_ltp * qty, 2), "qty": qty, "spread_width": 0}
        breakdown.append(entry)
        span_amounts.append(("PE", span))
        net_premium += sell_ltp * qty

    # SPAN aggregation — structure-based, not strategy_type-based
    ce_span = sum(v for k, v in span_amounts if k == "CE")
    pe_span = sum(v for k, v in span_amounts if k == "PE")

    # Whether each side is fully paired (sell + buy) — determines IC vs strangle logic
    ce_is_spread = len(ce_buys) >= len(ce_sells) > 0
    pe_is_spread = len(pe_buys) >= len(pe_sells) > 0
    is_paired_ic = ce_is_spread and pe_is_spread
    has_both_sides = ce_span > 0 and pe_span > 0

    # NSE one-side SPAN benefit:
    #   - Applies whenever BOTH CE and PE sides exist (IC *and* strangle both get it)
    #   - Exchange charges max(CE SPAN, PE SPAN) since Nifty can only breach one side
    #   - Confirmed by exchange data: total SPAN shown = max of the two legs' SPAN
    if has_both_sides:
        total_span = max(ce_span, pe_span)
        span_note = "One-side SPAN benefit: max(CE, PE) — NSE charges riskier side only"
    else:
        total_span = ce_span + pe_span
        span_note = "SPAN on single side"

    # Exposure margin: 2% of notional per sell leg
    #   - IC (both sides paired): exposure on one (riskier) side only
    #   - Strangle / naked / custom: exposure on every sell leg independently
    #   Confirmed: exchange shows ₹3,11,966 per leg × 2 for a strangle
    ce_sell_qty = sum(l.lots * qty_per_lot for l in legs if l.action == "SELL" and l.option_type == "CE")
    pe_sell_qty = sum(l.lots * qty_per_lot for l in legs if l.action == "SELL" and l.option_type == "PE")

    if is_paired_ic and ce_sell_qty > 0 and pe_sell_qty > 0:
        exposure = round(spot * max(ce_sell_qty, pe_sell_qty) * 0.02, 2)
    else:
        exposure = round(spot * (ce_sell_qty + pe_sell_qty) * 0.02, 2)

    # Exchange gross margin = SPAN + Exposure (premium is NOT deducted from margin requirement)
    # Premium received reduces the cash the trader needs to bring, but the
    # full SPAN + Exposure is always the exchange's margin block.
    total_margin = round(total_span + exposure, 2)
    effective_cost = round(max(total_margin - net_premium, 0), 2)
    expiry_day_margin = round(total_margin * 1.75, 2)

    # Per-lot and ROI metrics (ROI relative to effective cash outlay)
    total_sell_lots = sum(l.lots for l in legs if l.action == "SELL")
    margin_per_lot = round(total_margin / total_sell_lots, 2) if total_sell_lots > 0 else 0
    roi_on_margin = round(net_premium / effective_cost * 100, 2) if effective_cost > 0 else 0

    return {
        "spot": round(spot, 2),
        "span": round(total_span, 2),
        "span_note": span_note,
        "exposure": exposure,
        "net_premium_credit": round(net_premium, 2),
        "total_margin": total_margin,
        "effective_cost": effective_cost,
        "expiry_day_margin": expiry_day_margin,
        "margin_per_lot": margin_per_lot,
        "roi_on_margin": roi_on_margin,
        "breakdown": breakdown,
    }
