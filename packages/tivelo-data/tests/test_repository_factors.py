import hashlib
import json
import subprocess
import sys
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal

import polars as pl
import pytest
from pydantic import ValidationError
from tivelo_contracts.errors import ErrorCode, TiveloError

from tivelo_data.adapters.cn_daily import CN, RepositoryFactorRequest
from tivelo_data.providers import SyncContext, local_context
from tivelo_data.repository_factors import DEFINITIONS, RepositoryFactorProvider

DAY = date(2025, 7, 1)
A, B = "600000.SH", "000001.SZ"


def stamp(day):
    return datetime.combine(day, time.min, tzinfo=CN)


def spec(mode="cumulative_forward", start=DAY, end=DAY + timedelta(days=3), symbols=(A,)):
    ids = tuple(sorted(symbols))
    venues = {"SH": "XSHG", "SZ": "XSHE", "BJ": "XBSE"}
    return RepositoryFactorRequest(
        layout="event_factors_v1",
        representation=mode,
        baseline_date=DAY if mode == "cumulative_forward" else None,
        expected_provider="archive",
        symbols=[
            {"instrument_id": sym, "symbol": sym, "venue_id": venues[sym[-2:]]} for sym in ids
        ],
        request={
            "dataset_id": "factors",
            "record_type": "adjustment_factor",
            "market_id": "cn.equity",
            "asset_class": "equity",
            "instrument_ids": ids,
            "start": stamp(start),
            "end": stamp(end),
        },
    )


def write(root, rows, *, row_group_size=None):
    path = root / "adj_factor" / "all.parquet"
    path.parent.mkdir(parents=True, exist_ok=True)
    pl.DataFrame(
        rows,
        orient="row",
        schema={"symbol": pl.String, "trade_date": pl.Date, "ex_factor": pl.Float64},
    ).write_parquet(path, row_group_size=row_group_size)
    return path


def read(root, source):
    provider = RepositoryFactorProvider(root, source)
    ctx = local_context()
    try:
        provider.check_ready(ctx)
        page = provider.fetch(source.request, None, ctx)
        assert page.next_cursor is None
        return page
    finally:
        provider.close()


def test_locked_cli_and_historical_visibility(tmp_path):
    root = tmp_path / "v1"
    archive = write(
        root, [(A, DAY + timedelta(days=2), 1.25), (A, DAY, 1.5), (A, DAY + timedelta(days=1), 2)]
    )
    data = tmp_path / "data"
    config = tmp_path / "capture.json"
    config.write_text(spec().model_dump_json())
    prefix = [sys.executable, "-I", "-m", "tivelo_data.cli", "--root", str(data)]
    options = ["--capture", str(config), "--repository", str(root)]
    plan = subprocess.check_output([*prefix, "plan-repository", *options])
    assert b"latest_reconstruction" in plan and str(root).encode() not in plan
    assert json.loads(plan)["provider"]["version"] == "1"
    assert not data.exists()
    locked = tmp_path / "plan.json"
    locked.write_bytes(plan)
    import_cmd = [*prefix, "import-repository", *options, "--locked-plan", str(locked)]
    old_bytes = archive.read_bytes()
    before = datetime.now(UTC)
    snapshot = json.loads(subprocess.check_output(import_cmd))["snapshot_id"]
    after = datetime.now(UTC)
    assert archive.read_bytes() == old_bytes
    query = tmp_path / "query.json"
    query_cmd = [
        *prefix,
        "query",
        "--record-type",
        "adjustment_factor",
        "--snapshot",
        snapshot,
        "--request",
        str(query),
    ]

    def query_at(when):
        query.write_text(
            json.dumps(
                {
                    "instrument_ids": [A],
                    "effective_at": spec().request.end.isoformat(),
                    "as_of": when.isoformat(),
                }
            )
        )
        return [json.loads(row) for row in subprocess.check_output(query_cmd).splitlines()]

    assert query_at(before) == []
    rows = query_at(after)
    assert [r["value"] for r in rows] == ["1", "2", "2.5"]
    assert {r["baseline_id"] for r in rows} == {"v1:archive:2025-07-01"}
    assert {r["definition_id"] for r in rows} == {DEFINITIONS["cumulative_forward"]}
    assert len({r["available_at"] for r in rows}) == 1
    write(root, [(A, DAY, 3)])
    failed = subprocess.run(import_cmd, capture_output=True)
    assert failed.returncode == 1 and json.loads(failed.stderr)["code"] == "DATA_CONFLICT"
    assert query_at(after) == rows


