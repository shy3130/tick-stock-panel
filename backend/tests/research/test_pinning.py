import hashlib
import json
from types import SimpleNamespace

from app.data_providers.fquant import generation
from app.research.pinning import PinnedResearchRepository


def _route_digest(routes: dict) -> str:
    return hashlib.sha256(
        json.dumps(routes, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def test_ordered_trans_reader_reopens_exact_preflight_generation(monkeypatch):
    calls = {}

    class FakeReader:
        def __init__(self, root, *, generation, manifest_sha256):
            calls["args"] = (root, generation, manifest_sha256)

        def generation(self):
            return calls["args"][1]

        def manifest_sha256(self):
            return calls["args"][2]

    monkeypatch.setattr(generation, "root_for", lambda kind: "/snapshots/ordered-trans")
    monkeypatch.setattr(
        "app.data_providers.fquant.ordered_trans.PublishedOrderedTransMinuteReader",
        FakeReader,
    )
    proxy = PinnedResearchRepository.from_sources(
        SimpleNamespace(),
        [
            {
                "kind": "ordered_trans",
                "status": "ready",
                "generation": "ordered-g1",
                "manifest_sha256": "a" * 64,
            }
        ],
    )

    reader = proxy.open_ordered_trans_reader()

    assert calls["args"] == ("/snapshots/ordered-trans", "ordered-g1", "a" * 64)
    assert reader.generation() == "ordered-g1"


def test_escape_intraday_reader_reopens_exact_route_set(monkeypatch):
    routes = {
        "2026-08-28": {
            "minutes": {
                "path": "/snapshots/minutes/2026-08-28.duckdb",
                "generation": "minutes-g1",
                "file": "2026-08-28.duckdb",
                "manifest_sha256": "b" * 64,
            },
            "trans": {
                "path": "/snapshots/trans/2026-08-28.duckdb",
                "generation": "trans-g1",
                "file": "2026-08-28.duckdb",
                "manifest_sha256": "c" * 64,
            },
        }
    }
    digest = _route_digest(routes)
    calls = {}

    class FakeReader:
        @classmethod
        def from_pin(cls, days, markets_reader, route_pins):
            calls["args"] = (tuple(days), markets_reader, route_pins)
            return cls()

        def identity(self):
            return {
                "generation": f"catalog-routes:{digest[:16]}",
                "manifest_sha256": digest,
            }

    markets = object()
    monkeypatch.setattr(
        "app.data_providers.fquant.escape_risk_intraday.CatalogPinnedEscapeRiskIntradayReader",
        FakeReader,
    )
    proxy = PinnedResearchRepository.from_sources(
        SimpleNamespace(),
        [
            {
                "kind": "escape_intraday",
                "status": "ready",
                "generation": f"catalog-routes:{digest[:16]}",
                "manifest_sha256": digest,
                "pin": {"routes": routes},
            }
        ],
    )
    monkeypatch.setattr(proxy, "_open_research_market_pin", lambda: markets)

    reader = proxy.open_escape_risk_intraday_reader({}, ("2026-08-28",))

    assert calls["args"] == (("2026-08-28",), markets, routes)
    assert reader.identity()["manifest_sha256"] == digest

def test_preflight_records_ordered_trans_identity(monkeypatch):
    class FakeReader:
        def generation(self):
            return "ordered-g1"

        def manifest_sha256(self):
            return "d" * 64

        def close(self):
            return None

    provider = SimpleNamespace(
        open_ordered_trans_reader=lambda: FakeReader(),
        close=lambda: None,
    )
    monkeypatch.setattr(
        "app.data_providers.registry.get_active_provider_name",
        lambda **kwargs: "fquant_local",
    )
    monkeypatch.setattr("app.data_providers.registry.get_provider", lambda name: provider)

    from app.research.preflight import _collect_requirement_source

    sources = []
    blockers = []
    warnings = []
    _collect_requirement_source(
        SimpleNamespace(),
        "mtf-direction",
        "trans",
        None,
        None,
        sources,
        blockers,
        warnings,
    )

    assert [(source.kind, source.generation) for source in sources] == [
        ("ordered_trans", "ordered-g1")
    ]
    assert blockers == []
    assert warnings == []
