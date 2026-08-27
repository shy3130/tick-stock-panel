from datetime import date, datetime

import polars as pl
import pytest

from app.indicators.pipeline import (
    ENRICHED_STORAGE_COLS,
    _select_storage_cols,
    run_pipeline_local,
    run_pipeline_local_incremental,
)


class FakeProvider:
    name = "fquant_local"

    def get_daily(self, symbols, start_time, end_time, asset_type):
        rows = []
        for symbol in symbols:
            rows.extend([
                {
                    "symbol": symbol,
                    "date": date(2026, 6, 30),
                    "open": 10.0,
                    "high": 10.5,
                    "low": 9.8,
                    "close": 10.2,
                    "volume": 1000.0,
                    "amount": 10000.0,
                },
                {
                    "symbol": symbol,
                    "date": date(2026, 7, 1),
                    "open": 10.2,
                    "high": 10.8,
                    "low": 10.0,
                    "close": 10.6,
                    "volume": 1200.0,
                    "amount": 12000.0,
                },
            ])
        return pl.DataFrame(rows)

    def get_adj_factors(self, symbols, start_time, end_time, asset_type):
        return pl.DataFrame()


def test_run_pipeline_local_writes_enriched_without_raw(tmp_path, monkeypatch):
    monkeypatch.setattr("app.services.preferences.get_enriched_batch_size", lambda: 1)
    inst_dir = tmp_path / "instruments"
    inst_dir.mkdir(parents=True)
    pl.DataFrame({
        "symbol": ["600519.SH", "000001.SZ"],
        "name": ["贵州茅台", "平安银行"],
        "float_shares": [1_000_000_000.0, 1_000_000_000.0],
    }).write_parquet(inst_dir / "instruments.parquet")

    progress = []
    written = run_pipeline_local(
        FakeProvider(),
        data_dir=tmp_path,
        start_time=datetime(2026, 6, 30),
        end_time=datetime(2026, 7, 1),
        on_batch_done=lambda cur, tot: progress.append((cur, tot)),
    )

    assert written == 4
    assert progress == [(1, 2), (2, 2)]
    assert not (tmp_path / "kline_daily").exists()
    out = tmp_path / "kline_daily_enriched" / "date=2026-07-01" / "part.parquet"
    assert out.exists()
    df = pl.read_parquet(out).sort("symbol")
    assert df["symbol"].to_list() == ["000001.SZ", "600519.SH"]
    assert "raw_close" in df.columns


class DuplicateProvider:
    """Fake provider that returns repeated identical (symbol, date) rows,
    reproducing the exponential-duplicate root cause (e.g. 000001.SZ had
    16,777,216 identical rows in one partition)."""
    name = "fquant_local"

    def get_daily(self, symbols, start_time, end_time, asset_type):
        rows = []
        for symbol in symbols:
            for _ in range(5):  # 每个标的每个日期重复 5 行 (逐列完全一致)
                rows.append({
                    "symbol": symbol,
                    "date": date(2026, 6, 30),
                    "open": 10.0, "high": 10.5, "low": 9.8, "close": 10.2,
                    "volume": 1000.0, "amount": 10000.0,
                })
        return pl.DataFrame(rows)

    def get_adj_factors(self, symbols, start_time, end_time, asset_type):
        return pl.DataFrame()


def test_run_pipeline_local_deduplicates_identical_rows(tmp_path, monkeypatch):
    """管道必须把重复的 (symbol,date) 行去重到每键一行, 不能把指数级重复写入磁盘。"""
    monkeypatch.setattr("app.services.preferences.get_enriched_batch_size", lambda: 10)
    inst_dir = tmp_path / "instruments"
    inst_dir.mkdir(parents=True)
    pl.DataFrame({
        "symbol": ["000001.SZ", "000680.SZ"],
        "name": ["平安银行", "山推股份"],
        "float_shares": [1_000_000_000.0, 1_000_000_000.0],
    }).write_parquet(inst_dir / "instruments.parquet")

    written = run_pipeline_local(
        DuplicateProvider(),
        data_dir=tmp_path,
        start_time=datetime(2026, 6, 30),
        end_time=datetime(2026, 6, 30),
    )

    # 2 标的 × 1 日期, 去重后只有 2 行
    assert written == 2, f"expected 2 rows after dedup, got {written}"
    out = tmp_path / "kline_daily_enriched" / "date=2026-06-30" / "part.parquet"
    assert out.exists()
    df = pl.read_parquet(out).sort("symbol")
    # 自然键唯一: 每个 (symbol,date) 恰好一行
    keys = df.group_by(["symbol", "date"]).len()
    assert keys["len"].to_list() == [1, 1], "written partition still contains duplicate keys"
    assert df["symbol"].to_list() == ["000001.SZ", "000680.SZ"]


