from datetime import date, datetime

import polars as pl
import pytest

from app.services import kline_sync


def test_fetch_minute_single_uses_configured_minute_pipeline(monkeypatch) -> None:
    expected = pl.DataFrame({
        "symbol": ["NBIS.US"],
        "datetime": [datetime(2026, 7, 17, 9, 30)],
        "open": [177.0],
        "high": [178.0],
        "low": [176.0],
        "close": [177.5],
        "volume": [100.0],
        "amount": [17750.0],
    })
    captured: dict[str, object] = {}

    def configured_pipeline(symbols, start_time=None, end_time=None, **_kwargs):
        captured.update({
            "symbols": symbols,
            "start_time": start_time,
            "end_time": end_time,
        })
        return expected

    monkeypatch.setattr(kline_sync, "sync_minute_batch", configured_pipeline)
    monkeypatch.setattr(
        kline_sync,
        "get_client",
        lambda: pytest.fail("single-minute lookup bypassed the configured provider"),
    )

    result = kline_sync.fetch_minute_single("NBIS.US", date(2026, 7, 17))

    assert result.equals(expected)
    assert captured == {
        "symbols": ["NBIS.US"],
        "start_time": datetime(2026, 7, 17, 9, 25),
        "end_time": datetime(2026, 7, 17, 15, 5),
    }
