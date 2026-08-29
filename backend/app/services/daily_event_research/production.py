"""Production orchestration for the Issue #45/#47/#48 daily research modules."""

from __future__ import annotations

import math
from dataclasses import asdict
from datetime import date, timedelta
from typing import Any, Sequence

from app.services.hold_firm_patterns.adapters import (
    PinnedCanonicalDailyReader,
    PinnedPresenceUniverseReader,
    pinned_market_facts_source,
)
from app.services.hold_firm_patterns.models import Bar, MarketFactsRow

from .escape_risk import (
    DAILY_SIGNAL_IDS,
    MINUTE_SIGNAL_IDS,
    SIGNAL_CAPABILITIES,
    EscapeS1Detector,
    EscapeS8Detector,
    EscapeS9Detector,
    aggregate_escape_signals,
)
from .pre_surge import (
    ALL_VARIANTS,
    PreSurgeDetector,
    PreSurgeStudyAggregator,
    detection_payload,
)

PRE_SURGE_SCHEMA = "daily_event_research/pre_surge/v1"
ESCAPE_SCHEMA = "daily_event_research/escape_risk/v1"
PRE_SURGE_WARMUP_CALENDAR_DAYS = 400
ESCAPE_WARMUP_CALENDAR_DAYS = 180
FORWARD_CALENDAR_DAYS = 90
PRICE_ABS_TOL = 0.005


def _calendar_window(
    canonical: PinnedCanonicalDailyReader,
    start: date,
    end: date,
    *,
    warmup_calendar_days: int,
) -> tuple[date, ...]:
    return canonical.trading_days(
        start - timedelta(days=warmup_calendar_days),
        end + timedelta(days=FORWARD_CALENDAR_DAYS),
    )


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


def _future_surge_label(
    *,
    detection_date: date,
    calendar: Sequence[date],
    bars_by_date: dict[date, Bar],
    facts: Any,
    symbol: str,
    horizon_days: int,
    surge_threshold: float,
    cost_bps: float,
) -> bool | None:
    positions = {day: index for index, day in enumerate(calendar)}
    signal_index = positions.get(detection_date)
    if signal_index is None:
        return None
    entry_index = signal_index + 1
    exit_index = entry_index + horizon_days - 1
    if entry_index >= len(calendar) or exit_index >= len(calendar):
        return None
    entry_date = calendar[entry_index]
    exit_date = calendar[exit_index]
    entry_bar = bars_by_date.get(entry_date)
    exit_bar = bars_by_date.get(exit_date)
    entry_fact: MarketFactsRow | None = facts.row(symbol, entry_date)
    exit_fact: MarketFactsRow | None = facts.row(symbol, exit_date)
    if entry_bar is None or exit_bar is None or entry_fact is None or exit_fact is None:
        return None
    if entry_bar.research_open_adj <= 0:
        return None
    if _one_price_at(entry_fact.published_limit_up, entry_bar):
        return None
    if _one_price_at(exit_fact.published_limit_down, exit_bar):
        return None
    gross = exit_bar.research_close_adj / entry_bar.research_open_adj - 1.0
    one_way_cost = cost_bps / 10_000.0
    net = (1.0 + gross) * (1.0 - one_way_cost) ** 2 - 1.0
    return net >= surge_threshold


