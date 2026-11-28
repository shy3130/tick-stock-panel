"""A real legacy file layout, persisted v2 data and its installed CLI."""

import hashlib
import json
import subprocess
import sys
from datetime import UTC, date, datetime
from decimal import Decimal

import polars as pl
import pytest
from tivelo_contracts.data import BarQuery
from tivelo_contracts.errors import ErrorCode, TiveloError

from tivelo_data import DataEngine
from tivelo_data.adapters.cn_daily import RepositoryDailyRequest
from tivelo_data.cli import main
from tivelo_data.providers import FixedSourceResolver, SyncContext, local_context
from tivelo_data.repository_daily import BAR_CONVENTION, TABLES, RepositoryDailyProvider

DAY = date(2025, 7, 1)
NEXT = date(2025, 7, 2)
ROW = dict(
    symbol="600000.SH",
    date=DAY,
    open=11.0,
    high=12.0,
    low=10.0,
    close=11.5,
    volume=12.5,
    amount=14375.0,
)


def capture(layout="daily_raw_v1", basis="raw", *, symbol="600000.SH", venue="XSHG"):
    pytest.importorskip("tivelo_market_cn")
    return RepositoryDailyRequest(
        expected_provider="v1_desk",
        calendar_version="2026.2.0" if venue == "XBSE" else "2026.1.0",
        layout=layout,
        symbols=[{"instrument_id": "security-A", "symbol": symbol, "venue_id": venue}],
        request={
            "dataset_id": "daily",
            "market_id": "cn.equity",
            "asset_class": "equity",
            "instrument_ids": ["security-A"],
            "timeframe": "1d",
            "record_type": "bar",
            "price_basis": basis,
            "start": "2025-06-30T16:00:00Z",
            "end": "2025-07-02T16:00:00Z",
        },
    )


def write(root, spec, day, rows, filename="part.parquet"):
    file = root / TABLES[spec.layout] / f"date={day}" / filename
    file.parent.mkdir(parents=True, exist_ok=True)
    pl.DataFrame(rows).write_parquet(file)
    return file


def engine_for(tmp_path, spec):
    provider = RepositoryDailyProvider(tmp_path / "legacy", spec)
    engine = DataEngine(
        tmp_path / "new", FixedSourceResolver(provider, provider.configuration_digest)
    )
    return engine, engine.plan_sync(spec.request)


@pytest.mark.parametrize("basis", ["raw", "qfq"])
def test_console_imports_available_partitions_outside_calendar_coverage(tmp_path, capsys, basis):
    first, later = date(2019, 1, 2), date(2019, 1, 4)
    layout = "enriched_v1" if basis == "qfq" else "daily_raw_v1"
    spec = RepositoryDailyRequest(
        expected_provider="archive",
        layout=layout,
        symbols=[{"instrument_id": "seed", "symbol": ROW["symbol"], "venue_id": "XSHG"}],
        request={
            "dataset_id": "daily",
            "market_id": "cn.equity",
            "asset_class": "equity",
            "instrument_ids": ["seed"],
            "timeframe": "1d",
            "record_type": "bar",
            "price_basis": basis,
            "start": "2019-01-02T00:00:00+08:00",
            "end": "2019-01-05T00:00:00+08:00",
        },
    )
    source = tmp_path / "legacy"
    row = dict(ROW, high=12.1234567890126, close=11.1234567890125) if basis == "qfq" else ROW
    originals = {}
    for day in (first, later):
        file = write(source, spec, day, [dict(row, date=day)])
        originals[file] = file.read_bytes()
    config = tmp_path / "capture.json"
    config.write_text(spec.model_dump_json(), encoding="utf-8")
    prefix = ["--root", str(tmp_path / "new")]
    args = ["--capture", str(config), "--repository", str(source)]
    assert main([*prefix, "plan-repository", *args]) == 0
    plan = tmp_path / "plan.json"
    plan.write_text(capsys.readouterr().out)
    assert main([*prefix, "import-repository", *args, "--locked-plan", str(plan)]) == 0
    snapshot = json.loads(capsys.readouterr().out)["snapshot_id"]
    with DataEngine(tmp_path / "new").open_reader(snapshot) as reader:
        (dataset,) = reader.datasets
        assert dataset.contents.coverage.completed_units == 2
        assert dataset.contents.coverage.requested_units == 2
        query = BarQuery(
            instrument_ids=("seed",),
            timeframe="1d",
            price_basis=basis,
            start=spec.request.start,
            end=spec.request.end,
            as_of=datetime.now(UTC),
        )
        bars = reader.bars(query)
        assert [bar.trading_date for bar in bars] == [first, later]
        expected = (
            (Decimal("12.123456789013"), Decimal("11.123456789012"))
            if basis == "qfq"
            else (Decimal("12"), Decimal("11.5"))
        )
        assert [(bar.high, bar.close, bar.volume, bar.turnover) for bar in bars] == [
            (*expected, 1250, 14375)
        ] * 2
    assert all(file.read_bytes() == raw for file, raw in originals.items())

    missing = spec.request.model_copy(
        update={
            "start": datetime(2018, 1, 1, 16, tzinfo=UTC),
            "end": datetime(2018, 1, 2, 16, tzinfo=UTC),
        }
    )
    provider = RepositoryDailyProvider(source, spec.model_copy(update={"request": missing}))
    context = local_context()
    provider.check_ready(context)
    page = provider.fetch(missing, None, context)
    assert page.coverage.status == "empty"
    assert page.coverage.issues[0].reason == "missing"


