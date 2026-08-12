"""叠加策略子策略 override 透传测试。

验证修复: composite 执行时子策略应加载用户保存的 override(参数/评分等),
否则 composite 内跑子策略与单独跑子策略使用不同口径(CONTRIBUTING §5.1)。

场景: 子策略 filter 用 params["min_close"] 阈值。
- 默认 min_close=0 → 全选
- override 改 min_close=100 → 只选 close>100 的标的
验证 composite(引用该子策略)在有 override_loader 时使用 override 后的阈值。
"""
from __future__ import annotations

from datetime import date

import polars as pl

from app.strategy.engine import StrategyDataContext, StrategyEngine


def _param_filter_code(strategy_id: str) -> str:
    """子策略: filter 用 params["min_close"] 阈值, 命中 close > min_close。"""
    return f'''import polars as pl
META = {{
    "id": "{strategy_id}",
    "name": "{strategy_id}",
    "asset_types": ["stock"],
    "timeframes": ["1d"],
    "params": [{{"id": "min_close", "type": "float", "default": 0}}],
}}
EXECUTION_BACKEND = "polars_expr"
def filter(df, params):
    return pl.col("close") > params.get("min_close", 0)
'''


def _composite_code(strategy_id: str, child_id: str) -> str:
    return f'''META = {{
    "id": "{strategy_id}",
    "name": "{strategy_id}",
    "asset_types": ["stock"],
    "timeframes": ["1d"],
    "params": [],
    "children": [{{"strategy_id": "{child_id}", "weight": 1.0}}],
}}
EXECUTION_BACKEND = "composite"
'''


def _panel() -> pl.DataFrame:
    return pl.DataFrame({
        "symbol": ["LOW.SZ", "HIGH.SH"],
        "date": [date(2026, 1, 2)] * 2,
        "close": [10.0, 200.0],
    })


def test_composite_child_uses_default_params_without_loader(tmp_path):
    """无 override_loader 时, 子策略用默认参数(min_close=0 → 全选)。"""
    custom_dir = tmp_path / "strategies" / "custom"
    comp_dir = tmp_path / "strategies" / "composite"
    custom_dir.mkdir(parents=True)
    comp_dir.mkdir(parents=True)
    (custom_dir / "thresh.py").write_text(_param_filter_code("thresh"), encoding="utf-8")
    (comp_dir / "composite_blend.py").write_text(_composite_code("composite_blend", "thresh"), encoding="utf-8")

    engine = StrategyEngine(strategy_dirs=[custom_dir, comp_dir])  # 无 override_loader
    ctx = StrategyDataContext(
        asset_type="stock", timeframe="1d", as_of=date(2026, 1, 2),
        current=_panel(),
    )
    result = engine.run("composite_blend", ctx, overrides={"basic_filter": {"enabled": False}})
    # 默认 min_close=0 → 两个标的都命中
    assert {"LOW.SZ", "HIGH.SH"} == {r["symbol"] for r in result.rows}


def test_composite_child_uses_override_params_with_loader(tmp_path):
    """有 override_loader 时, 子策略用用户 override 的参数(min_close=100 → 只选 HIGH)。"""
    custom_dir = tmp_path / "strategies" / "custom"
    comp_dir = tmp_path / "strategies" / "composite"
    custom_dir.mkdir(parents=True)
    comp_dir.mkdir(parents=True)
    (custom_dir / "thresh.py").write_text(_param_filter_code("thresh"), encoding="utf-8")
    (comp_dir / "composite_blend.py").write_text(_composite_code("composite_blend", "thresh"), encoding="utf-8")

    # override_loader 返回 thresh 的 override: min_close=100
    overrides_store = {"thresh": {"params": {"min_close": 100}}}
    engine = StrategyEngine(
        strategy_dirs=[custom_dir, comp_dir],
        override_loader=lambda sid: overrides_store.get(sid, {}),
    )
    ctx = StrategyDataContext(
        asset_type="stock", timeframe="1d", as_of=date(2026, 1, 2),
        current=_panel(),
    )
    result = engine.run("composite_blend", ctx, overrides={"basic_filter": {"enabled": False}})
    # override 后 min_close=100 → 只有 HIGH.SH(close=200) 命中, LOW.SZ(close=10) 被过滤
    symbols = {r["symbol"] for r in result.rows}
    assert "HIGH.SH" in symbols
    assert "LOW.SZ" not in symbols, "子策略 override 的 min_close=100 应过滤掉 close=10 的标的"


def test_composite_child_override_loader_failure_is_safe(tmp_path):
    """override_loader 抛异常时, composite 应安全降级(用默认参数), 不崩溃。"""
    custom_dir = tmp_path / "strategies" / "custom"
    comp_dir = tmp_path / "strategies" / "composite"
    custom_dir.mkdir(parents=True)
    comp_dir.mkdir(parents=True)
    (custom_dir / "thresh.py").write_text(_param_filter_code("thresh"), encoding="utf-8")
    (comp_dir / "composite_blend.py").write_text(_composite_code("composite_blend", "thresh"), encoding="utf-8")

    def bad_loader(sid: str) -> dict:
        raise OSError("disk error")

    engine = StrategyEngine(
        strategy_dirs=[custom_dir, comp_dir],
        override_loader=bad_loader,
    )
    ctx = StrategyDataContext(
        asset_type="stock", timeframe="1d", as_of=date(2026, 1, 2),
        current=_panel(),
    )
    # loader 抛异常应被捕获, 用默认参数跑(全选), 不报错
    result = engine.run("composite_blend", ctx, overrides={"basic_filter": {"enabled": False}})
    assert result.total > 0
