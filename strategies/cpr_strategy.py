import logging
from typing import Dict, List, Optional, Tuple, Literal
from pydantic import BaseModel
from config.settings import settings

logger = logging.getLogger("CPR_System.Strategy")

class CPRLevels(BaseModel):
    pivot: float
    bc: float
    tc: float
    r1: float
    s1: float

def calculate_cpr_levels(high: float, low: float, close: float) -> CPRLevels:
    """
    Computes static daily CPR levels using previous day's OHLC prices.
    BC = (High + Low) / 2
    Pivot = (High + Low + Close) / 3
    TC = Pivot + (Pivot - BC)
    R1 = (2 * Pivot) - Low
    S1 = (2 * Pivot) - High
    """
    pivot   = (high + low + close) / 3.0
    cpr_mid = (high + low) / 2.0
    cpr_oth = pivot + (pivot - cpr_mid)
    tc      = max(cpr_mid, cpr_oth)   # TC always the HIGHER central line
    bc      = min(cpr_mid, cpr_oth)   # BC always the LOWER central line
    r1      = (2.0 * pivot) - low
    s1      = (2.0 * pivot) - high
    
    return CPRLevels(
        pivot=round(pivot, 2),
        bc=round(bc, 2),
        tc=round(tc, 2),
        r1=round(r1, 2),
        s1=round(s1, 2)
    )

def is_inside_cpr(price: float, levels: CPRLevels) -> bool:
    """A checks guard checking if price lines land within the CPR range."""
    lo = min(levels.bc, levels.tc)
    hi = max(levels.bc, levels.tc)
    return lo <= price <= hi

