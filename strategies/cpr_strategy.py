"""
CPR 4-Setup State Machine Strategy
====================================
Changes from original:
  1. Each stage has a 10-candle max window. If condition is not met within
     10 candles the state resets to IDLE.
  2. Retest stage uses a 3-point buffer: price must come within 3 pts of
     the key level (R1 / S1 / TC / BC) even if it does not touch it exactly.
     The retest candle close must still be on the correct side of the level.
  3. All four setups (A, B, C, D) updated consistently.

Backward-compatible exports (used by other modules in this repo):
  - is_inside_cpr(price, tc, bc)
  - is_above_cpr(price, tc)
  - is_below_cpr(price, bc)
  - SetupStateMachine          → alias for CPRStrategy
  - calculate_cpr_levels(...)
  - Candle, TradeSignal, SetupState, StageTracker
  - process_setup_a/b/c/d
"""

from dataclasses import dataclass
from enum import Enum, auto
from typing import Optional
import logging

logger = logging.getLogger(__name__)

RETEST_BUFFER_PTS  = 3   # price must come within this many points of the level
STAGE_CANDLE_LIMIT = 10  # max candles allowed in any single stage before reset


# ---------------------------------------------------------------------------
# Enums & dataclasses
# ---------------------------------------------------------------------------

class SetupState(Enum):
    IDLE     = auto()
    BREAKOUT = auto()
    PULLBACK = auto()
    RETEST   = auto()
    CONFIRM  = auto()


class SetupDirection(Enum):
    LONG  = "CE"
    SHORT = "PE"


@dataclass
class Candle:
    open:  float
    high:  float
    low:   float
    close: float


@dataclass
class TradeSignal:
    setup:       str
    direction:   SetupDirection
    stop_loss:   float
    take_profit: float


@dataclass
class StageTracker:
    state:            SetupState      = SetupState.IDLE
    candles_in_state: int             = 0
    retest_high:      Optional[float] = None
    retest_low:       Optional[float] = None

    def advance(self, new_state: SetupState) -> None:
        self.state            = new_state
        self.candles_in_state = 0

    def tick(self) -> None:
        self.candles_in_state += 1

    def reset(self) -> None:
        self.state            = SetupState.IDLE
        self.candles_in_state = 0
        self.retest_high      = None
        self.retest_low       = None

    @property
    def timed_out(self) -> bool:
        return self.candles_in_state >= STAGE_CANDLE_LIMIT


# ---------------------------------------------------------------------------
# CPR level calculation  (call once per day with previous session H/L/C)
# ---------------------------------------------------------------------------

def calculate_cpr_levels(prev_high: float, prev_low: float, prev_close: float) -> dict:
    """
    Returns daily CPR levels.  Order top-to-bottom:  R1 > TC > BC > S1
    """
    pivot = (prev_high + prev_low + prev_close) / 3
    bc    = (prev_high + prev_low) / 2
    tc    = (pivot - bc) + pivot
    r1    = (2 * pivot) - prev_low
    s1    = (2 * pivot) - prev_high
    return dict(pivot=pivot, bc=bc, tc=tc, r1=r1, s1=s1)


# ---------------------------------------------------------------------------
# CPR band helpers  (imported by other modules)
# ---------------------------------------------------------------------------

def is_inside_cpr(price: float, tc: float, bc: float) -> bool:
    """True if price is inside the CPR band (BC <= price <= TC)."""
    return bc <= price <= tc


def is_above_cpr(price: float, tc: float) -> bool:
    """True if price is above TC."""
    return price > tc


def is_below_cpr(price: float, bc: float) -> bool:
    """True if price is below BC."""
    return price < bc


def cpr_width(tc: float, bc: float) -> float:
    """Width of the CPR band in points."""
    return round(tc - bc, 2)


def is_narrow_cpr(tc: float, bc: float, threshold: float = 20.0) -> bool:
    """True if CPR width is less than threshold points (trending day signal)."""
    return cpr_width(tc, bc) < threshold


def is_wide_cpr(tc: float, bc: float, threshold: float = 50.0) -> bool:
    """True if CPR width is greater than threshold points (sideways day signal)."""
    return cpr_width(tc, bc) > threshold


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _check_timeout_and_tick(tracker: StageTracker, setup_name: str) -> bool:
    if tracker.state == SetupState.IDLE:
        return False
    tracker.tick()
    if tracker.timed_out:
        logger.info(
            "%s timed out after %d candles in %s — resetting to IDLE",
            setup_name, tracker.candles_in_state, tracker.state.name
        )
        tracker.reset()
        return True
    return False


# ---------------------------------------------------------------------------
# Setup A  —  Short  (R1 → TC)  BUY PE
# ---------------------------------------------------------------------------

