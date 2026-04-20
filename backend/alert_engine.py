"""Alert evaluation engine — checks armed rules against current market data
and triggers alerts when conditions are met.

Called by the scheduler on each refresh cycle.
"""

from datetime import datetime, timedelta
from sqlalchemy.orm import Session
from models import AlertRule, AlertLog, AlertStatus, Trade, TradeStatus


def evaluate_alerts(
    db: Session,
    snapshot: dict,
    chain_df=None,
    risk_data: dict | None = None,
) -> list[dict]:
    """Evaluate all armed alert rules against current data.

    Returns list of triggered alert dicts for WebSocket notification.
    """
    armed = db.query(AlertRule).filter(
        AlertRule.status == AlertStatus.ARMED.value,
    ).all()

    if not armed:
        return []

    triggered = []

    for rule in armed:
        # Cooldown check
        if rule.last_triggered_at:
            cooldown = timedelta(minutes=rule.cooldown_minutes or 15)
            if datetime.utcnow() - rule.last_triggered_at < cooldown:
                continue

        current_value = _get_metric_value(db, rule, snapshot, chain_df, risk_data)
        if current_value is None:
            continue

        if _check_condition(rule.condition, current_value, rule.threshold):
            # Trigger!
            msg = _build_message(rule, current_value)

            log = AlertLog(
                alert_rule_id=rule.id,
                triggered_value=round(current_value, 2),
                message=msg,
            )
            db.add(log)
            rule.last_triggered_at = datetime.utcnow()

            triggered.append({
                "alert_id": rule.id,
                "user_id": rule.user_id,
                "metric": rule.metric,
                "condition": rule.condition,
                "threshold": rule.threshold,
                "current_value": round(current_value, 2),
                "message": msg,
                "trade_id": rule.trade_id,
            })

    if triggered:
        db.commit()

    return triggered


def _get_metric_value(db, rule, snapshot, chain_df, risk_data):
    """Extract the current value of the metric from market data."""
    metric = rule.metric

    # Market-level metrics
    if metric == "nifty_spot":
        return snapshot.get("nifty_spot")
    if metric == "india_vix":
        return snapshot.get("india_vix")

    # Risk scores (from risk_data if provided)
    if metric == "ce_risk" and risk_data:
        ce = risk_data.get("ce_risk")
        return ce.get("score") if isinstance(ce, dict) else ce
    if metric == "pe_risk" and risk_data:
        pe = risk_data.get("pe_risk")
        return pe.get("score") if isinstance(pe, dict) else pe

    # Range breach: check if spot is outside calculated range
    if metric == "range_breach":
        spot = snapshot.get("nifty_spot", 0)
        top = snapshot.get("calc_range_top", 0)
        bottom = snapshot.get("calc_range_bottom", 0)
        if top and bottom:
            # Return how far outside range (positive = outside, negative = inside)
            if spot > top:
                return spot - top
            if spot < bottom:
                return bottom - spot
            return -(min(spot - bottom, top - spot))  # negative = inside range
        return None

    # Position-specific metrics
    if metric.startswith("position_") and rule.trade_id and chain_df is not None:
        trade = db.query(Trade).filter(
            Trade.id == rule.trade_id,
            Trade.status == TradeStatus.OPEN.value,
        ).first()
        if not trade:
            return None

        opt_type = "CE" if "CE" in trade.trade_type else "PE"
        match = chain_df[
            (chain_df["Strike"] == trade.strike) & (chain_df["Type"] == opt_type)
        ]
        if match.empty:
            return None

        row = match.iloc[0]

        if metric == "position_delta":
            return abs(float(row["delta"]))
        if metric == "position_iv":
            return float(row["IV"])
        if metric == "position_premium":
            # Return multiplier: how many times entry premium
            current = float(row["LTP"])
            if trade.entry_premium > 0:
                return current / trade.entry_premium
            return None

    return None


def _check_condition(condition: str, value: float, threshold: float) -> bool:
    """Check if condition is met."""
    if condition == "gt":
        return value > threshold
    if condition == "lt":
        return value < threshold
    if condition == "crosses_above":
        return value > threshold  # simplified — would need previous value for true crossing
    if condition == "crosses_below":
        return value < threshold
    return False


def _build_message(rule, value):
    """Build human-readable alert message."""
    cond_text = {
        "gt": "exceeded",
        "lt": "dropped below",
        "crosses_above": "crossed above",
        "crosses_below": "crossed below",
    }.get(rule.condition, rule.condition)

    metric_labels = {
        "nifty_spot": "Nifty Spot",
        "india_vix": "India VIX",
        "ce_risk": "CE Risk Score",
        "pe_risk": "PE Risk Score",
        "position_delta": "Position Delta",
        "position_iv": "Position IV",
        "position_premium": "Premium Multiplier",
        "range_breach": "Range Breach",
    }
    label = metric_labels.get(rule.metric, rule.metric)

    msg = f"{label} {cond_text} {rule.threshold}"
    if rule.trade_id and rule.trade:
        msg += f" ({rule.trade.trade_type} {rule.trade.strike})"
    msg += f" — current: {round(value, 2)}"
    return msg