@pytest.mark.parametrize(
    "layout,basis,symbol,venue",
    [
        ("daily_raw_v1", "raw", "600000.SH", "XSHG"),
        ("enriched_v1", "qfq", "600000.SH", "XSHG"),
        ("daily_raw_v1", "raw", "920001.BJ", "XBSE"),
    ],
)
def test_real_console_locks_and_reads_old_partitions_without_modifying_them(
    tmp_path, layout, basis, symbol, venue
):
    spec = capture(layout, basis, symbol=symbol, venue=venue)
    src = tmp_path / "legacy"
    observed = dict(ROW, symbol=symbol)
    file = write(src, spec, DAY, [observed, dict(ROW, symbol="600001.SH", close=10)])
    stamp, original = file.stat().st_mtime_ns, file.read_bytes()
    config = tmp_path / "capture.json"
    config.write_text(spec.model_dump_json())
    store = tmp_path / "new"
    prefix = [sys.executable, "-I", "-m", "tivelo_data.cli", "--root", str(store)]
    opts = ["--capture", str(config), "--repository", str(src)]
    pin = tmp_path / "plan.json"
    plan = subprocess.run([*prefix, "plan-repository", *opts], capture_output=True, check=True)
    pin.write_bytes(plan.stdout)
    assert not store.exists() and str(src).encode() not in plan.stdout

    def run():
        return subprocess.run(
            [*prefix, "import-repository", *opts, "--locked-plan", str(pin)], capture_output=True
        )

    result = run()
    assert result.returncode == 0, result.stderr
    snapshot = json.loads(result.stdout)["snapshot_id"]
    with DataEngine(store).open_reader(snapshot) as reader:
        (data,) = reader.datasets
        assert data.contents.knowledge_basis == "latest_reconstruction"
        assert data.contents.scope.bar_convention_id == BAR_CONVENTION
        cov = data.contents.coverage
        assert (cov.status, cov.requested_units, cov.completed_units) == ("partial", 2, 1)
        query = BarQuery(
            price_basis=basis,
            instrument_ids=("security-A",),
            timeframe="1d",
            start=spec.request.start,
            end=spec.request.end,
            as_of=datetime.now(UTC),
        )
        (bar,) = reader.bars(query)
        assert (bar.close, bar.volume, bar.turnover, bar.price_basis) == (
            Decimal("11.5"),
            1250,
            14375,
            basis,
        )
        assert (bar.bar_start, bar.bar_end) == (
            datetime(2025, 6, 30, 16, tzinfo=UTC),
            datetime(2025, 7, 1, 16, tzinfo=UTC),
        )
        assert bar.ingested_at == bar.available_at and not reader.bars(
            query.model_copy(update={"as_of": query.end})
        )
    assert file.read_bytes() == original and file.stat().st_mtime_ns == stamp
    assert [p for p in src.rglob("*") if p.is_file()] == [file]
    write(src, spec, NEXT, [dict(observed, date=NEXT)])
    stale = run()
    assert stale.returncode == 1 and json.loads(stale.stderr)["code"] == "DATA_CONFLICT"


