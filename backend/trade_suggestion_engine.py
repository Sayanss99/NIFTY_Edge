"""Trade Suggestion Engine — conservative buy signal generation.

Two signal types:
  - OVERNIGHT: triggered ~15:15, sanity check at ~15:25
  - INTRADAY: triggered ~10:00, sanity checks every 15 min until 14:00

Buy trades are risky — all thresholds are strict to minimize false signals.
"""

from datetime import datetime
from sqlalchemy.orm import Session
from models import TradeSuggestion

# ── Confidence thresholds ────────────────────────────────────────────────

OVERNIGHT_MIN_FACTORS = 6      # out of 8
OVERNIGHT_MIN_ABS_SCORE = 2.0
OVERNIGHT_MIN_CONFIDENCE = 60

INTRADAY_MIN_FACTORS = 6
INTRADAY_MIN_ABS_SCORE = 2.5
INTRADAY_MIN_CONFIDENCE = 65

SANITY_MIN_FACTORS = 5         # relaxed from trigger threshold


def _compute_confidence(factors_agree, score, tendency_agrees, rr_agrees):
    """Composite confidence score 0-100."""
    base = (factors_agree - 4) * 15       # 6 factors = 30, 7 = 45, 8 = 60
    score_bonus = min(30, abs(score) * 10) # up to 30
    agree_bonus = (10 if tendency_agrees else 0) + (10 if rr_agrees else 0)
    return min(100, base + score_bonus + agree_bonus)


def _tendency_agrees(tendency, direction):
    """True unless skew ACTIVELY contradicts the direction.
    'Flat' (normal structural put skew for Nifty) is not a contradiction.
    """
    if not tendency or "Flat" in tendency:
        return True
    if "Bearish Skew" in tendency and direction == "BEARISH":
        return True
    if "Bullish Skew" in tendency and direction == "BULLISH":
        return True
    return False  # skew contradicts direction


def _rr_agrees(rr_verdict, direction):
    if not rr_verdict:
        return False
    return (
        (rr_verdict.lower() == "bullish" and direction == "BULLISH")
        or (rr_verdict.lower() == "bearish" and direction == "BEARISH")
    )


def _select_buy_strike(chain_df, spot, direction):
    """Select a buy strike with ~0.30 delta, good OI, low premium."""
    from market_data import select_trade_strike, days_to_expiry

    # select_trade_strike already handles direction-based selection
    try:
        expiry = chain_df["Expiry"].iloc[0] if "Expiry" in chain_df.columns else None
        dte = days_to_expiry(expiry) if expiry else 7
    except Exception:
        dte = 7

    result = select_trade_strike(chain_df, spot, direction, dte)
    return result


def evaluate_overnight_trigger(snapshot_data, chain_df, rr_data, spot):
    """Evaluate whether to generate an overnight buy suggestion.
    Called in the 15:15-15:20 window.
    Returns TradeSuggestion dict or None.
    """
    dir_sig = snapshot_data.get("directional_signal", {}) or {}
    direction = dir_sig.get("direction")
    if direction not in ("BULLISH", "BEARISH"):
        return None

    factors = dir_sig.get("factors_agree", 0)
    score = dir_sig.get("score", 0)
    tendency = snapshot_data.get("tendency", "")
    rr_verdict = (rr_data or {}).get("verdict", "")

    if factors < OVERNIGHT_MIN_FACTORS:
        return None
    if abs(score) < OVERNIGHT_MIN_ABS_SCORE:
        return None

    tend_ok = _tendency_agrees(tendency, direction)
    rr_ok = _rr_agrees(rr_verdict, direction)

    # Tendency MUST agree for buy trades (strict filter)
    if not tend_ok:
        return None

    confidence = _compute_confidence(factors, score, tend_ok, rr_ok)
    if confidence < OVERNIGHT_MIN_CONFIDENCE:
        return None

    # Select strike
    strike_data = _select_buy_strike(chain_df, spot, direction)
    if not strike_data:
        return None

    entry_price = strike_data.get("ltp", 0)
    if entry_price <= 0:
        return None

    # SL = 30% of premium, Target = 50% of premium (conservative for overnight)
    sl = round(entry_price * 0.7, 2)
    target = round(entry_price * 1.5, 2)

    rationale_parts = [
        f"{factors}/8 directional factors agree ({direction})",
        f"Score: {score:+.1f}",
        f"Tendency: {tendency} ({'agrees' if tend_ok else 'disagrees'})",
        f"RR: {rr_verdict} ({'agrees' if rr_ok else 'neutral'})",
    ]

    return {
        "suggestion_type": "OVERNIGHT",
        "direction": direction,
        "option_type": strike_data.get("type", "CE").replace("_BUY", "").replace("CE_BUY", "CE").replace("PE_BUY", "PE"),
        "strike": strike_data.get("strike", 0),
        "entry_price": entry_price,
        "stop_loss": sl,
        "target": target,
        "directional_score": score,
        "directional_factors": factors,
        "tendency": tendency,
        "rr_value": (rr_data or {}).get("rr_value"),
        "confidence": confidence,
        "rationale": " | ".join(rationale_parts),
        "nifty_spot_at_creation": spot,
        "expiry_date": snapshot_data.get("expiry_date"),
    }


