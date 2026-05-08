"""Background data refresh scheduler — fetches market data periodically
and pushes updates via WebSocket.

Market hours: Weekdays 9:00-15:45 IST (excluding NSE holidays).
Outside these hours, no API calls are made — only a "market_closed" message is sent.
"""

import asyncio
import traceback
import numpy as np
from datetime import datetime, timezone, timedelta
from sqlalchemy.orm import Session
from database import SessionLocal
from models import MarketSnapshot, SRLevel, Trade, TradeStatus, IVDailyRecord
from config import (
    LOT_SIZE, FAST_REFRESH_SECONDS, SLOW_REFRESH_MULTIPLIER,
    NEWS_DESK_ENABLED, NEWS_POLL_CYCLE_MULTIPLIER,
    NEWS_ACTIVE_HOUR_START, NEWS_ACTIVE_HOUR_END,
)


def _py(val):
    """Convert numpy types to native Python for JSON serialization."""
    if isinstance(val, (np.integer,)):
        return int(val)
    if isinstance(val, (np.floating,)):
        return float(val)
    return val

# IST = UTC+5:30
IST = timezone(timedelta(hours=5, minutes=30))

# Cross-cycle state for directional signal OI buildup tracking
_prev_oi_snapshot = None

# NSE holidays for 2026 (update yearly)
# Source: NSE circular — add dates as they are announced
NSE_HOLIDAYS_2026 = {
    "2026-01-26",  # Republic Day
    "2026-02-17",  # Mahashivratri (tentative)
    "2026-03-10",  # Holi
    "2026-03-30",  # Id-Ul-Fitr (tentative)
    "2026-04-02",  # Ram Navami
    "2026-04-03",  # Mahavir Jayanti
    "2026-04-14",  # Dr. Ambedkar Jayanti / Good Friday
    "2026-05-01",  # Maharashtra Day
    "2026-05-25",  # Buddha Purnima (tentative)
    "2026-06-06",  # Bakri Id (tentative)
    "2026-07-06",  # Muharram (tentative)
    "2026-08-15",  # Independence Day
    "2026-08-25",  # Ganesh Chaturthi (tentative)
    "2026-10-02",  # Mahatma Gandhi Jayanti
    "2026-10-20",  # Dussehra (tentative)
    "2026-10-21",  # Dussehra (tentative)
    "2026-11-09",  # Diwali (Laxmi Puja) (tentative)
    "2026-11-10",  # Diwali (Balipratipada) (tentative)
    "2026-11-30",  # Guru Nanak Jayanti (tentative)
    "2026-12-25",  # Christmas
}


def is_market_open() -> dict:
    """Check if NSE market is currently open.

    Returns dict with:
        open: bool
        reason: str (why closed, if closed)
        next_open: str (approximate next open time)
    """
    now_ist = datetime.now(IST)
    today_str = now_ist.strftime("%Y-%m-%d")
    weekday = now_ist.weekday()  # 0=Mon, 6=Sun
    current_time = now_ist.strftime("%H:%M")

    # Weekend
    if weekday >= 5:
        days_to_monday = 7 - weekday
        next_open = (now_ist + timedelta(days=days_to_monday)).replace(
            hour=9, minute=0, second=0
        )
        return {
            "open": False,
            "reason": "Weekend",
            "next_open": next_open.strftime("%a %d %b, 9:00 AM"),
        }

    # NSE holiday
    if today_str in NSE_HOLIDAYS_2026:
        next_day = now_ist + timedelta(days=1)
        while next_day.weekday() >= 5 or next_day.strftime("%Y-%m-%d") in NSE_HOLIDAYS_2026:
            next_day += timedelta(days=1)
        return {
            "open": False,
            "reason": "NSE Holiday",
            "next_open": next_day.strftime("%a %d %b, 9:00 AM"),
        }

    # Before market hours
    if current_time < "09:00":
        return {
            "open": False,
            "reason": "Pre-market",
            "next_open": "Today, 9:00 AM",
        }

    # After market hours
    if current_time > "15:45":
        next_day = now_ist + timedelta(days=1)
        while next_day.weekday() >= 5 or next_day.strftime("%Y-%m-%d") in NSE_HOLIDAYS_2026:
            next_day += timedelta(days=1)
        return {
            "open": False,
            "reason": "Market closed",
            "next_open": next_day.strftime("%a %d %b, 9:00 AM"),
        }

    # Market is open
    return {"open": True, "reason": "", "next_open": ""}


