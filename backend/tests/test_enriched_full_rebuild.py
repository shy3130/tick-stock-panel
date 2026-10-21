from __future__ import annotations

from datetime import date

import polars as pl
import pytest

from app.indicators import pipeline


def _write_daily(data_dir, ds: str, close: float) -> None:
    out = data_dir / "kline_daily" / f"date={ds}" / "part.parquet"
    out.parent.mkdir(parents=True, exist_ok=True)
    pl.DataFrame({
        "symbol": ["600000.SH"],
        "date": [date.fromisoformat(ds)],
        "open": [close],
        "high": [close],
        "low": [close],
        "close": [close],
        "volume": [100.0],
        "amount": [1000.0],
        "quote_ts": [0],
    }).write_parquet(out)


def _write_existing(data_dir, ds: str, close: float) -> None:
    out = data_dir / "kline_daily_enriched" / f"date={ds}" / "part.parquet"
    out.parent.mkdir(parents=True, exist_ok=True)
    pl.DataFrame({
        "symbol": ["600000.SH"],
        "date": [date.fromisoformat(ds)],
        "close": [close],
    }).write_parquet(out)


def _write_daily_empty(data_dir, ds: str) -> None:
    """0 行日K分区: 目录在 (glob 可见) 但重建读不到任何行 — 构造「重建读漏」场景。"""
    out = data_dir / "kline_daily" / f"date={ds}" / "part.parquet"
    out.parent.mkdir(parents=True, exist_ok=True)
    pl.DataFrame(schema={
        "symbol": pl.String, "date": pl.Date,
        "open": pl.Float64, "high": pl.Float64, "low": pl.Float64,
        "close": pl.Float64, "volume": pl.Float64, "amount": pl.Float64,
    }).write_parquet(out)


def _fake_compute_enriched(raw: pl.DataFrame, **_kwargs) -> pl.DataFrame:
    return raw.with_columns(
        pl.col("close").alias("raw_close"),
        pl.col("high").alias("raw_high"),
        pl.col("low").alias("raw_low"),
        pl.lit(None, dtype=pl.Float64).alias("turnover_rate"),
        pl.lit(0, dtype=pl.UInt32).alias("consecutive_limit_ups"),
        pl.lit(0, dtype=pl.UInt32).alias("consecutive_limit_downs"),
    )


def test_full_rebuild_overwrites_existing_partitions_without_deleting_base(tmp_path, monkeypatch):
    _write_daily(tmp_path, "2026-07-14", 14.0)
    _write_daily(tmp_path, "2026-07-15", 15.0)
    _write_existing(tmp_path, "2026-07-15", 1.0)
    marker = tmp_path / "kline_daily_enriched" / "keep.txt"
    marker.write_text("keep", encoding="utf-8")
    monkeypatch.setattr(pipeline, "compute_enriched", _fake_compute_enriched)

    written = pipeline.run_pipeline(data_dir=tmp_path)

    assert written == 2
    assert marker.read_text(encoding="utf-8") == "keep"
    assert pl.read_parquet(
        tmp_path / "kline_daily_enriched" / "date=2026-07-14" / "part.parquet"
    )["close"].to_list() == [14.0]
    assert pl.read_parquet(
        tmp_path / "kline_daily_enriched" / "date=2026-07-15" / "part.parquet"
    )["close"].to_list() == [15.0]


