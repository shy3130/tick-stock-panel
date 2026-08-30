"""C1-C5 detectors and fail-closed chip-peak evaluation."""

from __future__ import annotations

import math
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from statistics import median

from app.services.hold_firm_patterns.statistics import gates, selection_cluster_bootstrap

from .adapters import ChipReaderBundle, request_windows, resolve_pit_turnover, row_limit_up
from .chip_distribution import MissingPitTurnoverError
from .features import ChipDayFeatures, build_feature_series, c2_holds
from .models import (
    BETA_ARMS,
    BOOTSTRAP_ROUNDS,
    BOOTSTRAP_SEED,
    C1_MAX_PEAKS,
    C1_MAX_PRICE_PCT,
    C1_MIN_CONCENTRATION,
    C1_MIN_WINNER,
    C2_LOOKBACK,
    C2_MIN_LOW_PEAK_SHARE,
    C2_MIN_PEAKS,
    C3_HIGH_MAIN_PCT,
    C3_LOW_MAIN_PCT,
    C3_WINDOW,
    C4_MIN_CONCENTRATION,
    C4_MIN_WINNER,
    C5_MAX_CONCENTRATION,
    C5_MIN_PEAKS,
    FACTOR_IDS,
    FORWARD_HORIZONS,
    MAIN_ARM,
    MIN_OOS_EVENTS,
    MIN_OOS_SYMBOLS,
    MIN_VALID_BOOTSTRAP_REPLICATES,
    PRICE_ABS_TOL,
    ArmResearch,
    BetaArm,
    CapabilityResult,
    ChipBar,
    ChipBetaStability,
    ChipCensor,
    ChipCensorReason,
    ChipDataIdentity,
    ChipDenominatorAuditEntry,
    ChipFactorResult,
    ChipModelParams,
    ChipPeakRequest,
    ChipPeakResponse,
    ChipProvenance,
    ChipStatus,
    ChipSymbolAudit,
    ChipTurnoverIdentity,
    ChipVerdict,
    FactorId,
    UnavailabilityReason,
)

PIT_TURNOVER_SOURCE = "pit_float_shares_notice_date"
PRIMARY_HORIZON = min(FORWARD_HORIZONS)


@dataclass(frozen=True)
class ChipEvent:
    factor_id: FactorId
    symbol: str
    signal_date: date
    bucket: str
    index: int
    prior_run_60: float | None = None


@dataclass(frozen=True)
class _Outcome:
    event: ChipEvent
    entry_date: date
    returns: Mapping[int, float]

    @property
    def primary(self) -> float:
        return self.returns[PRIMARY_HORIZON]


def beta_stability(directions: Mapping[str, str | None]) -> ChipBetaStability:
    valid = [v for v in directions.values() if v is not None]
    counts = Counter(valid)
    consensus, votes = (None, 0) if not counts else counts.most_common(1)[0]
    stable = (
        directions.get(MAIN_ARM) is not None
        and consensus == directions.get(MAIN_ARM)
        and votes >= 2
    )
    return ChipBetaStability(
        arm_directions=dict(directions),
        consensus=consensus,
        consensus_votes=votes,
        stable=stable,
        detail="main arm agrees with majority"
        if stable
        else "beta arm direction flip or insufficient consensus",
    )


def direction_flip(directions: Mapping[str, str | None]) -> bool:
    main = directions.get(MAIN_ARM)
    others = [v for k, v in directions.items() if k != MAIN_ARM and v is not None]
    return main is not None and len(others) >= 2 and Counter(others).most_common(1)[0][0] != main


