"""Off-hours snapshots — freeze the last close's screener picks & positions P&L
and serve them while the market is shut, so the app stops live-fetching after the
session ends and shows the 3:30 PM close until it reopens.

Design (kept entirely additive — no existing logic changes):
  • During market hours everything runs live exactly as before; the live results
    are also persisted as a rolling snapshot.
  • A snapshot is "close-final" once its session has ended (≥ 15:30 IST). When the
    market is closed the endpoints serve the close-final snapshot and skip fetching.
  • Snapshots are stamped with the session date, so a new day's close overwrites the
    previous one (effectively auto-deleting it).

Storage is a small JSON file per kind (and per user for positions) under
``backend/_snapshots/`` — no DB schema change.
"""

import os
import json
import threading
from datetime import datetime, timedelta

# Single source of truth for market hours / holidays (no circular import:
# scheduler imports only database/models/config, never the routes or this module).
from scheduler import IST, NSE_HOLIDAYS_2026, is_market_open

_DIR = os.path.join(os.path.dirname(__file__), "_snapshots")
_lock = threading.Lock()

_SESSION_CLOSE = "15:30"   # NSE close; data is frozen at/after this
_SESSION_OPEN = "09:00"


def _is_trading_day(d) -> bool:
    return d.weekday() < 5 and d.isoformat() not in NSE_HOLIDAYS_2026


def data_session_date(now_ist: datetime | None = None) -> str:
    """ISO date of the session the currently-available market data belongs to.

    Today once the session has started; otherwise the most recent completed
    trading day (covers pre-market, evenings, weekends, holidays)."""
    now = now_ist or datetime.now(IST)
    d = now.date()
    if _is_trading_day(d) and now.strftime("%H:%M") >= _SESSION_OPEN:
        return d.isoformat()
    d -= timedelta(days=1)
    while not _is_trading_day(d):
        d -= timedelta(days=1)
    return d.isoformat()


def is_final(now_ist: datetime | None = None) -> bool:
    """Is the data for ``data_session_date`` from a COMPLETED session (≥ 15:30)?
    Mid-session (today, before close) → False; everything else → True."""
    now = now_ist or datetime.now(IST)
    d = now.date()
    if _is_trading_day(d) and now.strftime("%H:%M") >= _SESSION_OPEN:
        return now.strftime("%H:%M") >= _SESSION_CLOSE   # today's session done?
    return True   # data belongs to a prior, already-completed session


def market_closed() -> bool:
    try:
        return not is_market_open().get("open", True)
    except Exception:
        return False


def _path(kind: str, user_id=None) -> str:
    name = kind if user_id is None else f"{kind}_{user_id}"
    return os.path.join(_DIR, f"{name}.json")


def save_snapshot(kind: str, payload, user_id=None) -> None:
    """Persist the latest live result as a snapshot, stamped with its session date
    and whether that session is close-final."""
    try:
        os.makedirs(_DIR, exist_ok=True)
        record = {
            "session_date": data_session_date(),
            "captured_at": datetime.now(IST).isoformat(),
            "final": is_final(),
            "payload": payload,
        }
        with _lock:
            with open(_path(kind, user_id), "w", encoding="utf-8") as f:
                json.dump(record, f, default=str)
    except Exception as e:
        print(f"[Snapshot] save {kind} failed: {e}")


def load_snapshot(kind: str, user_id=None):
    try:
        with open(_path(kind, user_id), encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def close_snapshot(kind: str, user_id=None):
    """The snapshot iff it is the close-final snapshot for the latest session —
    i.e. exactly "as of 3:30 PM today/last session". Otherwise None."""
    snap = load_snapshot(kind, user_id)
    if (snap and snap.get("final") and snap.get("payload") is not None
            and snap.get("session_date") == data_session_date()):
        return snap
    return None
