"""Analytics API routes — performance, strike patterns, insights."""

from datetime import datetime, timedelta
from collections import defaultdict
from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session
from database import get_db
from models import Trade, TradeAnnotation, TradeStatus, User, MarketSnapshot, DailySignalSnapshot, AlertLog, AlertRule
from auth import get_current_user
from config import LOT_SIZE

router = APIRouter(prefix="/api/analytics", tags=["analytics"])


@router.get("/performance")
def performance_overview(
    months: int = Query(0, description="Filter last N months, 0=all"),
    symbol: str = Query(None, description="Scope to one underlying; None/empty = all"),
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

    # All closed trades in window (drives the global "P&L by Underlying" heatmap,
    # which must stay symbol-agnostic so it can offer every symbol to filter by).
    all_trades = query.order_by(Trade.exit_date.asc()).all()

    # symbol_pnl is computed from ALL trades (global attribution), regardless of
    # the active symbol filter.
    symbol_agg = defaultdict(lambda: {"pnl": 0.0, "trades": 0, "wins": 0})
    for t in all_trades:
        sym = t.symbol or "NIFTY"
        p = t.realized_pnl or 0
        a = symbol_agg[sym]
        a["pnl"] += p
        a["trades"] += 1
        if p > 0:
            a["wins"] += 1
    symbol_pnl = [
        {
            "symbol": sym,
            "pnl": round(a["pnl"], 2),
            "trades": a["trades"],
            "win_rate": round(a["wins"] / a["trades"] * 100, 1) if a["trades"] else 0,
        }
        for sym, a in symbol_agg.items()
        if round(a["pnl"], 2) != 0   # skip underlyings with a flat ₹0 net P&L
    ]
    symbol_pnl.sort(key=lambda x: x["pnl"], reverse=True)

    # The rest of the page (summary, curves, heatmaps) respects the symbol filter.
    sym_filter = (symbol or "").upper().strip()
    trades = ([t for t in all_trades if (t.symbol or "NIFTY") == sym_filter]
              if sym_filter else all_trades)

    if not trades:
        return {
            "summary": {"total_pnl": 0, "win_rate": 0, "avg_per_expiry": 0, "max_drawdown": 0, "trade_count": 0},
            "cumulative_pnl": [],
            "expiry_pnl": [],
            "monthly_pnl": [],
            "side_pnl": [],
            "symbol_pnl": symbol_pnl,
            "active_symbol": sym_filter or None,
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

    # P&L by side (CE vs PE) per underlying — respects the active symbol filter.
    side_agg = defaultdict(lambda: {"ce": 0.0, "pe": 0.0, "ce_n": 0, "pe_n": 0})
    for t in trades:
        sym = t.symbol or "NIFTY"
        p = t.realized_pnl or 0
        s = side_agg[sym]
        if "CE" in t.trade_type:
            s["ce"] += p; s["ce_n"] += 1
        elif "PE" in t.trade_type:
            s["pe"] += p; s["pe_n"] += 1
    side_pnl = [
        {"symbol": sym, "ce_pnl": round(v["ce"], 2), "pe_pnl": round(v["pe"], 2),
         "ce_trades": v["ce_n"], "pe_trades": v["pe_n"]}
        for sym, v in side_agg.items()
    ]
    side_pnl.sort(key=lambda x: (x["ce_pnl"] + x["pe_pnl"]), reverse=True)

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
        "symbol_pnl": symbol_pnl,       # global attribution (all trades)
        "side_pnl": side_pnl,           # CE vs PE per underlying (filtered)
        "active_symbol": sym_filter or None,
    }


@router.get("/strike-patterns")
def strike_patterns(
    symbol: str = Query(None, description="Scope to one underlying; None/empty = all"),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Strike distance analysis and VIX regime breakdown."""
    q = db.query(Trade).filter(
        Trade.user_id == user.id,
        Trade.status == TradeStatus.CLOSED.value,
    )
    sym_filter = (symbol or "").upper().strip()
    if sym_filter:
        q = q.filter(Trade.symbol == sym_filter)
    trades = q.order_by(Trade.exit_date.asc()).all()

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
        # Skip trades with no usable spot anchor — without it the "distance"
        # field collapses to abs(strike - 0) ≈ 24000, which is the absolute
        # strike value, not OTM distance. Including those points blew the
        # axis scale on the histograms and scatter chart.
        if spot_at_entry <= 0:
            continue
        distance = abs(t.strike - spot_at_entry)

        entry = {
            "strike": t.strike,
            "distance": round(distance),
            "distance_pct": round(distance / spot_at_entry * 100, 1),
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


@router.get("/mistake-breakdown")
def mistake_breakdown(
    months: int = Query(0, description="Filter last N months, 0=all"),
    symbol: str = Query(None, description="Scope to one underlying; None/empty = all"),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Aggregate per-mistake-tag stats from TradeAnnotation × Trade.

    Returns rows ordered by total_loss DESC so the trader sees their costliest
    pattern first.  Only CLOSED trades are considered (annotations on still-open
    trades are technically allowed but they have no realized_pnl yet — including
    them would skew the "what cost me" view).
    """
    q = db.query(TradeAnnotation, Trade).join(
        Trade, Trade.id == TradeAnnotation.trade_id
    ).filter(
        TradeAnnotation.user_id == user.id,
        Trade.status == TradeStatus.CLOSED.value,
    )
    if months > 0:
        cutoff = datetime.utcnow() - timedelta(days=months * 30)
        q = q.filter(Trade.exit_date >= cutoff)
    sym_filter = (symbol or "").upper().strip()
    if sym_filter:
        q = q.filter(Trade.symbol == sym_filter)

    rows = q.all()

    # Aggregate per tag.  A trade with multiple tags contributes its full P&L
    # to each one — that's the correct attribution model: if a trade lost
    # because of "oversized AND no_hedge", *both* patterns share the blame.
    by_tag = defaultdict(lambda: {"count": 0, "total_pnl": 0.0, "wins": 0, "losses": 0})
    total_tagged_trades = 0
    for ann, trade in rows:
        tags = ann.mistake_tags or []
        if not tags:
            continue
        total_tagged_trades += 1
        pnl = float(trade.realized_pnl or 0)
        for slug in tags:
            row = by_tag[slug]
            row["count"]     += 1
            row["total_pnl"] += pnl
            if pnl >= 0: row["wins"]   += 1
            else:        row["losses"] += 1

    breakdown = [
        {
            "tag":         slug,
            "count":       v["count"],
            "wins":        v["wins"],
            "losses":      v["losses"],
            "total_pnl":   round(v["total_pnl"], 2),
            "avg_pnl":     round(v["total_pnl"] / v["count"], 2) if v["count"] else 0.0,
            # Aggregate loss only — useful for ranking which mistakes hurt
            "total_loss":  round(-v["total_pnl"], 2) if v["total_pnl"] < 0 else 0.0,
        }
        for slug, v in by_tag.items()
    ]
    # Costliest first; ties broken by frequency
    breakdown.sort(key=lambda r: (r["total_loss"], r["count"]), reverse=True)

    return {
        "breakdown":            breakdown,
        "total_tagged_trades":  total_tagged_trades,
        "months":               months,
    }


@router.get("/eod-summary")
def eod_summary(
    date: str | None = Query(None, description="YYYY-MM-DD (default = today IST)"),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """End-of-day wrap for the dashboard.

    Compute-on-demand (no scheduler dependency) so the card works on any
    historical date and survives a missed batch run.  Joins three sources:
      • Trade        — realised P&L of trades closed that day, top G/L.
      • AlertLog     — count of alerts the user fired that day.
      • DailySignalSnapshot — was the directional signal correct (backfilled
                              the next day; today's row may have null
                              `signal_correct` until after midnight).
    Plus a reference to today's NIFTY close from MarketSnapshot.
    """
    from datetime import timezone, timedelta as _td
    IST = timezone(_td(hours=5, minutes=30))
    target = datetime.strptime(date, "%Y-%m-%d").date() if date else datetime.now(IST).date()

    # Closed trades whose exit_date falls on the target IST date.
    rows = db.query(Trade).filter(
        Trade.user_id == user.id,
        Trade.status == TradeStatus.CLOSED.value,
    ).all()
    closed_today = []
    for t in rows:
        if not t.exit_date:
            continue
        ex_ist = (t.exit_date + _td(hours=5, minutes=30)).date()
        if ex_ist == target:
            closed_today.append(t)
    realized = sum((t.realized_pnl or 0) for t in closed_today)

    top_gainer = max(closed_today, key=lambda t: (t.realized_pnl or 0), default=None)
    top_loser  = min(closed_today, key=lambda t: (t.realized_pnl or 0), default=None)
    # Don't surface "top gainer" if best is a loss, "top loser" if worst is a profit.
    if top_gainer and (top_gainer.realized_pnl or 0) <= 0:
        top_gainer = None
    if top_loser and (top_loser.realized_pnl or 0) >= 0:
        top_loser = None

    # Current open positions count
    open_count = db.query(Trade).filter(
        Trade.user_id == user.id,
        Trade.status == TradeStatus.OPEN.value,
    ).count()

    # Alerts the user fired on the target date.  AlertRule.user_id scopes
    # the count; AlertLog.triggered_at is UTC, shift to IST for date check.
    alert_logs = db.query(AlertLog).join(AlertRule).filter(
        AlertRule.user_id == user.id,
    ).all()
    alerts_fired = 0
    for al in alert_logs:
        if not al.triggered_at:
            continue
        ts_ist = (al.triggered_at + _td(hours=5, minutes=30)).date()
        if ts_ist == target:
            alerts_fired += 1

    # Signal snapshot (system-wide, not per-user).  signal_correct gets
    # backfilled the next day, so today's value is typically null until then.
    sig = db.query(DailySignalSnapshot).filter(DailySignalSnapshot.date == target).first()

    def _trade_summary(t):
        if not t:
            return None
        return {
            "trade_id":    t.id,
            "symbol":      t.symbol or "NIFTY",
            "trade_type":  t.trade_type,
            "strike":      t.strike,
            "expiry_date": t.expiry_date,
            "realized_pnl": round(t.realized_pnl or 0, 2),
        }

    return {
        "date":             target.isoformat(),
        "realized_pnl":     round(realized, 2),
        "closed_count":     len(closed_today),
        "open_count":       open_count,
        "alerts_fired":     alerts_fired,
        "top_gainer":       _trade_summary(top_gainer),
        "top_loser":        _trade_summary(top_loser),
        "signal_direction": sig.directional_direction if sig else None,
        "signal_correct":   sig.signal_correct if sig else None,
        "nifty_close":      sig.nifty_close if sig else None,
        "next_day_change_pct": sig.next_day_change_pct if sig else None,
    }


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
