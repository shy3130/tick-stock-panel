"""repository 历史层回归: 去重刷新、惰性历史扫描与 get_enriched_range 三路径。

覆盖计划 Approach 2 的 Required assertions:
  - duplicate-safe refresh (最新日/盘中聚合不被重复行翻倍, 无全历史属性残留);
  - get_enriched_range 的 storage-only / price-change fast path / full-derived 三条路径;
  - 日期/符号/投影行为, 异常与空集约定;
  - 非交易日起点的 live state 仍能构建 (300 天闸门已删除);
  - cache_generation 在 refresh/clear 后自增。
"""
import json
from datetime import date, timedelta

import polars as pl
import pytest

from app.storage.repository import DataStore, KlineRepository


def repo(tmp_path) -> KlineRepository:
    return KlineRepository(DataStore(tmp_path))


def _write_inst(tmp_path, symbol: str = "000001.SZ") -> None:
    inst_dir = tmp_path / "instruments"
    inst_dir.mkdir(parents=True, exist_ok=True)
    pl.DataFrame({
        "symbol": [symbol],
        "name": ["测试股"],
        "float_shares": [1_000_000_000.0],
        "total_shares": [2_000_000_000.0],
    }).write_parquet(inst_dir / "instruments.parquet")


def _register_kline_enriched_view(r: KlineRepository) -> None:
    d = r.store.data_dir.as_posix()
    r.db.execute(
        f"""CREATE OR REPLACE VIEW kline_enriched AS
            SELECT * FROM read_parquet('{d}/kline_daily_enriched/**/*.parquet', union_by_name=true)"""
    )


def _storage_rows(symbol: str, n: int, start: date) -> pl.DataFrame:
    """n 行 14 列存储表数据 (前复权与原始价相同, 适合 append_enriched)。"""
    dates = [start + timedelta(days=i) for i in range(n)]
    base = [10.0 + i * 0.1 for i in range(n)]
    return pl.DataFrame({
        "symbol": [symbol] * n,
        "date": dates,
        "open": base,
        "high": [b + 0.5 for b in base],
        "low": [b - 0.3 for b in base],
        "close": base,
        "volume": [1000.0] * n,
        "amount": [10000.0] * n,
        "raw_close": base,
        "raw_high": [b + 0.5 for b in base],
        "raw_low": [b - 0.3 for b in base],
        "turnover_rate": [1.0] * n,
        "consecutive_limit_ups": [0] * n,
        "consecutive_limit_downs": [0] * n,
    })


def _unexpected_compute_all(*_args, **_kwargs):
    raise AssertionError("compute_all must not run on this range path")


# ── Approach 2: deleted history attributes ──────────────────────────


def test_no_full_history_attributes_after_init(tmp_path):
    r = repo(tmp_path)
    for attr in ("_enriched_history_cache", "_enriched_history_start",
                 "_hk_enriched_history_cache"):
        assert not hasattr(r, attr), f"{attr} should be deleted"
    for meth in ("get_enriched_history", "get_hk_enriched_history"):
        assert not hasattr(r, meth), f"{meth} should be deleted"
    assert isinstance(r.cache_generation, int)
    assert hasattr(type(r), "cache_generation")  # 是 property


# ── duplicate-safe refresh ───────────────────────────────────────────


def test_refresh_enriched_not_multiplied_by_duplicates(tmp_path, monkeypatch):
    monkeypatch.setattr("app.services.data_mode.is_local_daily_mode", lambda: False)
    _write_inst(tmp_path)
    r = repo(tmp_path)
    symbol = "000001.SZ"
    # 最新日重复 8 行 (同 symbol/date, 逐列一致)
    df = _storage_rows(symbol, 20, date(2026, 5, 1))
    latest = df["date"].max()
    dup_latest = df.filter(pl.col("date") == latest)
    df = pl.concat([df, *[dup_latest for _ in range(7)]], how="diagonal_relaxed")
    r.append_enriched(df)
    _register_kline_enriched_view(r)

    r._refresh_enriched()

    assert r._enriched_cache is not None
    # 最新日缓存里每个 (symbol,date) 恰好一行, 没有被重复行翻倍
    keys = r._enriched_cache.group_by(["symbol", "date"]).len()
    assert keys["len"].max() == 1
    # live_agg 行数 = 标的数 (1), 不应因重复行膨胀
    assert r._live_agg_cache is not None
    assert r._live_agg_cache.height == 1