def research_arm(
    arm: BetaArm | str,
    qualified: Mapping[str, Sequence[float]],
    control: Mapping[str, Sequence[float]],
    *,
    min_events: int = MIN_OOS_EVENTS,
    min_symbols: int = MIN_OOS_SYMBOLS,
) -> ArmResearch:
    result = selection_cluster_bootstrap(
        qualified,
        control,
        seed=BOOTSTRAP_SEED,
        rounds=BOOTSTRAP_ROUNDS,
        min_valid=MIN_VALID_BOOTSTRAP_REPLICATES,
    )
    qn, cn = sum(map(len, qualified.values())), sum(map(len, control.values()))
    qsym, csym = len(qualified), len(control)
    passed = gates(qn, qsym, min_events=min_events, min_symbols=min_symbols) and gates(
        cn, csym, min_events=min_events, min_symbols=min_symbols
    )
    direction = None
    if (
        passed
        and result.valid_replicates >= MIN_VALID_BOOTSTRAP_REPLICATES
        and result.lower is not None
        and result.upper is not None
    ):
        direction = "positive" if result.lower > 0 else "negative" if result.upper < 0 else "flat"
    verdict = (
        ChipVerdict.ACCEPTED
        if direction == "positive"
        else ChipVerdict.REJECTED
        if direction == "negative"
        else ChipVerdict.UNAVAILABLE
    )
    return ArmResearch(
        arm=BetaArm(arm),
        qualified_events=qn,
        qualified_symbols=qsym,
        control_events=cn,
        control_symbols=csym,
        oos_gate_passed=passed,
        bootstrap={
            "mean": result.mean_difference,
            "lower": result.lower,
            "upper": result.upper,
            "valid_replicates": result.valid_replicates,
        },
        direction=direction,
        verdict=verdict,
    )


def detect_events(
    symbol: str,
    bars: Sequence[ChipBar],
    features: Sequence[ChipDayFeatures],
    factor_id: FactorId,
    *,
    start: date,
    end: date,
) -> tuple[tuple[ChipEvent, ...], tuple[ChipEvent, ...]]:
    qualified: list[ChipEvent] = []
    control: list[ChipEvent] = []
    for i, f in enumerate(features):
        if not (start <= f.date <= end) or f.in_cooldown:
            continue
        if factor_id == "c1_low_single_peak":
            q = (
                f.peak_count <= C1_MAX_PEAKS
                and (f.price_pct_120 or 1) <= C1_MAX_PRICE_PCT
                and f.winner_ratio >= C1_MIN_WINNER
                and f.concentration_90 >= C1_MIN_CONCENTRATION
            )
            c = (
                (f.price_pct_120 or 1) <= C1_MAX_PRICE_PCT
                and f.winner_ratio >= C1_MIN_WINNER
                and f.concentration_90 < C1_MIN_CONCENTRATION
            )
        elif factor_id == "c2_double_peak_hold":
            q = (
                f.peak_count >= C2_MIN_PEAKS
                and f.low_chip_share >= C2_MIN_LOW_PEAK_SHARE
                and i >= C2_LOOKBACK
                and c2_holds(
                    features[i - C2_LOOKBACK], f, bars[i - C2_LOOKBACK].close, bars[i].close
                )
            )
            c = (
                f.peak_count >= C2_MIN_PEAKS
                and i >= C2_LOOKBACK
                and not c2_holds(
                    features[i - C2_LOOKBACK], f, bars[i - C2_LOOKBACK].close, bars[i].close
                )
            )
        elif factor_id == "c3_peak_relocation":
            old = features[i - C3_WINDOW].main_peak_position_60 if i >= C3_WINDOW else None
            q = (
                old is not None
                and f.main_peak_position_60 is not None
                and old <= C3_LOW_MAIN_PCT
                and f.main_peak_position_60 >= C3_HIGH_MAIN_PCT
            )
            c = (
                f.main_peak_position_60 is not None
                and f.main_peak_position_60 >= C3_HIGH_MAIN_PCT
                and not q
            )
        elif factor_id == "c4_dense_breakout":
            q = (
                f.is_60d_high_breakout
                and f.concentration_90 >= C4_MIN_CONCENTRATION
                and f.winner_ratio >= C4_MIN_WINNER
            )
            c = f.is_60d_high_breakout and not q
        else:
            q = (
                f.is_60d_high_breakout
                and f.peak_count >= C5_MIN_PEAKS
                and f.concentration_90 < C5_MAX_CONCENTRATION
            )
            c = f.is_60d_high_breakout and not q
        if q:
            qualified.append(ChipEvent(factor_id, symbol, f.date, "qualified", i, f.prior_run_60))
        elif c:
            control.append(ChipEvent(factor_id, symbol, f.date, "control", i, f.prior_run_60))
    return tuple(qualified), tuple(control)


def _identity(reader: object) -> object | None:
    try:
        return reader.identity()  # type: ignore[attr-defined]
    except Exception:
        return None


