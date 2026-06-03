import pytest
import brokers.upstox_client as upstox_client
from brokers.upstox_client import UpstoxClient
from datetime import datetime, timezone, timedelta, date


def _make_ist(dt: datetime) -> datetime:
    return dt.astimezone(upstox_client._IST)


@pytest.mark.parametrize(
    "now,expected_expiry",
    [
        (_make_ist(datetime(2026, 6, 2, 9, 0, tzinfo=timezone.utc)), date(2026, 6, 4)),
        (_make_ist(datetime(2026, 6, 4, 9, 0, tzinfo=timezone.utc)), date(2026, 6, 4)),
        (_make_ist(datetime(2026, 6, 4, 11, 0, tzinfo=timezone.utc)), date(2026, 6, 11)),
        (_make_ist(datetime(2026, 6, 5, 9, 0, tzinfo=timezone.utc)), date(2026, 6, 11)),
    ],
)
def test_get_nearest_weekly_expiry_date(monkeypatch, now, expected_expiry):
    class FakeDateTime:
        @staticmethod
        def now(tz=None):
            return now if tz is not None else now.replace(tzinfo=None)

    monkeypatch.setattr(upstox_client, "datetime", FakeDateTime)
    client = UpstoxClient()

    expiry = client._get_nearest_weekly_expiry_date()
    assert expiry == expected_expiry


def test_select_atm_option_uses_weekly_expiry(monkeypatch):
    now = _make_ist(datetime(2026, 6, 2, 10, 0, tzinfo=timezone.utc))

    class FakeDateTime:
        @staticmethod
        def now(tz=None):
            return now if tz is not None else now.replace(tzinfo=None)

    monkeypatch.setattr(upstox_client, "datetime", FakeDateTime)
    client = UpstoxClient()

    option_symbol, strike, opt_type = client.select_atm_option(18392.0, "BUY")
    assert strike == 18400.0
    assert option_symbol.startswith("NIFTY26JUN04")
    assert option_symbol.endswith("CE")
    assert opt_type == "CE"


def test_place_order_uses_paper_ltp(monkeypatch):
    client = UpstoxClient()
    monkeypatch.setattr(client, "get_option_ltp", lambda symbol: 146.7)

    order = client.place_order("NIFTY26JUN18400CE", "BUY", 1, paper=True)

    assert order["status"] == "success"
    assert order["avg_price"] == 146.7
    assert order["message"] == "Paper order processed"