def test_enriched_read_ceiling_isolates_unconfirmed_partition_without_deleting_it(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setattr("app.services.data_mode.is_local_daily_mode", lambda: False)
    _write_inst(tmp_path)
    r = repo(tmp_path)
    r.append_enriched(_storage_rows("000001.SZ", 20, date(2026, 7, 23)))
    _register_kline_enriched_view(r)
    r._refresh_enriched()
    assert r.get_enriched_latest()[1] == date(2026, 8, 11)

    r.set_enriched_canonical_date(date(2026, 8, 10))

    latest, latest_date = r.get_enriched_latest()
    assert not latest.is_empty()
    assert latest_date == date(2026, 8, 10)
    history = r.get_enriched_range(
        date(2026, 8, 10),
        date(2026, 8, 11),
        columns=["symbol", "date", "close"],
    )
    assert history is not None
    assert history["date"].max() == date(2026, 8, 10)
    assert (
        tmp_path
        / "kline_daily_enriched"
        / "date=2026-08-11"
        / "part.parquet"
    ).exists()

    r.trust_live_enriched_date(date(2026, 8, 11))
    r._refresh_enriched()
    assert r.get_enriched_latest()[1] == date(2026, 8, 11)

# ── cache_generation invalidation ────────────────────────────────────


def test_cache_generation_increments(tmp_path):
    r = repo(tmp_path)
    g0 = r.cache_generation
    r.refresh_cache()
    assert r.cache_generation == g0 + 1
    r._enriched_cache = pl.DataFrame({"symbol": ["000001.SZ"]})
    r.clear_cache()
    assert r.cache_generation == g0 + 2

# ── get_enriched_range: storage-only path ────────────────────────────


def test_get_enriched_range_storage_only(tmp_path, monkeypatch):
    monkeypatch.setattr("app.services.data_mode.is_local_daily_mode", lambda: False)
    _write_inst(tmp_path)
    r = repo(tmp_path)
    df = _storage_rows("000001.SZ", 30, date(2026, 5, 1))
    r.append_enriched(df)

    monkeypatch.setattr(
        "app.indicators.pipeline.compute_all",
        _unexpected_compute_all,
    )
    out = r.get_enriched_range(
        date(2026, 5, 5), date(2026, 5, 10),
        columns=["symbol", "date", "close", "volume"],
    )
    assert out is not None and not out.is_empty()
    assert out["date"].min() >= date(2026, 5, 5)
    assert out["date"].max() <= date(2026, 5, 10)
    assert set(out.columns) == {"symbol", "date", "close", "volume"}


# ── get_enriched_range: price-change fast path ───────────────────────


def test_get_enriched_range_price_change_fast_path(tmp_path, monkeypatch):
    monkeypatch.setattr("app.services.data_mode.is_local_daily_mode", lambda: False)
    _write_inst(tmp_path)
    r = repo(tmp_path)
    df = _storage_rows("000001.SZ", 400, date(2025, 1, 1))
    r.append_enriched(df)

    from app.indicators import pipeline
    real_compute_all = pipeline.compute_all
    monkeypatch.setattr(pipeline, "compute_all", _unexpected_compute_all)
    start, end = date(2026, 1, 1), date(2026, 1, 10)
    fast = r.get_enriched_range(start, end, columns=["symbol", "date", "change_pct"])
    assert fast is not None and not fast.is_empty()
    assert "change_pct" in fast.columns
    assert fast["date"].min() >= start
    assert fast["date"].max() <= end
    monkeypatch.setattr(pipeline, "compute_all", real_compute_all)

    # 与全量 compute_indicators 的 change_pct 一致 (同一份 raw_close 口径)
    full = r.get_enriched_range(start, end, columns=["symbol", "date", "change_pct", "ma5"])
    assert full is not None and not full.is_empty()
    joined = fast.join(full.select("symbol", "date", "change_pct"),
                       on=["symbol", "date"], suffix="_full")
    diff = (joined["change_pct"] - joined["change_pct_full"]).abs().max()
    assert diff < 1e-9, f"fast change_pct != full: max diff {diff}"


# ── get_enriched_range: full-derived path (columns=None) ─────────────


def test_get_enriched_range_full_derived(tmp_path, monkeypatch):
    monkeypatch.setattr("app.services.data_mode.is_local_daily_mode", lambda: False)
    _write_inst(tmp_path)
    r = repo(tmp_path)
    df = _storage_rows("000001.SZ", 400, date(2025, 1, 1))
    r.append_enriched(df)

    from app.indicators import pipeline
    real_compute_all = pipeline.compute_all
    calls = 0

    def counted_compute_all(*args, **kwargs):
        nonlocal calls
        calls += 1
        return real_compute_all(*args, **kwargs)

    monkeypatch.setattr(pipeline, "compute_all", counted_compute_all)
    out = r.get_enriched_range(date(2026, 1, 1), date(2026, 1, 5))
    assert out is not None and not out.is_empty()
    # 全套派生列应包含指标 + name (JOIN 自 instruments)
    assert "ma5" in out.columns
    assert "macd_dif" in out.columns
    assert "name" in out.columns
    assert calls == 1


# ── get_enriched_range: date/symbol/projection/edge ──────────────────


def test_get_enriched_range_filters_symbols(tmp_path, monkeypatch):
    monkeypatch.setattr("app.services.data_mode.is_local_daily_mode", lambda: False)
    _write_inst(tmp_path)
    r = repo(tmp_path)
    a = _storage_rows("000001.SZ", 20, date(2026, 5, 1))
    b = _storage_rows("600519.SH", 20, date(2026, 5, 1))
    r.append_enriched(pl.concat([a, b], how="diagonal_relaxed"))

    out = r.get_enriched_range(
        date(2026, 5, 1), date(2026, 5, 20),
        symbols=["000001.SZ"], columns=["symbol", "date", "close"],
    )
    assert out is not None
    assert set(out["symbol"].unique()) == {"000001.SZ"}


def test_get_enriched_range_empty_symbols_returns_empty_df(tmp_path):
    r = repo(tmp_path)
    out = r.get_enriched_range(
        date(2026, 1, 1), date(2026, 1, 5), symbols=[], columns=["close"],
    )
    assert out is not None
    assert out.is_empty()


def test_get_enriched_range_inverted_dates_returns_empty_df(tmp_path):
    r = repo(tmp_path)
    out = r.get_enriched_range(date(2026, 1, 10), date(2026, 1, 1))
    assert out is not None
    assert out.is_empty()


def test_get_enriched_range_forces_symbol_date_in_projection(tmp_path, monkeypatch):
    monkeypatch.setattr("app.services.data_mode.is_local_daily_mode", lambda: False)
    _write_inst(tmp_path)
    r = repo(tmp_path)
    df = _storage_rows("000001.SZ", 20, date(2026, 5, 1))
    r.append_enriched(df)

    out = r.get_enriched_range(
        date(2026, 5, 5), date(2026, 5, 10), columns=["close"],
    )
    assert out is not None
    assert "symbol" in out.columns
    assert "date" in out.columns



def test_get_enriched_range_returns_none_on_scan_failure(tmp_path):
    r = repo(tmp_path)
    out = r.get_enriched_range(
        date(2026, 5, 1),
        date(2026, 5, 2),
        columns=["symbol", "date", "close"],
    )
    assert out is None


# ── non-trading-day start: live state still builds ───────────────────


def test_build_live_agg_succeeds_when_start_not_trading_day(tmp_path, monkeypatch):
    """删除 `hist_all["date"].min() <= latest - 300d` 闸门后, 即便 warmup 起点
    不是交易日 (300 日历天很少恰好落在交易日), live state 仍应成功构建而非空表。
    """
    monkeypatch.setattr("app.services.data_mode.is_local_daily_mode", lambda: False)
    _write_inst(tmp_path)
    r = repo(tmp_path)
    symbol = "000001.SZ"
    # 340 天数据, 足够 engine_compat 的 120 根门槛
    df = _storage_rows(symbol, 340, date(2026, 1, 1))
    r.append_enriched(df)
    _register_kline_enriched_view(r)

    r._refresh_enriched()

    assert r._live_agg_cache is not None and not r._live_agg_cache.is_empty(), (
        "live_agg 应在 warmup 起点非交易日时仍构建成功"
    )
    assert r._live_agg_cache.height == 1


# ── dedup defense on append path ─────────────────────────────────────


def test_append_enriched_deduplicates_new_partition(tmp_path, monkeypatch):
    """append_enriched 在全新分区写入前也必须去重 (不依赖 out.exists())。"""
    monkeypatch.setattr("app.services.data_mode.is_local_daily_mode", lambda: False)
    r = repo(tmp_path)
    row = _storage_rows("000001.SZ", 1, date(2026, 7, 1))
    dup = pl.concat([row] * 6, how="diagonal_relaxed")
    r.append_enriched(dup)

    out = pl.read_parquet(tmp_path / "kline_daily_enriched" / "date=2026-07-01" / "part.parquet")
    assert out.height == 1, f"new partition should be deduplicated, got {out.height} rows"


def test_all_append_enriched_paths_deduplicate_new_partitions(tmp_path):
    row = _storage_rows("000001.SZ", 1, date(2026, 7, 2))
    dup = pl.concat([row] * 4, how="diagonal_relaxed")
    cases = [
        ("append_enriched", "kline_daily_enriched"),
        ("append_index_enriched", "kline_index_enriched"),
        ("append_etf_enriched", "kline_etf_enriched"),
        ("append_hk_enriched", "kline_hk_enriched"),
    ]

    for method_name, table in cases:
        root = tmp_path / method_name
        r = repo(root)
        getattr(r, method_name)(dup)
        out = pl.read_parquet(root / table / "date=2026-07-02" / "part.parquet")
        assert out.height == 1, f"{method_name} wrote duplicate natural keys"
        r.store.close()


def test_merge_and_flush_enriched_deduplicate_memory_and_disk(tmp_path):
    row = _storage_rows("000001.SZ", 1, date(2026, 7, 3))
    dup = pl.concat([row] * 5, how="diagonal_relaxed")

    for operation in ("merge_live_enriched_asset", "flush_live_enriched_asset"):
        root = tmp_path / operation
        r = repo(root)
        getattr(r, operation)("stock", dup)
        cached, cached_date = r.get_enriched_latest()
        on_disk = pl.read_parquet(
            root / "kline_daily_enriched" / "date=2026-07-03" / "part.parquet",
        )
        assert cached_date == date(2026, 7, 3)
        assert cached.height == 1
        assert on_disk.height == 1
        r.store.close()


def _publish_external_history(root, frame: pl.DataFrame) -> None:
    generation = "20260812T000000-deadbeef"
    generation_dir = root / "generations" / generation
    for value in frame.get_column("date").unique().sort().to_list():
        partition = generation_dir / f"date={value.isoformat()}"
        partition.mkdir(parents=True, exist_ok=True)
        frame.filter(pl.col("date") == value).write_parquet(partition / "part.parquet")
    manifest = {
        "schema_version": 1,
        "kind": "tickflow_canonical_enriched_history",
        "generation": generation,
        "path": f"generations/{generation}",
        "start_date": frame.get_column("date").min().isoformat(),
        "end_date": frame.get_column("date").max().isoformat(),
        "rows": frame.height,
        "symbols": frame.get_column("symbol").n_unique(),
        "trading_days": frame.get_column("date").n_unique(),
        "source": "test",
        "columns": frame.columns,
        "published_at": "2026-08-12T00:00:00+00:00",
    }
    payload = json.dumps(manifest)
    (generation_dir / "manifest.json").write_text(payload, encoding="utf-8")
    (root / "current.json").write_text(payload, encoding="utf-8")


def test_external_history_is_visible_without_local_partitions(tmp_path, monkeypatch):
    external_root = tmp_path / "published-history"
    _publish_external_history(
        external_root,
        _storage_rows("000001.SZ", 180, date(2025, 1, 1)),
    )
    monkeypatch.setenv("TICKFLOW_CANONICAL_HISTORY_ROOT", str(external_root))
    r = repo(tmp_path / "user-data")

    storage = r.get_enriched_range(
        date(2025, 5, 20),
        date(2025, 5, 30),
        columns=["symbol", "date", "close"],
    )
    assert storage is not None and storage.height == 11

    derived = r.get_enriched_range(
        date(2025, 5, 20),
        date(2025, 5, 30),
        columns=["symbol", "date", "change_pct", "ma5"],
    )
    assert derived is not None and derived.height == 11
    assert derived.get_column("ma5").drop_nulls().len() > 0

    chart = r.get_daily(
        "000001.SZ",
        date(2025, 5, 20),
        date(2025, 5, 30),
        columns=["symbol", "date", "close", "ma5"],
    )
    assert chart.height == 11
    assert chart.get_column("ma5").drop_nulls().len() > 0

    batch = r.get_daily_batch(
        ["000001.SZ"],
        date(2025, 5, 20),
        date(2025, 5, 30),
        columns=["symbol", "date", "close"],
    )
    assert batch.height == 11

    r._refresh_enriched()
    assert r.get_enriched_latest()[1] == date(2025, 6, 29)
    r.store.close()


def test_get_daily_returns_empty_when_no_local_or_external_history(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv(
        "TICKFLOW_CANONICAL_HISTORY_ROOT",
        str(tmp_path / "unpublished-history"),
    )
    r = repo(tmp_path / "user-data")

    out = r.get_daily(
        "000001.SZ",
        date(2026, 1, 1),
        date(2026, 1, 5),
    )

    assert out.is_empty()
    r.store.close()


def test_get_daily_asset_strictly_propagates_canonical_scan_failure(tmp_path, monkeypatch):
    r = repo(tmp_path)

    def _raise_scan(**_kwargs):
        raise RuntimeError("canonical catalog unavailable")

    monkeypatch.setattr(r, "_scan_merged_enriched", _raise_scan)

    assert r.get_daily_asset(
        "stock",
        "000001.SZ",
        date(2026, 1, 1),
        date(2026, 1, 5),
    ).is_empty()
    with pytest.raises(RuntimeError, match="canonical catalog unavailable"):
        r.get_daily_asset(
            "stock",
            "000001.SZ",
            date(2026, 1, 1),
            date(2026, 1, 5),
            raise_on_error=True,
        )
    r.store.close()


def test_trusted_local_overlay_wins_without_exposing_newer_untrusted_day(
    tmp_path,
    monkeypatch,
):
    external_root = tmp_path / "published-history"
    _publish_external_history(
        external_root,
        _storage_rows("000001.SZ", 3, date(2026, 1, 1)),
    )
    monkeypatch.setenv("TICKFLOW_CANONICAL_HISTORY_ROOT", str(external_root))
    user_data = tmp_path / "user-data"
    r = repo(user_data)
    local = _storage_rows("000001.SZ", 2, date(2026, 1, 3)).with_columns(
        pl.lit(99.0).alias("close"),
        pl.lit(99.0).alias("raw_close"),
    )
    r.append_enriched(local)
    r.set_enriched_canonical_date(date(2026, 1, 3))

    out = r.get_enriched_range(
        date(2026, 1, 1),
        date(2026, 1, 4),
        columns=["symbol", "date", "close"],
    )
    assert out is not None
    assert out.get_column("date").to_list() == [
        date(2026, 1, 1),
        date(2026, 1, 2),
        date(2026, 1, 3),
    ]
    assert out.filter(pl.col("date") == date(2026, 1, 3)).item(0, "close") == 99.0
    assert (
        user_data
        / "kline_daily_enriched"
        / "date=2026-01-04"
        / "part.parquet"
    ).exists()
    r.store.close()