@pytest.mark.parametrize(
    "fault,code",
    [
        ("intraday", ErrorCode.DATA_COVERAGE_INSUFFICIENT),
        ("wrong_quote_day", ErrorCode.DATA_COVERAGE_INSUFFICIENT),
        ("date", ErrorCode.CONTRACT_INVALID),
        ("bad_ohlc", ErrorCode.CONTRACT_INVALID),
        ("duplicate", ErrorCode.DATA_CONFLICT),
        ("missing_raw", ErrorCode.RAW_PRICE_REQUIRED),
        ("raw_as_qfq", ErrorCode.FACTOR_INCOMPATIBLE),
        ("corrupt", ErrorCode.CONTRACT_INVALID),
    ],
)
def test_invalid_partitions_never_publish(tmp_path, fault, code):
    spec = capture(
        "enriched_v1" if fault == "missing_raw" else "daily_raw_v1",
        "qfq" if fault == "raw_as_qfq" else "raw",
    )
    root = tmp_path / "legacy"
    row = dict(ROW)
    if fault in ("intraday", "wrong_quote_day"):
        row["quote_ts"] = int(
            datetime(2025, 7, 1 if fault == "intraday" else 2, 7, tzinfo=UTC).timestamp() * 1000
        )
    if fault == "date":
        row["date"] = NEXT
    if fault == "bad_ohlc":
        row["low"] = 20
    file = write(root, spec, DAY, [row])
    if fault == "duplicate":
        write(root, spec, DAY, [row], "second.parquet")
    if fault == "corrupt":
        file.write_bytes(b"invalid")
    with pytest.raises(TiveloError) as error:
        engine, plan = engine_for(tmp_path, spec)
        engine.sync(plan)
    assert error.value.code == code
    if fault in ("intraday", "wrong_quote_day"):
        (issue,) = error.value.issues
        assert (issue.field, issue.row_number, issue.symbol, issue.file_sha256) == (
            "quote_ts",
            1,
            ROW["symbol"],
            hashlib.sha256(file.read_bytes()).hexdigest(),
        )
        assert (
            issue.reason
            == {"intraday": "quote_before_finalization", "wrong_quote_day": "quote_date_mismatch"}[
                fault
            ]
        )
    assert not list((tmp_path / "new" / "staging").glob("*"))


def test_clock_regression_after_session_selection_preserves_quote_location(tmp_path):
    spec = capture()
    quote = datetime(2025, 7, 1, 8, tzinfo=UTC)
    write(
        tmp_path / "legacy",
        spec,
        DAY,
        [dict(ROW, quote_ts=int(quote.timestamp() * 1000), close=11.123456789012345)],
    )
    source = RepositoryDailyProvider(tmp_path / "legacy", spec)
    # Session selection first sees a completed range. The observation clock
    # subsequently moves backwards past the selected row's quote timestamp.
    readings = iter((datetime(2025, 7, 3, 16, tzinfo=UTC), quote.replace(hour=7)))
    source._now = readings.__next__
    engine = DataEngine(tmp_path / "lake", FixedSourceResolver(source, source.configuration_digest))
    with pytest.raises(TiveloError) as failure:
        engine.sync(engine.plan_sync(spec.request))
    assert failure.value.code == ErrorCode.DATA_COVERAGE_INSUFFICIENT
    assert [(i.field, i.reason) for i in failure.value.issues] == [
        ("quote_ts", "quote_after_observation")
    ]


@pytest.mark.parametrize("change", ["replace", "addition", "generation", "repair", "cancel"])
def test_source_must_remain_stable_until_observation_is_sealed(tmp_path, change):
    spec = capture("enriched_v1", "qfq")
    root = tmp_path / "legacy"
    write(root, spec, DAY, [ROW])
    marker = root / ".matrix_generation_stock.json"
    marker.write_text(json.dumps({"generation": "old"}))
    engine, plan = engine_for(tmp_path, spec)
    calls = []

    def progress(*_):
        calls.append(1)
        if change == "replace":
            write(root, spec, DAY, [dict(ROW, close=10)])
        if change == "addition":
            write(root, spec, NEXT, [dict(ROW, date=NEXT)])
        if change == "generation":
            marker.write_text(json.dumps({"state": "publishing", "generation": "new"}))
        if change == "repair":
            (root / "user_data").mkdir()
            (root / "user_data" / "daily_integrity_state.json").write_text(
                json.dumps(
                    {
                        "version": 1,
                        "entries": [
                            {
                                "asset_type": "stock",
                                "day": DAY.isoformat(),
                                "symbol": ROW["symbol"],
                                "needs_raw": True,
                                "kind": "corrupt",
                                "reason": "synthetic",
                            }
                        ],
                    }
                )
            )

    with pytest.raises(TiveloError) as error:
        engine.sync(
            plan,
            SyncContext(
                local_context().resolve_credentials,
                lambda: change == "cancel" and bool(calls),
                progress,
            ),
        )
    assert error.value.code == (
        ErrorCode.CANCELLED if change == "cancel" else ErrorCode.DATA_CONFLICT
    )
    assert calls == [1] and not list((tmp_path / "new" / "staging").glob("*"))
    if change == "generation":
        assert json.loads(marker.read_text())["state"] == "publishing"


