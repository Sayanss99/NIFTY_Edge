"""Market data API routes — live data, option chain, S/R levels."""

import numpy as np
from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session
from database import get_db
from models import MarketSnapshot, SRLevel
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
    """Live list of available NIFTY expiries from NSE — works in market-closed hours too."""
    from market_data import build_chain
    try:
        _, expiries, spot = build_chain("NIFTY")
        return {"expiries": [str(e) for e in expiries], "spot": _py(spot)}
    except Exception as e:
        return {"error": str(e), "expiries": []}


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
    try:
        tendency_data = compute_tendency(rr.get("rr_value", 0) or 0, vix, vix_change_pct or 0)
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