def test_full_rebuild_tolerates_orphan_partitions_absent_from_daily(tmp_path, monkeypatch):
    """孤儿分区 (enriched 有、日K已无): 重建告警放行, 孤儿分区原地保留不被删。

    现网案例: 用户清空日K重拉 (滑动一年窗口) 后, enriched 仍留着窗口外的旧分区;
    旧的「必须覆盖全部分区」校验会让强制全量重建永久失败 (拒绝覆盖: 2025-10-13..)。
    """
    _write_daily(tmp_path, "2026-07-14", 14.0)
    _write_daily(tmp_path, "2026-07-15", 15.0)
    _write_existing(tmp_path, "2026-07-13", 13.0)  # 孤儿: 日K已无此日期
    _write_existing(tmp_path, "2026-07-15", 1.0)
    monkeypatch.setattr(pipeline, "compute_enriched", _fake_compute_enriched)

    written = pipeline.run_pipeline(data_dir=tmp_path)

    assert written == 2
    # 孤儿分区原样保留 (合并只重写重建范围内的分区, 不删旧目录)
    assert pl.read_parquet(
        tmp_path / "kline_daily_enriched" / "date=2026-07-13" / "part.parquet"
    )["close"].to_list() == [13.0]
    assert pl.read_parquet(
        tmp_path / "kline_daily_enriched" / "date=2026-07-15" / "part.parquet"
    )["close"].to_list() == [15.0]


def test_full_rebuild_rejects_missing_existing_dates_before_writing(tmp_path, monkeypatch):
    """真实保护场景不回退: 日K里仍存在、enriched 已有的日期, 重建结果缺了 → 拒绝。

    用 0 行日K分区构造「重建读漏」: 目录在 (校验的日K日期集可见) 但暂存无该日行,
    若放行会拿缺日的重建结果覆盖全量数据。"""
    _write_daily(tmp_path, "2026-07-15", 15.0)
    _write_daily_empty(tmp_path, "2026-07-14")  # 日K在, 但重建读不到任何行
    _write_existing(tmp_path, "2026-07-14", 14.0)
    _write_existing(tmp_path, "2026-07-15", 1.0)
    monkeypatch.setattr(pipeline, "compute_enriched", _fake_compute_enriched)

    with pytest.raises(RuntimeError, match="缺少已有日期分区"):
        pipeline.run_pipeline(data_dir=tmp_path)

    existing = pl.read_parquet(
        tmp_path / "kline_daily_enriched" / "date=2026-07-15" / "part.parquet"
    )
    assert existing["close"].to_list() == [1.0]


# ================================================================
# 流式暂存发布 + 自适应批次 (#208/#174)
# ================================================================

def _write_daily_multi(data_dir, symbols: list[str], dates: list[str]) -> None:
    for ds in dates:
        rows = {
            "symbol": symbols,
            "date": [date.fromisoformat(ds)] * len(symbols),
            "open": [10.0] * len(symbols),
            "high": [11.0] * len(symbols),
            "low": [9.5] * len(symbols),
            "close": [10.5] * len(symbols),
            "volume": [100.0] * len(symbols),
            "amount": [1000.0] * len(symbols),
        }
        out = data_dir / "kline_daily" / f"date={ds}" / "part.parquet"
        out.parent.mkdir(parents=True, exist_ok=True)
        pl.DataFrame(rows).write_parquet(out)


def test_full_rebuild_streaming_output_matches_direct_compute(tmp_path, monkeypatch):
    """多批流式暂存 + 分块合并的输出与整帧直算完全一致 (#208)。"""
    symbols = [f"{600000 + i}.SH" for i in range(4)]
    dates = [f"2026-07-{d:02d}" for d in range(1, 6)]
    _write_daily_multi(tmp_path, symbols, dates)
    # 强制 2 只/批 → 2 批暂存 + 合并, 覆盖流式路径
    monkeypatch.setattr(pipeline, "_adaptive_sym_batch", lambda default, rows: 2)

    written = pipeline.run_pipeline(data_dir=tmp_path)
    assert written == len(symbols) * len(dates)

    from app.parquet import scan_daily_parquet
    raw = (scan_daily_parquet(str(tmp_path / "kline_daily" / "**" / "*.parquet"))
           .sort(["symbol", "date"]).collect())
    expected = pipeline._select_storage_cols(pipeline.compute_enriched(raw)).sort(["symbol", "date"])

    got = pl.read_parquet(str(tmp_path / "kline_daily_enriched" / "**" / "*.parquet"))
    got_cols = [c for c in expected.columns if c in got.columns]
    assert got.select(got_cols).sort(["symbol", "date"]).equals(expected.select(got_cols))


