"""get_daily 复用 enriched 历史缓存的等价性测试。

个股对话框打开时 /api/kline/daily 每个行情 tick 调用一次; 旧路径每次
150 天扫描 + 全套指标重算, 新路径优先从预计算历史缓存裁剪。
本测试证明: 同一份数据下两条路径输出逐列一致, 且缓存命中时不触发扫描。
"""
from __future__ import annotations

from datetime import date, timedelta

import polars as pl
from polars.testing import assert_frame_equal

from app.tickflow.repository import KlineRepository

SYM = "600001.SH"


def _raw_frame(days: int = 80) -> pl.DataFrame:
    """构造 ~57 个交易日的 14 列形态数据 (复权价与原始价一致, 无除权)。"""
    base = date(2026, 4, 1)
    rows = []
    price = 10.0
    d = base
    while len(rows) < days:
        if d.weekday() < 5:
            open_ = price * (1 + ((len(rows) % 7) - 3) * 0.004)
            close = price * (1 + ((len(rows) % 5) - 2) * 0.006)
            high = max(open_, close) * 1.01
            low = min(open_, close) * 0.99
            price = close
            rows.append({
                "symbol": SYM, "date": d,
                "open": round(open_, 4), "high": round(high, 4),
                "low": round(low, 4), "close": round(close, 4),
                "volume": 10000.0 + (len(rows) % 10) * 500.0,
                "amount": 1.0e7,
                "raw_close": round(close, 4), "raw_high": round(high, 4),
                "raw_low": round(low, 4),
            })
        d += timedelta(days=1)
    return pl.DataFrame(rows).sort(["symbol", "date"])


def _bare_repo(raw: pl.DataFrame) -> tuple[KlineRepository, dict]:
    repo = KlineRepository.__new__(KlineRepository)
    repo._enriched_history_cache = None
    repo._enriched_history_start = None
    repo._enriched_cache = None
    repo._enriched_cache_date = None
    repo.get_instruments = lambda: pl.DataFrame()  # type: ignore[method-assign]
    repo.get_historical_shares = lambda: pl.DataFrame()  # type: ignore[method-assign]
    repo.get_enriched_latest = lambda **kwargs: (pl.DataFrame(), None)  # type: ignore[method-assign]
    calls = {"scan": 0}

    def _scan(symbol, start, end, columns):
        calls["scan"] += 1
        return raw.filter((pl.col("date") >= start) & (pl.col("date") <= end))

    repo._scan_daily_symbol = _scan  # type: ignore[method-assign]
    return repo, calls


def test_get_daily_cache_path_matches_scan_path():
    raw = _raw_frame()
    dates = raw["date"].to_list()
    start, end = dates[30], dates[-1]

    # 旧路径: 无历史缓存 → 扫描 + 即时计算
    repo_old, calls_old = _bare_repo(raw)
    result_old = repo_old.get_daily(SYM, start, end)
    assert calls_old["scan"] == 1

    # 新路径: 预计算历史缓存 (同一真实计算栈 _compute_enriched_range 构建)
    repo_new, calls_new = _bare_repo(raw)
    hist = repo_new._compute_enriched_range(raw)
    repo_new._enriched_history_cache = hist
    repo_new._enriched_history_start = hist["date"].min()
    result_new = repo_new.get_daily(SYM, start, end)

    assert calls_new["scan"] == 0, "缓存命中时不得回退到扫描路径"
    assert result_new.height == result_old.height

    common = sorted(set(result_old.columns) & set(result_new.columns))
    assert {"open", "close", "ma5", "ma20", "ma60", "rsi_14"} <= set(common)
    assert_frame_equal(
        result_old.sort("date").select(common),
        result_new.sort("date").select(common),
        check_exact=False,
        rel_tol=1e-9,
    )


def test_get_daily_falls_back_to_scan_when_cache_does_not_cover_start():
    raw = _raw_frame()
    dates = raw["date"].to_list()

    repo, calls = _bare_repo(raw)
    hist = repo._compute_enriched_range(raw)
    repo._enriched_history_cache = hist
    repo._enriched_history_start = hist["date"].min()

    # 请求起点早于缓存覆盖 → 必须回退扫描路径
    result = repo.get_daily(SYM, dates[0] - timedelta(days=5), dates[-1])
    assert calls["scan"] == 1
    assert not result.is_empty()


def test_get_daily_with_columns_reuses_history_cache_and_overlays_latest():
    """带 columns 投影 (如 /minute 昨收 ["date", "close"]) 须优先命中内存历史缓存并叠加最新日。"""
    raw = _raw_frame()
    dates = raw["date"].to_list()
    start, end = dates[30], dates[-1]

    repo, calls = _bare_repo(raw)
    hist = repo._compute_enriched_range(raw)
    repo._enriched_history_cache = hist
    repo._enriched_history_start = hist["date"].min()
    latest_row = hist.filter(pl.col("date") == end).with_columns(pl.lit(99.5).alias("close"))
    repo.get_enriched_latest = lambda **kwargs: (latest_row, end)  # type: ignore[method-assign]

    projected = repo.get_daily(SYM, start, end, columns=["date", "close"])

    assert calls["scan"] == 0, "带 columns 投影命中历史缓存时不得退回全树扫描"
    assert projected.columns == ["date", "close"]
    assert projected.height == hist.filter((pl.col("date") >= start) & (pl.col("date") <= end)).height
    assert projected.filter(pl.col("date") == end)["close"].item() == 99.5