def _source(reader: object) -> object | None:
    value = _identity(reader)
    return value.get("source") if isinstance(value, Mapping) else getattr(value, "source", None)


def assess_capability(
    request: ChipPeakRequest, *, readers: ChipReaderBundle | None = None
) -> CapabilityResult:
    if readers is None:
        return CapabilityResult(
            status=ChipStatus.UNAVAILABLE, problems=(UnavailabilityReason.CANONICAL_READER.value,)
        )
    problems: list[str] = []
    if readers.bars is None:
        problems.append(UnavailabilityReason.CANONICAL_READER.value)
    else:
        try:
            days = tuple(readers.bars.market_days(request.start, request.end))
        except Exception:
            days = ()
            problems.append(UnavailabilityReason.CANONICAL_READER.value)
        if not days:
            problems.append(UnavailabilityReason.CANONICAL_READER.value)
        if _identity(readers.bars) is None:
            problems.append(UnavailabilityReason.INVALID_PROVENANCE.value)
    if readers.market_facts is None:
        problems.append(UnavailabilityReason.MARKET_FACTS_INCOMPLETE.value)
    elif _identity(readers.market_facts) is None:
        problems.append(UnavailabilityReason.INVALID_PROVENANCE.value)
    if readers.presence is None:
        problems.append(UnavailabilityReason.UNIVERSE_PRESENCE.value)
    elif _identity(readers.presence) is None:
        problems.append(UnavailabilityReason.INVALID_PROVENANCE.value)
    if readers.turnover is None or _source(readers.turnover) != PIT_TURNOVER_SOURCE:
        problems.append(UnavailabilityReason.TURNOVER_PROVENANCE.value)
    return CapabilityResult(
        status=ChipStatus.OK if not problems else ChipStatus.UNAVAILABLE, problems=tuple(problems)
    )


def _unavailable(
    problems: Sequence[str], audits: Sequence[ChipSymbolAudit] = ()
) -> ChipPeakResponse:
    order = (
        UnavailabilityReason.CANONICAL_READER,
        UnavailabilityReason.MARKET_FACTS_INCOMPLETE,
        UnavailabilityReason.UNIVERSE_PRESENCE,
        UnavailabilityReason.TURNOVER_PROVENANCE,
        UnavailabilityReason.INVALID_PROVENANCE,
    )
    reason = next((r for r in order if r.value in problems), UnavailabilityReason.CANONICAL_READER)
    return ChipPeakResponse(
        status=ChipStatus.UNAVAILABLE, unavailable_reason=reason, symbol_audit=list(audits)
    )


def _load_symbol(
    readers: ChipReaderBundle, request: ChipPeakRequest, symbol: str, start: date, end: date
) -> tuple[str, str, tuple[ChipBar, ...]]:
    try:
        bars = tuple(readers.bars.load_bars(symbol, start, end))
    except Exception:
        return "no_bars", "canonical load failed", ()
    if not bars:
        return "no_bars", "", ()
    try:
        turnover = resolve_pit_turnover(readers.turnover, symbol, bars)
    except Exception:
        return "missing_pit_turnover", "turnover lookup failed", ()
    for bar, day in zip(bars, turnover, strict=False):
        if (
            day is None
            or day.available_at is None
            or day.float_shares is None
            or day.available_at > bar.date
        ):
            return "missing_pit_turnover", f"{bar.date} PIT observation unavailable", ()
    if bars[0].date >= request.start:
        return "insufficient_history", "no warmup bars", ()
    return "ok", "", bars


def _membership_status(presence: object, symbol: str, day: date) -> str:
    try:
        raw = presence.membership(symbol, day)
    except Exception:
        return "coverage_missing"
    if isinstance(raw, bool):
        return "in_pool" if raw else "not_in_pool"
    value = getattr(raw, "value", raw)
    normalized = str(value).lower()
    if normalized in {"in_pool", "member", "present", "eligible", "true", "1"}:
        return "in_pool"
    if normalized in {"not_in_pool", "absent", "delisted", "excluded", "false", "0"}:
        return "not_in_pool"
    return "coverage_missing"


