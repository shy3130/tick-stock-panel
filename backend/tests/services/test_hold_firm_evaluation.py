from __future__ import annotations

from dataclasses import replace
from datetime import date, timedelta

from app.services.hold_firm_patterns.adapters import request_windows
from app.services.hold_firm_patterns.evaluation import (
    _PreparedEvent,
    _diagnostics,
    _materialize,
    _membership_days,
    _overlaps_active_horizon,
    _simulate,
    _split_statistics,
)
from app.services.hold_firm_patterns.models import (
    Bar,
    CensorReason,
    DenominatorAuditCode,
    DetectionEvidence,
    FactorResult,
    HoldFirmVerdict,
    Landmark,
    LandmarkKind,
    MarketFactsIdentity,
    MarketFactsRow,
    ParentDetection,
    ParentEvent,
    PitUniverseStatus,
    SelectionBucket,
    validate_factor_coverage,
)


def _days(count: int) -> tuple[date, ...]:
    start = date(2026, 1, 1)
    return tuple(start + timedelta(days=index) for index in range(count))


def _bar(symbol: str, day: date, *, close: float = 12.0, raw: float | None = None) -> Bar:
    raw_value = close if raw is None else raw
    return Bar(
        symbol=symbol,
        date=day,
        research_open_adj=close,
        research_high_adj=close,
        research_low_adj=close,
        research_close_adj=close,
        quote_open_raw=raw_value,
        quote_high_raw=raw_value,
        quote_low_raw=raw_value,
        quote_close_raw=raw_value,
        volume=100.0,
        amount=1_000.0,
    )


class _Facts:
    def __init__(self, rows: dict[tuple[str, date], MarketFactsRow]) -> None:
        self.rows = rows

    def identity(self) -> MarketFactsIdentity:
        return MarketFactsIdentity(generation="markets", manifest_sha256="a" * 64)

    def row(self, symbol: str, day: date) -> MarketFactsRow | None:
        return self.rows.get((symbol, day))


def _fact(symbol: str, day: date, bar: Bar, *, lower: float = 8.0) -> MarketFactsRow:
    return MarketFactsRow(
        symbol=symbol,
        date=day,
        quote_open_raw=bar.quote_open_raw,
        quote_high_raw=bar.quote_high_raw,
        quote_low_raw=bar.quote_low_raw,
        quote_close_raw=bar.quote_close_raw,
        pre_close=10.0,
        published_limit_up=20.0,
        published_limit_down=lower,
        regime="main_10",
        is_st=False,
        name="fixture",
    )


def _prepared(
    factor_id: str,
    symbol: str,
    calendar: tuple[date, ...],
    values: dict[str, object],
) -> _PreparedEvent:
    detection = ParentDetection(
        factor_id=factor_id,  # type: ignore[arg-type]
        symbol=symbol,
        anchor_date=calendar[0],
        landmark=Landmark(
            kind=LandmarkKind.SIGNAL_DAY_CLOSE,
            anchor_date=calendar[0],
            landmark_date=calendar[0],
        ),
        evidence=DetectionEvidence(qualified=True, values=values),
    )
    event = ParentEvent(
        factor_id=factor_id,  # type: ignore[arg-type]
        event_id=f"{factor_id}:{symbol}",
        symbol=symbol,
        anchor_date=calendar[0],
        bucket=SelectionBucket.QUALIFIED,
        pit_status=PitUniverseStatus.IN_POOL,
    )
    return _PreparedEvent(event=event, detection=detection)

class _Universe:
    def __init__(self, status: PitUniverseStatus) -> None:
        self.status = status
        self.calls: list[tuple[str, date]] = []

    def membership(self, symbol: str, day: date) -> PitUniverseStatus:
        self.calls.append((symbol, day))
        return self.status


