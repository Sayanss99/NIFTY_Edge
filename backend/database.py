"""Database setup — SQLite + SQLAlchemy async-compatible engine."""

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, DeclarativeBase

DATABASE_URL = "sqlite:///./niftyedge.db"

engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False})
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)


class Base(DeclarativeBase):
    pass


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db():
    Base.metadata.create_all(bind=engine)
    _migrate_db()


def _migrate_db():
    """Add new columns/tables to existing DB without Alembic."""
    import sqlite3
    conn = sqlite3.connect(DATABASE_URL.replace("sqlite:///", ""))
    c = conn.cursor()

    # Ensure market_snapshots has new columns
    c.execute("PRAGMA table_info(market_snapshots)")
    cols = {r[1] for r in c.fetchall()}
    for col, typ in [
        ("max_pain", "REAL"),
        ("directional_direction", "TEXT"),
        ("directional_score", "REAL"),
        ("directional_factors_agree", "INTEGER"),
        ("expiries_json", "TEXT"),
    ]:
        if col not in cols:
            c.execute(f"ALTER TABLE market_snapshots ADD COLUMN {col} {typ}")

    # Ensure trades has strategy_id column
    c.execute("PRAGMA table_info(trades)")
    trade_cols = {r[1] for r in c.fetchall()}
    if "strategy_id" not in trade_cols:
        c.execute("ALTER TABLE trades ADD COLUMN strategy_id INTEGER REFERENCES strategies(id)")

    # Create strategies table if not exists
    c.execute("""
        CREATE TABLE IF NOT EXISTS strategies (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL REFERENCES users(id),
            name TEXT NOT NULL,
            strategy_type TEXT NOT NULL,
            expiry_date TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'ACTIVE',
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # Create iv_daily_records table if not exists
    c.execute("""
        CREATE TABLE IF NOT EXISTS iv_daily_records (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date DATE NOT NULL UNIQUE,
            atm_iv REAL NOT NULL,
            india_vix REAL,
            nifty_close REAL,
            atm_strike INTEGER,
            expiry_date TEXT,
            is_proxy INTEGER NOT NULL DEFAULT 1
        )
    """)
    c.execute("CREATE INDEX IF NOT EXISTS ix_iv_daily_records_date ON iv_daily_records(date)")

    # Add new columns to iv_daily_records if they don't exist
    c.execute("PRAGMA table_info(iv_daily_records)")
    iv_cols = {r[1] for r in c.fetchall()}
    for col, typ in [
        ("atm_iv_ce", "REAL"),
        ("atm_iv_pe", "REAL"),
        ("atm_straddle_price", "REAL"),
    ]:
        if col not in iv_cols:
            c.execute(f"ALTER TABLE iv_daily_records ADD COLUMN {col} {typ}")

    # Create iv_term_structure table
    c.execute("""
        CREATE TABLE IF NOT EXISTS iv_term_structure (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date DATE NOT NULL,
            expiry TEXT NOT NULL,
            dte INTEGER,
            atm_iv REAL,
            atm_straddle REAL
        )
    """)
    c.execute("CREATE INDEX IF NOT EXISTS ix_iv_ts_date ON iv_term_structure(date)")

    # Create iv_skew_daily table
    c.execute("""
        CREATE TABLE IF NOT EXISTS iv_skew_daily (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date DATE NOT NULL UNIQUE,
            delta25_call_iv REAL,
            delta25_put_iv REAL,
            risk_reversal REAL,
            butterfly REAL,
            atm_iv REAL
        )
    """)
    c.execute("CREATE INDEX IF NOT EXISTS ix_iv_skew_date ON iv_skew_daily(date)")

    # Create news_items table
    c.execute("""
        CREATE TABLE IF NOT EXISTS news_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            url TEXT NOT NULL UNIQUE,
            url_hash TEXT NOT NULL,
            headline_hash TEXT NOT NULL,
            source TEXT NOT NULL,
            headline TEXT NOT NULL,
            summary TEXT,
            published_at DATETIME,
            fetched_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            impact_tier TEXT NOT NULL,
            raw_impact_score REAL NOT NULL,
            direction TEXT NOT NULL,
            category TEXT NOT NULL,
            matched_keywords TEXT,
            direction_word_counts TEXT,
            scorer_version TEXT NOT NULL DEFAULT 'rules-v1',
            is_pushed_to_ticker INTEGER NOT NULL DEFAULT 0
        )
    """)
    c.execute("CREATE INDEX IF NOT EXISTS ix_news_url_hash ON news_items(url_hash)")
    c.execute("CREATE INDEX IF NOT EXISTS ix_news_headline_hash ON news_items(headline_hash)")
    c.execute("CREATE INDEX IF NOT EXISTS ix_news_fetched_at ON news_items(fetched_at)")
    c.execute("CREATE INDEX IF NOT EXISTS ix_news_impact_tier ON news_items(impact_tier)")

    # Create news_dismissals table
    c.execute("""
        CREATE TABLE IF NOT EXISTS news_dismissals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL REFERENCES users(id),
            news_item_id INTEGER NOT NULL REFERENCES news_items(id),
            dismissed_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
    """)
    c.execute("CREATE UNIQUE INDEX IF NOT EXISTS ix_news_dismissals_unique ON news_dismissals(user_id, news_item_id)")

    conn.commit()

    # Bootstrap IV history from MarketSnapshot (VIX × 1.08 proxy) + synthetic history
    _bootstrap_iv_history(conn)

    conn.close()


def _bootstrap_iv_history(conn):
    """Seed IVDailyRecord from MarketSnapshot (proxy) + synthetic history.

    Synthetic data uses an Ornstein-Uhlenbeck process calibrated to India VIX
    (mean ~14.5%, vol ~1%/day, mean-reversion κ=0.05). Generates 252 trading
    days of history so IV Rank is immediately meaningful. Only runs once — skips
    dates already present.
    """
    import random
    from datetime import datetime, timedelta, date as _date
    from collections import defaultdict

    c = conn.cursor()

    c.execute("SELECT date FROM iv_daily_records ORDER BY date")
    existing_dates = {r[0] for r in c.fetchall()}

    # ── Step 1: seed from MarketSnapshot (real VIX proxy) ──────────────────
    c.execute("""
        SELECT timestamp, nifty_spot, india_vix
        FROM market_snapshots
        WHERE india_vix IS NOT NULL AND india_vix > 0
        ORDER BY timestamp ASC
    """)
    rows = c.fetchall()

    daily = defaultdict(list)
    for ts_str, spot, vix in rows:
        try:
            if isinstance(ts_str, str):
                ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00").split("+")[0])
            else:
                ts = ts_str
            ist = ts + timedelta(hours=5, minutes=30)
            date_key = ist.date().isoformat()
            daily[date_key].append((spot, vix))
        except Exception:
            continue

    for date_key, entries in sorted(daily.items()):
        if date_key in existing_dates:
            continue
        vix_values = [v for _, v in entries]
        spot_values = [s for s, _ in entries]
        median_vix = sorted(vix_values)[len(vix_values) // 2]
        median_spot = sorted(spot_values)[len(spot_values) // 2]
        atm_iv = round(median_vix * 1.08, 4)
        atm_strike = round(median_spot / 50) * 50
        c.execute("""
            INSERT OR IGNORE INTO iv_daily_records
                (date, atm_iv, india_vix, nifty_close, atm_strike, is_proxy)
            VALUES (?, ?, ?, ?, ?, 1)
        """, (date_key, atm_iv, round(median_vix, 4), round(median_spot, 2), atm_strike))
        existing_dates.add(date_key)

    conn.commit()

    # ── Step 2: if fewer than 60 records, generate synthetic history ────────
    c.execute("SELECT COUNT(*) FROM iv_daily_records")
    total = c.fetchone()[0]
    if total >= 60:
        return

    # Anchor: start synthetic series from 252 trading days before earliest real date
    c.execute("SELECT date, nifty_close FROM iv_daily_records WHERE nifty_close IS NOT NULL ORDER BY date ASC LIMIT 1")
    row = c.fetchone()
    earliest_str, earliest_spot = row if row else (None, None)

    if earliest_str:
        anchor_date = _date.fromisoformat(earliest_str)
    else:
        anchor_date = _date.today()
    start_spot = float(earliest_spot) * 0.80 if earliest_spot else 19000.0

    # Walk back 252 trading days from anchor
    synthetic_dates = []
    d = anchor_date - timedelta(days=1)
    while len(synthetic_dates) < 252:
        if d.weekday() < 5:
            synthetic_dates.append(d)
        d -= timedelta(days=1)
    synthetic_dates.reverse()  # oldest first

    # OU process parameters (India VIX calibration)
    MEAN_VIX = 14.5
    KAPPA    = 0.05   # daily mean-reversion speed
    SIGMA    = 1.0    # daily vol of VIX (absolute points)
    IV_MULT  = 1.08   # ATM IV ≈ VIX × 1.08
    IV_NOISE = 0.25   # additional per-day noise on ATM IV

    NIFTY_DRIFT = 0.00035   # ~9% annual = 0.035%/day
    NIFTY_SIGMA = 0.008     # ~0.8% daily vol

    random.seed(42)  # reproducible series
    current_vix = MEAN_VIX * (1 + random.gauss(0, 0.10))
    current_vix = max(10.0, min(25.0, current_vix))
    nifty_spot = start_spot

    synthetic_rows = []
    for d in synthetic_dates:
        date_key = d.isoformat()
        if date_key in existing_dates:
            continue

        # VIX: Ornstein-Uhlenbeck step
        shock = random.gauss(0, SIGMA)
        current_vix += KAPPA * (MEAN_VIX - current_vix) + shock
        current_vix = max(9.5, min(30.0, current_vix))

        # ATM IV with small noise
        atm_iv = current_vix * IV_MULT + random.gauss(0, IV_NOISE)
        atm_iv = max(8.0, round(atm_iv, 4))

        # Nifty spot random walk with drift
        ret = random.gauss(NIFTY_DRIFT, NIFTY_SIGMA)
        nifty_spot *= (1 + ret)
        nifty_spot = max(15000.0, min(30000.0, nifty_spot))
        atm_strike = round(nifty_spot / 50) * 50

        synthetic_rows.append((
            date_key,
            round(atm_iv, 4),
            round(current_vix, 4),
            round(nifty_spot, 2),
            int(atm_strike),
            1,  # is_proxy
        ))
        existing_dates.add(date_key)

    if synthetic_rows:
        c.executemany("""
            INSERT OR IGNORE INTO iv_daily_records
                (date, atm_iv, india_vix, nifty_close, atm_strike, is_proxy)
            VALUES (?, ?, ?, ?, ?, ?)
        """, synthetic_rows)
        conn.commit()
        print(f"[DB] Bootstrapped {len(synthetic_rows)} synthetic IV records for IV Rank")
