from datetime import date
from types import SimpleNamespace

import polars as pl
import pytest
from fastapi import HTTPException

from app.api import strategy as strategy_api
from app.strategy import config as strategy_config
from app.strategy.engine import StrategyDataContext, StrategyDef, StrategyEngine


def _make_engine() -> tuple[StrategyEngine, StrategyDataContext]:
    df = pl.DataFrame({"symbol": ["A", "B", "C"], "value": [1, 2, 3]})
    engine = StrategyEngine(strategy_dirs=[])
    engine._strategies["saved_params"] = StrategyDef(
        meta={"id": "saved_params", "scoring": {}, "limit": 100},
        basic_filter={"enabled": False},
        entry_signals=[],
        exit_signals=[],
        stop_loss=None,
        trailing_stop=None,
        trailing_take_profit_activate=None,
        trailing_take_profit_drawdown=None,
        max_hold_days=None,
        filter_fn=lambda _df, params: pl.col("value") >= params.get("min_value", 1),
        filter_history_fn=None,
        lookback_days=1,
        source="custom",
    )
    return engine, StrategyDataContext(
        asset_type="stock",
        timeframe="1d",
        as_of=date(2026, 7, 15),
        current=df,
    )


def test_run_applies_saved_strategy_params():
    engine, context = _make_engine()
    result = engine.run(
        "saved_params",
        context,
        overrides={"params": {"min_value": 2}},
    )

    assert [row["symbol"] for row in result.rows] == ["B", "C"]


def test_explicit_params_override_saved_strategy_params():
    engine, context = _make_engine()
    result = engine.run(
        "saved_params",
        context,
        params={"min_value": 3},
        overrides={"params": {"min_value": 2}},
    )

    assert [row["symbol"] for row in result.rows] == ["C"]


def test_patch_config_preserves_other_user_overrides(tmp_path):
    engine, _ = _make_engine()
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(
        strategy_engine=engine,
        repo=SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path)),
    )))
    strategy_config.save_override(tmp_path, "saved_params", {
        "params": {"min_value": 2},
        "stop_loss": -0.05,
    })

    strategy_api.patch_config(strategy_api.SaveConfigRequest(
        strategy_id="saved_params",
        overrides={
            "scoring": {"rsi_14": 1.0},
            "scoring_directions": {"rsi_14": "low"},
            "scoring_replace": True,
        },
    ), request)

    saved = strategy_config.load_override(tmp_path, "saved_params")
    assert saved["params"] == {"min_value": 2}
    assert saved["stop_loss"] == -0.05
    assert saved["scoring"] == {"rsi_14": 1.0}
    assert saved["scoring_directions"] == {"rsi_14": "low"}


def test_strategy_detail_echoes_saved_risk_overrides():
    """风控字段 (止盈/移动止损/回撤止盈) 的 override 优先回显 — 策略设置保存后,
    详情与回测页默认 overrides 都要能看到, 与 stop_loss 同口径。"""
    engine, _ = _make_engine()
    s = engine.get("saved_params")
    detail = strategy_api._strategy_detail(s, {
        "stop_loss": -0.05,
        "take_profit": 0.15,
        "trailing_stop": -0.08,
        "trailing_take_profit_activate": 0.2,
        "trailing_take_profit_drawdown": 0.05,
    }, engine)
    assert detail["stop_loss"] == -0.05
    assert detail["take_profit"] == 0.15
    assert detail["trailing_stop"] == -0.08
    assert detail["trailing_take_profit_activate"] == 0.2
    assert detail["trailing_take_profit_drawdown"] == 0.05


def test_strategy_detail_risk_falls_back_to_definition():
    """无 override 时回退策略定义里的风控值 (模块属性)。"""
    engine, _ = _make_engine()
    s = engine.get("saved_params")
    s.take_profit = 0.1  # StrategyDef 无该字段定义, 模拟模块属性 (getattr 路径)
    detail = strategy_api._strategy_detail(s, None, engine)
    assert detail["take_profit"] == 0.1
    detail_no_override = strategy_api._strategy_detail(s, {"stop_loss": -0.03}, engine)
    assert detail_no_override["take_profit"] == 0.1
    assert detail_no_override["stop_loss"] == -0.03


def test_save_config_rejects_invalid_scoring_direction(tmp_path):
    engine, _ = _make_engine()
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(
        strategy_engine=engine,
        repo=SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path)),
    )))

    with pytest.raises(HTTPException, match="方向无效"):
        strategy_api.save_config(strategy_api.SaveConfigRequest(
            strategy_id="saved_params",
            overrides={"scoring_directions": {"rsi_14": "sideways"}},
        ), request)
