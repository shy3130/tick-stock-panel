from datetime import date, timedelta

import numpy as np
import pytest
import polars as pl

from app.services.retrieval_routing_research import (
    NeighborLeakageError,
    PinnedFactorPanel,
    RetrievalRoutingRequest,
    RoutingEvent,
    SplitName,
    assert_neighbor_boundaries,
    build_pinned_factor_panel,
    evaluate_retrieval_routing,
    select_routing_config,
)
from app.services.retrieval_routing_research.routing import _query_scores


def _panel(symbols=30, dates=20, horizon=1):
    days = tuple(date(2020, 1, 1) + timedelta(days=i) for i in range(dates))
    syms = tuple(f"{i:06d}.SH" for i in range(symbols))
    x = np.arange(dates * symbols, dtype=float).reshape(dates, symbols)
    returns = np.roll(x, -horizon, axis=0) / np.maximum(x, 1) - 1
    returns[-horizon:] = np.nan
    return PinnedFactorPanel(
        ("f1",), days, syms, {"f1": x}, returns, horizon, 0, {"source": "test"}
    )


def test_selection_is_train_validation_only():
    decision = select_routing_config({(5, "euclidean"): (0.1, 0.2), (10, "cosine"): (0.0, 0.0)})
    assert (decision.k, decision.distance_metric) == (5, "euclidean")


def test_neighbor_boundary_guard_fails_closed():
    event = RoutingEvent(
        query_date=date(2020, 1, 2),
        symbol="000001.SH",
        split=SplitName.TEST,
        k_used=1,
        distance_metric="euclidean",
        neighbors=[
            {
                "neighbor_date": date(2020, 1, 1),
                "label_available_date": date(2020, 1, 2),
                "neighbor_symbol": "000002.SH",
                "distance": 0.1,
                "label": 1,
            }
        ],
        neighbor_label_mean=1,
        predicted_class=1,
        route_class="mid",
        routing_entropy=0,
        forward_return=0,
        label=1,
    )
    with pytest.raises(NeighborLeakageError):
        assert_neighbor_boundaries([event])


def test_retrieval_uses_only_fully_realized_forward_labels():
    matrix = np.arange(8, dtype=float).reshape(8, 1, 1)
    lib_dates = np.arange(6)
    lib_matrix = matrix[:6, 0]
    lib_labels = np.arange(6)
    scores, details = _query_scores(
        matrix,
        [(6, 0)],
        lib_matrix,
        lib_dates,
        lib_labels,
        3,
        "euclidean",
        3,
    )
    assert (6, 0) in scores
    chosen, _ = details[(6, 0)]
    assert lib_dates[chosen].max() == 2

    unavailable, _ = _query_scores(
        matrix,
        [(6, 0)],
        lib_matrix,
        lib_dates,
        lib_labels,
        4,
        "euclidean",
        3,
    )
    assert unavailable == {}


def test_panel_gate_is_explicitly_unavailable():
    response = evaluate_retrieval_routing(
        _panel(symbols=5), RetrievalRoutingRequest(placebo_rounds=20)
    )
    assert response.status.value == "unavailable"
    assert response.unavailable_reason.value == "unavailable_panel_coverage"
    assert response.promoted is False


def test_response_contract_is_serializable_on_unavailable():
    response = evaluate_retrieval_routing(
        _panel(symbols=5), RetrievalRoutingRequest(placebo_rounds=20)
    )
    restored = type(response).model_validate_json(response.model_dump_json())
    assert restored.model_dump(mode="json") == response.model_dump(mode="json")
    assert {
        "schema",
        "status",
        "definition_version",
        "request",
        "identity",
        "coverage",
        "censored",
        "events",
        "verdicts",
        "promoted",
    }.issubset(response.model_dump())


def test_placebo_primitives_are_fixed_seed_and_serializable():
    from app.services.retrieval_routing_research.placebo import (
        permuted_labels,
        random_neighbor_indices,
    )

    a = permuted_labels(np.random.default_rng(46052), np.arange(10))
    b = permuted_labels(np.random.default_rng(46052), np.arange(10))
    assert np.array_equal(a, b)
    assert np.array_equal(
        random_neighbor_indices(np.random.default_rng(46051), 20, 5),
        random_neighbor_indices(np.random.default_rng(46051), 20, 5),
    )


