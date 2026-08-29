"""F2 breakout_pullback detector tests.

Pins the frozen contract of ``docs/ISSUE-38/final-design.md`` §5(F2) and §4:
platform/breakout/pullback thresholds (inclusive/exclusive as designed), the
fixed day-5 selection landmark (early pullback never executes early),
selection-window censoring on truncation, qualified vs not_selected mutual
exclusivity, OLS/fake-breakout diagnostics that never enter the mask, and
truncation invariance. Adjusted OHLC/volume structure only; the detector must
never touch market facts (guarded by a raising stub).
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from app.services.hold_firm_patterns import models
from app.services.hold_firm_patterns.breakout_pullback import (
    BreakoutPullbackDetector,
)
from app.services.hold_firm_patterns.models import CensorReason, LandmarkKind

SYMBOL = "600519.SH"
START = date(2024, 1, 2)
LEVEL = 10.8
BREAKOUT_VOLUME = 160.0
PB_LOW = LEVEL * 1.01
PB_VOLUME = 0.70 * BREAKOUT_VOLUME
BREAKOUT_DAY = START + timedelta(days=20)
A_LANDMARK_DAY = START + timedelta(days=25)


def _day(offset: int) -> date:
    return START + timedelta(days=offset)


def _calendar(days: int = 60) -> tuple[date, ...]:
    return tuple(_day(offset) for offset in range(days))


def _bar(day, o, h, l, c, v):
    """Bar whose raw quotes deliberately differ from adjusted values."""

    def raw(x: float) -> float:
        return round(x * 1.37 + 0.11, 6)

    return models.Bar(
        symbol=SYMBOL,
        date=day,
        research_open_adj=o,
        research_high_adj=h,
        research_low_adj=l,
        research_close_adj=c,
        quote_open_raw=raw(o),
        quote_high_raw=raw(h),
        quote_low_raw=raw(l),
        quote_close_raw=raw(c),
        volume=v,
        amount=v * 100.0,
    )


def _bars(specs) -> tuple[models.Bar, ...]:
    return tuple(_bar(_day(offset), **spec) for offset, spec in enumerate(specs))


def _platform_day(**over):
    spec = dict(o=10.0, h=10.8, l=9.6, c=10.0, v=100.0)
    spec.update(over)
    return spec


def _breakout_day(**over):
    spec = dict(o=11.0, h=11.6, l=10.9, c=11.5, v=BREAKOUT_VOLUME)
    spec.update(over)
    return spec


def _neutral_day(**over):
    spec = dict(o=11.4, h=11.8, l=11.2, c=11.6, v=120.0)
    spec.update(over)
    return spec


def _pullback_day(**over):
    spec = dict(o=11.0, h=11.3, l=PB_LOW, c=11.0, v=PB_VOLUME)
    spec.update(over)
    return spec


def _scenario(*post_days):
    """Platform(20) + breakout day + the given post-breakout days."""
    specs = [_platform_day() for _ in range(20)]
    specs.append(_breakout_day())
    specs.extend(post_days)
    return _bars(specs)


class _UnusedFacts:
    """Guard: F2 detection must never read market facts."""

    def identity(self):
        raise AssertionError("F2 detector must not read market facts identity")

    def row(self, symbol, day):
        raise AssertionError("F2 detector must not read market facts rows")


def _detect(bars, calendar=None):
    return BreakoutPullbackDetector().detect(
        SYMBOL, bars, _UnusedFacts(), calendar if calendar is not None else _calendar()
    )


def _classify(events):
    out = {}
    for event in events:
        if event.evidence is not None:
            out[event.anchor_date] = "qualified" if event.evidence.qualified else "not_selected"
        else:
            out[event.anchor_date] = event.censor
    return out


def _qualified_event(events):
    assert len(events) == 1
    event = events[0]
    assert event.censor is None and event.evidence is not None
    return event


def test_factor_id_matches_models_and_signature():
    detector = BreakoutPullbackDetector()
    assert detector.factor_id == "breakout_pullback"
    assert detector.factor_id == models.FACTOR_IDS[1]
    events = _detect(_scenario(*[_neutral_day() for _ in range(5)]))
    assert isinstance(events, tuple)


def test_platform_ratio_boundary_is_strict():
    # (max high - min low) / min low == 0.15 exactly -> rejected.
    exact = [_platform_day(h=11.5, l=10.0) for _ in range(20)]
    exact.append(_breakout_day(o=11.6, h=11.8, l=11.2, c=11.6))
    exact.extend(_neutral_day() for _ in range(5))
    assert _detect(_bars(exact)) == ()
    # 0.149 -> platform accepted, parent event emitted.
    below = [_platform_day(h=11.49, l=10.0) for _ in range(20)]
    below.append(_breakout_day(o=11.5, h=11.8, l=11.2, c=11.6))
    below.extend(_neutral_day() for _ in range(5))
    events = _detect(_bars(below))
    assert len(events) == 1
    assert events[0].anchor_date == BREAKOUT_DAY


def test_breakout_close_must_strictly_exceed_level():
    # close == level -> no event.
    equal = [_platform_day() for _ in range(20)]
    equal.append(_breakout_day(o=10.8, h=10.9, l=10.7, c=LEVEL))
    equal.extend(_neutral_day() for _ in range(5))
    assert all(event.anchor_date != BREAKOUT_DAY for event in _detect(_bars(equal)))
    # close > level -> event.
    above = [_platform_day() for _ in range(20)]
    above.append(_breakout_day(c=10.9))
    above.extend(_neutral_day() for _ in range(5))
    events = _detect(_bars(above))
    assert len(events) == 1
    assert events[0].anchor_date == BREAKOUT_DAY


def test_volume_breakout_threshold_inclusive_at_1_5x():
    # exactly 1.50x prior-20-day mean (100.0) -> inclusive pass.
    events = _detect(
        _bars(
            [_platform_day() for _ in range(20)]
            + [_breakout_day(v=150.0)]
            + [_neutral_day() for _ in range(5)]
        )
    )
    assert len(events) == 1
    # 1.499x remains in the observable parent pool but is not selected.
    below = _detect(
        _bars(
            [_platform_day() for _ in range(20)]
            + [_breakout_day(v=149.9)]
            + [_neutral_day() for _ in range(5)]
        )
    )
    assert len(below) == 1
    assert below[0].evidence is not None
    assert below[0].evidence.qualified is False


def test_zero_prior_mean_volume_remains_not_selected_parent():
    events = _detect(
        _bars(
            [_platform_day(v=0.0) for _ in range(20)]
            + [_breakout_day(v=100.0)]
            + [_pullback_day(), *[_neutral_day() for _ in range(4)]]
        )
    )
    assert len(events) == 1
    assert events[0].evidence is not None
    assert events[0].evidence.qualified is False
    assert events[0].evidence.values["breakout"]["volume_ratio"] is None


def test_pullback_boundaries_are_inclusive():
    # low == level*1.01, volume == 0.70x breakout -> all at exact bounds, hit.
    event = _qualified_event(
        _detect(_scenario(_pullback_day(), *[_neutral_day() for _ in range(4)]))
    )
    assert event.evidence.qualified is True
    assert event.evidence.values["pullback"]["day_index"] == 1
    # close == level exactly (low well below, so the bar stays sane) -> hit.
    event = _qualified_event(
        _detect(
            _scenario(
                _pullback_day(o=LEVEL * 0.995, h=LEVEL * 1.005, l=LEVEL * 0.99, c=LEVEL),
                *[_neutral_day() for _ in range(4)],
            )
        )
    )
    assert event.evidence.qualified is True
    assert event.evidence.values["pullback"]["day_index"] == 1


def test_each_pullback_condition_is_required_first_hit_recorded():
    # low above level*1.01 -> day 1 fails, day 2 hits.
    events = _detect(
        _scenario(
            _pullback_day(l=LEVEL * 1.01 + 0.001),
            _pullback_day(),
            _neutral_day(),
            _neutral_day(),
            _neutral_day(),
        )
    )
    assert _qualified_event(events).evidence.values["pullback"]["day_index"] == 2
    # close strictly below level -> day 1 fails, day 2 hits.
    events = _detect(
        _scenario(
            _pullback_day(o=LEVEL * 0.995, h=LEVEL * 1.005, l=LEVEL * 0.99, c=LEVEL - 0.001),
            _pullback_day(),
            _neutral_day(),
            _neutral_day(),
            _neutral_day(),
        )
    )
    assert _qualified_event(events).evidence.values["pullback"]["day_index"] == 2
    # volume above 0.70x breakout -> day 1 fails, day 2 hits.
    events = _detect(
        _scenario(
            _pullback_day(v=BREAKOUT_VOLUME * 0.70 * 1.01),
            _pullback_day(),
            _neutral_day(),
            _neutral_day(),
            _neutral_day(),
        )
    )
    assert _qualified_event(events).evidence.values["pullback"]["day_index"] == 2
    # two consecutive pullback-shaped days -> only the first is recorded.
    events = _detect(
        _scenario(_pullback_day(), _pullback_day(), *[_neutral_day() for _ in range(3)])
    )
    assert _qualified_event(events).evidence.values["pullback"]["day_index"] == 1


def test_day5_landmark_fixed_and_early_pullback_never_executes_early():
    # Pullback already hit on day 1, but bars end on day 3: the event must be
    # censored, not qualified early.
    truncated = _scenario(_pullback_day(), _neutral_day(), _neutral_day())
    events = _detect(truncated)
    assert len(events) == 1
    event = events[0]
    assert event.evidence is None
    assert event.censor is CensorReason.SELECTION_WINDOW_INCOMPLETE
    assert event.anchor_date == BREAKOUT_DAY
    assert event.landmark is not None
    assert event.landmark.kind is LandmarkKind.BREAKOUT_DAY5_CLOSE
    assert event.landmark == models.Landmark(
        LandmarkKind.BREAKOUT_DAY5_CLOSE, BREAKOUT_DAY, A_LANDMARK_DAY
    )
    full = _scenario(_pullback_day(), *[_neutral_day() for _ in range(4)])
    event = _qualified_event(_detect(full))
    assert event.evidence.qualified is True
    assert event.landmark == event.landmark.__class__(
        LandmarkKind.BREAKOUT_DAY5_CLOSE, BREAKOUT_DAY, A_LANDMARK_DAY
    )
    assert event.evidence.values["landmark"]["landmark_date"] == A_LANDMARK_DAY.isoformat()


def test_complete_window_without_pullback_is_not_selected():
    events = _detect(_scenario(*[_neutral_day() for _ in range(5)]))
    assert _classify(events) == {BREAKOUT_DAY: "not_selected"}
    event = events[0]
    assert event.censor is None
    assert event.evidence.qualified is False
    assert event.evidence.values["pullback"]["hit"] is False
    assert "day_index" not in event.evidence.values["pullback"]


def test_evidence_carries_thresholds_and_actual_anchors():
    event = _qualified_event(
        _detect(_scenario(_pullback_day(), *[_neutral_day() for _ in range(4)]))
    )
    values = event.evidence.values
    platform = values["platform"]
    assert platform["range_ratio_threshold_max"] == 0.15
    assert platform["range_ratio"] == (LEVEL - 9.6) / 9.6
    assert platform["high_adj"] == LEVEL
    assert platform["low_adj"] == 9.6
    breakout = values["breakout"]
    assert breakout["date"] == BREAKOUT_DAY.isoformat()
    assert breakout["level_adj"] == LEVEL
    assert breakout["volume"] == BREAKOUT_VOLUME
    assert breakout["prior_20d_mean_volume"] == 100.0
    assert breakout["volume_ratio_threshold_min"] == 1.5
    assert breakout["volume_ratio"] == 1.6
    pullback = values["pullback"]
    assert pullback["level_adj"] == LEVEL
    assert pullback["low_level_ratio_threshold_max"] == 1.01
    assert pullback["volume_ratio_threshold_max"] == 0.70
    assert pullback["day_index"] == 1
    assert pullback["date"] == _day(21).isoformat()
    assert pullback["low_level_ratio"] == PB_LOW / LEVEL
    assert pullback["volume_vs_breakout_ratio"] == PB_VOLUME / BREAKOUT_VOLUME
    landmark = values["landmark"]
    assert landmark["kind"] == "breakout_day5_close"
    assert landmark["anchor_date"] == BREAKOUT_DAY.isoformat()
    assert landmark["landmark_date"] == A_LANDMARK_DAY.isoformat()
    assert values["definition_version"] == "v1"
    diagnostics = values["diagnostics"]
    assert "ols_log_volume_slope" in diagnostics
    assert diagnostics["fake_breakout"]["window_days"] == 5


def test_platform_level_and_range_use_high_low_adj():
    # Breakout close above every platform close but below the platform high:
    # level is max(high_adj), so no event.
    below_level = [_platform_day(c=10.2) for _ in range(20)]
    below_level.append(_breakout_day(o=10.3, h=10.4, l=10.2, c=10.5))
    below_level.extend(_neutral_day() for _ in range(5))
    assert all(event.anchor_date != BREAKOUT_DAY for event in _detect(_bars(below_level)))
    # Close above the platform high -> event.
    above_level = [_platform_day(c=10.2) for _ in range(20)]
    above_level.append(_breakout_day(o=10.85, h=10.9, l=10.8, c=10.9))
    above_level.extend(_neutral_day() for _ in range(5))
    assert len(_detect(_bars(above_level))) == 1
    # Range uses high/low: tight closes but a 20% high-low band -> no event.
    wide = [_platform_day(h=10.8, l=9.0, c=10.2) for _ in range(20)]
    wide.append(_breakout_day(o=10.3, h=10.4, l=10.2, c=10.5))
    wide.extend(_neutral_day() for _ in range(5))
    assert _detect(_bars(wide)) == ()


def test_ols_slope_is_diagnostic_only():
    declining = _detect(
        _scenario(
            _pullback_day(),
            _neutral_day(v=100.0),
            _neutral_day(v=80.0),
            _neutral_day(v=60.0),
            _neutral_day(v=40.0),
        )
    )
    event = _qualified_event(declining)
    slope = event.evidence.values["diagnostics"]["ols_log_volume_slope"]
    assert slope is not None and slope < 0
    assert event.evidence.values["diagnostics"]["ols_slope_negative"] is True
    assert event.evidence.qualified is True
    rising = _detect(
        _scenario(
            _pullback_day(),
            _neutral_day(v=300.0),
            _neutral_day(v=400.0),
            _neutral_day(v=500.0),
            _neutral_day(v=600.0),
        )
    )
    event = _qualified_event(rising)
    slope = event.evidence.values["diagnostics"]["ols_log_volume_slope"]
    assert slope is not None and slope > 0
    assert event.evidence.values["diagnostics"]["ols_slope_negative"] is False
    # Rising pullback-window volumes must NOT flip the mask.
    assert event.evidence.qualified is True
    # Zero volume makes the OLS undefined but changes nothing else.
    zero = _detect(
        _scenario(
            _pullback_day(),
            _neutral_day(v=0.0),
            _neutral_day(v=80.0),
            _neutral_day(v=60.0),
            _neutral_day(v=40.0),
        )
    )
    event = _qualified_event(zero)
    assert event.evidence.values["diagnostics"]["ols_log_volume_slope"] is None
    assert event.evidence.values["diagnostics"]["ols_slope_negative"] is None
    assert event.evidence.qualified is True


def test_fake_breakout_diagnostic_on_independent_window():
    event = _qualified_event(
        _detect(
            _scenario(
                _pullback_day(),
                *[_neutral_day() for _ in range(4)],
                _neutral_day(),
                dict(o=10.9, h=11.0, l=10.6, c=10.7, v=120.0),
                _neutral_day(),
                _neutral_day(),
                _neutral_day(),
            )
        )
    )
    fake = event.evidence.values["diagnostics"]["fake_breakout"]
    assert fake["status"] == "complete"
    assert fake["fake_breakout"] is True
    assert fake["first_breach_date"] == _day(27).isoformat()
    assert fake["min_close_adj"] == 10.7
    assert fake["window_start"] == _day(26).isoformat()
    assert fake["window_end"] == _day(30).isoformat()
    assert fake["threshold_close_adj_strictly_below_level"] == LEVEL
    # Close exactly AT level is not a breach (strict comparison).
    event = _qualified_event(
        _detect(
            _scenario(
                _pullback_day(),
                *[_neutral_day() for _ in range(4)],
                _neutral_day(),
                dict(o=10.9, h=11.0, l=10.75, c=LEVEL, v=120.0),
                _neutral_day(),
                _neutral_day(),
                _neutral_day(),
            )
        )
    )
    fake = event.evidence.values["diagnostics"]["fake_breakout"]
    assert fake["status"] == "complete"
    assert fake["fake_breakout"] is False
    assert fake["first_breach_date"] is None
    assert fake["min_close_adj"] == LEVEL
    assert event.evidence.qualified is True


def test_fake_breakout_window_incomplete_is_diagnostic_censor_only():
    # Bars end on fake-window day 2; selection stays qualified.
    event = _qualified_event(
        _detect(
            _scenario(
                _pullback_day(),
                *[_neutral_day() for _ in range(4)],
                _neutral_day(),
                dict(o=10.9, h=11.0, l=10.6, c=10.7, v=120.0),
            )
        )
    )
    fake = event.evidence.values["diagnostics"]["fake_breakout"]
    assert fake["status"] == CensorReason.DIAGNOSTIC_WINDOW_INCOMPLETE.value
    assert fake["fake_breakout"] is None
    assert event.evidence.qualified is True
    assert event.censor is None
    # Calendar itself ends inside the fake window.
    event = _qualified_event(
        _detect(
            _scenario(_pullback_day(), *[_neutral_day() for _ in range(4)]),
            calendar=_calendar(27),
        )
    )
    fake = event.evidence.values["diagnostics"]["fake_breakout"]
    assert fake["status"] == CensorReason.DIAGNOSTIC_WINDOW_INCOMPLETE.value
    assert fake["window_start"] is not None
    assert fake["window_end"] is None
    assert event.evidence.qualified is True


def test_no_new_event_inside_active_window():
    specs = (
        [_platform_day() for _ in range(20)]
        + [_breakout_day()]
        # Would be a breakout candidate itself but sits inside the active window.
        + [_breakout_day(o=11.0, h=11.9, l=11.0, c=11.8)]
        + [_neutral_day() for _ in range(4)]
        + [_platform_day(o=12.0, h=12.6, l=11.4, c=12.0) for _ in range(20)]
        + [_breakout_day(o=12.8, h=13.4, l=12.7, c=13.0)]
        + [dict(o=13.2, h=13.6, l=13.1, c=13.4, v=120.0) for _ in range(5)]
    )
    events = _detect(_bars(specs))
    assert len(events) == 2
    assert _classify(events) == {
        BREAKOUT_DAY: "not_selected",
        _day(46): "not_selected",
    }
    assert _day(21) not in _classify(events)


def test_calendar_ending_inside_selection_window_censors_without_landmark():
    bars = _bars(
        [_platform_day() for _ in range(20)]
        + [_breakout_day()]
        + [_neutral_day() for _ in range(2)]
    )
    events = _detect(bars, calendar=_calendar(23))
    assert len(events) == 1
    event = events[0]
    assert event.evidence is None
    assert event.censor is CensorReason.SELECTION_WINDOW_INCOMPLETE
    assert event.landmark is None


def test_insufficient_history_yields_no_events():
    bars = _bars([_platform_day() for _ in range(19)] + [_breakout_day()])
    assert _detect(bars) == ()
    # A breakout after full history but an incomplete window still censors.
    bars = _bars(
        [_platform_day() for _ in range(20)]
        + [_breakout_day()]
        + [_neutral_day() for _ in range(4)]
    )
    events = _detect(bars)
    assert len(events) == 1
    assert events[0].censor is CensorReason.SELECTION_WINDOW_INCOMPLETE


def test_missing_bar_inside_selection_window_is_censored():
    specs_by_offset = {
        **{offset: _platform_day() for offset in range(20)},
        20: _breakout_day(),
        21: _neutral_day(),
        # offset 22 has no canonical bar on market selection day 2.
        23: _pullback_day(),
        24: _neutral_day(),
        25: _neutral_day(),
    }
    bars = tuple(_bar(_day(offset), **spec) for offset, spec in sorted(specs_by_offset.items()))
    events = _detect(bars)
    assert len(events) == 1
    assert events[0].evidence is None
    assert events[0].censor is CensorReason.SELECTION_WINDOW_INCOMPLETE


def test_missing_bar_inside_prior_platform_does_not_fabricate_parent():
    complete = _scenario(_pullback_day(), *[_neutral_day() for _ in range(4)])
    missing_day = _day(10)
    bars = tuple(bar for bar in complete if bar.date != missing_day)
    assert _detect(bars) == ()


def test_truncation_invariance_prefix_and_extension():
    def two_event_specs():
        return (
            [_platform_day() for _ in range(20)]
            + [_breakout_day(), _pullback_day()]
            + [_neutral_day() for _ in range(4)]
            + [_platform_day(o=12.0, h=12.6, l=11.4, c=12.0) for _ in range(20)]
            + [_breakout_day(o=12.8, h=13.4, l=12.7, c=13.0)]
            + [dict(o=13.2, h=13.6, l=13.1, c=13.4, v=120.0) for _ in range(5)]
        )

    full = _bars(two_event_specs())
    full_classes = _classify(_detect(full))
    assert full_classes == {BREAKOUT_DAY: "qualified", _day(46): "not_selected"}
    # Cut exactly at A's day-5 landmark: A already classifies identically.
    assert _classify(_detect(full[:26])) == {BREAKOUT_DAY: "qualified"}
    # Cut mid-B window: A unchanged, B may only appear as a selection censor.
    cut_classes = _classify(_detect(full[:49]))
    assert cut_classes == {
        BREAKOUT_DAY: "qualified",
        _day(46): CensorReason.SELECTION_WINDOW_INCOMPLETE,
    }
    # Extending with future bars must not change past classifications.
    extended = _bars(
        list(two_event_specs()) + [dict(o=13.2, h=13.6, l=13.1, c=13.4, v=120.0) for _ in range(10)]
    )
    # Future bars may complete the diagnostic fake-breakout window, so only
    # selection classification and landmark remain invariant.
    extended_events = _detect(extended, calendar=_calendar(70))
    full_events = _detect(full)
    assert _classify(extended_events) == _classify(full_events)
    assert [event.landmark for event in extended_events] == [
        event.landmark for event in full_events
    ]


def test_purity_determinism_and_adjusted_only():
    bars = _scenario(_pullback_day(), *[_neutral_day() for _ in range(4)])
    detector = BreakoutPullbackDetector()
    first = detector.detect(SYMBOL, bars, _UnusedFacts(), _calendar())
    second = detector.detect(SYMBOL, bars, _UnusedFacts(), _calendar())
    assert first == second
    # Raw quotes differ from adjusted values everywhere; detection is driven
    # by the adjusted structure alone.
    assert len(first) == 1 and first[0].evidence.qualified is True


def test_misaligned_inputs_are_rejected():
    bars = _scenario(*[_neutral_day() for _ in range(5)])
    with pytest.raises(ValueError):
        _detect(bars, calendar=_calendar(10))
    with pytest.raises(ValueError):
        _detect(tuple(reversed(bars)))
