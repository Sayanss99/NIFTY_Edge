"""Trade/position management API routes."""

from datetime import datetime, timezone, timedelta
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session
from pydantic import BaseModel
from database import get_db
from models import Trade, TradeAnnotation, TradeStatus, User
from auth import get_current_user
from config import LOT_SIZE

_IST = timezone(timedelta(hours=5, minutes=30))


def _today_realized_pnl(db: Session, user_id: int) -> float:
    """Sum realised P&L of trades closed today (IST).  Used by the daily
    loss-stop guard.  Negative = today is in the red.  Includes both option
    and futures legs since they all share the same realized_pnl column."""
    today_ist = datetime.now(_IST).date()
    rows = db.query(Trade).filter(
        Trade.user_id == user_id,
        Trade.status == TradeStatus.CLOSED.value,
    ).all()
    total = 0.0
    for t in rows:
        if not t.exit_date:
            continue
        # exit_date is naive UTC; treat midnight-day comparison via IST shift
        ex_ist = (t.exit_date + timedelta(hours=5, minutes=30)).date()
        if ex_ist == today_ist and t.realized_pnl is not None:
            total += float(t.realized_pnl)
    return total


def _expire_past_trades(db: Session, user_id: int) -> int:
    """Auto-transition any OPEN trades whose expiry date has passed (IST) to EXPIRED.

    This is a lightweight cleanup that runs before position queries so that
    expired options never appear in the "open positions" views.
    Returns the count of trades transitioned.
    """
    today_ist = datetime.now(_IST).date()

    open_trades = db.query(Trade).filter(
        Trade.user_id == user_id,
        Trade.status == TradeStatus.OPEN.value,
    ).all()

    count = 0
    for t in open_trades:
        if not t.expiry_date:
            continue
        try:
            exp_date = datetime.strptime(t.expiry_date, "%d-%b-%Y").date()
        except ValueError:
            continue
        if exp_date < today_ist:          # strictly past — today's expiry is still live
            t.status = TradeStatus.EXPIRED.value
            if not t.exit_date:
                t.exit_date = datetime.utcnow()
            count += 1

    if count:
        # Detach OPEN children whose parent just expired so they keep showing
        # as standalone open positions instead of disappearing under a now-
        # EXPIRED parent. Mirrors the close-path promotion convention.
        expired_ids = [t.id for t in open_trades if t.status == TradeStatus.EXPIRED.value]
        if expired_ids:
            orphaned_children = db.query(Trade).filter(
                Trade.user_id == user_id,
                Trade.status == TradeStatus.OPEN.value,
                Trade.parent_trade_id.in_(expired_ids),
            ).all()
            for child in orphaned_children:
                child.parent_trade_id = None
        db.commit()
        print(f"[Trades] Auto-expired {count} trade(s) for user {user_id}")

    return count

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
    # Daily-loss circuit-breaker: when the user's realised loss for today
    # exceeds `max_daily_loss_inr`, POST /api/trades/ refuses to write the
    # row and returns `{requires_override: true, ...}` instead.  Re-posting
    # with this flag set to True acknowledges the breach and books anyway.
    confirm_override: bool = False


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
    # Sweep expired trades into EXPIRED status before returning the list
    # so the positions page never shows stale open positions.
    _expire_past_trades(db, user.id)

    query = db.query(Trade).filter(Trade.user_id == user.id)
    if status:
        if status == TradeStatus.CLOSED.value:
            # EXPIRED trades are effectively closed — include them in the "Closed" view
            # so users can still review their trade history for expired positions.
            query = query.filter(
                Trade.status.in_([TradeStatus.CLOSED.value, TradeStatus.EXPIRED.value])
            )
        else:
            query = query.filter(Trade.status == status)
    if expiry:
        query = query.filter(Trade.expiry_date == expiry)

    trades = query.order_by(Trade.entry_date.desc()).all()

    result = []
    for t in trades:
        trade_dict = {
            "id": t.id,
            "trade_type": t.trade_type,
            "instrument_type": t.instrument_type or "OPT",
            "lot_size_override": t.lot_size_override,
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
                "instrument_type": h.instrument_type or "OPT",
                "lot_size_override": h.lot_size_override,
                "strike": h.strike,
                "expiry_date": h.expiry_date,
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

    # ── Daily loss-stop gate (Batch C.1, warn + soft-block UX) ──────────
    # If today's realised loss has already exceeded the user's configured
    # max_daily_loss_inr, return a 200 with `requires_override` so the
    # frontend can show a confirmation dialog.  The user then re-posts
    # with confirm_override=True to acknowledge and book anyway.
    threshold = (user.settings or {}).get("max_daily_loss_inr")
    if threshold and threshold > 0 and not req.confirm_override:
        today_pnl = _today_realized_pnl(db, user.id)
        if today_pnl <= -float(threshold):
            return {
                "requires_override":  True,
                "today_pnl":          round(today_pnl, 2),
                "threshold":          float(threshold),
                "message": (
                    f"Daily loss ₹{int(abs(today_pnl)):,} exceeds your ₹"
                    f"{int(threshold):,} limit. Re-submit with confirm_override "
                    "to book anyway."
                ),
            }

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

    # Calculate realized P&L. Futures use their own lot size override
    # (currently the same 65, but kept overridable in case NSE bumps it).
    lot = trade.lot_size_override if (trade.instrument_type == "FUT" and trade.lot_size_override) else LOT_SIZE
    is_short_side = trade.trade_type in ("CE_SELL", "PE_SELL", "FUT_SELL")
    if is_short_side:
        pnl_per_lot = (trade.entry_premium - req.exit_price) * lot
    else:
        pnl_per_lot = (req.exit_price - trade.entry_premium) * lot

    realized_pnl = round(pnl_per_lot * close_qty, 2)

    if close_qty == trade.quantity:
        # Full close
        trade.exit_price = req.exit_price
        trade.exit_date = datetime.utcnow()
        trade.status = TradeStatus.CLOSED.value
        trade.realized_pnl = realized_pnl

        # Detach any open child trades (hedges, futures hedges, rolled legs)
        # so they keep showing in Open Positions as standalone trades. There
        # is still money in those legs — closing the parent must not orphan
        # them out of the user's view. We null parent_trade_id rather than
        # using a "promoted" flag so existing list filters
        # (`parent_trade_id IS NULL` → top-level) handle it automatically.
        open_children = db.query(Trade).filter(
            Trade.parent_trade_id == trade.id,
            Trade.status == TradeStatus.OPEN.value,
        ).all()
        for child in open_children:
            child.parent_trade_id = None
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
    """Compute risk scores for all open positions.

    Each trade is looked up in the option chain for *its own expiry*, so positions
    across different weekly/monthly expiries are all priced correctly.
    Expired trades (expiry date passed) are auto-transitioned before the query.
    """
    from market_data import build_chain, compute_risk_score, days_to_expiry

    # Sweep expired trades first — this is the "background cleanup on dashboard load"
    _expire_past_trades(db, user.id)

    open_trades = db.query(Trade).filter(
        Trade.user_id == user.id,
        Trade.status == TradeStatus.OPEN.value,
        Trade.parent_trade_id.is_(None),  # only parent (sell) positions, not hedges
    ).all()

    if not open_trades:
        return {
            "ce_risk": None, "pe_risk": None, "positions": [],
            "portfolio_greeks": {"delta": 0.0, "gamma": 0.0, "theta": 0.0, "vega": 0.0},
        }

    from models import MarketSnapshot
    snap = db.query(MarketSnapshot).order_by(MarketSnapshot.timestamp.desc()).first()
    vix = snap.india_vix if snap else 0
    vix_chg_pct = snap.vix_change_pct if snap else 0

    # Build a per-expiry chain cache — one NSE call per unique expiry among open trades
    unique_expiries = {t.expiry_date for t in open_trades if t.expiry_date}
    chain_by_expiry: dict = {}
    spot = 0.0
    for exp in unique_expiries:
        try:
            df, _, sp = build_chain("NIFTY", exp)
            chain_by_expiry[exp] = df
            if sp:
                spot = float(sp)  # spot is the same regardless of expiry
        except Exception:
            pass  # trade will be skipped below if its chain is missing

    if not chain_by_expiry:
        return {"error": "Could not fetch market data", "positions": []}

    ce_scores = []
    pe_scores = []
    positions = []

    # Portfolio-greek accumulators.  Sign convention from the trader's PoV:
    #   BUY  → +1 × row.greek (long exposure)
    #   SELL → -1 × row.greek (short exposure; theta becomes POSITIVE because
    #          the chain's theta is negative for both CE/PE — the seller gains)
    # Units are per-share scalars × qty × LOT_SIZE = position-level exposure.
    pf_delta = 0.0      # shares-equivalent (also = ₹/pt move in spot)
    pf_gamma = 0.0      # ₹/pt² (delta-change per 1pt move)
    pf_theta = 0.0      # ₹/day (positive = trader collects)
    pf_vega  = 0.0      # ₹ per 1% IV change

    # Futures contracts cache — populated lazily so we only hit NSE if the
    # user actually has open FUT positions.
    fut_by_expiry: dict | None = None

    for trade in open_trades:
        # ── Futures branch ─────────────────────────────────────────────
        # Futures aren't priced off the option chain; they have no strike,
        # IV, delta, or risk score. We MTM against the live futures LTP and
        # surface the row so the dashboard's "Open Positions" panel can
        # render its FUT card.
        if trade.instrument_type == "FUT" or trade.trade_type in ("FUT_BUY", "FUT_SELL"):
            if fut_by_expiry is None:
                try:
                    from nse_futures import fetch_nifty_futures, NIFTY_LOT as FUT_LOT
                    fut_data = fetch_nifty_futures()
                    fut_by_expiry = {c["expiry_date"]: c for c in fut_data.get("contracts", [])}
                except Exception:
                    fut_by_expiry = {}
                    FUT_LOT = LOT_SIZE
            contract = fut_by_expiry.get(trade.expiry_date) if fut_by_expiry else None
            ltp = float(contract["ltp"]) if contract and contract.get("ltp") else 0.0
            lot = trade.lot_size_override or LOT_SIZE
            qty_units = trade.quantity * lot
            is_long = trade.trade_type == "FUT_BUY"
            mtm = (ltp - trade.entry_premium) * qty_units if is_long else (trade.entry_premium - ltp) * qty_units
            # Futures: delta is exactly the unit count (sign by side), no
            # gamma/theta/vega exposure.  Contributes only to the Δ tile.
            pf_delta += qty_units if is_long else -qty_units
            positions.append({
                "trade_id": trade.id,
                "trade_type": trade.trade_type,
                "instrument_type": "FUT",
                "expiry_date": trade.expiry_date,
                "strike": 0,
                "quantity": trade.quantity,
                "current_ltp": round(ltp, 2) if ltp else None,
                "entry_price": trade.entry_premium,
                "unrealized_pnl": round(mtm, 2) if ltp else 0.0,
                "risk": None,
                "max_loss": None,
                "verified": (contract or {}).get("verified", True) if contract else False,
            })
            continue

        trade_expiry = trade.expiry_date
        chain_df = chain_by_expiry.get(trade_expiry)
        if chain_df is None:
            continue  # chain fetch failed for this expiry — skip rather than use wrong data

        dte = days_to_expiry(trade_expiry) if trade_expiry else 30
        opt_type = "CE" if "CE" in trade.trade_type else "PE"
        match = chain_df[(chain_df["Strike"] == trade.strike) & (chain_df["Type"] == opt_type)]

        if match.empty:
            continue

        row = match.iloc[0]
        # Margin/assignment risk only applies to SHORT options.  Longs have
        # defined risk = premium paid; no risk score, no hedge trigger.
        is_short = "SELL" in trade.trade_type
        risk = None
        if is_short:
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
                dte=dte,                  # ← per-trade DTE
                vix_chg_pct=vix_chg_pct,
            )

        # Per-share greeks pulled from the chain row.  Sign-flip for shorts
        # so the accumulator reflects the trader's exposure (a short option
        # gives the trader negative delta/gamma, positive theta — because
        # row.theta is negative for decaying options).
        sign = -1.0 if is_short else 1.0
        units = trade.quantity * LOT_SIZE
        row_gamma = float(row.get("gamma", 0) or 0)
        row_theta = float(row.get("theta", 0) or 0)
        row_vega  = float(row.get("vega",  0) or 0)
        pos_delta = float(row["delta"]) * sign * units
        pos_gamma = row_gamma * sign * units
        pos_theta = row_theta * sign * units      # ₹/day for the position
        pos_vega  = row_vega  * sign * units
        pf_delta += pos_delta
        pf_gamma += pos_gamma
        pf_theta += pos_theta
        pf_vega  += pos_vega

        position_data = {
            "trade_id": trade.id,
            "trade_type": trade.trade_type,
            "strike": trade.strike,
            "current_ltp": row["LTP"],
            "current_iv": row["IV"],
            "current_delta": row["delta"],
            "current_gamma": row_gamma,
            "current_theta": row_theta,
            "current_vega":  row_vega,
            "position_delta": round(pos_delta, 2),
            "position_theta": round(pos_theta, 2),
            "iv_change_pct": round(((row["IV"] - trade.entry_iv) / trade.entry_iv * 100) if trade.entry_iv > 0 else 0, 1),
            "unrealized_pnl": round(
                (trade.entry_premium - row["LTP"]) * LOT_SIZE * trade.quantity
                if is_short
                else (row["LTP"] - trade.entry_premium) * LOT_SIZE * trade.quantity,
                2
            ),
            "risk": risk,
            "max_loss": (None if is_short else round(trade.entry_premium * LOT_SIZE * trade.quantity, 2)),
        }
        positions.append(position_data)

        if is_short:
            if "CE" in trade.trade_type:
                ce_scores.append(risk["score"])
            else:
                pe_scores.append(risk["score"])

    ce_risk = round(max(ce_scores), 1) if ce_scores else None
    pe_risk = round(max(pe_scores), 1) if pe_scores else None

    # Portfolio-level greeks aggregation.  All values are at portfolio scale
    # already (per-share × qty × LOT_SIZE).  See accumulator init for sign
    # conventions and units.
    portfolio_greeks = {
        "delta": round(pf_delta, 2),       # shares-equivalent ⇄ ₹/pt move
        "gamma": round(pf_gamma, 4),       # ₹/pt² (delta change per pt)
        "theta": round(pf_theta, 2),       # ₹/day (positive = collecting)
        "vega":  round(pf_vega, 2),        # ₹ per 1% IV change
    }

    return {
        "ce_risk": {"score": ce_risk, "label": _risk_label(ce_risk)} if ce_risk is not None else None,
        "pe_risk": {"score": pe_risk, "label": _risk_label(pe_risk)} if pe_risk is not None else None,
        "positions": positions,
        "portfolio_greeks": portfolio_greeks,
        "spot": spot,
    }


