"""Shared execution, censoring, OOS, and provenance for daily-event research."""

from __future__ import annotations

import math
from datetime import date, timedelta
from typing import Any

from app.services.hold_firm_patterns.adapters import (
    PinnedCanonicalDailyReader,
    pinned_market_facts_source,
)
from app.services.hold_firm_patterns.models import Bar, MarketFactsRow

from .dugu_trend import (
    DUGU_ALIGNMENT_DAY_CHOICES,
    DUGU_SCAN_AXES,
    DUGU_SCAN_SCHEMA,
    DUGU_VARIANTS,
    DuguTrendDetector,
    dugu_scan_cell_id,
    resolve_dugu_config,
)
from .models import (
    CensorReason,
    DailyEventCoverage,
    DailyEventIdentity,
    DailyEventProvenance,
    DailyEventRequest,
    DailyEventResponse,
    DailyEventStatus,
    DailyEventVerdict,
    DailyEventVerdicts,
    EventCensor,
    EventOutcome,
    UnavailabilityReason,
    unavailable_response,
)

MIN_OOS_EVENTS = 30
MIN_OOS_SYMBOLS = 10
CI_Z = 1.96
LOOKBACK_CALENDAR_DAYS = 400
FORWARD_CALENDAR_DAYS = 130
WARMUP_MARKET_DAYS = 210
EXIT_SEARCH_DAYS = 5
PRICE_ABS_TOL = 0.005
CODE_VERSION = "issue45-daily-event-research-v1"
DEFINITION_DOCUMENT = "docs/ISSUE-45/final-design.md"


def _windows(
    reader: PinnedCanonicalDailyReader,
    request: DailyEventRequest,
) -> tuple[tuple[date, ...], date, date]:
    full = reader.trading_days(
        request.start - timedelta(days=LOOKBACK_CALENDAR_DAYS),
        request.end + timedelta(days=FORWARD_CALENDAR_DAYS),
    )
    if not full:
        return (), request.start, request.end
    first = next((i for i, day in enumerate(full) if day >= request.start), len(full))
    last = next(
        (i for i in range(len(full) - 1, -1, -1) if full[i] <= request.end),
        first - 1,
    )
    if first >= len(full) or last < first:
        return (), request.start, request.end
    start = full[max(0, first - WARMUP_MARKET_DAYS)]
    end = full[
        min(
            len(full) - 1,
            last + request.horizon_days + EXIT_SEARCH_DAYS + 1,
        )
    ]
    return full, start, end


def _stats(outcomes: list[EventOutcome]) -> dict[str, object]:
    values = [event.cost_adjusted_forward_return for event in outcomes]
    symbols = len({event.symbol for event in outcomes})
    if not values:
        return {
            "events": 0,
            "symbols": 0,
            "mean_return": None,
            "std_error": None,
            "ci95_lower": None,
        }
    mean = sum(values) / len(values)
    variance = (
        sum((value - mean) ** 2 for value in values) / (len(values) - 1) if len(values) > 1 else 0.0
    )
    standard_error = math.sqrt(variance / len(values)) if len(values) > 1 else None
    return {
        "events": len(values),
        "symbols": symbols,
        "mean_return": mean,
        "std_error": standard_error,
        "ci95_lower": (mean - CI_Z * standard_error if standard_error is not None else None),
    }


def _comparison_stats(
    qualified: list[EventOutcome],
    baseline: list[EventOutcome],
) -> dict[str, object]:
    qualified_stats = _stats(qualified)
    baseline_stats = _stats(baseline)
    qualified_mean = qualified_stats["mean_return"]
    baseline_mean = baseline_stats["mean_return"]
    difference = (
        float(qualified_mean) - float(baseline_mean)
        if isinstance(qualified_mean, float) and isinstance(baseline_mean, float)
        else None
    )
    qualified_se = qualified_stats["std_error"]
    baseline_se = baseline_stats["std_error"]
    difference_se = (
        math.sqrt(float(qualified_se) ** 2 + float(baseline_se) ** 2)
        if isinstance(qualified_se, float) and isinstance(baseline_se, float)
        else None
    )
    return {
        "qualified": qualified_stats,
        "baseline": baseline_stats,
        "baseline_id": "same_detector_not_selected",
        "mean_increment": difference,
        "increment_ci95_lower": (
            difference - CI_Z * difference_se
            if difference is not None and difference_se is not None
            else None
        ),
    }


def _one_price_at(value: float, bar: Bar) -> bool:
    return all(
        math.isclose(candidate, value, abs_tol=PRICE_ABS_TOL)
        for candidate in (
            bar.quote_open_raw,
            bar.quote_high_raw,
            bar.quote_low_raw,
            bar.quote_close_raw,
        )
    )


def _entry_blocked(fact: MarketFactsRow, bar: Bar) -> bool:
    return _one_price_at(fact.published_limit_up, bar)


def _exit_blocked(fact: MarketFactsRow, bar: Bar) -> bool:
    return _one_price_at(fact.published_limit_down, bar)


