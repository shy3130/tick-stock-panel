"""Production orchestration for the Issue #45/#47/#48 daily research modules."""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from datetime import date, timedelta
from typing import Any

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
from .escape_risk_intraday import detect_intraday_escape_signals
from .pre_surge import (
    ALL_VARIANTS,
    RISK_METRIC_DEFINITIONS,
    PreSurgeArmEventReturn,
    PreSurgeArmRiskLedger,
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


@dataclass(frozen=True, slots=True)
class _SurgeOutcome:
    label: bool | None
    net_return: float | None
    reachable: bool
    complete: bool
    entry_date: date | None = None
    exit_date: date | None = None


def _surge_outcome(
    *,
    detection_date: date,
    calendar: Sequence[date],
    bars_by_date: dict[date, Bar],
    facts: Any,
    symbol: str,
    horizon_days: int,
    surge_threshold: float,
    cost_bps: float,
) -> _SurgeOutcome:
    positions = {day: index for index, day in enumerate(calendar)}
    signal_index = positions.get(detection_date)
    if signal_index is None:
        return _SurgeOutcome(None, None, False, False)
    entry_index = signal_index + 1
    exit_index = entry_index + horizon_days - 1
    if entry_index >= len(calendar) or exit_index >= len(calendar):
        return _SurgeOutcome(None, None, False, False)
    entry_date = calendar[entry_index]
    exit_date = calendar[exit_index]
    entry_bar = bars_by_date.get(entry_date)
    exit_bar = bars_by_date.get(exit_date)
    entry_fact: MarketFactsRow | None = facts.row(symbol, entry_date)
    exit_fact: MarketFactsRow | None = facts.row(symbol, exit_date)
    if entry_bar is None or exit_bar is None or entry_fact is None or exit_fact is None:
        return _SurgeOutcome(None, None, False, False, entry_date, exit_date)
    if entry_bar.research_open_adj <= 0:
        return _SurgeOutcome(None, None, False, False, entry_date, exit_date)
    if _one_price_at(entry_fact.published_limit_up, entry_bar):
        return _SurgeOutcome(None, None, False, True, entry_date, exit_date)
    if _one_price_at(exit_fact.published_limit_down, exit_bar):
        return _SurgeOutcome(None, None, False, True, entry_date, exit_date)
    gross = exit_bar.research_close_adj / entry_bar.research_open_adj - 1.0
    one_way_cost = cost_bps / 10_000.0
    net = (1.0 + gross) * (1.0 - one_way_cost) ** 2 - 1.0
    return _SurgeOutcome(net >= surge_threshold, net, True, True, entry_date, exit_date)


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
    risk_ledger = PreSurgeArmRiskLedger()
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
        outcome = _surge_outcome(
            detection_date=detection.signal_date,
            calendar=calendar,
            bars_by_date=by_date,
            facts=facts,
            symbol=symbol,
            horizon_days=horizon_days,
            surge_threshold=surge_threshold,
            cost_bps=cost_bps,
        )
        aggregator.record(detection, outcome.label)
        if detection.censor is not None:
            code = detection.censor.value
            censored[code] = censored.get(code, 0) + 1
        elif detection.evidence is not None:
            evaluated += 1
            if detection.evidence.qualified:
                qualified_events.append(detection_payload(detection))
                if (
                    outcome.complete
                    and outcome.entry_date is not None
                    and outcome.exit_date is not None
                ):
                    risk_ledger.record(
                        detection.variant,
                        PreSurgeArmEventReturn(
                            entry_date=outcome.entry_date,
                            exit_date=outcome.exit_date,
                            net_return=outcome.net_return,
                            reachable=outcome.reachable,
                        ),
                    )
    stats = aggregator.summarize()
    risk_metrics = risk_ledger.metrics()
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
        "risk_metric_definitions": dict(RISK_METRIC_DEFINITIONS),
        "risk_metrics": {
            variant: asdict(risk_metrics[variant]) if variant in risk_metrics else None
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
    oos_start: date,
    canonical_reader: Any,
    cost_bps: float = 10.0,
    intraday_reader: Any | None = None,
) -> dict[str, object]:
    """Run all Issue #48 signals; missing intraday inputs censor only S2-S7/S10."""
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

    external_censors: dict[str, tuple[str, ...]] = {}
    intraday_identity: dict[str, object] | None = None
    intraday_coverage: dict[str, int] = {
        "requested_pairs": 0,
        "available_pairs": 0,
        "unavailable_pairs": 0,
    }
    intraday_status = "unavailable_reader"
    if intraday_reader is not None:
        start_position = next(
            (index for index, day in enumerate(calendar) if day >= start), len(calendar)
        )
        end_position = max(
            (index for index, day in enumerate(calendar) if day <= end),
            default=-1,
        )
        intraday_calendar = calendar[max(0, start_position - 5) : end_position + 1]
        try:
            bundle = intraday_reader.load(symbols)
            intraday_result = detect_intraday_escape_signals(
                bundle,
                symbols=symbols,
                calendar=intraday_calendar,
                start=start,
                end=end,
            )
            detections.extend(intraday_result.detections)
            external_censors = dict(intraday_result.censor_codes)
            intraday_coverage = dict(intraday_result.coverage)
            intraday_identity = intraday_reader.run_manifest()
            intraday_status = "available"
        except (AttributeError, OSError, RuntimeError, TypeError, ValueError) as exc:
            intraday_status = f"unavailable_reader:{exc}"
    if intraday_status != "available":
        external_censors = {
            signal_id: ("censor_intraday_data_missing",) for signal_id in MINUTE_SIGNAL_IDS
        }
    report = aggregate_escape_signals(
        detections,
        bars_by_symbol,
        cost_bps=cost_bps,
        oos_start=oos_start,
        external_censor_codes=external_censors,
    )
    return {
        "schema": ESCAPE_SCHEMA,
        "status": "ok",
        "definition_version": "v1",
        "request": {
            "symbols": list(symbols),
            "start": start.isoformat(),
            "end": end.isoformat(),
            "oos_start": oos_start.isoformat(),
            "cost_bps": cost_bps,
        },
        "identity": {
            "canonical": canonical.identity().model_dump(mode="json"),
            "intraday": intraday_identity,
        },
        "capabilities": {
            "daily": {key: SIGNAL_CAPABILITIES[key] for key in DAILY_SIGNAL_IDS},
            "intraday": {
                "signals": {key: SIGNAL_CAPABILITIES[key] for key in MINUTE_SIGNAL_IDS},
                "runtime_status": intraday_status,
                "coverage": intraday_coverage,
            },
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
