import json
import subprocess
import sys
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import polars as pl
import pytest
from pydantic import ValidationError
from tivelo_contracts.data import BarQuery
from tivelo_contracts.errors import ErrorCode, TiveloError

from tivelo_data import DataEngine
from tivelo_data.adapters.cn_daily import RepositoryMinuteRequest
from tivelo_data.providers import FixedSourceResolver, SyncContext, local_context

pytest.importorskip("tivelo_market_cn")
from tivelo_data.repository_minute import RepositoryMinuteProvider

DAY = date(2025, 7, 1)
A, B = "600000.SH", "000001.SZ"


def source(start="09:33", end="09:35", label="end", *, symbols=(A,), legacy=False, day=DAY):
    ids = tuple(sorted(symbols))
    return RepositoryMinuteRequest(
        layout="minute_v1",
        calendar_version="2026.1.0",
        expected_provider="archive",
        minute_label=label,
        price_evidence="legacy_close_anchor" if legacy else "marked_raw",
        symbols=[
            {"symbol": s, "venue_id": "XSHG" if s.endswith(".SH") else "XSHE", "instrument_id": s}
            for s in ids
        ],
        request=dict(
            dataset_id="minute",
            market_id="cn.equity",
            asset_class="equity",
            instrument_ids=ids,
            record_type="bar",
            timeframe="1m",
            price_basis="raw",
            start=f"{day}T{start}:00+08:00",
            end=f"{day}T{end}:00+08:00",
        ),
    )


def row(label, price=10, *, symbol=A, **changes):
    stamp = datetime.fromisoformat(f"{DAY}T{label}:00")
    return dict(
        {
            "symbol": symbol,
            "datetime": stamp,
            **{k: float(price) for k in ("open", "high", "low", "close")},
            "volume": 2.5,
            "amount": 2500.0,
        },
        **changes,
    )


def table(root, rows, folder=None, filename="part.parquet", marked=True):
    part = root / "kline_minute" / (folder or f"date={DAY}") / filename
    part.parent.mkdir(parents=True, exist_ok=True)
    if marked:
        (root / "kline_minute/.raw_basis").touch()
    pl.DataFrame(rows).write_parquet(part)
    return part


def daily(root, price=10, **extra):
    part = root / "kline_daily" / f"date={DAY}" / "part.parquet"
    part.parent.mkdir(parents=True, exist_ok=True)
    pl.DataFrame([dict(symbol=A, close=price, date=DAY, **extra)]).write_parquet(part)
    return part


def page(root, spec):
    p = RepositoryMinuteProvider(root, spec)
    ctx = local_context()
    try:
        p.check_ready(ctx)
        return p.fetch(spec.request, None, ctx)
    finally:
        p.close()


@pytest.mark.parametrize("label,expected", [("end", ["10", "11"]), ("start", ["9", "10"])])
def test_real_minute_console_pin_persist_query_and_byte_integrity(tmp_path, label, expected):
    root = tmp_path / "v1"
    path = table(root, [row("09:33", 9), row("09:34", 10), row("09:35", 11)])
    data = tmp_path / "data"
    cfg = tmp_path / "capture.json"
    spec = source(label=label)
    cfg.write_text(spec.model_dump_json())
    prefix = [sys.executable, "-I", "-m", "tivelo_data.cli", "--root", str(data)]
    args = ["--repository", str(root), "--capture", str(cfg)]
    locked = tmp_path / "lock.json"
    locked.write_bytes(subprocess.check_output([*prefix, "plan-repository", *args]))
    assert str(root).encode() not in locked.read_bytes() and not data.exists()
    before = datetime.now(UTC)
    old = path.read_bytes()
    cmd = [*prefix, "import-repository", *args, "--locked-plan", str(locked)]
    snapshot = json.loads(subprocess.check_output(cmd))["snapshot_id"]
    assert path.read_bytes() == old
    q = BarQuery(
        instrument_ids=(A,),
        timeframe="1m",
        price_basis="raw",
        start=spec.request.start,
        end=spec.request.end,
        as_of=datetime.now(UTC),
    )
    with DataEngine(data).open_reader(snapshot) as reader:
        (dataset,) = reader.datasets
        assert dataset.contents.knowledge_basis == "latest_reconstruction"
        assert dataset.contents.normalization_version == "minute.v1.marked-raw"
        assert dataset.contents.scope.bar_convention_id.endswith("." + label)
        assert dataset.contents.coverage.status == "complete"
        assert tuple(reader.bars(q.model_copy(update={"as_of": before}))) == ()
        bars = tuple(reader.bars(q))
        assert [b.close for b in bars] == list(map(Decimal, expected))
        assert [b.bar_start for b in bars] == [
            datetime(2025, 7, 1, 1, 33, tzinfo=UTC),
            datetime(2025, 7, 1, 1, 34, tzinfo=UTC),
        ]
        assert all(
            (b.volume, b.turnover, b.session_id, b.trading_date) == (250, 2500, "morning", DAY)
            for b in bars
        )
        assert all(b.available_at == b.ingested_at >= before for b in bars)
    table(root, [row("09:34", 8)])
    fail = subprocess.run(cmd, capture_output=True)
    assert fail.returncode == 1 and json.loads(fail.stderr)["code"] == "DATA_CONFLICT"


