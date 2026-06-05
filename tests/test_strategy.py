import pytest
from strategies.nifty_cpr_option_strategy import calculate_cpr_levels, find_trade_signal


def test_cpr_levels_calculation():
    high = 19600.0
    low = 19400.0
    close = 19500.0

    levels = calculate_cpr_levels(high, low, close)

    assert levels.pivot == 19500.0
    assert levels.bc == 19500.0
    assert levels.tc == 19500.0
    assert levels.r1 == 19600.0
    assert levels.s1 == 19400.0


def test_find_trade_signal_bullish_range_reversal():
    current_levels = calculate_cpr_levels(20000.0, 19000.0, 19400.0)
    yesterday_levels = calculate_cpr_levels(19700.0, 18900.0, 19300.0)

    candles = [
        {"open": 19410.0, "high": 19450.0, "low": 19400.0, "close": 19420.0}
        for _ in range(19)
    ]
    candles.append({"open": 19432.0, "high": 19440.0, "low": 19433.33, "close": 19435.0})

    signal = find_trade_signal(
        candles,
        current_levels,
        yesterday_levels,
        average_width=10.0,
        pdh=19600.0,
        pdl=19400.0,
    )

    assert signal is not None
    assert signal.strategy_name == "BULLISH_RANGE_REVERSAL"
    assert signal.trade_type == "BUY"
    assert signal.option_type == "CE"
    assert signal.entry_price == 19435.0
    assert signal.take_profit == 19466.67
    assert signal.market_classification == "wide"
