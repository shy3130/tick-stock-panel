from __future__ import annotations

from datetime import date
from pathlib import Path

import polars as pl

from app.strategy.engine import StrategyEngine
from app.strategy.monitor import MonitorRuleEngine
from app.tickflow.repository import DataStore, KlineRepository


def _repo(tmp_path) -> KlineRepository:
    repo = KlineRepository(DataStore(tmp_path))
    repo._instruments_cache = pl.DataFrame({
        "symbol": ["600000.SH", "000001.SZ"],
        "name": ["浦发银行", "平安银行"],
        "total_shares": [29_352_080_397.0, 19_405_918_198.0],
        "float_shares": [29_352_080_397.0, 19_405_918_198.0],
    })
    return repo


def _live_row(symbol: str, close: float) -> pl.DataFrame:
    return pl.DataFrame({
        "symbol": [symbol],
        "date": [date(2026, 7, 20)],
        "open": [close],
        "high": [close],
        "low": [close],
        "close": [close],
        "volume": [1000.0],
        "amount": [close * 1000.0],
        "raw_close": [close],
        "raw_high": [close],
        "raw_low": [close],
    })


def test_live_enriched_cache_keeps_instrument_metadata_without_persisting_it(tmp_path):
    repo = _repo(tmp_path)

    repo.flush_live_enriched_asset("stock", _live_row("600000.SH", 10.0))
    repo.merge_live_enriched_asset("stock", _live_row("000001.SZ", 12.0))

    cached, cached_date = repo.get_enriched_latest()
    assert cached_date == date(2026, 7, 20)
    assert cached.select("symbol", "name").sort("symbol").to_dicts() == [
        {"symbol": "000001.SZ", "name": "平安银行"},
        {"symbol": "600000.SH", "name": "浦发银行"},
    ]
    assert cached["total_shares"].null_count() == 0
    assert cached["float_shares"].null_count() == 0

    persisted = pl.read_parquet(
        tmp_path / "kline_daily_enriched" / "date=2026-07-20" / "part.parquet"
    )
    assert "name" not in persisted.columns
    assert "total_shares" not in persisted.columns
    assert "float_shares" not in persisted.columns


def test_history_strategy_monitor_keeps_live_row_with_exclude_st_enabled(tmp_path):
    strategy_dir = tmp_path / "strategies"
    strategy_dir.mkdir()
    (strategy_dir / "history_strategy.py").write_text(
        """import polars as pl

META = {
    "id": "history_strategy",
    "name": "历史策略",
    "basic_filter": {"exclude_st": True},
}
LOOKBACK_DAYS = 2

def filter_history(df: pl.DataFrame, params: dict) -> pl.DataFrame:
    return df
""",
        encoding="utf-8",
    )
    repo = _repo(tmp_path / "data")
    live = _live_row("600000.SH", 10.0).with_columns(
        pl.lit(30_000_000.0).alias("amount")
    )
    repo.flush_live_enriched_asset("stock", live)
    current, _ = repo.get_enriched_latest()
    history = current.with_columns(pl.lit(date(2026, 7, 17)).alias("date"))

    monitor = MonitorRuleEngine()
    monitor.set_strategy_engine(StrategyEngine([Path(strategy_dir)]))
    monitor.set_history_loader(lambda _as_of, _lookback: history)
    monitor.set_rules([{
        "id": "history_strategy_monitor",
        "name": "历史策略监控",
        "type": "strategy",
        "asset_type": "stock",
        "strategy_id": "history_strategy",
        "scope": "all",
    }])

    monitor.evaluate(current)

    assert monitor.latest_strategy_results()["history_strategy"]["total"] == 1
