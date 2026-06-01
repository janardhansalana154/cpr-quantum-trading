"""
Tests for updated CPR strategy — stdlib unittest only.
Covers:
  1. 10-candle stage timeout (all stages / all setups)
  2. 3-point retest buffer — within buffer accepted, beyond buffer rejected
  3. Retest candle closes wrong side → state machine restart
  4. Full happy-path entry signal + correct SL/TP for all four setups
"""

import unittest
from cpr_strategy import (
    CPRStrategy, Candle, SetupState, StageTracker,
    process_setup_a, process_setup_b, process_setup_c, process_setup_d,
    RETEST_BUFFER_PTS, STAGE_CANDLE_LIMIT,
)

R1 = 22500.0
TC = 22400.0
BC = 22300.0
S1 = 22200.0


def c(o=None, h=None, l=None, close=None, base=22350.0):
    return Candle(
        open =o     if o     is not None else base,
        high =h     if h     is not None else base,
        low  =l     if l     is not None else base,
        close=close if close is not None else base,
    )


class TestStageTimeout(unittest.TestCase):

    def test_timeout_in_breakout_stage(self):
        tracker = StageTracker()
        process_setup_a(c(close=R1+10), tracker, R1, TC)          # breakout
        self.assertEqual(tracker.state, SetupState.BREAKOUT)
        for _ in range(10):                                         # 10 non-pullback candles
            process_setup_a(c(close=R1+5), tracker, R1, TC)
        self.assertEqual(tracker.state, SetupState.IDLE)

    def test_timeout_in_pullback_stage(self):
        tracker = StageTracker()
        process_setup_a(c(close=R1+10), tracker, R1, TC)
        process_setup_a(c(close=R1-10), tracker, R1, TC)
        self.assertEqual(tracker.state, SetupState.PULLBACK)
        for _ in range(10):
            process_setup_a(c(h=R1-50, close=R1-50), tracker, R1, TC)
        self.assertEqual(tracker.state, SetupState.IDLE)

    def test_timeout_in_retest_stage(self):
        tracker = StageTracker()
        process_setup_a(c(close=R1+10), tracker, R1, TC)
        process_setup_a(c(close=R1-10), tracker, R1, TC)
        process_setup_a(c(h=R1-1, close=R1-5), tracker, R1, TC)   # valid retest
        self.assertEqual(tracker.state, SetupState.RETEST)
        for _ in range(10):
            process_setup_a(c(h=R1-10, l=R1-20, close=R1-10), tracker, R1, TC)
        self.assertEqual(tracker.state, SetupState.IDLE)

    def test_counter_resets_on_state_advance(self):
        tracker = StageTracker()
        process_setup_a(c(close=R1+10), tracker, R1, TC)           # breakout → counter=0
        self.assertEqual(tracker.candles_in_state, 0)
        process_setup_a(c(close=R1+5), tracker, R1, TC)            # tick → 1
        process_setup_a(c(close=R1+5), tracker, R1, TC)            # tick → 2
        self.assertEqual(tracker.candles_in_state, 2)
        process_setup_a(c(close=R1-5), tracker, R1, TC)            # pullback → counter resets
        self.assertEqual(tracker.state, SetupState.PULLBACK)
        self.assertEqual(tracker.candles_in_state, 0)

    def test_timeout_setup_b(self):
        tracker = StageTracker()
        process_setup_b(c(close=S1-10), tracker, S1, BC)
        for _ in range(10):
            process_setup_b(c(close=S1-5), tracker, S1, BC)
        self.assertEqual(tracker.state, SetupState.IDLE)

    def test_timeout_setup_c(self):
        tracker = StageTracker()
        process_setup_c(c(close=TC+10), tracker, TC, R1)
        for _ in range(10):
            process_setup_c(c(close=TC+5), tracker, TC, R1)
        self.assertEqual(tracker.state, SetupState.IDLE)

    def test_timeout_setup_d(self):
        tracker = StageTracker()
        process_setup_d(c(close=BC-10), tracker, BC, S1)
        for _ in range(10):
            process_setup_d(c(close=BC-5), tracker, BC, S1)
        self.assertEqual(tracker.state, SetupState.IDLE)


