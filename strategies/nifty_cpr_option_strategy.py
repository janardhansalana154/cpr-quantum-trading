import logging
from datetime import date, timedelta
from typing import Any, Dict, List, Literal, Optional, Tuple

from pydantic import BaseModel

logger = logging.getLogger("CPR_System.NiftyCPRStrategy")

# -----------------------------------------------------------------------------
# Strategy model definitions
# -----------------------------------------------------------------------------
class CPRLevels(BaseModel):
    pivot: float
    bc: float
    tc: float
    r1: float
    r2: float
    r3: float
    r4: float
    s1: float
    s2: float
    s3: float
    s4: float
    width: float


class TradeSignal(BaseModel):
    strategy_name: Literal[
        "BULLISH_BREAKOUT",
        "BEARISH_BREAKOUT",
        "BULLISH_RANGE_REVERSAL",
        "BEARISH_RANGE_REVERSAL",
    ]
    trade_type: Literal["BUY", "SELL"]
    option_type: Literal["CE", "PE"]
    entry_price: float
    stop_loss: float
    take_profit: float
    target_price: float
    trigger_level: float
    reason: str
    market_classification: Literal["narrow", "normal", "wide"]


# -----------------------------------------------------------------------------
# CPR / pivot level helpers
# -----------------------------------------------------------------------------

def calculate_cpr_levels(high: float, low: float, close: float) -> CPRLevels:
    """Compute CPR and extended support/resistance levels from previous-day OHLC."""
    pivot = (high + low + close) / 3.0
    midpoint = (high + low) / 2.0
    other_mid = pivot + (pivot - midpoint)
    tc = max(midpoint, other_mid)
    bc = min(midpoint, other_mid)

    # Standard levels
    r1 = (2.0 * pivot) - low
    s1 = (2.0 * pivot) - high
    r2 = pivot + (high - low)
    s2 = pivot - (high - low)
    r3 = high + 2.0 * (pivot - low)
    s3 = low - 2.0 * (high - pivot)
    r4 = r3 + (high - low)
    s4 = s3 - (high - low)
    width = abs(tc - bc)

    return CPRLevels(
        pivot=round(pivot, 2),
        bc=round(bc, 2),
        tc=round(tc, 2),
        r1=round(r1, 2),
        r2=round(r2, 2),
        r3=round(r3, 2),
        r4=round(r4, 2),
        s1=round(s1, 2),
        s2=round(s2, 2),
        s3=round(s3, 2),
        s4=round(s4, 2),
        width=round(width, 2),
    )


def calculate_ema(values: List[float], period: int = 20) -> Optional[float]:
    if len(values) < period:
        return None
    ema = sum(values[:period]) / period
    k = 2.0 / (period + 1)
    for value in values[period:]:
        ema = (value - ema) * k + ema
    return round(ema, 4)


def classify_cpr_width(current_width: float, average_width: float) -> str:
    if average_width <= 0:
        return "normal"
    if current_width <= average_width * 0.90:
        return "narrow"
    if current_width >= average_width * 1.10:
        return "wide"
    return "normal"


def bar_touches_level(bar: Dict[str, float], level: float) -> bool:
    return bar["low"] <= level <= bar["high"]


def find_nearest_target(entry: float, levels: List[float], higher: bool) -> Optional[float]:
    candidates = [level for level in levels if (level > entry if higher else level < entry)]
    if not candidates:
        return None
    return min(candidates) if higher else max(candidates)


def calculate_reward_risk(entry: float, stop_loss: float, target_price: float) -> Optional[float]:
    risk = abs(entry - stop_loss)
    if risk <= 0:
        return None
    reward = abs(target_price - entry)
    return round(reward / risk, 2)


def get_previous_cpr_widths(
    upstox_client: Any,
    reference_date: date,
    lookback: int = 20,
    max_lookback_days: int = 42,
) -> List[float]:
    widths: List[float] = []
    cursor = reference_date
    attempts = 0

    while len(widths) < lookback and attempts < max_lookback_days:
        prev_ohlc = upstox_client.get_previous_day_ohlc_for_date(cursor)
        if prev_ohlc:
            levels = calculate_cpr_levels(prev_ohlc["high"], prev_ohlc["low"], prev_ohlc["close"])
            if not widths or widths[-1] != levels.width:
                widths.append(levels.width)
                cursor = cursor - timedelta(days=1)
            else:
                cursor = cursor - timedelta(days=1)
        else:
            cursor = cursor - timedelta(days=1)
        attempts += 1

    return widths


