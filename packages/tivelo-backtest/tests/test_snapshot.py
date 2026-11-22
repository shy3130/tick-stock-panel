"""Portable reader conformance, including producer-independent corrupt fixtures."""

import hashlib
from decimal import Decimal

import polars as pl
import pytest
from test_daily import at, bar, opening, security
from tivelo_contracts.artifacts import SnapshotBundle
from tivelo_contracts.base import canonical_bytes
from tivelo_contracts.data import (
    AdjustmentFactor,
    Artifact,
    BarQuery,
    CorporateAction,
    DatasetContents,
    DatasetVersion,
    EventQuery,
    HistoryQuery,
    SnapshotContents,
    SnapshotManifest,
    SymbolMapping,
)
from tivelo_contracts.errors import ErrorCode, TiveloError
from tivelo_contracts.facts import fact_fields
from tivelo_contracts.protocols import ExecutionDataReader
from tivelo_contracts.testing import InMemoryReader

from tivelo_backtest.legacy.snapshot_daily import read_legacy_daily_panel
from tivelo_backtest.snapshot import PortableSnapshotReader, _dtype

GROUPS = {
    "bar": "bars",
    "instrument": "instruments",
    "corporate_action": "actions",
    "adjustment_factor": "factors",
    "opening_observation": "openings",
    "symbol_mapping": "symbols",
}


def write_bundle(root, *, transform=None, metadata_change=None, **groups):
    root.mkdir()
    reference = InMemoryReader(knowledge_basis="historical_versions", **groups)
    versions = []
    for version in reference.datasets:
        kind = version.contents.record_type
        if kind == "extension_value":
            records = groups["extension_values"][version.contents.dataset_id]
        else:
            records = [
                r
                for r in groups.get(GROUPS[kind], ())
                if kind != "bar" or r.price_basis == version.contents.price_basis
            ]
        frame = pl.from_dicts(
            [r.model_dump(mode="python") for r in records],
            schema={field.name: _dtype(field) for field in fact_fields(kind)},
        )
        if transform:
            frame = transform(frame)
        metadata = {
            "tivelo.schema": "2.0",
            "tivelo.record_type": kind,
            "tivelo.timezone": "UTC",
            "tivelo.decimal": "decimal128(38,12)",
        }
        if metadata_change:
            metadata.update(metadata_change)
        temporary = root / "fixture.parquet"
        frame.write_parquet(temporary, metadata=metadata)
        digest = hashlib.sha256(temporary.read_bytes()).hexdigest()
        relative = f"datasets/{digest[:2]}/{digest}.parquet"
        artifact = Artifact(
            path=relative,
            sha256=digest,
            row_count=frame.height,
            size_bytes=temporary.stat().st_size,
        )
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary.rename(target)
        fields = version.contents.model_dump(mode="python")
        fields["artifacts"] = (artifact,)
        versions.append(DatasetVersion.create(DatasetContents(**fields)))
    manifest = SnapshotManifest.create(
        SnapshotContents(
            datasets=tuple(
                {"dataset_id": v.contents.dataset_id, "version_id": v.version_id} for v in versions
            )
        )
    )
    bundle = SnapshotBundle(snapshot=manifest, datasets=tuple(versions))
    (root / "bundle.json").write_bytes(canonical_bytes(bundle))
    return reference, bundle


def query(day=3, **changes):
    values = dict(
        instrument_ids=("A",), timeframe="1d", start=at(1, 0), end=at(9, 0), as_of=at(day, 8)
    )
    values.update(changes)
    return BarQuery(**values)


def test_portable_bars_match_memory_reader_and_keep_exact_decimals(tmp_path):
    original = bar(2, "10.123456789123", turnover=None)
    revised = bar(2, "11", revision_id="2", available_at=at(4), ingested_at=at(4))
    root = tmp_path / "bundle"
    reference, bundle = write_bundle(
        root, bars=(revised, original, bar(2, "50", price_basis="qfq"))
    )
    with PortableSnapshotReader(root, snapshot_id=bundle.snapshot.snapshot_id) as reader:
        assert isinstance(reader, ExecutionDataReader)
        assert reader.manifest == bundle.snapshot and reader.datasets == bundle.datasets
        for day in (1, 2, 3, 4):
            assert reader.bars(query(day)) == reference.bars(query(day))
        assert reader.bars(query())[0].close == Decimal("10.123456789123")
        assert reader.bars(query())[0].turnover is None
        assert reader.bars(query(price_basis="qfq")) == reference.bars(query(price_basis="qfq"))
        assert reader.bars(query(instrument_ids=("unknown",))) == ()
    with pytest.raises(TiveloError) as raised:
        reader.bars(query())
    assert raised.value.code == ErrorCode.CONTRACT_INVALID


