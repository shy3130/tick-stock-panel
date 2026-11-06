from datetime import date, datetime

import pytest

from app.market_time import CN_TZ


def _row(day="2026-11-06", time="10:00:00"):
    stamp = int(datetime.fromisoformat(f"{day}T{time}").replace(tzinfo=CN_TZ).timestamp() * 1000)
    return {"symbol": "000985.SH", "date": day, "close": 100., "quote_ts": stamp}


@pytest.fixture
def context():
    from app.services.quote_freshness import QuoteFreshness

    return QuoteFreshness(now=datetime(2026, 11, 6, 10, 0, 10, tzinfo=CN_TZ), trading=True)


def test_each_symbol_uses_own_date_not_shared_asset_date(context):
    assert context.describe(_row())["state"] == "live"
    old = context.describe(_row("2026-11-05"))
    assert old["state"] == "stale"
    assert old["data_date"] == "2026-11-05"


def test_intraday_outage_is_delayed_but_value_is_preserved(context):
    row = _row(time="09:55:00")
    status = context.describe(row)
    assert status["state"] == "delayed"
    assert status["quote_ts"] == row["quote_ts"]
    assert row["close"] == 100.


def test_daily_preparation_and_unknown_timestamp_are_never_live(context):
    assert context.describe({**_row(), "prepared_daily": True})["state"] == "historical"
    assert context.describe({"date": "2026-11-06", "close": 100.})["state"] == "unknown"
    assert context.describe({"symbol": "000985.SH"})["state"] == "missing"


@pytest.mark.parametrize("now,trading", [
    (datetime(2026, 11, 7, 10, tzinfo=CN_TZ), False),
    (datetime(2026, 11, 6, 8, tzinfo=CN_TZ), True),
    (datetime(2026, 10, 1, 10, tzinfo=CN_TZ), False),
])
def test_weekend_preopen_and_holiday_do_not_claim_last_close_is_stale(now, trading):
    from app.services.quote_freshness import QuoteFreshness

    row = _row("2026-09-30") if now.month == 10 else _row("2026-11-05")
    assert QuoteFreshness(now=now, trading=trading).describe(row)["state"] == "historical"


def test_lunch_and_after_close_compare_to_session_boundary():
    from app.services.quote_freshness import QuoteFreshness

    lunch = QuoteFreshness(now=datetime(2026, 11, 6, 12, 20, tzinfo=CN_TZ), trading=True)
    assert lunch.describe(_row(time="11:30:00"))["state"] == "historical"
    assert lunch.describe(_row(time="11:00:00"))["state"] == "delayed"
    closed = QuoteFreshness(now=datetime(2026, 11, 6, 17, tzinfo=CN_TZ), trading=True)
    assert closed.describe(_row(time="15:00:00"))["state"] == "historical"
    assert closed.describe(_row("2026-11-05"))["state"] == "stale"


def test_future_or_invalid_timestamp_is_not_live(context):
    assert context.describe(_row(time="15:00:00"))["state"] == "unknown"
    for value in [float("nan"), float("inf"), -1, "invalid"]:
        assert context.describe({**_row(), "quote_ts": value})["state"] == "unknown"


def test_old_timestamp_is_not_hidden_by_current_partition_date(context):
    old = _row("2026-11-05")
    assert context.describe({**old, "date": date(2026, 11, 6)})["state"] == "stale"


@pytest.mark.parametrize("asset,symbol", [("stock", "600519.SH"), ("etf", "159558.SZ"), ("index", "000985.SH")])
def test_restarted_cache_preserves_each_asset_quote_timestamp(tmp_path, asset, symbol):
    import polars as pl

    from app.tickflow.repository import DataStore, KlineRepository

    repo = KlineRepository(DataStore(tmp_path))
    rows = pl.DataFrame([{**_row(), "symbol": symbol, "date": date(2026, 11, 6),
                          "open": 100., "high": 101., "low": 99., "volume": 100., "amount": 10000.}])
    repo.append_enriched_asset(asset, rows)
    # A new repository must recover provenance from existing compatible Parquets.
    restarted = KlineRepository(DataStore(tmp_path))
    frame, day = restarted.get_enriched_latest_asset(asset)
    assert day == date(2026, 11, 6)
    assert "quote_ts" in frame.columns
    assert frame["quote_ts"][0] == rows["quote_ts"][0]
