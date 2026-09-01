from datetime import date
from types import SimpleNamespace

import pytest

from app.services.full_market_adapters import hold_firm as hold_firm_module
from app.services.full_market_adapters.hold_firm import (
    DEFAULT_OOS_START,
    HoldFirmAdapter,
)
from app.services.full_market_research import RunnerContext
from app.services.hold_firm_patterns import HoldFirmResponse
from app.services.hold_firm_patterns.models import (
    FACTOR_IDS,
    CanonicalIdentity,
    DataIdentity,
    FactorResult,
    HoldFirmStatus,
    HoldFirmVerdict,
    MarketFactsIdentity,
    Provenance,
    UnavailabilityReason,
    UniverseIdentity,
)

START = date(2024, 1, 1)
END = date(2026, 1, 31)
HEX64 = "a" * 64


@pytest.fixture(autouse=True)
def _matching_pinned_sources(monkeypatch):
    monkeypatch.setattr(
        hold_firm_module,
        "production_scope_matches",
        lambda context, canonical, facts: True,
    )


def _cohort(size: int = 250) -> list[str]:
    return [f"{index:06d}.SZ" for index in range(1, size + 1)]


def _ok_response() -> HoldFirmResponse:
    universe = UniverseIdentity(
        generation="20260831T000000Z-0123456789abcdef",
        manifest_sha256=HEX64,
        schema_version=2,
        artifact="universe_presence",
        rule_version="presence_v1",
        retrospective=True,
        status_filter="daily_market_row_present_exact_day",
        source_artifact="fstore_snapshot",
        source_generation="20260831T000000",
        source_manifest_sha256=HEX64,
        day_identities=(),
    )
    provenance = Provenance(
        identities=DataIdentity(
            canonical=CanonicalIdentity(
                generation="canonical-generation",
                manifest_sha256=HEX64,
                source_generations={"canonical": "canonical-generation"},
                calendar_id="canonical-calendar",
            ),
            markets=MarketFactsIdentity(
                generation="markets-generation",
                manifest_sha256=HEX64,
            ),
            universe=universe,
        ),
        calendar_id="canonical-calendar",
        parameters={},
        params_provenance={},
        code_version="test",
    )
    factors = [
        FactorResult(
            factor_id=factor_id,
            parent_events=4,
            qualified_events=2,
            not_selected_events=2,
            selection_verdict=HoldFirmVerdict.ACCEPTED,
            holding_verdict=HoldFirmVerdict.ACCEPTED,
            verdict=HoldFirmVerdict.ACCEPTED,
        )
        for factor_id in FACTOR_IDS
    ]
    return HoldFirmResponse(
        status=HoldFirmStatus.OK,
        factors=factors,
        provenance=provenance,
    )


def test_build_request_keeps_complete_cohort_uncapped_in_one_request():
    adapter = HoldFirmAdapter()
    cohort = _cohort()

    request = adapter.build_request(
        START,
        END,
        cohort,
        oos_start=None,
        cost_bps=None,
    )

    assert request.symbols == cohort
    assert request.symbols is not cohort
    assert len(request.symbols) == 250
    assert request.oos_start == DEFAULT_OOS_START
    assert request.cost_bps == 10


def test_evaluate_passes_full_cohort_once_through_pinned_scope(monkeypatch):
    adapter = HoldFirmAdapter()
    request = adapter.build_request(
        START,
        END,
        _cohort(),
        oos_start=date(2025, 7, 1),
        cost_bps=12.5,
    )

    class Canonical:
        def __init__(self):
            self.preload_calls = []

        def preload_panel(self, start, end, *, symbols):
            self.preload_calls.append((start, end, symbols))

    canonical, market_facts, universe = Canonical(), object(), object()
    scope = SimpleNamespace(
        canonical=canonical,
        market_facts=market_facts,
        universe_reader=universe,
    )
    calls = []
    expected = _ok_response()

    class FakeScope:
        def __enter__(self):
            return scope

        def __exit__(self, *exc_info):
            return None

    def fake_scope(repo):
        assert repo is sentinel_repo
        return FakeScope()

    def fake_evaluate(received_request, received_reader, received_facts, received_universe):
        calls.append((received_request, received_reader, received_facts, received_universe))
        return expected

    sentinel_repo = object()
    monkeypatch.setattr(
        "app.services.full_market_adapters.hold_firm.production_reader_scope",
        fake_scope,
    )
    monkeypatch.setattr(
        "app.services.full_market_adapters.hold_firm.evaluate_hold_firm_patterns",
        fake_evaluate,
    )

    result = adapter.evaluate(RunnerContext(repo=sentinel_repo, reader=object()), request)

    assert result is expected
    assert calls == [(request, canonical, market_facts, universe)]
    assert calls[0][0].symbols == request.symbols
    assert canonical.preload_calls == [(date(2022, 11, 27), date(2026, 5, 31), request.symbols)]


def test_missing_production_reader_fails_closed_without_fallback():
    adapter = HoldFirmAdapter()
    request = adapter.build_request(
        START,
        END,
        _cohort(2),
        oos_start=date(2025, 7, 1),
        cost_bps=None,
    )

    result = adapter.evaluate(
        RunnerContext(repo=SimpleNamespace(), reader=object()),
        request,
    )

    assert result.status is HoldFirmStatus.UNAVAILABLE
    assert result.unavailable_reason is UnavailabilityReason.CANONICAL_READER

    serialized = adapter.serialize_verdict(result)
    assert serialized["status"] == "unavailable"
    assert serialized["unavailable_reasons"] == ["unavailable_canonical_reader"]
    disclosure = serialized["bias_disclosure"]
    assert disclosure["survivorship_bias"] == "unavailable"
    assert disclosure["retrospective_universe"] is None
    assert disclosure["cohort_expanded_to_hide_bias"] is False
    assert adapter.extract_coverage(serialized) is None


def test_serialize_retains_pit_provenance_and_bias_disclosure():
    adapter = HoldFirmAdapter()
    serialized = adapter.serialize_verdict(_ok_response())

    disclosure = serialized["bias_disclosure"]
    assert disclosure["survivorship_bias"] == "controlled_within_pinned_universe"
    assert disclosure["retrospective_universe"] is True
    assert disclosure["cohort_expanded_to_hide_bias"] is False
    assert disclosure["universe_provenance"]["artifact"] == "universe_presence"
    assert disclosure["universe_provenance"]["rule_version"] == "presence_v1"
    assert "full pinned-market PIT cohort" in disclosure["note"]

    coverage = adapter.extract_coverage(serialized)
    assert coverage is not None
    assert set(coverage) == set(FACTOR_IDS)
    assert coverage[FACTOR_IDS[0]]["parent_events"] == 4
    assert coverage[FACTOR_IDS[0]]["qualified_events"] == 2
