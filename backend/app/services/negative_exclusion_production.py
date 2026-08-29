"""Pinned production orchestration for the Issue #50 negative-exclusion study."""

from __future__ import annotations

from dataclasses import asdict
from datetime import date, timedelta
from typing import Any, Sequence

from app.services.hold_firm_patterns.adapters import (
    PinnedCanonicalDailyReader,
    PinnedPresenceUniverseReader,
    pinned_market_facts_source,
)
from app.services.negative_exclusion import (
    CLASS_V2,
    CLASS_V4,
    CLASS_V5,
    ClassSignal,
    ObservationRow,
    PitNegativeFact,
    SignalState,
    aggregate_exclusion,
    capability_report,
    detect_v2,
    detect_v4_series,
    detect_v5_series,
    require_available_class,
)
from app.services.universe_presence_history import PresenceHistoryError

SCHEMA = "negative_exclusion_research/production/v1"
WARMUP_CALENDAR_DAYS = 240
FORWARD_CALENDAR_DAYS = 90


def _unavailable(reason: str, detail: str) -> dict[str, object]:
    return {
        "schema": SCHEMA,
        "status": "unavailable",
        "reason": reason,
        "detail": detail,
        "capabilities": capability_report(),
        "promoted": False,
    }


def evaluate_negative_exclusion_production(
    *,
    symbols: Sequence[str],
    start: date,
    oos_start: date,
    end: date,
    canonical_reader: Any,
    market_facts_reader: Any,
    universe_reader: Any,
    enabled_classes: Sequence[str] | None = None,
    horizon_days: int = 10,
    cost_bps: float = 20.0,
) -> dict[str, object]:
    """Compare the unfiltered PIT pool with per-class and combined exclusions."""
    if not (start <= oos_start <= end):
        raise ValueError("dates must satisfy start <= oos_start <= end")
    if horizon_days <= 0:
        raise ValueError("horizon_days must be positive")
    if cost_bps < 0:
        raise ValueError("cost_bps must be non-negative")
    requested_symbols = tuple(sorted({str(symbol) for symbol in symbols if str(symbol)}))
    if not requested_symbols:
        raise ValueError("symbols must not be empty")
    resolved_enabled = (
        tuple(enabled_classes)
        if enabled_classes is not None
        else (
            CLASS_V2,
            CLASS_V4,
            CLASS_V5,
        )
    )
    for class_id in resolved_enabled:
        require_available_class(class_id)

    try:
        canonical = PinnedCanonicalDailyReader(canonical_reader)
        calendar = canonical.trading_days(
            start - timedelta(days=WARMUP_CALENDAR_DAYS),
            end + timedelta(days=FORWARD_CALENDAR_DAYS),
        )
        if not calendar:
            raise RuntimeError("canonical_calendar_empty")
        oos_days = tuple(day for day in calendar if oos_start <= day <= end)
        if not oos_days:
            raise RuntimeError("oos_market_days_empty")
        # Non-overlapping rebalance cohorts make compounded return/Sharpe a
        # portfolio statistic rather than a stack of overlapping forward labels.
        rebalance_days = oos_days[::horizon_days]
        facts = pinned_market_facts_source(market_facts_reader, requested_symbols, rebalance_days)
        universe = PinnedPresenceUniverseReader(universe_reader, rebalance_days)
        bars_by_symbol = {
            symbol: canonical.load_bars(symbol, calendar[0], calendar[-1])
            for symbol in requested_symbols
        }
    except (AttributeError, KeyError, OSError, RuntimeError, TypeError, ValueError) as exc:
        return _unavailable("unavailable_pinned_daily_inputs", str(exc))

    observations: list[ObservationRow] = []
    censored: dict[str, int] = {}
    index_by_day = {day: index for index, day in enumerate(calendar)}
    for symbol, bars in bars_by_symbol.items():
        if not bars:
            censored["canonical_symbol_empty"] = censored.get("canonical_symbol_empty", 0) + 1
            continue
        by_date = {bar.date: bar for bar in bars}
        close_values = [bar.research_close_adj for bar in bars]
        v4_states = detect_v4_series(close_values)
        v5_states = detect_v5_series(bars)
        state_by_date_v4 = {bar.date: state for bar, state in zip(bars, v4_states)}
        state_by_date_v5 = {bar.date: state for bar, state in zip(bars, v5_states)}
        for day in rebalance_days:
            try:
                universe.membership(symbol, day)
            except PresenceHistoryError:
                censored["universe_membership_unproven"] = (
                    censored.get("universe_membership_unproven", 0) + 1
                )
                continue
            position = index_by_day[day]
            entry_position = position + 1
            exit_position = entry_position + horizon_days - 1
            if entry_position >= len(calendar) or exit_position >= len(calendar):
                censored["forward_window_truncated"] = (
                    censored.get("forward_window_truncated", 0) + 1
                )
                continue
            entry_day = calendar[entry_position]
            exit_day = calendar[exit_position]
            entry_bar = by_date.get(entry_day)
            exit_bar = by_date.get(exit_day)
            if entry_bar is None or exit_bar is None:
                censored["forward_bars_missing"] = censored.get("forward_bars_missing", 0) + 1
                continue
            fact = facts.row(symbol, day)
            v2 = detect_v2(None if fact is None else PitNegativeFact(is_st=fact.is_st))
            signals = {
                CLASS_V2: v2,
                CLASS_V4: state_by_date_v4.get(
                    day, ClassSignal(SignalState.CENSORED, "canonical_day_missing")
                ),
                CLASS_V5: state_by_date_v5.get(
                    day, ClassSignal(SignalState.CENSORED, "canonical_day_missing")
                ),
            }
            observations.append(
                ObservationRow(
                    symbol=symbol,
                    date=day,
                    forward_return=(
                        exit_bar.research_close_adj / entry_bar.research_open_adj
                        - 1
                        - cost_bps / 10_000.0
                    ),
                    signals=signals,
                )
            )

    request_payload = {
        "start": start,
        "oos_start": oos_start,
        "end": end,
        "symbols": list(requested_symbols),
        "enabled_classes": list(resolved_enabled),
        "horizon_days": horizon_days,
        "cost_bps": cost_bps,
        "rebalance_rule": "non_overlapping_horizon_cohorts",
    }
    provenance = {
        "canonical": canonical.identity().model_dump(mode="json"),
        "market_facts": facts.identity().model_dump(mode="json"),
        "universe": universe.identity().model_dump(mode="json"),
        "definition": "docs/ISSUE-50/final-design.md",
    }
    coverage = {
        "observations": len(observations),
        "rebalance_days": len(rebalance_days),
        "market_facts_incomplete_rows": facts.incomplete_rows,
        "censored": censored,
    }
    if not observations:
        return {
            **_unavailable(
                "unavailable_no_evaluable_observations",
                "all pinned OOS rows were censored before aggregation",
            ),
            "request": request_payload,
            "provenance": provenance,
            "coverage": coverage,
        }

    try:
        aggregate = aggregate_exclusion(
            observations,
            enabled_classes=resolved_enabled,
            periods_per_year=252.0 / horizon_days,
        )
    except ValueError as exc:
        raise ValueError(f"invalid negative exclusion request: {exc}") from exc
    return {
        "schema": SCHEMA,
        "status": "ok",
        "request": request_payload,
        "provenance": provenance,
        "coverage": coverage,
        "capabilities": capability_report(),
        "evaluation": asdict(aggregate),
        "promoted": False,
    }


__all__ = ["SCHEMA", "evaluate_negative_exclusion_production"]
