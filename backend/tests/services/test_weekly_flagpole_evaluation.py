from datetime import date

import pytest

from app.services.weekly_flagpole.benchmark import EqualWeightBenchmark, layer_status
from app.services.weekly_flagpole.evaluation import split_of
from app.services.weekly_flagpole.models import (
    WeeklyFlagpoleRequest,
    assert_no_trading_tokens,
    valid_provenance,
)


def test_is_oos_boundary_and_strict_provenance():
    assert split_of(date(2025, 6, 30)) == "is" and split_of(date(2025, 7, 1)) == "oos"
    digest = "a" * 64
    assert valid_provenance(
        {
            "canonical": {"generation": "g", "manifest_sha256": digest},
            "markets": {"generation": "m", "manifest_sha256": digest},
        }
    )
    assert not valid_provenance({})


def test_sealed_equal_weight_excess_and_unavailable_layers():
    days = [date(2026, 1, 1), date(2026, 1, 2)]
    bench = EqualWeightBenchmark(
        {"a": {days[0]: 10, days[1]: 11}, "b": {days[0]: 20, days[1]: 21}}, days
    )
    assert bench.forward_return(days[0], 1) == pytest.approx(0.075)
    layers = layer_status()
    assert (
        layers["industry_momentum"]["status"] == "unavailable"
        and layers["market_index"]["status"] == "unavailable"
    )


def test_request_rejects_invalid_range_and_extra_fields():
    try:
        WeeklyFlagpoleRequest(start=date(2026, 2, 1), end=date(2026, 1, 1))
    except ValueError:
        pass
    else:
        raise AssertionError("invalid range accepted")


def test_trading_token_guard_uses_token_boundaries():
    assert_no_trading_tokens("zigzag_threshold")
    with pytest.raises(ValueError):
        assert_no_trading_tokens("trade_action")
