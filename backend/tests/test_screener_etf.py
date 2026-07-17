from __future__ import annotations

import types
from datetime import date, timedelta
from pathlib import Path

import polars as pl
import pytest

from app.services.screener import ScreenerService
from app.strategy.engine import StrategyDataContext, StrategyEngine

BUILTIN_DIR = Path(__file__).resolve().parents[1] / "app" / "strategy" / "builtin"


class _FakeRepo:
    """最小 repo 桩: 只实现 screener 数据上下文需要的资产取数接口。"""

    def __init__(self, data_dir, enriched=None, instruments=None, latest=None):
        self.store = types.SimpleNamespace(data_dir=data_dir)
        self._enriched = enriched if enriched is not None else pl.DataFrame()
        self._instruments = instruments if instruments is not None else pl.DataFrame()
        self._latest = latest

    def get_enriched_latest_asset(self, asset_type):
        return self._enriched, self._latest

    def get_instruments_asset(self, asset_type):
        return self._instruments

    def get_enriched_history(self, target_date, lookback_days):
        return None


def _engine() -> StrategyEngine:
    return StrategyEngine(strategy_dirs=[BUILTIN_DIR])


def test_all_builtin_strategies_declare_asset_types_and_timeframes():
    engine = _engine()
    assert engine.load_errors() == []
    for meta in engine.list_strategies():
        assert meta["asset_types"]
        assert meta["timeframes"] == ["1d"]


def test_all_builtin_strategies_use_matrix_backend_only():
    engine = _engine()
    assert engine.load_errors() == []
    strategies = [engine.get(meta["id"]) for meta in engine.list_strategies()]
    assert len(strategies) == 18
    assert all(strategy.execution_backend == "matrix_native" for strategy in strategies)
    assert all(strategy.matrix_strategy is not None for strategy in strategies)
    assert all(strategy.filter_fn is None for strategy in strategies)
    assert all(strategy.filter_history_fn is None for strategy in strategies)


def test_all_builtin_matrix_formulas_accept_base_market_matrix():
    rows = []
    start = date(2024, 1, 1)
    for offset in range(80):
        close = 10.0 + offset * 0.04
        rows.append({
            "symbol": "000001.SZ",
            "name": "测试股票",
            "date": start + timedelta(days=offset),
            "open": close - 0.05,
            "high": close + 0.15,
            "low": close - 0.15,
            "close": close,
            "volume": 1000.0 + offset * 5.0,
            "amount": 100000.0,
            "raw_close": close,
            "turnover_rate": 5.0,
            "consecutive_limit_ups": 0,
        })
    panel = pl.DataFrame(rows)
    engine = _engine()
    from app.backtest.matrix import build_market_data_matrix

    fields = set()
    for strategy in (engine.get(meta["id"]) for meta in engine.list_strategies()):
        fields.update(engine._matrix_field_columns(strategy))
    market = build_market_data_matrix(panel, field_columns=fields)
    for meta in engine.list_strategies():
        strategy = engine.get(meta["id"])
        signals = strategy.matrix_strategy.compute_signals(market, {})
        assert signals.shape == market.shape, meta["id"]


def test_limit_up_strategies_are_stock_only():
    engine = _engine()
    for sid in ("broken_board_recovery", "consecutive_limit_ups"):
        assert engine.get(sid).meta["asset_types"] == ["stock"]


def test_pure_technical_strategies_support_etf():
    engine = _engine()
    for sid in (
        "trend_breakout", "ma_golden_cross", "macd_golden",
        "volume_price_surge", "low_volatility_leader", "oversold_bounce",
        "boll_breakout", "bullish_alignment", "pullback_to_support",
        "n_day_low_reversal",
    ):
        assert "etf" in engine.get(sid).meta["asset_types"], sid


def test_custom_strategy_defaults_to_stock_and_daily(tmp_path):
    path = tmp_path / "custom_default.py"
    path.write_text(
        'import polars as pl\n'
        'META = {"id": "custom_default", "name": "x"}\n'
        'def filter(df, params):\n    return pl.lit(True)\n',
        encoding="utf-8",
    )
    strategy = StrategyEngine._load_file(path)
    assert strategy.meta["asset_types"] == ["stock"]
    assert strategy.meta["timeframes"] == ["1d"]


def test_service_defaults_to_stock_dir(tmp_path):
    svc = ScreenerService(_FakeRepo(tmp_path))
    assert svc.asset_type == "stock"
    assert svc._enriched_dirname == "kline_daily_enriched"


def test_service_etf_uses_etf_dir(tmp_path):
    svc = ScreenerService(_FakeRepo(tmp_path), asset_type="etf")
    assert svc.asset_type == "etf"
    assert svc._enriched_dirname == "kline_etf_enriched"


def test_etf_strategy_runs_through_engine_context(tmp_path):
    rows = []
    for offset in range(61):
        trade_date = date(2025, 11, 3) + timedelta(days=offset)
        leader_close = 3.0 + offset / 60.0
        weak_close = 3.0 - offset / 60.0
        rows.extend([
            {
                "symbol": "510300", "name": "沪深300ETF", "date": trade_date,
                "open": leader_close - 0.01, "high": leader_close + 0.01,
                "low": leader_close - 0.02, "close": leader_close,
                "volume": 300.0 if offset == 60 else 100.0,
            },
            {
                "symbol": "159915", "name": "创业板ETF", "date": trade_date,
                "open": weak_close + 0.01, "high": weak_close + 0.02,
                "low": weak_close - 0.01, "close": weak_close,
                "volume": 50.0 if offset == 60 else 100.0,
            },
        ])
    history = pl.DataFrame(rows)
    target_date = history["date"].max()
    current = history.filter(pl.col("date") == target_date)
    engine = _engine()
    result = engine.run(
        "trend_breakout",
        StrategyDataContext(
            asset_type="etf",
            timeframe="1d",
            as_of=target_date,
            current=current,
            history=history,
        ),
        overrides={"basic_filter": {"enabled": False}},
    )
    assert result.total == 1
    assert result.rows[0]["symbol"] == "510300"


def test_stock_only_strategy_on_etf_fails_explicitly():
    engine = _engine()
    with pytest.raises(ValueError, match="does not support asset_type"):
        engine.run(
            "consecutive_limit_ups",
            StrategyDataContext(
                asset_type="etf",
                timeframe="1d",
                as_of=date(2026, 1, 2),
                current=pl.DataFrame({"symbol": ["510300"], "close": [4.0]}),
            ),
        )
