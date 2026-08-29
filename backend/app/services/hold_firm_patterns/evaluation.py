"""Auditable orchestration, execution, diagnostics, and verdicts for Issue #38."""

from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass
from datetime import date
from typing import Mapping, Sequence

from app.services.hold_firm_patterns.adapters import (
    PinnedCanonicalDailyReader,
    PinnedMarketFactsSource,
    PinnedPresenceUniverseReader,
    ProductionReaderScope,
    ProductionReaderScopeUnavailable,
    presence_universe_identity,
    pinned_market_facts_source,
    production_reader_scope,
    request_windows,
)
from app.services.hold_firm_patterns.breakout_pullback import BreakoutPullbackDetector
from app.services.hold_firm_patterns.first_yin import FirstYinDetector
from app.services.hold_firm_patterns.gentle_slope import GentleSlopeDetector
from app.services.hold_firm_patterns.models import (
    BOOTSTRAP_ROUNDS,
    BOOTSTRAP_SEED,
    DEFINITION_DOCUMENT,
    FACTOR_IDS,
    FORWARD_CHECKPOINT_DAYS,
    HORIZON_DAYS,
    MIN_VALID_BOOTSTRAP_REPLICATES,
    PRICE_ABS_TOL,
    Bar,
    CapabilityResult,
    Censor,
    CensorReason,
    DataIdentity,
    DenominatorAuditCode,
    DenominatorAuditEntry,
    DetectionEvidence,
    ExecutionSegment,
    FactorId,
    FactorResult,
    HoldFirmPatternsRequest,
    HoldFirmResponse,
    HoldFirmStatus,
    HoldFirmVerdict,
    HoldingArm,
    MarketFactsIdentity,
    ParentDetection,
    ParentEvent,
    PitUniverseStatus,
    Provenance,
    SelectionBucket,
    UnavailabilityReason,
    UniverseIdentity,
    combine_verdicts,
    validate_count_invariants,
    validate_factor_coverage,
    validate_group_partition,
    validate_holding_arm_alignment,
)
from app.services.hold_firm_patterns.platform_breakout import PlatformBreakoutDetector
from app.services.hold_firm_patterns.statistics import (
    BootstrapResult,
    gates,
    paired_cluster_bootstrap,
    selection_cluster_bootstrap,
)
from app.services.universe_presence_history import PresenceHistoryError

_DETECTORS = (
    FirstYinDetector,
    BreakoutPullbackDetector,
    GentleSlopeDetector,
    PlatformBreakoutDetector,
)


@dataclass(frozen=True, slots=True)
class _PreparedEvent:
    event: ParentEvent
    detection: ParentDetection


@dataclass(frozen=True, slots=True)
class _Simulation:
    fixed: ExecutionSegment
    dynamic: ExecutionSegment | None
    censors: tuple[Censor, ...]
    consecutive_limit_down_days: int


def _unavailable(reason: UnavailabilityReason) -> HoldFirmResponse:
    return HoldFirmResponse(
        status=HoldFirmStatus.UNAVAILABLE,
        unavailable_reason=reason,
    )


def _raw_identity(reader: object) -> MarketFactsIdentity:
    return MarketFactsIdentity(
        generation=str(getattr(reader, "generation")()),
        manifest_sha256=str(getattr(reader, "manifest_sha256")()),
    )


def _universe_identity(reader: object) -> UniverseIdentity:
    manifest = getattr(reader, "source_manifest")()
    return presence_universe_identity(manifest)


def assess_capability(
    reader: object,
    market_facts: object,
    universe_reader: object,
) -> CapabilityResult:
    """Return a non-throwing capability envelope over three pinned sources."""
    try:
        canonical = PinnedCanonicalDailyReader(reader)  # type: ignore[arg-type]
        identities = DataIdentity(
            canonical=canonical.identity(),
            markets=_raw_identity(market_facts),
            universe=_universe_identity(universe_reader),
        )
    except Exception as exc:  # capability probes must fail closed
        return CapabilityResult(
            status=HoldFirmStatus.UNAVAILABLE,
            problems=(str(exc),),
        )
    return CapabilityResult(status=HoldFirmStatus.OK, identities=identities)


