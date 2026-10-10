"""ETF/指数矩阵链路不得依赖股本字段 (total_shares/float_shares)。

etf 维表 (index_sync) 物理上只有 symbol/name/code/asset_type, enriched
窄表也不落盘股本字段; 而矩阵缓存档的 common_filter 与 DEFAULT_BASIC_FILTER
都带非 None 的市值界, 依赖解析会因此无条件要求股本字段, 最终在
_resolve_matrix_storage_fields 抛
"matrix parquet fields unavailable: ['float_shares', 'total_shares']"
(用户反馈: ETF 因子挖掘死于「准备共享撮合矩阵」阶段)。非股票资产必须在
依赖解析前中和市值界, 股票行为保持不变。

换手率同族问题: common_filter 还强制 turnover_min: 0.0, 若不同时中和
turnover_min/max, 依赖解析会进一步要求 turnover_rate, 而 ETF enriched
窄表无 turnover_rate 列且无股本可派生 —— 旧代码在
_populate_matrix_derived_arrays 直接抛
"matrix turnover_rate requires float_shares", 使任何 ETF 矩阵回测
(含内置 ETF 策略) 全部失败。非股票资产在字段派生阶段应将缺失的
turnover_rate 降级为全 NaN 列 (与运行期 _optional_field 语义一致),
股票行为保持不变。
"""
from __future__ import annotations

from datetime import date
from pathlib import Path

import numpy as np
import polars as pl
import pyarrow.dataset as pads
import pytest

from app.backtest.matrix import (
    _normalize_matrix_cache_fields,
    _populate_matrix_derived_arrays,
    _resolve_matrix_storage_fields,
)
from app.backtest.strategy import (
    StrategyDependencyResolver,
    build_matrix_cache_profile,
)
from app.strategy.engine import StrategyEngine

_FIXTURES = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "strategies"
_RESEARCH = Path(__file__).resolve().parents[1] / "app" / "strategy" / "research"


def _engine() -> StrategyEngine:
    return StrategyEngine(strategy_dirs=[_FIXTURES, _RESEARCH, _FIXTURES.parent / "custom"])


def _resolve_research_plan(engine: StrategyEngine, asset_type: str = "stock"):
    research = engine.get("factor_rank_research")
    return StrategyDependencyResolver().resolve(
        research,
        params={
            "scoring": {"turnover_rate": 1.0},
            "directions": {"turnover_rate": "high"},
        },
        basic_filter=dict(research.basic_filter),
        entry_signals=research.entry_signals,
        exit_signals=research.exit_signals,
        overrides={},
        asset_type=asset_type,
    )


def test_etf_plan_does_not_require_share_fields():
    engine = _engine()
    assert engine.get("factor_rank_research").basic_filter.get("market_cap_min") is not None

    etf_plan = _resolve_research_plan(engine, asset_type="etf")
    assert "total_shares" not in etf_plan.instrument_columns
    assert "float_shares" not in etf_plan.instrument_columns

    stock_plan = _resolve_research_plan(engine, asset_type="stock")
    assert "total_shares" in stock_plan.instrument_columns


def test_etf_cache_profile_excludes_share_fields():
    engine = _engine()
    etf_plan = _resolve_research_plan(engine, asset_type="etf")
    etf_profile = build_matrix_cache_profile(
        engine, "etf", requested_plan=etf_plan, requested_forward_bars=5,
    )
    assert "total_shares" not in etf_profile.field_columns
    assert "float_shares" not in etf_profile.field_columns

    stock_plan = _resolve_research_plan(engine, asset_type="stock")
    stock_profile = build_matrix_cache_profile(
        engine, "stock", requested_plan=stock_plan, requested_forward_bars=5,
    )
    assert {"total_shares", "float_shares"} <= set(stock_profile.field_columns)


def _etf_instruments() -> pl.DataFrame:
    # index_sync._fetch_instruments_by_type("etf","etf") 的实际落盘 schema
    return pl.DataFrame({
        "symbol": ["510300.SH", "510500.SH"],
        "name": ["沪深300ETF", "中证500ETF"],
        "code": ["510300", "510500"],
        "asset_type": ["etf", "etf"],
    })


