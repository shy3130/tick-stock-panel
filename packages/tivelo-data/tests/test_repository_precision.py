"""Physical checks run in the base Data installation, including non-preset symbols."""

import hashlib
import json
from decimal import Decimal

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from pydantic import ValidationError
from tivelo_contracts.errors import ErrorCode, TiveloError

from tivelo_data.cli import main
from tivelo_data.providers import SyncContext, local_context
from tivelo_data.repository_precision import RepositoryPrecisionRequest, audit_repository_precision


def request(**kwargs):
    options = dict(layout="daily_raw_v1", start="2025-07-01", end="2025-07-02", fields=["close"])
    return RepositoryPrecisionRequest.model_validate(options | kwargs)


def shard(root, rows, *, name="a", day="2025-07-01", table="kline_daily"):
    path = root / table / f"date={day}" / f"{name}.parquet"
    path.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(pa.Table.from_pylist(rows), path)
    return path


def test_physical_ordinal_counts_and_bounded_sample_across_files_and_batches(tmp_path):
    good = dict(symbol="000001.SZ", close="1", amount=None, volume="1")
    bad = dict(symbol="920001.BJ", close="2.0000000000001", amount="1e26", volume="1e-13")
    a = shard(tmp_path, [good] * 2050 + [bad])
    b = shard(tmp_path, [dict(bad, close="NaN", amount=None)], name="b")
    options = dict(
        fields=["close", "amount", "volume"], symbols=["920001.BJ"], volume_multiplier="100"
    )
    full = audit_repository_precision(tmp_path, request(**options))
    assert (full.status, full.files, full.checked_rows, full.checked_values) == ("invalid", 2, 2, 5)
    assert (full.affected_rows, full.issue_count, full.truncated) == (2, 3, False)
    assert full.by_field == {"close": 2, "amount": 1}
    assert full.by_reason == {"fractional_precision": 1, "integral_range": 1, "invalid_number": 1}
    assert [(i.symbol, i.row_number, i.field, i.file_sha256) for i in full.issues] == [
        ("920001.BJ", 2051, "close", hashlib.sha256(a.read_bytes()).hexdigest()),
        ("920001.BJ", 2051, "amount", hashlib.sha256(a.read_bytes()).hexdigest()),
        ("920001.BJ", 1, "close", hashlib.sha256(b.read_bytes()).hexdigest()),
    ]
    for limit in (0, 1, 3):
        sample = audit_repository_precision(tmp_path, request(**options, max_issues=limit))
        assert (sample.issues, sample.truncated) == (full.issues[:limit], limit < 3)
        assert (sample.issue_count, sample.by_field, sample.source_sha256) == (
            full.issue_count,
            full.by_field,
            full.source_sha256,
        )


def test_cli_reports_complete_invalid_empty_and_clean_without_creating_engine(tmp_path, capsys):
    root = tmp_path / "legacy"
    table = "kline_daily_enriched"
    source = shard(
        root,
        [dict(symbol="600000.SH", close=Decimal("2.100"), raw_close=Decimal("3.1234567890123"))],
        table=table,
    )
    contents, mtime = source.read_bytes(), source.stat().st_mtime_ns
    config, lake = tmp_path / "request.json", tmp_path / "unused"
    args = [
        "--root",
        str(lake),
        "audit-repository-daily",
        "--repository",
        str(root),
        "--request",
        str(config),
    ]
    for fields, status in ((["close"], "clean"), (["raw_close"], "invalid"), (["amount"], "empty")):
        config.write_text(request(layout="enriched_v1", fields=fields).model_dump_json())
        assert main(args) == int(status != "clean")
        first = capsys.readouterr()
        assert not first.err and json.loads(first.out)["status"] == status
        assert main(args) == int(status != "clean") and capsys.readouterr().out == first.out
    config.write_text(request(layout="enriched_v1", fields=["raw_high"]).model_dump_json())
    assert main(args) == 1
    fatal = capsys.readouterr()
    assert not fatal.out and json.loads(fatal.err)["code"] == "CONTRACT_INVALID"
    assert not lake.exists() and (source.read_bytes(), source.stat().st_mtime_ns) == (
        contents,
        mtime,
    )
    assert (
        audit_repository_precision(
            root, request(layout="enriched_v1", symbols=["000001.SZ"])
        ).status
        == "empty"
    )


@pytest.mark.parametrize("change", ["replace", "add", "cancel"])
def test_audit_rejects_changes_or_cancellation_before_returning_a_report(tmp_path, change):
    row = dict(symbol="600000.SH", close=12)
    shard(tmp_path, [row])
    updated = []

    def progress(*_):
        updated.append(True)
        if change == "replace":
            shard(tmp_path, [dict(row, close=11)])
        if change == "add":
            shard(tmp_path, [row], day="2025-07-02")

    context = SyncContext(
        local_context().resolve_credentials, lambda: change == "cancel" and bool(updated), progress
    )
    with pytest.raises(TiveloError) as failure:
        audit_repository_precision(tmp_path, request(), context=context)
    assert failure.value.code == (
        ErrorCode.CANCELLED if change == "cancel" else ErrorCode.DATA_CONFLICT
    )


@pytest.mark.parametrize("limit", [0, 1])
def test_malformed_source_identifier_is_rejected_independently_of_sampling(tmp_path, limit):
    shard(tmp_path, [dict(symbol="private/path", close="invalid")])
    with pytest.raises(TiveloError) as failure:
        audit_repository_precision(tmp_path, request(max_issues=limit))
    assert failure.value.code == ErrorCode.CONTRACT_INVALID and "private/path" not in str(
        failure.value
    )


@pytest.mark.parametrize(
    "options",
    [
        {"fields": []},
        {"fields": ["close", "close"]},
        {"fields": ["other"]},
        {"symbols": ["000001.SZ", "000001.SZ"]},
        {"end": "2025-06-30"},
        {"volume_multiplier": "0"},
    ],
)
def test_invalid_selections_are_rejected(options):
    with pytest.raises(ValidationError):
        request(**options)


def test_unit_conversion_preserves_nonzero_digits_and_extreme_exponents(tmp_path):
    volumes = ["1e-13", "1." + "0" * 90 + "1", "1e1000000", "1e-1000000", "-9e26", "0"]
    shard(tmp_path, [dict(symbol="600000.SH", volume=value) for value in volumes])
    audit = audit_repository_precision(
        tmp_path, request(fields=["volume"], volume_multiplier="100")
    )
    assert [(i.row_number, i.reason) for i in audit.issues] == [
        (2, "fractional_precision"),
        (3, "integral_range"),
        (4, "fractional_precision"),
        (5, "integral_range"),
    ]