def test_fixed_baseline_events_duplicates_warmup_and_coverage(tmp_path):
    tomorrow = DAY + timedelta(days=1)
    last = DAY + timedelta(days=2)
    write(
        tmp_path,
        [
            (A, last, 1.25),
            (A, DAY, 3),
            (A, tomorrow, 2),
            (A, tomorrow, 2),
            (B, last + timedelta(days=1), 1.5),
            (A, DAY - timedelta(days=20), 4),
        ],
    )
    p = read(tmp_path, spec(symbols=(A, B)))
    assert [(v.effective_at, v.value) for v in p.records] == [
        (stamp(DAY), 1),
        (stamp(tomorrow), 2),
        (stamp(last), Decimal("2.5")),
    ]
    assert (p.coverage.status, p.coverage.completed_units, p.coverage.row_count) == (
        "partial",
        1,
        3,
    )
    assert [(v.instrument_id, v.reason) for v in p.coverage.issues] == [(B, "missing")]
    narrow = read(tmp_path, spec(start=tomorrow))
    assert [(v.effective_at, v.value, v.baseline_id) for v in narrow.records] == [
        (v.effective_at, v.value, v.baseline_id) for v in p.records[1:]
    ]
    event = read(tmp_path, spec("event_ratio", start=tomorrow))
    assert event.coverage.status == "complete"
    assert [(v.effective_at, v.value) for v in event.records] == [
        (stamp(tomorrow), 2),
        (stamp(last), Decimal("1.25")),
    ]
    assert all(
        v.factor_kind == "event_ratio" and v.definition_id == DEFINITIONS["event_ratio"]
        for v in event.records
    )
    missing = read(tmp_path, spec(symbols=(B,)))
    assert missing.coverage.status == "empty" and missing.records == ()
    # Previous evidence can carry forward, even in a window containing no event.
    later = last + timedelta(days=3)
    assert read(tmp_path, spec(start=later, end=later + timedelta(days=1))).records[
        0
    ].value == Decimal("2.5")
    assert (
        read(
            tmp_path, spec("event_ratio", start=later, end=later + timedelta(days=1))
        ).coverage.status
        == "empty"
    )


def test_unrounded_running_product_and_raw_fact_precision(tmp_path):
    ratio = 1.2345678901234567
    write(
        tmp_path,
        [(A, DAY + timedelta(days=offset), value) for offset, value in [(1, ratio), (2, 1e7)]],
    )
    products = pl.Series([ratio, 1e7]).cum_prod().to_list()
    expected = [Decimal.from_float(v).quantize(Decimal("1e-12")) for v in products]
    assert [v.value for v in read(tmp_path, spec()).records] == [1, *expected]
    with pytest.raises(TiveloError) as error:
        read(tmp_path, spec("event_ratio"))
    assert error.value.code == ErrorCode.CONTRACT_INVALID


def test_fixed_baseline_needs_valid_history_without_using_earlier_bad_ratios(tmp_path):
    north = "920001.BJ"
    old = DAY - timedelta(days=10)
    upcoming = DAY + timedelta(days=1)
    write(tmp_path, [(north, old, 0.0), (north, upcoming, 2.0), (B, old, 0.0)])
    page = read(tmp_path, spec(symbols=(B, north)))
    assert [(row.instrument_id, row.effective_at, row.value) for row in page.records] == [
        (north, stamp(DAY), 1),
        (north, stamp(upcoming), 2),
    ]
    assert page.coverage.status == "partial"
    assert [(issue.instrument_id, issue.reason) for issue in page.coverage.issues] == [
        (B, "missing")
    ]
    with pytest.raises(TiveloError) as raw:
        read(tmp_path, spec("event_ratio", start=old, symbols=(north,)))
    assert raw.value.code == ErrorCode.CONTRACT_INVALID
    earlier = spec(symbols=(north,)).model_copy(update={"baseline_date": old})
    with pytest.raises(TiveloError) as cumulative:
        read(tmp_path, earlier)
    assert cumulative.value.code == ErrorCode.CONTRACT_INVALID