def test_censored_parent_uses_landmark_membership_before_denominator_bucket() -> None:
    calendar = _days(3)
    symbol = "000001.SZ"
    detection = ParentDetection(
        factor_id="breakout_pullback",
        symbol=symbol,
        anchor_date=calendar[0],
        landmark=Landmark(
            kind=LandmarkKind.BREAKOUT_DAY5_CLOSE,
            anchor_date=calendar[0],
            landmark_date=calendar[1],
        ),
        censor=CensorReason.SELECTION_WINDOW_INCOMPLETE,
    )
    assert _membership_days(((detection,),)) == (calendar[1],)

    universe = _Universe(PitUniverseStatus.NOT_IN_POOL)
    qualified, not_selected, pit, selection_censored, censors = _materialize(
        "breakout_pullback",
        (detection,),
        universe,  # type: ignore[arg-type]
    )
    assert universe.calls == [(symbol, calendar[1])]
    assert qualified == []
    assert not_selected == []
    assert selection_censored == []
    assert censors == []
    assert len(pit) == 1
    assert pit[0].pit_status is PitUniverseStatus.NOT_IN_POOL
    assert pit[0].audit_code is DenominatorAuditCode.PIT_UNIVERSE_INELIGIBLE


def test_in_pool_censored_parent_preserves_censor_bucket() -> None:
    calendar = _days(3)
    detection = ParentDetection(
        factor_id="first_yin_complement",
        symbol="000001.SZ",
        anchor_date=calendar[1],
        landmark=None,
        censor=CensorReason.WARMUP_INCOMPLETE,
    )
    universe = _Universe(PitUniverseStatus.IN_POOL)
    qualified, not_selected, pit, selection_censored, censors = _materialize(
        "first_yin_complement",
        (detection,),
        universe,  # type: ignore[arg-type]
    )
    assert universe.calls == [("000001.SZ", calendar[1])]
    assert qualified == []
    assert not_selected == []
    assert pit == []
    assert len(selection_censored) == 1
    assert selection_censored[0].pit_status is PitUniverseStatus.IN_POOL
    assert len(censors) == 1



def test_dynamic_pending_stays_exposed_through_common_day20() -> None:
    calendar = _days(21)
    symbol = "000001.SZ"
    bars: dict[date, Bar] = {}
    rows: dict[tuple[str, date], MarketFactsRow] = {}
    for index, day in enumerate(calendar):
        if index <= 1:
            bar = _bar(symbol, day, close=12.0)
        elif index == 2:
            bar = _bar(symbol, day, close=9.0)
        else:
            bar = _bar(symbol, day, close=8.0, raw=8.0)
        bars[day] = bar
        rows[(symbol, day)] = _fact(symbol, day, bar)
    simulation, censor = _simulate(
        _prepared(
            "bottom_platform_breakout",
            symbol,
            calendar,
            {"entity_bottom_adj": 10.0},
        ),
        bars,
        _Facts(rows),  # type: ignore[arg-type]
        calendar,
        10.0,
        with_dynamic=True,
    )
    assert censor is None
    assert simulation is not None and simulation.dynamic is not None
    assert simulation.dynamic.pending_exit is True
    assert simulation.dynamic.exit_date is None
    assert simulation.dynamic.holding_days == 20
    assert simulation.dynamic.terminal_return < 0
    assert simulation.censors[0].reason.value == "realization_censor_pending_exit"


def test_f1_diagnostics_report_limit_down_and_pending_ratios() -> None:
    calendar = _days(21)
    symbol = "000001.SZ"
    bars = {
        day: _bar(symbol, day, close=12.0 if index < 4 else 8.0)
        for index, day in enumerate(calendar)
    }
    rows = {(symbol, day): _fact(symbol, day, bars[day], lower=8.0) for day in calendar}
    prepared = _prepared("first_yin_complement", symbol, calendar, {})
    simulation, censor = _simulate(
        prepared,
        bars,
        _Facts(rows),  # type: ignore[arg-type]
        calendar,
        0.0,
        with_dynamic=True,
    )
    assert censor is None and simulation is not None
    payload = _diagnostics(
        "first_yin_complement",
        [prepared],
        {symbol: bars},
        calendar,
        {prepared.event.event_id: simulation},
        [],
    )
    assert payload["qualified_simulated_events"] == 1
    assert payload["max_consecutive_limit_down_days"] == 16
    assert payload["unreachable_exit_event_ratio"] == 1.0
    assert payload["pending_exit_event_ratio"] == 1.0


