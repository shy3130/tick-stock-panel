from types import SimpleNamespace

from app.services.full_market_adapters.pinning import (
    production_scope_matches,
    source_reader_matches,
)
from app.services.full_market_research import RunnerContext


class _Composite:
    def source_provenance(self):
        return {
            "canonical": {"generation": "canonical-g1", "manifest_sha256": "a" * 64},
            "markets": {"generation": "markets-g1", "manifest_sha256": "b" * 64},
        }


class _Reader:
    def __init__(self, generation: str, digest: str):
        self._generation = generation
        self._digest = digest

    def generation(self):
        return self._generation

    def manifest_sha256(self):
        return self._digest


def test_source_reader_requires_exact_runner_pin():
    context = RunnerContext(repo=SimpleNamespace(), reader=_Composite())

    assert source_reader_matches(context, "canonical", _Reader("canonical-g1", "a" * 64))
    assert not source_reader_matches(context, "canonical", _Reader("canonical-g2", "a" * 64))
    assert not source_reader_matches(context, "canonical", object())


def test_production_scope_requires_both_source_pins():
    context = RunnerContext(repo=SimpleNamespace(), reader=_Composite())
    canonical = _Reader("canonical-g1", "a" * 64)
    markets = _Reader("markets-g1", "b" * 64)

    assert production_scope_matches(context, canonical, markets)
    assert not production_scope_matches(context, canonical, _Reader("markets-g2", "b" * 64))
