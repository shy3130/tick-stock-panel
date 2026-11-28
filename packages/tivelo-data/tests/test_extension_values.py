import json

import pytest
from pydantic import ValidationError
from tivelo_contracts.base import canonical_bytes, fingerprint
from tivelo_contracts.errors import ErrorCode, TiveloError
from tivelo_contracts.extension_fields import ExtensionField, ExtensionQuery, ExtensionValue

from tivelo_data.engine import DataEngine
from tivelo_data.jsonl import JSONLinesProvider
from tivelo_data.providers import FixedSourceResolver, SyncRequest


def field(**changes):
    return ExtensionField(
        **{
            "table_id": "ranks",
            "table_version": "1",
            "field_id": "rank",
            "field_version": "1",
            "value_type": "int",
            "unit": "rank",
            "granularity": "instrument",
            **changes,
        }
    )


def at(hour):
    return f"2025-01-02T{hour:02d}:00:00Z"


def value(descriptor, observed, rank, **changes):
    return ExtensionValue(
        **{
            "field_sha256": fingerprint(descriptor),
            "instrument_id": "A",
            "effective_at": at(1),
            "available_at": at(observed),
            "ingested_at": at(observed),
            "source_id": "tsp.ranks",
            "revision_id": str(observed),
            "int_value": rank,
            **changes,
        }
    )


def source(tmp_path, descriptor, records, **overrides):
    request = SyncRequest(
        **{
            "record_type": "extension_value",
            "dataset_id": "ranking",
            "market_id": "cn.equity",
            "asset_class": "equity",
            "instrument_ids": ("A",) if descriptor.granularity == "instrument" else (),
            "start": at(0),
            "end": at(23),
            "extension_field": descriptor,
            **overrides,
        }
    )
    path = tmp_path / "values.jsonl"
    path.write_bytes(b"".join(canonical_bytes(row) + b"\n" for row in records))
    provider = JSONLinesProvider(
        path,
        request,
        source_id="tsp.ranks",
        knowledge_basis="historical_versions",
        bar_convention_id=None,
    )
    engine = DataEngine(
        tmp_path / "store", FixedSourceResolver(provider, provider.configuration_digest)
    )
    version = engine.publish(engine.sync(engine.plan_sync(request)))
    return engine, engine.create_snapshot((version.version_id,))


def query(descriptor, hour, **changes):
    return ExtensionQuery(
        **{
            "dataset_id": "ranking",
            "field_sha256": fingerprint(descriptor),
            "instrument_ids": ("A",) if descriptor.granularity == "instrument" else (),
            "effective_at": at(hour),
            "as_of": at(hour),
            **changes,
        }
    )


def test_rank_revision_is_causal_and_old_snapshot_survives(tmp_path):
    descriptor = field()
    a = value(descriptor, 6, 8)
    b = value(descriptor, 10, 2)
    engine, old = source(tmp_path, descriptor, (a,))
    _, new = source(tmp_path, descriptor, (a, b))
    assert old.snapshot_id != new.snapshot_id
    with engine.open_reader(new.snapshot_id) as reader:
        assert reader.extension_values(query(descriptor, 5)) == ()
        assert reader.extension_values(query(descriptor, 9)) == (a,)
        assert reader.extension_values(query(descriptor, 10)) == (b,)
        with pytest.raises(TiveloError, match="field"):
            reader.extension_values(query(field(unit="percent"), 10))
    with engine.open_reader(old.snapshot_id) as reader:
        assert reader.extension_values(query(descriptor, 10)) == (a,)


def test_market_observation_and_expiry_do_not_revive_older_value(tmp_path):
    descriptor = field(granularity="market", value_type="bool", unit="flag")
    first = value(descriptor, 2, None, instrument_id=None, bool_value=False)
    later = value(
        descriptor,
        4,
        None,
        instrument_id=None,
        effective_at=at(3),
        valid_until=at(8),
        bool_value=True,
    )
    engine, snap = source(tmp_path, descriptor, (first, later))
    with engine.open_reader(snap.snapshot_id) as reader:
        assert reader.datasets[0].contents.coverage.completed_units == 1
        assert reader.extension_values(query(descriptor, 5)) == (later,)
        assert reader.extension_values(query(descriptor, 8)) == ()


def test_ingest_rejects_wrong_type_grain_and_changed_definition(tmp_path):
    descriptor = field()
    for row in (
        value(descriptor, 2, None, text_value="2"),
        value(descriptor, 2, 1, instrument_id=None),
        value(field(unit="percent"), 2, 1),
        value(descriptor, 2, 1, effective_at=at(23)),
    ):
        with pytest.raises(TiveloError) as exc:
            source(tmp_path, descriptor, (row,))
        assert exc.value.code == ErrorCode.CONTRACT_INVALID
    with pytest.raises(ValidationError):
        value(descriptor, 2, 0, bool_value=False)
    with pytest.raises(ValidationError):
        query(descriptor, 3, as_of=at(2))
    assert "extension_field" not in json.loads(
        canonical_bytes(
            SyncRequest(
                dataset_id="old",
                record_type="instrument",
                market_id="cn.equity",
                asset_class="equity",
                instrument_ids=("A",),
                start=at(0),
                end=at(23),
            )
        )
    )
