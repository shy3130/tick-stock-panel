from datetime import date, timedelta

import pytest

from app.services.hold_firm_patterns.models import Bar
from app.services.negative_exclusion import (
    CAPABILITY_AVAILABLE,
    CAPABILITY_V1_UNVERIFIED,
    CAPABILITY_V3_NO_PIT_SOURCE,
    CENSOR_PIT_FACT_MISSING,
    CLASS_CAPABILITIES,
    CLASS_V1,
    CLASS_V2,
    CLASS_V3,
    CLASS_V4,
    CLASS_V5,
    ClassSignal,
    MIN_ACTIVE_SAMPLES,
    ObservationRow,
    PitNegativeFact,
    SignalState,
    VERDICT_INSUFFICIENT_SAMPLES,
    VERDICT_ACCEPTED,
    VERDICT_REJECTED,
    VERDICT_UNAVAILABLE_CAPABILITY,
    aggregate_exclusion,
    capability_for,
    detect_v2,
    detect_v4_series,
    detect_v5_series,
    require_available_class,
    v5_conditions,
)


def _bar(i, close=100.0, low=None, volume=100.0):
    low = close if low is None else low
    return Bar(
        "AAA.SZ",
        date(2024, 1, 1) + timedelta(days=i),
        close,
        close,
        low,
        close,
        close,
        close,
        low,
        close,
        volume,
        1_000.0,
    )


def _signals(v2=SignalState.INACTIVE, v4=SignalState.INACTIVE, v5=SignalState.INACTIVE):
    return {
        key: ClassSignal(state, "missing" if state is SignalState.CENSORED else None)
        for key, state in ((CLASS_V2, v2), (CLASS_V4, v4), (CLASS_V5, v5))
    }


def test_capabilities_are_fixed_and_fail_closed():
    assert CLASS_CAPABILITIES == {
        CLASS_V1: CAPABILITY_V1_UNVERIFIED,
        CLASS_V2: CAPABILITY_AVAILABLE,
        CLASS_V3: CAPABILITY_V3_NO_PIT_SOURCE,
        CLASS_V4: CAPABILITY_AVAILABLE,
        CLASS_V5: CAPABILITY_AVAILABLE,
    }
    for class_id in (CLASS_V2, CLASS_V4, CLASS_V5):
        require_available_class(class_id)
    for class_id in (CLASS_V1, CLASS_V3):
        with pytest.raises(ValueError):
            require_available_class(class_id)
    with pytest.raises(ValueError):
        capability_for("unknown")


def test_v2_consumes_same_day_canonical_st_fact_and_censors_missing_fact():
    assert detect_v2(PitNegativeFact(is_st=True)).state is SignalState.ACTIVE
    assert detect_v2(PitNegativeFact(is_st=False)).state is SignalState.INACTIVE
    missing = detect_v2(None)
    assert missing.state is SignalState.CENSORED
    assert missing.reason == CENSOR_PIT_FACT_MISSING


def test_v4_requires_five_consecutive_days_and_is_prefix_closed():
    closes = [100.0 - i for i in range(40)]
    full = detect_v4_series(closes)
    assert full[23].state is SignalState.INACTIVE
    assert full[24].state is SignalState.ACTIVE
    assert tuple(full[:30]) == detect_v4_series(closes[:30])


def test_v5_requires_all_three_conditions_and_each_ablation_fails():
    base = [_bar(i) for i in range(79)] + [_bar(79, close=60.0, low=60.0, volume=300.0)]
    assert v5_conditions(base, 79).all_hold
    assert detect_v5_series(base)[79].state is SignalState.ACTIVE

    shallow = base[:-1] + [_bar(79, close=90.0, low=90.0, volume=300.0)]
    no_break = base[:59] + [_bar(i, low=55.0) for i in range(59, 79)] + [base[-1]]
    no_surge = base[:-1] + [_bar(79, close=60.0, low=60.0, volume=150.0)]
    assert not v5_conditions(shallow, 79).deep_drawdown
    assert not v5_conditions(no_break, 79).broke_platform
    assert not v5_conditions(no_surge, 79).volume_surge
    assert detect_v5_series(shallow)[79].state is SignalState.INACTIVE
    assert detect_v5_series(no_break)[79].state is SignalState.INACTIVE
    assert detect_v5_series(no_surge)[79].state is SignalState.INACTIVE
    assert detect_v5_series(base[:59])[-1].state is SignalState.CENSORED


