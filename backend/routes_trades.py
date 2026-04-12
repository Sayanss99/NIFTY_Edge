"""Trade/position management API routes."""

from datetime import datetime
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session
from pydantic import BaseModel
from database import get_db
from models import Trade, TradeStatus, User
from auth import get_current_user
from config import LOT_SIZE

router = APIRouter(prefix="/api/trades", tags=["trades"])


class AddTradeRequest(BaseModel):
    trade_type: str  # CE_SELL, PE_SELL, CE_BUY, PE_BUY
    strike: int
    expiry_date: str  # DD-Mon-YYYY
    entry_premium: float
    quantity: int = 1  # lots
    entry_iv: float = 0.0
    entry_delta: float = 0.0
    parent_trade_id: int | None = None


class CloseTradeRequest(BaseModel):
    exit_price: float
    close_quantity: int | None = None  # None = close all


@router.get("/")
def list_trades(
    status: str = Query(None),
    expiry: str = Query(None),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """List user's trades, optionally filtered by status and expiry."""
    query = db.query(Trade).filter(Trade.user_id == user.id)
    if status:
        query = query.filter(Trade.status == status)
    if expiry:
        query = query.filter(Trade.expiry_date == expiry)

    trades = query.order_by(Trade.entry_date.desc()).all()

    result = []
    for t in trades:
        trade_dict = {
            "id": t.id,
            "trade_type": t.trade_type,
            "strike": t.strike,
            "expiry_date": t.expiry_date,
            "entry_premium": t.entry_premium,
            "entry_iv": t.entry_iv,
            "entry_delta": t.entry_delta,
            "entry_date": t.entry_date.isoformat() if t.entry_date else None,
            "quantity": t.quantity,
            "status": t.status,
            "exit_price": t.exit_price,
            "exit_date": t.exit_date.isoformat() if t.exit_date else None,
            "realized_pnl": t.realized_pnl,
            "parent_trade_id": t.parent_trade_id,
            "hedges": [],
        }
        # Load hedges
        for h in t.hedges:
            trade_dict["hedges"].append({
                "id": h.id,
                "trade_type": h.trade_type,
                "strike": h.strike,
                "entry_premium": h.entry_premium,
                "entry_iv": h.entry_iv,
                "entry_delta": h.entry_delta,
                "entry_date": h.entry_date.isoformat() if h.entry_date else None,
                "quantity": h.quantity,
                "status": h.status,
                "exit_price": h.exit_price,
                "realized_pnl": h.realized_pnl,
            })
        result.append(trade_dict)

    return {"trades": result}


@router.post("/")
def add_trade(
    req: AddTradeRequest,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Add a new trade position."""
    valid_types = ["CE_SELL", "PE_SELL", "CE_BUY", "PE_BUY"]
    if req.trade_type not in valid_types:
        raise HTTPException(status_code=400, detail=f"Invalid trade_type. Must be one of {valid_types}")

    if req.parent_trade_id:
        parent = db.query(Trade).filter(
            Trade.id == req.parent_trade_id,
            Trade.user_id == user.id,
            Trade.status == TradeStatus.OPEN.value,
        ).first()
        if not parent:
            raise HTTPException(status_code=404, detail="Parent trade not found or already closed")

    trade = Trade(
        user_id=user.id,
        trade_type=req.trade_type,
        strike=req.strike,
        expiry_date=req.expiry_date,
        entry_premium=req.entry_premium,
        entry_iv=req.entry_iv,
        entry_delta=req.entry_delta,
        quantity=req.quantity,
        parent_trade_id=req.parent_trade_id,
    )
    db.add(trade)
    db.commit()
    db.refresh(trade)

    return {"id": trade.id, "message": "Trade added successfully"}


@router.post("/{trade_id}/close")
def close_trade(
    trade_id: int,
    req: CloseTradeRequest,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Close an open trade position (full or partial)."""
    trade = db.query(Trade).filter(
        Trade.id == trade_id,
        Trade.user_id == user.id,
        Trade.status == TradeStatus.OPEN.value,
    ).first()
    if not trade:
        raise HTTPException(status_code=404, detail="Trade not found or already closed")

    close_qty = req.close_quantity or trade.quantity

    if close_qty > trade.quantity:
        raise HTTPException(status_code=400, detail="Close quantity exceeds open quantity")

    # Calculate realized P&L
    if trade.trade_type in ("CE_SELL", "PE_SELL"):
        pnl_per_lot = (trade.entry_premium - req.exit_price) * LOT_SIZE
    else:
        pnl_per_lot = (req.exit_price - trade.entry_premium) * LOT_SIZE

    realized_pnl = round(pnl_per_lot * close_qty, 2)

    if close_qty == trade.quantity:
        # Full close
        trade.exit_price = req.exit_price
        trade.exit_date = datetime.utcnow()
        trade.status = TradeStatus.CLOSED.value
        trade.realized_pnl = realized_pnl
    else:
        # Partial close: reduce quantity and create a closed record
        trade.quantity -= close_qty
        closed_trade = Trade(
            user_id=user.id,
            trade_type=trade.trade_type,
            strike=trade.strike,
            expiry_date=trade.expiry_date,
            entry_premium=trade.entry_premium,
            entry_iv=trade.entry_iv,
            entry_delta=trade.entry_delta,
            entry_date=trade.entry_date,
            quantity=close_qty,
            exit_price=req.exit_price,
            exit_date=datetime.utcnow(),
            status=TradeStatus.CLOSED.value,
            realized_pnl=realized_pnl,
            parent_trade_id=trade.parent_trade_id,
        )
        db.add(closed_trade)

    db.commit()

    return {
        "message": "Trade closed successfully",
        "realized_pnl": realized_pnl,
        "close_quantity": close_qty,
    }


@router.get("/journal/summary")
def journal_summary(
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Aggregate P&L stats for closed trades."""
    closed = db.query(Trade).filter(
        Trade.user_id == user.id,
        Trade.status == TradeStatus.CLOSED.value,
    ).all()

    if not closed:
        return {
            "total_pnl": 0,
            "trade_count": 0,
            "win_count": 0,
            "loss_count": 0,
            "win_rate": 0,
            "avg_pnl": 0,
            "best_trade": None,
            "worst_trade": None,
            "month_pnl": 0,
        }

    pnls = [t.realized_pnl or 0 for t in closed]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]

    now = datetime.utcnow()
    month_trades = [t for t in closed if t.exit_date and t.exit_date.month == now.month and t.exit_date.year == now.year]
    month_pnl = sum(t.realized_pnl or 0 for t in month_trades)

    best = max(closed, key=lambda t: t.realized_pnl or 0)
    worst = min(closed, key=lambda t: t.realized_pnl or 0)

    def _trade_brief(t):
        return {
            "id": t.id, "trade_type": t.trade_type, "strike": t.strike,
            "expiry_date": t.expiry_date, "realized_pnl": t.realized_pnl,
        }

    return {
        "total_pnl": round(sum(pnls), 2),
        "trade_count": len(closed),
        "win_count": len(wins),
        "loss_count": len(losses),
        "win_rate": round(len(wins) / len(closed) * 100, 1) if closed else 0,
        "avg_pnl": round(sum(pnls) / len(pnls), 2),
        "best_trade": _trade_brief(best),
        "worst_trade": _trade_brief(worst),
        "month_pnl": round(month_pnl, 2),
    }


@router.get("/journal/export")
def export_journal_csv(
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Export closed trades as CSV download."""
    from fastapi.responses import StreamingResponse
    import io, csv

    closed = db.query(Trade).filter(
        Trade.user_id == user.id,
        Trade.status == TradeStatus.CLOSED.value,
    ).order_by(Trade.exit_date.desc()).all()

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow([
        "Exit Date", "Type", "Strike", "Expiry", "Entry Premium",
        "Exit Price", "Quantity", "Realized P&L", "Entry IV",
        "Entry Delta", "Entry Date", "Duration (days)",
    ])

    for t in closed:
        entry_d = t.entry_date
        exit_d = t.exit_date
        duration = (exit_d - entry_d).days if entry_d and exit_d else 0
        writer.writerow([
            exit_d.strftime("%Y-%m-%d %H:%M") if exit_d else "",
            t.trade_type,
            t.strike,
            t.expiry_date,
            t.entry_premium,
            t.exit_price,
            t.quantity,
            t.realized_pnl,
            t.entry_iv,
            t.entry_delta,
            entry_d.strftime("%Y-%m-%d %H:%M") if entry_d else "",
            duration,
        ])

    output.seek(0)
    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=niftyedge_trades.csv"},
    )


@router.get("/risk")
def get_risk_scores(
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Compute risk scores for all open positions."""
    from market_data import build_chain, compute_risk_score, days_to_expiry

    open_trades = db.query(Trade).filter(
        Trade.user_id == user.id,
        Trade.status == TradeStatus.OPEN.value,
        Trade.parent_trade_id.is_(None),  # only sell positions, not hedges
    ).all()

    if not open_trades:
        return {"ce_risk": None, "pe_risk": None, "positions": []}

    # Get the expiry from first trade (all should share expiry for risk calc)
    expiry = open_trades[0].expiry_date
    dte = days_to_expiry(expiry) if expiry else 30

    try:
        chain_df, _, spot = build_chain("NIFTY", expiry)
    except Exception:
        return {"error": "Could not fetch market data", "positions": []}

    from models import MarketSnapshot
    snap = db.query(MarketSnapshot).order_by(MarketSnapshot.timestamp.desc()).first()
    vix = snap.india_vix if snap else 0
    vix_chg_pct = snap.vix_change_pct if snap else 0

    ce_scores = []
    pe_scores = []
    positions = []

    for trade in open_trades:
        opt_type = "CE" if "CE" in trade.trade_type else "PE"
        match = chain_df[(chain_df["Strike"] == trade.strike) & (chain_df["Type"] == opt_type)]

        if match.empty:
            continue

        row = match.iloc[0]
        risk = compute_risk_score(
            delta=row["delta"],
            entry_iv=trade.entry_iv,
            current_iv=row["IV"],
            entry_premium=trade.entry_premium,
            current_ltp=row["LTP"],
            current_spot=spot,
            strike=trade.strike,
            vix_abs=vix,
            gamma=float(row.get("gamma", 0)),
            theta=float(row.get("theta", 0)),
            dte=dte,
            vix_chg_pct=vix_chg_pct,
        )

        position_data = {
            "trade_id": trade.id,
            "trade_type": trade.trade_type,
            "strike": trade.strike,
            "current_ltp": row["LTP"],
            "current_iv": row["IV"],
            "current_delta": row["delta"],
            "iv_change_pct": round(((row["IV"] - trade.entry_iv) / trade.entry_iv * 100) if trade.entry_iv > 0 else 0, 1),
            "unrealized_pnl": round(
                (trade.entry_premium - row["LTP"]) * LOT_SIZE * trade.quantity
                if "SELL" in trade.trade_type
                else (row["LTP"] - trade.entry_premium) * LOT_SIZE * trade.quantity,
                2
            ),
            "risk": risk,
        }
        positions.append(position_data)

        if "CE" in trade.trade_type:
            ce_scores.append(risk["score"])
        else:
            pe_scores.append(risk["score"])

    ce_risk = round(max(ce_scores), 1) if ce_scores else None
    pe_risk = round(max(pe_scores), 1) if pe_scores else None

    return {
        "ce_risk": {"score": ce_risk, "label": _risk_label(ce_risk)} if ce_risk is not None else None,
        "pe_risk": {"score": pe_risk, "label": _risk_label(pe_risk)} if pe_risk is not None else None,
        "positions": positions,
        "spot": spot,
    }


def _risk_label(score):
    if score is None: return "N/A"
    if score < 35: return "Safe"
    if score < 60: return "Warning"
    if score < 80: return "Danger"
    return "Critical"