def _etf_dataset(tmp_path: Path) -> pads.Dataset:
    pl.DataFrame({
        "symbol": ["510300.SH", "510500.SH"],
        "date": [date(2024, 1, 2)] * 2,
        "open": [4.0, 6.0],
        "high": [4.1, 6.1],
        "low": [3.9, 5.9],
        "close": [4.0, 6.0],
        "volume": [100.0, 200.0],
        "amount": [400.0, 1200.0],
        "raw_close": [4.0, 6.0],
        "raw_high": [4.1, 6.1],
        "raw_low": [3.9, 5.9],
        "turnover_rate": [0.01, 0.02],
    }).write_parquet(tmp_path / "part.parquet")
    return pads.dataset(str(tmp_path / "part.parquet"), format="parquet")


def test_matrix_storage_fields_resolve_with_etf_instruments(tmp_path):
    engine = _engine()
    etf_plan = _resolve_research_plan(engine, asset_type="etf")
    profile = build_matrix_cache_profile(
        engine, "etf", requested_plan=etf_plan, requested_forward_bars=5,
    )
    requested = (
        set(etf_plan.base_columns)
        | set(etf_plan.instrument_columns)
        | set(etf_plan.matrix_columns)
    )
    build_fields = frozenset(
        _normalize_matrix_cache_fields(frozenset(requested))
        | _normalize_matrix_cache_fields(profile.field_columns)
    )
    assert "total_shares" not in build_fields

    _parquet_fields, matrix_fields, vector_fields = _resolve_matrix_storage_fields(
        _etf_dataset(tmp_path), build_fields, _etf_instruments(),
    )
    assert "total_shares" not in matrix_fields + vector_fields
    assert vector_fields == []


def test_share_fields_still_unavailable_without_sanitization(tmp_path):
    # 反向对照: 若市值界未被中和, ETF 维表下解析仍然失败 —— 锁定失败模式,
    # 防止未来把中和逻辑误删。
    with pytest.raises(ValueError, match=r"matrix parquet fields unavailable"):
        _resolve_matrix_storage_fields(
            _etf_dataset(tmp_path),
            frozenset({"name", "total_shares", "float_shares"}),
            _etf_instruments(),
        )


def _etf_dataset_without_turnover(tmp_path: Path) -> pads.Dataset:
    """真实 ETF enriched 窄表: 只有 OHLCV 基础列, 无 turnover_rate。"""
    pl.DataFrame({
        "symbol": ["510300.SH", "510500.SH"],
        "date": [date(2024, 1, 2)] * 2,
        "open": [4.0, 6.0],
        "high": [4.1, 6.1],
        "low": [3.9, 5.9],
        "close": [4.0, 6.0],
        "volume": [100.0, 200.0],
        "amount": [400.0, 1200.0],
        "raw_close": [4.0, 6.0],
        "raw_high": [4.1, 6.1],
        "raw_low": [3.9, 5.9],
    }).write_parquet(tmp_path / "part.parquet")
    return pads.dataset(str(tmp_path / "part.parquet"), format="parquet")


def test_etf_turnover_rate_degrades_to_nan_without_float_shares(tmp_path):
    """ETF enriched 无 turnover_rate 列且维表无 float_shares 时,
    派生阶段不得抛 "matrix turnover_rate requires float_shares":
    turnover_rate 保持全 NaN 列 (与运行期 _optional_field 降级语义一致),
    使不依赖换手率的 ETF 矩阵回测可以正常构建。"""
    engine = _engine()
    plan = _resolve_research_plan(engine, asset_type="etf")
    assert "turnover_rate" in plan.matrix_columns  # 依赖链仍会请求该字段

    dataset = _etf_dataset_without_turnover(tmp_path)
    parquet_fields, matrix_fields, vector_fields = _resolve_matrix_storage_fields(
        dataset,
        frozenset({"close", "turnover_rate"}),
        _etf_instruments(),
    )
    assert "turnover_rate" in matrix_fields
    assert vector_fields == []  # 无股本可派生

    shape = (2, 2)
    arrays = {
        "volume": np.array([[100.0, 200.0], [150.0, 250.0]], dtype=np.float32),
    }
    fields = {
        "turnover_rate": np.full(shape, np.nan, dtype=np.float32),
        "close": np.array([[4.0, 6.0], [4.2, 6.3]], dtype=np.float32),
    }
    seen = np.ones(shape, dtype=bool)
    # 修复前: 抛 "matrix turnover_rate requires float_shares"
    names, _limits = _populate_matrix_derived_arrays(
        ["510300.SH", "510500.SH"],
        arrays,
        fields,
        frozenset({"close", "turnover_rate"}),
        _etf_instruments(),
        seen,
        parquet_fields=parquet_fields,
        vector_fields=vector_fields,
    )
    assert names == ["沪深300ETF", "中证500ETF"]
    assert np.isnan(fields["turnover_rate"]).all()  # 降级为 NaN 列