def evaluate_pre_surge_production(
    *,
    symbols: Sequence[str],
    start: date,
    oos_start: date,
    end: date,
    canonical_reader: Any,
    market_facts_reader: Any,
    universe_reader: Any,
    benchmark_symbol: str,
    horizon_days: int = 10,
    surge_threshold: float = 0.20,
    cost_bps: float = 10.0,
) -> dict[str, object]:
    """Evaluate F1-F4 on the frozen OOS window with T+1 reachability."""
    try:
        canonical = PinnedCanonicalDailyReader(canonical_reader)
        calendar = _calendar_window(
            canonical,
            start,
            end,
            warmup_calendar_days=PRE_SURGE_WARMUP_CALENDAR_DAYS,
        )
        if not calendar:
            raise RuntimeError("canonical_calendar_empty")
        facts = pinned_market_facts_source(market_facts_reader, symbols, calendar)
        if facts.incomplete_rows:
            raise RuntimeError("market_facts_incomplete")
        bars_by_symbol = {
            symbol: canonical.load_bars(symbol, calendar[0], calendar[-1]) for symbol in symbols
        }
        benchmark_bars = canonical.load_bars(
            benchmark_symbol,
            calendar[0],
            calendar[-1],
        )
    except (AttributeError, KeyError, OSError, RuntimeError, TypeError, ValueError) as exc:
        return {
            "schema": PRE_SURGE_SCHEMA,
            "status": "unavailable",
            "reason": "unavailable_pinned_daily_inputs",
            "detail": str(exc),
            "promoted": False,
        }

    aggregator = PreSurgeStudyAggregator()
    detector = PreSurgeDetector()
    pending: list[tuple[str, dict[date, Bar], Any]] = []
    qualified_events: list[dict[str, object]] = []
    censored: dict[str, int] = {}
    evaluated = 0
    for symbol, bars in bars_by_symbol.items():
        by_date = {bar.date: bar for bar in bars}
        fact_map = {
            (symbol, day): row for day in calendar if (row := facts.row(symbol, day)) is not None
        }
        detections = detector.detect(
            symbol,
            bars,
            fact_map,
            benchmark_bars,
            calendar,
        )
        for variant in ALL_VARIANTS:
            pending.extend(
                (symbol, by_date, detection)
                for detection in detections[variant]
                if oos_start <= detection.signal_date <= end
            )

    signal_days = tuple(sorted({item[2].signal_date for item in pending}))
    try:
        universe = PinnedPresenceUniverseReader(universe_reader, signal_days)
    except (AttributeError, KeyError, OSError, RuntimeError, TypeError, ValueError) as exc:
        return {
            "schema": PRE_SURGE_SCHEMA,
            "status": "unavailable",
            "reason": "unavailable_pit_universe",
            "detail": str(exc),
            "promoted": False,
        }

    pit_universe_ineligible = 0
    for symbol, by_date, detection in pending:
        try:
            universe.membership(symbol, detection.signal_date)
        except (KeyError, OSError, RuntimeError, TypeError, ValueError):
            pit_universe_ineligible += 1
            continue
        future_surge = _future_surge_label(
            detection_date=detection.signal_date,
            calendar=calendar,
            bars_by_date=by_date,
            facts=facts,
            symbol=symbol,
            horizon_days=horizon_days,
            surge_threshold=surge_threshold,
            cost_bps=cost_bps,
        )
        aggregator.record(detection, future_surge)
        if detection.censor is not None:
            code = detection.censor.value
            censored[code] = censored.get(code, 0) + 1
        elif detection.evidence is not None:
            evaluated += 1
            if detection.evidence.qualified:
                qualified_events.append(detection_payload(detection))
    stats = aggregator.summarize()
    canonical_identity = canonical.identity().model_dump(mode="json")
    market_identity = facts.identity().model_dump(mode="json")
    return {
        "schema": PRE_SURGE_SCHEMA,
        "status": "ok",
        "definition_version": "v1",
        "request": {
            "symbols": list(symbols),
            "start": start.isoformat(),
            "oos_start": oos_start.isoformat(),
            "end": end.isoformat(),
            "benchmark_symbol": benchmark_symbol,
            "horizon_days": horizon_days,
            "surge_threshold": surge_threshold,
            "cost_bps": cost_bps,
        },
        "identity": {
            "canonical": canonical_identity,
            "market_facts": market_identity,
            "universe": universe.identity().model_dump(mode="json"),
        },
        "coverage": {
            "symbols": len(symbols),
            "calendar_days": len(calendar),
            "evaluated_detections": evaluated,
            "qualified_events": len(qualified_events),
            "censored": censored,
            "pit_universe_ineligible": pit_universe_ineligible,
        },
        "factors": {
            variant: asdict(stats[variant]) if variant in stats else None
            for variant in ALL_VARIANTS
        },
        "events": qualified_events,
        "promoted": False,
    }


def evaluate_escape_risk_production(
    *,
    symbols: Sequence[str],
    start: date,
    end: date,
    canonical_reader: Any,
    cost_bps: float = 10.0,
) -> dict[str, object]:
    """Run the daily escape-risk detectors; minute signals stay unavailable."""
    try:
        canonical = PinnedCanonicalDailyReader(canonical_reader)
        calendar = _calendar_window(
            canonical,
            start,
            end,
            warmup_calendar_days=ESCAPE_WARMUP_CALENDAR_DAYS,
        )
        if not calendar:
            raise RuntimeError("canonical_calendar_empty")
        bars_by_symbol = {
            symbol: canonical.load_bars(symbol, calendar[0], calendar[-1]) for symbol in symbols
        }
    except (AttributeError, KeyError, OSError, RuntimeError, TypeError, ValueError) as exc:
        return {
            "schema": ESCAPE_SCHEMA,
            "status": "unavailable",
            "reason": "unavailable_pinned_daily_inputs",
            "detail": str(exc),
            "capabilities": dict(SIGNAL_CAPABILITIES),
            "promoted": False,
        }

    detectors = (EscapeS1Detector(), EscapeS8Detector(), EscapeS9Detector())
    detections = []
    for symbol, bars in bars_by_symbol.items():
        for detector in detectors:
            detections.extend(
                detection
                for detection in detector.detect(symbol, bars, calendar)
                if start <= detection.signal_date <= end
            )
    report = aggregate_escape_signals(
        detections,
        bars_by_symbol,
        cost_bps=cost_bps,
    )
    return {
        "schema": ESCAPE_SCHEMA,
        "status": "ok",
        "definition_version": "v1",
        "request": {
            "symbols": list(symbols),
            "start": start.isoformat(),
            "end": end.isoformat(),
            "cost_bps": cost_bps,
        },
        "identity": {
            "canonical": canonical.identity().model_dump(mode="json"),
        },
        "capabilities": {
            "daily": {key: SIGNAL_CAPABILITIES[key] for key in DAILY_SIGNAL_IDS},
            "minute": {key: SIGNAL_CAPABILITIES[key] for key in MINUTE_SIGNAL_IDS},
        },
        "report": asdict(report),
        "promoted": False,
    }


__all__ = [
    "ESCAPE_SCHEMA",
    "PRE_SURGE_SCHEMA",
    "evaluate_escape_risk_production",
    "evaluate_pre_surge_production",
]
