import pytest
from strategies.cpr_strategy import calculate_cpr_levels, SetupStateMachine, CPRLevels

def test_cpr_levels_calculation():
    # Previous Day's OHLC
    high = 19600.0
    low = 19400.0
    close = 19500.0
    
    levels = calculate_cpr_levels(high, low, close)
    
    # Assert Pivot calculations
    # Pivot = (19600 + 19400 + 19500) / 3 = 19500
    assert levels.pivot == 19500.0
    
    # BC = (19600 + 19400) / 2 = 19500
    assert levels.bc == 19500.0
    
    # TC = Pivot + (Pivot - BC) = 19500 + 0 = 19500
    assert levels.tc == 19500.0
    
    # R1 = 2 * 19500 - 19400 = 19600
    assert levels.r1 == 19600.0
    
    # S1 = 2 * 19500 - 19600 = 19400
    assert levels.s1 == 19400.0

def test_setup_a_state_machine_flow():
    """
    Tests Setup A: R1 -> TC Short
    1. Close above R1
    2. Close below R1 within failWin (failWin=10)
    3. Retest: High between R1-5 and R1+5, close below R1 within retWin (10)
    4. Confirmation: Closes below Retest Low within conWin (10)
    5. Entry: Breaks Confirmation Low within entWin (10) + Close of entry candle > TC (above CPR)
    """
    machine = SetupStateMachine("SETUP_A")
    machine.fail_win = 10
    machine.ret_win = 10
    machine.con_win = 10
    machine.ent_win = 10
    machine.ret_tol = 5.0
    
    levels = CPRLevels(pivot=19500.0, bc=19500.0, tc=19500.0, r1=19600.0, s1=19400.0)
    
    # Step 1: Candle 0 closes ABOVE R1 (close = 19610)
    c0 = {"open": 19590.0, "high": 19620.0, "low": 19580.0, "close": 19610.0}
    triggered, details = machine.update(c0, 0, levels)
    assert machine.state == 1
    assert triggered is False

    # Confirm no timeout on breakout-to-recover: Candle 20 remains broken
    c20 = {"open": 19615.0, "high": 19625.0, "low": 19605.0, "close": 19610.0}
    triggered, details = machine.update(c20, 20, levels)
    assert machine.state == 1
    assert triggered is False

    # Candle 21 recovers, and the setup should still move to recovered state
    c21 = {"open": 19605.0, "high": 19610.0, "low": 19590.0, "close": 19585.0}
    triggered, details = machine.update(c21, 21, levels)
    assert machine.state == 2
    assert triggered is False

    # Step 3: Candle 24 (within 10 candles) retests R1-R2 range.
    # High is 19602.0 (High lies in [19595, 19605]), Close is below R1 (19590.0)
    c24 = {"open": 19580.0, "high": 19602.0, "low": 19575.0, "close": 19590.0}
    triggered, details = machine.update(c24, 24, levels)
    assert machine.state == 3
    assert machine.r_low == 19575.0
    assert triggered is False

    # Step 4: Candle 27 (within 10) confirmation breaks. Close below Retest Low (19575.0) -> close is 19570
    c27 = {"open": 19589.0, "high": 19592.0, "low": 19565.0, "close": 19570.0}
    triggered, details = machine.update(c27, 27, levels)
    assert machine.state == 4
    assert machine.c_low == 19565.0
    assert triggered is False

    # Step 5: Candle 30 (within 10) entry trigger breaks Confirmation Low (19565.0) -> low is 19560
    # Close of entry candle must be > TC (19500.0) -> close is 19555
    c30 = {"open": 19568.0, "high": 19570.0, "low": 19560.0, "close": 19562.0}
    triggered, details = machine.update(c30, 30, levels)
    assert triggered is True
    assert details["setup_name"] == "SETUP_A"
    assert details["trade_type"] == "SELL"
    assert details["stop_loss"] == 19602.0 + 3.0 # Retest High (19602.0) + SL Buffer (3)
    assert details["take_profit"] == 19476.0 # min(1:RR target, TC) = 19476.0