# -----------------------------------------------------------------------------
# Signal generation helpers
# -----------------------------------------------------------------------------

def _market_open_above_cpr(candles: List[Dict[str, float]], levels: CPRLevels) -> bool:
    if not candles:
        return False
    return candles[0]["open"] > levels.tc


def _market_open_below_cpr(candles: List[Dict[str, float]], levels: CPRLevels) -> bool:
    if not candles:
        return False
    return candles[0]["open"] < levels.bc


def _extract_price_series(candles: List[Dict[str, float]]) -> List[float]:
    return [c["close"] for c in candles if "close" in c]


def _extract_recent_candle(candles: List[Dict[str, float]], offset: int = 1) -> Optional[Dict[str, float]]:
    if len(candles) < offset + 1:
        return None
    return candles[-1 - offset]


def _bullish_breakout_retest(
    candles: List[Dict[str, float]],
    pdh: float,
    levels: CPRLevels,
    entry_price: float,
    ema20: float,
    yesterday_levels: CPRLevels,
) -> Optional[TradeSignal]:
    if entry_price <= levels.tc:
        return None
    if entry_price <= ema20:
        return None
    if levels.pivot <= yesterday_levels.pivot:
        return None
    if not _market_open_above_cpr(candles, levels):
        return None

    breakout_index = None
    for idx, bar in enumerate(candles[:-2]):
        if bar["high"] > pdh and bar["close"] > pdh:
            breakout_index = idx
            break
    if breakout_index is None:
        return None

    retest_index = None
    for idx in range(breakout_index + 1, len(candles) - 1):
        bar = candles[idx]
        if bar_touches_level(bar, pdh):
            retest_index = idx
            break
    if retest_index is None:
        return None

    confirmation = candles[retest_index + 1]
    if confirmation["close"] <= pdh:
        return None
    if confirmation["close"] <= confirmation["open"]:
        return None

    stop_by_candle = confirmation["low"]
    stop_by_premium = round(confirmation["close"] * 0.80, 2)
    stop_loss = min(stop_by_candle, stop_by_premium)

    target = find_nearest_target(confirmation["close"], [levels.r1, levels.r2, levels.r3], higher=True)
    if target is None:
        return None

    rr = calculate_reward_risk(confirmation["close"], stop_loss, target)
    if rr is None or rr < 2.0:
        logger.info(f"Bullish breakout rejected: RR={rr} < 2.0")
        return None

    return TradeSignal(
        strategy_name="BULLISH_BREAKOUT",
        trade_type="BUY",
        option_type="CE",
        entry_price=round(confirmation["close"], 2),
        stop_loss=round(stop_loss, 2),
        take_profit=round(target, 2),
        target_price=round(target, 2),
        trigger_level=pdh,
        reason="Bullish breakout retest confirmed above PDH.",
        market_classification="narrow",
    )


def _bearish_breakout_retest(
    candles: List[Dict[str, float]],
    pdl: float,
    levels: CPRLevels,
    entry_price: float,
    ema20: float,
    yesterday_levels: CPRLevels,
) -> Optional[TradeSignal]:
    if entry_price >= levels.bc:
        return None
    if entry_price >= ema20:
        return None
    if levels.pivot >= yesterday_levels.pivot:
        return None
    if not _market_open_below_cpr(candles, levels):
        return None

    breakout_index = None
    for idx, bar in enumerate(candles[:-2]):
        if bar["low"] < pdl and bar["close"] < pdl:
            breakout_index = idx
            break
    if breakout_index is None:
        return None

    retest_index = None
    for idx in range(breakout_index + 1, len(candles) - 1):
        bar = candles[idx]
        if bar_touches_level(bar, pdl):
            retest_index = idx
            break
    if retest_index is None:
        return None

    confirmation = candles[retest_index + 1]
    if confirmation["close"] >= pdl:
        return None
    if confirmation["close"] >= confirmation["open"]:
        return None

    stop_by_candle = confirmation["high"]
    stop_by_premium = round(confirmation["close"] * 1.20, 2)
    stop_loss = max(stop_by_candle, stop_by_premium)

    target = find_nearest_target(confirmation["close"], [levels.s1, levels.s2, levels.s3], higher=False)
    if target is None:
        return None

    rr = calculate_reward_risk(confirmation["close"], stop_loss, target)
    if rr is None or rr < 2.0:
        logger.info(f"Bearish breakout rejected: RR={rr} < 2.0")
        return None

    return TradeSignal(
        strategy_name="BEARISH_BREAKOUT",
        trade_type="SELL",
        option_type="PE",
        entry_price=round(confirmation["close"], 2),
        stop_loss=round(stop_loss, 2),
        take_profit=round(target, 2),
        target_price=round(target, 2),
        trigger_level=pdl,
        reason="Bearish breakout retest confirmed below PDL.",
        market_classification="narrow",
    )


