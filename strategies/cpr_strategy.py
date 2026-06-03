import logging
from typing import Dict, Optional, Tuple, Literal
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
    Computes static daily CPR levels from previous day's OHLC.
    TC is always the HIGHER central line, BC always the LOWER.
    """
    pivot   = (high + low + close) / 3.0
    cpr_mid = (high + low) / 2.0
    cpr_oth = pivot + (pivot - cpr_mid)
    tc      = max(cpr_mid, cpr_oth)
    bc      = min(cpr_mid, cpr_oth)
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
    return min(levels.bc, levels.tc) <= price <= max(levels.bc, levels.tc)


class SetupStateMachine:
    """
    3-state machine per setup (IDLE → BROKEN → RECOVERED → RETESTED → ENTRY).

    State 0  IDLE       — waiting for initial breakout
    State 1  BROKEN     — candle broke AND closed beyond the key level
    State 2  RECOVERED  — price pulled back inside (closed back through key level)
    State 3  RETESTED   — price came back to touch key level from inside; entry is armed

    ── Change 1: State 1 (BROKEN) ──
      Candle must BOTH pierce AND close beyond the key level.
      Setup A/D (short): hi > level AND cl > level
      Setup B/C (long):  lo < level AND cl < level

    ── Change 2: Entry on retest candle cross ──
      State 3 stores the RETEST candle's high and low.
      Entry fires the moment a subsequent candle crosses the retest candle's
      low (shorts) or high (longs) — NO separate confirmation state needed.
      SL = entry candle's high + SL_BUFFER (shorts)
           entry candle's low  - SL_BUFFER (longs)

    ── Change 3: Target = min(1:RR, CPR level) with 1:1 minimum for all setups ──
      Setup A  short: TP = min(1:RR target, TC)   i.e. whichever is LOWER (deeper profit)
      Setup B  long:  TP = min(1:RR target, BC)   i.e. whichever is LOWER (closer to entry)
      Setup C  long:  TP = min(1:RR target, R1)   i.e. whichever is LOWER (closer to entry)
      Setup D  short: TP = min(1:RR target, S1)   i.e. whichever is LOWER (deeper profit)
      Trade is SKIPPED if the resulting TP gives less than 1:1 RR.

    ── Retest invalidation ──
      In State 2, if a candle closes back through the key level in the
      original breakout direction → back to State 1 (re-broken, not reset).
    """

    def __init__(self, name: Literal["SETUP_A", "SETUP_B", "SETUP_C", "SETUP_D"]):
        self.name      = name
        self.state     = 0
        self.state_bar = 0

        self.fail_win  = settings.FAILURE_WINDOW
        self.ret_win   = settings.RETEST_WINDOW
        self.con_win   = settings.CONFIRMATION_WINDOW
        self.ent_win   = settings.ENTRY_TRIGGER_WINDOW
        self.ret_tol   = settings.RETEST_TOLERANCE

        # Retest candle trackers
        self.r_high: Optional[float] = None
        self.r_low:  Optional[float] = None
        self.c_high: Optional[float] = None
        self.c_low:  Optional[float] = None

    def bars_elapsed(self, idx: int) -> int:
        return idx - self.state_bar

    def reset_state(self, bar_idx: int, reason: str = ""):
        if self.state != 0:
            logger.info(f"{self.name}: RESET {self.state}→0 @ bar {bar_idx}. {reason}")
        self.state     = 0
        self.state_bar = bar_idx
        self.r_high    = None
        self.r_low     = None

    def _re_enter_state1(self, bar_idx: int, reason: str = ""):
        """Re-broken through key level — restart from State 1, not full reset."""
        logger.info(f"{self.name}: RE-BROKEN 2→1 @ bar {bar_idx}. {reason}")
        self.state     = 1
        self.state_bar = bar_idx
        self.r_high    = None
        self.r_low     = None

    # ──────────────────────────────────────────────────────────────
    # Target helper — applies Change 3 for all setups
    # ──────────────────────────────────────────────────────────────
    def _calc_target(
        self,
        entry: float,
        sl: float,
        cpr_level: float,
        direction: str,       # "short" or "long"
    ) -> Optional[float]:
        """
        Returns the final TP or None if the trade doesn't meet 1:1 minimum.

        direction="short":
          1:RR target = entry - (REWARD_RATIO * risk)
          CPR anchor  = TC  (price is heading DOWN toward TC)
          Final TP    = min(rr_target, cpr_level)   ← whichever is LOWER
                        i.e. target is 1:RR or the level, whichever gives deeper profit.
          Minimum 1:1 check: TP must be <= entry - risk

        direction="long":
          1:RR target = entry + (REWARD_RATIO * risk)
          CPR anchor  = BC / R1  (price heading UP)
          Final TP    = min(rr_target, cpr_level)   ← whichever is LOWER
                        i.e. whichever is CLOSER to entry
          Minimum 1:1 check: TP must be >= entry + risk
        """
        risk = abs(entry - sl)
        if risk == 0:
            return None

        if direction == "short":
            rr_tp  = entry - (settings.REWARD_RATIO * risk)
            cpr_target = cpr_level + settings.TARGET_BUFFER
            final_tp = min(rr_tp, cpr_target)   # target is 1:RR or the level, whichever is lower
            # 1:1 check — TP must be at least 1× risk away from entry
            if final_tp >= entry - risk:
                logger.info(
                    f"{self.name}: Trade SKIPPED — TP {final_tp:.2f} too close "
                    f"(entry {entry:.2f} risk {risk:.2f}, need TP <= {entry - risk:.2f})"
                )
                return None
        else:  # long
            rr_tp  = entry + (settings.REWARD_RATIO * risk)
            cpr_target = cpr_level - settings.TARGET_BUFFER
            final_tp = min(rr_tp, cpr_target)   # closer to entry = lower price
            # 1:1 check
            if final_tp <= entry + risk:
                logger.info(
                    f"{self.name}: Trade SKIPPED — TP {final_tp:.2f} too close "
                    f"(entry {entry:.2f} risk {risk:.2f}, need TP >= {entry + risk:.2f})"
                )
                return None

        return round(final_tp, 2)

    # ──────────────────────────────────────────────────────────────
    def update(self, candle: Dict, idx: int, levels: CPRLevels) -> Tuple[bool, Optional[Dict]]:
        cl = candle["close"]
        hi = candle["high"]
        lo = candle["low"]

        trigger_entry = False
        order_details = None

        # ══════════════════════════════════════════════════════════
        # SETUP A — Short  R1 → TC
        # Break: hi > R1 AND cl > R1
        # Recover: cl < R1
        # Retest: hi touches R1 (± tol) AND cl < R1
        # Entry: next candle's lo < r_low  →  enter short
        # SL: entry candle hi + SL_BUFFER
        # TP: min(1:RR, TC)
        # ══════════════════════════════════════════════════════════
        if self.name == "SETUP_A":

            if self.state == 0:
                # Change 1: both hi AND cl must be above R1
                if hi > levels.r1 and cl > levels.r1:
                    self.state = 1; self.state_bar = idx
                    logger.info(f"SETUP_A: bar {idx} STATE1 BROKEN. hi={hi} cl={cl} > R1={levels.r1}")

            elif self.state == 1:
                if self.bars_elapsed(idx) <= self.fail_win:
                    if cl < levels.r1:
                        self.state = 2; self.state_bar = idx
                        logger.info(f"SETUP_A: bar {idx} STATE2 RECOVERED. cl={cl} < R1={levels.r1}")
                    # still above R1 — stay in state 1
                else:
                    self.reset_state(idx, "Failure window elapsed")

            elif self.state == 2:
                if self.bars_elapsed(idx) <= self.ret_win:
                    if cl > levels.r1:
                        # Re-broken above R1 → back to State 1
                        self._re_enter_state1(idx, f"cl={cl} > R1={levels.r1} in State 2")
                    elif (levels.r1 - self.ret_tol) <= hi <= (levels.r1 + self.ret_tol) and cl < levels.r1:
                        self.state = 3; self.state_bar = idx
                        self.r_high = hi; self.r_low = lo
                        logger.info(f"SETUP_A: bar {idx} STATE3 RETESTED. hi={hi} ≈ R1={levels.r1} r_low={lo}")
                    else:
                        self.state_bar = idx
                else:
                    self.reset_state(idx, "Retest window elapsed")

            elif self.state == 3:
                if self.bars_elapsed(idx) <= self.con_win:
                    if (levels.r1 - self.ret_tol) <= hi <= (levels.r1 + self.ret_tol) and cl < levels.r1:
                        self.state_bar = idx
                        self.r_high = hi; self.r_low = lo
                        logger.info(
                            f"SETUP_A: bar {idx} STATE3 RETEST UPDATED. hi={hi} ≈ R1={levels.r1} r_low={lo}"
                        )
                    elif lo < self.r_low:
                        self.state = 4
                        self.state_bar = idx
                        self.c_low = lo
                        logger.info(
                            f"SETUP_A: bar {idx} STATE4 CONFIRMED. lo={lo} < r_low={self.r_low}"
                        )
                else:
                    self.reset_state(idx, "Confirmation window elapsed")

            elif self.state == 4:
                if self.bars_elapsed(idx) <= self.ent_win:
                    if lo < self.c_low:
                        _sl    = self.r_high + settings.SL_BUFFER
                        _entry = cl
                        _tp    = self._calc_target(_entry, _sl, levels.tc, "short")
                        if _tp is not None and not is_inside_cpr(cl, levels):
                            trigger_entry = True
                            order_details = {
                                "setup_name":    "SETUP_A",
                                "trade_type":    "SELL",
                                "trigger_price": lo,
                                "stop_loss":     round(_sl, 2),
                                "take_profit":   _tp,
                                "lots":          settings.POSITION_LOTS,
                            }
                            logger.info(
                                f"SETUP_A: bar {idx} ENTRY. lo={lo} < c_low={self.c_low} "
                                f"entry={_entry} SL={_sl} TP={_tp}"
                            )
                        else:
                            logger.info(
                                f"SETUP_A: bar {idx} SKIPPED. TP={_tp} insufficient or inside CPR."
                            )
                        self.reset_state(idx, "Entry decision made")
                else:
                    self.reset_state(idx, "Entry window elapsed")

        # ══════════════════════════════════════════════════════════
        # SETUP B — Long  S1 → BC
        # Break: lo < S1 AND cl < S1
        # Recover: cl > S1
        # Retest: lo touches S1 (± tol) AND cl > S1
        # Entry: next candle's hi > r_high  →  enter long
        # SL: entry candle lo - SL_BUFFER
        # TP: min(1:RR, BC)
        # ══════════════════════════════════════════════════════════
        elif self.name == "SETUP_B":

            if self.state == 0:
                if lo < levels.s1 and cl < levels.s1:
                    self.state = 1; self.state_bar = idx
                    logger.info(f"SETUP_B: bar {idx} STATE1 BROKEN. lo={lo} cl={cl} < S1={levels.s1}")

            elif self.state == 1:
                if self.bars_elapsed(idx) <= self.fail_win:
                    if cl > levels.s1:
                        self.state = 2; self.state_bar = idx
                        logger.info(f"SETUP_B: bar {idx} STATE2 RECOVERED. cl={cl} > S1={levels.s1}")
                else:
                    self.reset_state(idx, "Failure window elapsed")

            elif self.state == 2:
                if self.bars_elapsed(idx) <= self.ret_win:
                    if cl < levels.s1:
                        self._re_enter_state1(idx, f"cl={cl} < S1={levels.s1} in State 2")
                    elif (levels.s1 - self.ret_tol) <= lo <= (levels.s1 + self.ret_tol) and cl > levels.s1:
                        self.state = 3; self.state_bar = idx
                        self.r_high = hi; self.r_low = lo
                        logger.info(f"SETUP_B: bar {idx} STATE3 RETESTED. lo={lo} ≈ S1={levels.s1} r_high={hi}")
                    else:
                        self.state_bar = idx
                else:
                    self.reset_state(idx, "Retest window elapsed")

            elif self.state == 3:
                if self.bars_elapsed(idx) <= self.con_win:
                    if (levels.s1 - self.ret_tol) <= lo <= (levels.s1 + self.ret_tol) and cl > levels.s1:
                        self.state_bar = idx
                        self.r_high = hi; self.r_low = lo
                        logger.info(
                            f"SETUP_B: bar {idx} STATE3 RETEST UPDATED. lo={lo} ≈ S1={levels.s1} r_high={hi}"
                        )
                    elif hi > self.r_high:
                        self.state = 4
                        self.state_bar = idx
                        self.c_high = hi
                        logger.info(
                            f"SETUP_B: bar {idx} STATE4 CONFIRMED. hi={hi} > r_high={self.r_high}"
                        )
                else:
                    self.reset_state(idx, "Confirmation window elapsed")

            elif self.state == 4:
                if self.bars_elapsed(idx) <= self.ent_win:
                    if hi > self.c_high:
                        _sl    = lo - settings.SL_BUFFER
                        _entry = cl
                        _tp    = self._calc_target(_entry, _sl, levels.bc, "long")
                        if _tp is not None and not is_inside_cpr(cl, levels):
                            trigger_entry = True
                            order_details = {
                                "setup_name":    "SETUP_B",
                                "trade_type":    "BUY",
                                "trigger_price": hi,
                                "stop_loss":     round(_sl, 2),
                                "take_profit":   _tp,
                                "lots":          settings.POSITION_LOTS,
                            }
                            logger.info(
                                f"SETUP_B: bar {idx} ENTRY. hi={hi} > c_high={self.c_high} "
                                f"entry={_entry} SL={_sl} TP={_tp}"
                            )
                        else:
                            logger.info(f"SETUP_B: bar {idx} SKIPPED. TP={_tp} insufficient or inside CPR.")
                        self.reset_state(idx, "Entry decision made")
                else:
                    self.reset_state(idx, "Entry window elapsed")

        # ══════════════════════════════════════════════════════════
        # SETUP C — Long  TC → R1
        # Break: lo < TC AND cl < TC   (price closed below TC)
        #   wait — Setup C is a long that plays TC as support.
        #   State 1: price broke below TC
        #   State 2: price recovered above TC
        #   Retest: lo touches TC (± tol) AND cl > TC — bounces off TC as support
        #   Entry: hi > r_high of retest candle
        #   SL: retest candle low - SL_BUFFER
        #   TP: min(1:RR, R1)
        # ══════════════════════════════════════════════════════════
        elif self.name == "SETUP_C":

            if self.state == 0:
                # Change 1: both lo AND cl must be below TC for a valid breakout
                if lo < levels.tc and cl < levels.tc:
                    self.state = 1; self.state_bar = idx
                    logger.info(f"SETUP_C: bar {idx} STATE1 BROKEN. lo={lo} cl={cl} < TC={levels.tc}")

            elif self.state == 1:
                if self.bars_elapsed(idx) <= self.fail_win:
                    if cl < levels.tc:
                        self.state = 2; self.state_bar = idx
                        logger.info(f"SETUP_C: bar {idx} STATE2 RECOVERED. cl={cl} < TC={levels.tc}")
                else:
                    self.reset_state(idx, "Failure window elapsed")

            elif self.state == 2:
                if self.bars_elapsed(idx) <= self.ret_win:
                    if cl < levels.tc:
                        self._re_enter_state1(idx, f"cl={cl} < TC={levels.tc} in State 2")
                    elif (levels.tc - self.ret_tol) <= lo <= (levels.tc + self.ret_tol) and cl > levels.tc:
                        self.state = 3; self.state_bar = idx
                        self.r_high = hi; self.r_low = lo
                        logger.info(f"SETUP_C: bar {idx} STATE3 RETESTED. lo={lo} ≈ TC={levels.tc} r_high={hi}")
                    else:
                        self.state_bar = idx
                else:
                    self.reset_state(idx, "Retest window elapsed")

            elif self.state == 3:
                if self.bars_elapsed(idx) <= self.con_win:
                    if (levels.tc - self.ret_tol) <= lo <= (levels.tc + self.ret_tol) and cl > levels.tc:
                        self.state_bar = idx
                        self.r_high = hi; self.r_low = lo
                        logger.info(
                            f"SETUP_C: bar {idx} STATE3 RETEST UPDATED. lo={lo} ≈ TC={levels.tc} r_high={hi}"
                        )
                    elif hi > self.r_high:
                        self.state = 4
                        self.state_bar = idx
                        self.c_high = hi
                        logger.info(
                            f"SETUP_C: bar {idx} STATE4 CONFIRMED. hi={hi} > r_high={self.r_high}"
                        )
                else:
                    self.reset_state(idx, "Confirmation window elapsed")

            elif self.state == 4:
                if self.bars_elapsed(idx) <= self.ent_win:
                    if hi > self.c_high:
                        _sl    = self.r_low - settings.SL_BUFFER
                        _entry = cl
                        _tp    = self._calc_target(_entry, _sl, levels.r1, "long")
                        if _tp is not None and not is_inside_cpr(cl, levels):
                            trigger_entry = True
                            order_details = {
                                "setup_name":    "SETUP_C",
                                "trade_type":    "BUY",
                                "trigger_price": hi,
                                "stop_loss":     round(_sl, 2),
                                "take_profit":   _tp,
                                "lots":          settings.POSITION_LOTS,
                            }
                            logger.info(
                                f"SETUP_C: bar {idx} ENTRY. hi={hi} > c_high={self.c_high} "
                                f"entry={_entry} SL={_sl} TP={_tp}"
                            )
                        else:
                            logger.info(f"SETUP_C: bar {idx} SKIPPED. TP={_tp} insufficient or inside CPR.")
                        self.reset_state(idx, "Entry decision made")
                else:
                    self.reset_state(idx, "Entry window elapsed")

        # ══════════════════════════════════════════════════════════
        # SETUP D — Short  BC → S1
        # Break: hi > BC AND cl > BC   (price closed above BC)
        #   wait — Setup D is a short that plays BC as resistance.
        #   State 1: price broke above BC
        #   State 2: price recovered below BC
        #   Retest: hi touches BC (± tol) AND cl < BC — rejected at BC
        #   Entry: lo < r_low of retest candle
        #   SL: retest candle high + SL_BUFFER
        #   TP: min(1:RR, S1)
        # ══════════════════════════════════════════════════════════
        elif self.name == "SETUP_D":

            if self.state == 0:
                # Change 1: both hi AND cl must be above BC for a valid breakout
                if hi > levels.bc and cl > levels.bc:
                    self.state = 1; self.state_bar = idx
                    logger.info(f"SETUP_D: bar {idx} STATE1 BROKEN. hi={hi} cl={cl} > BC={levels.bc}")

            elif self.state == 1:
                if self.bars_elapsed(idx) <= self.fail_win:
                    if cl > levels.bc:
                        self.state = 2; self.state_bar = idx
                        logger.info(f"SETUP_D: bar {idx} STATE2 RECOVERED. cl={cl} > BC={levels.bc}")
                else:
                    self.reset_state(idx, "Recovery window elapsed")

            elif self.state == 2:
                if self.bars_elapsed(idx) <= self.ret_win:
                    if cl > levels.bc:
                        self._re_enter_state1(idx, f"cl={cl} > BC={levels.bc} in State 2")
                    elif (levels.bc - self.ret_tol) <= hi <= (levels.bc + self.ret_tol) and cl < levels.bc:
                        self.state = 3; self.state_bar = idx
                        self.r_high = hi; self.r_low = lo
                        logger.info(f"SETUP_D: bar {idx} STATE3 RETESTED. hi={hi} ≈ BC={levels.bc} r_low={lo}")
                    else:
                        self.state_bar = idx
                else:
                    self.reset_state(idx, "Retest window elapsed")

            elif self.state == 3:
                if self.bars_elapsed(idx) <= self.con_win:
                    if (levels.bc - self.ret_tol) <= hi <= (levels.bc + self.ret_tol) and cl < levels.bc:
                        self.state_bar = idx
                        self.r_high = hi; self.r_low = lo
                        logger.info(
                            f"SETUP_D: bar {idx} STATE3 RETEST UPDATED. hi={hi} ≈ BC={levels.bc} r_low={lo}"
                        )
                    elif lo < self.r_low:
                        self.state = 4
                        self.state_bar = idx
                        self.c_low = lo
                        logger.info(
                            f"SETUP_D: bar {idx} STATE4 CONFIRMED. lo={lo} < r_low={self.r_low}"
                        )
                else:
                    self.reset_state(idx, "Confirmation window elapsed")

            elif self.state == 4:
                if self.bars_elapsed(idx) <= self.ent_win:
                    if lo < self.c_low:
                        _sl    = self.r_high + settings.SL_BUFFER
                        _entry = cl
                        _tp    = self._calc_target(_entry, _sl, levels.s1, "short")
                        if _tp is not None and not is_inside_cpr(cl, levels):
                            trigger_entry = True
                            order_details = {
                                "setup_name":    "SETUP_D",
                                "trade_type":    "SELL",
                                "trigger_price": lo,
                                "stop_loss":     round(_sl, 2),
                                "take_profit":   _tp,
                                "lots":          settings.POSITION_LOTS,
                            }
                            logger.info(
                                f"SETUP_D: bar {idx} ENTRY. lo={lo} < c_low={self.c_low} "
                                f"entry={_entry} SL={_sl} TP={_tp}"
                            )
                        else:
                            logger.info(f"SETUP_D: bar {idx} SKIPPED. TP={_tp} insufficient or inside CPR.")
                        self.reset_state(idx, "Entry decision made")
                else:
                    self.reset_state(idx, "Entry window elapsed")

        return trigger_entry, order_details