class TestRetestBufferSetupA(unittest.TestCase):
    """Setup A — price must probe R1 from below."""

    def _to_pullback(self):
        t = StageTracker()
        process_setup_a(c(close=R1+10), t, R1, TC)
        process_setup_a(c(close=R1-10), t, R1, TC)
        return t

    def test_exact_touch_accepted(self):
        t = self._to_pullback()
        process_setup_a(c(h=R1, close=R1-5), t, R1, TC)
        self.assertEqual(t.state, SetupState.RETEST)

    def test_within_buffer_accepted(self):
        t = self._to_pullback()
        process_setup_a(c(h=R1-RETEST_BUFFER_PTS, close=R1-5), t, R1, TC)
        self.assertEqual(t.state, SetupState.RETEST)

    def test_one_point_beyond_buffer_rejected(self):
        t = self._to_pullback()
        process_setup_a(c(h=R1-RETEST_BUFFER_PTS-1, close=R1-10), t, R1, TC)
        self.assertEqual(t.state, SetupState.PULLBACK)   # unchanged

    def test_close_above_r1_restarts_to_breakout(self):
        t = self._to_pullback()
        process_setup_a(c(h=R1+5, close=R1+2), t, R1, TC)
        self.assertEqual(t.state, SetupState.BREAKOUT)

    def test_retest_high_stored(self):
        t = self._to_pullback()
        process_setup_a(c(h=R1-1.5, close=R1-5), t, R1, TC)
        self.assertAlmostEqual(t.retest_high, R1-1.5)


class TestRetestBufferSetupB(unittest.TestCase):
    """Setup B — price must probe S1 from above."""

    def _to_pullback(self):
        t = StageTracker()
        process_setup_b(c(close=S1-10), t, S1, BC)
        process_setup_b(c(close=S1+10), t, S1, BC)
        return t

    def test_within_buffer_accepted(self):
        t = self._to_pullback()
        process_setup_b(c(l=S1+RETEST_BUFFER_PTS, close=S1+5), t, S1, BC)
        self.assertEqual(t.state, SetupState.RETEST)

    def test_too_far_rejected(self):
        t = self._to_pullback()
        process_setup_b(c(l=S1+RETEST_BUFFER_PTS+1, close=S1+10), t, S1, BC)
        self.assertEqual(t.state, SetupState.PULLBACK)

    def test_close_below_s1_restarts(self):
        t = self._to_pullback()
        process_setup_b(c(l=S1-5, close=S1-2), t, S1, BC)
        self.assertEqual(t.state, SetupState.BREAKOUT)

    def test_retest_low_stored(self):
        t = self._to_pullback()
        process_setup_b(c(l=S1+2.0, close=S1+5), t, S1, BC)
        self.assertAlmostEqual(t.retest_low, S1+2.0)


class TestRetestBufferSetupC(unittest.TestCase):
    """Setup C — price must probe TC from below."""

    def _to_pullback(self):
        t = StageTracker()
        process_setup_c(c(close=TC+10), t, TC, R1)
        process_setup_c(c(close=TC-10), t, TC, R1)
        return t

    def test_within_buffer_accepted(self):
        t = self._to_pullback()
        process_setup_c(c(h=TC-RETEST_BUFFER_PTS, close=TC-5), t, TC, R1)
        self.assertEqual(t.state, SetupState.RETEST)

    def test_too_far_rejected(self):
        t = self._to_pullback()
        process_setup_c(c(h=TC-RETEST_BUFFER_PTS-1, close=TC-8), t, TC, R1)
        self.assertEqual(t.state, SetupState.PULLBACK)

    def test_close_above_tc_restarts(self):
        t = self._to_pullback()
        process_setup_c(c(h=TC+5, close=TC+2), t, TC, R1)
        self.assertEqual(t.state, SetupState.BREAKOUT)


class TestRetestBufferSetupD(unittest.TestCase):
    """Setup D — price must probe BC from above."""

    def _to_pullback(self):
        t = StageTracker()
        process_setup_d(c(close=BC-10), t, BC, S1)
        process_setup_d(c(close=BC+10), t, BC, S1)
        return t

    def test_within_buffer_accepted(self):
        t = self._to_pullback()
        process_setup_d(c(l=BC+RETEST_BUFFER_PTS, close=BC+5), t, BC, S1)
        self.assertEqual(t.state, SetupState.RETEST)

    def test_too_far_rejected(self):
        t = self._to_pullback()
        process_setup_d(c(l=BC+RETEST_BUFFER_PTS+1, close=BC+8), t, BC, S1)
        self.assertEqual(t.state, SetupState.PULLBACK)

    def test_close_below_bc_restarts(self):
        t = self._to_pullback()
        process_setup_d(c(l=BC-5, close=BC-2), t, BC, S1)
        self.assertEqual(t.state, SetupState.BREAKOUT)