def _one_price_at(value: float, bar: Bar, tolerance: float = PRICE_ABS_TOL) -> bool:
    return all(
        math.isfinite(candidate) and abs(candidate - value) <= tolerance
        for candidate in (
            bar.quote_open_raw,
            bar.quote_high_raw,
            bar.quote_low_raw,
            bar.quote_close_raw,
        )
    )


def _event_id(detection: ParentDetection) -> str:
    landmark = detection.landmark.landmark_date if detection.landmark else detection.anchor_date
    return (
        f"{detection.factor_id}:{detection.symbol}:"
        f"{detection.anchor_date.isoformat()}:{landmark.isoformat()}"
    )


def _membership_date(detection: ParentDetection) -> date:
    return (
        detection.landmark.landmark_date
        if detection.landmark is not None
        else detection.anchor_date
    )


def _membership_days(
    detections_by_factor: Sequence[Sequence[ParentDetection]],
) -> tuple[date, ...]:
    return tuple(
        sorted(
            {
                _membership_date(detection)
                for detections in detections_by_factor
                for detection in detections
            }
        )
    )


def _overlaps_active_horizon(prepared: _PreparedEvent, blocked_through: Mapping[str, date]) -> bool:
    landmark = prepared.detection.landmark
    if landmark is None:
        raise ValueError("materialized event lacks landmark")
    return landmark.landmark_date <= blocked_through.get(prepared.event.symbol, date.min)


def _materialize(
    factor_id: FactorId,
    detections: Sequence[ParentDetection],
    universe: PinnedPresenceUniverseReader,
) -> tuple[
    list[_PreparedEvent],
    list[_PreparedEvent],
    list[ParentEvent],
    list[ParentEvent],
    list[Censor],
]:
    qualified: list[_PreparedEvent] = []
    not_selected: list[_PreparedEvent] = []
    pit_ineligible: list[ParentEvent] = []
    selection_censored: list[ParentEvent] = []
    censors: list[Censor] = []
    for detection in detections:
        event_id = _event_id(detection)
        membership = universe.membership(detection.symbol, _membership_date(detection))
        if membership is PitUniverseStatus.NOT_IN_POOL:
            pit_ineligible.append(
                ParentEvent(
                    factor_id=factor_id,
                    event_id=event_id,
                    symbol=detection.symbol,
                    anchor_date=detection.anchor_date,
                    bucket=None,
                    pit_status=membership,
                    audit_code=DenominatorAuditCode.PIT_UNIVERSE_INELIGIBLE,
                )
            )
            continue
        if detection.censor is not None:
            censors.append(
                Censor(
                    factor_id=factor_id,
                    symbol=detection.symbol,
                    event_date=detection.anchor_date,
                    reason=detection.censor,
                    detail="detector",
                )
            )
            selection_censored.append(
                ParentEvent(
                    factor_id=factor_id,
                    event_id=event_id,
                    symbol=detection.symbol,
                    anchor_date=detection.anchor_date,
                    bucket=None,
                    pit_status=membership,
                    censor=detection.censor,
                )
            )
            continue
        if detection.landmark is None or detection.evidence is None:
            raise ValueError("facts-complete detection lacks landmark/evidence")
        bucket = (
            SelectionBucket.QUALIFIED
            if detection.evidence.qualified
            else SelectionBucket.NOT_SELECTED
        )
        event = ParentEvent(
            factor_id=factor_id,
            event_id=event_id,
            symbol=detection.symbol,
            anchor_date=detection.anchor_date,
            bucket=bucket,
            pit_status=membership,
        )
        prepared = _PreparedEvent(event=event, detection=detection)
        (qualified if bucket is SelectionBucket.QUALIFIED else not_selected).append(prepared)
    validate_group_partition(
        [item.event.event_id for item in qualified],
        [item.event.event_id for item in not_selected],
    )
    return qualified, not_selected, pit_ineligible, selection_censored, censors


def _ma_close(
    bars: Mapping[date, Bar], calendar: Sequence[date], index: int, window: int
) -> float | None:
    if index + 1 < window:
        return None
    values: list[float] = []
    for day in calendar[index - window + 1 : index + 1]:
        bar = bars.get(day)
        if bar is None:
            return None
        values.append(bar.research_close_adj)
    return sum(values) / window


