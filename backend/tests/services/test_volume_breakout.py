from datetime import date, timedelta

import polars as pl
import pytest

from app.services.volume_breakout import (
    BOX_WIDTH_MAX,
    CONSOLIDATION_MAX_DAYS,
    CONSOLIDATION_MIN_DAYS,
    FORWARD_HORIZONS,
    REFERENCE_WINDOW,
    VARIANTS,
    VOLUME_PERCENTILE,
    assert_no_trading_tokens,
    evaluate_volume_breakout,
    resolve_pinned_reader,
)


def _evaluate(**overrides):
    params = {
        "start": date(2026, 1, 1), "end": date(2026, 3, 20),
        "symbols": ["600000.SH"], "pinned_reader": None,
        "pit_universe": None, "calendar": None,
        "oos_start": date(2026, 2, 1), "cost_bps": 10.0,
    }
    params.update(overrides)
    return evaluate_volume_breakout(**params)


def test_missing_capabilities_are_structured_without_fake_implementation_reasons():
    result = _evaluate()
    assert result["status"] == "unavailable"
    assert set(result["unavailable_reasons"]) == {
        "generation_pinned_reader_missing",
        "pit_eligible_universe_missing",
        "versioned_exchange_calendar_missing",
    }
    assert not any("not_implemented" in reason for reason in result["unavailable_reasons"])
    assert result["events"] == []


class _Calendar:
    def __init__(self):
        self.days = [date(2026, 1, 1) + timedelta(days=index) for index in range(80)]

    def version(self):
        return "calendar-test"

    def market_days(self, start, end):
        return [day for day in self.days if start <= day <= end]


class _Universe:
    def as_of(self):
        return date(2026, 3, 20)

    def snapshot_hash(self):
        return "universe-hash"

    def eligible_symbols(self, event_date):
        return ["600000.SH"]


class _Reader:
    def __init__(self, calendar, *, break_direction="up"):
        rows = []
        for index, day in enumerate(calendar.days):
            volume = amount = 100.0
            close, high, low = 10.0, 10.2, 9.8
            if index == 20:
                volume = amount = 1000.0
            if index == 24:
                close = 11.5 if break_direction == "up" else 8.5
                high, low = (11.6, 10.0) if break_direction == "up" else (10.0, 8.4)
            rows.append({
                "symbol": "600000.SH", "date": day,
                "raw_high": high, "raw_low": low, "raw_close": close,
                "volume": volume, "amount": amount,
            })
        self.frame = pl.DataFrame(rows)

    def generation(self):
        return "generation-test"

    def manifest_sha256(self):
        return "a" * 64

    def daily_bars(self, symbol, start, end):
        return self.frame.filter((pl.col("date") >= start) & (pl.col("date") <= end))


def test_full_capabilities_run_frozen_box_and_forward_oos_diagnostics():
    calendar = _Calendar()
    result = _evaluate(
        pinned_reader=_Reader(calendar), pit_universe=_Universe(), calendar=calendar,
    )
    assert result["status"] == "ok"
    event = result["events"][0]
    assert event["variant"] == "up_breakout"
    assert event["event_date"] == "2026-01-21"
    assert event["freeze_date"] == "2026-01-24"
    assert event["confirm_date"] == "2026-01-25"
    assert event["box_high"] == 10.2
    assert event["forward"]["1"]["post_cost_return"] == pytest.approx(
        event["forward"]["1"]["gross_return"] - 0.001
    )
    assert result["research"]["segments"]["is"]["events"] == 1
    assert result["research"]["verdict"] == "rejected"


def test_down_variant_uses_strict_frozen_lower_boundary():
    calendar = _Calendar()
    result = _evaluate(
        pinned_reader=_Reader(calendar, break_direction="down"),
        pit_universe=_Universe(), calendar=calendar,
    )
    assert result["events"][0]["variant"] == "down_breakout"

def test_empty_calendar_is_unavailable_instead_of_index_error():
    class EmptyCalendar(_Calendar):
        def market_days(self, start, end):
            return []

    result = _evaluate(
        pinned_reader=_Reader(_Calendar()),
        pit_universe=_Universe(),
        calendar=EmptyCalendar(),
    )

    assert result["status"] == "unavailable"
    assert result["unavailable_reasons"] == ["versioned_exchange_calendar_empty"]


def test_implicit_symbol_scope_unions_pit_universe_across_request_dates():
    calendar = _Calendar()

    class ChangingUniverse(_Universe):
        def eligible_symbols(self, event_date):
            return ["600000.SH"] if event_date < date(2026, 2, 1) else ["600001.SH"]

    class MultiSymbolReader(_Reader):
        def daily_bars(self, symbol, start, end):
            return (
                super()
                .daily_bars(symbol, start, end)
                .with_columns(pl.lit(symbol).alias("symbol"))
            )

    result = _evaluate(
        symbols=None,
        pinned_reader=MultiSymbolReader(calendar),
        pit_universe=ChangingUniverse(),
        calendar=calendar,
    )

    assert result["status"] == "ok"
    assert result["coverage"]["symbols"] == 2


def test_partial_reader_is_rejected_by_capability_gate():
    class PartialReader:
        def generation(self):
            return "generation-test"

    assert resolve_pinned_reader(PartialReader()) is None
    assert resolve_pinned_reader(object()) is None


def test_frozen_parameter_contract_and_invalid_range():
    assert REFERENCE_WINDOW == 20
    assert VOLUME_PERCENTILE == 0.90
    assert (CONSOLIDATION_MIN_DAYS, CONSOLIDATION_MAX_DAYS) == (3, 15)
    assert BOX_WIDTH_MAX == 0.12
    assert FORWARD_HORIZONS == (1, 5, 10, 20)
    assert VARIANTS == ("up_breakout", "down_breakout")
    with pytest.raises(ValueError, match="start must be <= end"):
        _evaluate(start=date(2026, 2, 1), end=date(2026, 1, 1))


def test_payload_has_no_trading_semantics_keys():
    result = _evaluate()

    def walk(value):
        if isinstance(value, dict):
            for key, child in value.items():
                assert_no_trading_tokens(str(key))
                walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)

    walk(result)
