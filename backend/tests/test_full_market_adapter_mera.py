from datetime import date, timedelta
from types import SimpleNamespace

from app.services.full_market_adapters.mera import MeraAdapter
from app.services.full_market_research import RunnerContext
from app.services.retrieval_routing_research.models import (
    CLAIM_IDS,
    DEFAULT_COST_BPS,
    ClaimVerdict,
    CoverageReport,
    FrozenStatistics,
    PlaceboResult,
    Provenance,
    RetrievalRoutingResponse,
    RoutingStatus,
    RoutingVerdictStatus,
    SplitMetrics,
    SplitName,
)

START = date(2024, 1, 1)
END = date(2025, 1, 31)
COHORT = ["600000.SH", "000001.SZ", "300750.SZ", "688001.SH"]


def _request():
    return MeraAdapter().build_request(
        START, END, COHORT, oos_start=date(2025, 7, 1), cost_bps=12.5
    )


def test_build_request_keeps_complete_cohort_and_frozen_protocol():
    request = MeraAdapter().build_request(
        START, END, COHORT, oos_start=date(2025, 7, 1), cost_bps=None
    )

    assert list(request.symbols) == COHORT
    assert request.symbols is not COHORT
    assert request.start == START
    assert request.end == END
    assert request.routing.cost_bps == DEFAULT_COST_BPS
    assert request.routing.label_horizon == 1
    assert request.routing.placebo_rounds == 200


def test_evaluate_passes_complete_cohort_to_panel_and_evaluator_once(monkeypatch):
    adapter = MeraAdapter()
    request = _request()
    panel = object()
    expected = object()
    panel_calls = []
    evaluator_calls = []

    def fake_panel(reader, symbols, start, end, *, feature_ids, label_horizon):
        panel_calls.append((reader, symbols, start, end, feature_ids, label_horizon))
        return panel

    def fake_evaluator(received_panel, received_request):
        evaluator_calls.append((received_panel, received_request))
        return expected

    monkeypatch.setattr(
        "app.services.full_market_adapters.mera.build_pinned_factor_panel", fake_panel
    )
    monkeypatch.setattr(
        "app.services.full_market_adapters.mera.evaluate_retrieval_routing",
        fake_evaluator,
    )
    reader = object()

    result = adapter.evaluate(RunnerContext(repo=SimpleNamespace(), reader=reader), request)

    assert result is expected
    assert len(panel_calls) == 1
    assert panel_calls[0][0] is reader
    assert tuple(panel_calls[0][1]) == tuple(COHORT)
    assert panel_calls[0][2:4] == (START, END)
    assert len(evaluator_calls) == 1
    assert evaluator_calls == [(panel, request.routing)]


def test_preload_panel_runs_once_with_padded_full_cohort_and_fails_closed(monkeypatch):
    adapter = MeraAdapter()
    request = _request()
    preload_calls = []
    panel_calls = []
    panel = object()

    class Reader:
        def preload_panel(self, start, end, *, symbols):
            preload_calls.append((start, end, symbols))

    monkeypatch.setattr(
        "app.services.full_market_adapters.mera.build_pinned_factor_panel",
        lambda *args, **kwargs: panel_calls.append((args, kwargs)) or panel,
    )
    monkeypatch.setattr(
        "app.services.full_market_adapters.mera.evaluate_retrieval_routing",
        lambda received_panel, received_request: (received_panel, received_request),
    )

    result = adapter.evaluate(RunnerContext(repo=SimpleNamespace(), reader=Reader()), request)

    assert result == (panel, request.routing)
    assert len(preload_calls) == 1
    assert preload_calls[0] == (
        START - timedelta(days=400),
        END + timedelta(days=180),
        COHORT,
    )
    assert len(panel_calls) == 1

    failing_calls = []

    class FailingReader:
        def preload_panel(self, start, end, *, symbols):
            failing_calls.append((start, end, symbols))
            raise RuntimeError("preload unavailable")

    def must_not_build(*args, **kwargs):
        raise AssertionError("panel build must not follow preload failure")

    monkeypatch.setattr(
        "app.services.full_market_adapters.mera.build_pinned_factor_panel", must_not_build
    )
    unavailable = adapter.serialize_verdict(
        adapter.evaluate(
            RunnerContext(repo=SimpleNamespace(), reader=FailingReader()),
            request,
        )
    )

    assert len(failing_calls) == 1
    assert unavailable["status"] == "unavailable"
    assert unavailable["unavailable_reason"] == "unavailable_panel_coverage"
    assert unavailable["verdicts"] == []
    assert unavailable["placebos"] == []


