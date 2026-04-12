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

    conn.commit()
    conn.close()
