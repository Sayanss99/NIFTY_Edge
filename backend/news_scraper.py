"""RSS news scraper — fetch, parse, deduplicate, score, persist.

All functions are synchronous (blocking I/O).
Call fetch_and_store_news via asyncio run_in_executor from the scheduler.
"""

import re
import hashlib
import calendar
from datetime import datetime
from typing import Optional

from sqlalchemy.orm import Session
from sqlalchemy.exc import IntegrityError

from news_sources import NEWS_SOURCES
from news_scorer import score_news_item
from models import NewsItem


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _parse_published(entry) -> Optional[datetime]:
    """Parse feedparser published_parsed (struct_time) to naive UTC datetime."""
    pp = getattr(entry, "published_parsed", None)
    if pp:
        try:
            return datetime.utcfromtimestamp(calendar.timegm(pp))
        except Exception:
            pass
    return None


def _strip_html(text: str) -> str:
    """Remove HTML tags and collapse whitespace."""
    clean = re.sub(r"<[^>]+>", " ", text)
    return " ".join(clean.split())


def _entry_to_raw(entry, source_name: str) -> Optional[dict]:
    """Extract fields from a feedparser entry. Returns None if headline missing."""
    headline = getattr(entry, "title", None)
    if not headline:
        return None
    headline = headline.strip()

    url = (getattr(entry, "link", None) or getattr(entry, "id", None) or "").strip()

    summary_raw = (
        getattr(entry, "summary", None)
        or getattr(entry, "description", None)
        or ""
    )
    summary = _strip_html(summary_raw)[:500] or None

    url_key = url if url else headline
    return {
        "url": url or f"no-url-{_hash(headline)[:16]}",
        "url_hash": _hash(url_key),
        "headline_hash": _hash(headline.lower()),
        "source": source_name,
        "headline": headline,
        "summary": summary,
        "published_at": _parse_published(entry),
    }


def fetch_and_store_news(db: Session) -> tuple[dict, list]:
    """Fetch all enabled RSS sources, score, dedup against DB, and persist.

    Returns:
        (stats_dict, new_items_list)
        stats_dict keys: fetched, new_stored, skipped_dup, skipped_low_score, errors
        new_items_list: list of persisted NewsItem ORM objects (with IDs assigned)
    """
    try:
        import feedparser
    except ImportError:
        print("[NewsScraper] feedparser not installed — run: pip install feedparser")
        return {"fetched": 0, "new_stored": 0, "skipped_dup": 0, "skipped_low_score": 0, "errors": 1}, []

    stats = {"fetched": 0, "new_stored": 0, "skipped_dup": 0, "skipped_low_score": 0, "errors": 0}
    new_items: list[NewsItem] = []

    for source in NEWS_SOURCES:
        if not source.get("enabled", True):
            continue

        try:
            feed = feedparser.parse(source["url"])
            if feed.bozo:
                print(f"[NewsScraper] Bozo feed from {source['name']}: {feed.bozo_exception}")
            entries = feed.entries[:30]
        except Exception as e:
            print(f"[NewsScraper] Failed to fetch {source['name']}: {e}")
            stats["errors"] += 1
            continue

        for entry in entries:
            raw = _entry_to_raw(entry, source["name"])
            if not raw:
                continue
            stats["fetched"] += 1

            # Dedup: check url_hash and headline_hash
            existing = (
                db.query(NewsItem)
                .filter(
                    (NewsItem.url_hash == raw["url_hash"])
                    | (NewsItem.headline_hash == raw["headline_hash"])
                )
                .first()
            )
            if existing:
                stats["skipped_dup"] += 1
                continue

            score = score_news_item(
                headline=raw["headline"],
                summary=raw["summary"],
                source_weight=source.get("weight", 1.0),
            )
            if not score.should_store:
                stats["skipped_low_score"] += 1
                continue

            item = NewsItem(
                url=raw["url"],
                url_hash=raw["url_hash"],
                headline_hash=raw["headline_hash"],
                source=raw["source"],
                headline=raw["headline"],
                summary=raw["summary"],
                published_at=raw["published_at"],
                fetched_at=datetime.utcnow(),
                impact_tier=score.impact_tier,
                raw_impact_score=score.raw_impact_score,
                direction=score.direction,
                category=score.category,
                matched_keywords=score.matched_keywords,
                direction_word_counts=score.direction_word_counts,
                scorer_version=score.scorer_version,
                is_pushed_to_ticker=False,
            )
            try:
                db.add(item)
                db.flush()  # get ID without full commit, catch IntegrityError early
                new_items.append(item)
            except IntegrityError:
                db.rollback()
                stats["skipped_dup"] += 1

    if new_items:
        try:
            db.commit()
            for item in new_items:
                db.refresh(item)
            stats["new_stored"] = len(new_items)
        except Exception as e:
            db.rollback()
            print(f"[NewsScraper] Commit error: {e}")
            new_items = []

    # Fallback: try gnews if RSS yielded nothing at all
    if stats["fetched"] == 0:
        try:
            from gnews import GNews
            gn = GNews(language="en", country="IN", max_results=15)
            results = gn.get_news("NIFTY OR Sensex OR RBI OR NSE India")
            for r in (results or []):
                headline = r.get("title", "").strip()
                url = r.get("url", "").strip()
                if not headline:
                    continue
                stats["fetched"] += 1
                url_key = url if url else headline
                url_hash = _hash(url_key)
                hl_hash = _hash(headline.lower())
                existing = db.query(NewsItem).filter(
                    (NewsItem.url_hash == url_hash) | (NewsItem.headline_hash == hl_hash)
                ).first()
                if existing:
                    stats["skipped_dup"] += 1
                    continue
                score = score_news_item(headline=headline, summary=None, source_weight=0.9)
                if not score.should_store:
                    stats["skipped_low_score"] += 1
                    continue
                item = NewsItem(
                    url=url or f"no-url-{url_hash[:16]}",
                    url_hash=url_hash, headline_hash=hl_hash,
                    source="GNews", headline=headline, summary=None,
                    published_at=None, fetched_at=datetime.utcnow(),
                    impact_tier=score.impact_tier, raw_impact_score=score.raw_impact_score,
                    direction=score.direction, category=score.category,
                    matched_keywords=score.matched_keywords,
                    direction_word_counts=score.direction_word_counts,
                    scorer_version=score.scorer_version, is_pushed_to_ticker=False,
                )
                try:
                    db.add(item)
                    db.flush()
                    new_items.append(item)
                except IntegrityError:
                    db.rollback()
                    stats["skipped_dup"] += 1
            if new_items:
                db.commit()
                for item in new_items:
                    db.refresh(item)
                stats["new_stored"] = len(new_items)
        except Exception as e:
            print(f"[NewsScraper] GNews fallback error: {e}")

    return stats, new_items