def test_same_symbol_landmark_boundary_allows_next_day_entry() -> None:
    calendar = _days(30)
    first = _prepared("first_yin_complement", "000001.SZ", calendar, {})

    def candidate(landmark_day: date, event_id: str) -> _PreparedEvent:
        return _PreparedEvent(
            event=replace(
                first.event,
                event_id=event_id,
                anchor_date=landmark_day,
            ),
            detection=replace(
                first.detection,
                anchor_date=landmark_day,
                landmark=Landmark(
                    LandmarkKind.SIGNAL_DAY_CLOSE,
                    landmark_day,
                    landmark_day,
                ),
            ),
        )

    blocked_through = {"000001.SZ": calendar[19]}
    assert _overlaps_active_horizon(candidate(calendar[19], "blocked"), blocked_through)
    assert not _overlaps_active_horizon(candidate(calendar[20], "allowed"), blocked_through)
    assert not _overlaps_active_horizon(
        candidate(calendar[10], "other-symbol"),
        {"000002.SZ": calendar[20]},
    )


def test_dynamic_exit_cash_is_constant_to_day20() -> None:
    calendar = _days(21)
    symbol = "000001.SZ"
    closes = [12.0, 12.0, 9.0, 9.0] + [20.0] * 17
    bars = {day: _bar(symbol, day, close=closes[index]) for index, day in enumerate(calendar)}
    rows = {(symbol, day): _fact(symbol, day, bars[day], lower=5.0) for day in calendar}
    simulation, censor = _simulate(
        _prepared(
            "breakout_pullback",
            symbol,
            calendar,
            {"breakout": {"level_adj": 10.0}},
        ),
        bars,
        _Facts(rows),  # type: ignore[arg-type]
        calendar,
        0.0,
        with_dynamic=True,
    )
    assert censor is None
    assert simulation is not None and simulation.dynamic is not None
    assert simulation.dynamic.exit_date == calendar[3]
    assert simulation.dynamic.terminal_return < simulation.fixed.terminal_return
    assert simulation.dynamic.holding_days == 3


def test_missing_bar_inside_common_horizon_is_censored() -> None:
    calendar = _days(21)
    symbol = "000001.SZ"
    bars = {day: _bar(symbol, day) for day in calendar if day != calendar[10]}
    rows = {(symbol, day): _fact(symbol, day, _bar(symbol, day)) for day in calendar}
    simulation, censor = _simulate(
        _prepared(
            "bottom_platform_breakout",
            symbol,
            calendar,
            {"entity_bottom_adj": 10.0},
        ),
        bars,
        _Facts(rows),  # type: ignore[arg-type]
        calendar,
        10.0,
        with_dynamic=True,
    )
    assert simulation is None
    assert censor is not None
    assert censor.reason.value == "censor_horizon_incomplete"


def test_insufficient_oos_groups_are_unavailable_not_rejected() -> None:
    payload, selection, holding = _split_statistics([], [])
    assert selection is HoldFirmVerdict.UNAVAILABLE
    assert holding is HoldFirmVerdict.UNAVAILABLE
    assert payload["qualified_complete_events"] == 0


def test_request_windows_keeps_forward_calendar_separate_from_event_start() -> None:
    calendar = _days(300)

    class Reader:
        def market_days(self, start: date, end: date) -> list[date]:
            return [day for day in calendar if start <= day <= end]

    start, end = calendar[180], calendar[200]
    full, events, bar_start, bar_end = request_windows(Reader(), start, end)  # type: ignore[arg-type]
    assert events[0] == start and events[-1] == end
    assert bar_start < start
    assert bar_end > end
    assert full.index(start) > 0


def test_factor_coverage_requires_four_independent_results() -> None:
    results = [
        FactorResult(
            factor_id=factor_id,
            parent_events=0,
            qualified_events=0,
            not_selected_events=0,
            selection_verdict=HoldFirmVerdict.UNAVAILABLE,
            holding_verdict=HoldFirmVerdict.UNAVAILABLE,
            verdict=HoldFirmVerdict.UNAVAILABLE,
        )
        for factor_id in (
            "first_yin_complement",
            "breakout_pullback",
            "low_gentle_slope",
            "bottom_platform_breakout",
        )
    ]
    validate_factor_coverage(results)