def test_portable_reader_implements_the_public_reader_vectors(tmp_path):
    from tivelo_contracts.conformance import reader_fixture, verify_reader

    root = tmp_path / "snapshot"
    write_bundle(root, **reader_fixture())
    with PortableSnapshotReader(root) as reader:
        assert len(verify_reader(reader, execution=True)) == 18


def test_streamed_legacy_panel_preserves_visibility_units_and_columns(tmp_path):
    root = tmp_path / "bundle"
    reference, _ = write_bundle(
        root,
        bars=(
            bar(2, "10.123456789012", volume="9007199254740993.000000000001"),
            bar(2, "11", revision_id="2", available_at=at(4), ingested_at=at(4)),
            bar(2, "50", price_basis="qfq", turnover=None),
            bar(3, "10", is_final=False),
        ),
    )
    with PortableSnapshotReader(root) as reader:
        for basis in ("raw", "qfq"):
            for day in (1, 3, 4):
                requested = query(day, price_basis=basis)
                assert tuple(reader.iter_bars(requested)) == reference.bars(requested)
                symbols = {"A": "600000.SH"}
                panel = read_legacy_daily_panel(reader, requested, symbols=symbols)
                expected = read_legacy_daily_panel(reference, requested, symbols=symbols)
                assert panel.schema == expected.schema and panel.equals(expected)
                if basis == "raw":
                    assert panel["close"].equals(panel["raw_close"])


def test_latest_unfinished_bar_or_opening_does_not_resurrect_old_fact(tmp_path):
    bars = (
        bar(2, "10"),
        bar(2, "11", revision_id="2", available_at=at(3), ingested_at=at(3), is_final=False),
    )
    openings = (
        opening(2, "10"),
        opening(2, "11", revision_id="2", available_at=at(3), ingested_at=at(3), is_final=False),
    )
    root = tmp_path / "bundle"
    reference, _ = write_bundle(root, bars=bars, openings=openings)
    with PortableSnapshotReader(root) as reader:
        assert reader.bars(query(2)) == (bars[0],)
        assert reader.bars(query(3)) == reference.bars(query(3)) == ()
        for day in (1, 2, 3):
            request = EventQuery(instrument_ids=("A",), start=at(1), end=at(4), as_of=at(day, 8))
            assert reader.openings(request) == reference.openings(request)


@pytest.mark.parametrize("cancelled", [True, False])
def test_event_cancellation_or_reschedule_uses_latest_visible_revision(tmp_path, cancelled):
    original = CorporateAction(
        action_id="split",
        instrument_id="A",
        action_type="split",
        quantity_ratio="2",
        effective_at=at(2),
        available_at=at(1),
        revision_id="1",
        source_id="synthetic",
    )
    revised = CorporateAction(
        **{
            **original.model_dump(),
            "revision_id": "2",
            "available_at": at(2),
            "cancelled": cancelled,
            "effective_at": at(8) if not cancelled else at(2),
        }
    )
    factor = AdjustmentFactor(
        factor_id="event",
        instrument_id="A",
        value="2",
        factor_kind="event_ratio",
        definition_id="split",
        baseline_id="listing",
        effective_at=at(2),
        available_at=at(1),
        revision_id="1",
        source_id="synthetic",
    )
    removed = AdjustmentFactor(
        **{**factor.model_dump(), "revision_id": "2", "available_at": at(3), "cancelled": True}
    )
    root = tmp_path / "bundle"
    reference, _ = write_bundle(root, actions=(original, revised), factors=(factor, removed))
    with PortableSnapshotReader(root) as reader:
        for day in (1, 2, 3, 8):
            request = HistoryQuery(instrument_ids=("A",), effective_at=at(day), as_of=at(day))
            assert reader.actions(request) == reference.actions(request)
            assert reader.factors(request) == reference.factors(request)


