"""SQLAlchemy ORM models for NiftyEdge."""

from datetime import datetime
from sqlalchemy import (
    Column, Integer, Float, String, DateTime, Date, Boolean,
    ForeignKey, JSON, Text, Enum as SQLEnum
)
from sqlalchemy.orm import relationship
from database import Base
import enum


# ── Enums ────────────────────────────────────────────────────────────────────

class TradeType(str, enum.Enum):
    CE_SELL = "CE_SELL"
    PE_SELL = "PE_SELL"
    CE_BUY = "CE_BUY"
    PE_BUY = "PE_BUY"


class TradeStatus(str, enum.Enum):
    OPEN = "OPEN"
    CLOSED = "CLOSED"
    EXPIRED = "EXPIRED"   # option expiry passed while trade was still open


class AlertStatus(str, enum.Enum):
    ARMED = "ARMED"
    TRIGGERED = "TRIGGERED"
    PAUSED = "PAUSED"


# ── User ─────────────────────────────────────────────────────────────────────

class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)
    email = Column(String, unique=True, index=True, nullable=False)
    name = Column(String, nullable=False)
    password_hash = Column(String, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)
    settings = Column(JSON, default=dict)

    trades = relationship("Trade", back_populates="user")
    alerts = relationship("AlertRule", back_populates="user")


# ── Trade ────────────────────────────────────────────────────────────────────

class Trade(Base):
    __tablename__ = "trades"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    trade_type = Column(String, nullable=False)  # CE_SELL, PE_SELL, CE_BUY, PE_BUY
    strike = Column(Integer, nullable=False)
    expiry_date = Column(String, nullable=False)  # DD-Mon-YYYY format
    entry_premium = Column(Float, nullable=False)
    entry_iv = Column(Float, default=0.0)
    entry_delta = Column(Float, default=0.0)
    entry_date = Column(DateTime, default=datetime.utcnow)
    quantity = Column(Integer, nullable=False, default=1)  # in lots
    exit_price = Column(Float, nullable=True)
    exit_date = Column(DateTime, nullable=True)
    status = Column(String, default=TradeStatus.OPEN.value)
    parent_trade_id = Column(Integer, ForeignKey("trades.id"), nullable=True)
    strategy_id = Column(Integer, ForeignKey("strategies.id"), nullable=True)
    realized_pnl = Column(Float, nullable=True)

    user = relationship("User", back_populates="trades")
    hedges = relationship("Trade", foreign_keys=[parent_trade_id])
    strategy = relationship("Strategy", back_populates="trades", foreign_keys=[strategy_id])


# ── Market Snapshot (shared, written by data engine) ─────────────────────────

class MarketSnapshot(Base):
    __tablename__ = "market_snapshots"

    id = Column(Integer, primary_key=True, index=True)
    timestamp = Column(DateTime, default=datetime.utcnow, index=True)
    nifty_spot = Column(Float, nullable=False)
    nifty_change = Column(Float, nullable=True)
    nifty_change_pct = Column(Float, nullable=True)
    india_vix = Column(Float, nullable=True)
    vix_change = Column(Float, nullable=True)
    vix_change_pct = Column(Float, nullable=True)
    calculated_range_top = Column(Float, nullable=True)
    calculated_range_bottom = Column(Float, nullable=True)
    vix_range_top = Column(Float, nullable=True)
    vix_range_bottom = Column(Float, nullable=True)
    expected_move = Column(Float, nullable=True)
    days_to_expiry = Column(Integer, nullable=True)
    tendency = Column(String, nullable=True)
    expiry_date = Column(String, nullable=True)
    expiries_json = Column(JSON, nullable=True)  # full list of available expiries
    # 25-Delta Risk Reversal fields
    rr_ce_strike = Column(Integer, nullable=True)
    rr_ce_iv = Column(Float, nullable=True)
    rr_pe_strike = Column(Integer, nullable=True)
    rr_pe_iv = Column(Float, nullable=True)
    rr_value = Column(Float, nullable=True)
    rr_verdict = Column(String, nullable=True)
    # Max Pain & Directional Signal
    max_pain = Column(Float, nullable=True)
    directional_direction = Column(String, nullable=True)  # BULLISH, BEARISH, NEUTRAL
    directional_score = Column(Float, nullable=True)
    directional_factors_agree = Column(Integer, nullable=True)


# ── Strike Data (per strike per snapshot) ────────────────────────────────────

