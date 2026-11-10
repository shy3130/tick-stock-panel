"""历史首日与定日修补必须覆盖早盘，旧目录重试不能覆盖已落盘记录。"""

from datetime import date, datetime, timedelta

import polars as pl
import pytest

from app.market_time import CN_TZ
from app.services import kline_sync, minute_gaps
from app.tickflow.capabilities import Cap, CapabilitySet
from app.tickflow.repository import DataStore, KlineRepository

DAY = date(2026, 11, 2)
SYM = "600000.SH"


def session(symbol=SYM, day=DAY):
    times = [
        datetime.combine(day, datetime.min.time()).replace(hour=hour, minute=minute)
        + timedelta(minutes=i)
        for hour, minute in ((9, 31), (13, 1))
        for i in range(120)
    ]
    return pl.DataFrame(
        {
            "symbol": [symbol] * 240,
            "datetime": times,
            "close": [5.0] * 240,
            "volume": [100.0] * 240,
            "amount": [500.0] * 240,
        }
    )


@pytest.fixture
def market(tmp_path, monkeypatch):
    repo = KlineRepository(DataStore(tmp_path))
    caps = CapabilitySet()
    caps.grant(Cap.KLINE_MINUTE_BATCH)
    monkeypatch.setattr(kline_sync, "_selected_minute_provider_name", lambda: "test")
    monkeypatch.setattr(kline_sync, "_resolve_minute_provider", lambda _: (object(), False, None))
    monkeypatch.setattr(
        kline_sync, "cn_now", lambda: datetime(2026, 11, 9, 13, 10, 30, tzinfo=CN_TZ)
    )
    return repo, caps


@pytest.mark.parametrize(
    "options",
    [{"days": 7}, {"days": 5, "extend_backward": True}, {"days": 2, "force_full_days": True}],
)
def test_first_historical_day_includes_morning(market, monkeypatch, options):
    repo, caps = market

    def fetch(symbols, *, start_time, end_time, on_segment, **_):
        assert start_time.date() == DAY and start_time.utcoffset() == timedelta(hours=8)
        assert end_time == kline_sync.cn_now()
        on_segment(session().filter(pl.col("datetime") >= start_time.replace(tzinfo=None)))

    monkeypatch.setattr(kline_sync, "sync_minute_batch", fetch)
    kline_sync.sync_and_persist_minute([SYM], repo, caps, asset_type="stock", **options)
    assert repo.get_minute(SYM, DAY)["datetime"].to_list() == session()["datetime"].to_list()


def test_targeted_repair_filters_provider_and_counts_only_new_bars(market, monkeypatch):
    repo, caps = market
    folder = repo.store.data_dir / "kline_minute"
    afternoon = session().tail(110)
    other = session("000001.SZ").head(2)
    kline_sync._write_minute_partition(pl.concat([afternoon, other]), folder)

    def fetch(symbols, *, start_time, end_time, on_segment, **_):
        assert start_time == datetime(2026, 11, 2, 9, 25, tzinfo=CN_TZ)
        assert end_time == datetime(2026, 11, 2, 15, 5, tzinfo=CN_TZ)
        # 源忽略过滤，重复帧还带来旧字段、其他日期和未请求的标的。
        frame = pl.concat(
            [session(), session("000001.SZ"), session(day=DAY + timedelta(days=1))]
        ).drop("amount")
        for _ in range(2):
            on_segment(frame)

    monkeypatch.setattr(kline_sync, "sync_minute_batch", fetch)

    def repair():
        return kline_sync.sync_and_persist_minute(
            [SYM], repo, caps, asset_type="stock", repair_date=DAY
        )

    assert repair() == 130
    out = folder / f"date={DAY}" / "part.parquet"
    before = out.stat().st_mtime_ns
    assert repair() == 0 and out.stat().st_mtime_ns == before
    result = repo.get_minute(SYM, DAY)
    assert (
        result.height == 240
        and result.tail(110)["amount"].to_list() == afternoon["amount"].to_list()
    )
    assert repo.get_minute("000001.SZ", DAY).height == 2
    assert list(folder.glob("date=*")) == [out.parent]


