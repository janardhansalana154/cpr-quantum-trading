"""
Patch for app/main.py — /api/status endpoint.

Replace your existing Nifty price fetch inside the status endpoint with this.
The key change: use get_nifty_cmp() which handles closed-market fallback,
instead of reading last_price directly.

Also adds `price_source` and `market_open` fields to the response so the
frontend can show "CMP (prev close)" vs "CMP (live)" to the user.
"""

from fastapi import APIRouter
from upstox_price_fix import get_nifty_cmp, is_market_open

router = APIRouter()


@router.get("/api/status")
async def get_status(access_token: str):  # inject however your app does auth
    """
    Returns system status including correct Nifty CMP regardless of market hours.
    """
    try:
        quote = get_nifty_cmp(access_token)

        nifty_data = {
            "cmp": quote.price,               # always a valid non-zero price
            "prev_close": quote.prev_close,
            "change": quote.change,
            "change_pct": quote.change_pct,
            "price_source": quote.source,     # "live_ltp" | "prev_close" | "ohlc_close"
            "market_open": quote.is_market_open,
            "price_label": (
                "Live" if quote.is_market_open else "Prev Close"
            ),
        }
    except Exception as e:
        nifty_data = {
            "cmp": None,
            "error": str(e),
            "market_open": is_market_open(),
        }

    return {
        "status": "running",
        "nifty": nifty_data,
        # ... rest of your existing status fields
    }