def _append_censor(
    target: list[EventCensor],
    *,
    detector_id: str,
    variant: str,
    symbol: str,
    signal_date: date,
    reason: CensorReason,
) -> None:
    target.append(
        EventCensor(
            detector_id=detector_id,
            variant=variant,
            symbol=symbol,
            signal_date=signal_date,
            reason=reason,
        )
    )


def evaluate_daily_events(
    request: DailyEventRequest,
    reader: Any,
    market_facts: Any | None = None,
) -> DailyEventResponse:
    """Evaluate a frozen detector with T+1 execution and PIT reachability."""
    if market_facts is None:
        return unavailable_response(request, UnavailabilityReason.MARKET_FACTS)
    try:
        canonical = PinnedCanonicalDailyReader(reader)
        identity = canonical.identity()
        full_days, bar_start, bar_end = _windows(canonical, request)
        if not full_days:
            return unavailable_response(request, UnavailabilityReason.CANONICAL_READER)
        calendar = tuple(day for day in full_days if bar_start <= day <= bar_end)
    except (AttributeError, KeyError, OSError, RuntimeError, TypeError, ValueError):
        return unavailable_response(request, UnavailabilityReason.CANONICAL_READER)
    try:
        facts = pinned_market_facts_source(market_facts, request.symbols, calendar)
        if facts.incomplete_rows:
            return unavailable_response(request, UnavailabilityReason.MARKET_FACTS)
    except (AttributeError, KeyError, OSError, RuntimeError, TypeError, ValueError):
        return unavailable_response(request, UnavailabilityReason.MARKET_FACTS)
    alignment_days = getattr(request, "alignment_days", 30)
    detector = DuguTrendDetector(
        resolve_dugu_config(
            request.variant,
            request.band_mode,
            request.require_m3,
            alignment_days,
        )
    )

    calendar_index = {day: index for index, day in enumerate(calendar)}
    events: list[EventOutcome] = []
    censored: list[EventCensor] = []
    parent = qualified = not_selected = 0
    symbols_with_bars = 0
    bar_rows = 0

    for symbol in request.symbols:
        try:
            bars = canonical.load_bars(symbol, bar_start, bar_end)
            bar_rows += len(bars)
            if bars:
                symbols_with_bars += 1
            by_date = {bar.date: bar for bar in bars}
            detections = detector.detect(symbol, bars, calendar)
        except (KeyError, OSError, RuntimeError, TypeError, ValueError):
            continue
        for detection in detections:
            if not request.start <= detection.signal_date <= request.end:
                continue
            if detection.censor is not None:
                _append_censor(
                    censored,
                    detector_id=detection.detector_id,
                    variant=detection.variant,
                    symbol=symbol,
                    signal_date=detection.signal_date,
                    reason=detection.censor,
                )
                continue

            parent += 1
            assert detection.evidence is not None
            if detection.evidence.qualified:
                qualified += 1
            else:
                not_selected += 1

            signal_index = calendar_index.get(detection.signal_date)
            entry_index = None if signal_index is None else signal_index + 1
            exit_index = None if entry_index is None else entry_index + request.horizon_days - 1
            if (
                entry_index is None
                or entry_index >= len(calendar)
                or exit_index is None
                or exit_index >= len(calendar)
            ):
                _append_censor(
                    censored,
                    detector_id=detection.detector_id,
                    variant=detection.variant,
                    symbol=symbol,
                    signal_date=detection.signal_date,
                    reason=CensorReason.HORIZON_INCOMPLETE,
                )
                continue

            entry_date = calendar[entry_index]
            exit_date = calendar[exit_index]
            entry_bar = by_date.get(entry_date)
            exit_bar = by_date.get(exit_date)
            if entry_bar is None:
                _append_censor(
                    censored,
                    detector_id=detection.detector_id,
                    variant=detection.variant,
                    symbol=symbol,
                    signal_date=detection.signal_date,
                    reason=CensorReason.ENTRY_INCOMPLETE,
                )
                continue
            if exit_bar is None or entry_bar.research_open_adj <= 0:
                _append_censor(
                    censored,
                    detector_id=detection.detector_id,
                    variant=detection.variant,
                    symbol=symbol,
                    signal_date=detection.signal_date,
                    reason=CensorReason.HORIZON_INCOMPLETE,
                )
                continue

            entry_fact = facts.row(symbol, entry_date)
            exit_fact = facts.row(symbol, exit_date)
            if entry_fact is None or exit_fact is None:
                _append_censor(
                    censored,
                    detector_id=detection.detector_id,
                    variant=detection.variant,
                    symbol=symbol,
                    signal_date=detection.signal_date,
                    reason=CensorReason.MARKET_FACTS_MISSING,
                )
                continue
            if _entry_blocked(entry_fact, entry_bar):
                _append_censor(
                    censored,
                    detector_id=detection.detector_id,
                    variant=detection.variant,
                    symbol=symbol,
                    signal_date=detection.signal_date,
                    reason=CensorReason.ENTRY_LIMIT_UP_BLOCKED,
                )
                continue
            if _exit_blocked(exit_fact, exit_bar):
                _append_censor(
                    censored,
                    detector_id=detection.detector_id,
                    variant=detection.variant,
                    symbol=symbol,
                    signal_date=detection.signal_date,
                    reason=CensorReason.EXIT_LIMIT_DOWN_BLOCKED,
                )
                continue

            entry_price = entry_bar.research_open_adj
            exit_price = exit_bar.research_close_adj
            gross = exit_price / entry_price - 1.0
            one_way_cost = request.cost_bps / 10000.0
            net = (1.0 + gross) * (1.0 - one_way_cost) ** 2 - 1.0
            event_id = (
                f"{detection.detector_id}:{detection.variant}:"
                f"{symbol}:{detection.signal_date.isoformat()}"
            )
            events.append(
                EventOutcome(
                    event_id=event_id,
                    detector_id=detection.detector_id,
                    variant=detection.variant,
                    symbol=symbol,
                    signal_date=detection.signal_date,
                    qualified=detection.evidence.qualified,
                    entry_date=entry_date,
                    entry_price=entry_price,
                    exit_date=exit_date,
                    exit_price=exit_price,
                    raw_forward_return=gross,
                    cost_adjusted_forward_return=net,
                    oos=detection.signal_date >= request.oos_start,
                )
            )

    if not parent:
        return unavailable_response(request, UnavailabilityReason.NO_EVENTS)

    in_sample = [event for event in events if not event.oos]
    oos_qualified = [event for event in events if event.oos and event.qualified]
    oos_baseline = [event for event in events if event.oos and not event.qualified]
    oos_stats = _comparison_stats(oos_qualified, oos_baseline)
    qualified_stats = oos_stats["qualified"]
    baseline_stats = oos_stats["baseline"]
    enough_samples = (
        int(qualified_stats["events"]) >= MIN_OOS_EVENTS
        and int(qualified_stats["symbols"]) >= MIN_OOS_SYMBOLS
        and int(baseline_stats["events"]) >= MIN_OOS_EVENTS
        and int(baseline_stats["symbols"]) >= MIN_OOS_SYMBOLS
    )
    if not enough_samples:
        verdict = DailyEventVerdict.UNAVAILABLE
    else:
        lower = oos_stats["increment_ci95_lower"]
        verdict = (
            DailyEventVerdict.ACCEPTED
            if isinstance(lower, float) and lower > 0
            else DailyEventVerdict.REJECTED
        )
    detector_config = getattr(detector, "config", None)
    scan_cell_id = (
        dugu_scan_cell_id(detector_config)
        if detector_config is not None
        else "unknown"
    )
    provenance_params = {
        "variant": request.variant,
        "band_mode": request.band_mode,
        "require_m3": request.require_m3,
        "alignment_days": alignment_days,
        "alignment_day_choices": list(DUGU_ALIGNMENT_DAY_CHOICES),
        "scan_grid_schema": DUGU_SCAN_SCHEMA,
        "scan_grid_cell_id": scan_cell_id,
        "scan_grid_axes": {key: list(values) for key, values in DUGU_SCAN_AXES.items()},
        "ma_windows": list(DUGU_VARIANTS[request.variant]),
        "reclaim_ma_days": 5,
        "pullback_lookback_days": 10,
        "m3_window_days": 20,
        "m3_max_return": 0.30,
        "fixed_band_pct": 0.03,
        "atr_window_days": 20,
        "atr_band_mult": 1.0,
        "execution": "signal_close_then_next_market_day_open",
        "round_trip_cost_bps": request.cost_bps * 2,
        "baseline": "same_detector_not_selected",
        "min_oos_events": MIN_OOS_EVENTS,
        "min_oos_symbols": MIN_OOS_SYMBOLS,
        "ci_z": CI_Z,
    }
    provenance = DailyEventProvenance(
        code_version=CODE_VERSION,
        parameters=provenance_params,
        params_provenance={key: DEFINITION_DOCUMENT for key in provenance_params},
    )
    coverage = DailyEventCoverage(
        symbols_requested=len(request.symbols),
        symbols_with_bars=symbols_with_bars,
        bar_rows=bar_rows,
        parent_events=parent,
        qualified_events=qualified,
        not_selected_events=not_selected,
        censored_detections=len(censored),
        horizon_incomplete=sum(
            item.reason
            in {
                CensorReason.ENTRY_INCOMPLETE,
                CensorReason.HORIZON_INCOMPLETE,
            }
            for item in censored
        ),
    )
    return DailyEventResponse(
        status=DailyEventStatus.OK,
        request=request,
        identity=DailyEventIdentity(
            canonical=identity.model_dump(),
            available_at=identity.generation,
            market_facts=facts.identity().model_dump(),
        ),
        coverage=coverage,
        censored=censored,
        events=events,
        verdicts=DailyEventVerdicts(
            verdict=verdict,
            in_sample=_stats(in_sample),
            oos=oos_stats,
        ),
        provenance=provenance,
    )


__all__ = ["evaluate_daily_events"]