async def run_scheduler(ws_manager):
    """Main scheduler loop — runs in background."""
    cycle = 0
    last_market_status = None
    last_news_fetch_ts = 0.0   # epoch seconds; tracks news independently of market status
    news_interval = NEWS_POLL_CYCLE_MULTIPLIER * FAST_REFRESH_SECONDS  # 6 × 15 = 90s
    print("[Scheduler] Starting market data refresh loop...")

    # ── Seed fetch: if DB is empty, do one-time fetch regardless of market hours ──
    try:
        db = SessionLocal()
        try:
            has_data = db.query(MarketSnapshot).first() is not None
        finally:
            db.close()

        if not has_data:
            print("[Scheduler] No snapshot data in DB — running initial seed fetch...")
            db = SessionLocal()
            try:
                data = await asyncio.get_event_loop().run_in_executor(
                    None, _fetch_market_data, db, True  # slow_cycle=True to also fetch S/R
                )
                if data:
                    print("[Scheduler] Seed fetch complete — initial data stored")
                else:
                    print("[Scheduler] Seed fetch returned no data (NSE may be unreachable)")
            finally:
                db.close()
    except Exception as e:
        print(f"[Scheduler] Seed fetch error: {e}")

    while True:
        try:
            import time as _time
            now_ts = _time.monotonic()

            # ── News fetch — 24/7 cadence, no market-hour or active-window gate.
            # The dashboard ticker and News Desk page consume this feed at all
            # hours, so we let the poll fire on its own ~90s interval round
            # the clock; the active-hour window has been removed.
            if (NEWS_DESK_ENABLED and
                    now_ts - last_news_fetch_ts >= news_interval):
                last_news_fetch_ts = now_ts
                db = SessionLocal()
                try:
                    await _fetch_and_store_news(db, ws_manager)
                except Exception as e:
                    print(f"[Scheduler] News fetch error: {e}")
                finally:
                    db.close()

            market_status = is_market_open()

            # If market is closed, broadcast status and skip data fetch
            if not market_status["open"]:
                if last_market_status is None or last_market_status.get("open", True):
                    # Status just changed to closed — broadcast once
                    print(f"[Scheduler] Market closed: {market_status['reason']}")

                    # Send last known snapshot + closed status
                    db = SessionLocal()
                    try:
                        snap = db.query(MarketSnapshot).order_by(
                            MarketSnapshot.timestamp.desc()
                        ).first()
                        snapshot_data = None
                        if snap:
                            snapshot_data = {
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
                                "timestamp": snap.timestamp.isoformat() if snap.timestamp else None,
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
                    finally:
                        db.close()

                    await ws_manager.broadcast({
                        "type": "market_status",
                        "data": {
                            "open": False,
                            "reason": market_status["reason"],
                            "next_open": market_status["next_open"],
                            "last_snapshot": snapshot_data,
                        },
                    })

                last_market_status = market_status
                # Sleep longer when market is closed (check every 30s)
                await asyncio.sleep(30)
                continue

            # Market is open — normal refresh cycle
            if last_market_status and not last_market_status.get("open", True):
                print("[Scheduler] Market opened — resuming live data refresh")
                await ws_manager.broadcast({
                    "type": "market_status",
                    "data": {"open": True, "reason": "", "next_open": ""},
                })

            last_market_status = market_status
            cycle += 1
            is_slow_cycle = (cycle % SLOW_REFRESH_MULTIPLIER == 0)

            db = SessionLocal()
            try:
                data = await asyncio.get_event_loop().run_in_executor(
                    None, _fetch_market_data, db, is_slow_cycle
                )

                if data:
                    await ws_manager.broadcast({
                        "type": "market_update",
                        "data": data["snapshot"],
                    })

                    if data.get("option_chain"):
                        await ws_manager.broadcast({
                            "type": "chain_update",
                            "data": data["option_chain"],
                        })

                    for user_id in ws_manager.connected_user_ids:
                        user_data = await asyncio.get_event_loop().run_in_executor(
                            None, _compute_user_positions, db, user_id, data
                        )
                        if user_data:
                            await ws_manager.send_to_user(user_id, {
                                "type": "positions_update",
                                "data": user_data,
                            })

                    if is_slow_cycle and data.get("sr_levels"):
                        await ws_manager.broadcast({
                            "type": "sr_update",
                            "data": data["sr_levels"],
                        })

                    if data.get("risk_reversal"):
                        await ws_manager.broadcast({
                            "type": "risk_reversal_update",
                            "data": data["risk_reversal"],
                        })

                    # ── Evaluate alert rules ───────────────────────────
                    try:
                        from alert_engine import evaluate_alerts
                        triggered = evaluate_alerts(
                            db,
                            snapshot=data["snapshot"],
                            chain_df=data.get("chain_df"),
                            risk_data=None,  # risk computed per-user above
                        )
                        for alert in triggered:
                            await ws_manager.send_to_user(alert["user_id"], {
                                "type": "alert_triggered",
                                "data": alert,
                            })
                        if triggered:
                            print(f"[Scheduler] {len(triggered)} alert(s) triggered")
                    except Exception as e:
                        print(f"[Scheduler] Alert eval error: {e}")

                    # ── Trade Suggestion Engine ───────────────────────
                    try:
                        from trade_suggestion_engine import (
                            evaluate_overnight_trigger, evaluate_intraday_trigger,
                            run_sanity_check, update_suggestion_prices,
                            expire_stale_suggestions,
                        )
                        now_ist = datetime.now(IST)
                        time_str = now_ist.strftime("%H:%M")
                        chain_df = data.get("chain_df")
                        spot = data.get("spot", 0)
                        rr = data.get("risk_reversal", {})
                        snap = data.get("snapshot", {})

                        # Expire stale suggestions
                        expire_stale_suggestions(db, time_str)

                        # Overnight trigger (15:15-15:20)
                        if "15:15" <= time_str <= "15:20":
                            from models import TradeSuggestion
                            existing = db.query(TradeSuggestion).filter(
                                TradeSuggestion.suggestion_type == "OVERNIGHT",
                                TradeSuggestion.status.in_(["PENDING", "ACTIVE"]),
                            ).first()
                            if not existing and chain_df is not None:
                                sug = evaluate_overnight_trigger(snap, chain_df, rr, spot)
                                if sug:
                                    db.add(TradeSuggestion(**sug))
                                    db.commit()
                                    print(f"[Scheduler] Overnight suggestion: {sug['direction']} {sug['option_type']} {sug['strike']}")

                        # Overnight sanity check (15:25-15:30)
                        if "15:25" <= time_str <= "15:30":
                            from models import TradeSuggestion
                            pending = db.query(TradeSuggestion).filter(
                                TradeSuggestion.suggestion_type == "OVERNIGHT",
                                TradeSuggestion.status == "PENDING",
                            ).all()
                            for s in pending:
                                passed = run_sanity_check(db, s, snap, chain_df)
                                msg = "PASSED" if passed else "FAILED"
                                print(f"[Scheduler] Overnight sanity check {msg}: {s.strike}")

                        # Intraday trigger (10:00-10:05)
                        if "10:00" <= time_str <= "10:05":
                            from models import TradeSuggestion
                            existing = db.query(TradeSuggestion).filter(
                                TradeSuggestion.suggestion_type == "INTRADAY",
                                TradeSuggestion.status.in_(["PENDING", "ACTIVE"]),
                            ).first()
                            if not existing and chain_df is not None:
                                sug = evaluate_intraday_trigger(snap, chain_df, rr, spot)
                                if sug:
                                    db.add(TradeSuggestion(**sug))
                                    db.commit()
                                    print(f"[Scheduler] Intraday suggestion: {sug['direction']} {sug['option_type']} {sug['strike']}")

                        # Intraday sanity checks (every cycle 10:15-14:00, ~15min intervals handled by scheduler cycle)
                        if "10:15" <= time_str <= "14:00":
                            from models import TradeSuggestion
                            active_intra = db.query(TradeSuggestion).filter(
                                TradeSuggestion.suggestion_type == "INTRADAY",
                                TradeSuggestion.status == "ACTIVE",
                            ).all()
                            for s in active_intra:
                                # Only recheck every ~3 min (slow cycle)
                                if is_slow_cycle:
                                    run_sanity_check(db, s, snap, chain_df)

                        # Update prices for active suggestions
                        if chain_df is not None:
                            update_suggestion_prices(db, chain_df, spot)

                        # Broadcast active suggestions to all users
                        from models import TradeSuggestion
                        active_sug = db.query(TradeSuggestion).filter(
                            TradeSuggestion.status.in_(["ACTIVE", "PENDING"]),
                        ).all()
                        if active_sug:
                            await ws_manager.broadcast({
                                "type": "trade_suggestions",
                                "data": [
                                    {
                                        "id": s.id,
                                        "type": s.suggestion_type,
                                        "direction": s.direction,
                                        "option_type": s.option_type,
                                        "strike": s.strike,
                                        "entry_price": s.entry_price,
                                        "current_price": s.current_price,
                                        "stop_loss": s.stop_loss,
                                        "target": s.target,
                                        "confidence": s.confidence,
                                        "rationale": s.rationale,
                                        "status": s.status,
                                        "created_at": s.created_at.isoformat() if s.created_at else None,
                                    }
                                    for s in active_sug
                                ],
                            })
                    except Exception as e:
                        print(f"[Scheduler] Trade suggestion error: {e}")

                    # ── EOD Signal Capture (3:28-3:35 window) ─────────
                    try:
                        now_ist = datetime.now(IST)
                        time_str = now_ist.strftime("%H:%M")
                        if "15:28" <= time_str <= "15:35":
                            _capture_eod_snapshot(db, data)
                    except Exception as e:
                        print(f"[Scheduler] EOD capture error: {e}")

            finally:
                db.close()

        except Exception as e:
            print(f"[Scheduler] Error: {e}")
            traceback.print_exc()

        await asyncio.sleep(FAST_REFRESH_SECONDS)


async def _fetch_and_store_news(db: Session, ws_manager):
    """Fetch RSS news in executor, broadcast HIGH/CRITICAL items via WebSocket."""
    from news_scraper import fetch_and_store_news

    stats, new_items = await asyncio.get_event_loop().run_in_executor(
        None, fetch_and_store_news, db
    )
    print(f"[Scheduler] News: fetched={stats['fetched']}, stored={stats['new_stored']}, "
          f"dup={stats['skipped_dup']}, low={stats['skipped_low_score']}, err={stats['errors']}")

    push_items = [i for i in new_items if i.impact_tier in ("HIGH", "CRITICAL")]
    if push_items:
        import json
        payload = []
        for item in push_items:
            payload.append({
                "id": item.id,
                "headline": item.headline,
                "source": item.source,
                "impact_tier": item.impact_tier,
                "direction": item.direction,
                "category": item.category,
                "fetched_at": item.fetched_at.isoformat() if item.fetched_at else None,
                "published_at": item.published_at.isoformat() if item.published_at else None,
            })
            item.is_pushed_to_ticker = True
        db.commit()
        await ws_manager.broadcast({"type": "news_alert", "data": {"items": payload}})


def _fetch_market_data(db: Session, is_slow_cycle: bool) -> dict | None:
    """Fetch all market data (runs in thread pool)."""
    global _prev_oi_snapshot
    from market_data import (
        build_chain, fetch_vix, calculate_ranges, days_to_expiry,
        find_oi_sr_levels, fetch_fibonacci_levels, fetch_pivot_levels,
        merge_sr_levels, compute_pivot_oi_sr,
        compute_risk_reversal, compute_tendency,
        compute_directional_signal, snapshot_oi, compute_max_pain,
        select_trade_strike, pick_active_expiry,
    )

    try:
        chain_df, expiries, spot = build_chain("NIFTY")
        if chain_df.empty:
            print("[Scheduler] Empty chain data")
            return None
    except Exception as e:
        print(f"[Scheduler] Chain fetch failed: {e}")
        return None

    # Pick the *active* expiry: nearest one with ≥ 2 trading days remaining.
    # On expiry day OR the day before, the chain's IV/skew/RR readings are too
    # degenerate to drive the dashboard / signal / tendency engines reliably,
    # so we roll to the next series.  This rule (centralised in
    # `pick_active_expiry`) is what every surface — dashboard, analytics,
    # strategy builder — uses, so they never disagree on what "this expiry"
    # means.
    expiry = pick_active_expiry(expiries) or ""
    dte = days_to_expiry(expiry) if expiry else 0

    # If we rolled past the default nearest expiry, rebuild chain for the
    # selected expiry so OI/IV/greeks reflect the tradeable series.
    if expiry and expiries and expiry != str(expiries[0]):
        try:
            chain_df, _exp_list, spot = build_chain("NIFTY", expiry)
            if chain_df.empty:
                print(f"[Scheduler] Empty chain for rolled expiry {expiry}; keeping nearest")
        except Exception as e:
            print(f"[Scheduler] Failed to rebuild chain for {expiry}: {e}")

    vix_data = fetch_vix()
    vix = vix_data["last"] if vix_data else 0
    vix_change = vix_data["change"] if vix_data else 0
    vix_change_pct = vix_data["pChange"] if vix_data else 0
    nifty_change = vix_data.get("nifty_change", 0) if vix_data else 0
    nifty_change_pct = vix_data.get("nifty_change_pct", 0) if vix_data else 0

    # Use default offset for snapshot; per-user offsets applied on frontend
    default_offset = 50  # default VIX offset %
    ranges = calculate_ranges(spot, vix, dte, vix_offset_pct=default_offset)
    rr = compute_risk_reversal(chain_df)
    # Compute RR z-score so the broadcast tendency uses the same definition as
    # the Strategy Builder & Dashboard verdict.  Without this the WS push (15s
    # cadence) would overwrite the Skew card with the absolute-fallback label
    # ("Flat") and contradict the verdict-driven label ("Bullish Skew").
    rr_zscore = None
    try:
        from routes_strategies import _compute_rr_zscore
        rr_zscore = _compute_rr_zscore(db, rr.get("rr_value", 0) or 0)
    except Exception:
        rr_zscore = None
    tendency_data = compute_tendency(
        rr["rr_value"], vix, vix_change_pct, rr_zscore=rr_zscore,
    )

    # 8-factor directional signal
    vix_data_full = vix_data if vix_data else None
    dir_signal = compute_directional_signal(chain_df, spot, vix_data_full, _prev_oi_snapshot)
    _prev_oi_snapshot = snapshot_oi(chain_df, spot)

    # Trade suggestion based on directional signal
    trade_suggestion = None
    if dir_signal["direction"] != "NEUTRAL":
        trade_suggestion = select_trade_strike(chain_df, spot, dir_signal["direction"], dte)

    snapshot = MarketSnapshot(
        nifty_spot=spot,
        nifty_change=nifty_change,
        nifty_change_pct=nifty_change_pct,
        india_vix=vix,
        vix_change=vix_change,
        vix_change_pct=vix_change_pct,
        calculated_range_top=ranges["calc_range_top"],
        calculated_range_bottom=ranges["calc_range_bottom"],
        vix_range_top=ranges["vix_range_top"],
        vix_range_bottom=ranges["vix_range_bottom"],
        expected_move=0,  # deprecated, range shown directly
        days_to_expiry=dte,
        tendency=tendency_data["label"],
        expiry_date=expiry,
        expiries_json=[str(e) for e in expiries],
        rr_ce_strike=rr.get("ce_strike"),
        rr_ce_iv=rr.get("ce_iv"),
        rr_pe_strike=rr.get("pe_strike"),
        rr_pe_iv=rr.get("pe_iv"),
        rr_value=rr.get("rr_value"),
        rr_verdict=rr.get("verdict"),
        max_pain=dir_signal.get("max_pain", 0),
        directional_direction=dir_signal.get("direction"),
        directional_score=dir_signal.get("score"),
        directional_factors_agree=dir_signal.get("factors_agree"),
    )
    db.add(snapshot)
    db.commit()

    now_ist = datetime.now(IST)
    snapshot_data = {
        "nifty_spot": spot,
        "nifty_change": nifty_change,
        "nifty_change_pct": nifty_change_pct,
        "india_vix": vix,
        "vix_change": vix_change,
        "vix_change_pct": vix_change_pct,
        "calc_range_top": ranges["calc_range_top"],
        "calc_range_bottom": ranges["calc_range_bottom"],
        "vix_range_top": ranges["vix_range_top"],
        "vix_range_bottom": ranges["vix_range_bottom"],
        "days_to_expiry": dte,
        "tendency": tendency_data["label"],
        "tendency_detail": tendency_data,
        "directional_signal": dir_signal,
        "trade_suggestion": trade_suggestion,
        "max_pain": dir_signal.get("max_pain", 0),
        "expiry_date": expiry,
        "expiries": expiries,
        "timestamp": now_ist.isoformat(),
    }

    result = {
        "snapshot": snapshot_data,
        "risk_reversal": rr,
        "chain_df": chain_df,
        "spot": spot,
        "expiry": expiry,
    }

    # Build option chain for frontend
    strikes = sorted(chain_df["Strike"].unique())
    chain_rows = []
    for strike in strikes:
        row_data = {"strike": _py(strike)}
        ce = chain_df[(chain_df["Strike"] == strike) & (chain_df["Type"] == "CE")]
        pe = chain_df[(chain_df["Strike"] == strike) & (chain_df["Type"] == "PE")]
        if not ce.empty:
            c = ce.iloc[0]
            row_data.update({
                "ce_ltp": _py(c["LTP"]), "ce_iv": _py(c["IV"]),
                "ce_delta": _py(c["delta"]), "ce_oi": _py(c["OI"]),
            })
        if not pe.empty:
            p = pe.iloc[0]
            row_data.update({
                "pe_ltp": _py(p["LTP"]), "pe_iv": _py(p["IV"]),
                "pe_delta": _py(p["delta"]), "pe_oi": _py(p["OI"]),
            })
        chain_rows.append(row_data)

    result["option_chain"] = {"rows": chain_rows, "spot": spot}

    # S/R levels on slow cycle
    if is_slow_cycle:
        try:
            oi_sr = find_oi_sr_levels(chain_df, spot)
            fib_levels = fetch_fibonacci_levels()
            pivot_data = fetch_pivot_levels()

            # Merge OI + Fibonacci with confluence prioritisation
            merged_supports, merged_resistances = merge_sr_levels(
                oi_sr["supports"], oi_sr["resistances"],
                fib_levels, spot,
            )

            # Pivot + OI ranked S/R
            pivot_supports, pivot_resistances = compute_pivot_oi_sr(
                chain_df, spot, pivot_data,
            )

            now = datetime.utcnow()
            for s in merged_supports:
                db.add(SRLevel(timestamp=now, expiry_date=expiry, source="OI_FIB",
                               level_type="SUPPORT", strike=int(s["strike"]),
                               oi=s.get("oi"), label=s.get("source")))
            for r in merged_resistances:
                db.add(SRLevel(timestamp=now, expiry_date=expiry, source="OI_FIB",
                               level_type="RESISTANCE", strike=int(r["strike"]),
                               oi=r.get("oi"), label=r.get("source")))
            for s in pivot_supports:
                db.add(SRLevel(timestamp=now, expiry_date=expiry, source="PIVOT_OI",
                               level_type="SUPPORT", strike=int(s["strike"]),
                               oi=s.get("oi"), label=s.get("source")))
            for r in pivot_resistances:
                db.add(SRLevel(timestamp=now, expiry_date=expiry, source="PIVOT_OI",
                               level_type="RESISTANCE", strike=int(r["strike"]),
                               oi=r.get("oi"), label=r.get("source")))
            db.commit()

            result["sr_levels"] = {
                "oi_fib": {
                    "supports": merged_supports,
                    "resistances": merged_resistances,
                },
                "pivot": {
                    "supports": pivot_supports,
                    "resistances": pivot_resistances,
                    "raw": pivot_data,
                },
                "fib_levels": fib_levels,
            }
        except Exception as e:
            print(f"[Scheduler] S/R fetch failed: {e}")

    return result


def _capture_eod_snapshot(db: Session, data: dict):
    """Capture end-of-day signal snapshot for historical insights."""
    from models import DailySignalSnapshot, IVDailyRecord

    today = datetime.now(IST).date()

    # Already captured today?
    existing = db.query(DailySignalSnapshot).filter(
        DailySignalSnapshot.date == today
    ).first()
    if existing:
        return

    snap = data.get("snapshot", {})
    rr = data.get("risk_reversal", {})
    dir_sig = snap.get("directional_signal", {}) or {}

    eod = DailySignalSnapshot(
        date=today,
        nifty_close=snap.get("nifty_spot", 0),
        india_vix=snap.get("india_vix"),
        rr_value=rr.get("rr_value"),
        rr_verdict=rr.get("verdict"),
        tendency=snap.get("tendency"),
        directional_direction=dir_sig.get("direction"),
        directional_score=dir_sig.get("score"),
        directional_factors_agree=dir_sig.get("factors_agree"),
        max_pain=snap.get("max_pain"),
    )
    db.add(eod)

    # Backfill previous day's next_day outcome
    prev = db.query(DailySignalSnapshot).filter(
        DailySignalSnapshot.date < today,
        DailySignalSnapshot.next_day_close.is_(None),
    ).order_by(DailySignalSnapshot.date.desc()).first()

    if prev and snap.get("nifty_spot"):
        prev.next_day_close = snap["nifty_spot"]
        prev.next_day_change = snap["nifty_spot"] - prev.nifty_close
        prev.next_day_change_pct = (
            (snap["nifty_spot"] - prev.nifty_close) / prev.nifty_close * 100
            if prev.nifty_close else 0
        )
        # Was the signal correct?
        if prev.directional_direction in ("BULLISH", "BEARISH"):
            actual_up = prev.next_day_change > 0
            prev.signal_correct = (
                (prev.directional_direction == "BULLISH" and actual_up)
                or (prev.directional_direction == "BEARISH" and not actual_up)
            )

    db.commit()
    print(f"[Scheduler] EOD signal snapshot captured for {today}")

    # ── Capture actual ATM IV for IV Rank history ──────────────────────────
    try:
        from models import IVTermStructureRecord, IVSkewDaily
        from market_data import build_chain, find_25delta_iv, days_to_expiry as _dte

        chain_df = data.get("chain_df")
        spot = data.get("spot") or snap.get("nifty_spot", 0)
        expiry = data.get("expiry") or snap.get("expiry_date", "")
        expiries_list = snap.get("expiries") or (data.get("snapshot", {}).get("expiries") or [])

        if chain_df is not None and not chain_df.empty and spot:
            atm_strike = round(spot / 50) * 50

            # ATM IV (CE and PE separately)
            ce_row = chain_df[(chain_df["Strike"] == atm_strike) & (chain_df["Type"] == "CE")]
            pe_row = chain_df[(chain_df["Strike"] == atm_strike) & (chain_df["Type"] == "PE")]
            ce_iv = float(ce_row["IV"].iloc[0]) if not ce_row.empty else 0.0
            pe_iv = float(pe_row["IV"].iloc[0]) if not pe_row.empty else 0.0
            ce_ltp = float(ce_row["LTP"].iloc[0]) if not ce_row.empty else 0.0
            pe_ltp = float(pe_row["LTP"].iloc[0]) if not pe_row.empty else 0.0

            n_valid = int(ce_iv > 0) + int(pe_iv > 0)
            if n_valid > 0:
                atm_iv = (ce_iv + pe_iv) / n_valid
                straddle_price = round(ce_ltp + pe_ltp, 2)

                iv_rec = db.query(IVDailyRecord).filter(IVDailyRecord.date == today).first()
                if iv_rec:
                    iv_rec.atm_iv = round(atm_iv, 4)
                    iv_rec.atm_iv_ce = round(ce_iv, 4) if ce_iv > 0 else None
                    iv_rec.atm_iv_pe = round(pe_iv, 4) if pe_iv > 0 else None
                    iv_rec.atm_straddle_price = straddle_price
                    iv_rec.india_vix = snap.get("india_vix")
                    iv_rec.nifty_close = spot
                    iv_rec.atm_strike = atm_strike
                    iv_rec.expiry_date = expiry
                    iv_rec.is_proxy = False
                else:
                    iv_rec = IVDailyRecord(
                        date=today,
                        atm_iv=round(atm_iv, 4),
                        atm_iv_ce=round(ce_iv, 4) if ce_iv > 0 else None,
                        atm_iv_pe=round(pe_iv, 4) if pe_iv > 0 else None,
                        atm_straddle_price=straddle_price,
                        india_vix=snap.get("india_vix"),
                        nifty_close=spot,
                        atm_strike=atm_strike,
                        expiry_date=expiry,
                        is_proxy=False,
                    )
                    db.add(iv_rec)

                # 25-delta skew (risk reversal + butterfly)
                try:
                    ce25_strike, ce25_iv = find_25delta_iv(chain_df, "CE")
                    pe25_strike, pe25_iv = find_25delta_iv(chain_df, "PE")
                    if ce25_iv > 0 and pe25_iv > 0:
                        rr_val = round(ce25_iv - pe25_iv, 4)
                        butterfly = round((ce25_iv + pe25_iv) / 2.0 - atm_iv, 4)
                        skew_rec = db.query(IVSkewDaily).filter(IVSkewDaily.date == today).first()
                        if skew_rec:
                            skew_rec.delta25_call_iv = round(ce25_iv, 4)
                            skew_rec.delta25_put_iv = round(pe25_iv, 4)
                            skew_rec.risk_reversal = rr_val
                            skew_rec.butterfly = butterfly
                            skew_rec.atm_iv = round(atm_iv, 4)
                        else:
                            db.add(IVSkewDaily(
                                date=today,
                                delta25_call_iv=round(ce25_iv, 4),
                                delta25_put_iv=round(pe25_iv, 4),
                                risk_reversal=rr_val,
                                butterfly=butterfly,
                                atm_iv=round(atm_iv, 4),
                            ))
                except Exception as e_skew:
                    print(f"[Scheduler] Skew capture error: {e_skew}")

                db.commit()
                print(f"[Scheduler] IV record captured for {today}: ATM IV={atm_iv:.2f}% (CE={ce_iv:.2f}%, PE={pe_iv:.2f}%)")

        # ── Term structure: capture ATM IV for up to 4 expiries ───────────
        try:
            if expiries_list:
                # Delete today's old term structure records before re-inserting
                db.query(IVTermStructureRecord).filter(
                    IVTermStructureRecord.date == today
                ).delete(synchronize_session=False)
                db.commit()

                for exp in expiries_list[:4]:
                    try:
                        ts_df, _, ts_spot = build_chain("NIFTY", exp)
                        if ts_df is None or ts_df.empty:
                            continue
                        ts_atm = round(ts_spot / 50) * 50
                        ts_ce = ts_df[(ts_df["Strike"] == ts_atm) & (ts_df["Type"] == "CE")]
                        ts_pe = ts_df[(ts_df["Strike"] == ts_atm) & (ts_df["Type"] == "PE")]
                        ts_ce_iv = float(ts_ce["IV"].iloc[0]) if not ts_ce.empty else 0.0
                        ts_pe_iv = float(ts_pe["IV"].iloc[0]) if not ts_pe.empty else 0.0
                        ts_atm_iv = (ts_ce_iv + ts_pe_iv) / max(1, int(ts_ce_iv > 0) + int(ts_pe_iv > 0))
                        ts_ce_ltp = float(ts_ce["LTP"].iloc[0]) if not ts_ce.empty else 0.0
                        ts_pe_ltp = float(ts_pe["LTP"].iloc[0]) if not ts_pe.empty else 0.0
                        db.add(IVTermStructureRecord(
                            date=today,
                            expiry=exp,
                            dte=_dte(exp),
                            atm_iv=round(ts_atm_iv, 4),
                            atm_straddle=round(ts_ce_ltp + ts_pe_ltp, 2),
                        ))
                    except Exception:
                        continue
                db.commit()
                print(f"[Scheduler] Term structure captured for {today} ({min(4, len(expiries_list))} expiries)")
        except Exception as e_ts:
            print(f"[Scheduler] Term structure capture error: {e_ts}")

    except Exception as e:
        print(f"[Scheduler] IV record capture error: {e}")


def _expire_user_past_trades(db: Session, user_id: int) -> int:
    """Transition OPEN trades whose expiry date has passed (IST) to EXPIRED.

    Duplicates the logic in routes_trades._expire_past_trades so the scheduler
    can call it without creating a circular import with the routes module.
    """
    today_ist = datetime.now(IST).date()

    open_trades = db.query(Trade).filter(
        Trade.user_id == user_id,
        Trade.status == TradeStatus.OPEN.value,
    ).all()

    count = 0
    for t in open_trades:
        if not t.expiry_date:
            continue
        try:
            from datetime import datetime as _dt
            exp_date = _dt.strptime(t.expiry_date, "%d-%b-%Y").date()
        except ValueError:
            continue
        if exp_date < today_ist:
            t.status = TradeStatus.EXPIRED.value
            if not t.exit_date:
                t.exit_date = datetime.now(IST).replace(tzinfo=None)
            count += 1

    if count:
        # Promote OPEN children of just-expired parents so they keep showing
        # as standalone open positions (parity with the close-path behaviour
        # in routes_trades).
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
        print(f"[Scheduler] Auto-expired {count} trade(s) for user {user_id}")

    return count


def _compute_user_positions(db: Session, user_id: int, market_data: dict) -> dict | None:
    """Compute live position data for a specific user.

    Each trade's IV/delta/LTP is looked up from the chain for *that trade's own
    expiry*, not the scheduler's current (nearest) expiry.  Chains for non-default
    expiries are fetched on demand and cached for the duration of this call.
    """
    from market_data import compute_risk_score, compute_hedge_v2, days_to_expiry, build_chain

    chain_df_default = market_data.get("chain_df")   # nearest-expiry chain from scheduler
    spot = market_data.get("spot", 0)
    vix = market_data["snapshot"].get("india_vix", 0)
    vix_chg_pct = market_data["snapshot"].get("vix_change_pct", 0)
    default_expiry = market_data.get("expiry", "")

    if chain_df_default is None or chain_df_default.empty:
        return None

    open_trades = db.query(Trade).filter(
        Trade.user_id == user_id,
        Trade.status == TradeStatus.OPEN.value,
    ).all()

    if not open_trades:
        return None

    # Auto-expire any trades whose expiry date has passed before computing positions.
    # This keeps the WS push consistent with the REST endpoints.
    _expire_user_past_trades(db, user_id)
    # Re-query after potential status changes
    open_trades = db.query(Trade).filter(
        Trade.user_id == user_id,
        Trade.status == TradeStatus.OPEN.value,
    ).all()
    if not open_trades:
        return None

    # Build a per-expiry chain cache so we look up each trade in its own expiry.
    # Start with the already-fetched default chain to avoid a redundant NSE call.
    chain_by_expiry: dict = {default_expiry: chain_df_default}
    unique_expiries = {
        t.expiry_date for t in open_trades
        if t.expiry_date and t.expiry_date != default_expiry
    }
    for exp in unique_expiries:
        try:
            df, _, _ = build_chain("NIFTY", exp)
            if df is not None and not df.empty:
                chain_by_expiry[exp] = df
            else:
                chain_by_expiry[exp] = chain_df_default  # fallback
        except Exception as e:
            print(f"[Scheduler] Failed to fetch chain for expiry {exp}: {e}")
            chain_by_expiry[exp] = chain_df_default  # fallback

    # Get S/R levels for hedge engine
    sr_levels = market_data.get("sr_levels")
    supports = []
    resistances = []
    if sr_levels and sr_levels.get("oi_fib"):
        supports = sr_levels["oi_fib"].get("supports", [])
        resistances = sr_levels["oi_fib"].get("resistances", [])

    positions = []
    ce_risk_scores = []
    pe_risk_scores = []
    hedge_suggestions = []

    for trade in open_trades:
        # Resolve the chain and DTE for *this trade's* expiry
        trade_expiry = trade.expiry_date or default_expiry
        chain_df = chain_by_expiry.get(trade_expiry, chain_df_default)
        trade_dte = days_to_expiry(trade_expiry) if trade_expiry else 30

        # Futures hedges live outside the option chain — pull live LTP from
        # the validated NSE futures feed and emit a position with proper MTM
        # so the Positions page renders them alongside option legs without
        # showing "—" everywhere.
        if (trade.instrument_type or "OPT") == "FUT":
            try:
                from nse_futures import get_contract_by_expiry, NIFTY_LOT
                contract = get_contract_by_expiry(trade.expiry_date)
            except Exception:
                contract = None
            fut_lot = trade.lot_size_override or LOT_SIZE
            if contract:
                fut_ltp = contract["ltp"]
                if trade.trade_type == "FUT_BUY":
                    fut_pnl = (fut_ltp - trade.entry_premium) * trade.quantity * fut_lot
                else:
                    fut_pnl = (trade.entry_premium - fut_ltp) * trade.quantity * fut_lot
                positions.append({
                    "trade_id": trade.id,
                    "trade_type": trade.trade_type,
                    "instrument_type": "FUT",
                    "strike": trade.strike,
                    "expiry_date": trade.expiry_date,
                    "entry_premium": trade.entry_premium,
                    "quantity": trade.quantity,
                    "lot_size": fut_lot,
                    "current_ltp": fut_ltp,
                    "current_iv": None,
                    "current_delta": 1.0 if trade.trade_type == "FUT_BUY" else -1.0,
                    "iv_change_pct": 0,
                    "unrealized_pnl": round(fut_pnl, 2),
                    "verified": contract.get("verified", True),
                    "parent_trade_id": trade.parent_trade_id,
                })
            else:
                positions.append({
                    "trade_id": trade.id,
                    "trade_type": trade.trade_type,
                    "instrument_type": "FUT",
                    "strike": trade.strike,
                    "current_ltp": None,
                    "current_iv": None,
                    "current_delta": None,
                    "unrealized_pnl": None,
                    "parent_trade_id": trade.parent_trade_id,
                })
            continue

        opt_type = "CE" if "CE" in trade.trade_type else "PE"
        match = chain_df[(chain_df["Strike"] == trade.strike) & (chain_df["Type"] == opt_type)]

        if match.empty:
            positions.append({
                "trade_id": trade.id,
                "trade_type": trade.trade_type,
                "strike": trade.strike,
                "current_ltp": None,
                "current_iv": None,
                "current_delta": None,
                "unrealized_pnl": None,
            })
            continue

        row = match.iloc[0]
        current_ltp = float(row["LTP"])
        current_iv = float(row["IV"])
        current_delta = float(row["delta"])
        current_gamma = float(row.get("gamma", 0))
        current_theta = float(row.get("theta", 0))

        if "SELL" in trade.trade_type:
            unrealized_pnl = round((trade.entry_premium - current_ltp) * LOT_SIZE * trade.quantity, 2)
        else:
            unrealized_pnl = round((current_ltp - trade.entry_premium) * LOT_SIZE * trade.quantity, 2)

        iv_change_pct = round(((current_iv - trade.entry_iv) / trade.entry_iv * 100)
                              if trade.entry_iv > 0 else 0, 1)

        risk = None
        hedge = None
        # Risk scoring (margin/assignment risk) only applies to SHORT options.
        # Long options have defined risk = premium paid up-front; there is no
        # margin block and no "hedge required" trigger to compute.
        is_short = "SELL" in trade.trade_type
        is_top_short = trade.parent_trade_id is None and is_short
        if is_top_short:
            risk = compute_risk_score(
                delta=current_delta,
                entry_iv=trade.entry_iv,
                current_iv=current_iv,
                entry_premium=trade.entry_premium,
                current_ltp=current_ltp,
                current_spot=spot,
                strike=trade.strike,
                vix_abs=vix,
                gamma=current_gamma,
                theta=current_theta,
                dte=trade_dte,        # ← per-trade DTE, not scheduler's expiry DTE
                vix_chg_pct=vix_chg_pct,
            )
            if "CE" in trade.trade_type:
                ce_risk_scores.append(risk["score"])
            else:
                pe_risk_scores.append(risk["score"])

            # Compute hedge suggestion when needed (already gated to SHORT above)
            if risk.get("hedge_required"):
                try:
                    hedge = compute_hedge_v2(
                        side=opt_type,
                        sold_strike=trade.strike,
                        entry_premium=trade.entry_premium,
                        current_spot=spot,
                        chain_df=chain_df,   # ← per-trade chain
                        dte=trade_dte,       # ← per-trade DTE
                        supports=supports,
                        resistances=resistances,
                    )
                    if hedge:
                        hedge_suggestions.append({
                            "trade_id": trade.id,
                            "trade_type": trade.trade_type,
                            "strike": trade.strike,
                            "hedge": hedge,
                        })
                except Exception as e:
                    print(f"[Scheduler] Hedge computation failed for trade {trade.id}: {e}")

        # For long options, max loss is the premium paid (no margin, no assignment).
        max_loss = (
            round(trade.entry_premium * LOT_SIZE * trade.quantity, 2)
            if not is_short else None
        )

        positions.append({
            "trade_id": trade.id,
            "trade_type": trade.trade_type,
            "strike": trade.strike,
            "expiry_date": trade.expiry_date,
            "entry_premium": trade.entry_premium,
            "entry_iv": trade.entry_iv,
            "entry_delta": trade.entry_delta,
            "quantity": trade.quantity,
            "current_ltp": current_ltp,
            "current_iv": current_iv,
            "current_delta": current_delta,
            "current_gamma": current_gamma,
            "current_theta": current_theta,
            "iv_change_pct": iv_change_pct,
            "unrealized_pnl": unrealized_pnl,
            "risk": risk,
            "hedge": hedge,
            "max_loss": max_loss,        # only set for long options
            "parent_trade_id": trade.parent_trade_id,
        })

    return {
        "positions": positions,
        "ce_risk": max(ce_risk_scores) if ce_risk_scores else None,
        "pe_risk": max(pe_risk_scores) if pe_risk_scores else None,
        "hedge_suggestions": hedge_suggestions,
        "spot": spot,
    }
