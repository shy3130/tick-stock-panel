"""Daily-proxy MERA retrieval routing evaluator with strict PIT boundaries."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Sequence

import numpy as np

from .metrics import (
    apply_standardization,
    cross_sectional_rank_ic,
    digitize_labels,
    nearest_indices,
    normalized_entropy,
    pairwise_distances,
    quantile_edges,
    standardization_params,
)
from .models import (
    CENSOR_INSUFFICIENT_NEIGHBORS,
    CENSOR_LABEL_WINDOW,
    DISTANCE_METRICS,
    K_CANDIDATES,
    LABEL_CLASS_NAMES,
    MAX_K,
    MIN_PANEL_SYMBOLS,
    MIN_WARMED_SAMPLES_PER_EVAL_DATE,
    PLACEBO_KIND_RANDOM_LABEL,
    PLACEBO_KINDS,
    PLACEBO_QUANTILE,
    PLACEBO_SEED_RANDOM_LABEL,
    PLACEBO_SEED_RANDOM_NEIGHBOR,
    CensorRecord,
    ClaimId,
    ClaimVerdict,
    CoverageReport,
    FrozenStatistics,
    NeighborRecord,
    PlaceboResult,
    Provenance,
    RetrievalRoutingRequest,
    RetrievalRoutingResponse,
    RoutingEvent,
    RoutingStatus,
    RoutingVerdictStatus,
    SplitMetrics,
    SplitName,
    UnavailabilityReason,
    unavailable_response,
)
from .panel import PinnedFactorPanel
from .placebo import placebo_summary, permuted_labels, random_neighbor_indices


class NeighborLeakageError(ValueError):
    """Raised when an auditable neighbor violates strict PIT constraints."""


@dataclass(frozen=True, slots=True)
class SelectionDecision:
    k: int
    distance_metric: str
    candidates: tuple[dict[str, float | int | str], ...]


def split_date_indices(n_dates: int) -> tuple[range, range, range]:
    train_n, validation_n = int(n_dates * 0.6), int(n_dates * 0.2)
    return (
        range(0, train_n),
        range(train_n, train_n + validation_n),
        range(train_n + validation_n, n_dates),
    )


def select_routing_config(
    scores: Mapping[tuple[int, str], tuple[float, float]],
) -> SelectionDecision:
    if not scores:
        raise ValueError("empty candidate scores")
    candidates = tuple(
        {
            "k": k,
            "distance_metric": metric,
            "train_rank_ic": float(tv),
            "validation_rank_ic": float(vv),
            "selection_score": float((tv + vv) / 2),
        }
        for (k, metric), (tv, vv) in sorted(scores.items())
    )
    best = max(
        scores, key=lambda key: (sum(scores[key]) / 2, -key[0], -DISTANCE_METRICS.index(key[1]))
    )
    return SelectionDecision(best[0], best[1], candidates)


def assert_neighbor_boundaries(events: Sequence[RoutingEvent]) -> None:
    for event in events:
        for neighbor in event.neighbors:
            if neighbor.neighbor_date >= event.query_date:
                raise NeighborLeakageError("neighbor_date must be strictly earlier than query_date")
            if neighbor.label_available_date >= event.query_date:
                raise NeighborLeakageError(
                    "neighbor label must be fully realized before query_date"
                )


def _unavailable(
    request: RetrievalRoutingRequest,
    reason: UnavailabilityReason,
    detail: str,
    panel: PinnedFactorPanel,
) -> RetrievalRoutingResponse:  # noqa: F405
    return unavailable_response(request, reason, detail, dict(panel.identity))


def _query_scores(
    matrix,
    queries,
    lib_matrix,
    lib_dates,
    lib_labels,
    k,
    metric,
    label_horizon,
):
    """Neighbor scores using only labels fully realized before each query."""
    scores, details = {}, {}
    for d, s in queries:
        allowed_positions = np.flatnonzero(lib_dates + label_horizon < d)
        if allowed_positions.size < k:
            continue
        distances = pairwise_distances(
            matrix[d, s][None, :], lib_matrix[allowed_positions], metric
        )[0]
        nearest = nearest_indices(distances, k)
        chosen = allowed_positions[nearest]
        scores[(d, s)] = float(lib_labels[chosen].mean())
        details[(d, s)] = (chosen, distances[nearest])
    return scores, details


def _events_from_details(panel, queries, details, lib_idx, labels, k, metric, split):
    events = []
    for d, s in queries:
        if (d, s) not in details:
            continue
        chosen, distances = details[(d, s)]
        neighbors = [
            NeighborRecord(
                neighbor_date=panel.dates[int(lib_idx[li, 0])],
                label_available_date=panel.dates[int(lib_idx[li, 0]) + panel.label_horizon],
                neighbor_symbol=panel.symbols[int(lib_idx[li, 1])],
                distance=float(dist),
                label=int(labels[lib_idx[li, 0], lib_idx[li, 1]]),
            )
            for li, dist in zip(chosen, distances)
        ]
        nlabels = np.array([n.label for n in neighbors], dtype=int)
        counts = np.bincount(nlabels, minlength=3)
        pred = int(np.flatnonzero(counts == counts.max())[0])
        events.append(
            RoutingEvent(
                query_date=panel.dates[d],
                symbol=panel.symbols[s],
                split=split,
                k_used=k,
                distance_metric=metric,
                neighbors=neighbors,
                neighbor_label_mean=float(np.mean(nlabels)),
                predicted_class=pred,
                route_class=LABEL_CLASS_NAMES[pred],
                routing_entropy=normalized_entropy(nlabels),
                forward_return=float(panel.forward_returns[d, s]),
                label=int(labels[d, s]),
            )
        )
    return events


def _grid_metric(scores, returns, idxs):
    a, b = np.full(returns.shape, np.nan), np.full(returns.shape, np.nan)
    for d, s in idxs:
        if (d, s) in scores:
            a[d, s], b[d, s] = scores[(d, s)], returns[d, s]
    return cross_sectional_rank_ic(a, b)


def _score_class(score: float) -> int:
    return 0 if score < 2.0 / 3.0 else (1 if score < 4.0 / 3.0 else 2)


def _pool_metrics(panel, scores, date_range, cost_rate):
    """Long(class-2)/short(class-0) daily pools with turnover cost; None when no valid date."""
    prev = None
    spreads, turnovers = [], []
    for d in date_range:
        long_set = {s for (dd, s), v in scores.items() if dd == d and _score_class(v) == 2}
        short_set = {s for (dd, s), v in scores.items() if dd == d and _score_class(v) == 0}
        if not long_set or not short_set:
            continue
        spread = float(np.mean([panel.forward_returns[d, s] for s in long_set])) - float(
            np.mean([panel.forward_returns[d, s] for s in short_set])
        )
        spreads.append(spread)
        if prev is None:
            turnover = 2.0
        else:
            turnover = (1.0 - len(long_set & prev[0]) / len(long_set)) + (
                1.0 - len(short_set & prev[1]) / len(short_set)
            )
        turnovers.append(turnover)
        prev = (long_set, short_set)
    if not spreads:
        return None, None, None
    gross = float(np.mean(spreads))
    cost = cost_rate * float(np.mean(turnovers))
    return gross, cost, gross - cost


def _placebo_round(
    panel,
    matrix,
    lib_matrix,
    lib_dates,
    lib_labels,
    test_queries,
    k,
    metric,
    kind,
    rng,
):
    if kind == PLACEBO_KIND_RANDOM_LABEL:
        scores, _ = _query_scores(
            matrix,
            test_queries,
            lib_matrix,
            lib_dates,
            permuted_labels(rng, lib_labels),
            k,
            metric,
            panel.label_horizon,
        )
    else:
        scores = {}
        for d, s in test_queries:
            pool = np.flatnonzero(lib_dates + panel.label_horizon < d)
            if pool.size < k:
                continue
            chosen = pool[random_neighbor_indices(rng, pool.size, k)]
            scores[(d, s)] = float(lib_labels[chosen].mean())
    return scores


def evaluate_retrieval_routing(
    panel: PinnedFactorPanel, request: RetrievalRoutingRequest | None = None
) -> RetrievalRoutingResponse:  # noqa: F405
    request = request or RetrievalRoutingRequest()
    if request.label_horizon != panel.label_horizon:
        return _unavailable(
            request,
            UnavailabilityReason.LABEL_HORIZON_MISMATCH,
            "request and panel horizons differ",
            panel,
        )
    names = tuple(request.feature_names or panel.feature_names)
    if not names or any(name not in panel.feature_names for name in names):
        raise ValueError("unknown feature name")
    tr, va, te = split_date_indices(len(panel.dates))
    if len(panel.symbols) < MIN_PANEL_SYMBOLS or not len(tr) or not len(va) or not len(te):
        return _unavailable(
            request, UnavailabilityReason.PANEL_COVERAGE, "panel coverage gate failed", panel
        )
    raw = panel.feature_matrix(names)
    train_rows = np.array(
        [
            (d, s)
            for d in tr
            for s in range(len(panel.symbols))
            if np.isfinite(panel.forward_returns[d, s]) and np.isfinite(raw[d, s]).all()
        ]
    )
    if len(train_rows) < MAX_K:
        return _unavailable(
            request,
            UnavailabilityReason.PANEL_COVERAGE,
            "insufficient train retrieval library",
            panel,
        )
    means, stds, deg = standardization_params(raw[train_rows[:, 0], train_rows[:, 1]])
    kept = [i for i, bad in enumerate(deg) if not bad]
    if not kept:
        return _unavailable(
            request,
            UnavailabilityReason.PANEL_COVERAGE,
            "all requested features degenerate in train",
            panel,
        )
    names = tuple(names[i] for i in kept)
    raw = raw[:, :, kept]
    train_rows = np.array(
        [
            (d, s)
            for d in tr
            for s in range(len(panel.symbols))
            if np.isfinite(panel.forward_returns[d, s]) and np.isfinite(raw[d, s]).all()
        ]
    )
    means, stds, _ = standardization_params(raw[train_rows[:, 0], train_rows[:, 1]])
    matrix = apply_standardization(raw.reshape(-1, len(names)), means, stds).reshape(raw.shape)
    edges = quantile_edges(panel.forward_returns[train_rows[:, 0], train_rows[:, 1]])
    labels = digitize_labels(panel.forward_returns, edges)
    eligible = np.isfinite(panel.forward_returns) & np.isfinite(raw).all(axis=2)
    eval_dates = [d for d in list(va) + list(te) if int(eligible[d].sum()) > 0]
    below = [
        panel.dates[d]
        for d in eval_dates
        if int(eligible[d].sum()) < MIN_WARMED_SAMPLES_PER_EVAL_DATE
    ]
    if below:
        return _unavailable(
            request,
            UnavailabilityReason.PANEL_COVERAGE,
            "evaluation-date warmup gate failed",
            panel,
        )
    lib_idx = train_rows.astype(int)
    lib_matrix = matrix[lib_idx[:, 0], lib_idx[:, 1]]
    lib_dates, lib_labels = lib_idx[:, 0], labels[lib_idx[:, 0], lib_idx[:, 1]]
    query_sets = {
        SplitName.TRAIN: [(d, s) for d in tr for s in range(len(panel.symbols)) if eligible[d, s]],
        SplitName.VALIDATION: [
            (d, s) for d in va for s in range(len(panel.symbols)) if eligible[d, s]
        ],
        SplitName.TEST: [(d, s) for d in te for s in range(len(panel.symbols)) if eligible[d, s]],
    }
    config_scores, score_pairs = {}, {}
    for k in K_CANDIDATES:
        for metric in DISTANCE_METRICS:
            train_scores, _ = _query_scores(
                matrix,
                query_sets[SplitName.TRAIN],
                lib_matrix,
                lib_dates,
                lib_labels,
                k,
                metric,
                panel.label_horizon,
            )
            val_scores, _ = _query_scores(
                matrix,
                query_sets[SplitName.VALIDATION],
                lib_matrix,
                lib_dates,
                lib_labels,
                k,
                metric,
                panel.label_horizon,
            )
            config_scores[(k, metric)] = {
                SplitName.TRAIN: train_scores,
                SplitName.VALIDATION: val_scores,
            }
            score_pairs[(k, metric)] = (
                _grid_metric(train_scores, panel.forward_returns, query_sets[SplitName.TRAIN])[0],
                _grid_metric(val_scores, panel.forward_returns, query_sets[SplitName.VALIDATION])[
                    0
                ],
            )
    selected = select_routing_config(score_pairs)
    test_scores, test_details = _query_scores(
        matrix,
        query_sets[SplitName.TEST],
        lib_matrix,
        lib_dates,
        lib_labels,
        selected.k,
        selected.distance_metric,
        panel.label_horizon,
    )
    config_scores[(selected.k, selected.distance_metric)][SplitName.TEST] = test_scores
    final_scores = config_scores[(selected.k, selected.distance_metric)]
    val_event_details = _query_scores(
        matrix,
        query_sets[SplitName.VALIDATION],
        lib_matrix,
        lib_dates,
        lib_labels,
        selected.k,
        selected.distance_metric,
        panel.label_horizon,
    )[1]
    events = _events_from_details(
        panel,
        query_sets[SplitName.VALIDATION],
        val_event_details,
        lib_idx,
        labels,
        selected.k,
        selected.distance_metric,
        SplitName.VALIDATION,
    )
    events += _events_from_details(
        panel,
        query_sets[SplitName.TEST],
        test_details,
        lib_idx,
        labels,
        selected.k,
        selected.distance_metric,
        SplitName.TEST,
    )
    assert_neighbor_boundaries(events)
    selection_dates = list(range(va.stop))
    best_name, best_ic = names[0], -np.inf
    for feature_index, name in enumerate(names):
        ic, _ = cross_sectional_rank_ic(
            raw[selection_dates, :, feature_index],
            panel.forward_returns[selection_dates],
        )
        if ic > best_ic:
            best_name, best_ic = name, ic
    baseline_values = raw[:, :, names.index(best_name)]
    base_edges = quantile_edges(baseline_values[train_rows[:, 0], train_rows[:, 1]])
    splits, split_nets = [], {}
    for split, date_range in (
        (SplitName.TRAIN, tr),
        (SplitName.VALIDATION, va),
        (SplitName.TEST, te),
    ):
        queries = query_sets[split]
        ic, n_ic = _grid_metric(final_scores[split], panel.forward_returns, queries)
        baseline_grid = np.where(np.isfinite(baseline_values), baseline_values, np.nan)
        bic, _ = cross_sectional_rank_ic(
            baseline_grid[list(date_range)], panel.forward_returns[list(date_range)]
        )
        gross, cost, net = _pool_metrics(
            panel, final_scores[split], date_range, request.cost_bps / 1e4
        )
        base_scores = {
            (d, s): float(np.searchsorted(base_edges, baseline_values[d, s], side="right"))
            for d, s in queries
        }
        _, _, base_net = _pool_metrics(panel, base_scores, date_range, request.cost_bps / 1e4)
        inc = None if (net is None or base_net is None) else net - base_net
        split_nets[split] = (net, base_net, inc)
        splits.append(
            SplitMetrics(
                split=split,
                dates=len(date_range),
                samples=int(sum(eligible[d].sum() for d in date_range)),
                queries=len(queries),
                censored_queries=len(queries) - len(final_scores[split]),
                rank_ic_dates=n_ic,
                routing_rank_ic=ic,
                baseline_feature=best_name,
                baseline_rank_ic=bic,
                rank_ic_increment=ic - bic,
                long_short_gross=gross,
                long_short_cost=cost,
                long_short_net=net,
                cost_adjusted_increment=inc,
            )
        )
    label_tail = [
        (d, s)
        for d in range(len(panel.dates))
        for s in range(len(panel.symbols))
        if np.isfinite(raw[d, s]).all() and not np.isfinite(panel.forward_returns[d, s])
    ]
    censored = [
        CensorRecord(
            code=CENSOR_LABEL_WINDOW,
            detail="forward return unknown within panel tail",
            count=len(label_tail),
            first_date=panel.dates[label_tail[0][0]] if label_tail else None,
            last_date=panel.dates[label_tail[-1][0]] if label_tail else None,
        ),
        CensorRecord(
            code=CENSOR_INSUFFICIENT_NEIGHBORS,
            detail="train queries with fewer than k earlier train rows",
            count=len(query_sets[SplitName.TRAIN]) - len(final_scores[SplitName.TRAIN]),
        ),
    ]
    coverage = CoverageReport(
        symbols=len(panel.symbols),
        dates=len(panel.dates),
        warmed_samples=int(np.isfinite(raw).all(axis=2).sum()),
        eligible_samples=int(eligible.sum()),
        train_dates=len(tr),
        validation_dates=len(va),
        test_dates=len(te),
        train_samples=sum(eligible[d].sum() for d in tr),
        validation_samples=sum(eligible[d].sum() for d in va),
        test_samples=sum(eligible[d].sum() for d in te),
        min_eligible_per_eval_date=min(int(eligible[d].sum()) for d in eval_dates),
        eval_dates_below_gate=[],
        degenerate_features=[panel.feature_names[i] for i, bad in enumerate(deg) if bad],
        train_label_counts={
            LABEL_CLASS_NAMES[i]: int((labels[train_rows[:, 0], train_rows[:, 1]] == i).sum())
            for i in range(3)
        },
    )
    frozen = FrozenStatistics(
        feature_names=list(names),
        feature_means={n: float(means[i]) for i, n in enumerate(names)},
        feature_stds={n: float(stds[i]) for i, n in enumerate(names)},
        label_quantile_edges=[float(x) for x in edges],
        train_start=panel.dates[tr.start],
        train_end=panel.dates[tr.stop - 1],
        validation_start=panel.dates[va.start],
        validation_end=panel.dates[va.stop - 1],
        test_start=panel.dates[te.start],
        test_end=panel.dates[te.stop - 1],
    )
    prov = Provenance(
        frozen=frozen,
        panel=dict(panel.identity),
        selection={
            "k": selected.k,
            "distance_metric": selected.distance_metric,
            "candidates": list(selected.candidates),
        },
        notes=(
            "Daily factor-zoo proxy; not minute-level MERA.",
            "Neighbor library, standardization, labels, and K/distance selection never touch test.",
        ),
    )
    baseline_net = split_nets[SplitName.TEST][1]
    placebos, verdicts = [], []
    claims = (
        (ClaimId.RANK_IC_INCREMENT, splits[-1].rank_ic_increment),
        (ClaimId.COST_ADJUSTED_INCREMENT, split_nets[SplitName.TEST][2]),
    )
    for kind in PLACEBO_KINDS:
        ics, nets = [], []
        for r in range(request.placebo_rounds):
            rng = np.random.default_rng(
                (
                    PLACEBO_SEED_RANDOM_LABEL
                    if kind == PLACEBO_KIND_RANDOM_LABEL
                    else PLACEBO_SEED_RANDOM_NEIGHBOR
                )
                + r
            )
            p_scores = _placebo_round(
                panel,
                matrix,
                lib_matrix,
                lib_dates,
                lib_labels,
                query_sets[SplitName.TEST],
                selected.k,
                selected.distance_metric,
                kind,
                rng,
            )
            ics.append(
                _grid_metric(p_scores, panel.forward_returns, query_sets[SplitName.TEST])[0]
                - splits[-1].baseline_rank_ic
            )
            _, _, p_net = _pool_metrics(panel, p_scores, te, request.cost_bps / 1e4)
            if p_net is not None and baseline_net is not None:
                nets.append(p_net - baseline_net)
        for claim, real in claims:
            values = ics if claim is ClaimId.RANK_IC_INCREMENT else np.asarray(nets)
            if real is None:
                continue
            blocked, mean, q95 = placebo_summary(float(real), values, PLACEBO_QUANTILE)
            placebos.append(
                PlaceboResult(
                    kind=kind,
                    claim=claim,
                    rounds=request.placebo_rounds,
                    real_increment=float(real),
                    placebo_mean=mean,
                    placebo_q95=q95,
                    blocked=blocked,
                )
            )
    for claim, real in claims:
        rows = [p for p in placebos if p.claim is claim]
        if real is None:
            verdicts.append(
                ClaimVerdict(
                    claim=claim,
                    verdict=RoutingVerdictStatus.UNAVAILABLE,
                    evidence={},
                    detail="insufficient_pool_samples",
                )
            )
            continue
        blocked = [p.kind for p in rows if p.blocked]
        if blocked:
            verdicts.append(
                ClaimVerdict(
                    claim=claim,
                    verdict=RoutingVerdictStatus.UNAVAILABLE,
                    evidence={
                        p.kind: {"real": p.real_increment, "placebo_q95": p.placebo_q95}
                        for p in rows
                    },
                    detail="placebo_gain_not_distinguishable:" + ",".join(sorted(blocked)),
                )
            )
        else:
            verdicts.append(
                ClaimVerdict(
                    claim=claim,
                    verdict=RoutingVerdictStatus.ACCEPTED
                    if real > 0
                    else RoutingVerdictStatus.REJECTED,
                    evidence={"test_increment": float(real), "baseline": best_name},
                )
            )
    return RetrievalRoutingResponse(
        status=RoutingStatus.OK,
        request=request,
        identity=dict(panel.identity),
        coverage=coverage,
        censored=censored,
        events=events,
        verdicts=verdicts,
        splits=splits,
        placebos=placebos,
        provenance=prov,
    )


__all__ = [
    "NeighborLeakageError",
    "SelectionDecision",
    "split_date_indices",
    "select_routing_config",
    "assert_neighbor_boundaries",
    "evaluate_retrieval_routing",
]
