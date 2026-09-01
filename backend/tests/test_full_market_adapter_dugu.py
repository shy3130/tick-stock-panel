"""Full-market adapter tests for the frozen dugu-trend 32-cell scan."""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from app.services.daily_event_research.dugu_trend import (
    dugu_scan_cell_id,
    iter_dugu_scan_grid,
)
from app.services.full_market_adapters import dugu as dugu_module
from app.services.full_market_adapters.dugu import DUGU_FULL_MARKET_SCHEMA, DuguTrendAdapter
from app.services.full_market_research import FactorAdapter, RunnerContext

COHORT = [f"{600000 + index:06d}.SH" for index in range(250)]
START, OOS_START, END = date(2024, 1, 2), date(2025, 1, 2), date(2025, 6, 30)
DAYS = (date(2024, 1, 2), date(2024, 6, 3), date(2025, 6, 30))


class _Canonical:
    def __init__(self):
        self.preload_calls = []

    def generation(self):
        return "canonical-generation"

    def manifest_sha256(self):
        return "b" * 64

    def manifest(self):
        return {"source_generations": {"markets": "markets-generation"}}

    def market_days(self, start, end):
        return [day for day in DAYS if start <= day <= end]

    def preload_panel(self, start, end, *, symbols):
        self.preload_calls.append((start, end, tuple(symbols)))


class _Facts:
    def __init__(self):
        self.closed = False
        self.fact_calls = []

    def generation(self):
        return "markets-generation"

    def manifest_sha256(self):
        return "a" * 64

    def limit_band_facts(self, symbol, start, end):
        self.fact_calls.append((symbol, start, end))
        return {day: object() for day in DAYS}

    def close(self):
        self.closed = True


class _Composite:
    def source_provenance(self):
        return {
            "canonical": {
                "generation": "canonical-generation",
                "manifest_sha256": "b" * 64,
            },
            "markets": {
                "generation": "markets-generation",
                "manifest_sha256": "a" * 64,
            },
        }


class _Repo:
    pass


class _CanonicalFactory:
    @classmethod
    def from_repository(cls, repo):
        return getattr(repo, "canonical", None)


class _FactsFactory:
    @classmethod
    def from_canonical_manifest(cls, manifest):
        return _Facts()


class _Response:
    def __init__(self, payload):
        self.payload = payload

    def model_dump(self, *, mode):
        assert mode == "json"
        return self.payload


def _request(cohort=COHORT):
    return DuguTrendAdapter().build_request(START, END, cohort, oos_start=OOS_START, cost_bps=None)


def test_adapter_satisfies_factor_protocol():
    adapter = DuguTrendAdapter()
    assert isinstance(adapter, FactorAdapter)
    assert adapter.name == "dugu-trend"


def test_build_request_carries_complete_cohort_and_defaults():
    request = _request()
    assert request.symbols == COHORT
    assert request.cost_bps == 10.0
    with pytest.raises(ValueError, match="dugu_oos_start_required"):
        DuguTrendAdapter().build_request(START, END, COHORT, oos_start=None, cost_bps=None)


def test_evaluate_preloads_once_prefetches_facts_once_and_reuses_bundle(monkeypatch):
    adapter, repo = DuguTrendAdapter(), _Repo()
    repo.canonical = _Canonical()
    calls = []
    monkeypatch.setattr(dugu_module, "PublishedCanonicalDailyReader", _CanonicalFactory)
    facts = _Facts()

    class FactsFactory:
        @classmethod
        def from_canonical_manifest(cls, manifest):
            return facts

    monkeypatch.setattr(dugu_module, "PublishedDailyMarketFactsReader", FactsFactory)

    def spy(request, reader, bundle):
        calls.append((request, reader, bundle))
        return _Response({"status": "ok", "coverage": {"symbols_requested": len(request.symbols)}})

    monkeypatch.setattr(dugu_module, "evaluate_daily_events", spy)
    verdict = adapter.evaluate(RunnerContext(repo=repo, reader=_Composite()), _request())
    expected = [dugu_scan_cell_id(config) for config in iter_dugu_scan_grid()]
    assert len(expected) == len(set(expected)) == 32 and len(calls) == 32
    assert {
        dugu_scan_cell_id(
            next(
                config
                for config in iter_dugu_scan_grid()
                if (config.variant, config.band_mode, config.require_m3, config.alignment_days)
                == (r.variant, r.band_mode, r.require_m3, r.alignment_days)
            )
        )
        for r, _, _ in calls
    } == set(expected)
    assert all(r.symbols == COHORT for r, _, _ in calls)
    assert len(repo.canonical.preload_calls) == 1
    assert repo.canonical.preload_calls[0] == (
        START - timedelta(days=400),
        END + timedelta(days=120),
        tuple(COHORT),
    )
    assert len(facts.fact_calls) == len(COHORT) and [item[0] for item in facts.fact_calls] == COHORT
    assert len({id(bundle) for _, _, bundle in calls}) == 1
    assert facts.closed
    assert verdict["schema"] == DUGU_FULL_MARKET_SCHEMA and verdict["status"] == "ok"
    assert verdict["scan_grid"]["cell_count"] == 32 and set(verdict["cells"]) == set(expected)
    assert set(adapter.extract_coverage(verdict)) == set(expected)


def test_missing_canonical_reader_is_explicitly_unavailable(monkeypatch):
    monkeypatch.setattr(dugu_module, "PublishedCanonicalDailyReader", _CanonicalFactory)
    verdict = DuguTrendAdapter().evaluate(RunnerContext(repo=_Repo(), reader=object()), _request())
    assert (
        verdict["status"] == "unavailable"
        and verdict["unavailable_reasons"] == ["unavailable_canonical_reader"]
        and verdict["cells"] == {}
    )


def test_missing_market_facts_is_explicitly_unavailable(monkeypatch):
    repo = _Repo()
    repo.canonical = _Canonical()
    monkeypatch.setattr(dugu_module, "PublishedCanonicalDailyReader", _CanonicalFactory)

    class UnavailableFacts:
        @classmethod
        def from_canonical_manifest(cls, manifest):
            raise RuntimeError("market facts unavailable")

    monkeypatch.setattr(dugu_module, "PublishedDailyMarketFactsReader", UnavailableFacts)
    verdict = DuguTrendAdapter().evaluate(RunnerContext(repo=repo, reader=_Composite()), _request())
    assert (
        verdict["status"] == "unavailable"
        and verdict["unavailable_reasons"] == ["unavailable_market_facts"]
        and verdict["cells"] == {}
    )