def test_lunch_break_close_auction_gap_coverage_and_separate_asset_table(tmp_path):
    table(
        tmp_path,
        [row("09:30"), row("11:30"), row("12:30"), row("13:01"), row("14:57"), row("15:00")],
    )
    e = tmp_path / "kline_etf_minute" / f"date={DAY}"
    e.mkdir(parents=True)
    pl.DataFrame([row("13:01", 12, symbol=B)]).write_parquet(e / "part.parquet")
    p = page(tmp_path, source("11:29", "13:02", symbols=(A, B)))
    assert [b.session_id for b in p.records] == ["morning", "afternoon"]
    assert (p.coverage.status, p.coverage.completed_units, p.coverage.requested_units) == (
        "partial",
        2,
        6,
    )
    p = page(tmp_path, source("09:30", "15:00"))
    assert (
        p.coverage.requested_units == 240
        and [b.session_id for b in p.records][-1] == "closing-auction"
    )
    assert page(tmp_path, source("11:30", "13:00")).coverage.issues[0].reason == "no_trades"
    assert page(tmp_path, source(day=date(2025, 7, 5))).coverage.issues[0].reason == "no_trades"


def test_old_symbol_partitions_overlay_only_missing_keys_and_preserve_other_dates(tmp_path):
    table(tmp_path, [row("09:34", 10), row("09:34", 10)])
    legacy = [row("09:34", 5), row("09:35", 11)]
    future = row("09:33", 16)
    future["datetime"] += timedelta(days=1)
    table(
        tmp_path,
        [{k: v for k, v in r.items() if k != "symbol"} for r in [*legacy, future]],
        folder="symbol=" + A,
    )
    p = page(tmp_path, source())
    assert p.coverage.status == "complete" and [b.close for b in p.records] == [10, 11]
    table(tmp_path, [row("09:34", 4)], filename="conflict.parquet")
    with pytest.raises(TiveloError) as error:
        page(tmp_path, source())
    assert error.value.code == ErrorCode.DATA_CONFLICT


@pytest.mark.parametrize(
    "label,first,terminal", [("end", "09:34", "15:00"), ("start", "09:33", "14:59")]
)
def test_legacy_raw_close_reconstruction_requires_explicit_policy_and_locked_anchor(
    tmp_path, label, first, terminal
):
    file = table(tmp_path, [row(first, 4.2), row(terminal, 5)], marked=False)
    baseline = file.read_bytes()
    with pytest.raises(TiveloError) as error:
        page(tmp_path, source(label=label))
    assert error.value.code == ErrorCode.RAW_PRICE_REQUIRED
    spec = source(label=label, legacy=True)
    with pytest.raises(TiveloError) as error:
        page(tmp_path, spec)
    assert error.value.code == ErrorCode.RAW_PRICE_REQUIRED
    daily(tmp_path, 10)
    (bar,) = page(tmp_path, spec).records
    assert (bar.close, bar.volume, bar.turnover) == (Decimal("8.4"), 250, 2500)
    assert file.read_bytes() == baseline and not (tmp_path / "kline_minute/.raw_basis").exists()
    p = RepositoryMinuteProvider(tmp_path, spec)
    e = DataEngine(tmp_path / "v2", FixedSourceResolver(p, p.configuration_digest))
    lock = e.plan_sync(spec.request)
    daily(tmp_path, 12)
    with pytest.raises(TiveloError) as error:
        e.sync(lock)
    assert error.value.code == ErrorCode.DATA_CONFLICT


