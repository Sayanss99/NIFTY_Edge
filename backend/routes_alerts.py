"""Alert management API routes — CRUD for alert rules + history."""

from datetime import datetime
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session
from pydantic import BaseModel
from database import get_db
from models import AlertRule, AlertLog, AlertStatus, User, Trade, TradeStatus
from auth import get_current_user

router = APIRouter(prefix="/api/alerts", tags=["alerts"])

VALID_METRICS = [
    "nifty_spot", "india_vix", "ce_risk", "pe_risk",
    "position_delta", "position_iv", "position_premium", "range_breach",
]

VALID_CONDITIONS = ["gt", "lt", "crosses_above", "crosses_below"]


class CreateAlertRequest(BaseModel):
    metric: str
    condition: str
    threshold: float
    trade_id: int | None = None
    notification_method: str = "in_app"
    cooldown_minutes: int = 15


class UpdateAlertRequest(BaseModel):
    metric: str | None = None
    condition: str | None = None
    threshold: float | None = None
    status: str | None = None
    cooldown_minutes: int | None = None


@router.get("/")
def list_alerts(
    status: str = Query(None),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """List all alert rules for the user."""
    query = db.query(AlertRule).filter(AlertRule.user_id == user.id)
    if status:
        query = query.filter(AlertRule.status == status)

    rules = query.order_by(AlertRule.created_at.desc()).all()

    return {"alerts": [_rule_dict(r) for r in rules]}


@router.post("/")
def create_alert(
    req: CreateAlertRequest,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Create a new alert rule."""
    if req.metric not in VALID_METRICS:
        raise HTTPException(400, f"Invalid metric. Must be one of {VALID_METRICS}")
    if req.condition not in VALID_CONDITIONS:
        raise HTTPException(400, f"Invalid condition. Must be one of {VALID_CONDITIONS}")

    # Position-specific alerts require a valid open trade
    if req.metric.startswith("position_"):
        if not req.trade_id:
            raise HTTPException(400, "Position alerts require a trade_id")
        trade = db.query(Trade).filter(
            Trade.id == req.trade_id,
            Trade.user_id == user.id,
            Trade.status == TradeStatus.OPEN.value,
        ).first()
        if not trade:
            raise HTTPException(404, "Trade not found or already closed")

    rule = AlertRule(
        user_id=user.id,
        metric=req.metric,
        condition=req.condition,
        threshold=req.threshold,
        trade_id=req.trade_id,
        notification_method=req.notification_method,
        cooldown_minutes=req.cooldown_minutes,
    )
    db.add(rule)
    db.commit()
    db.refresh(rule)

    return {"id": rule.id, "message": "Alert created", "alert": _rule_dict(rule)}


@router.put("/{alert_id}")
def update_alert(
    alert_id: int,
    req: UpdateAlertRequest,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Update an alert rule (threshold, status, etc.)."""
    rule = db.query(AlertRule).filter(
        AlertRule.id == alert_id,
        AlertRule.user_id == user.id,
    ).first()
    if not rule:
        raise HTTPException(404, "Alert not found")

    if req.metric and req.metric in VALID_METRICS:
        rule.metric = req.metric
    if req.condition and req.condition in VALID_CONDITIONS:
        rule.condition = req.condition
    if req.threshold is not None:
        rule.threshold = req.threshold
    if req.status and req.status in [s.value for s in AlertStatus]:
        rule.status = req.status
    if req.cooldown_minutes is not None:
        rule.cooldown_minutes = req.cooldown_minutes

    db.commit()
    return {"message": "Alert updated"}


@router.delete("/{alert_id}")
def delete_alert(
    alert_id: int,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Delete an alert rule and its logs."""
    rule = db.query(AlertRule).filter(
        AlertRule.id == alert_id,
        AlertRule.user_id == user.id,
    ).first()
    if not rule:
        raise HTTPException(404, "Alert not found")

    db.query(AlertLog).filter(AlertLog.alert_rule_id == alert_id).delete()
    db.delete(rule)
    db.commit()
    return {"message": "Alert deleted"}


@router.post("/{alert_id}/toggle")
def toggle_alert(
    alert_id: int,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Toggle alert between ARMED and PAUSED."""
    rule = db.query(AlertRule).filter(
        AlertRule.id == alert_id,
        AlertRule.user_id == user.id,
    ).first()
    if not rule:
        raise HTTPException(404, "Alert not found")

    if rule.status == AlertStatus.ARMED.value:
        rule.status = AlertStatus.PAUSED.value
    elif rule.status in (AlertStatus.PAUSED.value, AlertStatus.TRIGGERED.value):
        rule.status = AlertStatus.ARMED.value

    db.commit()
    return {"message": f"Alert {rule.status.lower()}", "status": rule.status}


@router.get("/history")
def alert_history(
    limit: int = Query(50, le=200),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Get recent alert trigger history."""
    logs = db.query(AlertLog).join(AlertRule).filter(
        AlertRule.user_id == user.id,
    ).order_by(AlertLog.triggered_at.desc()).limit(limit).all()

    result = []
    for log in logs:
        rule = log.rule
        result.append({
            "id": log.id,
            "alert_id": rule.id,
            "metric": rule.metric,
            "condition": rule.condition,
            "threshold": rule.threshold,
            "triggered_at": log.triggered_at.isoformat() if log.triggered_at else None,
            "triggered_value": log.triggered_value,
            "message": log.message,
        })

    return {"history": result}


@router.get("/unread-count")
def unread_count(
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Count recent unread alerts (triggered in last 24h)."""
    from datetime import timedelta
    cutoff = datetime.utcnow() - timedelta(hours=24)
    count = db.query(AlertLog).join(AlertRule).filter(
        AlertRule.user_id == user.id,
        AlertLog.triggered_at >= cutoff,
    ).count()
    return {"count": count}


def _rule_dict(r):
    return {
        "id": r.id,
        "metric": r.metric,
        "condition": r.condition,
        "threshold": r.threshold,
        "trade_id": r.trade_id,
        "notification_method": r.notification_method,
        "status": r.status,
        "cooldown_minutes": r.cooldown_minutes,
        "last_triggered_at": r.last_triggered_at.isoformat() if r.last_triggered_at else None,
        "created_at": r.created_at.isoformat() if r.created_at else None,
        "trade_info": {
            "trade_type": r.trade.trade_type,
            "strike": r.trade.strike,
            "expiry_date": r.trade.expiry_date,
        } if r.trade else None,
        "recent_triggers": len(r.logs) if r.logs else 0,
    }
