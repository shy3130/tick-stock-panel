import json
from datetime import UTC, datetime

import pytest
from tivelo_contracts import ErrorCode, TiveloError
from tivelo_contracts.data import (
    AdjustmentFactor,
    CorporateAction,
    HistoryQuery,
    Instrument,
    SymbolMapping,
)
from tivelo_contracts.testing import InMemoryReader


def query(day):
    return HistoryQuery(
        instrument_ids=("security-1",),
        effective_at=f"2025-01-{day:02d}T00:00:00Z",
        as_of=f"2025-01-{day:02d}T00:00:00Z",
    )


def instrument(**changes):
    values = dict(
        instrument_id="security-1",
        venue_id="XSHG",
        market_id="cn.equity",
        asset_class="equity",
        instrument_type="common_stock",
        display_symbol="600000",
        name="Synthetic",
        quote_currency="CNY",
        settlement_currency="CNY",
        calendar_id="XSHG",
        timezone="Asia/Shanghai",
        tradable=True,
        listing_date={"state": "known", "value": "2020-01-01"},
        delisting_date={"state": "not_yet"},
        spec_revision="1",
        valid_from="2020-01-01T00:00:00Z",
        available_at="2020-01-01T00:00:00Z",
        revision_id="1",
        source_id="fixture",
    )
    values.update(changes)
    return Instrument(**values)


def test_d06_nested_specs_and_delisting_match_public_reader(make_engine):
    records = (
        instrument(),
        instrument(
            valid_to="2025-01-03T00:00:00Z",
            available_at="2025-01-02T00:00:00Z",
            revision_id="2",
            delisting_date={"state": "known", "value": "2025-01-03"},
        ),
    )
    reference = InMemoryReader(instruments=records, knowledge_basis="historical_versions")
    engine, _, plan = make_engine(records, record_type="instrument")
    version = engine.publish(engine.sync(plan))
    snapshot = engine.create_snapshot((version.version_id,))
    with engine.open_reader(snapshot.snapshot_id) as reader:
        for day in (1, 2, 3):
            assert reader.instruments(query(day)) == reference.instruments(query(day))
        assert reader.instruments(query(3)) == ()


@pytest.mark.parametrize("cancelled,rescheduled", [(True, False), (False, True)])
def test_d07_action_cancellation_and_reschedule_do_not_resurrect_old_version(
    make_engine, cancelled, rescheduled
):
    original = CorporateAction(
        action_id="split",
        instrument_id="security-1",
        action_type="split",
        quantity_ratio="2",
        effective_at="2025-01-02T00:00:00Z",
        available_at="2025-01-01T00:00:00Z",
        revision_id="1",
        source_id="fixture",
    )
    values = original.model_dump(mode="python")
    values.update(revision_id="2", available_at="2025-01-02T00:00:00Z", cancelled=cancelled)
    if rescheduled:
        values["effective_at"] = "2025-01-10T00:00:00Z"
    records = (original, CorporateAction(**values))
    reference = InMemoryReader(actions=records, knowledge_basis="historical_versions")
    engine, _, plan = make_engine(records, record_type="corporate_action")
    snapshot = engine.create_snapshot((engine.publish(engine.sync(plan)).version_id,))
    with engine.open_reader(snapshot.snapshot_id) as reader:
        for day in (1, 2, 3, 10):
            assert reader.actions(query(day)) == reference.actions(query(day))
        assert reader.actions(query(3)) == ()


def test_d07_factor_cancellation_matches_reference_reader(make_engine):
    original = AdjustmentFactor(
        factor_id="event",
        instrument_id="security-1",
        value="2",
        factor_kind="event_ratio",
        definition_id="split-ratio-v1",
        baseline_id="listing",
        revision_id="1",
        source_id="fixture",
        effective_at="2025-01-02T00:00:00Z",
        available_at="2025-01-01T00:00:00Z",
    )
    values = original.model_dump(mode="python")
    values.update(revision_id="2", available_at="2025-01-03T00:00:00Z", cancelled=True)
    records = (original, AdjustmentFactor(**values))
    engine, _, plan = make_engine(records, record_type="adjustment_factor")
    snapshot = engine.create_snapshot((engine.publish(engine.sync(plan)).version_id,))
    with engine.open_reader(snapshot.snapshot_id) as reader:
        assert reader.factors(query(2)) == (original,)
        assert reader.factors(query(3)) == ()


def test_d01_symbol_rename_and_reuse_are_resolved_by_visible_history(make_engine):
    original = SymbolMapping(
        instrument_id="security-1",
        symbol="OLD",
        venue_id="XSHG",
        source_id="fixture",
        valid_from="2020-01-01T00:00:00Z",
        available_at="2020-01-01T00:00:00Z",
        revision_id="1",
    )
    values = original.model_dump(mode="python")
    values.update(symbol="NEW", revision_id="2", available_at="2025-01-02T00:00:00Z")
    records = (original, SymbolMapping(**values))
    engine, _, plan = make_engine(records, record_type="symbol_mapping")
    snapshot = engine.create_snapshot((engine.publish(engine.sync(plan)).version_id,))
    with engine.open_reader(snapshot.snapshot_id) as reader:
        options = dict(
            source_id="fixture",
            venue_id="XSHG",
            effective_at=query(3).effective_at,
            as_of=query(3).as_of,
        )
        assert reader.resolve_symbol("NEW", **options) == "security-1"
        with pytest.raises(TiveloError) as raised:
            reader.resolve_symbol("OLD", **options)
        assert raised.value.code == ErrorCode.INSTRUMENT_NOT_FOUND
        options["as_of"] = datetime(2025, 1, 1, tzinfo=UTC)
        assert reader.resolve_symbol("OLD", **options) == "security-1"