def _outcomes(
    fid: FactorId,
    symbol: str,
    bars: Sequence[ChipBar],
    events: Sequence[ChipEvent],
    *,
    next_day: Mapping[date, date],
    facts: object,
    presence: object,
    cost: float,
) -> tuple[list[_Outcome], list[ChipCensor]]:
    outcomes: list[_Outcome] = []
    censored: list[ChipCensor] = []
    for event in events:
        membership = _membership_status(presence, symbol, event.signal_date)
        if membership != "in_pool":
            reason = (
                ChipCensorReason.PIT_UNIVERSE_INELIGIBLE
                if membership == "not_in_pool"
                else ChipCensorReason.UNIVERSE_COVERAGE_MISSING
            )
            censored.append(
                ChipCensor(
                    factor_id=fid,
                    symbol=symbol,
                    event_date=event.signal_date,
                    reason=reason,
                    detail=membership,
                )
            )
            continue
        idx = event.index + 1
        if idx >= len(bars):
            censored.append(
                ChipCensor(
                    factor_id=fid,
                    symbol=symbol,
                    event_date=event.signal_date,
                    reason=ChipCensorReason.ENTRY_BAR_MISSING,
                    detail="no T+1 bar",
                )
            )
            continue
        if next_day.get(event.signal_date) != bars[idx].date:
            censored.append(
                ChipCensor(
                    factor_id=fid,
                    symbol=symbol,
                    event_date=event.signal_date,
                    reason=ChipCensorReason.ENTRY_UNREACHABLE,
                    detail="T+1 market bar unavailable",
                )
            )
            continue
        entry = bars[idx]
        limit = row_limit_up(facts.row(symbol, entry.date)) if facts is not None else None
        if limit is not None and entry.raw_open >= limit - PRICE_ABS_TOL:
            censored.append(
                ChipCensor(
                    factor_id=fid,
                    symbol=symbol,
                    event_date=event.signal_date,
                    reason=ChipCensorReason.ENTRY_UNREACHABLE,
                    detail="published limit-up prevents fill",
                )
            )
            continue
        if (
            not math.isfinite(entry.raw_open)
            or entry.raw_open <= 0
            or not math.isfinite(entry.open)
            or entry.open <= 0
        ):
            censored.append(
                ChipCensor(
                    factor_id=fid,
                    symbol=symbol,
                    event_date=event.signal_date,
                    reason=ChipCensorReason.ENTRY_OPEN_INVALID,
                    detail="invalid raw or adjusted open",
                )
            )
            continue
        if abs(entry.adj_ratio - bars[event.index].adj_ratio) > 1e-9:
            censored.append(
                ChipCensor(
                    factor_id=fid,
                    symbol=symbol,
                    event_date=event.signal_date,
                    reason=ChipCensorReason.EX_DIV_COOLDOWN,
                    detail="corporate action before entry",
                )
            )
            continue
        returns: dict[int, float] = {}
        for horizon in FORWARD_HORIZONS:
            end = idx + horizon
            if end >= len(bars):
                censored.append(
                    ChipCensor(
                        factor_id=fid,
                        symbol=symbol,
                        event_date=event.signal_date,
                        reason=ChipCensorReason.HORIZON_INCOMPLETE,
                        detail=f"horizon={horizon}",
                    )
                )
                continue
            terminal = bars[end]
            if not math.isfinite(terminal.close) or terminal.close <= 0:
                censored.append(
                    ChipCensor(
                        factor_id=fid,
                        symbol=symbol,
                        event_date=event.signal_date,
                        reason=ChipCensorReason.HORIZON_INCOMPLETE,
                        detail=f"horizon={horizon}; adjusted close invalid",
                    )
                )
                continue
            returns[horizon] = terminal.close / entry.open - 1.0 - cost
        if returns:
            outcomes.append(_Outcome(event=event, entry_date=entry.date, returns=returns))
    return outcomes, censored


def _arm_data(
    fid: FactorId,
    symbol: str,
    bars: tuple[ChipBar, ...],
    features: Sequence[ChipDayFeatures],
    request: ChipPeakRequest,
    next_day: Mapping[date, date],
    facts: object,
    presence: object,
    cost: float,
) -> tuple[dict[str, list[float]], dict[str, list[float]]]:
    q, c = detect_events(symbol, bars, features, fid, start=request.start, end=request.end)
    outcomes, _ = _outcomes(
        fid, symbol, bars, q + c, next_day=next_day, facts=facts, presence=presence, cost=cost
    )
    qr: dict[str, list[float]] = defaultdict(list)
    cr: dict[str, list[float]] = defaultdict(list)
    for outcome in outcomes:
        if outcome.event.signal_date < request.oos_start:
            continue
        (qr if outcome.event.bucket == "qualified" else cr)[symbol].append(outcome.primary)
    return dict(qr), dict(cr)