def process_setup_a(candle: Candle, tracker: StageTracker,
                    r1: float, tc: float) -> Optional[TradeSignal]:
    if _check_timeout_and_tick(tracker, "Setup A"):
        return None

    if tracker.state == SetupState.IDLE:
        if candle.close > r1:
            logger.info("Setup A ① BREAKOUT: close %.2f > R1 %.2f", candle.close, r1)
            tracker.advance(SetupState.BREAKOUT)

    elif tracker.state == SetupState.BREAKOUT:
        if candle.close < r1:
            logger.info("Setup A ② PULLBACK: close %.2f < R1 %.2f", candle.close, r1)
            tracker.advance(SetupState.PULLBACK)

    elif tracker.state == SetupState.PULLBACK:
        touched = candle.high >= (r1 - RETEST_BUFFER_PTS)
        if candle.close >= r1:
            logger.info("Setup A retest invalidated — restarting BREAKOUT")
            tracker.advance(SetupState.BREAKOUT)
        elif touched and candle.close < r1:
            tracker.retest_high = candle.high
            logger.info("Setup A ③ RETEST: high %.2f, SL ref %.2f", candle.high, tracker.retest_high)
            tracker.advance(SetupState.RETEST)

    elif tracker.state == SetupState.RETEST:
        break_confirm = candle.low < (r1 - RETEST_BUFFER_PTS)
        guard_ok      = candle.close > tc
        if break_confirm and guard_ok:
            sl  = tracker.retest_high + 3
            tp  = tc + 3
            logger.info("Setup A ⑤ ENTRY: BUY PE | SL %.2f | TP %.2f", sl, tp)
            tracker.reset()
            return TradeSignal("A", SetupDirection.SHORT, sl, tp)
        elif candle.close >= r1:
            logger.info("Setup A confirm invalidated — restarting BREAKOUT")
            tracker.advance(SetupState.BREAKOUT)

    return None


# ---------------------------------------------------------------------------
# Setup B  —  Long  (S1 → BC)  BUY CE
# ---------------------------------------------------------------------------

def process_setup_b(candle: Candle, tracker: StageTracker,
                    s1: float, bc: float) -> Optional[TradeSignal]:
    if _check_timeout_and_tick(tracker, "Setup B"):
        return None

    if tracker.state == SetupState.IDLE:
        if candle.close < s1:
            logger.info("Setup B ① BREAKDOWN: close %.2f < S1 %.2f", candle.close, s1)
            tracker.advance(SetupState.BREAKOUT)

    elif tracker.state == SetupState.BREAKOUT:
        if candle.close > s1:
            logger.info("Setup B ② RECOVERY: close %.2f > S1 %.2f", candle.close, s1)
            tracker.advance(SetupState.PULLBACK)

    elif tracker.state == SetupState.PULLBACK:
        touched = candle.low <= (s1 + RETEST_BUFFER_PTS)
        if candle.close <= s1:
            logger.info("Setup B retest invalidated — restarting BREAKDOWN")
            tracker.advance(SetupState.BREAKOUT)
        elif touched and candle.close > s1:
            tracker.retest_low = candle.low
            logger.info("Setup B ③ RETEST: low %.2f, SL ref %.2f", candle.low, tracker.retest_low)
            tracker.advance(SetupState.RETEST)

    elif tracker.state == SetupState.RETEST:
        break_confirm = candle.high > (s1 + RETEST_BUFFER_PTS)
        guard_ok      = candle.close < bc
        if break_confirm and guard_ok:
            sl  = tracker.retest_low - 3
            tp  = bc - 3
            logger.info("Setup B ⑤ ENTRY: BUY CE | SL %.2f | TP %.2f", sl, tp)
            tracker.reset()
            return TradeSignal("B", SetupDirection.LONG, sl, tp)
        elif candle.close <= s1:
            logger.info("Setup B confirm invalidated — restarting BREAKDOWN")
            tracker.advance(SetupState.BREAKOUT)

    return None


# ---------------------------------------------------------------------------
# Setup C  —  Long  (TC → R1)  BUY CE
# ---------------------------------------------------------------------------

def process_setup_c(candle: Candle, tracker: StageTracker,
                    tc: float, r1: float) -> Optional[TradeSignal]:
    if _check_timeout_and_tick(tracker, "Setup C"):
        return None

    if tracker.state == SetupState.IDLE:
        if candle.close > tc:
            logger.info("Setup C ① BREAKOUT above TC: close %.2f", candle.close)
            tracker.advance(SetupState.BREAKOUT)

    elif tracker.state == SetupState.BREAKOUT:
        if candle.close < tc:
            logger.info("Setup C ② PULLBACK below TC: close %.2f", candle.close)
            tracker.advance(SetupState.PULLBACK)

    elif tracker.state == SetupState.PULLBACK:
        touched = candle.high >= (tc - RETEST_BUFFER_PTS)
        if candle.close >= tc:
            logger.info("Setup C retest invalidated — restarting BREAKOUT")
            tracker.advance(SetupState.BREAKOUT)
        elif touched and candle.close < tc:
            tracker.retest_low = candle.low
            logger.info("Setup C ③ RETEST: SL ref low %.2f", tracker.retest_low)
            tracker.advance(SetupState.RETEST)

    elif tracker.state == SetupState.RETEST:
        break_confirm = candle.high > (tc + RETEST_BUFFER_PTS)
        guard_ok      = candle.close > tc
        if break_confirm and guard_ok:
            sl  = tracker.retest_low - 3
            tp  = r1 - 3
            logger.info("Setup C ⑤ ENTRY: BUY CE | SL %.2f | TP %.2f", sl, tp)
            tracker.reset()
            return TradeSignal("C", SetupDirection.LONG, sl, tp)
        elif candle.close <= tc:
            logger.info("Setup C confirm invalidated — restarting BREAKOUT")
            tracker.advance(SetupState.BREAKOUT)

    return None


