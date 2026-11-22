from datetime import time, timedelta, timezone

import pytest
from test_daily import at, bar, security
from test_snapshot import query, write_bundle
from tivelo_contracts.base import fingerprint
from tivelo_contracts.data import DatasetScope
from tivelo_contracts.errors import ErrorCode, TiveloError
from tivelo_contracts.extension_fields import (
    ExtensionField,
    ExtensionFieldRef,
    ExtensionQuery,
    ExtensionValue,
)

from tivelo_backtest.legacy.snapshot_daily import read_legacy_daily_panel
from tivelo_backtest.research.matrix import build_market_data_matrix
from tivelo_backtest.research.matrix_selection import build_basic_filter_mask
from tivelo_backtest.snapshot import PortableSnapshotReader


def test_market_field_cutoffs_and_dataset_isolation_across_readers(tmp_path):
    field = ExtensionField(
        table_id="sentiment",
        table_version="1",
        field_id="phase",
        field_version="1",
        granularity="market",
        value_type="string",
        unit="category",
    )
    scope = DatasetScope(
        market_id="cn.equity",
        asset_class="equity",
        instrument_ids=(),
        start=at(1, 0),
        end=at(9, 0),
        extension_field=field,
    )
    first = ExtensionValue(
        field_sha256=fingerprint(field),
        instrument_id=None,
        effective_at=at(2),
        available_at=at(2, 5),
        ingested_at=at(2, 5),
        revision_id="1",
        source_id="public",
        text_value="steady",
    )
    correction = first.model_copy(
        update={
            "revision_id": "2",
            "available_at": at(3, 5),
            "ingested_at": at(3, 5),
            "text_value": None,
        }
    )
    other = first.model_copy(update={"text_value": "unrelated", "source_id": "alternate"})
    root = tmp_path / "bundle"
    memory, _ = write_bundle(
        root,
        scopes={"market": scope, "alternative": scope},
        extension_values={"market": (first, correction), "alternative": (other,)},
    )
    request = ExtensionQuery(
        dataset_id="market",
        field_sha256=fingerprint(field),
        instrument_ids=(),
        effective_at=at(2, 6),
        as_of=at(2, 6),
    )
    with PortableSnapshotReader(root) as portable:
        for reader in (memory, portable):
            assert reader.extension_values(request) == (first,)
            assert reader.extension_values(request.model_copy(update={"as_of": at(3, 6)})) == (
                correction,
            )
            assert reader.extension_values(
                request.model_copy(update={"dataset_id": "alternative"})
            ) == (other,)
            with pytest.raises(TiveloError) as exc:
                reader.extension_values(request.model_copy(update={"instrument_ids": ("A",)}))
            assert exc.value.code == ErrorCode.DATA_COVERAGE_INSUFFICIENT
            with pytest.raises(TiveloError):
                reader.extension_values(
                    request.model_copy(update={"effective_at": at(10), "as_of": at(10)})
                )
        portable.prepare(max_bytes=2**20)
        assert portable.extension_values(request) == (first,)


@pytest.mark.parametrize("date_span", [False, True])
def test_legacy_panel_uses_visible_share_revisions_and_risk_warning_names(tmp_path, date_span):
    scopes, references, fields = {}, {}, {}
    for name, kind, unit, initial in (
        ("total_shares", "int", "share", {"int_value": 1_000_000}),
        ("float_shares", "int", "share", {"int_value": 200_000}),
        ("ext_signal", "bool", "flag", {"bool_value": True}),
    ):
        market = name == "ext_signal"
        field = ExtensionField(
            table_id="historical",
            table_version="1",
            field_id=name,
            field_version="1",
            granularity="market" if market else "instrument",
            value_type=kind,
            unit=unit,
        )
        digest = fingerprint(field)
        references[name] = ExtensionFieldRef(dataset_id=name, field_sha256=digest)
        scopes[name] = DatasetScope(
            market_id="cn.equity",
            asset_class="equity",
            instrument_ids=() if market else ("A",),
            start=at(1, 0),
            end=at(9, 0),
            extension_field=field,
        )
        published = at(3, 8) if market else at(1, 1)
        fields[name] = [ExtensionValue(
            field_sha256=digest,
            instrument_id=None if market else "A",
            effective_at=at(1, 1),
            available_at=published,
            ingested_at=published,
            revision_id="1",
            source_id="fixture",
            **initial,
        )]

    updated_at = at(3, 8)
    fields["total_shares"].append(fields["total_shares"][0].model_copy(update={
        "revision_id": "2", "available_at": updated_at, "ingested_at": updated_at,
        "int_value": 2_000_000,
    }))
    fields["float_shares"].append(fields["float_shares"][0].model_copy(update={
        "effective_at": at(4, 0), "valid_until": at(4, 7),
    }))
    original = security()
    st = original.model_copy(update={"available_at": updated_at, "revision_id": "2", "name": "*ST 样本"})
    root = tmp_path / "daily"
    days = (2, 3, 4)
    bars = tuple(bar(day, "10") for day in days)
    cutoff = None
    if date_span:
        bars = tuple(
            bar(day, "10", bar_start=at(day - 1, 16), bar_end=at(day, 16),
                available_at=at(day, 16), ingested_at=at(day, 16))
            for day in days
        )
        cutoff = time(15, tzinfo=timezone(timedelta(hours=8)))
    memory, _ = write_bundle(
        root,
        bars=bars,
        instruments=(original, st),
        scopes=scopes,
        extension_values=fields,
    )
    requested = query(day=8)
    options = dict(extension_fields=references, include_st=True, include_names=True, field_cutoff_time=cutoff)
    with PortableSnapshotReader(root) as reader:
        panel = read_legacy_daily_panel(reader, requested, **options)
    assert panel.equals(read_legacy_daily_panel(memory, requested, **options))
    assert panel["name"].to_list() == [original.name, original.name, st.name]
    assert panel.select("total_shares", "float_shares", "turnover_rate", "is_st", "ext_signal").rows() == [
        (1_000_000, 200_000, 5, 0, None),
        (1_000_000, 200_000, 5, 0, None),
        (2_000_000, None, None, 1, 1),
    ]
    matrix = build_market_data_matrix(panel, field_columns={*references, "is_st", "turnover_rate"})
    assert build_basic_filter_mask(matrix, {
        "market_cap_min": 9_000_000, "float_cap_min": 1_500_000,
        "turnover_min": 4, "exclude_st": True,
    }).ravel().tolist() == [True, True, False]
