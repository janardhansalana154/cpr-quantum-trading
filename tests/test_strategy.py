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
    
    # Step 2: Candle 3 (within 10 candles) closes BELOW R1 (close = 19585)
    c3 = {"open": 19605.0, "high": 19615.0, "low": 19580.0, "close": 19585.0}
    triggered, details = machine.update(c3, 3, levels)
    assert machine.state == 2
    assert triggered is False
    
    # Step 3: Candle 6 (within 10 candles) retests R1-R2 range.
    # High is 19602.0 (High lies in [19595, 19605]), Close is below R1 (19590.0)
    c6 = {"open": 19580.0, "high": 19602.0, "low": 19575.0, "close": 19590.0}
    triggered, details = machine.update(c6, 6, levels)
    assert machine.state == 3
    assert machine.r_low == 19575.0
    assert triggered is False
    
    # Step 4: Candle 9 (within 10) confirmation breaks. Close below Retest Low (19575.0) -> close is 19570
    c9 = {"open": 19589.0, "high": 19592.0, "low": 19565.0, "close": 19570.0}
    triggered, details = machine.update(c9, 9, levels)
    assert machine.state == 4
    assert machine.c_low == 19565.0
    assert triggered is False
    
    # Step 5: Candle 12 (within 10) entry trigger breaks Confirmation Low (19565.0) -> low is 19560
    # Close of entry candle must be > TC (19500.0) -> close is 19555
    c12 = {"open": 19568.0, "high": 19570.0, "low": 19560.0, "close": 19562.0}
    triggered, details = machine.update(c12, 12, levels)
    assert triggered is True
    assert details["setup_name"] == "SETUP_A"
    assert details["trade_type"] == "SELL"
    assert details["stop_loss"] == 19602.0 + 3.0 # Retest High (19602.0) + SL Buffer (3)
    assert details["take_profit"] == 19500.0 + 3.0 # TC (19500.0) + target buffer (3)