def test_missing_reader_is_unavailable_without_claim_or_placebo_merge():
    adapter = MeraAdapter()
    request = _request()

    result = adapter.evaluate(RunnerContext(repo=SimpleNamespace(), reader=None), request)
    serialized = adapter.serialize_verdict(result)

    assert serialized["status"] == "unavailable"
    assert serialized["unavailable_reason"] == "unavailable_panel_coverage"
    assert serialized["verdicts"] == []
    assert serialized["placebos"] == []
    assert serialized["events"] == []
    assert serialized["splits"] == []
    assert adapter.extract_coverage(serialized) is None


def test_panel_failure_is_unavailable_without_partial_results(monkeypatch):
    adapter = MeraAdapter()
    request = _request()

    def fail_panel(*args, **kwargs):
        raise RuntimeError("canonical_symbol_missing:600000.SH")

    monkeypatch.setattr(
        "app.services.full_market_adapters.mera.build_pinned_factor_panel", fail_panel
    )

    serialized = adapter.serialize_verdict(
        adapter.evaluate(RunnerContext(repo=SimpleNamespace(), reader=object()), request)
    )

    assert serialized["status"] == "unavailable"
    assert "canonical_symbol_missing:600000.SH" in serialized["unavailable_detail"]
    assert serialized["verdicts"] == []
    assert serialized["placebos"] == []
    assert serialized["splits"] == []


def test_serialization_preserves_separate_claims_and_placebo_diagnostics():
    request = _request()
    frozen = FrozenStatistics(
        feature_names=["alpha_1"],
        feature_means={"alpha_1": 0.0},
        feature_stds={"alpha_1": 1.0},
        label_quantile_edges=[1 / 3, 2 / 3],
        train_start=date(2024, 1, 1),
        train_end=date(2024, 6, 30),
        validation_start=date(2024, 7, 1),
        validation_end=date(2024, 9, 30),
        test_start=date(2024, 10, 1),
        test_end=date(2025, 1, 31),
    )
    coverage = CoverageReport(
        symbols=len(COHORT),
        dates=10,
        warmed_samples=30,
        eligible_samples=25,
        train_dates=6,
        validation_dates=2,
        test_dates=2,
        train_samples=18,
        validation_samples=6,
        test_samples=6,
        min_eligible_per_eval_date=20,
    )
    splits = [
        SplitMetrics(
            split=split,
            dates=2,
            samples=6,
            queries=6,
            censored_queries=0,
            rank_ic_dates=2,
            routing_rank_ic=0.1,
            baseline_feature="alpha_1",
            baseline_rank_ic=0.05,
            rank_ic_increment=0.05,
            cost_adjusted_increment=0.03,
        )
        for split in SplitName
    ]
    verdict = RetrievalRoutingResponse(
        status=RoutingStatus.OK,
        request=request.routing,
        coverage=coverage,
        splits=splits,
        verdicts=[
            ClaimVerdict(
                claim=CLAIM_IDS[0],
                verdict=RoutingVerdictStatus.ACCEPTED,
                evidence={"metric": "rank_ic_increment"},
            ),
            ClaimVerdict(
                claim=CLAIM_IDS[1],
                verdict=RoutingVerdictStatus.REJECTED,
                evidence={"metric": "cost_adjusted_increment"},
            ),
        ],
        placebos=[
            PlaceboResult(
                kind="random_neighbor",
                claim=CLAIM_IDS[0],
                rounds=200,
                real_increment=0.05,
                placebo_mean=0.0,
                placebo_q95=0.01,
                blocked=False,
            ),
            PlaceboResult(
                kind="random_label",
                claim=CLAIM_IDS[1],
                rounds=200,
                real_increment=0.03,
                placebo_mean=0.0,
                placebo_q95=0.01,
                blocked=False,
            ),
        ],
        provenance=Provenance(frozen=frozen),
    )

    serialized = MeraAdapter().serialize_verdict(verdict)

    assert [item["claim"] for item in serialized["verdicts"]] == [
        "test_rank_ic_increment",
        "test_cost_adjusted_increment",
    ]
    assert serialized["verdicts"][0]["evidence"] != serialized["verdicts"][1]["evidence"]
    assert {
        "kind",
        "claim",
        "rounds",
        "real_increment",
        "placebo_mean",
        "placebo_q95",
        "blocked",
    } <= set(serialized["placebos"][0])
    assert {item["kind"] for item in serialized["placebos"]} == {
        "random_neighbor",
        "random_label",
    }
    assert MeraAdapter().extract_coverage(serialized)["symbols"] == len(COHORT)