def _risk_label(score):
    if score is None: return "N/A"
    if score < 35: return "Safe"
    if score < 60: return "Warning"
    if score < 80: return "Danger"
    return "Critical"


# ── Trade annotations (Batch B.1) ────────────────────────────────────────────

# Recognised mistake-tag slugs. The server stores whatever the client sends;
# this list is published via /api/trades/annotation-tags so the client can
# render a consistent chip palette without a separate config endpoint.
MISTAKE_TAGS = [
    {"slug": "wrong_direction",    "label": "Wrong direction"},
    {"slug": "held_too_long",      "label": "Held too long"},
    {"slug": "no_hedge",           "label": "No hedge"},
    {"slug": "oversized",          "label": "Oversized"},
    {"slug": "fomo_entry",         "label": "FOMO entry"},
    {"slug": "ignored_signal",     "label": "Ignored signal"},
    {"slug": "exited_too_early",   "label": "Exited too early"},
    {"slug": "no_plan",            "label": "No plan"},
    {"slug": "other",              "label": "Other"},
]


@router.get("/annotation-tags")
def list_annotation_tags(_: User = Depends(get_current_user)):
    """Mistake-tag taxonomy — single source of truth consumed by Journal +
    Positions close-modal so the chip set never drifts between surfaces."""
    return {"tags": MISTAKE_TAGS}