def test_complete_original_price_columns_are_used_only_when_available(tmp_path):
    spec = capture("enriched_v1", "raw")
    row = dict(ROW, raw_open=2.0, raw_high=3.0, raw_low=1.0, raw_close=2.5)
    root = tmp_path / "legacy"
    write(root, spec, DAY, [row])
    write(root, spec, NEXT, [dict(row, date=NEXT, volume=0.0, amount=0.0)])
    engine, plan = engine_for(tmp_path, spec)
    version = engine.publish(engine.sync(plan))
    assert version.contents.coverage.row_count == 1
    snap = engine.create_snapshot((version.version_id,))
    with engine.open_reader(snap.snapshot_id) as reader:
        (bar,) = reader.bars(
            BarQuery(
                instrument_ids=("security-A",),
                timeframe="1d",
                start=spec.request.start,
                end=spec.request.end,
                as_of=datetime.now(UTC),
            )
        )
        assert (bar.open, bar.close) == (Decimal(2), Decimal("2.5"))


def test_locked_content_is_checked_even_when_file_stat_does_not_distinguish_edits(
    tmp_path, monkeypatch
):
    from tivelo_data import repository_daily

    # Simulate a filesystem reporting indistinguishable stat metadata.
    monkeypatch.setattr(repository_daily, "_identity", lambda info: (0,))
    spec = capture()
    root = tmp_path / "legacy"
    write(root, spec, DAY, [ROW])
    engine, plan = engine_for(tmp_path, spec)
    write(root, spec, DAY, [dict(ROW, close=10.0)])
    with pytest.raises(TiveloError) as error:
        engine.sync(plan)
    assert error.value.code == ErrorCode.DATA_CONFLICT
    assert not list((tmp_path / "new" / "staging").glob("*"))


@pytest.mark.parametrize("layout,prefix", [("daily_raw_v1", ""), ("enriched_v1", "raw_")])
def test_repaired_daily_requires_new_plan_and_keeps_previous_snapshot(
    tmp_path, capsys, layout, prefix
):
    spec = capture(layout)
    src, dest = tmp_path / "source", tmp_path / "new"
    config, pin = tmp_path / "capture.json", tmp_path / "plan.json"
    config.write_text(spec.model_dump_json())
    flags = ["--repository", str(src), "--capture", str(config)]

    def plan():
        assert main(["--root", str(dest), "plan-repository", *flags]) == 0
        pin.write_text(capsys.readouterr().out)

    def ingest(expected_code):
        assert (
            main(["--root", str(dest), "import-repository", *flags, "--locked-plan", str(pin)])
            == expected_code
        )
        result = capsys.readouterr()
        return json.loads(result.err if expected_code else result.out)

    good = dict(
        ROW, **({f"raw_{c}": ROW[c] for c in ("open", "high", "low", "close")} if prefix else {})
    )
    write(src, spec, DAY, [good])
    plan()
    old = ingest(0)["snapshot_id"]
    previous_stages = set((dest / "staging").iterdir())
    bad = good | {prefix + "close": 11.123456789012345, "amount": 1e27}
    file = write(src, spec, DAY, [dict(good, symbol="000001.SZ")] * 2050 + [bad])
    plan()
    failure = ingest(1)
    assert failure == ingest(1) and failure["code"] == "CONTRACT_INVALID"
    assert [(i["field"], i["reason"]) for i in failure["source_issues"]] == [
        (prefix + "close", "fractional_precision"),
        ("amount", "integral_range"),
    ]
    for issue in failure["source_issues"]:
        assert (
            issue["row_number"],
            issue["symbol"],
            issue["trading_date"],
            issue["file_sha256"],
        ) == (
            2051,
            ROW["symbol"],
            DAY.isoformat(),
            hashlib.sha256(file.read_bytes()).hexdigest(),
        )
    assert str(src) not in json.dumps(failure) and "11.123456" not in json.dumps(failure)
    store = DataEngine(dest)
    with store.store.connection() as db:
        assert db.execute("SELECT snapshot_id FROM snapshots").fetchall() == [(old,)]
        assert db.execute("SELECT count(*) FROM versions").fetchone() == (1,)
    assert set((dest / "staging").iterdir()) == previous_stages
    write(src, spec, DAY, [good | {prefix + "close": 11.25}])
    assert ingest(1)["code"] == "DATA_CONFLICT"
    plan()
    new = ingest(0)["snapshot_id"]
    assert old != new
    query = BarQuery(
        instrument_ids=("security-A",),
        timeframe="1d",
        start=spec.request.start,
        end=spec.request.end,
        as_of=datetime.now(UTC),
    )
    for snapshot, expected in ((old, "11.5"), (new, "11.25")):
        with store.open_reader(snapshot) as reader:
            (bar,) = reader.bars(query)
            assert bar.close == Decimal(expected)