@pytest.mark.parametrize(
    "when,anchor,extra",
    [
        ("14:59", 10, {}),
        ("15:00", 0, {}),
        ("15:00", 100, {}),
        (
            "15:00",
            10,
            {
                "quote_ts": int(
                    datetime.fromisoformat("2025-07-01T11:00:00+08:00").timestamp() * 1000
                )
            },
        ),
    ],
)
def test_incomplete_mismatched_or_intraday_close_is_not_used_as_raw(tmp_path, when, anchor, extra):
    table(tmp_path, [row("09:34", 4), row(when, 5)], marked=False)
    daily(tmp_path, anchor, **extra)
    with pytest.raises(TiveloError) as error:
        page(tmp_path, source(legacy=True))
    assert error.value.code == ErrorCode.RAW_PRICE_REQUIRED


@pytest.mark.parametrize(
    "changes",
    [
        {"datetime": datetime(2025, 7, 1, 9, 34, tzinfo=UTC)},
        {"datetime": datetime(2025, 7, 1, 9, 34, 1)},
        {"datetime": datetime(2025, 7, 2, 9, 34)},
        {"open": True},
        {"volume": -1},
        {"low": 11},
        {"volume": 0, "amount": 1},
    ],
)
def test_invalid_bar_schema_and_units_are_rejected(tmp_path, changes):
    table(tmp_path, [row("09:34", **changes)])
    with pytest.raises(TiveloError) as error:
        page(tmp_path, source())
    assert error.value.code == ErrorCode.CONTRACT_INVALID


def test_no_trade_rows_paging_cancellation_and_marker_change(tmp_path):
    table(tmp_path, [row("09:34", 0, volume=0, amount=0)])
    assert page(tmp_path, source()).coverage.status == "empty"
    symbols = tuple(f"{600000 + n}.SH" for n in range(11))
    labels = [datetime(2025, 7, 1, 9, 31) + timedelta(minutes=i) for i in range(120)] + [
        datetime(2025, 7, 1, 13, 1) + timedelta(minutes=i) for i in range(120)
    ]
    table(tmp_path, [row("09:34", symbol=s, datetime=dt) for s in symbols for dt in labels])
    spec = source("09:30", "15:00", symbols=symbols)
    provider = RepositoryMinuteProvider(tmp_path, spec)
    ctx = local_context()
    try:
        provider.check_ready(ctx)
        first = provider.fetch(spec.request, None, ctx)
        assert first.next_cursor == "2048" and len(first.records) == 2048
        with pytest.raises(TiveloError) as error:
            provider.fetch(
                spec.request,
                first.next_cursor,
                SyncContext(ctx.resolve_credentials, lambda: True, ctx.progress),
            )
        assert error.value.code == ErrorCode.CANCELLED
        tail = provider.fetch(spec.request, first.next_cursor, ctx)
        assert tail.coverage.status == "complete" and tail.coverage.row_count == len(symbols) * 240
        provider.check_ready(ctx)
        first = provider.fetch(spec.request, None, ctx)
        (tmp_path / "kline_minute/.raw_basis").unlink()
        with pytest.raises(TiveloError) as error:
            provider.fetch(spec.request, first.next_cursor, ctx)
        assert error.value.code == ErrorCode.DATA_CONFLICT
    finally:
        provider.close()


def test_capture_requires_minute_label_stock_scope_and_whole_intervals():
    data = source().model_dump()
    for change in (
        dict(minute_label=None),
        dict(request={**data["request"], "asset_class": "fund"}),
        dict(request={**data["request"], "start": "2025-07-01T09:33:15+08:00"}),
        dict(request={**data["request"], "price_basis": "qfq"}),
    ):
        with pytest.raises(ValidationError):
            RepositoryMinuteRequest.model_validate({**data, **change})


def test_float32_archive_cannot_silently_round_a_raw_price(tmp_path):
    path = table(tmp_path, [row("09:34", 1.2)])
    pl.read_parquet(path).with_columns(
        pl.col("open", "high", "low", "close").cast(pl.Float32)
    ).write_parquet(path)
    with pytest.raises(TiveloError) as error:
        page(tmp_path, source())
    assert error.value.code == ErrorCode.CONTRACT_INVALID