def _mean_volume_before(
    bars: Mapping[date, Bar], calendar: Sequence[date], index: int, window: int
) -> float | None:
    if index < window:
        return None
    values: list[float] = []
    for day in calendar[index - window : index]:
        bar = bars.get(day)
        if bar is None or bar.volume <= 0:
            return None
        values.append(bar.volume)
    return sum(values) / window


def _defense_triggered(
    factor_id: FactorId,
    evidence: DetectionEvidence,
    bar: Bar,
    bars: Mapping[date, Bar],
    calendar: Sequence[date],
    index: int,
) -> bool:
    values = evidence.values
    if factor_id == FACTOR_IDS[0]:
        ma5 = _ma_close(bars, calendar, index, 5)
        return ma5 is not None and bar.research_close_adj < ma5
    if factor_id == FACTOR_IDS[1]:
        breakout = values.get("breakout")
        if not isinstance(breakout, Mapping):
            raise ValueError("F2 evidence lacks breakout")
        level = float(breakout["level_adj"])
        return bar.research_close_adj < level
    if factor_id == FACTOR_IDS[2]:
        ma20 = _ma_close(bars, calendar, index, 20)
        mean_volume = _mean_volume_before(bars, calendar, index, 20)
        if ma20 is None or mean_volume is None or bar.research_open_adj <= 0:
            return False
        stalled = (
            bar.volume >= 1.5 * mean_volume
            and abs(bar.research_close_adj / bar.research_open_adj - 1.0) <= 0.01
        )
        return bar.research_close_adj < ma20 or stalled
    entity_bottom = float(values["entity_bottom_adj"])
    return bar.research_close_adj < entity_bottom


def _segment(
    *,
    event: ParentEvent,
    arm: HoldingArm,
    entry_day: date,
    entry_bar: Bar,
    path_values: Sequence[float],
    cost_bps: float,
    exit_day: date | None,
    exit_quote_raw: float | None,
    pending_exit: bool,
    holding_days: int,
    pending_days: int,
    realized_exit: bool,
) -> ExecutionSegment:
    terminal_value = path_values[-1]
    exit_fee = cost_bps / 10_000.0
    liquidation_value = terminal_value if realized_exit else terminal_value * (1.0 - exit_fee)
    checkpoints = {
        checkpoint: path_values[checkpoint - 1] - 1.0 for checkpoint in FORWARD_CHECKPOINT_DAYS
    }
    return ExecutionSegment(
        event_id=event.event_id,
        factor_id=event.factor_id,
        symbol=event.symbol,
        arm=arm,
        entry_date=entry_day,
        entry_quote_raw=entry_bar.quote_open_raw,
        exit_date=exit_day,
        exit_quote_raw=exit_quote_raw,
        pending_exit=pending_exit,
        entry_cost_bps=cost_bps,
        exit_cost_bps=cost_bps if realized_exit else None,
        holding_days=holding_days,
        pending_days=pending_days,
        checkpoint_returns=checkpoints,
        terminal_return=terminal_value - 1.0,
        liquidation_cost_adjusted_terminal_return=liquidation_value - 1.0,
        mae=max(0.0, 1.0 - min(path_values)),
        mfe=max(0.0, max(path_values) - 1.0),
    )