class StrikeData(Base):
    __tablename__ = "strike_data"

    id = Column(Integer, primary_key=True, index=True)
    timestamp = Column(DateTime, default=datetime.utcnow, index=True)
    strike = Column(Integer, nullable=False, index=True)
    expiry_date = Column(String, nullable=False)
    option_type = Column(String, nullable=False)  # CE or PE
    ltp = Column(Float, nullable=True)
    iv = Column(Float, nullable=True)
    delta = Column(Float, nullable=True)
    gamma = Column(Float, nullable=True)
    theta = Column(Float, nullable=True)
    vega = Column(Float, nullable=True)
    oi = Column(Float, nullable=True)
    volume = Column(Float, nullable=True)


# ── Support/Resistance Levels ────────────────────────────────────────────────

class SRLevel(Base):
    __tablename__ = "sr_levels"

    id = Column(Integer, primary_key=True, index=True)
    timestamp = Column(DateTime, default=datetime.utcnow)
    expiry_date = Column(String, nullable=True)
    source = Column(String, nullable=False)  # OI_FIB, PIVOT_OI
    level_type = Column(String, nullable=False)  # SUPPORT, RESISTANCE
    strike = Column(Integer, nullable=False)
    oi = Column(Float, nullable=True)
    label = Column(String, nullable=True)  # e.g. "OI+Fib ✓ (fib 23200)"


# ── Alert Rule ───────────────────────────────────────────────────────────────

class AlertRule(Base):
    __tablename__ = "alert_rules"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    metric = Column(String, nullable=False)
    # Metrics: nifty_spot, india_vix, ce_risk, pe_risk,
    #          position_delta, position_iv, position_premium, range_breach
    condition = Column(String, nullable=False)  # gt, lt, crosses_above, crosses_below
    threshold = Column(Float, nullable=False)
    trade_id = Column(Integer, ForeignKey("trades.id"), nullable=True)  # for position-specific alerts
    notification_method = Column(String, default="in_app")
    status = Column(String, default=AlertStatus.ARMED.value)
    cooldown_minutes = Column(Integer, default=15)
    last_triggered_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    user = relationship("User", back_populates="alerts")
    trade = relationship("Trade", foreign_keys=[trade_id])
    logs = relationship("AlertLog", back_populates="rule")


# ── Daily Signal Snapshot (EOD capture for historical insights) ─────────────

class DailySignalSnapshot(Base):
    __tablename__ = "daily_signal_snapshots"

    id = Column(Integer, primary_key=True, index=True)
    date = Column(Date, unique=True, nullable=False, index=True)
    nifty_close = Column(Float, nullable=False)
    india_vix = Column(Float, nullable=True)

    # Signals captured at EOD
    rr_value = Column(Float, nullable=True)
    rr_verdict = Column(String, nullable=True)
    tendency = Column(String, nullable=True)
    directional_direction = Column(String, nullable=True)
    directional_score = Column(Float, nullable=True)
    directional_factors_agree = Column(Integer, nullable=True)
    max_pain = Column(Float, nullable=True)

    # Next-day actual outcome (filled retroactively)
    next_day_close = Column(Float, nullable=True)
    next_day_change = Column(Float, nullable=True)
    next_day_change_pct = Column(Float, nullable=True)
    signal_correct = Column(Boolean, nullable=True)


# ── Trade Suggestion (auto-generated buy signals) ──────────────────────────

class TradeSuggestion(Base):
    __tablename__ = "trade_suggestions"

    id = Column(Integer, primary_key=True, index=True)
    suggestion_type = Column(String, nullable=False)      # OVERNIGHT or INTRADAY
    direction = Column(String, nullable=False)             # BULLISH or BEARISH
    option_type = Column(String, nullable=False)           # CE or PE
    strike = Column(Integer, nullable=False)
    entry_price = Column(Float, nullable=False)
    stop_loss = Column(Float, nullable=True)
    target = Column(Float, nullable=True)
    expiry_date = Column(String, nullable=True)

    # Signal data at creation
    directional_score = Column(Float, nullable=True)
    directional_factors = Column(Integer, nullable=True)
    tendency = Column(String, nullable=True)
    rr_value = Column(Float, nullable=True)
    confidence = Column(Float, nullable=True)              # 0-100
    rationale = Column(Text, nullable=True)

    # Status tracking
    status = Column(String, default="PENDING")             # PENDING, ACTIVE, REJECTED_AUTO, REJECTED_USER, EXECUTED, EXPIRED, SL_HIT
    created_at = Column(DateTime, default=datetime.utcnow)
    sanity_checked_at = Column(DateTime, nullable=True)
    resolved_at = Column(DateTime, nullable=True)
    current_price = Column(Float, nullable=True)
    nifty_spot_at_creation = Column(Float, nullable=True)


class AlertLog(Base):
    __tablename__ = "alert_logs"

    id = Column(Integer, primary_key=True, index=True)
    alert_rule_id = Column(Integer, ForeignKey("alert_rules.id"), nullable=False)
    triggered_at = Column(DateTime, default=datetime.utcnow)
    triggered_value = Column(Float, nullable=True)
    message = Column(String, nullable=True)

    rule = relationship("AlertRule", back_populates="logs")


