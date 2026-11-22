from datetime import UTC, datetime
from itertools import permutations

import pytest

from tivelo_contracts import DataReader, ErrorCode, TiveloError
from tivelo_contracts.data import BarQuery, HistoryQuery, SymbolMapping
from tivelo_contracts.testing import InMemoryReader


def query(as_of="2025-01-03T00:00:00Z"):
    return BarQuery(
        instrument_ids=("security-1",),
        timeframe="1d",
        start="2025-01-01T00:00:00Z",
        end="2025-01-04T00:00:00Z",
        as_of=as_of,
    )


def test_d07_later_revision_is_visible_only_after_publication(make_bar):
    original = make_bar()
    revision = make_bar(
        close="10.4",
        revision_id="r2",
        available_at="2025-01-04T00:00:00Z",
        ingested_at="2025-01-04T00:01:00Z",
    )
    reader = InMemoryReader(bars=(revision, original), knowledge_basis="historical_versions")
    assert isinstance(reader, DataReader)
    assert reader.bars(query()) == (original,)
    assert reader.bars(query("2025-01-04T01:00:00Z")) == (revision,)
    assert reader.bars(query("2025-01-02T06:59:00Z")) == ()


def test_d07_snapshot_changes_with_revision_without_mutating_old_reader(make_bar):
    inputs = [make_bar()]
    first = InMemoryReader(bars=inputs, knowledge_basis="historical_versions")
    original_id = first.manifest.snapshot_id
    inputs.append(
        make_bar(
            revision_id="r2",
            close="10.4",
            available_at="2025-01-04T00:00:00Z",
            ingested_at="2025-01-04T00:01:00Z",
        )
    )
    second = InMemoryReader(bars=inputs, knowledge_basis="historical_versions")
    assert original_id == first.manifest.snapshot_id
    assert original_id != second.manifest.snapshot_id
    assert len(first.bars(query())) == 1


def test_d07_conflicting_visible_revision_fails_independent_of_input_order(make_bar):
    left, right = make_bar(), make_bar(revision_id="other", close="10.3")
    for records in permutations((left, right)):
        reader = InMemoryReader(bars=records, knowledge_basis="historical_versions")
        with pytest.raises(TiveloError) as raised:
            reader.bars(query())
        assert raised.value.code == ErrorCode.DATA_CONFLICT


def test_old_conflict_does_not_depend_on_record_order_after_new_revision(make_bar):
    records = (
        make_bar(),
        make_bar(revision_id="conflict", close="10.3"),
        make_bar(
            revision_id="new",
            close="10.4",
            available_at="2025-01-04T00:00:00Z",
            ingested_at="2025-01-04T00:01:00Z",
        ),
    )
    ids = set()
    for ordering in permutations(records):
        reader = InMemoryReader(bars=ordering, knowledge_basis="historical_versions")
        assert reader.bars(query("2025-01-04T01:00:00Z")) == (records[-1],)
        ids.add(reader.manifest.snapshot_id)
    assert len(ids) == 1


def test_d01_symbol_reuse_and_ambiguous_mapping(make_bar):
    def mapping(instrument, begin, end=None):
        return SymbolMapping(
            instrument_id=instrument,
            symbol="ABC",
            venue_id="SYNTHETIC",
            source_id="fixture",
            valid_from=begin,
            valid_to=end,
            available_at=begin,
            revision_id=instrument,
        )

    old = mapping("old-security", "2020-01-01T00:00:00Z", "2025-01-01T00:00:00Z")
    new = mapping("new-security", "2025-01-01T00:00:00Z")
    reader = InMemoryReader(
        bars=(make_bar(),), symbols=(old, new), knowledge_basis="historical_versions"
    )
    kwargs = dict(source_id="fixture", venue_id="SYNTHETIC", as_of=datetime(2025, 1, 3, tzinfo=UTC))
    assert (
        reader.resolve_symbol("ABC", effective_at=datetime(2024, 1, 1, tzinfo=UTC), **kwargs)
        == "old-security"
    )
    assert (
        reader.resolve_symbol("ABC", effective_at=datetime(2025, 1, 1, tzinfo=UTC), **kwargs)
        == "new-security"
    )
    collision = mapping("third-security", "2025-01-01T00:00:00Z")
    reader = InMemoryReader(symbols=(new, collision), knowledge_basis="historical_versions")
    with pytest.raises(TiveloError) as raised:
        reader.resolve_symbol("ABC", effective_at=datetime(2025, 1, 1, tzinfo=UTC), **kwargs)
    assert raised.value.code == ErrorCode.AMBIGUOUS_INSTRUMENT


def test_d06_historical_universe_does_not_drop_delisted_instruments(make_instrument):
    instrument = make_instrument(valid_to="2025-01-03T00:00:00Z")
    reader = InMemoryReader(instruments=(instrument,), knowledge_basis="historical_versions")
    before = HistoryQuery(
        instrument_ids=(instrument.instrument_id,),
        effective_at="2025-01-02T00:00:00Z",
        as_of="2025-01-02T00:00:00Z",
    )
    after = HistoryQuery(
        instrument_ids=(instrument.instrument_id,),
        effective_at="2025-01-03T00:00:00Z",
        as_of="2025-01-03T00:00:00Z",
    )
    assert reader.instruments(before) == (instrument,)
    assert reader.instruments(after) == ()


def test_d09_adjusted_facts_are_not_silently_read_as_raw(make_bar):
    reader = InMemoryReader(
        bars=(make_bar(price_basis="qfq"),), knowledge_basis="latest_reconstruction"
    )
    assert reader.bars(query()) == ()
    assert reader.datasets[0].contents.price_basis == "qfq"
    assert reader.datasets[0].contents.knowledge_basis == "latest_reconstruction"