def test_aggregation_reports_symmetric_stats_and_switches():
    start = date(2024, 1, 1)
    returns = (-0.10, 0.02, -0.10, 0.02, 0.03, 0.50)
    states = (SignalState.ACTIVE,) * 4 + (SignalState.INACTIVE, SignalState.CENSORED)
    rows = [
        ObservationRow(
            "AAA.SZ",
            start + timedelta(days=i),
            r,
            {
                CLASS_V2: ClassSignal(s, "missing" if s is SignalState.CENSORED else None),
                CLASS_V4: ClassSignal(SignalState.INACTIVE),
                CLASS_V5: ClassSignal(SignalState.INACTIVE),
            },
        )
        for i, (r, s) in enumerate(zip(returns, states))
    ]
    result = aggregate_exclusion(rows, enabled_classes=(CLASS_V2,))
    stats = result.classes[CLASS_V2]
    assert stats.evaluable_days == 5
    assert stats.censored_days == 1
    assert stats.coverage == pytest.approx(4 / 5)
    assert stats.missed_rebounds.count == 2
    assert stats.avoided_declines.count == 2
    assert stats.net_benefit == pytest.approx(0.16)
    assert stats.verdict == VERDICT_INSUFFICIENT_SAMPLES
    assert result.combined.verdict == VERDICT_INSUFFICIENT_SAMPLES
    assert result.promoted is False
    assert stats.portfolio is not None
    expected_full = (1 - 0.10) * (1 + 0.02) * (1 - 0.10) * (1 + 0.02) * (1 + 0.03) - 1
    assert stats.portfolio.full_return == pytest.approx(expected_full)

    many = [
        ObservationRow("AAA.SZ", start + timedelta(days=i), r, _signals(v2=SignalState.ACTIVE))
        for i, r in enumerate((-0.10, 0.02, -0.10, 0.02) * 8)
    ]
    assert (
        aggregate_exclusion(many, enabled_classes=(CLASS_V2,)).classes[CLASS_V2].verdict
        == VERDICT_ACCEPTED
    )


def test_unavailable_classes_never_become_available_and_no_stable_gain_rejected():
    row = ObservationRow("AAA.SZ", date(2024, 1, 1), 0.1, _signals())
    result = aggregate_exclusion([row], enabled_classes=(CLASS_V2,))
    assert result.classes[CLASS_V1].verdict == VERDICT_UNAVAILABLE_CAPABILITY
    assert result.classes[CLASS_V3].verdict == VERDICT_UNAVAILABLE_CAPABILITY
    with pytest.raises(ValueError):
        aggregate_exclusion([row], enabled_classes=(CLASS_V1,))
    rows = [
        ObservationRow(
            "AAA.SZ", date(2024, 2, 1) + timedelta(days=i), 0.02, _signals(v2=SignalState.ACTIVE)
        )
        for i in range(MIN_ACTIVE_SAMPLES)
    ]
    assert (
        aggregate_exclusion(rows, enabled_classes=(CLASS_V2,)).classes[CLASS_V2].verdict
        == VERDICT_REJECTED
    )


def test_combined_is_union_of_enabled_available_classes():
    start = date(2024, 3, 1)
    rows = [
        ObservationRow(
            "AAA.SZ",
            start + timedelta(days=i),
            -0.05,
            _signals(
                v2=SignalState.ACTIVE if i == 0 else SignalState.INACTIVE,
                v4=SignalState.ACTIVE if i == 1 else SignalState.INACTIVE,
            ),
        )
        for i in range(2)
    ]
    both = aggregate_exclusion(rows, enabled_classes=(CLASS_V2, CLASS_V4))
    only_v2 = aggregate_exclusion(rows, enabled_classes=(CLASS_V2,))
    assert both.combined.active_days == 2
    assert only_v2.combined.active_days == 1
    assert both.enabled_classes == (CLASS_V2, CLASS_V4)
    assert both.promoted is False


def test_combined_censors_unknown_component_unless_another_component_is_active():
    start = date(2024, 4, 1)
    unknown = ObservationRow(
        "AAA.SZ",
        start,
        -0.1,
        _signals(v2=SignalState.CENSORED, v4=SignalState.INACTIVE),
    )
    active = ObservationRow(
        "AAA.SZ",
        start + timedelta(days=1),
        -0.1,
        _signals(v2=SignalState.CENSORED, v4=SignalState.ACTIVE),
    )
    result = aggregate_exclusion(
        [unknown, active],
        enabled_classes=(CLASS_V2, CLASS_V4),
    )
    assert result.combined.censored_days == 1
    assert result.combined.evaluable_days == 1
    assert result.combined.active_days == 1


def test_missing_or_nonfinite_observation_inputs_fail_closed():
    with pytest.raises(ValueError):
        aggregate_exclusion([ObservationRow("AAA.SZ", date(2024, 1, 1), float("nan"), _signals())])
    with pytest.raises(ValueError):
        aggregate_exclusion(
            [
                ObservationRow(
                    "AAA.SZ", date(2024, 1, 1), 0.0, {CLASS_V2: ClassSignal(SignalState.INACTIVE)}
                )
            ]
        )