def test_full_rebuild_cleans_staging_and_keeps_it_outside_globs(tmp_path, monkeypatch):
    """重建完成后暂存目录被清理, 业务 glob 不会扫到暂存文件 (#208)。"""
    _write_daily(tmp_path, "2026-07-14", 14.0)
    _write_daily(tmp_path, "2026-07-15", 15.0)
    monkeypatch.setattr(pipeline, "compute_enriched", _fake_compute_enriched)

    pipeline.run_pipeline(data_dir=tmp_path)

    staging_root = tmp_path / ".staging" / "enriched_rebuild"
    assert not staging_root.exists() or not any(staging_root.iterdir())
    # 暂存位于 enriched 树外: enriched glob 只见 date=* 分区
    parts = list((tmp_path / "kline_daily_enriched").glob("date=*"))
    assert sorted(p.name for p in parts) == ["date=2026-07-14", "date=2026-07-15"]


def test_stale_staging_swept_on_full_rebuild(tmp_path, monkeypatch):
    """崩溃/取消残留的暂存目录按 mtime 被清扫 (#208)。"""
    import os
    import time as _time
    stale = tmp_path / ".staging" / "enriched_rebuild" / "dead-run"
    stale.mkdir(parents=True)
    junk = stale / "batch-0000.parquet"
    junk.write_bytes(b"junk")
    old = _time.time() - 2 * 24 * 3600
    os.utime(stale, (old, old))

    _write_daily(tmp_path, "2026-07-14", 14.0)
    monkeypatch.setattr(pipeline, "compute_enriched", _fake_compute_enriched)
    pipeline.run_pipeline(data_dir=tmp_path)

    assert not stale.exists()


def test_adaptive_sym_batch_shrinks_only_on_small_ram(monkeypatch):
    """小内存: 按目标行数收缩; 大内存: 保持用户设置 (#208)。"""
    monkeypatch.setattr(pipeline, "_total_ram_bytes", lambda: 2 * 1024 ** 3)
    got = pipeline._adaptive_sym_batch(1000, 1500)
    assert got == max(pipeline._BATCH_MIN_SYMBOLS, min(1000, 150_000 // 1500))
    # 行数极少时不低于下限
    assert pipeline._adaptive_sym_batch(1000, 1) == 1000

    monkeypatch.setattr(pipeline, "_total_ram_bytes", lambda: 16 * 1024 ** 3)
    assert pipeline._adaptive_sym_batch(1000, 1500) == 1000


def test_history_window_batched_equals_direct(tmp_path):
    """刷新窗口分批计算与整帧顺序执行等价 (#208)。"""
    import numpy as np
    rng = np.random.default_rng(11)
    n_syms, n_days = 6, 30
    close = 10.0 * np.cumprod(1 + rng.normal(0, 0.02, (n_syms, n_days)), axis=1)
    df_hist = pl.DataFrame({
        "symbol": np.repeat([f"{600000 + i}.SH" for i in range(n_syms)], n_days),
        "_day": np.tile(np.arange(n_days), n_syms),
        "open": (close * 0.99).reshape(-1),
        "high": (close * 1.01).reshape(-1),
        "low": (close * 0.98).reshape(-1),
        "close": close.reshape(-1),
        "volume": rng.integers(1000, 9000, n_syms * n_days).astype(float),
        "amount": rng.integers(1, 99, n_syms * n_days).astype(float),
    }).with_columns(
        (pl.lit(date(2026, 6, 1)) + pl.duration(days=pl.col("_day"))).alias("date")
    ).drop("_day")

    direct = pipeline.compute_signals(
        pipeline.attach_deviation_columns(
            pipeline.compute_indicators(df_hist.clone()), tmp_path
        )
    )
    batched = pipeline.compute_enriched_history_window(
        df_hist.clone(), tmp_path, sym_batch=2
    )
    assert batched.sort(["symbol", "date"]).equals(direct.sort(["symbol", "date"]))