def test_legacy_migration_keeps_published_bars_and_unreadable_source(market):
    repo, _ = market
    folder = repo.store.data_dir / "kline_minute"
    morning = session().head(120)
    other = session("000001.SZ").head(1)
    kline_sync._write_minute_partition(pl.concat([morning, other]), folder)
    old = folder / f"symbol={SYM}"
    old.mkdir()
    legacy = session().tail(121).with_columns(pl.lit(9.0).alias("close"))
    legacy.write_parquet(old / "part.parquet")
    broken = old / "broken.parquet"
    broken.write_bytes(b"unreadable")
    with pytest.raises(pl.exceptions.PolarsError):
        kline_sync._migrate_symbol_to_date_partition(repo)
    assert broken.exists()
    broken.unlink()
    kline_sync._migrate_symbol_to_date_partition(repo)
    kline_sync._migrate_symbol_to_date_partition(repo)
    df = pl.read_parquet(folder / f"date={DAY}" / "part.parquet")
    assert df.filter(pl.col("symbol") != SYM).equals(other)
    prices = df.filter(pl.col("symbol") == SYM)["close"].to_list()
    assert prices == [5.0] * 120 + [9.0] * 120
    assert not old.exists()


def test_repair_today_does_not_store_future_bars(market, monkeypatch):
    repo, caps = market
    now = kline_sync.cn_now()
    day = now.date()

    def fetch(symbols, *, start_time, end_time, on_segment, **_):
        assert start_time.hour == 9 and start_time.minute == 25
        assert end_time == now
        on_segment(session(day=day))

    monkeypatch.setattr(kline_sync, "sync_minute_batch", fetch)
    assert kline_sync.sync_and_persist_minute([SYM], repo, caps, repair_date=day) == 130
    assert repo.get_minute(SYM, day)["datetime"].max() == now.replace(second=0, tzinfo=None)


def test_recheck_counts_absent_and_duplicate_afternoon_as_unresolved(market):
    repo, _ = market
    folder = repo.store.data_dir / "kline_minute"
    part = folder / f"date={DAY}" / "part.parquet"
    part.parent.mkdir(parents=True)
    half = session().tail(120)
    pl.concat([half, half]).write_parquet(part)
    now = kline_sync.cn_now()
    assert minute_gaps.count_remaining_gaps(folder, DAY, [SYM, "000001.SZ"], now) == 2
    assert minute_gaps.count_remaining_gaps(folder, DAY + timedelta(days=1), [SYM], now) == 1


def test_interrupted_migration_retries_without_losing_either_day(market, monkeypatch):
    repo, _ = market
    folder = repo.store.data_dir / "kline_minute"
    other_day = DAY + timedelta(days=1)
    old = folder / f"symbol={SYM}" / "part.parquet"
    old.parent.mkdir(parents=True)
    pl.concat([session(day=DAY), session(day=other_day)]).drop("symbol").write_parquet(old)
    original_write = kline_sync._atomic_write_parquet

    def interrupted(frame, path):
        if path.parent.name == f"date={other_day}":
            raise OSError("disk full")
        original_write(frame, path)

    with monkeypatch.context() as m:
        m.setattr(kline_sync, "_atomic_write_parquet", interrupted)
        with pytest.raises(OSError, match="disk full"):
            kline_sync._migrate_symbol_to_date_partition(repo)
    assert old.exists()
    first = folder / f"date={DAY}" / "part.parquet"
    version = first.stat().st_mtime_ns
    kline_sync._migrate_symbol_to_date_partition(repo)
    assert not old.exists() and first.stat().st_mtime_ns == version
    for day in (DAY, other_day):
        assert repo.get_minute(SYM, day).height == 240
