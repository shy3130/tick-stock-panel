"""每日信号路径的字段依赖展开回归测试。

挖掘发布的 FactorRankResearchMatrixStrategy 把因子权重放在类级 SCORING,
META["scoring"] 为空。每日信号/实时矩阵路径通过 _matrix_field_columns 决定
矩阵字段, 必须展开 required_fields_for_params 的虚拟因子依赖
(limit_up_count_* -> consecutive_limit_ups), 否则 compute_signals 抛
"MarketDataMatrix missing field: consecutive_limit_ups"。
"""
from __future__ import annotations

import types
from datetime import date, timedelta

import polars as pl

from app.backtest.matrix import build_market_data_matrix
from app.strategy.engine import StrategyEngine
from app.strategy.research.factor_rank_research import FactorRankResearchMatrixStrategy


def _mined_limit_up_strategy() -> types.SimpleNamespace:
    strategy = FactorRankResearchMatrixStrategy(
        {"amplitude": 2.0, "limit_up_count_60d": 1.0},
        {"amplitude": "low", "limit_up_count_60d": "low"},
    )
    return types.SimpleNamespace(
        matrix_strategy=strategy,
        basic_filter=None,
        meta={"scoring": {}},
    )


def test_matrix_field_columns_expand_parameter_scoring_dependencies():
    strategy = _mined_limit_up_strategy()
    fields = StrategyEngine._matrix_field_columns(strategy, None, {})
    assert "consecutive_limit_ups" in fields
    assert "amplitude" in fields


def test_mined_limit_up_strategy_signals_build_from_panel_fields():
    rows = []
    start = date(2024, 1, 1)
    for offset in range(80):
        close = 10.0 + offset * 0.04
        rows.append({
            "symbol": "000001.SZ",
            "date": start + timedelta(days=offset),
            "open": close - 0.05,
            "high": close + 0.15,
            "low": close - 0.15,
            "close": close,
            "volume": 1000.0 + offset * 5.0,
            "amount": 100000.0,
            "amplitude": 1.5,
            "turnover_rate": 5.0,
            "consecutive_limit_ups": (offset % 17) + 1 if offset % 17 == 0 else 0,
        })
    panel = pl.DataFrame(rows)
    strategy = _mined_limit_up_strategy()
    market = build_market_data_matrix(
        panel,
        field_columns=StrategyEngine._matrix_field_columns(strategy, None, {}),
    )
    signals = strategy.matrix_strategy.compute_signals(market, {})
    assert signals.shape == market.shape
