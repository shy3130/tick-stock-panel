from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal, localcontext

import pytest
from tivelo_contracts import ErrorCode, TiveloError

from tivelo_data.adapters.v1 import V1BarAdapter, normalize_ratio


def adapter(**changes):
    value = V1BarAdapter(
        source_id="fixture",
        input_schema="minute_raw_v1",
        price_basis="raw",
        timeframe="1m",
        currency="CNY",
        quantity_unit="share",
        volume_unit="lot",
        resolve_instrument=lambda symbol, day: "stable-security",
        lot_size=lambda instrument, day: Decimal("100"),
        minute_label="end",
        ingested_at=datetime(2025, 1, 3, tzinfo=UTC),
    )
    return replace(value, **changes)


def row(**changes):
    value = dict(
        symbol="600000.SH",
        datetime=datetime(2025, 1, 2, 9, 31),
        open=10.1,
        high=10.5,
        low=10.0,
        close=10.2,
        volume=123.0,
        amount=125460.0,
    )
    value.update(changes)
    return value


@pytest.mark.parametrize("minute", [31, 35])
def test_d02_v1_beijing_datetime_is_localized_without_mutating_legacy_input(minute):
    legacy = row(datetime=datetime(2025, 1, 2, 9, minute))
    result = adapter().convert(legacy, revision_id="1")
    assert legacy["datetime"] == datetime(2025, 1, 2, 9, minute)
    assert legacy["datetime"].tzinfo is None
    assert result.bar_start == datetime(2025, 1, 2, 1, minute - 1, tzinfo=UTC)
    assert result.bar_end == datetime(2025, 1, 2, 1, minute, tzinfo=UTC)
    assert result.available_at == result.ingested_at
    assert adapter().knowledge_basis == "latest_reconstruction"


def test_d04_lots_and_ratio_units_are_explicit():
    assert adapter().convert(row(), revision_id="1").volume == Decimal("12300")
    assert adapter(volume_unit="quantity").convert(row(), revision_id="1").volume == Decimal("123")
    assert normalize_ratio(5, unit="percent") == Decimal("0.05")
    assert normalize_ratio(5, unit="ratio") == Decimal("5")
    with localcontext() as context:
        context.prec = 4
        assert adapter().convert(row(volume="123456789"), revision_id="1").volume == Decimal(
            "12345678900"
        )


@pytest.mark.parametrize(
    "changes", [{"lot_size": None}, {"minute_label": None}, {"minute_label": "guess"}]
)
def test_unknown_units_and_timestamp_labels_fail_closed(changes):
    with pytest.raises(TiveloError):
        adapter(**changes).convert(row(), revision_id="1")


def test_v1_aware_timestamp_is_not_localized_twice():
    with pytest.raises(TiveloError):
        adapter().convert(row(datetime=datetime(2025, 1, 2, 1, 31, tzinfo=UTC)), revision_id="1")


def test_d09_enriched_qfq_is_not_masqueraded_as_hfq_or_incomplete_raw_ohlc():
    def bounds(day):
        return (
            datetime(2025, 1, 2, 1, 30, tzinfo=UTC),
            datetime(2025, 1, 2, 7, tzinfo=UTC),
            "fixture.day",
        )

    with pytest.raises(TiveloError):
        adapter(input_schema="enriched_v1", timeframe="1d", price_basis="hfq", daily_bounds=bounds)
    raw = adapter(
        input_schema="enriched_v1", timeframe="1d", price_basis="raw", daily_bounds=bounds
    )
    with pytest.raises(TiveloError) as raised:
        raw.convert(
            row(date="2025-01-02", raw_close=10.2, raw_high=10.5, raw_low=10), revision_id="1"
        )
    assert raised.value.code == ErrorCode.RAW_PRICE_REQUIRED
    result = raw.convert(
        row(date="2025-01-02", raw_open=10.1, raw_close=10.2, raw_high=10.5, raw_low=10),
        revision_id="1",
    )
    assert result.price_basis == "raw" and result.open == Decimal("10.1")