@pytest.mark.parametrize(
    "bad,reason", [(1.123456789012345, "fractional_precision"), (1e27, "integral_range")]
)
def test_raw_import_locates_original_archive_row_before_filter_and_sort(tmp_path, bad, reason):
    first, second = DAY + timedelta(days=1), DAY + timedelta(days=2)
    # Cross several row groups, filtered securities, an excluded date and an identical duplicate.
    source = tmp_path / "old"
    path = write(
        source,
        [(B, DAY, 2)] * 2048
        + [(A, DAY - timedelta(days=1), bad), (A, second, 2), (A, first, bad), (A, first, bad)],
        row_group_size=384,
    )
    data = tmp_path / "lake"
    capture = tmp_path / "capture.json"
    capture.write_text(spec("event_ratio").model_dump_json())
    result = subprocess.run(
        [
            sys.executable,
            "-I",
            "-m",
            "tivelo_data.cli",
            "--root",
            str(data),
            "import-repository",
            "--repository",
            str(source),
            "--capture",
            str(capture),
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 1 and not result.stdout
    error = json.loads(result.stderr)
    assert error["code"] == "CONTRACT_INVALID"
    assert error["source_issues"] == [
        {
            "layout": "event_factors_v1",
            "trading_date": first.isoformat(),
            "symbol": A,
            "field": "ex_factor",
            "reason": reason,
            "row_number": 2051,
            "file_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        }
    ]
    assert not list((data / "staging").iterdir())


def test_decimal256_archive_preserves_digits_before_raw_validation(tmp_path):
    import pyarrow as pa
    import pyarrow.parquet as pq

    first = Decimal("9007199254740993.125")
    second = Decimal("1.0000000000000000000000000000000000001")
    path = tmp_path / "adj_factor/all.parquet"
    path.parent.mkdir()
    table = pa.table(
        {
            "ex_factor": pa.array([first, second], type=pa.decimal256(64, 38)),
            "trade_date": [DAY, DAY + timedelta(days=1)],
            "symbol": [A, A],
        }
    )
    pq.write_table(table, path, row_group_size=1)
    provider = RepositoryFactorProvider(tmp_path, spec("event_ratio"))
    assert provider.describe().version == "2"
    provider.close()
    actual = read(tmp_path, spec("event_ratio", end=DAY + timedelta(days=1)))
    assert [row.value for row in actual.records] == [first]
    with pytest.raises(TiveloError) as error:
        read(tmp_path, spec("event_ratio"))
    (issue,) = error.value.issues
    assert (issue.row_number, issue.reason, issue.file_sha256) == (
        2,
        "fractional_precision",
        hashlib.sha256(path.read_bytes()).hexdigest(),
    )
    assert [row.value for row in read(tmp_path, spec()).records] == [1, 1]
    # Distinct source ratios can have the same Float64 value.
    conflicting = table.set_column(
        0, "ex_factor", pa.array([Decimal(1), second], type=pa.decimal256(64, 38))
    )
    pq.write_table(conflicting.set_column(1, "trade_date", pa.array([DAY, DAY])), path)
    with pytest.raises(TiveloError) as conflict:
        read(tmp_path, spec())
    assert conflict.value.code == ErrorCode.DATA_CONFLICT


@pytest.mark.parametrize(
    "rows,code",
    [
        ([(A, DAY, 2), (A, DAY, 3)], ErrorCode.DATA_CONFLICT),
        ([(A, None, 2)], ErrorCode.CONTRACT_INVALID),
        ([(A, DAY, 0)], ErrorCode.CONTRACT_INVALID),
        ([(A, DAY, float("nan"))], ErrorCode.CONTRACT_INVALID),
        ([(A, DAY + timedelta(days=1), 1e-14)], ErrorCode.CONTRACT_INVALID),
        ([(A, DAY + timedelta(days=1), 1e27)], ErrorCode.CONTRACT_INVALID),
    ],
)
def test_invalid_observations(tmp_path, rows, code):
    write(tmp_path, rows)
    with pytest.raises(TiveloError) as error:
        read(tmp_path, spec())
    assert error.value.code == code


def test_missing_file_empty_file_schema_and_date_bounds(tmp_path):
    with pytest.raises(TiveloError) as error:
        read(tmp_path, spec())
    assert error.value.code == ErrorCode.SOURCE_UNAVAILABLE
    path = write(tmp_path, [])
    assert read(tmp_path, spec()).coverage.status == "empty"
    pl.DataFrame({"symbol": [A], "trade_date": [DAY], "ex_factor": [True]}).write_parquet(path)
    with pytest.raises(TiveloError) as error:
        read(tmp_path, spec())
    assert error.value.code == ErrorCode.CONTRACT_INVALID
    data = spec().model_dump()
    for overrides in (
        dict(baseline_date=None),
        dict(baseline_date=DAY + timedelta(days=1)),
        dict(representation="event_ratio"),
        dict(request={**data["request"], "start": "2025-07-01T01:00:00+08:00"}),
    ):
        with pytest.raises(ValidationError):
            RepositoryFactorRequest.model_validate({**data, **overrides})
    future = datetime.now(CN).date() + timedelta(days=2)
    future_scope = spec(start=future, end=future + timedelta(days=2))
    with pytest.raises(TiveloError) as error:
        read(tmp_path, future_scope)
    assert error.value.code == ErrorCode.FUTURE_INFORMATION


def test_paginated_source_replacement_and_cancellation(tmp_path):
    start = date(2010, 1, 1)
    days = 2050
    rows = [(A, start + timedelta(days=n), 1.0) for n in range(days)]
    write(tmp_path, rows[::-1])
    source = spec("event_ratio", start=start, end=start + timedelta(days=days))
    provider = RepositoryFactorProvider(tmp_path, source)
    ctx = local_context()
    try:
        provider.check_ready(ctx)
        p = provider.fetch(source.request, None, ctx)
        assert p.next_cursor == "2048" and p.coverage is None and len(p.records) == 2048
        with pytest.raises(TiveloError) as error:
            provider.fetch(
                source.request,
                p.next_cursor,
                SyncContext(ctx.resolve_credentials, lambda: True, ctx.progress),
            )
        assert error.value.code == ErrorCode.CANCELLED
        tail = provider.fetch(source.request, p.next_cursor, ctx)
        assert len(tail.records) == 2 and tail.coverage.row_count == days
        provider.check_ready(ctx)
        p = provider.fetch(source.request, None, ctx)
        write(tmp_path, rows[:2])
        with pytest.raises(TiveloError) as error:
            provider.fetch(source.request, p.next_cursor, ctx)
        assert error.value.code == ErrorCode.DATA_CONFLICT
    finally:
        provider.close()