def _simulate(
    prepared: _PreparedEvent,
    bars: Mapping[date, Bar],
    facts: PinnedMarketFactsSource,
    calendar: Sequence[date],
    cost_bps: float,
    *,
    with_dynamic: bool,
) -> tuple[_Simulation | None, Censor | None]:
    detection = prepared.detection
    if detection.landmark is None or detection.evidence is None:
        raise ValueError("simulation requires landmark/evidence")
    try:
        landmark_index = calendar.index(detection.landmark.landmark_date)
    except ValueError:
        return None, Censor(
            factor_id=prepared.event.factor_id,
            symbol=prepared.event.symbol,
            event_date=prepared.event.anchor_date,
            reason=CensorReason.SELECTION_WINDOW_INCOMPLETE,
            detail="landmark not in pinned calendar",
        )
    entry_index = landmark_index + 1
    end_index = entry_index + HORIZON_DAYS - 1
    if end_index >= len(calendar):
        return None, Censor(
            factor_id=prepared.event.factor_id,
            symbol=prepared.event.symbol,
            event_date=prepared.event.anchor_date,
            reason=CensorReason.HORIZON_INCOMPLETE,
            detail="common day-20 horizon unavailable",
        )
    days = tuple(calendar[entry_index : end_index + 1])
    path_bars = [bars.get(day) for day in days]
    if any(bar is None for bar in path_bars):
        return None, Censor(
            factor_id=prepared.event.factor_id,
            symbol=prepared.event.symbol,
            event_date=prepared.event.anchor_date,
            reason=CensorReason.HORIZON_INCOMPLETE,
            detail="canonical bar missing inside common horizon",
        )
    complete_bars = [bar for bar in path_bars if bar is not None]
    if any(facts.row(prepared.event.symbol, day) is None for day in days):
        raise ValueError("participating market facts incomplete")
    entry_bar = complete_bars[0]
    entry_fact = facts.row(prepared.event.symbol, days[0])
    if entry_fact is None:
        raise ValueError("entry facts missing")
    if not math.isfinite(entry_bar.quote_open_raw) or entry_bar.quote_open_raw <= 0:
        return None, Censor(
            factor_id=prepared.event.factor_id,
            symbol=prepared.event.symbol,
            event_date=prepared.event.anchor_date,
            reason=CensorReason.ENTRY_OPEN_INVALID,
            detail="entry raw open invalid",
        )
    if _one_price_at(entry_fact.published_limit_up, entry_bar):
        return None, Censor(
            factor_id=prepared.event.factor_id,
            symbol=prepared.event.symbol,
            event_date=prepared.event.anchor_date,
            reason=CensorReason.ENTRY_UNREACHABLE,
            detail="entry day is one-price limit-up",
        )
    if entry_bar.research_open_adj <= 0:
        return None, Censor(
            factor_id=prepared.event.factor_id,
            symbol=prepared.event.symbol,
            event_date=prepared.event.anchor_date,
            reason=CensorReason.ENTRY_OPEN_INVALID,
            detail="entry adjusted open invalid",
        )
    fee = cost_bps / 10_000.0
    entry_scale = (1.0 - fee) / entry_bar.research_open_adj
    fixed_values = [entry_scale * bar.research_close_adj for bar in complete_bars]
    fixed = _segment(
        event=prepared.event,
        arm=HoldingArm.FIXED_HOLD_20D,
        entry_day=days[0],
        entry_bar=entry_bar,
        path_values=fixed_values,
        cost_bps=cost_bps,
        exit_day=None,
        exit_quote_raw=None,
        pending_exit=False,
        holding_days=HORIZON_DAYS,
        pending_days=0,
        realized_exit=False,
    )
    if not with_dynamic:
        return _Simulation(
            fixed=fixed,
            dynamic=None,
            censors=(),
            consecutive_limit_down_days=0,
        ), None

    trigger_index: int | None = None
    exit_index: int | None = None
    pending_days = 0
    for offset, bar in enumerate(complete_bars):
        calendar_index = entry_index + offset
        if trigger_index is None and _defense_triggered(
            prepared.event.factor_id,
            detection.evidence,
            bar,
            bars,
            calendar,
            calendar_index,
        ):
            trigger_index = offset
            continue
        if trigger_index is None or offset <= trigger_index:
            continue
        fact = facts.row(prepared.event.symbol, days[offset])
        if fact is None:
            raise ValueError("exit facts missing")
        if _one_price_at(fact.published_limit_down, bar):
            pending_days += 1
            continue
        exit_index = offset
        break

    dynamic_values: list[float] = []
    realized_exit = exit_index is not None
    cash_value: float | None = None
    for offset, bar in enumerate(complete_bars):
        if exit_index is not None and offset >= exit_index:
            if cash_value is None:
                cash_value = entry_scale * bar.research_open_adj * (1.0 - fee)
            dynamic_values.append(cash_value)
        else:
            dynamic_values.append(entry_scale * bar.research_close_adj)
    pending = trigger_index is not None and exit_index is None
    dynamic = _segment(
        event=prepared.event,
        arm=HoldingArm.DYNAMIC_DEFENSE,
        entry_day=days[0],
        entry_bar=entry_bar,
        path_values=dynamic_values,
        cost_bps=cost_bps,
        exit_day=days[exit_index] if exit_index is not None else None,
        exit_quote_raw=(
            complete_bars[exit_index].quote_open_raw if exit_index is not None else None
        ),
        pending_exit=pending,
        holding_days=(exit_index + 1 if exit_index is not None else HORIZON_DAYS),
        pending_days=pending_days,
        realized_exit=realized_exit,
    )
    realization_censors: tuple[Censor, ...] = ()
    if pending:
        realization_censors = (
            Censor(
                factor_id=prepared.event.factor_id,
                symbol=prepared.event.symbol,
                event_date=prepared.event.anchor_date,
                reason=CensorReason.PENDING_EXIT,
                detail="defense triggered but exit remained unreachable through day 20",
            ),
        )
    return _Simulation(
        fixed=fixed,
        dynamic=dynamic,
        censors=realization_censors,
        consecutive_limit_down_days=pending_days,
    ), None


