import pytest
from pydantic import ValidationError

from tivelo_contracts.data import EventQuery, OpeningObservation
from tivelo_contracts.protocols import ExecutionDataReader
from tivelo_contracts.testing import InMemoryReader


def opening(**changes):
    values = dict(
        instrument_id="A",
        event_at="2025-01-02T01:30:00Z",
        trading_date="2025-01-02",
        session_id="fixture.open",
        state="tradable",
        raw_open="10",
        quote_currency="CNY",
        reference_price="9",
        reference_kind="previous_close",
        available_at="2025-01-02T01:25:00Z",
        ingested_at="2025-01-02T01:25:01Z",
        source_id="fixture",
        revision_id="1",
    )
    values.update(changes)
    return OpeningObservation(**values)


def query(as_of):
    return EventQuery(
        instrument_ids=("A",), start="2025-01-02T00:00:00Z", end="2025-01-03T00:00:00Z", as_of=as_of
    )


def test_opening_fact_is_independent_of_the_unfinished_daily_bar():
    observation = opening()
    assert OpeningObservation.model_validate_json(observation.model_dump_json()) == observation
    reader = InMemoryReader(openings=(observation,), knowledge_basis="historical_versions")
    assert isinstance(reader, ExecutionDataReader)
    assert reader.openings(query("2025-01-02T01:29:59Z")) == ()
    assert reader.openings(query("2025-01-02T01:30:00Z")) == (observation,)
    assert not hasattr(observation, "high")
    assert not hasattr(observation, "close")


@pytest.mark.parametrize(
    "changes",
    [
        dict(raw_open=None),
        dict(state="suspended"),
        dict(reference_kind=None),
        dict(ingested_at="2025-01-01T00:00:00Z"),
        dict(raw_open=10.0),
    ],
)
def test_opening_state_and_provenance_are_not_inferred(changes):
    with pytest.raises(ValidationError):
        opening(**changes)


def test_later_revisions_and_unfinished_revisions_never_change_an_earlier_open():
    original = opening()
    revision = opening(
        raw_open="11",
        revision_id="2",
        available_at="2025-01-02T07:00:00Z",
        ingested_at="2025-01-02T07:00:00Z",
    )
    unfinished = opening(
        revision_id="3",
        is_final=False,
        available_at="2025-01-03T00:00:00Z",
        ingested_at="2025-01-03T00:00:00Z",
    )
    reader = InMemoryReader(
        openings=(unfinished, revision, original), knowledge_basis="historical_versions"
    )
    assert reader.openings(query("2025-01-02T01:30:00Z")) == (original,)
    assert reader.openings(query("2025-01-02T07:00:00Z")) == (revision,)
    assert reader.openings(query("2025-01-03T00:00:00Z")) == ()


def test_unfinished_bar_revision_does_not_resurrect_an_old_final_bar(make_bar):
    from tivelo_contracts.data import BarQuery

    original = make_bar()
    revision = make_bar(
        is_final=False,
        revision_id="2",
        available_at="2025-01-03T00:00:00Z",
        ingested_at="2025-01-03T00:00:00Z",
    )
    reader = InMemoryReader(bars=(original, revision), knowledge_basis="historical_versions")
    assert (
        reader.bars(
            BarQuery(
                instrument_ids=("security-1",),
                timeframe="1d",
                start="2025-01-01T00:00:00Z",
                end="2025-01-04T00:00:00Z",
                as_of="2025-01-03T00:00:00Z",
            )
        )
        == ()
    )
