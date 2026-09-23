"""ETF 除权因子: 增量日K 必须用完整本地历史重算前复权 (issue #362)。

股票路径在新除权事件到达后会重算受影响标的的全部日期。ETF 路径只把本次
拉取窗口送进 compute_enriched, 历史分区停在未复权价, 日K 在拆分日留下跳空。
"""

from __future__ import annotations

from datetime import date, datetime
from types import SimpleNamespace

import polars as pl
import pytest

from app.services import index_sync, kline_sync
from app.tickflow.capabilities import Cap, CapabilityLimits, CapabilitySet
from app.tickflow.repository import DataStore, KlineRepository

ETF = "588200.SH"
PRE = date(2026, 3, 2)
EX = date(2026, 3, 3)
NEW = date(2026, 3, 4)
EX_FACTOR = 1.25
PRE_RAW = 10.0
EX_RAW = 8.0
NEW_RAW = 8.1
PRE_QFQ = PRE_RAW / EX_FACTOR  # 8.0


def _capset() -> CapabilitySet:
    return CapabilitySet(
        {
            Cap.KLINE_DAILY_BATCH: CapabilityLimits(batch=50, rpm=30),
            Cap.ADJ_FACTOR: CapabilityLimits(batch=50, rpm=30),
        }
    )


def _bar(day: date, close: float) -> pl.DataFrame:
    return pl.DataFrame(
        {
            "symbol": [ETF],
            "date": [day],
            "open": [close],
            "high": [close],
            "low": [close],
            "close": [close],
            "volume": [1000.0],
            "amount": [close * 1000.0],
        }
    )


def _write_factor(data_dir, day: date, factor: float) -> None:
    out = data_dir / "adj_factor_etf" / "all.parquet"
    out.parent.mkdir(parents=True, exist_ok=True)
    pl.DataFrame(
        {"symbol": [ETF], "trade_date": [day], "ex_factor": [factor]},
        schema={"symbol": pl.String, "trade_date": pl.Date, "ex_factor": pl.Float64},
    ).write_parquet(out)


def _enriched_close(data_dir, day: date) -> float:
    part = data_dir / "kline_etf_enriched" / f"date={day.isoformat()}" / "part.parquet"
    df = pl.read_parquet(part).filter(pl.col("symbol") == ETF)
    assert df.height == 1
    return float(df["close"][0])


def test_etf_incremental_daily_reapplies_adj_to_history(tmp_path, monkeypatch):
    """本地已有拆分前未复权 enriched, 增量只拉新一日时, 历史收盘必须改写成前复权价。"""
    repo = KlineRepository(DataStore(tmp_path))
    repo.append_etf_daily(pl.concat([_bar(PRE, PRE_RAW), _bar(EX, EX_RAW)]))
    repo.append_etf_enriched(pl.concat([_bar(PRE, PRE_RAW), _bar(EX, EX_RAW)]))
    _write_factor(tmp_path, EX, EX_FACTOR)

    monkeypatch.setattr(
        index_sync.kline_sync,
        "sync_daily_batch",
        lambda *a, **k: _bar(NEW, NEW_RAW),
    )
    monkeypatch.setattr(index_sync.preferences, "get_index_daily_batch_size", lambda: 50)

    written = index_sync.sync_and_persist_etf_daily(
        repo,
        _capset(),
        start_date=datetime(NEW.year, NEW.month, NEW.day),
        end_date=datetime(NEW.year, NEW.month, NEW.day, 15, 0),
        symbols_override=[ETF],
    )
    assert written == 1

    assert abs(_enriched_close(tmp_path, PRE) - PRE_QFQ) < 1e-9
    assert abs(_enriched_close(tmp_path, EX) - EX_RAW) < 1e-9
    assert abs(_enriched_close(tmp_path, NEW) - NEW_RAW) < 1e-9
    pre = pl.read_parquet(
        tmp_path / "kline_etf_enriched" / f"date={PRE.isoformat()}" / "part.parquet"
    ).filter(pl.col("symbol") == ETF)
    assert abs(float(pre["raw_close"][0]) - PRE_RAW) < 1e-9


def test_etf_adj_window_without_file_uses_history_start(tmp_path):
    start = datetime(2025, 3, 4)
    got = index_sync.etf_adj_factor_window_start(tmp_path / "missing.parquet", start)
    assert got == start


def test_etf_adj_window_with_file_continues_from_last_event(tmp_path):
    path = tmp_path / "all.parquet"
    pl.DataFrame(
        {
            "symbol": [ETF, ETF],
            "trade_date": [date(2025, 6, 1), date(2026, 1, 15)],
            "ex_factor": [1.05, 1.10],
        }
    ).write_parquet(path)
    got = index_sync.etf_adj_factor_window_start(path, datetime(2025, 3, 4))
    assert got == datetime(2026, 1, 15, 0, 0)


def test_sync_adj_factor_etf_custom_empty_falls_back_to_tickflow(tmp_path, monkeypatch):
    """扶摇等自定义源对 ETF 空返回时, 有 TickFlow 除权能力则回退, 不能当成无事件。"""
    repo = KlineRepository(DataStore(tmp_path))
    empty = pl.DataFrame(
        schema={
            "symbol": pl.String,
            "trade_date": pl.Date,
            "ex_factor": pl.Float64,
        }
    )

    class _EmptyETF:
        def get_adj_factors(
            self, symbols, start_time, end_time, asset_type="stock", on_chunk_done=None
        ):
            assert asset_type == "etf"
            return empty

    monkeypatch.setattr(kline_sync.preferences, "get_adj_factor_provider", lambda: "fuyao")
    from app.data_providers import custom as custom_sources

    monkeypatch.setattr(
        custom_sources,
        "provider_has_dataset",
        lambda name, dataset: name == "fuyao" and dataset == "adj_factor",
    )
    monkeypatch.setattr(custom_sources, "get_provider", lambda name: _EmptyETF())

    class _Klines:
        def ex_factors(self, symbols, **kwargs):
            return {ETF: [{"trade_date": EX, "ex_factor": EX_FACTOR}]}

    monkeypatch.setattr(
        kline_sync,
        "get_client",
        lambda: SimpleNamespace(klines=_Klines()),
    )

    written, affected = kline_sync.sync_adj_factor(
        [ETF],
        repo,
        _capset(),
        asset_type="etf",
    )
    assert written == 1
    assert affected == [ETF]
    stored = pl.read_parquet(tmp_path / "adj_factor_etf" / "all.parquet")
    assert stored["ex_factor"][0] == pytest.approx(EX_FACTOR)
