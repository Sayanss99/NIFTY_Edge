"""Futures API — live NIFTY index-futures contracts, user holdings,
margin estimate, and the "execute hedge" flow that persists a futures
hedge as a child Trade row attached to its parent option position.

All data flows through `nse_futures.fetch_nifty_futures()` which guards
the response with basis/lot/inversion sanity checks (see that module's
docstring) and surfaces freshness via `fetched_at` + `stale`.
"""

from datetime import datetime
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from database import get_db
from auth import get_current_user
from models import Trade, TradeStatus, User
from nse_futures import (
    fetch_nifty_futures,
    get_contract_by_expiry,
    estimate_futures_margin,
    NIFTY_LOT,
)

router = APIRouter(prefix="/api/futures", tags=["futures"])


@router.get("/contracts")
def list_contracts(_=Depends(get_current_user)):
    """Live snapshot of all available NIFTY futures contracts (current,
    next, far month). Cross-validated; warnings surfaced for the UI."""
    return fetch_nifty_futures()


@router.get("/positions")
def list_positions(
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """User's open futures trades, marked-to-market against the live LTP."""
    rows = db.query(Trade).filter(
        Trade.user_id == user.id,
        Trade.instrument_type == "FUT",
        Trade.status == TradeStatus.OPEN.value,
    ).order_by(Trade.entry_date.desc()).all()

    if not rows:
        return {"positions": [], "live": {}, "fetched_at": None}

    fut_data = fetch_nifty_futures()
    by_expiry = {c["expiry_date"]: c for c in fut_data["contracts"]}

    positions = []
    aggregate_pnl = 0.0
    aggregate_margin = 0.0
    for t in rows:
        contract = by_expiry.get(t.expiry_date)
        ltp = contract["ltp"] if contract else 0
        lot = t.lot_size_override or NIFTY_LOT
        qty = t.quantity * lot
        side = "LONG" if t.trade_type == "FUT_BUY" else "SHORT"
        # MTM: long profits when LTP > entry; short profits when LTP < entry.
        mtm = (ltp - t.entry_premium) * qty if side == "LONG" else (t.entry_premium - ltp) * qty
        margin = estimate_futures_margin(t.entry_premium, t.quantity, lot)["total"]
        aggregate_pnl += mtm
        aggregate_margin += margin
        positions.append({
            "trade_id": t.id,
            "side": side,
            "trade_type": t.trade_type,
            "expiry_date": t.expiry_date,
            "entry_price": t.entry_premium,
            "current_ltp": ltp,
            "lots": t.quantity,
            "lot_size": lot,
            "contract_value": round(ltp * qty, 2) if ltp else 0,
            "margin_blocked": margin,
            "unrealized_pnl": round(mtm, 2),
            "parent_trade_id": t.parent_trade_id,
            "verified": (contract or {}).get("verified", True),
        })

    return {
        "positions": positions,
        "totals": {
            "unrealized_pnl": round(aggregate_pnl, 2),
            "margin_blocked": round(aggregate_margin, 2),
            "open_count": len(positions),
        },
        "fetched_at": fut_data.get("fetched_at"),
        "stale": fut_data.get("stale", False),
        "warnings": fut_data.get("warnings", []),
    }


class PlaceFuturesRequest(BaseModel):
    expiry_date: str          # which futures contract (DD-Mon-YYYY)
    side: str                 # "BUY" or "SELL"
    lots: int = 1


@router.post("/place")
def place_futures_trade(
    req: PlaceFuturesRequest,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Place a direct NIFTY-FUT trade (not a hedge — standalone position).

    Persists as a Trade row with `instrument_type='FUT'` and no parent. The
    entry price is taken from the live (or PCP-derived) NSE quote at the
    moment of confirmation, after passing the basis sanity check from
    `nse_futures.fetch_nifty_futures()`.
    """
    if req.side.upper() not in ("BUY", "SELL"):
        raise HTTPException(status_code=400, detail="side must be BUY or SELL")
    if req.lots <= 0:
        raise HTTPException(status_code=400, detail="lots must be positive")

    contract = get_contract_by_expiry(req.expiry_date)
    if not contract:
        raise HTTPException(status_code=404, detail=f"No futures contract for {req.expiry_date}")
    if not contract.get("verified", True):
        raise HTTPException(
            status_code=503,
            detail=f"Live LTP for {req.expiry_date} failed sanity check (basis {contract.get('basis_pct')}%); refusing to book.",
        )

    side = req.side.upper()
    trade = Trade(
        user_id=user.id,
        trade_type=f"FUT_{side}",
        instrument_type="FUT",
        lot_size_override=contract["lot_size"],
        strike=0,                           # futures have no strike
        expiry_date=req.expiry_date,
        entry_premium=contract["ltp"],      # entry price for futures
        entry_iv=0,
        entry_delta=1.0 if side == "BUY" else -1.0,
        quantity=req.lots,
        status=TradeStatus.OPEN.value,
        parent_trade_id=None,               # standalone, not a hedge
    )
    db.add(trade)
    db.commit()
    db.refresh(trade)

    margin = estimate_futures_margin(contract["ltp"], req.lots, contract["lot_size"])
    return {
        "trade_id": trade.id,
        "side": side,
        "expiry_date": req.expiry_date,
        "entry_price": contract["ltp"],
        "lots": req.lots,
        "lot_size": contract["lot_size"],
        "contract_value": margin["contract_value"],
        "margin_blocked": margin["total"],
        "fetched_at": datetime.utcnow().isoformat() + "Z",
    }


class PlaceHedgeRequest(BaseModel):
    parent_trade_id: int      # the option leg this hedge is protecting
    expiry_date: str          # which futures contract (DD-Mon-YYYY)
    side: str                 # "BUY" or "SELL"
    lots: int = 1


@router.post("/place-hedge")
def place_hedge(
    req: PlaceHedgeRequest,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Persist an accepted futures-delta-hedge suggestion as a child Trade.

    The hedge is linked to the parent option leg via `parent_trade_id` so
    Positions / Risk views can render it under the protected position, and
    the FUT instrument_type keeps it cleanly separated from option trades
    in every aggregation.
    """
    parent = db.query(Trade).filter(
        Trade.id == req.parent_trade_id,
        Trade.user_id == user.id,
    ).first()
    if not parent:
        raise HTTPException(status_code=404, detail="Parent trade not found")
    if parent.status != TradeStatus.OPEN.value:
        raise HTTPException(status_code=400, detail=f"Parent trade is {parent.status}; cannot attach hedge.")
    if req.side.upper() not in ("BUY", "SELL"):
        raise HTTPException(status_code=400, detail="side must be BUY or SELL")
    if req.lots <= 0:
        raise HTTPException(status_code=400, detail="lots must be positive")

    contract = get_contract_by_expiry(req.expiry_date)
    if not contract:
        raise HTTPException(status_code=404, detail=f"No futures contract for {req.expiry_date}")
    if not contract.get("verified", True):
        # Don't silently book a hedge against a suspicious quote.
        raise HTTPException(
            status_code=503,
            detail=f"Live LTP for {req.expiry_date} failed sanity check (basis {contract.get('basis_pct')}%); refusing to book.",
        )

    side = req.side.upper()
    trade = Trade(
        user_id=user.id,
        trade_type=f"FUT_{side}",
        instrument_type="FUT",
        lot_size_override=contract["lot_size"],
        strike=0,                            # futures have no strike
        expiry_date=req.expiry_date,
        entry_premium=contract["ltp"],       # for futures this is the entry price
        entry_iv=0,
        entry_delta=1.0 if side == "BUY" else -1.0,
        quantity=req.lots,
        status=TradeStatus.OPEN.value,
        parent_trade_id=parent.id,
    )
    db.add(trade)
    db.commit()
    db.refresh(trade)

    margin = estimate_futures_margin(contract["ltp"], req.lots, contract["lot_size"])
    return {
        "trade_id": trade.id,
        "parent_trade_id": parent.id,
        "side": side,
        "expiry_date": req.expiry_date,
        "entry_price": contract["ltp"],
        "lots": req.lots,
        "lot_size": contract["lot_size"],
        "contract_value": margin["contract_value"],
        "margin_blocked": margin["total"],
        "fetched_at": datetime.utcnow().isoformat() + "Z",
    }


@router.post("/{trade_id}/close")
def close_futures_position(
    trade_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Close an open futures position at the live LTP."""
    t = db.query(Trade).filter(
        Trade.id == trade_id,
        Trade.user_id == user.id,
        Trade.instrument_type == "FUT",
    ).first()
    if not t:
        raise HTTPException(status_code=404, detail="Futures trade not found")
    if t.status != TradeStatus.OPEN.value:
        raise HTTPException(status_code=400, detail=f"Trade is already {t.status}")

    contract = get_contract_by_expiry(t.expiry_date)
    if not contract:
        raise HTTPException(status_code=503, detail="Cannot close — no live quote for contract")

    exit_price = contract["ltp"]
    lot = t.lot_size_override or NIFTY_LOT
    qty = t.quantity * lot
    if t.trade_type == "FUT_BUY":
        realized = (exit_price - t.entry_premium) * qty
    else:
        realized = (t.entry_premium - exit_price) * qty

    t.exit_price = exit_price
    t.exit_date = datetime.utcnow()
    t.status = TradeStatus.CLOSED.value
    t.realized_pnl = round(realized, 2)
    # Promote any open child trades to top-level so they keep showing in
    # Open Positions after this leg closes (mirrors the option close path).
    open_children = db.query(Trade).filter(
        Trade.parent_trade_id == t.id,
        Trade.status == TradeStatus.OPEN.value,
    ).all()
    for child in open_children:
        child.parent_trade_id = None
    db.commit()

    return {
        "trade_id": t.id,
        "exit_price": exit_price,
        "realized_pnl": t.realized_pnl,
    }


@router.get("/margin-estimate")
def margin_estimate(
    expiry_date: str,
    lots: int = 1,
    _=Depends(get_current_user),
):
    """SPAN+Exposure estimate for `lots` of the named futures contract."""
    contract = get_contract_by_expiry(expiry_date)
    if not contract:
        raise HTTPException(status_code=404, detail=f"No contract for {expiry_date}")
    return {
        "expiry_date": expiry_date,
        "ltp": contract["ltp"],
        "lots": lots,
        **estimate_futures_margin(contract["ltp"], lots, contract["lot_size"]),
        "verified": contract.get("verified", True),
        "basis_pct": contract.get("basis_pct"),
    }