class SetupStateMachine:
    """
    State machine implementing Setup A, B, C, D transitions on Nifty 5m candle bars.
    State representation:
      0 = IDLE
      1 = BROKEN
      2 = RECOVERED
      3 = RETESTED
      4 = CONFIRMED
    """
    def __init__(self, name: Literal["SETUP_A", "SETUP_B", "SETUP_C", "SETUP_D"]):
        self.name = name
        self.state = 0
        self.state_bar = 0      # Bar index when current state was updated
        
        # Windows from settings/inputs
        self.fail_win = settings.FAILURE_WINDOW
        self.ret_win = settings.RETEST_WINDOW
        self.con_win = settings.CONFIRMATION_WINDOW
        self.ent_win = settings.ENTRY_TRIGGER_WINDOW
        self.ret_tol = settings.RETEST_TOLERANCE
        
        # Setup specific trigger trackers
        self.r_high: Optional[float] = None  # Retest Bar High
        self.r_low: Optional[float] = None   # Retest Bar Low
        self.c_high: Optional[float] = None  # Confirmation Bar High
        self.c_low: Optional[float] = None   # Confirmation Bar Low

    def bars_elapsed(self, current_bar_idx: int) -> int:
        return current_bar_idx - self.state_bar

    def reset_state(self, bar_idx: int, log_reason: str = ""):
        if self.state != 0:
            logger.info(f"{self.name}: State reset from {self.state} to 0 at bar {bar_idx}. Reason: {log_reason}")
        self.state = 0
        self.state_bar = bar_idx
        self.r_high = None
        self.r_low = None
        self.c_high = None
        self.c_low = None

    def update(self, candle: Dict, idx: int, levels: CPRLevels) -> Tuple[bool, Optional[Dict]]:
        """
        Receives current candle data (open, high, low, close, index) and returns:
          (trigger_entry: bool, order_details: Optional[Dict])
        """
        cl = candle["close"]
        hi = candle["high"]
        lo = candle["low"]
        
        trigger_entry = False
        order_details = None

        if self.name == "SETUP_A":
            # SETUP A: R1 -> TC Short
            # State 0 - Break: close > R1
            if self.state == 0:
                if cl > levels.r1:
                    self.state = 1
                    self.state_bar = idx
                    logger.info(f"SETUP_A: Bar {idx} - state 1 [BROKEN]. Close ({cl}) above R1 ({levels.r1})")
                    
            # State 1 - Failure: close back below R1 within fail_win
            elif self.state == 1:
                elapsed = self.bars_elapsed(idx)
                if elapsed <= self.fail_win:
                    if cl < levels.r1:
                        self.state = 2
                        self.state_bar = idx
                        logger.info(f"SETUP_A: Bar {idx} - state 2 [RECOVERED]. Close ({cl}) below R1 ({levels.r1}) inside fail window ({elapsed} bars)")
                else:
                    self.reset_state(idx, "Failure window elapsed")
                    
            # State 2 - Retest: high in [R1-tol, R1+tol] and close < R1 within ret_win
            elif self.state == 2:
                elapsed = self.bars_elapsed(idx)
                if elapsed <= self.ret_win:
                    if (levels.r1 - self.ret_tol) <= hi <= (levels.r1 + self.ret_tol) and cl < levels.r1:
                        self.state = 3
                        self.state_bar = idx
                        self.r_high = hi
                        self.r_low = lo
                        logger.info(f"SETUP_A: Bar {idx} - state 3 [RETESTED]. High ({hi}) retesting R1 ({levels.r1}) within retest tolerance ({self.ret_tol})")
                else:
                    self.reset_state(idx, "Retest window elapsed")
                    
            # State 3 - Confirmation: close < Retest Low within con_win
            elif self.state == 3:
                elapsed = self.bars_elapsed(idx)
                if elapsed <= self.con_win:
                    if cl < self.r_low:
                        self.state = 4
                        self.state_bar = idx
                        self.c_high = hi
                        self.c_low = lo
                        logger.info(f"SETUP_A: Bar {idx} - state 4 [CONFIRMED]. Close ({cl}) broke Retest Low ({self.r_low})")
                else:
                    self.reset_state(idx, "Confirmation window elapsed")
                    
            # State 4 - Entry Trigger: price low breaks below Confirmation Low within ent_win
            # Guard: close > TC (above CPR) and not inside CPR
            elif self.state == 4:
                elapsed = self.bars_elapsed(idx)
                if elapsed <= self.ent_win:
                    if lo < self.c_low:
                        # Checks guards
                        inside = is_inside_cpr(cl, levels)
                        if not inside and cl > levels.tc:
                            trigger_entry = True
                            _sl = self.r_high + settings.SL_BUFFER
                            _entry = cl
                            _risk  = abs(_entry - _sl)          # points at risk
                            _tp    = _entry - (2.0 * _risk)     # 1:2 RR target
                            order_details = {
                                "setup_name": "SETUP_A",
                                "trade_type": "SELL",  # Short direction
                                "trigger_price": lo,
                                "stop_loss": round(_sl, 2),
                                "take_profit": round(_tp, 2),
                                "lots": settings.POSITION_LOTS
                            }
                            logger.info(f"SETUP_A: Bar {idx} - ENTRY TRIGGERED. Low ({lo}) under Confirmation Low ({self.c_low}). SL={order_details['stop_loss']}, TP={order_details['take_profit']}")
                        else:
                            logger.info(f"SETUP_A: Bar {idx} - Break occurred but block guards activated. insideCPR: {inside}, Close: {cl} (TC={levels.tc})")
                        self.reset_state(idx, "Entry decision computed")
                else:
                    self.reset_state(idx, "Entry window elapsed")

        elif self.name == "SETUP_B":
            # SETUP B: S1 -> BC Long
            # State 0 - Break: close < S1
            if self.state == 0:
                if cl < levels.s1:
                    self.state = 1
                    self.state_bar = idx
                    logger.info(f"SETUP_B: Bar {idx} - state 1 [BROKEN]. Close ({cl}) below S1 ({levels.s1})")
                    
            # State 1 - Recovery: close above S1 within fail_win
            elif self.state == 1:
                elapsed = self.bars_elapsed(idx)
                if elapsed <= self.fail_win:
                    if cl > levels.s1:
                        self.state = 2
                        self.state_bar = idx
                        logger.info(f"SETUP_B: Bar {idx} - state 2 [RECOVERED]. Close ({cl}) above S1 ({levels.s1}) inside fail window ({elapsed} bars)")
                else:
                    self.reset_state(idx, "Recovery window elapsed")
                    
            # State 2 - Retest: low in [S1-tol, S1+tol] and close > S1 within ret_win
            elif self.state == 2:
                elapsed = self.bars_elapsed(idx)
                if elapsed <= self.ret_win:
                    if (levels.s1 - self.ret_tol) <= lo <= (levels.s1 + self.ret_tol) and cl > levels.s1:
                        self.state = 3
                        self.state_bar = idx
                        self.r_high = hi
                        self.r_low = lo
                        logger.info(f"SETUP_B: Bar {idx} - state 3 [RETESTED]. Low ({lo}) retesting S1 ({levels.s1}) within tolerance")
                else:
                    self.reset_state(idx, "Retest window elapsed")
                    
            # State 3 - Confirmation: close > Retest High within con_win
            elif self.state == 3:
                elapsed = self.bars_elapsed(idx)
                if elapsed <= self.con_win:
                    if cl > self.r_high:
                        self.state = 4
                        self.state_bar = idx
                        self.c_high = hi
                        self.c_low = lo
                        logger.info(f"SETUP_B: Bar {idx} - state 4 [CONFIRMED]. Close ({cl}) broke Retest High ({self.r_high})")
                else:
                    self.reset_state(idx, "Confirmation window elapsed")
                    
            # State 4 - Entry Trigger: high breaks above Confirmation High within ent_win
            # Guard: close < BC (below CPR) and not inside CPR
            elif self.state == 4:
                elapsed = self.bars_elapsed(idx)
                if elapsed <= self.ent_win:
                    if hi > self.c_high:
                        inside = is_inside_cpr(cl, levels)
                        if not inside and cl < levels.bc:
                            trigger_entry = True
                            _sl = self.r_low - settings.SL_BUFFER
                            _entry = cl
                            _risk  = abs(_entry - _sl)
                            _tp    = _entry + (2.0 * _risk)     # 1:2 RR target
                            order_details = {
                                "setup_name": "SETUP_B",
                                "trade_type": "BUY",  # Long direction
                                "trigger_price": hi,
                                "stop_loss": round(_sl, 2),
                                "take_profit": round(_tp, 2),
                                "lots": settings.POSITION_LOTS
                            }
                            logger.info(f"SETUP_B: Bar {idx} - ENTRY TRIGGERED. High ({hi}) above Confirmation High ({self.c_high}). SL={order_details['stop_loss']}, TP={order_details['take_profit']}")
                        else:
                            logger.info(f"SETUP_B: Bar {idx} - Break occurred but guards activated. insideCPR: {inside}, Close: {cl} (BC={levels.bc})")
                        self.reset_state(idx, "Entry decision computed")
                else:
                    self.reset_state(idx, "Entry window elapsed")

        elif self.name == "SETUP_C":
            # SETUP C: TC -> R1 Long
            # State 0 - Break: close > TC (price sits above CPR)
            if self.state == 0:
                if cl > levels.tc:
                    self.state = 1
                    self.state_bar = idx
                    logger.info(f"SETUP_C: Bar {idx} - state 1 [BROKEN]. Close ({cl}) above TC ({levels.tc})")
                    
            # State 1 - Failure: close < TC within fail_win
            elif self.state == 1:
                elapsed = self.bars_elapsed(idx)
                if elapsed <= self.fail_win:
                    if cl < levels.tc:
                        self.state = 2
                        self.state_bar = idx
                        logger.info(f"SETUP_C: Bar {idx} - state 2 [RECOVERED]. Close ({cl}) fell back below TC ({levels.tc})")
                else:
                    self.reset_state(idx, "Failure window elapsed")
                    
            # State 2 - Retest: low in [TC-tol, TC+tol] and close > TC within ret_win
            elif self.state == 2:
                elapsed = self.bars_elapsed(idx)
                if elapsed <= self.ret_win:
                    if (levels.tc - self.ret_tol) <= lo <= (levels.tc + self.ret_tol) and cl > levels.tc:
                        self.state = 3
                        self.state_bar = idx
                        self.r_high = hi
                        self.r_low = lo
                        logger.info(f"SETUP_C: Bar {idx} - state 3 [RETESTED]. Low ({lo}) retested TC ({levels.tc})")
                else:
                    self.reset_state(idx, "Retest window elapsed")
                    
            # State 3 - Confirmation: close > Retest High within con_win
            elif self.state == 3:
                elapsed = self.bars_elapsed(idx)
                if elapsed <= self.con_win:
                    if cl > self.r_high:
                        self.state = 4
                        self.state_bar = idx
                        self.c_high = hi
                        self.c_low = lo
                        logger.info(f"SETUP_C: Bar {idx} - state 4 [CONFIRMED]. Close ({cl}) broke Retest High ({self.r_high})")
                else:
                    self.reset_state(idx, "Confirmation window elapsed")
                    
            # State 4 - Entry Trigger: high breaks above Confirmation High within ent_win
            # Guard: close > TC (above CPR) and not inside CPR
            elif self.state == 4:
                elapsed = self.bars_elapsed(idx)
                if elapsed <= self.ent_win:
                    if hi > self.c_high:
                        inside = is_inside_cpr(cl, levels)
                        if not inside and cl > levels.tc:
                            trigger_entry = True
                            _sl = self.r_low - settings.SL_BUFFER
                            _entry = cl
                            _risk  = abs(_entry - _sl)
                            _tp    = _entry + (2.0 * _risk)     # 1:2 RR target
                            order_details = {
                                "setup_name": "SETUP_C",
                                "trade_type": "BUY",  # Long direction
                                "trigger_price": hi,
                                "stop_loss": round(_sl, 2),
                                "take_profit": round(_tp, 2),
                                "lots": settings.POSITION_LOTS
                            }
                            logger.info(f"SETUP_C: Bar {idx} - ENTRY TRIGGERED. High ({hi}) above Confirmation High ({self.c_high}). SL={order_details['stop_loss']}, TP={order_details['take_profit']}")
                        else:
                            logger.info(f"SETUP_C: Bar {idx} - Break occurred but guards activated. insideCPR: {inside}, Close: {cl} (TC={levels.tc})")
                        self.reset_state(idx, "Entry decision computed")
                else:
                    self.reset_state(idx, "Entry window elapsed")

        elif self.name == "SETUP_D":
            # SETUP D: BC -> S1 Short
            # State 0 - Break: close < BC (price sits below CPR)
            if self.state == 0:
                if cl < levels.bc:
                    self.state = 1
                    self.state_bar = idx
                    logger.info(f"SETUP_D: Bar {idx} - state 1 [BROKEN]. Close ({cl}) below BC ({levels.bc})")
                    
            # State 1 - Recovery: close > BC within fail_win
            elif self.state == 1:
                elapsed = self.bars_elapsed(idx)
                if elapsed <= self.fail_win:
                    if cl > levels.bc:
                        self.state = 2
                        self.state_bar = idx
                        logger.info(f"SETUP_D: Bar {idx} - state 2 [RECOVERED]. Close ({cl}) recovered above BC ({levels.bc})")
                else:
                    self.reset_state(idx, "Recovery window elapsed")
                    
            # State 2 - Retest: high in [BC-tol, BC+tol] and close < BC within ret_win
            elif self.state == 2:
                elapsed = self.bars_elapsed(idx)
                if elapsed <= self.ret_win:
                    if (levels.bc - self.ret_tol) <= hi <= (levels.bc + self.ret_tol) and cl < levels.bc:
                        self.state = 3
                        self.state_bar = idx
                        self.r_high = hi
                        self.r_low = lo
                        logger.info(f"SETUP_D: Bar {idx} - state 3 [RETESTED]. High ({hi}) retests BC ({levels.bc})")
                else:
                    self.reset_state(idx, "Retest window elapsed")
                    
            # State 3 - Confirmation: close < Retest Low within con_win
            elif self.state == 3:
                elapsed = self.bars_elapsed(idx)
                if elapsed <= self.con_win:
                    if cl < self.r_low:
                        self.state = 4
                        self.state_bar = idx
                        self.c_high = hi
                        self.c_low = lo
                        logger.info(f"SETUP_D: Bar {idx} - state 4 [CONFIRMED]. Close ({cl}) broke Retest Low ({self.r_low})")
                else:
                    self.reset_state(idx, "Confirmation window elapsed")
                    
            # State 4 - Entry Trigger: low breaks below Confirmation Low within ent_win
            # Guard: close < BC (below CPR) and not inside CPR
            elif self.state == 4:
                elapsed = self.bars_elapsed(idx)
                if elapsed <= self.ent_win:
                    if lo < self.c_low:
                        inside = is_inside_cpr(cl, levels)
                        if not inside and cl < levels.bc:
                            trigger_entry = True
                            _sl = self.r_high + settings.SL_BUFFER
                            _entry = cl
                            _risk  = abs(_entry - _sl)
                            _tp    = _entry - (2.0 * _risk)     # 1:2 RR target
                            order_details = {
                                "setup_name": "SETUP_D",
                                "trade_type": "SELL",  # Short direction
                                "trigger_price": lo,
                                "stop_loss": round(_sl, 2),
                                "take_profit": round(_tp, 2),
                                "lots": settings.POSITION_LOTS
                            }
                            logger.info(f"SETUP_D: Bar {idx} - ENTRY TRIGGERED. Low ({lo}) under Confirmation Low ({self.c_low}). SL={order_details['stop_loss']}, TP={order_details['take_profit']}")
                        else:
                            logger.info(f"SETUP_D: Bar {idx} - Break occurred but guards activated. insideCPR: {inside}, Close: {cl} (BC={levels.bc})")
                        self.reset_state(idx, "Entry decision computed")
                else:
                    self.reset_state(idx, "Entry window elapsed")

        return trigger_entry, order_details