class AnnotationRequest(BaseModel):
    notes: str | None = None
    mistake_tags: list[str] | None = None


@router.get("/{trade_id}/annotation")
def get_annotation(
    trade_id: int,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Fetch annotation for a trade. Returns null if none yet — the journal
    UI uses that to decide between "Add note" and "Edit note" affordances."""
    trade = db.query(Trade).filter(Trade.id == trade_id, Trade.user_id == user.id).first()
    if not trade:
        raise HTTPException(404, "Trade not found")
    a = db.query(TradeAnnotation).filter(TradeAnnotation.trade_id == trade_id).first()
    if not a:
        return {"annotation": None}
    return {
        "annotation": {
            "trade_id":     a.trade_id,
            "notes":        a.notes,
            "mistake_tags": a.mistake_tags or [],
            "updated_at":   a.updated_at.isoformat() if a.updated_at else None,
        }
    }


@router.put("/{trade_id}/annotation")
def upsert_annotation(
    trade_id: int,
    req: AnnotationRequest,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Upsert annotation. Empty notes + empty tags deletes the row to keep
    the table tidy (no zombie blank entries from accidental opens)."""
    trade = db.query(Trade).filter(Trade.id == trade_id, Trade.user_id == user.id).first()
    if not trade:
        raise HTTPException(404, "Trade not found")
    notes = (req.notes or "").strip() or None
    tags  = [t for t in (req.mistake_tags or []) if isinstance(t, str)]
    a = db.query(TradeAnnotation).filter(TradeAnnotation.trade_id == trade_id).first()
    if not notes and not tags:
        if a:
            db.delete(a)
            db.commit()
        return {"annotation": None}
    if a:
        a.notes = notes
        a.mistake_tags = tags or None
    else:
        a = TradeAnnotation(
            trade_id=trade_id, user_id=user.id,
            notes=notes, mistake_tags=tags or None,
        )
        db.add(a)
    db.commit()
    db.refresh(a)
    return {
        "annotation": {
            "trade_id":     a.trade_id,
            "notes":        a.notes,
            "mistake_tags": a.mistake_tags or [],
            "updated_at":   a.updated_at.isoformat(),
        }
    }


# ── Margin & capital tracker ─────────────────────────────────────────────────
# Crude SPAN+exposure approximation for index instruments.  Exact intraday
# block depends on SPAN scenario shocks the exchange runs; treat this as a
# *planning* number (parity with the futures Margin/lot disclaimer).
#
# Coefficients:
#   short OPT  → 12% of strike × LOT_SIZE × qty   (SEBI ATM index-short range
#                                                  is ~12-15%; we pick the low end
#                                                  so we don't overstate "used")
#   long  OPT  → 0 margin block, but premium paid is "deployed capital"
#   FUT (any)  → reuse nse_futures.estimate_futures_margin (8.5% SPAN + 1.5%
#                exposure, same convention as the Futures page)
SHORT_OPT_MARGIN_PCT = 0.12


def _estimate_opt_margin(trade: Trade) -> float:
    """Per-trade margin block (₹) for an open option position."""
    if "BUY" in trade.trade_type:
        return 0.0
    return SHORT_OPT_MARGIN_PCT * float(trade.strike) * LOT_SIZE * trade.quantity


@router.get("/margin-summary")
def get_margin_summary(
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Capital deployment summary across all open positions.

    Returns total margin blocked, free capital (vs the user's configured
    `total_capital_inr`), and a breakdown by instrument so the dashboard
    can show "how much room do I have left".
    """
    from nse_futures import estimate_futures_margin

    open_trades = db.query(Trade).filter(
        Trade.user_id == user.id,
        Trade.status == TradeStatus.OPEN.value,
    ).all()

    capital = (user.settings or {}).get("total_capital_inr")
    capital = float(capital) if capital and capital > 0 else None

    by_instrument = {"OPT": 0.0, "FUT": 0.0}
    by_position = []
    long_premium_deployed = 0.0   # capital tied up in long-option premium

    for t in open_trades:
        if t.instrument_type == "FUT" or t.trade_type in ("FUT_BUY", "FUT_SELL"):
            lot = t.lot_size_override or LOT_SIZE
            mgn = estimate_futures_margin(t.entry_premium, t.quantity, lot)["total"]
            by_instrument["FUT"] += mgn
            by_position.append({"trade_id": t.id, "kind": "FUT", "margin": mgn})
        else:
            mgn = _estimate_opt_margin(t)
            by_instrument["OPT"] += mgn
            by_position.append({"trade_id": t.id, "kind": "OPT", "margin": mgn})
            if "BUY" in t.trade_type:
                long_premium_deployed += t.entry_premium * LOT_SIZE * t.quantity

    used = round(by_instrument["OPT"] + by_instrument["FUT"], 2)

    # Daily loss-stop status (Batch C.1) — piggybacked on this endpoint so
    # the dashboard's red-banner check doesn't need a separate poll.
    threshold = (user.settings or {}).get("max_daily_loss_inr")
    threshold = float(threshold) if threshold and threshold > 0 else None
    today_pnl = _today_realized_pnl(db, user.id)
    breached = bool(threshold) and today_pnl <= -threshold

    return {
        "capital":              capital,                                  # may be None
        "used":                 used,
        "long_premium":         round(long_premium_deployed, 2),
        "free":                 round(capital - used, 2) if capital else None,
        "deployed_pct":         round((used / capital) * 100, 1) if capital else None,
        "by_instrument":        {k: round(v, 2) for k, v in by_instrument.items()},
        "by_position":          by_position,
        "open_count":           len(open_trades),
        "method":               "SPAN+exposure approximation",
        # Daily loss-stop block
        "today_pnl":            round(today_pnl, 2),
        "loss_threshold":       threshold,
        "loss_breached":        breached,
    }


# ── Roll Suggestion ───────────────────────────────────────────────────────────

router_trades = APIRouter(prefix="/api/trades", tags=["trades-roll"])


@router_trades.get("/{trade_id}/roll-scenarios")
def get_roll_scenarios(
    trade_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Compute 3 roll/hedge scenarios for a trade, sorted best-first."""
    from market_data import (
        build_chain, greeks, days_to_expiry, time_to_expiry, find_25delta_iv,
        compute_max_pain,
    )
    from config import RISK_FREE_RATE
    from models import IVDailyRecord
    from sqlalchemy import desc as _desc

    trade = db.query(Trade).filter(Trade.id == trade_id, Trade.user_id == user.id).first()
    if not trade:
        raise HTTPException(status_code=404, detail="Trade not found")

    # Guard: roll is meaningless for closed / expired trades.
    if trade.status != TradeStatus.OPEN.value:
        raise HTTPException(status_code=400, detail=f"Trade is {trade.status}; only OPEN trades can be rolled.")
    # Guard: roll/hedge logic is built around premium-collected (short) positions
    # under unbounded risk. Long options have defined risk = premium paid, no
    # margin block, and no assignment risk — the rolls/futures-hedge produced
    # by this engine would be misleading. Close, exit, or convert to a spread
    # via "Add Hedge" instead.
    if "BUY" in trade.trade_type:
        raise HTTPException(
            status_code=400,
            detail="Long options have defined risk (max loss = premium paid). Rolling and futures-delta hedging apply to short positions only — close the trade or pair it into a spread instead.",
        )
    try:
        exp_date = datetime.strptime(trade.expiry_date, "%d-%b-%Y").date()
        if exp_date < datetime.now(_IST).date():
            raise HTTPException(status_code=400, detail=f"Expiry {trade.expiry_date} has already passed — close this position or wait for auto-expiry sweep.")
    except ValueError:
        pass

    # Load current chain for trade's expiry
    try:
        chain_df, expiries, spot = build_chain("NIFTY", trade.expiry_date)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Chain fetch failed: {e}")

    if chain_df is None or chain_df.empty or "Strike" not in chain_df.columns:
        raise HTTPException(status_code=503, detail="Option chain unavailable right now — try again during market hours.")

    dte = days_to_expiry(trade.expiry_date)
    T = time_to_expiry(trade.expiry_date)

    # Current ATM IV for greeks computation
    atm_strike = round(spot / 50) * 50
    ce_atm = chain_df[(chain_df["Strike"] == atm_strike) & (chain_df["Type"] == "CE")]
    pe_atm = chain_df[(chain_df["Strike"] == atm_strike) & (chain_df["Type"] == "PE")]
    ce_atm_iv = float(ce_atm["IV"].iloc[0]) / 100 if not ce_atm.empty else 0.15
    pe_atm_iv = float(pe_atm["IV"].iloc[0]) / 100 if not pe_atm.empty else 0.15
    atm_iv_frac = (ce_atm_iv + pe_atm_iv) / 2

    # Current LTP for trade's own strike
    opt_type = "CE" if "CE" in trade.trade_type else "PE"
    trade_row = chain_df[(chain_df["Strike"] == trade.strike) & (chain_df["Type"] == opt_type)]
    current_ltp = float(trade_row["LTP"].iloc[0]) if not trade_row.empty else 0.0

    # Position delta
    trade_iv = float(trade_row["IV"].iloc[0]) / 100 if not trade_row.empty else atm_iv_frac
    trade_greeks = greeks(spot, trade.strike, T or 0.001, RISK_FREE_RATE, trade_iv, opt_type)
    pos_delta = trade_greeks["delta"] * trade.quantity * LOT_SIZE
    # For shorts, flip sign (sold option gains when delta moves against buyer)
    if "SELL" in trade.trade_type:
        pos_delta = -pos_delta

    # IV rank for Scenario C scoring
    iv_rank = 50  # default if no history
    try:
        latest_iv = db.query(IVDailyRecord).order_by(_desc(IVDailyRecord.date)).first()
        all_iv = db.query(IVDailyRecord).order_by(_desc(IVDailyRecord.date)).limit(252).all()
        if latest_iv and all_iv:
            vals = [r.atm_iv for r in all_iv]
            cur = latest_iv.atm_iv
            iv_min, iv_max = min(vals), max(vals)
            iv_rank = (cur - iv_min) / (iv_max - iv_min) * 100 if iv_max > iv_min else 50
    except Exception:
        pass

    scenarios = []

    # ── Scenario A: OTM Roll (same expiry) ────────────────────────────────
    try:
        roll_dist = 200  # pts further OTM
        new_strike_a = trade.strike + roll_dist if opt_type == "CE" else trade.strike - roll_dist
        # Round to nearest 50
        new_strike_a = round(new_strike_a / 50) * 50
        new_row_a = chain_df[(chain_df["Strike"] == new_strike_a) & (chain_df["Type"] == opt_type)]
        new_ltp_a = float(new_row_a["LTP"].iloc[0]) if not new_row_a.empty else 0.0
        new_iv_a = float(new_row_a["IV"].iloc[0]) / 100 if not new_row_a.empty else atm_iv_frac
        debit_credit_a = current_ltp - new_ltp_a  # positive = net credit received
        new_greeks_a = greeks(spot, new_strike_a, T or 0.001, RISK_FREE_RATE, new_iv_a, opt_type)
        new_delta_a = new_greeks_a["delta"]
        dist_from_spot = abs(new_strike_a - spot)

        score_a = 50
        if dte < 7: score_a += 30
        if debit_credit_a > 0: score_a += 20
        if dist_from_spot > 300: score_a += 15
        score_a = min(score_a, 100)

        scenarios.append({
            "scenario_id": "otm_roll",
            "label": f"OTM Roll (+{roll_dist}pts, same expiry)",
            "new_strike": new_strike_a,
            "new_expiry": trade.expiry_date,
            "new_ltp": round(new_ltp_a, 2),
            "roll_debit_credit": round(debit_credit_a * trade.quantity * LOT_SIZE, 2),
            "new_delta": round(new_delta_a, 3),
            "new_dte": dte,
            "rationale": (
                f"Roll to {new_strike_a} ({'+' if opt_type=='CE' else '-'}{roll_dist}pts OTM). "
                f"{'Net credit of ₹' + str(round(debit_credit_a * trade.quantity * LOT_SIZE, 2)) if debit_credit_a > 0 else 'Net debit of ₹' + str(abs(round(debit_credit_a * trade.quantity * LOT_SIZE, 2)))}. "
                f"Delta improves to {round(abs(new_delta_a), 3)}."
            ),
            "recommendation_score": score_a,
        })
    except Exception:
        pass

    # ── Scenario B: Calendar Roll (next expiry, same strike) ──────────────
    try:
        curr_idx = expiries.index(trade.expiry_date) if trade.expiry_date in expiries else 0
        next_expiry = expiries[curr_idx + 1] if curr_idx + 1 < len(expiries) else None
        if next_expiry:
            chain_df_b, _, _ = build_chain("NIFTY", next_expiry)
            new_row_b = chain_df_b[(chain_df_b["Strike"] == trade.strike) & (chain_df_b["Type"] == opt_type)]
            new_ltp_b = float(new_row_b["LTP"].iloc[0]) if not new_row_b.empty else 0.0
            new_iv_b = float(new_row_b["IV"].iloc[0]) / 100 if not new_row_b.empty else atm_iv_frac
            debit_credit_b = current_ltp - new_ltp_b
            new_dte_b = days_to_expiry(next_expiry)
            T_b = time_to_expiry(next_expiry)
            new_greeks_b = greeks(spot, trade.strike, T_b or 0.001, RISK_FREE_RATE, new_iv_b, opt_type)
            new_theta_b = new_greeks_b["theta"] * trade.quantity * LOT_SIZE

            score_b = 50
            if new_ltp_b > current_ltp: score_b += 25  # good roll credit
            if dte < 3: score_b += 20
            if new_iv_b < (float(trade_row["IV"].iloc[0]) / 100 if not trade_row.empty else atm_iv_frac): score_b += 10
            score_b = min(score_b, 100)

            scenarios.append({
                "scenario_id": "calendar_roll",
                "label": f"Calendar Roll → {next_expiry} (same strike {trade.strike})",
                "new_strike": trade.strike,
                "new_expiry": next_expiry,
                "new_ltp": round(new_ltp_b, 2),
                "roll_debit_credit": round(debit_credit_b * trade.quantity * LOT_SIZE, 2),
                "new_delta": round(new_greeks_b["delta"], 3),
                "new_dte": new_dte_b,
                "new_theta": round(abs(new_theta_b), 2),
                "rationale": (
                    f"Roll same strike to {next_expiry} ({new_dte_b}d). "
                    f"{'Higher LTP (₹' + str(round(new_ltp_b, 2)) + ') offers roll credit.' if new_ltp_b > current_ltp else 'Lower LTP — pays debit to roll.'} "
                    f"Daily theta: ₹{abs(round(new_theta_b, 2))}."
                ),
                "recommendation_score": score_b,
            })
    except Exception:
        pass

    # ── Scenario C: Futures Delta Hedge ───────────────────────────────────
    try:
        net_delta = pos_delta
        futures_lots = round(abs(net_delta) / LOT_SIZE)
        if futures_lots == 0: futures_lots = 1
        futures_action = "SELL" if net_delta > 0 else "BUY"
        hedge_margin = round(spot * LOT_SIZE * futures_lots * 0.10, 2)

        score_c = 50
        if abs(net_delta) > 0.30 * trade.quantity * LOT_SIZE: score_c += 30
        if dte > 7: score_c += 20
        if iv_rank < 40: score_c += 15
        score_c = min(score_c, 100)

        scenarios.append({
            "scenario_id": "futures_hedge",
            "label": f"Futures Delta Hedge ({futures_action} {futures_lots} lot{'s' if futures_lots != 1 else ''})",
            "futures_lots": futures_lots,
            "futures_action": futures_action,
            "hedge_margin": hedge_margin,
            "new_delta": None,
            "new_dte": dte,
            "roll_debit_credit": 0,
            "new_strike": None,
            "new_expiry": None,
            "new_ltp": 0,
            "rationale": (
                f"Current position delta: {round(net_delta, 2)}. "
                f"{futures_action} {futures_lots} futures lot(s) to neutralize delta. "
                f"Est. margin: ₹{int(hedge_margin):,}. "
                f"Option position stays intact — only delta risk is hedged. Adds basis risk."
            ),
            "recommendation_score": score_c,
        })
    except Exception:
        pass

    # Sort best-first
    scenarios.sort(key=lambda s: s["recommendation_score"], reverse=True)

    return {"trade_id": trade_id, "spot": round(spot, 2), "scenarios": scenarios}


def Utils_format(n):
    """Simple comma formatter for use in rationale strings."""
    return f"{n:,.0f}"


class RollRequest(BaseModel):
    scenario_id: str
    new_strike: int = 0
    new_expiry: str = ""
    new_ltp: float = 0.0
    futures_lots: int = 0
    futures_action: str = ""


@router_trades.post("/{trade_id}/roll")
def execute_roll(
    trade_id: int,
    req: RollRequest,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Execute a roll or futures hedge for a trade."""
    from market_data import build_chain, days_to_expiry

    trade = db.query(Trade).filter(Trade.id == trade_id, Trade.user_id == user.id).first()
    if not trade:
        raise HTTPException(status_code=404, detail="Trade not found")

    now = datetime.now(_IST).replace(tzinfo=None)

    if req.scenario_id == "futures_hedge":
        # Book the futures hedge against the LIVE NIFTY-FUT contract LTP
        # (not the index spot) and persist with instrument_type='FUT'. The
        # nse_futures module guards the quote with basis/lot/inversion
        # sanity checks and raises 503 if the LTP looks suspect — we surface
        # that to the user instead of silently booking a wrong price.
        from nse_futures import get_contract_by_expiry, NIFTY_LOT
        fut_type = f"FUT_{req.futures_action}"  # FUT_BUY or FUT_SELL
        contract = get_contract_by_expiry(trade.expiry_date)
        if contract is None:
            # No futures for the option's expiry — fall back to the nearest
            # active contract (NSE only lists current/next/far month for FUT,
            # while options have weeklies).
            from nse_futures import nearest_active_future
            contract = nearest_active_future()
            if contract is None:
                raise HTTPException(status_code=503, detail="NIFTY futures quote unavailable; cannot book hedge.")
        if not contract.get("verified", True):
            raise HTTPException(
                status_code=503,
                detail=f"Live NIFTY-FUT LTP for {contract['expiry_date']} failed sanity check (basis {contract.get('basis_pct')}%); refusing to book.",
            )
        new_trade = Trade(
            user_id=user.id,
            trade_type=fut_type,
            instrument_type="FUT",
            lot_size_override=contract["lot_size"] or NIFTY_LOT,
            strike=0,
            expiry_date=contract["expiry_date"],
            entry_premium=contract["ltp"],
            entry_iv=0.0,
            entry_delta=1.0 if req.futures_action == "BUY" else -1.0,
            quantity=req.futures_lots or 1,
            status=TradeStatus.OPEN.value,
            parent_trade_id=trade.id,
            strategy_id=trade.strategy_id,
            entry_date=now,
        )
        db.add(new_trade)
        db.commit()
        db.refresh(new_trade)
        return {
            "status": "ok",
            "action": "futures_hedge",
            "new_trade_id": new_trade.id,
            "futures_expiry": contract["expiry_date"],
            "entry_price": contract["ltp"],
            "lot_size": contract["lot_size"],
        }

    else:
        # Scenarios A & B: close original trade, open new one
        # Get current LTP for original strike (exit price)
        opt_type = "CE" if "CE" in trade.trade_type else "PE"
        try:
            chain_df, _, spot = build_chain("NIFTY", trade.expiry_date)
            orig_row = chain_df[(chain_df["Strike"] == trade.strike) & (chain_df["Type"] == opt_type)]
            exit_price = float(orig_row["LTP"].iloc[0]) if not orig_row.empty else req.new_ltp
        except Exception:
            exit_price = req.new_ltp

        # Realized P&L: for SELL trades, profit = entry - exit (sold high, buy back low)
        if "SELL" in trade.trade_type:
            realized_pnl = round((trade.entry_premium - exit_price) * trade.quantity * LOT_SIZE, 2)
        else:
            realized_pnl = round((exit_price - trade.entry_premium) * trade.quantity * LOT_SIZE, 2)

        trade.exit_price = exit_price
        trade.exit_date = now
        trade.status = TradeStatus.CLOSED.value
        trade.realized_pnl = realized_pnl

        # Create new trade at rolled position
        new_trade = Trade(
            user_id=user.id,
            trade_type=trade.trade_type,
            strike=req.new_strike,
            expiry_date=req.new_expiry or trade.expiry_date,
            entry_premium=req.new_ltp,
            entry_iv=0.0,
            entry_delta=0.0,
            quantity=trade.quantity,
            status=TradeStatus.OPEN.value,
            parent_trade_id=trade.id,
            strategy_id=trade.strategy_id,
            entry_date=now,
        )
        db.add(new_trade)
        db.commit()
        db.refresh(new_trade)
        return {
            "status": "ok",
            "action": req.scenario_id,
            "closed_trade_id": trade.id,
            "realized_pnl": realized_pnl,
            "new_trade_id": new_trade.id,
        }
