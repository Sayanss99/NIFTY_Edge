"""Stock-options screener API — /api/stocks/*

Serves the layered-filter screen results to the dedicated Stock Options page.
The heavy Step 1 + Step 2 prefilter runs in the background scheduler; these
endpoints apply the user's adjustable Step 3 thresholds against the cached
survivors (fast — reuses cached chains).
"""

from fastapi import APIRouter, Depends, Query
from auth import get_current_user

router = APIRouter(prefix="/api/stocks", tags=["stocks"])


@router.get("/fno-list")
def get_fno_list(_=Depends(get_current_user)):
    """F&O stock symbols (for the option-chain search datalist)."""
    from stock_screener import fno_universe
    return {"symbols": fno_universe()}


@router.get("/most-active")
def get_most_active(limit: int = Query(8, ge=1, le=20), _=Depends(get_current_user)):
    """Live, volume-ranked most-active stock-option contracts (stock options only)."""
    from stock_screener import most_active_stock_options
    return {"rows": most_active_stock_options(limit)}


@router.get("/confirmed-history")
def get_confirmed_history_route(_=Depends(get_current_user)):
    """Track Record — confirmed suggestions that aged out of the live tab (≥ 2
    days), each with its entry snapshot and a live re-evaluation of whether it's
    still tradable under the confirmed criterion (DeMark 9 + RSI + Step-3 gates)."""
    from stock_screener import get_confirmed_history
    return {"rows": get_confirmed_history()}


@router.get("/screener")
def get_screener(
    ivp_min: float = Query(45.0),
    ivp_max: float = Query(70.0),
    min_yield: float = Query(12.0),
    delta_min: float = Query(0.10),
    delta_max: float = Query(0.25),
    _=Depends(get_current_user),
):
    """Confirmed stock-option suggestions + DeMark radar, under the given
    Step-3 thresholds (adjustable via the page's sliders).

    When the market is closed, serve the frozen close snapshot (the picks shown at
    3:30 PM) and do NOT live-fetch — the background scheduler captures it once after
    close. During market hours this is the normal live screen, unchanged."""
    from snapshots import market_closed, close_snapshot, load_snapshot
    if market_closed():
        snap = close_snapshot("screener") or load_snapshot("screener")
        if snap and snap.get("payload"):
            out = dict(snap["payload"])
            meta = dict(out.get("meta") or {})
            meta["closed_snapshot"] = True
            meta["snapshot_at"] = snap.get("captured_at")
            out["meta"] = meta
            return out
        # Snapshot not built yet (scheduler is capturing it) — show the building state.
        return {"confirmed": [], "radar": [],
                "meta": {"universe": 0, "closed_snapshot": True, "snapshot_pending": True}}

    from stock_screener import get_screen_results
    thresholds = {
        "ivp_min": ivp_min, "ivp_max": ivp_max, "min_yield": min_yield,
        "delta_min": delta_min, "delta_max": delta_max,
    }
    return get_screen_results(thresholds)


@router.get("/suggest")
def suggest(
    symbol: str = Query(...),
    side: str = Query(..., pattern="^(CE|PE)$"),
    ivp_min: float = Query(45.0),
    ivp_max: float = Query(70.0),
    min_yield: float = Query(12.0),
    delta_min: float = Query(0.10),
    delta_max: float = Query(0.25),
    _=Depends(get_current_user),
):
    """Build a live sell suggestion for a specific symbol + side (used by the
    confirmation flow from both confirmed and radar cards). Returns the best
    strike with live IVP/premium-yield and per-gate flags; does not hard-reject
    so the user can review even a sub-threshold strike."""
    from stock_screener import build_stock_suggestion
    thresholds = {"ivp_min": ivp_min, "ivp_max": ivp_max, "min_yield": min_yield,
                  "delta_min": delta_min, "delta_max": delta_max}
    # rsi={} → skip the (slow) multi-timeframe yfinance fetch; the confirmation
    # modal only needs the live strike/premium/IVP, and the side is already
    # chosen by the user, so the directional bias from forward-vs-spot suffices.
    try:
        sug = build_stock_suggestion((symbol or "").upper(), thresholds,
                                     force_side=side, enforce_gates=False, rsi={})
    except Exception as e:
        return {"error": f"Could not build suggestion for {symbol}: {e}"}
    if not sug:
        return {"error": f"No tradable {side} strike found for {symbol}"}
    return sug


@router.post("/refresh")
def refresh_prefilter(limit: int | None = Query(None), _=Depends(get_current_user)):
    """Manually re-run the DeMark-9 prefilter (also runs periodically in the
    background). `limit` caps the universe for a quick refresh."""
    from stock_screener import run_prefilter
    state = run_prefilter(limit=limit)
    return {
        "universe": state["universe"],
        "pool": len(state["pool"]),
        "ts": state["ts"],
    }