def evaluate(
    request: ChipPeakRequest, *, readers: ChipReaderBundle | None = None
) -> ChipPeakResponse:
    capability = assess_capability(request, readers=readers)
    if capability.status is ChipStatus.UNAVAILABLE or readers is None:
        return _unavailable(capability.problems)
    params = ChipModelParams()
    days, bar_start, bar_end = request_windows(readers.bars, request.start, request.end)
    if not days:
        return _unavailable((UnavailabilityReason.CANONICAL_READER.value,))
    next_day = {day: days[i + 1] for i, day in enumerate(days[:-1])}
    cost = 2.0 * request.cost_bps / 10000.0
    audits: list[ChipSymbolAudit] = []
    usable: dict[str, tuple[ChipBar, ...]] = {}
    for symbol in request.symbols:
        status, detail, bars = _load_symbol(readers, request, symbol, bar_start, bar_end)
        audits.append(ChipSymbolAudit(symbol=symbol, status=status, detail=detail))
        if bars:
            usable[symbol] = bars
    if not usable:
        reason = (
            UnavailabilityReason.TURNOVER_PROVENANCE
            if any(a.status == "missing_pit_turnover" for a in audits)
            else UnavailabilityReason.CANONICAL_READER
        )
        return _unavailable((reason.value,), audits)
    features: dict[str, dict[str, tuple[ChipDayFeatures, ...]]] = {}
    for symbol, bars in usable.items():
        try:
            turnover = resolve_pit_turnover(readers.turnover, symbol, bars)
            features[symbol] = {
                arm: build_feature_series(bars, turnover, arm=arm, params=params)
                for arm in BETA_ARMS
            }
        except (MissingPitTurnoverError, ValueError, ArithmeticError):
            audits = [
                a
                if a.symbol != symbol
                else ChipSymbolAudit(
                    symbol=symbol,
                    status="missing_pit_turnover",
                    detail="feature build failed closed",
                )
                for a in audits
            ]
    usable = {s: b for s, b in usable.items() if s in features}
    if not usable:
        return _unavailable((UnavailabilityReason.TURNOVER_PROVENANCE.value,), audits)
    factors: list[ChipFactorResult] = []
    for fid in FACTOR_IDS:
        parent = 0
        censored: list[ChipCensor] = []
        denominator: list[ChipDenominatorAuditEntry] = []
        is_q: list[float] = []
        is_c: list[float] = []
        oos_q: dict[str, list[float]] = defaultdict(list)
        oos_c: dict[str, list[float]] = defaultdict(list)
        horizons = {str(h): {"qualified": 0, "control": 0} for h in FORWARD_HORIZONS}
        for symbol in sorted(usable):
            bars = usable[symbol]
            q, c = detect_events(
                symbol, bars, features[symbol][MAIN_ARM], fid, start=request.start, end=request.end
            )
            parent += len(q) + len(c)
            outcomes, dropped = _outcomes(
                fid,
                symbol,
                bars,
                q + c,
                next_day=next_day,
                facts=readers.market_facts,
                presence=readers.presence,
                cost=cost,
            )
            censored.extend(dropped)
            for outcome in outcomes:
                bucket = outcome.event.bucket
                denominator.append(
                    ChipDenominatorAuditEntry(
                        event_id=f"{fid}:{symbol}:{outcome.event.signal_date}:{bucket}",
                        factor_id=fid,
                        symbol=symbol,
                        event_date=outcome.event.signal_date,
                        code=bucket,
                    )
                )
                for h in outcome.returns:
                    horizons[str(h)][bucket] += 1
                if outcome.event.signal_date >= request.oos_start:
                    (oos_q if bucket == "qualified" else oos_c)[symbol].append(outcome.primary)
                elif bucket == "qualified":
                    is_q.append(outcome.primary)
                else:
                    is_c.append(outcome.primary)
            for drop in dropped:
                denominator.append(
                    ChipDenominatorAuditEntry(
                        event_id=f"{fid}:{symbol}:{drop.event_date or request.end}:{drop.reason.value}",
                        factor_id=fid,
                        symbol=symbol,
                        event_date=drop.event_date or request.end,
                        code=f"censored:{drop.reason.value}",
                    )
                )
        arm_research: dict[str, ArmResearch] = {}
        directions: dict[str, str | None] = {}
        for arm in BETA_ARMS:
            qr: dict[str, list[float]] = defaultdict(list)
            cr: dict[str, list[float]] = defaultdict(list)
            for symbol in sorted(usable):
                got_q, got_c = _arm_data(
                    fid,
                    symbol,
                    usable[symbol],
                    features[symbol][arm],
                    request,
                    next_day,
                    readers.market_facts,
                    readers.presence,
                    cost,
                )
                for key, vals in got_q.items():
                    qr[key].extend(vals)
                for key, vals in got_c.items():
                    cr[key].extend(vals)
            result = research_arm(arm, dict(qr), dict(cr))
            arm_research[arm] = result
            directions[arm] = result.direction
        if direction_flip(directions):
            return ChipPeakResponse(
                status=ChipStatus.UNAVAILABLE,
                unavailable_reason=UnavailabilityReason.BETA_INSTABILITY,
                symbol_audit=audits,
            )
        stability = beta_stability(directions)
        main = arm_research[MAIN_ARM]
        factors.append(
            ChipFactorResult(
                factor_id=fid,
                parent_events=parent,
                qualified_events=sum(map(len, oos_q.values())),
                control_events=sum(map(len, oos_c.values())),
                qualified_bucket="qualified",
                control_bucket="control",
                censored=censored,
                denominator_audit=denominator,
                is_diagnostic={
                    "qualified_events": len(is_q),
                    "control_events": len(is_c),
                    "median_qualified_return": median(is_q) if is_q else None,
                    "median_control_return": median(is_c) if is_c else None,
                },
                oos={
                    "arms": arm_research,
                    "primary_horizon": PRIMARY_HORIZON,
                    "horizons": horizons,
                },
                diagnostics={
                    "parent_events": parent,
                    "censored": len(censored),
                    "symbols": len(usable),
                    "cost_bps": request.cost_bps,
                    "cost_model": "round_trip",
                },
                beta_stability=stability,
                phase2_pending=fid == "c5_dispersed_exclusion",
                phase2_note="portfolio-level Sharpe/MaxDD is phase 2"
                if fid == "c5_dispersed_exclusion"
                else None,
                verdict=main.verdict if stability.stable else ChipVerdict.UNAVAILABLE,
            )
        )
    provenance = ChipProvenance(
        identities=ChipDataIdentity(
            canonical=_identity(readers.bars),
            markets=_identity(readers.market_facts),
            universe=_identity(readers.presence),
            turnover=ChipTurnoverIdentity(
                source=PIT_TURNOVER_SOURCE,
                rows=sum(len(v) for v in usable.values()),
                symbols=len(usable),
            ),
        ),
        calendar_id="canonical_sealed_market_days",
        parameters={
            "cost_bps": request.cost_bps,
            "cost_model": "round_trip",
            "oos_start": request.oos_start.isoformat(),
            "forward_horizons": FORWARD_HORIZONS,
            "primary_horizon": PRIMARY_HORIZON,
            "beta_arms": BETA_ARMS,
            "warmup_market_days": params.warmup_market_days,
            "bootstrap_seed": BOOTSTRAP_SEED,
            "bootstrap_rounds": BOOTSTRAP_ROUNDS,
        },
        params_provenance={
            "chip_model": "frozen defaults in models.py",
            "bootstrap": f"seed={BOOTSTRAP_SEED} rounds={BOOTSTRAP_ROUNDS}",
            "entry": "T+1 raw open with limit-up reachability",
        },
    )
    return ChipPeakResponse(
        status=ChipStatus.OK,
        factors=factors,
        symbol_audit=audits,
        arms_evaluated=tuple(BETA_ARMS),
        provenance=provenance,
    )


__all__ = [
    "ChipEvent",
    "assess_capability",
    "beta_stability",
    "detect_events",
    "direction_flip",
    "evaluate",
    "research_arm",
]