def _bullish_range_reversal(
    candles: List[Dict[str, float]],
    levels: CPRLevels,
    entry_price: float,
    ema20: float,
) -> Optional[TradeSignal]:
    if entry_price <= ema20:
        return None
    if not levels.width or levels.width <= 0:
        return None

    latest = candles[-1]
    if latest["close"] <= latest["open"]:
        return None

    support_levels = [("BC", levels.bc), ("S1", levels.s1), ("S2", levels.s2)]
    touched = None
    for _, level in support_levels:
        if bar_touches_level(latest, level):
            touched = level
            break
    if touched is None:
        return None

    if latest["close"] <= touched:
        return None

    stop_loss = round(min(latest["low"], touched - 0.5), 2)
    target = find_nearest_target(latest["close"], [levels.pivot, levels.tc], higher=True)
    if target is None:
        return None

    rr = calculate_reward_risk(latest["close"], stop_loss, target)
    if rr is None or rr < 2.0:
        logger.info(f"Range reversal bullish rejected: RR={rr} < 2.0")
        return None

    return TradeSignal(
        strategy_name="BULLISH_RANGE_REVERSAL",
        trade_type="BUY",
        option_type="CE",
        entry_price=round(latest["close"], 2),
        stop_loss=round(stop_loss, 2),
        take_profit=round(target, 2),
        target_price=round(target, 2),
        trigger_level=round(touched, 2),
        reason="Wide CPR bullish reversal after support touch.",
        market_classification="wide",
    )


def _bearish_range_reversal(
    candles: List[Dict[str, float]],
    levels: CPRLevels,
    entry_price: float,
    ema20: float,
) -> Optional[TradeSignal]:
    if entry_price >= ema20:
        return None
    if not levels.width or levels.width <= 0:
        return None

    latest = candles[-1]
    if latest["close"] >= latest["open"]:
        return None

    resistance_levels = [("TC", levels.tc), ("R1", levels.r1), ("R2", levels.r2)]
    touched = None
    for _, level in resistance_levels:
        if bar_touches_level(latest, level):
            touched = level
            break
    if touched is None:
        return None

    if latest["close"] >= touched:
        return None

    stop_loss = round(max(latest["high"], touched + 0.5), 2)
    target = find_nearest_target(latest["close"], [levels.pivot, levels.bc], higher=False)
    if target is None:
        return None

    rr = calculate_reward_risk(latest["close"], stop_loss, target)
    if rr is None or rr < 2.0:
        logger.info(f"Range reversal bearish rejected: RR={rr} < 2.0")
        return None

    return TradeSignal(
        strategy_name="BEARISH_RANGE_REVERSAL",
        trade_type="SELL",
        option_type="PE",
        entry_price=round(latest["close"], 2),
        stop_loss=round(stop_loss, 2),
        take_profit=round(target, 2),
        target_price=round(target, 2),
        trigger_level=round(touched, 2),
        reason="Wide CPR bearish reversal after resistance touch.",
        market_classification="wide",
    )


def find_trade_signal(
    candles: List[Dict[str, float]],
    current_levels: CPRLevels,
    yesterday_levels: CPRLevels,
    average_width: float,
    pdh: float,
    pdl: float,
) -> Optional[TradeSignal]:
    closes = _extract_price_series(candles)
    ema20 = calculate_ema(closes, 20)
    if ema20 is None:
        logger.debug("Not enough bars to compute 20 EMA.")
        return None

    market_class = classify_cpr_width(current_levels.width, average_width)
    logger.info(
        f"Market classification: {market_class} | CPR width={current_levels.width:.2f} "
        f"avg={average_width:.2f} | pivot={current_levels.pivot:.2f}"
    )

    if market_class == "narrow":
        signal = _bullish_breakout_retest(candles, pdh, current_levels, candles[-1]["close"], ema20, yesterday_levels)
        if signal:
            return signal
        return _bearish_breakout_retest(candles, pdl, current_levels, candles[-1]["close"], ema20, yesterday_levels)

    if market_class == "wide":
        signal = _bullish_range_reversal(candles, current_levels, candles[-1]["close"], ema20)
        if signal:
            return signal
        return _bearish_range_reversal(candles, current_levels, candles[-1]["close"], ema20)

    return None