def _bootstrap_payload(result: BootstrapResult) -> dict[str, object]:
    return {
        "mean_difference": result.mean_difference,
        "ci95": [result.lower, result.upper],
        "valid_replicates": result.valid_replicates,
        "rounds": result.rounds,
    }


def _split_statistics(
    qualified: Sequence[tuple[_PreparedEvent, _Simulation]],
    not_selected: Sequence[tuple[_PreparedEvent, _Simulation]],
) -> tuple[dict[str, object], HoldFirmVerdict, HoldFirmVerdict]:
    qualified_returns: dict[str, list[float]] = defaultdict(list)
    not_selected_returns: dict[str, list[float]] = defaultdict(list)
    pairs: dict[str, tuple[str, float, float]] = {}
    for prepared, simulation in qualified:
        qualified_returns[prepared.event.symbol].append(
            simulation.fixed.liquidation_cost_adjusted_terminal_return
        )
        if simulation.dynamic is not None:
            pairs[prepared.event.event_id] = (
                prepared.event.symbol,
                simulation.dynamic.liquidation_cost_adjusted_terminal_return
                - simulation.fixed.liquidation_cost_adjusted_terminal_return,
                simulation.dynamic.mae - simulation.fixed.mae,
            )
    for prepared, simulation in not_selected:
        not_selected_returns[prepared.event.symbol].append(
            simulation.fixed.liquidation_cost_adjusted_terminal_return
        )

    selection_bootstrap = selection_cluster_bootstrap(qualified_returns, not_selected_returns)
    selection_gate = gates(
        sum(len(values) for values in qualified_returns.values()),
        len(qualified_returns),
    ) and gates(
        sum(len(values) for values in not_selected_returns.values()),
        len(not_selected_returns),
    )
    if not selection_gate or selection_bootstrap.valid_replicates < MIN_VALID_BOOTSTRAP_REPLICATES:
        selection_verdict = HoldFirmVerdict.UNAVAILABLE
    elif selection_bootstrap.lower is not None and selection_bootstrap.lower > 0:
        selection_verdict = HoldFirmVerdict.ACCEPTED
    else:
        selection_verdict = HoldFirmVerdict.REJECTED

    return_bootstrap, mae_bootstrap = paired_cluster_bootstrap(pairs)
    holding_gate = gates(len(pairs), len({value[0] for value in pairs.values()}))
    holding_ci_complete = (
        return_bootstrap.valid_replicates >= MIN_VALID_BOOTSTRAP_REPLICATES
        and mae_bootstrap.valid_replicates >= MIN_VALID_BOOTSTRAP_REPLICATES
        and return_bootstrap.lower is not None
        and mae_bootstrap.upper is not None
    )
    if not holding_gate or not holding_ci_complete:
        holding_verdict = HoldFirmVerdict.UNAVAILABLE
    elif return_bootstrap.lower >= 0 and mae_bootstrap.upper < 0:
        holding_verdict = HoldFirmVerdict.ACCEPTED
    else:
        holding_verdict = HoldFirmVerdict.REJECTED

    payload = {
        "qualified_complete_events": sum(len(values) for values in qualified_returns.values()),
        "qualified_unique_symbols": len(qualified_returns),
        "not_selected_complete_events": sum(
            len(values) for values in not_selected_returns.values()
        ),
        "not_selected_unique_symbols": len(not_selected_returns),
        "holding_paired_events": len(pairs),
        "holding_unique_symbols": len({value[0] for value in pairs.values()}),
        "selection": _bootstrap_payload(selection_bootstrap),
        "holding_return": _bootstrap_payload(return_bootstrap),
        "holding_mae": _bootstrap_payload(mae_bootstrap),
    }
    return payload, selection_verdict, holding_verdict