# ---------------------------------------------------------------------------
# Setup D  —  Short  (BC → S1)  BUY PE
# ---------------------------------------------------------------------------

def process_setup_d(candle: Candle, tracker: StageTracker,
                    bc: float, s1: float) -> Optional[TradeSignal]:
    if _check_timeout_and_tick(tracker, "Setup D"):
        return None

    if tracker.state == SetupState.IDLE:
        if candle.close < bc:
            logger.info("Setup D ① BREAKDOWN below BC: close %.2f", candle.close)
            tracker.advance(SetupState.BREAKOUT)

    elif tracker.state == SetupState.BREAKOUT:
        if candle.close > bc:
            logger.info("Setup D ② RECOVERY above BC: close %.2f", candle.close)
            tracker.advance(SetupState.PULLBACK)

    elif tracker.state == SetupState.PULLBACK:
        touched = candle.low <= (bc + RETEST_BUFFER_PTS)
        if candle.close <= bc:
            logger.info("Setup D retest invalidated — restarting BREAKDOWN")
            tracker.advance(SetupState.BREAKOUT)
        elif touched and candle.close > bc:
            tracker.retest_high = candle.high
            logger.info("Setup D ③ RETEST: SL ref high %.2f", tracker.retest_high)
            tracker.advance(SetupState.RETEST)

    elif tracker.state == SetupState.RETEST:
        break_confirm = candle.low < (bc - RETEST_BUFFER_PTS)
        guard_ok      = candle.close < bc
        if break_confirm and guard_ok:
            sl  = tracker.retest_high + 3
            tp  = s1 + 3
            logger.info("Setup D ⑤ ENTRY: BUY PE | SL %.2f | TP %.2f", sl, tp)
            tracker.reset()
            return TradeSignal("D", SetupDirection.SHORT, sl, tp)
        elif candle.close >= bc:
            logger.info("Setup D confirm invalidated — restarting BREAKDOWN")
            tracker.advance(SetupState.BREAKOUT)

    return None


# ---------------------------------------------------------------------------
# Main strategy coordinator
# ---------------------------------------------------------------------------

class CPRStrategy:
    """
    Holds state for all four setup trackers across the trading session.
    Instantiate once per day; call reset() at session close.
    """

    def __init__(self):
        self.trackers = {
            "A": StageTracker(),
            "B": StageTracker(),
            "C": StageTracker(),
            "D": StageTracker(),
        }
        self.levels: dict = {}

    def set_daily_levels(self, prev_high: float, prev_low: float, prev_close: float) -> None:
        """Call once before market open with previous session H/L/C."""
        self.levels = calculate_cpr_levels(prev_high, prev_low, prev_close)
        logger.info(
            "Daily CPR levels — R1:%.2f TC:%.2f BC:%.2f S1:%.2f",
            self.levels["r1"], self.levels["tc"],
            self.levels["bc"], self.levels["s1"],
        )

    def on_candle_close(self, candle: Candle) -> list:
        if not self.levels:
            raise RuntimeError("Call set_daily_levels() before processing candles.")
        lv = self.levels
        signals = []
        for sig in (
            process_setup_a(candle, self.trackers["A"], lv["r1"], lv["tc"]),
            process_setup_b(candle, self.trackers["B"], lv["s1"], lv["bc"]),
            process_setup_c(candle, self.trackers["C"], lv["tc"], lv["r1"]),
            process_setup_d(candle, self.trackers["D"], lv["bc"], lv["s1"]),
        ):
            if sig is not None:
                signals.append(sig)
        return signals

    def reset(self) -> None:
        for tracker in self.trackers.values():
            tracker.reset()
        self.levels = {}
        logger.info("CPR strategy reset.")

    @property
    def active_states(self) -> dict:
        """Returns {setup_name: state_name} for all trackers."""
        return {k: v.state.name for k, v in self.trackers.items()}

    def is_any_active(self) -> bool:
        """True if any setup is past IDLE (sequence in progress)."""
        return any(t.state != SetupState.IDLE for t in self.trackers.values())


# ---------------------------------------------------------------------------
# Backward-compatible alias — original class name was SetupStateMachine
# ---------------------------------------------------------------------------

SetupStateMachine = CPRStrategy
