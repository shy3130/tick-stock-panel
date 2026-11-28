import pytest

from tivelo_contracts.data import AdjustmentFactor, CorporateAction, HistoryQuery, SymbolMapping
from tivelo_contracts.testing import InMemoryReader


def query(day):
    return HistoryQuery(
        instrument_ids=("security-1",),
        effective_at=f"2025-01-{day:02d}T00:00:00Z",
        as_of=f"2025-01-{day:02d}T00:00:00Z",
    )


def test_d06_known_delisting_does_not_resurrect_older_open_interval(make_instrument):
    original = make_instrument()
    ended = make_instrument(
        valid_to="2025-01-03T00:00:00Z",
        available_at="2025-01-02T00:00:00Z",
        revision_id="r2",
        delisting_date={"state": "known", "value": "2025-01-03"},
    )
    reader = InMemoryReader(instruments=(original, ended), knowledge_basis="historical_versions")
    assert reader.instruments(query(1)) == (original,)
    assert reader.instruments(query(2)) == (ended,)
    assert reader.instruments(query(3)) == ()


@pytest.mark.parametrize("cancelled,rescheduled", [(True, False), (False, True)])
def test_d07_action_revision_is_selected_before_effective_time_filter(cancelled, rescheduled):
    base = dict(
        action_id="split",
        instrument_id="security-1",
        action_type="split",
        quantity_ratio="2",
        effective_at="2025-01-02T00:00:00Z",
        available_at="2025-01-01T00:00:00Z",
        source_id="synthetic",
        revision_id="r1",
    )
    original = CorporateAction(**base)
    base.update(revision_id="r2", available_at="2025-01-02T00:00:00Z", cancelled=cancelled)
    if rescheduled:
        base["effective_at"] = "2025-01-10T00:00:00Z"
    revised = CorporateAction(**base)
    reader = InMemoryReader(actions=(original, revised), knowledge_basis="historical_versions")
    assert reader.actions(query(3)) == ()
    assert reader.actions(query(10)) == (() if cancelled else (revised,))


def test_d07_cancelled_factor_does_not_fall_back_to_earlier_revision():
    base = dict(
        factor_id="split",
        instrument_id="security-1",
        factor_kind="event_ratio",
        value="2",
        effective_at="2025-01-02T00:00:00Z",
        available_at="2025-01-01T00:00:00Z",
        source_id="synthetic",
        revision_id="r1",
        definition_id="test",
        baseline_id="listing",
    )
    original = AdjustmentFactor(**base)
    base.update(revision_id="r2", available_at="2025-01-03T00:00:00Z", cancelled=True)
    cancelled = AdjustmentFactor(**base)
    reader = InMemoryReader(factors=(original, cancelled), knowledge_basis="historical_versions")
    assert reader.factors(query(2)) == (original,)
    assert reader.factors(query(3)) == ()


def test_d01_closed_symbol_mapping_does_not_resurrect_open_revision():
    base = dict(
        instrument_id="security-1",
        symbol="ABC",
        venue_id="SYNTHETIC",
        source_id="fixture",
        valid_from="2020-01-01T00:00:00Z",
        available_at="2020-01-01T00:00:00Z",
        revision_id="r1",
    )
    original = SymbolMapping(**base)
    base.update(
        valid_to="2025-01-03T00:00:00Z", available_at="2025-01-02T00:00:00Z", revision_id="r2"
    )
    closed = SymbolMapping(**base)
    reader = InMemoryReader(symbols=(original, closed), knowledge_basis="historical_versions")
    from tivelo_contracts import ErrorCode, TiveloError

    with pytest.raises(TiveloError) as raised:
        reader.resolve_symbol(
            "ABC",
            source_id="fixture",
            venue_id="SYNTHETIC",
            effective_at=query(3).effective_at,
            as_of=query(3).as_of,
        )
    assert raised.value.code == ErrorCode.INSTRUMENT_NOT_FOUND


def test_d01_revised_symbol_does_not_leave_old_alias_visible():
    from tivelo_contracts import ErrorCode, TiveloError

    original = SymbolMapping(
        instrument_id="security-1",
        symbol="OLD",
        venue_id="SYNTHETIC",
        source_id="fixture",
        valid_from="2020-01-01T00:00:00Z",
        available_at="2020-01-01T00:00:00Z",
        revision_id="r1",
    )
    values = original.model_dump(mode="python")
    values.update(symbol="NEW", available_at="2025-01-02T00:00:00Z", revision_id="r2")
    revised = SymbolMapping(**values)
    reader = InMemoryReader(symbols=(original, revised), knowledge_basis="historical_versions")
    kwargs = dict(
        source_id="fixture",
        venue_id="SYNTHETIC",
        effective_at=query(3).effective_at,
        as_of=query(3).as_of,
    )
    assert reader.resolve_symbol("NEW", **kwargs) == "security-1"
    with pytest.raises(TiveloError) as raised:
        reader.resolve_symbol("OLD", **kwargs)
    assert raised.value.code == ErrorCode.INSTRUMENT_NOT_FOUND