def test_select_storage_cols_enforces_uniqueness():
    """_select_storage_cols 是所有 staging/full/partial 写入的最后一道去重防线:
    即便上游绕过 compute 去重, 直接喂重复行也必须收敛到每键一行。
    """
    df = pl.DataFrame({
        "symbol": ["A", "A", "A", "B"],
        "date": [date(2026, 7, 1)] * 4,
        "open": [10.0] * 4, "high": [10.5] * 4, "low": [9.8] * 4,
        "close": [10.2, 10.2, 10.2, 5.0],
        "volume": [1000.0] * 4, "amount": [10000.0] * 4,
        "raw_open": [10.0] * 4,
        "raw_close": [10.2, 10.2, 10.2, 5.0],
        "raw_high": [10.5] * 4, "raw_low": [9.8] * 4,
        "turnover_rate": [1.0] * 4,
        "consecutive_limit_ups": [0] * 4,
        "consecutive_limit_downs": [0] * 4,
    })
    out = _select_storage_cols(df)
    assert set(out.columns) == set(ENRICHED_STORAGE_COLS)
    # 4 行 (3×A + 1×B) 去重后 2 行 (1×A + 1×B)
    assert out.height == 2
    keys = out.group_by(["symbol", "date"]).len()
    assert keys["len"].max() == 1


def test_range_repair_preserves_history_and_publishes_complete_partitions(tmp_path, monkeypatch):
    """指定范围补算应读取旧分区暖机，并且仅发布完整的目标日期。"""
    monkeypatch.setattr("app.services.preferences.get_enriched_batch_size", lambda: 2)
    inst_dir = tmp_path / "instruments"
    inst_dir.mkdir(parents=True)
    pl.DataFrame({
        "symbol": ["600519.SH", "000001.SZ"],
        "name": ["贵州茅台", "平安银行"],
        "float_shares": [1_000_000_000.0, 1_000_000_000.0],
    }).write_parquet(inst_dir / "instruments.parquet")

    written = run_pipeline_local_incremental(
        FakeProvider(),
        data_dir=tmp_path,
        start_time=datetime(2026, 7, 1),
        end_time=datetime(2026, 7, 1),
        min_partition_coverage=0.9,
    )

    assert written == 2
    assert not (tmp_path / "kline_daily").exists()
    out = tmp_path / "kline_daily_enriched" / "date=2026-07-01" / "part.parquet"
    assert out.exists()
    assert pl.read_parquet(out).sort("symbol")["symbol"].to_list() == ["000001.SZ", "600519.SH"]


def test_range_repair_rejects_partial_partition_before_publish(tmp_path, monkeypatch):
    """上游漏标的时必须保留旧分区，不能以残缺 staging 覆盖它。"""
    monkeypatch.setattr("app.services.preferences.get_enriched_batch_size", lambda: 2)
    inst_dir = tmp_path / "instruments"
    inst_dir.mkdir(parents=True)
    symbols = ["600519.SH", "000001.SZ"]
    pl.DataFrame({
        "symbol": symbols,
        "name": ["贵州茅台", "平安银行"],
        "float_shares": [1_000_000_000.0, 1_000_000_000.0],
    }).write_parquet(inst_dir / "instruments.parquet")

    class PartialProvider(FakeProvider):
        def get_daily(self, requested_symbols, start_time, end_time, asset_type):
            return super().get_daily(requested_symbols[:1], start_time, end_time, asset_type)

    with pytest.raises(RuntimeError, match="覆盖率不足"):
        run_pipeline_local_incremental(
            PartialProvider(),
            data_dir=tmp_path,
            start_time=datetime(2026, 7, 1),
            end_time=datetime(2026, 7, 1),
            min_partition_coverage=0.9,
        )

    assert not (tmp_path / "kline_daily_enriched" / "date=2026-07-01").exists()
    assert not (tmp_path / "_staging_kline_daily_enriched").exists()