def _diagnostics(
    factor_id: FactorId,
    qualified: Sequence[_PreparedEvent],
    bars_by_symbol: Mapping[str, Mapping[date, Bar]],
    calendar: Sequence[date],
    simulations: Mapping[str, _Simulation],
    censors: list[Censor],
) -> dict[str, object]:
    result: dict[str, object] = {"market_regime": "unavailable"}
    if factor_id == FACTOR_IDS[0]:
        qualified_ids = {item.event.event_id for item in qualified}
        qualified_simulations = [
            simulation for event_id, simulation in simulations.items() if event_id in qualified_ids
        ]
        pending = [
            simulation.dynamic.pending_days
            for simulation in qualified_simulations
            if simulation.dynamic is not None and simulation.dynamic.pending_exit
        ]
        unreachable = [
            simulation.consecutive_limit_down_days
            for simulation in qualified_simulations
            if simulation.consecutive_limit_down_days > 0
        ]
        denominator = len(qualified_simulations)
        result.update(
            volume_states=[
                item.detection.evidence.values.get("volume_state")
                for item in qualified
                if item.detection.evidence is not None
            ],
            qualified_simulated_events=denominator,
            consecutive_limit_down_days=unreachable,
            max_consecutive_limit_down_days=max(unreachable, default=0),
            unreachable_exit_events=len(unreachable),
            unreachable_exit_event_ratio=(len(unreachable) / denominator if denominator else None),
            pending_exit_events=len(pending),
            pending_exit_event_ratio=(len(pending) / denominator if denominator else None),
            pending_exit_days=sum(pending),
        )
    elif factor_id == FACTOR_IDS[1]:
        fake_blocks: list[object] = []
        for item in qualified:
            evidence = item.detection.evidence
            if evidence is None:
                continue
            diagnostics = evidence.values.get("diagnostics")
            if isinstance(diagnostics, Mapping):
                fake_blocks.append(diagnostics.get("fake_breakout"))
        result["fake_breakout"] = fake_blocks
    elif factor_id == FACTOR_IDS[2]:
        result.update(
            hypothesis_label="control_inference_unverified",
            liquidity_diagnostic_inputs=[
                item.detection.evidence.values.get("liquidity_diagnostic_inputs")
                for item in qualified
                if item.detection.evidence is not None
            ],
        )
    else:
        counts: dict[str, int] = defaultdict(int)
        for item in qualified:
            detection = item.detection
            if detection.landmark is None or detection.evidence is None:
                continue
            try:
                anchor_index = calendar.index(detection.anchor_date)
            except ValueError:
                continue
            days = tuple(calendar[anchor_index + 1 : anchor_index + 6])
            bars = bars_by_symbol[item.event.symbol]
            if len(days) != 5 or any(day not in bars for day in days):
                counts["diagnostic_censored"] += 1
                censors.append(
                    Censor(
                        factor_id=factor_id,
                        symbol=item.event.symbol,
                        event_date=detection.anchor_date,
                        reason=CensorReason.DIAGNOSTIC_WINDOW_INCOMPLETE,
                        detail="F4 five-day strength window incomplete",
                    )
                )
                continue
            values = detection.evidence.values
            entity_bottom = float(values["entity_bottom_adj"])
            breakout_close = float(values["breakout_close_adj"])
            platform_high = float(values["platform_high_adj"])
            closes = [bars[day].research_close_adj for day in days]
            if any(value < entity_bottom for value in closes):
                bucket = "broken"
            elif all(value >= breakout_close for value in closes):
                bucket = "very_strong"
            elif all(value >= entity_bottom for value in closes):
                bucket = "strong"
            else:
                bucket = "unclassified"
            counts[bucket] += 1
            if any(value < platform_high for value in closes):
                counts["fake_breakout"] += 1
        result["strength_buckets"] = dict(counts)
    return result


