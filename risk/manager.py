import logging
from datetime import datetime, date
from typing import Optional, List, Dict, Any, Tuple
from sqlalchemy.orm import Session
from database.models import Trade, DailyState
from config.settings import settings
from telegram.bot import notify_limit_reached, notify_system_error

logger = logging.getLogger("CPR_System.Risk")

class RiskManager:
    def __init__(self, db: Session):
        self.db = db
        self.max_trades = settings.MAX_DAILY_TRADES
        self.loss_limit = settings.DAILY_LOSS_LIMIT
        self.lots = settings.POSITION_LOTS

    def get_or_create_daily_state(self) -> DailyState:
        """Retrieves or initializes the risk configuration tracker for today."""
        today_str = date.today().strftime("%Y-%m-%d")
        state = self.db.query(DailyState).filter(DailyState.trade_date == today_str).first()
        
        if not state:
            logger.info(f"Initializing new risk status records for date: {today_str}")
            state = DailyState(
                trade_date=today_str,
                trade_count=0,
                realized_pnl=0.0,
                is_blocked=False
            )
            self.db.add(state)
            self.db.commit()
            self.db.refresh(state)
        return state

    def check_open_position(self) -> bool:
        """Returns True if there is an active open position in the system."""
        open_trade = self.db.query(Trade).filter(Trade.status == "OPEN").first()
        return open_trade is not None

    def can_trade(self) -> bool:
        """
        Validates risk guidelines. Returns True if strategy is cleared to execute trades.
        Prerequisites:
        1. No current position open.
        2. Today's realized trades < Max trades (2).
        3. Today's loss has not reached limits (INR 2000).
        4. Day status is not explicitly blocked.
        """
        state = self.get_or_create_daily_state()
        logger.info(f"TRADES_TODAY={state.trade_count}/{self.max_trades} REALIZED_PNL=₹{state.realized_pnl:.2f} BLOCKED={state.is_blocked}")
        
        # 1. Position checking
        if self.check_open_position():
            logger.info("Risk Check: Blocked. Active trade position currently open.")
            return False
            
        # 2. Block checking
        if state.is_blocked:
            logger.warning("Risk Check: Blocked. Day state marked is_blocked.")
            return False

        # 3. Maximum Trades checking
        if state.trade_count >= self.max_trades:
            logger.warning(f"Risk Check: Blocked. Daily trade count {state.trade_count} matches or exceeds limit ({self.max_trades}).")
            state.is_blocked = True
            self.db.commit()
            notify_limit_reached(f"Trade limit reached ({state.trade_count}/{self.max_trades} trades executed)", state.realized_pnl)
            return False

        # 4. Maximum Loss checking
        if state.realized_pnl <= -self.loss_limit:
            logger.warning(f"Risk Check: Blocked. Financial loss check fails: today realized {state.realized_pnl} <= limit -{self.loss_limit}")
            state.is_blocked = True
            self.db.commit()
            notify_limit_reached(f"Financial Loss Limit (INR {self.loss_limit}) breached: current loss ₹{state.realized_pnl}", state.realized_pnl)
            return False

        logger.info(f"Risk check passes. Current trades today: {state.trade_count}/{self.max_trades}. Realized P&L today: ₹{state.realized_pnl:.2f}")
        return True

    def register_trade_entry(self, setup_name: str, trade_type: str, option_symbol: str, strike: float, option_type: str, entry_price: float, is_paper: bool = True):
        """Deducts limits and writes a trade event execution upon Upstox transaction completion."""
        state = self.get_or_create_daily_state()
        
        # Write Trade
        new_trade = Trade(
            setup_name=setup_name,
            trade_type=trade_type,
            option_symbol=option_symbol,
            strike_price=strike,
            option_type=option_type,
            entry_price=entry_price,
            stop_loss=0.0,  # We'll calculate SL and write afterwards
            take_profit=0.0,
            lots=self.lots,
            status="OPEN",
            is_paper=is_paper,
            entry_time=datetime.utcnow()
        )
        
        try:
            self.db.add(new_trade)
            
            # Increment trade execution counts
            state.trade_count += 1
            self.db.commit()
            self.db.refresh(new_trade)
            
            logger.info(f"Registered Trade entry: ID {new_trade.id} for {setup_name} - {option_symbol} at avg entry price {entry_price}")
            return new_trade
        except Exception as e:
            self.db.rollback()
            logger.error(f"Failed to record trade entry: {e}")
            notify_system_error(f"Database error committing entry: {e}")
            return None

    def register_trade_exit(self, trade_id: int, exit_price: float, exit_status: str) -> Optional[Trade]:
        """Finalizes positions in DB, computes actual trade gains, and updates cash ledgers."""
        trade = self.db.query(Trade).filter(Trade.id == trade_id).first()
        if not trade:
            logger.error(f"Trade ID {trade_id} not found for closure.")
            return None
            
        state = self.get_or_create_daily_state()
        
        qty = trade.lots * 75  # Nifty option lot size is 75
        pnl = 0.0
        
        # In Indian option trading: Options are ALWAYS BOUGHT to execute strategy directives.
        # Long (Setup B, C) -> Buy CE, then Sell CE to close. PNL = (Exit - Entry) * Qty
        # Short (Setup A, D) -> Buy PE, then Sell PE to close. PNL = (Exit - Entry) * Qty
        # Under our setup rule, we are buying contracts to play whichever direction, so Option PNL is ALWAYS (Exit - Entry) * Qty!
        pnl = (exit_price - trade.entry_price) * qty
        
        trade.exit_price = exit_price
        trade.exit_time = datetime.utcnow()
        trade.status = exit_status
        trade.pnl = pnl
        
        state.realized_pnl += pnl
        
        # Check if daily loss trigger was breached
        if state.realized_pnl <= -self.loss_limit:
            state.is_blocked = True
            logger.warning(f"Extreme loss limit triggered after trade exit. Realized today: ₹{state.realized_pnl}")
            
        try:
            self.db.commit()
            self.db.refresh(trade)
            logger.info(f"Closed Trade ID {trade_id}: PNL Calculated: ₹{pnl:.2f}. Running Day Realized Balance: ₹{state.realized_pnl:.2f}")
            return trade
        except Exception as e:
            self.db.rollback()
            logger.error(f"Failed to record trade exit: {e}")
            notify_system_error(f"Database error on committing trade closure: {e}")
            return None
