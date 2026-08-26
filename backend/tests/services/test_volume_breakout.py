from datetime import date

import pytest

from app.services.volume_breakout import (
    BOX_WIDTH_MAX,
    CONSOLIDATION_MAX_DAYS,
    CONSOLIDATION_MIN_DAYS,
    FORWARD_HORIZONS,
    REFERENCE_WINDOW,
    UNIMPLEMENTED_REASONS,
    VARIANTS,
    VOLUME_PERCENTILE,
    assert_no_trading_tokens,
    evaluate_volume_breakout,
    resolve_pinned_reader,
)


def _evaluate(**overrides):
    params = {
        "start": date(2026, 1, 1),
        "end": date(2026, 1, 31),
        "symbols": ["600000.SH"],
        "pinned_reader": None,
        "pit_universe": None,
        "calendar": None,
    }
    params.update(overrides)
    return evaluate_volume_breakout(**params)


def test_missing_reader_and_pit_are_structured_unavailable():
    result = _evaluate()

    assert result["status"] == "unavailable"
    assert set(result["unavailable_reasons"]) == {
        "generation_pinned_reader_missing",
        "pit_eligible_universe_missing",
        "versioned_exchange_calendar_missing",
        *UNIMPLEMENTED_REASONS,
    }
    assert result["events"] == []
    assert result["clusters"] == []
    assert result["coverage"] is None
    assert result["provenance"] == {}


def test_full_capabilities_still_fail_closed_until_implementation():
    class Reader:
        def generation(self):
            return "generation-test"

        def daily_bars(self, symbol, start, end):
            raise AssertionError("must not read bars before implementation")

    class Universe:
        def as_of(self):
            return date(2026, 1, 31)

        def snapshot_hash(self):
            return "hash-test"

        def eligible_symbols(self, event_date):
            return ["600000.SH"]

    class Calendar:
        def version(self):
            return "calendar-test"

        def market_days(self, start, end):
            return []

    result = _evaluate(pinned_reader=Reader(), pit_universe=Universe(), calendar=Calendar())
    assert result["status"] == "unavailable"
    assert result["unavailable_reasons"] == list(UNIMPLEMENTED_REASONS)
    assert result["events"] == []


def test_partial_reader_is_rejected_by_capability_gate():
    class PartialReader:
        def generation(self):
            return "generation-test"

    assert resolve_pinned_reader(PartialReader()) is None
    assert resolve_pinned_reader(object()) is None


def test_frozen_parameter_contract():
    assert REFERENCE_WINDOW == 20
    assert VOLUME_PERCENTILE == 0.90
    assert (CONSOLIDATION_MIN_DAYS, CONSOLIDATION_MAX_DAYS) == (3, 15)
    assert BOX_WIDTH_MAX == 0.12
    assert FORWARD_HORIZONS == (1, 5, 10, 20)
    assert VARIANTS == ("up_breakout", "down_breakout")


def test_invalid_range_rejected():
    with pytest.raises(ValueError, match="start must be <= end"):
        _evaluate(start=date(2026, 2, 1), end=date(2026, 1, 1))


def test_trading_semantics_are_rejected():
    with pytest.raises(ValueError, match="target"):
        assert_no_trading_tokens("target_price")


def test_unavailable_envelope_has_no_trading_keys():
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