def evaluate_intraday_trigger(snapshot_data, chain_df, rr_data, spot):
    """Evaluate whether to generate an intraday buy suggestion.
    Called in the 10:00-10:05 window.
    Returns TradeSuggestion dict or None.
    """
    dir_sig = snapshot_data.get("directional_signal", {}) or {}
    direction = dir_sig.get("direction")
    if direction not in ("BULLISH", "BEARISH"):
        return None

    factors = dir_sig.get("factors_agree", 0)
    score = dir_sig.get("score", 0)
    tendency = snapshot_data.get("tendency", "")
    rr_verdict = (rr_data or {}).get("verdict", "")

    if factors < INTRADAY_MIN_FACTORS:
        return None
    if abs(score) < INTRADAY_MIN_ABS_SCORE:
        return None

    tend_ok = _tendency_agrees(tendency, direction)
    rr_ok = _rr_agrees(rr_verdict, direction)

    if not tend_ok:
        return None

    confidence = _compute_confidence(factors, score, tend_ok, rr_ok)
    if confidence < INTRADAY_MIN_CONFIDENCE:
        return None

    strike_data = _select_buy_strike(chain_df, spot, direction)
    if not strike_data:
        return None

    entry_price = strike_data.get("ltp", 0)
    if entry_price <= 0:
        return None

    # Intraday: tighter SL (25%), moderate target (40%)
    sl = round(entry_price * 0.75, 2)
    target = round(entry_price * 1.4, 2)

    rationale_parts = [
        f"{factors}/8 directional factors agree ({direction})",
        f"Score: {score:+.1f}",
        f"Tendency: {tendency} ({'agrees' if tend_ok else 'disagrees'})",
        f"RR: {rr_verdict} ({'agrees' if rr_ok else 'neutral'})",
    ]

    return {
        "suggestion_type": "INTRADAY",
        "direction": direction,
        "option_type": strike_data.get("type", "CE").replace("_BUY", "").replace("CE_BUY", "CE").replace("PE_BUY", "PE"),
        "strike": strike_data.get("strike", 0),
        "entry_price": entry_price,
        "stop_loss": sl,
        "target": target,
        "directional_score": score,
        "directional_factors": factors,
        "tendency": tendency,
        "rr_value": (rr_data or {}).get("rr_value"),
        "confidence": confidence,
        "rationale": " | ".join(rationale_parts),
        "nifty_spot_at_creation": spot,
        "expiry_date": snapshot_data.get("expiry_date"),
    }


def run_sanity_check(db: Session, suggestion, snapshot_data, chain_df):
    """Re-evaluate signal strength. Returns True if still valid."""
    dir_sig = snapshot_data.get("directional_signal", {}) or {}
    direction = dir_sig.get("direction")
    factors = dir_sig.get("factors_agree", 0)

    # Direction must not have flipped
    if direction != suggestion.direction:
        suggestion.status = "REJECTED_AUTO"
        suggestion.resolved_at = datetime.utcnow()
        suggestion.rationale = (suggestion.rationale or "") + " | SANITY FAIL: direction flipped"
        db.commit()
        return False

    # Factors must not drop below minimum
    if factors < SANITY_MIN_FACTORS:
        suggestion.status = "REJECTED_AUTO"
        suggestion.resolved_at = datetime.utcnow()
        suggestion.rationale = (suggestion.rationale or "") + f" | SANITY FAIL: factors dropped to {factors}"
        db.commit()
        return False

    suggestion.sanity_checked_at = datetime.utcnow()
    suggestion.status = "ACTIVE"
    db.commit()
    return True


def update_suggestion_prices(db: Session, chain_df, spot):
    """Update current_price on ACTIVE suggestions. Check SL/target."""
    active = db.query(TradeSuggestion).filter(
        TradeSuggestion.status.in_(["ACTIVE", "PENDING"])
    ).all()

    for s in active:
        opt_type = s.option_type
        match = chain_df[
            (chain_df["Strike"] == s.strike) & (chain_df["Type"] == opt_type)
        ]
        if not match.empty:
            ltp = float(match.iloc[0]["LTP"])
            s.current_price = ltp

            # Check SL hit
            if s.status == "ACTIVE" and s.stop_loss and ltp <= s.stop_loss:
                s.status = "SL_HIT"
                s.resolved_at = datetime.utcnow()

            # Check target hit
            if s.status == "ACTIVE" and s.target and ltp >= s.target:
                s.status = "EXECUTED"
                s.resolved_at = datetime.utcnow()

    db.commit()


def expire_stale_suggestions(db: Session, time_str: str):
    """Expire intraday suggestions past 14:00, overnight past next day 10:00."""
    if time_str >= "14:00":
        stale_intra = db.query(TradeSuggestion).filter(
            TradeSuggestion.suggestion_type == "INTRADAY",
            TradeSuggestion.status.in_(["PENDING", "ACTIVE"]),
        ).all()
        for s in stale_intra:
            s.status = "EXPIRED"
            s.resolved_at = datetime.utcnow()

    if time_str >= "10:00":
        # Expire overnight suggestions from a previous day that are still pending
        today = datetime.utcnow().date()
        stale_overnight = db.query(TradeSuggestion).filter(
            TradeSuggestion.suggestion_type == "OVERNIGHT",
            TradeSuggestion.status.in_(["PENDING", "ACTIVE"]),
        ).all()
        for s in stale_overnight:
            if s.created_at and s.created_at.date() < today:
                s.status = "EXPIRED"
                s.resolved_at = datetime.utcnow()

    db.commit()
