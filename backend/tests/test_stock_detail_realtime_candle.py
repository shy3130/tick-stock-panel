from datetime import date, datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from app.api import kline


def _request() -> SimpleNamespace:
    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(quote_service=None)))


def test_us_stock_detail_uses_realtime_ohlc_and_previous_daily_close(monkeypatch) -> None:
    from app.data_providers import custom as custom_sources
    from app.services import preferences

    class FixedDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            fixed = datetime(
                2026,
                7,
                20,
                12,
                15,
                tzinfo=ZoneInfo("America/New_York"),
            )
            return fixed.astimezone(tz) if tz is not None else fixed.replace(tzinfo=None)

    monkeypatch.setattr(kline, "datetime", FixedDateTime)
    timestamp = int(
        datetime(2026, 7, 20, 12, 15, tzinfo=ZoneInfo("America/New_York")).timestamp() * 1000
    )

    class RealtimeProvider:
        def get_realtime(self, universes=None, symbols=None):
            assert symbols == ["NBIS.US"]
            return [{
                "symbol": "NBIS.US",
                "last_price": 186.185,
                "prev_close": None,
                "open": 186.57,
                "high": 194.34,
                "low": 179.09,
                "volume": 9_894_191,
                "amount": 1_841_000_000,
                "change_pct": None,
                "timestamp": timestamp,
            }]

    monkeypatch.setattr(preferences, "get_realtime_data_provider", lambda: "clickhouse")
    monkeypatch.setattr(custom_sources, "provider_has_dataset", lambda name, dataset: True)
    monkeypatch.setattr(custom_sources, "get_provider", lambda name: RealtimeProvider())

    rows = [{"date": date(2026, 7, 17), "symbol": "NBIS.US", "close": 177.71}]
    result = kline._maybe_inject_live_candle(
        _request(), "NBIS.US", rows, end_date=date(2026, 7, 21)
    )

    assert result[-1]["date"] == "2026-07-20"
    assert result[-1]["open"] == 186.57
    assert result[-1]["high"] == 194.34
    assert result[-1]["low"] == 179.09
    assert result[-1]["close"] == 186.185
    assert result[-1]["volume"] == 9_894_191
    assert result[-1]["amount"] == 1_841_000_000
    assert result[-1]["change_pct"] == (186.185 - 177.71) / 177.71
    assert result[-1]["is_live"] is True


def test_historical_stock_detail_does_not_read_realtime_provider(monkeypatch) -> None:
    from app.data_providers import custom as custom_sources
    from app.services import preferences

    class RealtimeProvider:
        def get_realtime(self, universes=None, symbols=None):
            raise AssertionError("historical range must not read realtime quotes")

    monkeypatch.setattr(preferences, "get_realtime_data_provider", lambda: "clickhouse")
    monkeypatch.setattr(custom_sources, "provider_has_dataset", lambda name, dataset: True)
    monkeypatch.setattr(custom_sources, "get_provider", lambda name: RealtimeProvider())

    rows = [{"date": date(2026, 7, 17), "symbol": "NBIS.US", "close": 177.71}]
    result = kline._maybe_inject_live_candle(
        _request(), "NBIS.US", rows, end_date=date(2026, 7, 17)
    )

    assert result == rows