def evaluate_hold_firm_patterns(
    request: HoldFirmPatternsRequest,
    reader: object,
    market_facts: object,
    universe_reader: object,
) -> HoldFirmResponse:
    """Evaluate four independent factors over one immutable three-source run."""
    try:
        canonical = PinnedCanonicalDailyReader(reader)  # type: ignore[arg-type]
        full_days, _event_days, bar_start, bar_end = request_windows(
            reader,
            request.start,
            request.end,  # type: ignore[arg-type]
        )
    except (OSError, RuntimeError, TypeError, ValueError):
        return _unavailable(UnavailabilityReason.CANONICAL_READER)
    if not full_days:
        return _unavailable(UnavailabilityReason.CANONICAL_READER)

    try:
        facts = pinned_market_facts_source(market_facts, request.symbols, full_days)
        bars_by_symbol = {
            symbol: {bar.date: bar for bar in canonical.load_bars(symbol, bar_start, bar_end)}
            for symbol in request.symbols
        }
        if facts.incomplete_rows:
            return _unavailable(UnavailabilityReason.MARKET_FACTS_INCOMPLETE)
        for symbol, bars in bars_by_symbol.items():
            if any(not facts.covers(symbol, day) for day in bars):
                return _unavailable(UnavailabilityReason.MARKET_FACTS_INCOMPLETE)
    except (OSError, RuntimeError, TypeError, ValueError):
        return _unavailable(UnavailabilityReason.MARKET_FACTS_INCOMPLETE)

    detections_by_factor = tuple(
        tuple(
            detection
            for symbol in request.symbols
            for detection in detector_type().detect(
                symbol,
                tuple(bars_by_symbol[symbol].values()),
                facts,
                full_days,
            )
            if request.start <= detection.anchor_date <= request.end
        )
        for detector_type in _DETECTORS
    )

    try:
        universe = PinnedPresenceUniverseReader(  # type: ignore[arg-type]
            universe_reader,
            _membership_days(detections_by_factor),
        )
    except (OSError, PresenceHistoryError, RuntimeError, TypeError, ValueError):
        return _unavailable(UnavailabilityReason.UNIVERSE_PRESENCE)

    try:
        identities = DataIdentity(
            canonical=canonical.identity(),
            markets=facts.identity(),
            universe=universe.identity(),
        )
        provenance = Provenance(
            identities=identities,
            calendar_id=canonical.identity().calendar_id,
            parameters={
                "symbols": request.symbols,
                "start": request.start.isoformat(),
                "oos_start": request.oos_start.isoformat(),
                "end": request.end.isoformat(),
                "cost_bps": request.cost_bps,
                "horizon_days": HORIZON_DAYS,
                "bootstrap_seed": BOOTSTRAP_SEED,
                "bootstrap_rounds": BOOTSTRAP_ROUNDS,
                "min_valid_bootstrap_replicates": MIN_VALID_BOOTSTRAP_REPLICATES,
            },
            params_provenance={
                "definition": DEFINITION_DOCUMENT,
                "numeric_thresholds": "Issue #38 research-defined; not attributed to the source video",
            },
            code_version="issue38-hold-firm-patterns-v1",
        )
    except (TypeError, ValueError):
        return _unavailable(UnavailabilityReason.INVALID_PROVENANCE)

    results: list[FactorResult] = []
    for factor_id, detections in zip(FACTOR_IDS, detections_by_factor):
        try:
            qualified, not_selected, pit, selection_censored, censors = _materialize(
                factor_id, detections, universe
            )
        except (PresenceHistoryError, ValueError):
            return _unavailable(UnavailabilityReason.UNIVERSE_PRESENCE)
        complete_qualified: list[tuple[_PreparedEvent, _Simulation]] = []
        complete_not_selected: list[tuple[_PreparedEvent, _Simulation]] = []
        all_segments: list[ExecutionSegment] = []
        simulation_by_event: dict[str, _Simulation] = {}
        blocked_through: dict[str, date] = {}
        candidates = [
            *((item, True) for item in qualified),
            *((item, False) for item in not_selected),
        ]
        candidates.sort(
            key=lambda pair: (
                pair[0].event.symbol,
                pair[0].detection.landmark.landmark_date
                if pair[0].detection.landmark is not None
                else pair[0].event.anchor_date,
                pair[0].event.event_id,
            )
        )
        try:
            for item, is_qualified in candidates:
                landmark = item.detection.landmark
                if landmark is None:
                    raise ValueError("materialized event lacks landmark")
                if _overlaps_active_horizon(item, blocked_through):
                    censors.append(
                        Censor(
                            factor_id=factor_id,
                            symbol=item.event.symbol,
                            event_date=item.event.anchor_date,
                            reason=CensorReason.EVENT_OVERLAP,
                            detail="same factor/symbol event overlaps an active common horizon",
                        )
                    )
                    continue
                simulation, censor = _simulate(
                    item,
                    bars_by_symbol[item.event.symbol],
                    facts,
                    full_days,
                    request.cost_bps,
                    with_dynamic=is_qualified,
                )
                if censor is not None:
                    censors.append(censor)
                    continue
                if simulation is None:
                    continue
                landmark_index = full_days.index(landmark.landmark_date)
                # A new signal may form at the prior event's day-20 close:
                # its entry is the following market day, after that horizon.
                blocked_through[item.event.symbol] = full_days[landmark_index + HORIZON_DAYS - 1]
                simulation_by_event[item.event.event_id] = simulation
                all_segments.append(simulation.fixed)
                if simulation.dynamic is not None:
                    all_segments.append(simulation.dynamic)
                censors.extend(simulation.censors)
                (complete_qualified if is_qualified else complete_not_selected).append(
                    (item, simulation)
                )
        except ValueError:
            return _unavailable(UnavailabilityReason.MARKET_FACTS_INCOMPLETE)

        is_qualified = [
            pair
            for pair in complete_qualified
            if pair[0].detection.landmark is not None
            and pair[0].detection.landmark.landmark_date < request.oos_start
        ]
        is_not_selected = [
            pair
            for pair in complete_not_selected
            if pair[0].detection.landmark is not None
            and pair[0].detection.landmark.landmark_date < request.oos_start
        ]
        oos_qualified = [
            pair
            for pair in complete_qualified
            if pair[0].detection.landmark is not None
            and pair[0].detection.landmark.landmark_date >= request.oos_start
        ]
        oos_not_selected = [
            pair
            for pair in complete_not_selected
            if pair[0].detection.landmark is not None
            and pair[0].detection.landmark.landmark_date >= request.oos_start
        ]
        is_payload, _, _ = _split_statistics(is_qualified, is_not_selected)
        oos_payload, selection_verdict, holding_verdict = _split_statistics(
            oos_qualified, oos_not_selected
        )
        diagnostics = _diagnostics(
            factor_id,
            qualified,
            bars_by_symbol,
            full_days,
            simulation_by_event,
            censors,
        )
        validate_count_invariants(
            len(qualified) + len(not_selected) + len(pit) + len(selection_censored),
            len(qualified),
            len(not_selected),
            len(pit),
            len(selection_censored),
        )
        qualified_ids = {item.event.event_id for item, _simulation in complete_qualified}
        validate_holding_arm_alignment(
            [
                segment.event_id
                for segment in all_segments
                if segment.arm is HoldingArm.DYNAMIC_DEFENSE
            ],
            [
                segment.event_id
                for segment in all_segments
                if segment.arm is HoldingArm.FIXED_HOLD_20D and segment.event_id in qualified_ids
            ],
        )
        results.append(
            FactorResult(
                factor_id=factor_id,
                parent_events=(
                    len(qualified) + len(not_selected) + len(pit) + len(selection_censored)
                ),
                qualified_events=len(qualified),
                not_selected_events=len(not_selected),
                segments=all_segments,
                censored=censors,
                denominator_audit=[
                    DenominatorAuditEntry(
                        event_id=event.event_id,
                        factor_id=factor_id,
                        symbol=event.symbol,
                        event_date=event.anchor_date,
                        code=DenominatorAuditCode.PIT_UNIVERSE_INELIGIBLE,
                    )
                    for event in pit
                ],
                is_diagnostic=is_payload,
                oos=oos_payload,
                diagnostics=diagnostics,
                selection_verdict=selection_verdict,
                holding_verdict=holding_verdict,
                verdict=combine_verdicts(selection_verdict, holding_verdict),
            )
        )
    validate_factor_coverage(results)
    return HoldFirmResponse(
        status=HoldFirmStatus.OK,
        factors=results,
        provenance=provenance,
    )


__all__ = [
    "ProductionReaderScope",
    "ProductionReaderScopeUnavailable",
    "production_reader_scope",
    "assess_capability",
    "evaluate_hold_firm_patterns",
]
