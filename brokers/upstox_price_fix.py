"""
Fix for Nifty CMP showing wrong/zero price when market is closed.

The Upstox v2 Market Quote API returns:
  - last_price  → live LTP during market hours (9:15–15:30 IST)
  - ohlc.close  → previous session close (always present, even after market close)

When the market is closed, last_price is typically 0 or stale.
The correct CMP to display outside market hours is ohlc.close.

This module provides:
  - get_nifty_cmp()  → always returns correct price regardless of market hours
  - is_market_open() → IST-aware market hours check
  - NiftyQuote       → structured result with price, source label, timestamp
"""

from dataclasses import dataclass
from datetime import datetime, time
import pytz
import logging

logger = logging.getLogger(__name__)

IST = pytz.timezone("Asia/Kolkata")
MARKET_OPEN  = time(9, 15)
MARKET_CLOSE = time(15, 30)

# Upstox v2 instrument key for Nifty 50 index
NIFTY_INSTRUMENT_KEY = "NSE_INDEX|Nifty 50"


@dataclass
class NiftyQuote:
    price: float          # always a valid non-zero price
    source: str           # "live_ltp" | "prev_close" | "ohlc_close"
    timestamp: datetime
    change: float         # absolute change from prev close
    change_pct: float     # % change from prev close
    prev_close: float
    is_market_open: bool


def is_market_open() -> bool:
    """True only during NSE cash market hours on weekdays (IST)."""
    now_ist = datetime.now(IST)
    if now_ist.weekday() >= 5:          # Saturday=5, Sunday=6
        return False
    current_time = now_ist.time()
    return MARKET_OPEN <= current_time <= MARKET_CLOSE


def get_nifty_cmp(access_token: str) -> NiftyQuote:
    """
    Fetch Nifty CMP using Upstox v2 full market quote.
    Always returns a valid price:
      - During market hours  → last_price (live LTP)
      - Outside market hours → ohlc.close (previous session close)

    Raises:
        RuntimeError if the API call fails or returns unexpected data.
    """
    import requests

    url = "https://api.upstox.com/v2/market-quote/quotes"
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Accept": "application/json",
    }
    params = {"instrument_key": NIFTY_INSTRUMENT_KEY}

    resp = requests.get(url, headers=headers, params=params, timeout=10)
    resp.raise_for_status()

    body = resp.json()

    # Upstox v2 wraps in data → <instrument_key>
    quote = body.get("data", {}).get(NIFTY_INSTRUMENT_KEY)
    if not quote:
        raise RuntimeError(
            f"Upstox quote response missing key '{NIFTY_INSTRUMENT_KEY}': {body}"
        )

    return _parse_quote(quote)


def _parse_quote(quote: dict) -> NiftyQuote:
    """
    Parse raw Upstox v2 quote dict into NiftyQuote.

    Upstox v2 full-quote structure (relevant fields):
    {
      "last_price": 24500.0,       ← 0 or stale when market closed
      "ohlc": {
        "open":  24450.0,
        "high":  24600.0,
        "low":   24400.0,
        "close": 24480.0           ← ALWAYS previous session close
      },
      "last_trade_time": "...",
      ...
    }
    """
    last_price  = float(quote.get("last_price", 0) or 0)
    ohlc        = quote.get("ohlc", {})
    prev_close  = float(ohlc.get("close", 0) or 0)
    market_open = is_market_open()

    # ── Choose the correct price ──────────────────────────────────────────────
    if market_open and last_price > 0:
        # Live session: use real-time LTP
        price  = last_price
        source = "live_ltp"
    elif prev_close > 0:
        # Market closed or last_price unavailable: use previous session close.
        # This is what every financial terminal shows as CMP after hours.
        price  = prev_close
        source = "prev_close" if not market_open else "ohlc_close"
    elif last_price > 0:
        # Fallback: use whatever last_price we have even if it looks stale
        price  = last_price
        source = "stale_ltp"
        logger.warning("Nifty CMP: falling back to stale last_price=%.2f", price)
    else:
        raise RuntimeError("Upstox returned zero for both last_price and ohlc.close")
    # ─────────────────────────────────────────────────────────────────────────

    change     = round(price - prev_close, 2) if prev_close > 0 else 0.0
    change_pct = round((change / prev_close) * 100, 2) if prev_close > 0 else 0.0

    return NiftyQuote(
        price=round(price, 2),
        source=source,
        timestamp=datetime.now(IST),
        change=change,
        change_pct=change_pct,
        prev_close=round(prev_close, 2),
        is_market_open=market_open,
    )


# ── Drop-in for upstox_client.py ─────────────────────────────────────────────
# Replace wherever you currently fetch the Nifty price with this function.
# Example existing code that's likely broken:
#
#   BROKEN:
#   price = quote["data"]["NSE_INDEX|Nifty 50"]["last_price"]
#
#   FIXED:
#   from upstox_price_fix import get_nifty_cmp
#   quote = get_nifty_cmp(access_token)
#   price = quote.price          # always correct
#   source = quote.source        # tells you where the price came from
#   change_pct = quote.change_pct
# ─────────────────────────────────────────────────────────────────────────────