def test_date_partition_files_prunes_range_and_handles_legacy_layouts(tmp_path):
    """纯按日分区目录只返回 [start, end] 内 parquet; 遇旧布局返回 None 回退 glob。"""
    base = tmp_path / "kline_daily_enriched"
    for day in ("2026-01-05", "2026-01-06", "2026-01-07", "2026-01-08"):
        part_dir = base / f"date={day}"
        part_dir.mkdir(parents=True)
        pl.DataFrame({"symbol": [SYM], "date": [date.fromisoformat(day)], "close": [10.0]}).write_parquet(
            part_dir / "part.parquet"
        )
    (base / ".generation").write_text("1", encoding="utf-8")

    selected = KlineRepository._date_partition_files(base, date(2026, 1, 6), date(2026, 1, 7))
    assert selected is not None
    assert [p.split("date=")[-1].split("/")[0].split("\\")[0] for p in selected] == [
        "2026-01-06",
        "2026-01-07",
    ]

    empty = KlineRepository._date_partition_files(base, date(2026, 2, 1), date(2026, 2, 5))
    assert empty == []

    # 出现旧 symbol= 子目录时必须返回 None，触发调用方回退全树 glob
    (base / f"symbol={SYM}").mkdir()
    assert KlineRepository._date_partition_files(base, date(2026, 1, 6), date(2026, 1, 7)) is None


def test_latest_daily_and_minute_date_avoid_duckdb_on_date_partitions(tmp_path):
    """按日分区目录的 latest_daily_date / latest_minute_date 直读目录, 不触发 DuckDB 全扫。"""
    from datetime import datetime
    from types import SimpleNamespace

    daily_root = tmp_path / "kline_daily"
    for day in ("2026-01-05", "2026-01-06"):
        part_dir = daily_root / f"date={day}"
        part_dir.mkdir(parents=True)
        pl.DataFrame({"symbol": [SYM], "date": [date.fromisoformat(day)], "close": [10.0]}).write_parquet(
            part_dir / "part.parquet"
        )
    # 空分区目录与 0 行 parquet 不得误报为最新日
    (daily_root / "date=2026-01-09").mkdir(parents=True)
    zero_dir = daily_root / "date=2026-01-08"
    zero_dir.mkdir(parents=True)
    pl.DataFrame({
        "symbol": pl.Series([], dtype=pl.Utf8),
        "date": pl.Series([], dtype=pl.Date),
        "close": pl.Series([], dtype=pl.Float64),
    }).write_parquet(zero_dir / "part.parquet")

    minute_root = tmp_path / "kline_minute"
    for day, sym in (("2026-01-05", SYM), ("2026-01-06", "000001.SZ")):
        part_dir = minute_root / f"date={day}"
        part_dir.mkdir(parents=True)
        pl.DataFrame({
            "symbol": [sym],
            "datetime": [datetime.fromisoformat(f"{day}T09:30:00")],
            "close": [10.0],
        }).write_parquet(part_dir / "part.parquet")

    repo = KlineRepository.__new__(KlineRepository)
    repo.store = SimpleNamespace(data_dir=tmp_path)
    duckdb_calls = {"n": 0}

    def _fail_duckdb(*args, **kwargs):
        duckdb_calls["n"] += 1
        return (date(2025, 12, 31),)

    repo.execute_one = _fail_duckdb  # type: ignore[method-assign]

    assert repo.latest_daily_date() == date(2026, 1, 6)
    assert repo.latest_minute_date(SYM) == date(2026, 1, 5)
    assert repo.latest_minute_date("000001.SZ") == date(2026, 1, 6)
    assert duckdb_calls["n"] == 0

    # 最近 5 分区未命中时回退 DuckDB 兜底 (如长期停牌股)
    assert repo.latest_minute_date("999999.SH") == date(2025, 12, 31)
    assert duckdb_calls["n"] == 1


def test_get_daily_columns_without_date_deduplicates_latest_overlay():
    """缓存未命中且 columns 不含 date 时，叠加最新日缓存不得重复产出当日行。"""
    raw = _raw_frame()
    dates = raw["date"].to_list()
    start, end = dates[30], dates[-1]
    expected_rows = raw.filter((pl.col("date") >= start) & (pl.col("date") <= end)).height

    repo, calls = _bare_repo(raw)
    latest_row = raw.filter(pl.col("date") == end).with_columns(pl.lit(88.8).alias("close"))
    repo.get_enriched_latest = lambda **kwargs: (latest_row, end)  # type: ignore[method-assign]

    projected = repo.get_daily(SYM, start, end, columns=["close"])

    assert calls["scan"] == 1
    assert projected.columns == ["close"]
    assert projected.height == expected_rows
    assert projected["close"][-1] == 88.8
