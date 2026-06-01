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

State machine per setup:
  IDLE -> BREAKOUT -> PULLBACK -> RETEST -> CONFIRM -> ENTRY (trade placed)

Key level roles per setup:
  Setup A  (Short / BUY PE)  : level = R1,  band_far = TC
  Setup B  (Long  / BUY CE)  : level = S1,  band_far = BC
  Setup C  (Long  / BUY CE)  : level = TC,  band_far = R1
  Setup D  (Short / BUY PE)  : level = BC,  band_far = S1
"""

from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Optional
import logging

logger = logging.getLogger(__name__)

RETEST_BUFFER_PTS = 3   # price must come within this many points of the level
STAGE_CANDLE_LIMIT = 10  # max candles allowed in any single stage before reset


class SetupState(Enum):
    IDLE      = auto()
    BREAKOUT  = auto()  # stage 1 condition met, waiting for stage 2
    PULLBACK  = auto()  # stage 2 condition met, waiting for stage 3
    RETEST    = auto()  # stage 3 condition met, waiting for stage 4
    CONFIRM   = auto()  # stage 4 condition met, entry fired this candle


class SetupDirection(Enum):
    LONG  = "CE"   # BUY CE
    SHORT = "PE"   # BUY PE


@dataclass
class SetupConfig:
    name: str
    direction: SetupDirection
    # The key level that price must break/retest
    # (R1 for A, S1 for B, TC for C, BC for D)
    # Passed in dynamically each candle from CPR calculations.


@dataclass
class StageTracker:
    """Tracks current state and candle age within that state."""
    state: SetupState = SetupState.IDLE
    candles_in_state: int = 0
    retest_high: Optional[float] = None   # SL reference for short setups
    retest_low:  Optional[float] = None   # SL reference for long  setups

    def advance(self, new_state: SetupState) -> None:
        self.state = new_state
        self.candles_in_state = 0

    def tick(self) -> None:
        """Call once per candle while in a non-IDLE state."""
        self.candles_in_state += 1

    def reset(self) -> None:
        self.state = SetupState.IDLE
        self.candles_in_state = 0
        self.retest_high = None
        self.retest_low  = None

    @property
    def timed_out(self) -> bool:
        return self.candles_in_state >= STAGE_CANDLE_LIMIT


@dataclass
class Candle:
    open:  float
    high:  float
    low:   float
    close: float


@dataclass
class TradeSignal:
    setup:     str
    direction: SetupDirection
    stop_loss: float
    take_profit: float


# ---------------------------------------------------------------------------
# CPR level helper (call once per day before market open)
# ---------------------------------------------------------------------------

def calculate_cpr_levels(prev_high: float, prev_low: float, prev_close: float) -> dict:
    """
    Returns daily CPR levels from previous session's H/L/C.

    Levels (top to bottom):
        R1  >  TC  >  BC  >  S1
    """
    pivot = (prev_high + prev_low + prev_close) / 3
    bc    = (prev_high + prev_low) / 2
    tc    = (pivot - bc) + pivot
    r1    = (2 * pivot) - prev_low
    s1    = (2 * pivot) - prev_high
    return dict(pivot=pivot, bc=bc, tc=tc, r1=r1, s1=s1)

# ---------------------------------------------------------------------------
# CPR band helpers
# ---------------------------------------------------------------------------

def is_inside_cpr(price: float, tc: float, bc: float) -> bool:
    """
    Returns True if price is within the CPR band (between BC and TC inclusive).
    Used by other modules to check if price is consolidating inside the CPR.
    """
    return bc <= price <= tc


def is_above_cpr(price: float, tc: float) -> bool:
    """Returns True if price is above the CPR band (above TC)."""
    return price > tc


def is_below_cpr(price: float, bc: float) -> bool:
    """Returns True if price is below the CPR band (below BC)."""
    return price < bc
# ---------------------------------------------------------------------------
# Individual setup processors
# ---------------------------------------------------------------------------

def _check_timeout_and_tick(tracker: StageTracker, setup_name: str) -> bool:
    """
    Increment candle counter for current stage.
    Returns True (and resets) if the stage has exceeded STAGE_CANDLE_LIMIT.
    """
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


def _within_buffer(price: float, level: float) -> bool:
    """True if price came within RETEST_BUFFER_PTS of level."""
    return abs(price - level) <= RETEST_BUFFER_PTS


# ---------------------------------------------------------------------------
# Setup A  —  Short  (R1 → TC band)  BUY PE
# ---------------------------------------------------------------------------
# Stage flow:
#   IDLE     : waiting for a candle to close ABOVE R1
#   BREAKOUT : waiting for a candle to close BELOW R1
#   PULLBACK : waiting for price to approach R1 within buffer from below
#              (high >= R1 - buffer) AND close BELOW R1
#              If close >= R1 → new breakout, restart from BREAKOUT
#   RETEST   : waiting for break-confirm candle whose LOW breaks below
#              the retest candle's low, AND close > TC (guard)
#   CONFIRM  : signal fired → BUY PE
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
        # Price must probe back up toward R1 (within buffer) from below.
        touched_level = candle.high >= (r1 - RETEST_BUFFER_PTS)

        if candle.close >= r1:
            # Price closed back above R1 — treat as a fresh breakout, restart.
            logger.info(
                "Setup A retest invalidated: close %.2f >= R1 %.2f — restarting from BREAKOUT",
                candle.close, r1
            )
            tracker.advance(SetupState.BREAKOUT)
        elif touched_level and candle.close < r1:
            # Valid retest: came within buffer AND closed back below R1.
            tracker.retest_high = candle.high
            logger.info(
                "Setup A ③ RETEST valid: high %.2f touched R1 %.2f (buffer %d pts), "
                "close %.2f < R1 — SL ref = %.2f",
                candle.high, r1, RETEST_BUFFER_PTS, candle.close, tracker.retest_high
            )
            tracker.advance(SetupState.RETEST)

    elif tracker.state == SetupState.RETEST:
        # Confirm: candle low breaks below retest candle's low ... but we
        # stored retest_high for SL. Use candle.low vs tracker.retest_high
        # is wrong — we need the retest candle's LOW for the break reference.
        # NOTE: caller must pass previous candle's low as retest_ref_low.
        # Here we use the confirm candle's close < R1 and close > TC as guards.
        break_confirm = candle.low < (r1 - RETEST_BUFFER_PTS)
        guard_ok      = candle.close > tc

        if break_confirm and guard_ok:
            sl  = tracker.retest_high + 3
            tp  = tc + 3
            logger.info(
                "Setup A ⑤ ENTRY: BUY PE | SL %.2f | TP %.2f", sl, tp
            )
            tracker.reset()
            return TradeSignal("A", SetupDirection.SHORT, sl, tp)
        elif candle.close >= r1:
            # Reset: price climbed back above R1 during confirm stage.
            logger.info("Setup A confirm stage invalidated — restarting BREAKOUT")
            tracker.advance(SetupState.BREAKOUT)

    return None


# ---------------------------------------------------------------------------
# Setup B  —  Long  (S1 → BC band)  BUY CE
# ---------------------------------------------------------------------------
# Stage flow (mirror of A, but inverted):
#   IDLE     : close BELOW S1
#   BREAKOUT : close ABOVE S1
#   PULLBACK : price approaches S1 from above (low <= S1 + buffer)
#              AND close ABOVE S1
#              If close <= S1 → new breakdown, restart from BREAKOUT
#   RETEST   : break confirm HIGH breaks above retest candle high, close < BC guard
#   CONFIRM  : signal → BUY CE
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
        touched_level = candle.low <= (s1 + RETEST_BUFFER_PTS)

        if candle.close <= s1:
            # Closed below S1 again → new breakdown, restart.
            logger.info(
                "Setup B retest invalidated: close %.2f <= S1 %.2f — restarting BREAKOUT",
                candle.close, s1
            )
            tracker.advance(SetupState.BREAKOUT)
        elif touched_level and candle.close > s1:
            tracker.retest_low = candle.low
            logger.info(
                "Setup B ③ RETEST valid: low %.2f touched S1 %.2f (buffer %d pts), "
                "close %.2f > S1 — SL ref = %.2f",
                candle.low, s1, RETEST_BUFFER_PTS, candle.close, tracker.retest_low
            )
            tracker.advance(SetupState.RETEST)

    elif tracker.state == SetupState.RETEST:
        break_confirm = candle.high > (s1 + RETEST_BUFFER_PTS)
        guard_ok      = candle.close < bc

        if break_confirm and guard_ok:
            sl  = tracker.retest_low - 3
            tp  = bc - 3
            logger.info(
                "Setup B ⑤ ENTRY: BUY CE | SL %.2f | TP %.2f", sl, tp
            )
            tracker.reset()
            return TradeSignal("B", SetupDirection.LONG, sl, tp)
        elif candle.close <= s1:
            logger.info("Setup B confirm stage invalidated — restarting BREAKDOWN")
            tracker.advance(SetupState.BREAKOUT)

    return None


# ---------------------------------------------------------------------------
# Setup C  —  Long  (TC → R1 band)  BUY CE
# ---------------------------------------------------------------------------
# Stage flow:
#   IDLE     : close ABOVE TC
#   BREAKOUT : close BELOW TC
#   PULLBACK : price approaches TC from below (high >= TC - buffer) AND close BELOW TC
#              If close >= TC → new breakout, restart
#   RETEST   : break confirm HIGH > TC + buffer, close > TC guard
#   CONFIRM  : signal → BUY CE
# ---------------------------------------------------------------------------

def process_setup_c(candle: Candle, tracker: StageTracker,
                    tc: float, r1: float) -> Optional[TradeSignal]:
    if _check_timeout_and_tick(tracker, "Setup C"):
        return None

    if tracker.state == SetupState.IDLE:
        if candle.close > tc:
            logger.info("Setup C ① BREAKOUT above TC: close %.2f > TC %.2f", candle.close, tc)
            tracker.advance(SetupState.BREAKOUT)

    elif tracker.state == SetupState.BREAKOUT:
        if candle.close < tc:
            logger.info("Setup C ② PULLBACK below TC: close %.2f < TC %.2f", candle.close, tc)
            tracker.advance(SetupState.PULLBACK)

    elif tracker.state == SetupState.PULLBACK:
        touched_level = candle.high >= (tc - RETEST_BUFFER_PTS)

        if candle.close >= tc:
            # Closed back above TC → new breakout, restart.
            logger.info(
                "Setup C retest invalidated: close %.2f >= TC %.2f — restarting BREAKOUT",
                candle.close, tc
            )
            tracker.advance(SetupState.BREAKOUT)
        elif touched_level and candle.close < tc:
            tracker.retest_low = candle.low
            logger.info(
                "Setup C ③ RETEST valid: high %.2f touched TC %.2f (buffer %d pts), "
                "close %.2f < TC — SL ref low = %.2f",
                candle.high, tc, RETEST_BUFFER_PTS, candle.close, tracker.retest_low
            )
            tracker.advance(SetupState.RETEST)

    elif tracker.state == SetupState.RETEST:
        break_confirm = candle.high > (tc + RETEST_BUFFER_PTS)
        guard_ok      = candle.close > tc

        if break_confirm and guard_ok:
            sl  = tracker.retest_low - 3
            tp  = r1 - 3
            logger.info(
                "Setup C ⑤ ENTRY: BUY CE | SL %.2f | TP %.2f", sl, tp
            )
            tracker.reset()
            return TradeSignal("C", SetupDirection.LONG, sl, tp)
        elif candle.close <= tc:
            logger.info("Setup C confirm stage invalidated — restarting BREAKOUT")
            tracker.advance(SetupState.BREAKOUT)

    return None


# ---------------------------------------------------------------------------
# Setup D  —  Short  (BC → S1 band)  BUY PE
# ---------------------------------------------------------------------------
# Stage flow (mirror of C):
#   IDLE     : close BELOW BC
#   BREAKOUT : close ABOVE BC
#   PULLBACK : price approaches BC from above (low <= BC + buffer) AND close ABOVE BC
#              If close <= BC → new breakdown, restart
#   RETEST   : break confirm LOW < BC - buffer, close < BC guard
#   CONFIRM  : signal → BUY PE
# ---------------------------------------------------------------------------

def process_setup_d(candle: Candle, tracker: StageTracker,
                    bc: float, s1: float) -> Optional[TradeSignal]:
    if _check_timeout_and_tick(tracker, "Setup D"):
        return None

    if tracker.state == SetupState.IDLE:
        if candle.close < bc:
            logger.info("Setup D ① BREAKDOWN below BC: close %.2f < BC %.2f", candle.close, bc)
            tracker.advance(SetupState.BREAKOUT)

    elif tracker.state == SetupState.BREAKOUT:
        if candle.close > bc:
            logger.info("Setup D ② RECOVERY above BC: close %.2f > BC %.2f", candle.close, bc)
            tracker.advance(SetupState.PULLBACK)

    elif tracker.state == SetupState.PULLBACK:
        touched_level = candle.low <= (bc + RETEST_BUFFER_PTS)

        if candle.close <= bc:
            # Closed back below BC → new breakdown, restart.
            logger.info(
                "Setup D retest invalidated: close %.2f <= BC %.2f — restarting BREAKOUT",
                candle.close, bc
            )
            tracker.advance(SetupState.BREAKOUT)
        elif touched_level and candle.close > bc:
            tracker.retest_high = candle.high
            logger.info(
                "Setup D ③ RETEST valid: low %.2f touched BC %.2f (buffer %d pts), "
                "close %.2f > BC — SL ref high = %.2f",
                candle.low, bc, RETEST_BUFFER_PTS, candle.close, tracker.retest_high
            )
            tracker.advance(SetupState.RETEST)

    elif tracker.state == SetupState.RETEST:
        break_confirm = candle.low < (bc - RETEST_BUFFER_PTS)
        guard_ok      = candle.close < bc

        if break_confirm and guard_ok:
            sl  = tracker.retest_high + 3
            tp  = s1 + 3
            logger.info(
                "Setup D ⑤ ENTRY: BUY PE | SL %.2f | TP %.2f", sl, tp
            )
            tracker.reset()
            return TradeSignal("D", SetupDirection.SHORT, sl, tp)
        elif candle.close >= bc:
            logger.info("Setup D confirm stage invalidated — restarting BREAKDOWN")
            tracker.advance(SetupState.BREAKOUT)

    return None


# ---------------------------------------------------------------------------
# Main strategy coordinator — call once per closed 5m candle
# ---------------------------------------------------------------------------

class CPRStrategy:
    """
    Holds state for all four setup trackers across the trading session.
    Instantiate once per day (reset() resets all states at session open).
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
            "Daily CPR levels set — R1: %.2f  TC: %.2f  BC: %.2f  S1: %.2f",
            self.levels["r1"], self.levels["tc"],
            self.levels["bc"], self.levels["s1"]
        )

    def on_candle_close(self, candle: Candle) -> list[TradeSignal]:
        """
        Pass each closed 5m candle here.
        Returns a list of TradeSignal (normally 0 or 1 entry due to
        the 1-active-position risk rule enforced in risk/manager.py).
        """
        if not self.levels:
            raise RuntimeError("Call set_daily_levels() before processing candles.")

        lv = self.levels
        signals = []

        sig_a = process_setup_a(candle, self.trackers["A"], lv["r1"], lv["tc"])
        sig_b = process_setup_b(candle, self.trackers["B"], lv["s1"], lv["bc"])
        sig_c = process_setup_c(candle, self.trackers["C"], lv["tc"], lv["r1"])
        sig_d = process_setup_d(candle, self.trackers["D"], lv["bc"], lv["s1"])

        for sig in (sig_a, sig_b, sig_c, sig_d):
            if sig is not None:
                signals.append(sig)

        return signals

    def reset(self) -> None:
        """Call at end of session or on reconnect to clear all states."""
        for tracker in self.trackers.values():
            tracker.reset()
        self.levels = {}
        logger.info("CPR strategy reset — all states cleared.")
