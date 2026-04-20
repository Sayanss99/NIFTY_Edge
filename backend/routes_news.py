"""News Desk API routes — prefix /api/news."""

from datetime import datetime, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session
from sqlalchemy import desc

from database import get_db
from models import NewsItem, NewsDismissal
from routes_auth import get_current_user

router = APIRouter(prefix="/api/news", tags=["news"])


def _item_to_dict(item: NewsItem) -> dict:
    return {
        "id": item.id,
        "headline": item.headline,
        "summary": item.summary,
        "source": item.source,
        "url": item.url,
        "impact_tier": item.impact_tier,
        "raw_impact_score": item.raw_impact_score,
        "direction": item.direction,
        "category": item.category,
        "matched_keywords": item.matched_keywords,
        "direction_word_counts": item.direction_word_counts,
        "scorer_version": item.scorer_version,
        "published_at": item.published_at.isoformat() if item.published_at else None,
        "fetched_at": item.fetched_at.isoformat() if item.fetched_at else None,
    }


@router.get("/feed")
def get_feed(
    tier: Optional[str] = None,
    direction: Optional[str] = None,
    category: Optional[str] = None,
    source: Optional[str] = None,
    hours: int = Query(default=24, ge=1, le=168),
    page: int = Query(default=1, ge=1),
    per_page: int = Query(default=20, ge=1, le=100),
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    cutoff = datetime.utcnow() - timedelta(hours=hours)
    q = db.query(NewsItem).filter(NewsItem.fetched_at >= cutoff)

    if tier:
        q = q.filter(NewsItem.impact_tier == tier.upper())
    if direction:
        q = q.filter(NewsItem.direction == direction.upper())
    if category:
        q = q.filter(NewsItem.category == category.upper())
    if source:
        q = q.filter(NewsItem.source == source)

    total = q.count()
    items = (
        q.order_by(desc(NewsItem.fetched_at))
        .offset((page - 1) * per_page)
        .limit(per_page)
        .all()
    )

    return {
        "items": [_item_to_dict(i) for i in items],
        "total": total,
        "page": page,
        "pages": max(1, (total + per_page - 1) // per_page),
    }


@router.get("/ticker")
def get_ticker(
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    cutoff = datetime.utcnow() - timedelta(hours=4)

    dismissed_subquery = (
        db.query(NewsDismissal.news_item_id)
        .filter(NewsDismissal.user_id == current_user.id)
        .subquery()
    )

    items = (
        db.query(NewsItem)
        .filter(
            NewsItem.fetched_at >= cutoff,
            NewsItem.impact_tier.in_(["HIGH", "CRITICAL"]),
            ~NewsItem.id.in_(dismissed_subquery),
        )
        .order_by(desc(NewsItem.fetched_at))
        .limit(20)
        .all()
    )

    return {"items": [_item_to_dict(i) for i in items]}


@router.get("/recent-context")
def get_recent_context(
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    cutoff = datetime.utcnow() - timedelta(hours=2)
    items = db.query(NewsItem).filter(NewsItem.fetched_at >= cutoff).all()

    counts = {"CRITICAL": 0, "HIGH": 0, "MEDIUM": 0}
    dir_counts = {"BULLISH": 0, "BEARISH": 0, "NEUTRAL": 0}

    for item in items:
        if item.impact_tier in counts:
            counts[item.impact_tier] += 1
        if item.direction in dir_counts:
            dir_counts[item.direction] += 1

    return {
        "critical_count": counts["CRITICAL"],
        "high_count": counts["HIGH"],
        "medium_count": counts["MEDIUM"],
        "bullish_count": dir_counts["BULLISH"],
        "bearish_count": dir_counts["BEARISH"],
        "neutral_count": dir_counts["NEUTRAL"],
        "total": len(items),
    }


@router.post("/{item_id}/dismiss")
def dismiss_item(
    item_id: int,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    item = db.query(NewsItem).filter(NewsItem.id == item_id).first()
    if not item:
        raise HTTPException(status_code=404, detail="News item not found")

    existing = db.query(NewsDismissal).filter(
        NewsDismissal.user_id == current_user.id,
        NewsDismissal.news_item_id == item_id,
    ).first()
    if not existing:
        db.add(NewsDismissal(
            user_id=current_user.id,
            news_item_id=item_id,
            dismissed_at=datetime.utcnow(),
        ))
        db.commit()

    return {"ok": True}


@router.get("/stats")
def get_stats(
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    cutoff_24h = datetime.utcnow() - timedelta(hours=24)
    cutoff_4h = datetime.utcnow() - timedelta(hours=4)

    items_24h = db.query(NewsItem).filter(NewsItem.fetched_at >= cutoff_24h).all()
    items_4h = [i for i in items_24h if i.fetched_at >= cutoff_4h]

    by_tier = {"CRITICAL": 0, "HIGH": 0, "MEDIUM": 0, "LOW": 0}
    for item in items_24h:
        if item.impact_tier in by_tier:
            by_tier[item.impact_tier] += 1

    dir_counts = {"BULLISH": 0, "BEARISH": 0, "NEUTRAL": 0}
    for item in items_4h:
        if item.direction in dir_counts:
            dir_counts[item.direction] += 1

    if dir_counts["BULLISH"] > dir_counts["BEARISH"]:
        tone = "BULLISH"
    elif dir_counts["BEARISH"] > dir_counts["BULLISH"]:
        tone = "BEARISH"
    else:
        tone = "NEUTRAL"

    return {
        "by_tier": by_tier,
        "market_tone": tone,
        "tone_counts": dir_counts,
        "total_24h": len(items_24h),
    }