def test_cost_pool_helper_returns_cost_adjusted_increment():
    from app.services.retrieval_routing_research.routing import _pool_metrics

    panel = _panel()
    scores = {(d, s): float(s % 3) for d in range(12, 16) for s in range(30)}
    gross0, cost0, net0 = _pool_metrics(panel, scores, range(12, 16), 0.0)
    gross1, cost1, net1 = _pool_metrics(panel, scores, range(12, 16), 0.01)
    assert gross0 is not None and net0 == gross0
    assert cost1 is not None and net1 < gross1


def test_cost_pool_turnover_counts_liquidation_when_pool_shrinks():
    from app.services.retrieval_routing_research.routing import _pool_metrics

    panel = _panel()
    scores = {
        (12, 0): 0.0,
        (12, 3): 0.0,
        (12, 2): 2.0,
        (12, 5): 2.0,
        (13, 0): 0.0,
        (13, 3): 0.0,
        (13, 2): 2.0,
    }
    _, cost, _ = _pool_metrics(panel, scores, range(12, 14), 0.01)
    assert cost == pytest.approx(0.015)


def test_evaluator_placebo_output_is_deterministic():
    panel = _panel()
    request = RetrievalRoutingRequest(placebo_rounds=20)
    first = evaluate_retrieval_routing(panel, request)
    second = evaluate_retrieval_routing(panel, request)
    assert first.model_dump(mode="json") == second.model_dump(mode="json")


def test_successful_evaluator_emits_only_fully_realized_neighbor_labels():
    panel = _panel(dates=80)
    response = evaluate_retrieval_routing(
        panel,
        RetrievalRoutingRequest(placebo_rounds=20),
    )
    assert response.status.value == "ok"
    assert response.events
    for event in response.events:
        for neighbor in event.neighbors:
            assert neighbor.label_available_date < event.query_date


def test_evaluator_purges_labels_that_cross_split_boundaries():
    panel = _panel(dates=80, horizon=3)
    response = evaluate_retrieval_routing(
        panel,
        RetrievalRoutingRequest(label_horizon=3, placebo_rounds=20),
    )
    assert response.status.value == "ok"

    train_label_count = sum(response.coverage.train_label_counts.values())
    assert train_label_count == 45 * len(panel.symbols)

    test_start = panel.dates[64]
    validation_events = [event for event in response.events if event.split is SplitName.VALIDATION]
    assert validation_events
    for event in validation_events:
        event_index = panel.dates.index(event.query_date)
        assert panel.dates[event_index + panel.label_horizon] < test_start


def test_production_panel_builder_freezes_identity_and_forward_tail():
    days = tuple(date(2020, 1, 1) + timedelta(days=i) for i in range(80))
    symbols = tuple(f"{index:06d}.SH" for index in range(30))

    class Reader:
        def generation(self):
            return "canonical-v1"

        def manifest_sha256(self):
            return "a" * 64

        def market_days(self, start, end):
            return [day for day in days if start <= day <= end]

        def daily_bars(self, symbol, start, end):
            symbol_offset = symbols.index(symbol) / 100
            return pl.DataFrame(
                {
                    "date": [day for day in days if start <= day <= end],
                    "open": [10 + symbol_offset + index * 0.01 for index in range(len(days))],
                    "high": [10.2 + symbol_offset + index * 0.01 for index in range(len(days))],
                    "low": [9.8 + symbol_offset + index * 0.01 for index in range(len(days))],
                    "close": [10.1 + symbol_offset + index * 0.01 for index in range(len(days))],
                    "volume": [1000 + index for index in range(len(days))],
                }
            )

    panel = build_pinned_factor_panel(
        Reader(),
        symbols,
        days[0],
        days[-1],
        feature_ids=("alpha101_004",),
        label_horizon=1,
    )
    assert panel.features["alpha101_004"].shape == (80, 30)
    assert np.isnan(panel.forward_returns[-1]).all()
    assert panel.identity["generation"] == "canonical-v1"
    assert len(panel.identity["content_sha256"]) == 64