def test_cli_reads_instrument_and_action_revisions_from_the_selected_snapshot(
    make_engine, tmp_path, capsys
):
    from tivelo_data.cli import main

    rules = instrument()
    withdrawal = instrument(
        valid_to="2025-01-03T00:00:00Z",
        available_at="2025-01-04T00:00:00Z",
        revision_id="2",
        delisting_date={"state": "known", "value": "2025-01-03"},
    )
    split = CorporateAction(
        instrument_id="security-1",
        action_id="split",
        action_type="split",
        quantity_ratio="2",
        effective_at="2025-01-03T00:00:00Z",
        available_at="2025-01-01T00:00:00Z",
        revision_id="1",
        source_id="fixture",
    )
    revoked = split.model_copy(
        update={"cancelled": True, "revision_id": "2", "available_at": query(4).as_of}
    )
    versions = []
    for securities, actions in [((rules,), (split,)), ((rules, withdrawal), (split, revoked))]:
        selected = []
        for kind, records in (("instrument", securities), ("corporate_action", actions)):
            engine, _, plan = make_engine(records, record_type=kind)
            selected.append(engine.publish(engine.sync(plan)).version_id)
        versions.append(engine.create_snapshot(tuple(selected)).snapshot_id)

    request = tmp_path / "query.json"
    for snapshot, known_on, expected in (
        (versions[1], 3, (rules, split)),
        (versions[1], 4, (None, None)),
        (versions[0], 4, (rules, split)),
    ):
        request.write_text(
            query(3).model_copy(update={"as_of": query(known_on).as_of}).model_dump_json()
        )
        for kind, record in zip(("instrument", "corporate_action"), expected, strict=True):
            args = [
                "--root",
                str(engine.store.root),
                "query",
                "--snapshot",
                snapshot,
                "--record-type",
                kind,
                "--request",
                str(request),
            ]
            assert main(args) == 0
            captured = capsys.readouterr()
            assert not captured.err
            assert [json.loads(line) for line in captured.out.splitlines()] == (
                [] if record is None else [record.model_dump(mode="json")]
            )


def test_cli_resolves_codes_by_source_venue_business_and_knowledge_time(
    make_engine, tmp_path, capsys
):
    from tivelo_data.cli import main

    first = SymbolMapping(
        symbol="DEMO",
        instrument_id="security-1",
        source_id="fixture",
        venue_id="XSHG",
        valid_from=query(1).effective_at,
        available_at=query(1).as_of,
        revision_id="1",
    )
    ended = first.model_copy(
        update={
            "valid_to": query(3).effective_at,
            "available_at": query(4).as_of,
            "revision_id": "2",
        }
    )
    successor = first.model_copy(
        update={
            "instrument_id": "security-2",
            "valid_from": query(3).effective_at,
            "available_at": query(3).as_of,
        }
    )
    another_venue = first.model_copy(update={"instrument_id": "security-3", "venue_id": "XSHE"})
    engine, _, plan = make_engine(
        (first, ended, successor, another_venue), record_type="symbol_mapping"
    )
    snapshot = engine.create_snapshot((engine.publish(engine.sync(plan)).version_id,))
    request = tmp_path / "lookup.json"

    def lookup(effective, known, **overrides):
        fields = {
            "symbol": "DEMO",
            "source_id": "fixture",
            "venue_id": "XSHG",
            "effective_at": query(effective).effective_at.isoformat(),
            "as_of": query(known).as_of.isoformat(),
        }
        request.write_text(json.dumps({**fields, **overrides}))
        result = main(
            [
                "--root",
                str(engine.store.root),
                "resolve-symbol",
                "--snapshot",
                snapshot.snapshot_id,
                "--request",
                str(request),
            ]
        )
        captured = capsys.readouterr()
        if result == 0:
            assert not captured.err
            return json.loads(captured.out)["instrument_id"]
        assert result == 1 and not captured.out
        return json.loads(captured.err)["code"]

    assert lookup(3, 2) == "security-1"
    assert lookup(3, 3) == "AMBIGUOUS_INSTRUMENT"
    assert lookup(3, 4) == "security-2"
    assert lookup(2, 4) == "security-1"
    assert lookup(3, 4, venue_id="XSHE") == "security-3"
    assert lookup(3, 4, source_id="other") == "INSTRUMENT_NOT_FOUND"
    assert lookup(3, 4, as_of="2025-01-04") == "CONTRACT_INVALID"
