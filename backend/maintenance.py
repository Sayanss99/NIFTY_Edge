"""Daily DB maintenance — retention pruning, weekly VACUUM, and backups.

Runs from the scheduler loop once per day. Only deletes rows older than the
retention windows below; live data flow is untouched. Also copies the SQLite
file into _backups/ with a 7-file rotation.
"""

import logging
import os
import shutil
from datetime import datetime, timedelta

from sqlalchemy import text
from database import SessionLocal, DATABASE_PATH
from models import MarketSnapshot, SRLevel, NewsItem, AlertLog

log = logging.getLogger("niftyedge.maintenance")

# Retention windows (days)
RETAIN_SNAPSHOTS_DAYS = 30
RETAIN_SR_LEVELS_DAYS = 14
RETAIN_NEWS_DAYS = 30
RETAIN_ALERT_LOGS_DAYS = 90

BACKUP_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_backups")
BACKUP_KEEP = 7

# Module state: last run date (so the scheduler can call this every cycle
# cheaply — it no-ops until the day changes).
_last_run_date = None


def run_daily_maintenance(force: bool = False) -> dict | None:
    """Prune old rows, back up the DB, VACUUM weekly. No-op if already run today."""
    global _last_run_date
    today = datetime.utcnow().date()
    if not force and _last_run_date == today:
        return None
    _last_run_date = today

    result = {"pruned": {}, "backup": None, "vacuum": False}
    now = datetime.utcnow()
    db = SessionLocal()
    try:
        prunes = [
            ("market_snapshots", MarketSnapshot, MarketSnapshot.timestamp, RETAIN_SNAPSHOTS_DAYS),
            ("sr_levels", SRLevel, SRLevel.timestamp, RETAIN_SR_LEVELS_DAYS),
            ("news_items", NewsItem, NewsItem.fetched_at, RETAIN_NEWS_DAYS),
            ("alert_logs", AlertLog, AlertLog.triggered_at, RETAIN_ALERT_LOGS_DAYS),
        ]
        for name, model, col, days in prunes:
            cutoff = now - timedelta(days=days)
            try:
                n = db.query(model).filter(col < cutoff).delete(synchronize_session=False)
                db.commit()
                result["pruned"][name] = n
                if n:
                    log.info("Pruned %d rows from %s (older than %dd)", n, name, days)
            except Exception:
                db.rollback()
                log.exception("Prune failed for %s", name)

        # Weekly VACUUM (Sundays) reclaims space from the prunes above
        if today.weekday() == 6 or force:
            try:
                db.execute(text("VACUUM"))
                db.commit()
                result["vacuum"] = True
                log.info("VACUUM completed")
            except Exception:
                db.rollback()
                log.exception("VACUUM failed")
    finally:
        db.close()

    # Daily backup with rotation
    try:
        os.makedirs(BACKUP_DIR, exist_ok=True)
        dest = os.path.join(BACKUP_DIR, f"niftyedge-{today.isoformat()}.db")
        if not os.path.exists(dest):
            shutil.copy2(DATABASE_PATH, dest)
            log.info("DB backed up to %s", dest)
        result["backup"] = dest
        backups = sorted(
            f for f in os.listdir(BACKUP_DIR)
            if f.startswith("niftyedge-") and f.endswith(".db")
        )
        for old in backups[:-BACKUP_KEEP]:
            os.remove(os.path.join(BACKUP_DIR, old))
    except Exception:
        log.exception("DB backup failed")

    return result
