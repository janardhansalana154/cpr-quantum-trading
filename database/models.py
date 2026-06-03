from datetime import datetime
from sqlalchemy import Column, Integer, String, Float, DateTime, Boolean
from database.db import Base

class Trade(Base):
    __tablename__ = "trades"

    id = Column(Integer, primary_key=True, index=True)
    setup_name = Column(String(20), nullable=False)  # SETUP_A, SETUP_B, SETUP_C, SETUP_D
    trade_type = Column(String(10), nullable=False)  # BUY (Long) or SELL (Short)
    option_symbol = Column(String(50), nullable=False) # e.g., NIFTY26MAY19500CE
    strike_price = Column(Float, nullable=False)
    option_type = Column(String(10), nullable=False)  # CE or PE
    entry_price = Column(Float, nullable=False)
    exit_price = Column(Float, nullable=True)
    stop_loss = Column(Float, nullable=False)
    take_profit = Column(Float, nullable=False)
    lots = Column(Integer, default=1)
    status = Column(String(20), default="OPEN")  # OPEN, CLOSED_TP, CLOSED_SL, CLOSED_MANUAL, FAILED
    pnl = Column(Float, default=0.0)
    entry_time = Column(DateTime, default=datetime.utcnow)
    exit_time = Column(DateTime, nullable=True)
    is_paper = Column(Boolean, default=True)

class DailyState(Base):
    __tablename__ = "daily_states"

    id = Column(Integer, primary_key=True, index=True)
    trade_date = Column(String(10), unique=True, index=True)  # YYYY-MM-DD
    trade_count = Column(Integer, default=0)
    realized_pnl = Column(Float, default=0.0)
    is_blocked = Column(Boolean, default=False)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

class StrategyState(Base):
    __tablename__ = "strategy_states"

    id = Column(Integer, primary_key=True, index=True)
    setup_name = Column(String(20), unique=True, index=True)  # SETUP_A, SETUP_B, SETUP_C, SETUP_D
    current_state = Column(Integer, default=0)  # 0=IDLE, 1=broken, 2=recovered, 3=retested, 4=confirmed
    state_start_bar = Column(Integer, default=0)
    retest_high = Column(Float, nullable=True)
    retest_low = Column(Float, nullable=True)
    confirmation_high = Column(Float, nullable=True)
    confirmation_low = Column(Float, nullable=True)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

class UpstoxToken(Base):
    __tablename__ = "upstox_tokens"

    id = Column(Integer, primary_key=True, index=True)
    access_token = Column(String(500), nullable=True)
    refresh_token = Column(String(500), nullable=True)
    status = Column(String(55), default="Disconnected")  # Connected, Disconnected, Expired
    expiry_time = Column(DateTime, nullable=True)
    last_authenticated_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

