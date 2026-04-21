"""Analytics API routes — performance, strike patterns, insights."""

from datetime import datetime, timedelta
from collections import defaultdict
from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session
from database import get_db
from models import Trade, TradeStatus, User, MarketSnapshot, DailySignalSnapshot
from auth import get_current_user
from config import LOT_SIZE

router = APIRouter(prefix="/api/analytics", tags=["analytics"])


@router.get("/performance")
def performance_overview(
    months: int = Query(0, description="Filter last N months, 0=all"),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Performance overview — P&L curve, expiry bars, monthly heatmap.

    Only CLOSED trades with a defined realized_pnl are included.
    OPEN trades (even those with a past expiry) are never included — their
    realized_pnl is undefined, so counting them would distort all metrics.
    We do run the expiry-cleanup sweep here so that visiting Analytics also
    transitions any lingering OPEN-but-expired trades to EXPIRED status.
    """
    # Sweep expired trades so the DB stays consistent regardless of which page
    # the user visits first.  Analytics queries are unaffected (filter = CLOSED),
    # but this keeps the positions page accurate on next visit.
    from routes_trades import _expire_past_trades
    _expire_past_trades(db, user.id)

    query = db.query(Trade).filter(
        Trade.user_id == user.id,
        Trade.status == TradeStatus.CLOSED.value,
    )
    if months > 0:
        cutoff = datetime.utcnow() - timedelta(days=months * 30)
        query = query.filter(Trade.exit_date >= cutoff)

    trades = query.order_by(Trade.exit_date.asc()).all()

    if not trades:
        return {
            "summary": {"total_pnl": 0, "win_rate": 0, "avg_per_expiry": 0, "max_drawdown": 0, "trade_count": 0},
            "cumulative_pnl": [],
            "expiry_pnl": [],
            "monthly_pnl": [],
        }

    # Summary metrics
    pnls = [t.realized_pnl or 0 for t in trades]
    wins = sum(1 for p in pnls if p > 0)

    # P&L by expiry
    expiry_map = defaultdict(float)
    for t in trades:
        expiry_map[t.expiry_date] += t.realized_pnl or 0

    avg_per_expiry = sum(expiry_map.values()) / len(expiry_map) if expiry_map else 0

    # Max drawdown from cumulative P&L
    cumulative = []
    running = 0
    peak = 0
    max_dd = 0
    for t in trades:
        running += t.realized_pnl or 0
        peak = max(peak, running)
        dd = peak - running
        max_dd = max(max_dd, dd)
        cumulative.append({
            "date": t.exit_date.strftime("%Y-%m-%d") if t.exit_date else None,
            "pnl": round(running, 2),
            "trade_type": t.trade_type,
            "strike": t.strike,
        })

    # Expiry P&L bars
    expiry_pnl = [
        {"expiry": exp, "pnl": round(pnl, 2)}
        for exp, pnl in sorted(expiry_map.items())
    ]

    # Monthly P&L heatmap
    monthly_map = defaultdict(float)
    for t in trades:
        if t.exit_date:
            key = t.exit_date.strftime("%Y-%m")
            monthly_map[key] += t.realized_pnl or 0

    monthly_pnl = [
        {"month": m, "pnl": round(p, 2)}
        for m, p in sorted(monthly_map.items())
    ]

    return {
        "summary": {
            "total_pnl": round(sum(pnls), 2),
            "win_rate": round(wins / len(pnls) * 100, 1) if pnls else 0,
            "avg_per_expiry": round(avg_per_expiry, 2),
            "max_drawdown": round(max_dd, 2),
            "trade_count": len(trades),
        },
        "cumulative_pnl": cumulative,
        "expiry_pnl": expiry_pnl,
        "monthly_pnl": monthly_pnl,
    }


@router.get("/strike-patterns")
def strike_patterns(
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Strike distance analysis and VIX regime breakdown."""
    trades = db.query(Trade).filter(
        Trade.user_id == user.id,
        Trade.status == TradeStatus.CLOSED.value,
    ).order_by(Trade.exit_date.asc()).all()

    if not trades:
        return {"ce_distances": [], "pe_distances": [], "vix_regime": [], "insights": []}

    # We need snapshot data to know spot at entry time for distance calc.
    # Use entry_delta as a proxy for OTM-ness, but also compute strike distance
    # from closest snapshot at trade entry time.
    ce_data = []
    pe_data = []

    for t in trades:
        pnl = t.realized_pnl or 0
        # Get spot near entry time
        snap = None
        if t.entry_date:
            snap = db.query(MarketSnapshot).filter(
                MarketSnapshot.timestamp <= t.entry_date
            ).order_by(MarketSnapshot.timestamp.desc()).first()

        spot_at_entry = snap.nifty_spot if snap else 0
        distance = abs(t.strike - spot_at_entry) if spot_at_entry > 0 else 0

        entry = {
            "strike": t.strike,
            "distance": round(distance),
            "distance_pct": round(distance / spot_at_entry * 100, 1) if spot_at_entry else 0,
            "pnl": round(pnl, 2),
            "win": pnl > 0,
            "entry_iv": t.entry_iv,
            "entry_delta": abs(t.entry_delta) if t.entry_delta else 0,
            "quantity": t.quantity,
            "expiry": t.expiry_date,
        }

        if "CE" in t.trade_type:
            ce_data.append(entry)
        else:
            pe_data.append(entry)

    # VIX regime analysis (from entry IV as proxy — or entry snapshot VIX)
    vix_buckets = {"low": {"label": "VIX < 15", "wins": 0, "total": 0, "pnl": 0},
                   "mid": {"label": "15 <= VIX < 25", "wins": 0, "total": 0, "pnl": 0},
                   "high": {"label": "VIX >= 25", "wins": 0, "total": 0, "pnl": 0}}

    for t in trades:
        snap = None
        if t.entry_date:
            snap = db.query(MarketSnapshot).filter(
                MarketSnapshot.timestamp <= t.entry_date
            ).order_by(MarketSnapshot.timestamp.desc()).first()
        vix_at_entry = snap.india_vix if snap else 0

        pnl = t.realized_pnl or 0
        if vix_at_entry < 15:
            bucket = vix_buckets["low"]
        elif vix_at_entry < 25:
            bucket = vix_buckets["mid"]
        else:
            bucket = vix_buckets["high"]

        bucket["total"] += 1
        bucket["pnl"] += pnl
        if pnl > 0:
            bucket["wins"] += 1

    vix_regime = []
    for key in ["low", "mid", "high"]:
        b = vix_buckets[key]
        vix_regime.append({
            "label": b["label"],
            "total": b["total"],
            "wins": b["wins"],
            "win_rate": round(b["wins"] / b["total"] * 100, 1) if b["total"] else 0,
            "pnl": round(b["pnl"], 2),
        })

    # Auto-generated insights
    insights = _generate_insights(ce_data, pe_data, vix_regime)

    return {
        "ce_distances": ce_data,
        "pe_distances": pe_data,
        "vix_regime": vix_regime,
        "insights": insights,
    }


def _generate_insights(ce_data, pe_data, vix_regime):
    """Generate auto-insights from trade patterns.

    Each insight aims to be *actionable* — either a threshold to respect, a regime
    to avoid, or a confirmed edge to lean on. Small samples (<5 trades) get a
    caveat so the user doesn't over-fit to noise.
    """
    insights = []

    def _caveat(n):
        return " <span style='opacity:0.7'>(small sample — treat as tentative)</span>" if n < 5 else ""

    # Per-side win/loss distance comparison — the most actionable signal.
    for label, data in [("CE", ce_data), ("PE", pe_data)]:
        if not data:
            continue
        wins = [d for d in data if d["win"]]
        losses = [d for d in data if not d["win"]]
        wr = len(wins) / len(data) * 100
        insights.append(
            f"{label} sells: {wr:.0f}% win rate over {len(data)} trade{'s' if len(data) != 1 else ''}." + _caveat(len(data))
        )
        if wins and losses:
            avg_win = sum(d["distance"] for d in wins) / len(wins)
            avg_loss = sum(d["distance"] for d in losses) / len(losses)
            gap = avg_win - avg_loss
            if gap >= 100:
                insights.append(
                    f"{label} winners sat ~{gap:.0f}pts further OTM than losers "
                    f"(~{avg_win:.0f} vs ~{avg_loss:.0f}). Treat ~{avg_win:.0f}pts as your safety floor."
                    + _caveat(len(data))
                )
            elif gap <= -100:
                insights.append(
                    f"{label}: closer-to-spot strikes actually won more often here — "
                    f"unusual. Re-check entry timing rather than just pushing strikes further OTM."
                    + _caveat(len(data))
                )

    # CE vs PE comparative bias — tells the user which side is bleeding.
    if ce_data and pe_data and len(ce_data) >= 3 and len(pe_data) >= 3:
        ce_wr = sum(1 for d in ce_data if d["win"]) / len(ce_data) * 100
        pe_wr = sum(1 for d in pe_data if d["win"]) / len(pe_data) * 100
        diff = ce_wr - pe_wr
        if abs(diff) >= 20:
            weak, strong = ("PE", "CE") if diff > 0 else ("CE", "PE")
            insights.append(
                f"{strong} side is outperforming {weak} by {abs(diff):.0f}pp in win rate — "
                f"consider sizing {weak} smaller or widening {weak} strikes until it catches up."
            )

    # VIX regime insight — flag both weak and strong buckets.
    for regime in vix_regime:
        if regime["total"] >= 3:
            if regime["win_rate"] < 50:
                insights.append(
                    f"Win rate drops to {regime['win_rate']}% when {regime['label']} "
                    f"({regime['total']} trades) — reduce size or skip until vol normalises."
                    + _caveat(regime["total"])
                )
            elif regime["win_rate"] >= 80:
                insights.append(
                    f"Strong {regime['win_rate']}% win rate when {regime['label']} "
                    f"({regime['total']} trades) — this is your home turf."
                    + _caveat(regime["total"])
                )

    # Far-OTM edge confirmation.
    for label, data in [("CE", ce_data), ("PE", pe_data)]:
        far_otm = [d for d in data if d["distance"] > 800]
        if len(far_otm) >= 2:
            far_wins = sum(1 for d in far_otm if d["win"])
            far_wr = far_wins / len(far_otm) * 100
            if far_wr >= 80:
                insights.append(
                    f"Far-OTM {label} (>800pts) held up in {far_wr:.0f}% of cases "
                    f"({len(far_otm)} trades) — keep defaulting here when unsure."
                    + _caveat(len(far_otm))
                )

    # Tail-risk check — a single loss that dwarfs the average win erases the edge.
    all_data = ce_data + pe_data
    wins = [d["pnl"] for d in all_data if d["win"]]
    losses = [-d["pnl"] for d in all_data if not d["win"] and d["pnl"] < 0]
    if wins and losses:
        avg_win = sum(wins) / len(wins)
        worst_loss = max(losses)
        if worst_loss >= 3 * avg_win and worst_loss > 0:
            insights.append(
                f"Your worst loss was ~{worst_loss/avg_win:.1f}× the average win. "
                f"One tail event wipes out multiple winners — review stop discipline on losers."
            )

    return insights if insights else ["Not enough trade history for pattern insights yet."]


@router.get("/historical-signals")
def historical_signals(
    days: int = Query(90, le=365),
    db: Session = Depends(get_db),
    _=Depends(get_current_user),
):
    """Get daily signal snapshots for historical accuracy tracking."""
    cutoff = datetime.utcnow().date() - timedelta(days=days)
    rows = (
        db.query(DailySignalSnapshot)
        .filter(DailySignalSnapshot.date >= cutoff)
        .order_by(DailySignalSnapshot.date.asc())
        .all()
    )

    return {
        "signals": [
            {
                "date": str(r.date),
                "nifty_close": r.nifty_close,
                "india_vix": r.india_vix,
                "rr_value": r.rr_value,
                "rr_verdict": r.rr_verdict,
                "tendency": r.tendency,
                "direction": r.directional_direction,
                "score": r.directional_score,
                "factors_agree": r.directional_factors_agree,
                "max_pain": r.max_pain,
                "next_day_close": r.next_day_close,
                "next_day_change": r.next_day_change,
                "next_day_change_pct": r.next_day_change_pct,
                "signal_correct": r.signal_correct,
            }
            for r in rows
        ],
    }


@router.get("/signal-accuracy")
def signal_accuracy(
    days: int = Query(90, le=365),
    db: Session = Depends(get_db),
    _=Depends(get_current_user),
):
    """Aggregated signal accuracy stats."""
    cutoff = datetime.utcnow().date() - timedelta(days=days)
    rows = (
        db.query(DailySignalSnapshot)
        .filter(
            DailySignalSnapshot.date >= cutoff,
            DailySignalSnapshot.signal_correct.isnot(None),
        )
        .all()
    )

    total = len(rows)
    if total == 0:
        return {
            "total_signals": 0,
            "bullish": {"total": 0, "correct": 0, "accuracy": 0},
            "bearish": {"total": 0, "correct": 0, "accuracy": 0},
            "overall_accuracy": 0,
            "tendency_accuracy": 0,
        }

    correct = sum(1 for r in rows if r.signal_correct)
    bullish = [r for r in rows if r.directional_direction == "BULLISH"]
    bearish = [r for r in rows if r.directional_direction == "BEARISH"]
    bull_correct = sum(1 for r in bullish if r.signal_correct)
    bear_correct = sum(1 for r in bearish if r.signal_correct)

    # Tendency accuracy: when tendency agreed with direction AND was correct
    tend_agree = [
        r for r in rows
        if (r.tendency == "Positive" and r.directional_direction == "BULLISH")
        or (r.tendency == "Negative" and r.directional_direction == "BEARISH")
    ]
    tend_correct = sum(1 for r in tend_agree if r.signal_correct)

    return {
        "total_signals": total,
        "bullish": {
            "total": len(bullish),
            "correct": bull_correct,
            "accuracy": round(bull_correct / len(bullish) * 100, 1) if bullish else 0,
        },
        "bearish": {
            "total": len(bearish),
            "correct": bear_correct,
            "accuracy": round(bear_correct / len(bearish) * 100, 1) if bearish else 0,
        },
        "overall_accuracy": round(correct / total * 100, 1),
        "tendency_agreement": {
            "total": len(tend_agree),
            "correct": tend_correct,
            "accuracy": round(tend_correct / len(tend_agree) * 100, 1) if tend_agree else 0,
        },
    }
