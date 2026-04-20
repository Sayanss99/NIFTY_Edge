"""Trade Suggestion API routes — auto-generated buy signals."""

from datetime import datetime
from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session
from database import get_db
from models import TradeSuggestion
from auth import get_current_user

router = APIRouter(prefix="/api/suggestions", tags=["suggestions"])


@router.get("/")
def list_suggestions(
    status: str = None,
    db: Session = Depends(get_db),
    _=Depends(get_current_user),
):
    """List trade suggestions, optionally filtered by status."""
    query = db.query(TradeSuggestion).order_by(TradeSuggestion.created_at.desc())
    if status:
        query = query.filter(TradeSuggestion.status == status)
    else:
        # Default: show active and pending
        query = query.filter(TradeSuggestion.status.in_(["ACTIVE", "PENDING"]))

    suggestions = query.limit(20).all()
    return {
        "suggestions": [
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
                "expiry_date": s.expiry_date,
                "nifty_spot": s.nifty_spot_at_creation,
                "created_at": s.created_at.isoformat() if s.created_at else None,
                "sanity_checked_at": s.sanity_checked_at.isoformat() if s.sanity_checked_at else None,
            }
            for s in suggestions
        ]
    }


@router.get("/history")
def suggestion_history(
    limit: int = 50,
    db: Session = Depends(get_db),
    _=Depends(get_current_user),
):
    """Get past suggestions with outcomes."""
    suggestions = (
        db.query(TradeSuggestion)
        .filter(TradeSuggestion.status.notin_(["ACTIVE", "PENDING"]))
        .order_by(TradeSuggestion.created_at.desc())
        .limit(limit)
        .all()
    )
    return {
        "suggestions": [
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
                "expiry_date": s.expiry_date,
                "created_at": s.created_at.isoformat() if s.created_at else None,
                "resolved_at": s.resolved_at.isoformat() if s.resolved_at else None,
            }
            for s in suggestions
        ]
    }


@router.post("/{suggestion_id}/reject")
def reject_suggestion(
    suggestion_id: int,
    db: Session = Depends(get_db),
    _=Depends(get_current_user),
):
    """User rejects a suggestion."""
    s = db.query(TradeSuggestion).filter(TradeSuggestion.id == suggestion_id).first()
    if not s:
        return {"error": "Not found"}
    s.status = "REJECTED_USER"
    s.resolved_at = datetime.utcnow()
    db.commit()
    return {"status": "rejected"}


@router.post("/{suggestion_id}/execute")
def execute_suggestion(
    suggestion_id: int,
    db: Session = Depends(get_db),
    _=Depends(get_current_user),
):
    """User marks suggestion as executed (took the trade)."""
    s = db.query(TradeSuggestion).filter(TradeSuggestion.id == suggestion_id).first()
    if not s:
        return {"error": "Not found"}
    s.status = "EXECUTED"
    s.resolved_at = datetime.utcnow()
    db.commit()
    return {"status": "executed"}
