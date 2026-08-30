"""Fail-closed evaluation entry points for D1-D4 doji research."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping, Sequence
from datetime import date

from app.services.hold_firm_patterns.adapters import (
    PinnedCanonicalDailyReader,
    PinnedPresenceUniverseReader,
    ProductionReaderScope,
    ProductionReaderScopeUnavailable,
    pinned_market_facts_source,
    presence_universe_identity,
    production_reader_scope,
    request_windows,
)
from app.services.hold_firm_patterns.models import (
    BOOTSTRAP_ROUNDS,
    BOOTSTRAP_SEED,
    MIN_OOS_EVENTS,
    MIN_OOS_SYMBOLS,
    MIN_VALID_BOOTSTRAP_REPLICATES,
    PRICE_ABS_TOL,
    CapabilityResult,
    CensorReason,
    DataIdentity,
    DenominatorAuditCode,
    HoldFirmStatus,
    PitUniverseStatus,
    SelectionBucket,
    UnavailabilityReason,
)
from app.services.universe_presence_history import PresenceHistoryError

from .confirmation import ConfirmationDetector
from .gravestone import GravestoneDetector
from .models import (
    DEFINITION_DOCUMENT,
    DOJI_FACTOR_IDS,
    FORWARD_CHECKPOINT_DAYS,
    HORIZON_DAYS,
    DojiArm,
    DojiCensor,
    DojiDenominatorAuditEntry,
    DojiDetection,
    DojiEvent,
    DojiExecutionSegment,
    DojiFactorId,
    DojiFactorResult,
    DojiPatternsRequest,
    DojiProvenance,
    DojiResponse,
    DojiStatus,
    DojiVerdict,
)
from .position_interaction import DojiPositionDetector
from .statistics import (
    gates,
    interaction_cluster_bootstrap,
    paired_cluster_bootstrap,
    selection_cluster_bootstrap,
)
from .t_bar import TBarDetector

_DETECTORS = (DojiPositionDetector, GravestoneDetector, TBarDetector, ConfirmationDetector)


def _bootstrap_ready(result: object) -> bool:
    return (
        getattr(result, "valid_replicates", 0) >= MIN_VALID_BOOTSTRAP_REPLICATES
        and getattr(result, "lower", None) is not None
        and getattr(result, "upper", None) is not None
    )


def _interaction_gate(
    groups: Sequence[Mapping[str, Sequence[float]]],
    result: object,
) -> bool:
    return all(
        gates(sum(len(values) for values in group.values()), len(group)) for group in groups
    ) and _bootstrap_ready(result)


def _unavailable(reason: UnavailabilityReason) -> DojiResponse:
    return DojiResponse(status=DojiStatus.UNAVAILABLE, unavailable_reason=reason)


def _raw_identity(reader: object) -> object:
    from app.services.hold_firm_patterns.models import MarketFactsIdentity

    return MarketFactsIdentity(
        generation=str(reader.generation()),
        manifest_sha256=str(reader.manifest_sha256()),
    )


def assess_doji_capability(
    reader: object, market_facts: object, universe_reader: object
) -> CapabilityResult:
    try:
        canonical = PinnedCanonicalDailyReader(reader)
        return CapabilityResult(
            status=HoldFirmStatus.OK,
            identities=DataIdentity(
                canonical=canonical.identity(),
                markets=_raw_identity(market_facts),
                universe=presence_universe_identity(universe_reader.source_manifest()),
            ),
        )
    except Exception as exc:
        return CapabilityResult(status=HoldFirmStatus.UNAVAILABLE, problems=(str(exc),))


def _event_id(d: DojiDetection) -> str:
    return f"{d.factor_id}:{d.symbol}:{d.anchor_date.isoformat()}"


def _membership(d: DojiDetection) -> date:
    return d.landmark.landmark_date if d.landmark else d.anchor_date


def _materialize(factor_id: DojiFactorId, detections: Sequence[DojiDetection], universe: object):
    q = []
    n = []
    pit = []
    censored = []
    censors = []
    for d in detections:
        eid = _event_id(d)
        status = universe.membership(d.symbol, _membership(d))
        if status is PitUniverseStatus.NOT_IN_POOL:
            pit.append(
                DojiEvent(
                    factor_id,
                    eid,
                    d.symbol,
                    d.anchor_date,
                    None,
                    status,
                    audit_code=DenominatorAuditCode.PIT_UNIVERSE_INELIGIBLE,
                )
            )
            continue
        if d.censor is not None:
            c = DojiCensor(
                factor_id=factor_id,
                symbol=d.symbol,
                event_date=d.anchor_date,
                reason=d.censor,
                detail="detector",
            )
            censors.append(c)
            pit_event = DojiEvent(
                factor_id, eid, d.symbol, d.anchor_date, None, status, censor=d.censor
            )
            censored.append(pit_event)
            continue
        if d.evidence is None or d.landmark is None:
            raise ValueError("facts-complete detection incomplete")
        bucket = SelectionBucket.QUALIFIED if d.evidence.qualified else SelectionBucket.NOT_SELECTED
        event = DojiEvent(factor_id, eid, d.symbol, d.anchor_date, bucket, status)
        (q if bucket is SelectionBucket.QUALIFIED else n).append((event, d))
    return q, n, pit, censored, censors


def _simulate(
    event: DojiEvent,
    landmark: date,
    arm: DojiArm,
    bars: Mapping[date, object],
    facts: object,
    calendar: Sequence[date],
    cost_bps: float,
):
    try:
        i = calendar.index(landmark)
    except ValueError:
        return None, DojiCensor(
            factor_id=event.factor_id,
            symbol=event.symbol,
            event_date=event.anchor_date,
            reason=CensorReason.SELECTION_WINDOW_INCOMPLETE,
            detail="landmark missing",
        )
    start = i + 1
    end = start + HORIZON_DAYS - 1
    if end >= len(calendar):
        return None, DojiCensor(
            factor_id=event.factor_id,
            symbol=event.symbol,
            event_date=event.anchor_date,
            reason=CensorReason.HORIZON_INCOMPLETE,
            detail="horizon incomplete",
        )
    days = calendar[start : end + 1]
    path = [bars.get(d) for d in days]
    if any(x is None for x in path):
        return None, DojiCensor(
            factor_id=event.factor_id,
            symbol=event.symbol,
            event_date=event.anchor_date,
            reason=CensorReason.HORIZON_INCOMPLETE,
            detail="bar missing",
        )
    first = path[0]
    fact = facts.row(event.symbol, days[0])
    if fact is None:
        raise ValueError("facts incomplete")
    if first.quote_open_raw <= 0 or first.research_open_adj <= 0:
        return None, DojiCensor(
            factor_id=event.factor_id,
            symbol=event.symbol,
            event_date=event.anchor_date,
            reason=CensorReason.ENTRY_OPEN_INVALID,
            detail="open invalid",
        )
    if all(
        abs(v - fact.published_limit_up) <= PRICE_ABS_TOL
        for v in (
            first.quote_open_raw,
            first.quote_high_raw,
            first.quote_low_raw,
            first.quote_close_raw,
        )
    ):
        return None, DojiCensor(
            factor_id=event.factor_id,
            symbol=event.symbol,
            event_date=event.anchor_date,
            reason=CensorReason.ENTRY_UNREACHABLE,
            detail="one-price limit-up",
        )
    fee = cost_bps / 10000
    scale = (1 - fee) / first.research_open_adj
    values = [scale * x.research_close_adj for x in path]
    terminal = values[-1]
    return DojiExecutionSegment(
        event_id=event.event_id,
        factor_id=event.factor_id,
        symbol=event.symbol,
        arm=arm,
        entry_date=days[0],
        entry_quote_raw=first.quote_open_raw,
        entry_cost_bps=cost_bps,
        holding_days=HORIZON_DAYS,
        checkpoint_returns={c: values[c - 1] - 1 for c in FORWARD_CHECKPOINT_DAYS},
        terminal_return=terminal - 1,
        liquidation_cost_adjusted_terminal_return=terminal * (1 - fee) - 1,
        mae=max(0, 1 - min(values)),
        mfe=max(0, max(values) - 1),
    ), None


def evaluate_doji_patterns(
    request: DojiPatternsRequest, reader: object, market_facts: object, universe_reader: object
) -> DojiResponse:
    try:
        canonical = PinnedCanonicalDailyReader(reader)
        full, event_days, bar_start, bar_end = request_windows(reader, request.start, request.end)
        if not full:
            return _unavailable(UnavailabilityReason.CANONICAL_READER)
    except Exception:
        return _unavailable(UnavailabilityReason.CANONICAL_READER)
    try:
        facts = pinned_market_facts_source(market_facts, request.symbols, full)
        bars = {
            s: {b.date: b for b in canonical.load_bars(s, bar_start, bar_end)}
            for s in request.symbols
        }
        if facts.incomplete_rows or any(facts.row(s, d) is None for s in bars for d in bars[s]):
            return _unavailable(UnavailabilityReason.MARKET_FACTS_INCOMPLETE)
    except Exception:
        return _unavailable(UnavailabilityReason.MARKET_FACTS_INCOMPLETE)
    detections = tuple(
        tuple(
            d
            for s in request.symbols
            for d in cls(request.theta_body_ratio).detect(s, tuple(bars[s].values()), facts, full)
            if request.start <= d.anchor_date <= request.end
        )
        for cls in _DETECTORS
    )
    try:
        universe = PinnedPresenceUniverseReader(
            universe_reader, tuple(sorted({_membership(d) for group in detections for d in group}))
        )
    except (OSError, PresenceHistoryError, RuntimeError, TypeError, ValueError):
        return _unavailable(UnavailabilityReason.UNIVERSE_PRESENCE)
    try:
        provenance = DojiProvenance(
            identities=DataIdentity(
                canonical=canonical.identity(),
                markets=facts.identity(),
                universe=universe.identity(),
            ),
            calendar_id=canonical.identity().calendar_id,
            parameters={
                "symbols": request.symbols,
                "start": request.start.isoformat(),
                "oos_start": request.oos_start.isoformat(),
                "end": request.end.isoformat(),
                "theta_body_ratio": request.theta_body_ratio,
                "cost_bps": request.cost_bps,
                "horizon_days": HORIZON_DAYS,
                "checkpoint_days": FORWARD_CHECKPOINT_DAYS,
                "bootstrap_seed": BOOTSTRAP_SEED,
                "bootstrap_rounds": BOOTSTRAP_ROUNDS,
                "min_oos_events": MIN_OOS_EVENTS,
                "min_oos_symbols": MIN_OOS_SYMBOLS,
            },
            params_provenance={
                "definition": DEFINITION_DOCUMENT,
                "numeric_thresholds": "research-defined; not attributed to source video",
            },
            code_version="doji-patterns-v1",
        )
    except Exception:
        return _unavailable(UnavailabilityReason.INVALID_PROVENANCE)
    factors = []
    for fid, ds in zip(DOJI_FACTOR_IDS, detections, strict=False):
        try:
            q, n, pit, sc, censors = _materialize(fid, ds, universe)
        except (PresenceHistoryError, ValueError):
            return _unavailable(UnavailabilityReason.UNIVERSE_PRESENCE)
        segments = []
        complete_q = []
        complete_n = []
        blocked = {}
        candidates = sorted(q + n, key=lambda x: (x[0].symbol, x[0].anchor_date, x[0].event_id))
        for event, detection in candidates:
            landmark = detection.landmark.landmark_date if detection.landmark else event.anchor_date
            try:
                li = full.index(landmark)
            except ValueError:
                continue
            if li <= blocked.get(event.symbol, -1):
                censors.append(
                    DojiCensor(
                        factor_id=fid,
                        symbol=event.symbol,
                        event_date=event.anchor_date,
                        reason=CensorReason.EVENT_OVERLAP,
                        detail="same-symbol horizon overlap",
                    )
                )
                continue
            bare, bc = _simulate(
                event, landmark, DojiArm.BARE, bars[event.symbol], facts, full, request.cost_bps
            )
            if bc is not None:
                censors.append(bc)
                continue
            if bare is None:
                continue
            if event.bucket is SelectionBucket.QUALIFIED and fid == "next_day_confirmation":
                confirmation = (
                    detection.evidence.values.get("confirmation_date")
                    if detection.evidence
                    else None
                )
                if not isinstance(confirmation, date):
                    continue
                confirmed, cc = _simulate(
                    event,
                    confirmation,
                    DojiArm.CONFIRMED,
                    bars[event.symbol],
                    facts,
                    full,
                    request.cost_bps,
                )
                if cc is not None:
                    censors.append(cc)
                    continue
                if confirmed is None:
                    continue
                segments.extend((bare, confirmed))
                complete_q.append((event, detection, bare, confirmed))
                blocked[event.symbol] = full.index(confirmation) + HORIZON_DAYS - 1
            else:
                segments.append(bare)
                (complete_q if event.bucket is SelectionBucket.QUALIFIED else complete_n).append(
                    (event, detection, bare)
                )
                blocked[event.symbol] = li + HORIZON_DAYS - 1

        def retmap(items):
            out = defaultdict(list)
            for event, _d, segment in items:
                out[event.symbol].append(segment.liquidation_cost_adjusted_terminal_return)
            return out

        iq = [x for x in complete_q if x[0].anchor_date < request.oos_start]
        inn = [x for x in complete_n if x[0].anchor_date < request.oos_start]
        oq = [x for x in complete_q if x[0].anchor_date >= request.oos_start]
        onn = [x for x in complete_n if x[0].anchor_date >= request.oos_start]
        if fid == "next_day_confirmation":
            pairs = {
                x[0].event_id: (
                    x[0].symbol,
                    x[3].liquidation_cost_adjusted_terminal_return
                    - x[2].liquidation_cost_adjusted_terminal_return,
                    x[3].mae - x[2].mae,
                )
                for x in oq
            }
            rb, _mb = paired_cluster_bootstrap(pairs)
            stats = rb
            gate = gates(len(pairs), len({v[0] for v in pairs.values()})) and _bootstrap_ready(rb)
            verdict = (
                DojiVerdict.UNAVAILABLE
                if not gate
                else (
                    DojiVerdict.ACCEPTED
                    if rb.lower is not None and rb.lower > 0
                    else DojiVerdict.REJECTED
                )
            )
        elif fid == "doji_position_interaction":
            hq = defaultdict(list)
            hn = defaultdict(list)
            lq = defaultdict(list)
            ln = defaultdict(list)
            for event, d, s in oq:
                target = (
                    hq
                    if d.evidence.values.get("stratum") == "high"
                    else lq
                    if d.evidence.values.get("stratum") == "low"
                    else None
                )
                if target is not None:
                    target[event.symbol].append(s.liquidation_cost_adjusted_terminal_return)
            for event, d, s in onn:
                target = (
                    hn
                    if d.evidence.values.get("stratum") == "high"
                    else ln
                    if d.evidence.values.get("stratum") == "low"
                    else None
                )
                if target is not None:
                    target[event.symbol].append(s.liquidation_cost_adjusted_terminal_return)
            stats = interaction_cluster_bootstrap(hq, hn, lq, ln)
            gate = _interaction_gate((hq, hn, lq, ln), stats)
            verdict = (
                DojiVerdict.UNAVAILABLE
                if not gate
                else (
                    DojiVerdict.ACCEPTED
                    if stats.upper is not None and stats.upper < 0
                    else DojiVerdict.REJECTED
                )
            )
        else:
            qmap = retmap(oq)
            nmap = retmap(onn)
            stats = selection_cluster_bootstrap(qmap, nmap)
            gate = (
                gates(sum(len(v) for v in qmap.values()), len(qmap))
                and gates(sum(len(v) for v in nmap.values()), len(nmap))
                and _bootstrap_ready(stats)
            )
            verdict = (
                DojiVerdict.UNAVAILABLE
                if not gate
                else (
                    DojiVerdict.ACCEPTED
                    if (fid == "gravestone_high" and stats.upper is not None and stats.upper < 0)
                    or (fid == "t_bar_low" and stats.lower is not None and stats.lower > 0)
                    else DojiVerdict.REJECTED
                )
            )
        qn = len(q)
        nn = len(n)
        parent = qn + nn + len(pit) + len(sc)
        factors.append(
            DojiFactorResult(
                factor_id=fid,
                parent_events=parent,
                qualified_events=qn,
                not_selected_events=nn,
                segments=segments,
                censored=censors,
                denominator_audit=[
                    DojiDenominatorAuditEntry(
                        event_id=e.event_id,
                        factor_id=fid,
                        symbol=e.symbol,
                        event_date=e.anchor_date,
                        code=DenominatorAuditCode.PIT_UNIVERSE_INELIGIBLE,
                    )
                    for e in pit
                ],
                is_diagnostic={"complete_events": len(iq) + len(inn)},
                oos={
                    "valid_replicates": getattr(stats, "valid_replicates", 0),
                    "mean_difference": getattr(stats, "mean_difference", None),
                },
                diagnostics={
                    "censor_reasons": {
                        c.reason.value: sum(x.reason is c.reason for x in censors) for c in censors
                    },
                    "entry_price_degradation": [
                        x[3].entry_quote_raw - x[2].entry_quote_raw for x in oq if len(x) > 3
                    ],
                },
                verdict=verdict,
            )
        )
    return DojiResponse(
        status=DojiStatus.OK,
        factors=factors,
        provenance=provenance,
        coverage={
            "requested_symbols": len(request.symbols),
            "event_days": len(event_days),
            "calendar_days": len(full),
        },
    )


__all__ = [
    "ProductionReaderScope",
    "ProductionReaderScopeUnavailable",
    "assess_doji_capability",
    "evaluate_doji_patterns",
    "production_reader_scope",
]