def test_stock_turnover_rate_still_derived_when_float_shares_present(tmp_path):
    """对照: 股票场景若维表提供 float_shares, turnover_rate 仍按
    volume*10000/float_shares 正常派生 —— 修复只对"无股本资产"降级为 NaN,
    不得影响有股本数据的派生路径。"""
    engine = _engine()
    plan = _resolve_research_plan(engine, asset_type="stock")
    assert "turnover_rate" in plan.matrix_columns

    stock_inst = pl.DataFrame({
        "symbol": ["510300.SH", "510500.SH"],
        "name": ["沪深300ETF", "中证500ETF"],
        "code": ["510300", "510500"],
        "asset_type": ["stock", "stock"],
        "float_shares": [1.0e6, 2.0e6],  # 测试用极小股本, 便于断言非 NaN
    })
    dataset = _etf_dataset_without_turnover(tmp_path)  # parquet 无 turnover_rate
    parquet_fields, matrix_fields, vector_fields = _resolve_matrix_storage_fields(
        dataset,
        frozenset({"close", "turnover_rate", "float_shares"}),
        stock_inst,
    )
    assert "turnover_rate" in matrix_fields
    assert vector_fields == ["float_shares"]  # 有股本 -> 走派生

    shape = (2, 2)
    arrays = {
        "volume": np.array([[100.0, 200.0], [150.0, 250.0]], dtype=np.float32),
    }
    fields = {
        "turnover_rate": np.full(shape, np.nan, dtype=np.float32),
        "close": np.array([[4.0, 6.0], [4.2, 6.3]], dtype=np.float32),
    }
    seen = np.ones(shape, dtype=bool)
    _names, _limits = _populate_matrix_derived_arrays(
        ["510300.SH", "510500.SH"],
        arrays,
        fields,
        frozenset({"close", "turnover_rate", "float_shares"}),
        stock_inst,
        seen,
        parquet_fields=parquet_fields,
        vector_fields=vector_fields,
    )
    # volume(手)*10000/float_shares: 100*10000/1e6 = 1.0
    assert np.isfinite(fields["turnover_rate"]).all()
    assert float(fields["turnover_rate"][0, 0]) == pytest.approx(1.0)
    assert float(fields["turnover_rate"][1, 0]) == pytest.approx(1.5)


# ── #215: 运行期股票专属过滤键中和 ────────────────────────────

def test_basic_filter_for_asset_neutralizes_stock_only_runtime_keys() -> None:
    """#215 回归: boards 按股票代码前缀匹配、price_min=3 是股票专属口径,
    对 ETF 不可满足 —— 运行期不中和会让入场候选在掩码阶段静默清零
    (回测"正常完成"但零信号)。"""
    from app.backtest.strategy import _basic_filter_for_asset
    from app.strategy.engine import DEFAULT_BASIC_FILTER

    sanitized = _basic_filter_for_asset(dict(DEFAULT_BASIC_FILTER), "etf")
    for key in (
        "price_min", "price_max", "boards",
        "market_cap_min", "float_cap_min", "float_cap_max",
        "turnover_min", "turnover_max",
    ):
        assert sanitized[key] is None, f"{key} 应被中和"

    # 与资产类型无关的键保留原值
    assert sanitized["amount_min"] == DEFAULT_BASIC_FILTER["amount_min"]
    assert sanitized["exclude_st"] is True
    assert sanitized["exclude_new_days"] == 30

    # 股票口径完全不变
    stock = _basic_filter_for_asset(dict(DEFAULT_BASIC_FILTER), "stock")
    assert stock == DEFAULT_BASIC_FILTER