# ── IV Daily Record (ATM IV history for IV Rank / IV Percentile) ─────────────

class IVDailyRecord(Base):
    __tablename__ = "iv_daily_records"

    id = Column(Integer, primary_key=True, index=True)
    date = Column(Date, unique=True, nullable=False, index=True)
    atm_iv = Column(Float, nullable=False)            # avg ATM IV (actual or VIX×1.08 proxy)
    atm_iv_ce = Column(Float, nullable=True)          # CE-side ATM IV
    atm_iv_pe = Column(Float, nullable=True)          # PE-side ATM IV
    atm_straddle_price = Column(Float, nullable=True) # ATM CE LTP + ATM PE LTP
    india_vix = Column(Float, nullable=True)
    nifty_close = Column(Float, nullable=True)
    atm_strike = Column(Integer, nullable=True)
    expiry_date = Column(String, nullable=True)
    is_proxy = Column(Boolean, default=False)         # True = bootstrapped from VIX


# ── IV Term Structure (per-expiry ATM IV snapshot at EOD) ─────────────────────

class IVTermStructureRecord(Base):
    __tablename__ = "iv_term_structure"

    id = Column(Integer, primary_key=True, index=True)
    date = Column(Date, nullable=False, index=True)
    expiry = Column(String, nullable=False)
    dte = Column(Integer, nullable=True)
    atm_iv = Column(Float, nullable=True)
    atm_straddle = Column(Float, nullable=True)


# ── IV Skew Daily (25-delta risk reversal + butterfly at EOD) ─────────────────

class IVSkewDaily(Base):
    __tablename__ = "iv_skew_daily"

    id = Column(Integer, primary_key=True, index=True)
    date = Column(Date, unique=True, nullable=False, index=True)
    delta25_call_iv = Column(Float, nullable=True)   # 25-delta CE IV
    delta25_put_iv = Column(Float, nullable=True)    # 25-delta PE IV
    risk_reversal = Column(Float, nullable=True)     # CE_IV - PE_IV (negative = fear)
    butterfly = Column(Float, nullable=True)         # (CE_IV + PE_IV)/2 - ATM_IV
    atm_iv = Column(Float, nullable=True)            # ATM IV on that date


# ── Strategy (multi-leg option strategy) ─────────────────────────────────────

class Strategy(Base):
    __tablename__ = "strategies"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    name = Column(String, nullable=False)         # e.g. "Iron Condor – Apr25"
    strategy_type = Column(String, nullable=False) # IRON_CONDOR | STRANGLE | BULL_PUT_SPREAD | BEAR_CALL_SPREAD | CALENDAR_SPREAD
    expiry_date = Column(String, nullable=False)   # DD-Mon-YYYY
    status = Column(String, default="ACTIVE")      # ACTIVE | CLOSED
    created_at = Column(DateTime, default=datetime.utcnow)

    user = relationship("User")
    trades = relationship("Trade", back_populates="strategy", foreign_keys="Trade.strategy_id")


# ── News Item (RSS-sourced, rule-scored) ──────────────────────────────────────

class NewsItem(Base):
    __tablename__ = "news_items"

    id                    = Column(Integer, primary_key=True)
    url                   = Column(String, unique=True, nullable=False)
    url_hash              = Column(String(64), index=True, nullable=False)
    headline_hash         = Column(String(64), index=True, nullable=False)
    source                = Column(String(100), nullable=False)
    headline              = Column(Text, nullable=False)
    summary               = Column(Text, nullable=True)
    published_at          = Column(DateTime, nullable=True)
    fetched_at            = Column(DateTime, default=datetime.utcnow, nullable=False)
    impact_tier           = Column(String(20), nullable=False)   # CRITICAL|HIGH|MEDIUM|LOW|IRRELEVANT
    raw_impact_score      = Column(Float, nullable=False)
    direction             = Column(String(10), nullable=False)   # BULLISH|BEARISH|NEUTRAL
    category              = Column(String(50), nullable=False)
    matched_keywords      = Column(JSON, nullable=True)
    direction_word_counts = Column(JSON, nullable=True)
    scorer_version        = Column(String(20), default="rules-v1", nullable=False)
    is_pushed_to_ticker   = Column(Boolean, default=False, nullable=False)


class NewsDismissal(Base):
    __tablename__ = "news_dismissals"

    id           = Column(Integer, primary_key=True)
    user_id      = Column(Integer, ForeignKey("users.id"), nullable=False)
    news_item_id = Column(Integer, ForeignKey("news_items.id"), nullable=False)
    dismissed_at = Column(DateTime, default=datetime.utcnow, nullable=False)