def test_nested_instrument_history_and_symbol_rename(tmp_path):
    original = security()
    revised = type(original)(
        **{
            **original.model_dump(),
            "revision_id": "2",
            "available_at": at(2),
            "valid_to": at(3),
            "delisting_date": {"state": "known", "value": "2025-01-03"},
        }
    )
    symbol = SymbolMapping(
        instrument_id="A",
        symbol="OLD",
        venue_id="fixture",
        source_id="synthetic",
        valid_from=at(1),
        available_at=at(1),
        revision_id="1",
    )
    renamed = SymbolMapping(
        **{**symbol.model_dump(), "symbol": "NEW", "available_at": at(2), "revision_id": "2"}
    )
    root = tmp_path / "bundle"
    reference, _ = write_bundle(root, instruments=(original, revised), symbols=(symbol, renamed))
    with PortableSnapshotReader(root) as reader:
        for day in (1, 2, 3):
            request = HistoryQuery(instrument_ids=("A",), effective_at=at(day), as_of=at(day))
            assert reader.instruments(request) == reference.instruments(request)
        options = dict(source_id="synthetic", venue_id="fixture", effective_at=at(3), as_of=at(3))
        assert reader.resolve_symbol("NEW", **options) == "A"
        with pytest.raises(TiveloError) as raised:
            reader.resolve_symbol("OLD", **options)
        assert raised.value.code == ErrorCode.INSTRUMENT_NOT_FOUND


def test_conflict_is_detected_before_final_filter_and_newer_revision_resolves_it(tmp_path):
    records = (
        bar(2, "10"),
        bar(2, "11", revision_id="conflict", is_final=False),
        bar(2, "12", revision_id="new", available_at=at(4), ingested_at=at(4)),
    )
    root = tmp_path / "bundle"
    write_bundle(root, bars=records)
    with PortableSnapshotReader(root) as reader:
        with pytest.raises(TiveloError) as raised:
            reader.bars(query(3))
        assert raised.value.code == ErrorCode.DATA_CONFLICT
        assert reader.bars(query(4)) == (records[-1],)


@pytest.mark.parametrize("change", ["hash", "schema", "timezone", "metadata", "null", "basis"])
def test_corrupt_or_relabelled_facts_are_rejected_before_use(tmp_path, change):
    root = tmp_path / "bundle"
    transform = {
        "schema": lambda frame: frame.with_columns(pl.col("close").cast(pl.Float64)),
        "timezone": lambda frame: frame.with_columns(
            pl.col("available_at").dt.replace_time_zone(None)
        ),
        "null": lambda frame: frame.with_columns(
            pl.lit(None, dtype=pl.Datetime("us", "UTC")).alias("available_at")
        ),
        "basis": lambda frame: frame.with_columns(pl.lit("qfq").alias("price_basis")),
    }.get(change)
    _, bundle = write_bundle(
        root,
        bars=(bar(2, "10"),),
        transform=transform,
        metadata_change={"tivelo.schema": "3.0"} if change == "metadata" else None,
    )
    if change == "hash":
        (root / bundle.datasets[0].contents.artifacts[0].path).write_bytes(b"broken")
    with pytest.raises(TiveloError) as raised:
        PortableSnapshotReader(root)
    assert raised.value.code == ErrorCode.SNAPSHOT_CORRUPT


def test_mutation_after_open_and_missing_publication_marker_are_rejected(tmp_path):
    root = tmp_path / "bundle"
    _, bundle = write_bundle(root, bars=(bar(2, "10"),))
    with PortableSnapshotReader(root) as reader:
        (root / bundle.datasets[0].contents.artifacts[0].path).write_bytes(b"changed")
        with pytest.raises(TiveloError) as raised:
            reader.bars(query())
        assert raised.value.code == ErrorCode.SNAPSHOT_CORRUPT
    (root / "bundle.json").unlink()
    with pytest.raises(TiveloError) as raised:
        PortableSnapshotReader(root)
    assert raised.value.code == ErrorCode.SNAPSHOT_NOT_FOUND


def test_wrong_snapshot_and_symlink_facts_are_rejected(tmp_path):
    root = tmp_path / "bundle"
    _, bundle = write_bundle(root, bars=(bar(2, "10"),))
    with pytest.raises(TiveloError) as raised:
        PortableSnapshotReader(root, snapshot_id="a" * 64)
    assert raised.value.code == ErrorCode.DATA_CONFLICT
    fact = root / bundle.datasets[0].contents.artifacts[0].path
    outside = tmp_path / "outside.parquet"
    fact.rename(outside)
    try:
        fact.symlink_to(outside)
    except OSError:
        pytest.skip("symbolic links unavailable on this platform")
    with pytest.raises(TiveloError) as raised:
        PortableSnapshotReader(root)
    assert raised.value.code == ErrorCode.SNAPSHOT_CORRUPT
