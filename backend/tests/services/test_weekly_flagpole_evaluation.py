from datetime import date, timedelta

import pytest

from app.services.weekly_flagpole.benchmark import (
    EqualWeightBenchmark,
    attribution_layers,
    layer_status,
)
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


def test_f5_attribution_requires_explicit_sealed_pit_provenance():
    unavailable = attribution_layers({})
    assert unavailable["industry_momentum"]["status"] == "unavailable"
    available = attribution_layers(
        {
            "industry": {"sealed": True, "as_of": "2026-01-01", "generation": "g"},
            "market": {"sealed": True, "as_of": "2026-01-01", "generation": "m"},
        }
    )
    assert available["industry_momentum"]["status"] == "ok"
    assert available["market_index"]["status"] == "ok"


def test_factor_verdicts_keep_factor_semantics_and_low_sample_fail_closed():
    from app.services.weekly_flagpole.evaluation import _factor_verdicts

    day = date(2025, 7, 1)
    calendar = [day]
    benchmark = EqualWeightBenchmark({"x": {day: 10.0}}, calendar)
    result = _factor_verdicts(
        [],
        calendar,
        benchmark,
        oos_start=day,
        cost_bps=10.0,
        diagnostics={"poles": 0, "failure_records": []},
    )
    assert set(result) == {"F1", "F2", "F3", "F4"}
    assert result["F1"]["verdict"] == "unavailable"
    assert set(result["F2"]["arms"]) == {
        "weekly_reclaim",
        "volume_shrink_restart",
        "flag_low_retest",
    }
    assert all(arm["verdict"] == "unavailable" for arm in result["F2"]["arms"].values())
    assert result["F3"]["verdict"] == "unavailable"
    assert set(result["F4"]["arms"]) == set(result["F2"]["arms"])
    assert all(arm["verdict"] == "unavailable" for arm in result["F4"]["arms"].values())


def test_f3_uses_failure_records_and_missing_records_are_unavailable():
    from app.services.weekly_flagpole.evaluation import _factor_verdicts

    day = date(2025, 7, 1)
    calendar = [day]
    benchmark = EqualWeightBenchmark({"x": {day: 10.0}}, calendar)
    records = [
        {"symbol": f"S{i % 10}", "failure_week": day, "re_established": i % 2 == 0}
        for i in range(30)
    ]
    result = _factor_verdicts(
        [],
        calendar,
        benchmark,
        oos_start=day,
        cost_bps=10.0,
        diagnostics={"poles": 30, "failure_records": records},
    )
    assert result["F3"]["metrics"]["failures"] == 30
    assert result["F3"]["metrics"]["re_established"] == 15
    assert result["F3"]["metrics"]["re_establishment_rate"] == pytest.approx(0.5)
    missing = _factor_verdicts(
        [], calendar, benchmark, oos_start=day, cost_bps=10.0, diagnostics={"failure_records": None}
    )
    assert missing["F3"]["verdict"] == "unavailable"
    assert missing["F3"]["verdict_reasons"] == ["failure_records_unavailable"]


def test_canonical_close_panel_fails_closed_without_valid_values():
    from app.services.weekly_flagpole.service import _canonical_closes

    good, error = _canonical_closes([{"date": date(2026, 1, 1), "close": "10.0"}], "X")
    assert error is None and good[date(2026, 1, 1)] == pytest.approx(10.0)
    bad, error = _canonical_closes([{"date": date(2026, 1, 1), "close": float("nan")}], "X")
    assert bad == {} and error["code"] == "censor_canonical_close_missing"


def test_event_arm_consumes_canonical_forward_return_key():
    from app.services.weekly_flagpole.evaluation import _event_arm

    days = [date(2025, 7, 1) + timedelta(days=i) for i in range(130)]
    benchmark = EqualWeightBenchmark({"m": {day: 100.0 for day in days}}, days)
    events = [
        {
            "symbol": f"S{i % 10}",
            "confirm_date": days[i + 1],
            "variants": ["weekly_reclaim"],
            "forward": {"forward_63d_return": 0.10},
        }
        for i in range(30)
    ]
    arm = _event_arm(events, days, benchmark, days[0], 10.0, 60)
    assert arm["metrics"]["3m_n"] == 30


def test_trading_token_guard_uses_token_boundaries():
    assert_no_trading_tokens("zigzag_threshold")
    with pytest.raises(ValueError):
        assert_no_trading_tokens("trade_action")
