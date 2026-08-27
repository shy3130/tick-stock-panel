from datetime import date

import polars as pl

from app.services import n_shape_research_data as module
from app.storage.repository import KlineRepository


class _Canonical:
    def generation(self):
        return "canon-gen"

    def manifest_sha256(self):
        return "a" * 64

    def market_days(self, start, end):
        return [date(2026, 8, 27)]

    def daily_bars(self, *args):
        return pl.DataFrame({"raw_close": [1.0]})


class _Facts:
    def generation(self):
        return "markets-gen"

    def manifest_sha256(self):
        return "b" * 64

    def universe(self, start, end):
        return ["600519.SH"]

    def limit_regime_facts(self, *args):
        return {
            date(2026, 8, 27): {
                "limit_up_price": 11.0,
                "name": "普通",
                "is_st": False,
                "regime": "main_10",
            }
        }

    def close(self):
        self.closed = True


def test_composite_pins_both_sources_and_delegates(monkeypatch):
    monkeypatch.setattr(
        module.PublishedCanonicalDailyReader, "from_repository", lambda repo: _Canonical()
    )
    monkeypatch.setattr(
        module.PublishedDailyMarketFactsReader, "from_repository", lambda repo: _Facts()
    )
    reader = module.PublishedNShapeResearchReader.from_repository(object())
    assert reader is not None
    assert reader.generation() == "canonical:canon-gen|markets:markets-gen"
    assert (
        reader.manifest_sha256()
        == module.hashlib.sha256(
            ("canonical:" + "a" * 64 + "|markets:" + "b" * 64).encode()
        ).hexdigest()
    )
    assert reader.source_provenance()["markets"]["generation"] == "markets-gen"
    assert reader.universe(date.min, date.max) == ["600519.SH"]
    assert reader.market_days(date.min, date.max) == [date(2026, 8, 27)]


def test_composite_fails_closed_if_source_missing(monkeypatch):
    facts = _Facts()
    monkeypatch.setattr(
        module.PublishedCanonicalDailyReader,
        "from_repository",
        lambda repo: None,
    )
    monkeypatch.setattr(
        module.PublishedDailyMarketFactsReader,
        "from_repository",
        lambda repo: facts,
    )
    assert module.PublishedNShapeResearchReader.from_repository(object()) is None
    assert facts.closed is True

    monkeypatch.setattr(
        module.PublishedCanonicalDailyReader,
        "from_repository",
        lambda repo: _Canonical(),
    )
    monkeypatch.setattr(
        module.PublishedDailyMarketFactsReader,
        "from_repository",
        lambda repo: None,
    )
    assert module.PublishedNShapeResearchReader.from_repository(object()) is None


def test_repository_property_pins_fresh_reader_per_request(monkeypatch):
    readers = [object(), object()]
    calls = []

    def build(repo):
        calls.append(repo)
        return readers[len(calls) - 1]

    monkeypatch.setattr(module.PublishedNShapeResearchReader, "from_repository", build)
    repo = object()
    getter = KlineRepository.n_shape_research_reader.fget
    assert getter(repo) is readers[0]
    assert getter(repo) is readers[1]
    assert calls == [repo, repo]