class TestFullHappyPath(unittest.TestCase):

    def test_setup_a_entry_sl_tp(self):
        t = StageTracker()
        process_setup_a(c(close=R1+10), t, R1, TC)
        process_setup_a(c(close=R1-10), t, R1, TC)
        retest_h = R1 - 1
        process_setup_a(c(h=retest_h, close=R1-5), t, R1, TC)
        self.assertEqual(t.state, SetupState.RETEST)
        sig = process_setup_a(c(h=R1-2, l=R1-10, close=TC+5), t, R1, TC)
        self.assertIsNotNone(sig)
        self.assertEqual(sig.setup, "A")
        self.assertAlmostEqual(sig.stop_loss,   retest_h + 3)
        self.assertAlmostEqual(sig.take_profit, TC + 3)
        self.assertEqual(t.state, SetupState.IDLE)

    def test_setup_b_entry_sl_tp(self):
        t = StageTracker()
        process_setup_b(c(close=S1-10), t, S1, BC)
        process_setup_b(c(close=S1+10), t, S1, BC)
        retest_l = S1 + 2
        process_setup_b(c(l=retest_l, close=S1+5), t, S1, BC)
        sig = process_setup_b(c(l=S1+1, h=S1+15, close=BC-5), t, S1, BC)
        self.assertIsNotNone(sig)
        self.assertEqual(sig.setup, "B")
        self.assertAlmostEqual(sig.stop_loss,   retest_l - 3)
        self.assertAlmostEqual(sig.take_profit, BC - 3)

    def test_setup_c_entry_sl_tp(self):
        t = StageTracker()
        process_setup_c(c(close=TC+10), t, TC, R1)
        process_setup_c(c(close=TC-10), t, TC, R1)
        retest_l = TC - 8
        process_setup_c(c(h=TC-1, l=retest_l, close=TC-5), t, TC, R1)
        sig = process_setup_c(c(h=TC+10, l=TC-2, close=TC+5), t, TC, R1)
        self.assertIsNotNone(sig)
        self.assertEqual(sig.setup, "C")
        self.assertAlmostEqual(sig.stop_loss,   retest_l - 3)
        self.assertAlmostEqual(sig.take_profit, R1 - 3)

    def test_setup_d_entry_sl_tp(self):
        t = StageTracker()
        process_setup_d(c(close=BC-10), t, BC, S1)
        process_setup_d(c(close=BC+10), t, BC, S1)
        retest_h = BC + 2
        process_setup_d(c(l=BC+RETEST_BUFFER_PTS, h=retest_h, close=BC+5), t, BC, S1)
        sig = process_setup_d(c(l=BC-10, h=BC+1, close=BC-5), t, BC, S1)
        self.assertIsNotNone(sig)
        self.assertEqual(sig.setup, "D")
        self.assertAlmostEqual(sig.stop_loss,   retest_h + 3)
        self.assertAlmostEqual(sig.take_profit, S1 + 3)

    def test_no_signal_without_guard(self):
        """Setup A: confirm candle close is BELOW TC (guard fails) → no entry."""
        t = StageTracker()
        process_setup_a(c(close=R1+10), t, R1, TC)
        process_setup_a(c(close=R1-10), t, R1, TC)
        process_setup_a(c(h=R1-1, close=R1-5), t, R1, TC)
        # close < TC — guard fails
        sig = process_setup_a(c(h=R1-2, l=R1-10, close=TC-5), t, R1, TC)
        self.assertIsNone(sig)


class TestCPRStrategyCoordinator(unittest.TestCase):

    def test_set_daily_levels_ordering(self):
        strat = CPRStrategy()
        strat.set_daily_levels(22650, 22100, 22500)
        lv = strat.levels
        self.assertGreater(lv["r1"], lv["tc"])
        self.assertGreater(lv["tc"], lv["bc"])
        self.assertGreater(lv["bc"], lv["s1"])

    def test_raises_without_levels(self):
        strat = CPRStrategy()
        try:
            strat.on_candle_close(c())
            self.fail("Expected RuntimeError")
        except RuntimeError:
            pass

    def test_reset_clears_everything(self):
        strat = CPRStrategy()
        strat.set_daily_levels(22650, 22100, 22500)
        strat.trackers["A"].advance(SetupState.BREAKOUT)
        strat.reset()
        for t in strat.trackers.values():
            self.assertEqual(t.state, SetupState.IDLE)
        self.assertEqual(strat.levels, {})


if __name__ == "__main__":
    unittest.main(verbosity=2)
