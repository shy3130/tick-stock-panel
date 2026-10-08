"""每策略「叠加条件」(overlay_filter) — 校验 / 编译 / 选股过滤 / 回测掩码。

契约 (设计口径):
- 多条件 AND 硬过滤, 字段白名单与自定义信号同源 (含注册表因子/字符串扩展字段);
- 只挡入场/候选, exit_signal_hits 在过滤前收集, 已持仓卖出不受影响;
- null / 缺数据 ≡ 不命中 (fill_null(False), fail-closed);
- 仅日线 polars_expr / matrix_native 支持, composite/minute 保存期+运行期双拦。
"""
from __future__ import annotations

from datetime import date
from types import SimpleNamespace

import numpy as np
import polars as pl
import pytest

from app.strategy import custom_signals
from app.strategy.engine import StrategyDataContext, StrategyEngine

# ── 校验 ────────────────────────────────────────────────

def test_validate_overlay_accepts_whitelisted_conditions() -> None:
    custom_signals.validate_overlay_conditions([
        {"left": "close", "op": "<=", "right": 100},
        {"left": "vol_ratio_5d", "op": ">", "right": "1.5"},
    ])
    # 空列表 = 清空叠加条件, 合法
    custom_signals.validate_overlay_conditions([])


def test_validate_overlay_rejects_bad_conditions() -> None:
    with pytest.raises(ValueError, match="不在白名单"):
        custom_signals.validate_overlay_conditions([{"left": "nope_col", "op": ">", "right": 1}])
    with pytest.raises(ValueError, match="最多 8 条"):
        custom_signals.validate_overlay_conditions([
            {"left": "close", "op": ">", "right": 1}] * 9)
    with pytest.raises(ValueError, match="运算符"):
        custom_signals.validate_overlay_conditions([{"left": "close", "op": "contains", "right": "x"}])
    with pytest.raises(ValueError, match="日期偏移"):
        custom_signals.validate_overlay_conditions([
            {"left": "close", "op": ">", "right": 1, "leftDays": 1}])
    with pytest.raises(ValueError, match="必须是条件数组"):
        custom_signals.validate_overlay_conditions({"left": "close"})


def test_validate_overlay_string_fields(monkeypatch) -> None:
    monkeypatch.setattr(
        custom_signals, "_string_ext_fields",
        lambda: frozenset({"ext_x_name"}),
    )
    # 字符串字段: contains/==/!= 可用, 数值运算符拒绝
    custom_signals.validate_overlay_conditions([
        {"left": "ext_x_name", "op": "contains", "right": "AI"}])
    with pytest.raises(ValueError, match="字符串字段"):
        custom_signals.validate_overlay_conditions([
            {"left": "ext_x_name", "op": ">", "right": 1}])


# ── 编译与列依赖 ─────────────────────────────────────────

def test_build_overlay_expr_and_null_semantics() -> None:
    expr = custom_signals.build_overlay_expr([{"left": "close", "op": "<=", "right": 100}])
    assert expr is not None
    df = pl.DataFrame({"close": [10.0, 200.0, None]})
    mask = df.select(expr.fill_null(False).alias("m"))["m"]
    # null ≡ 不命中 (fail-closed): 缺数据的行被剔除
    assert mask.to_list() == [True, False, False]
    # 空条件 → None (未配置)
    assert custom_signals.build_overlay_expr([]) is None


def test_build_overlay_rejects_day_offset() -> None:
    with pytest.raises(ValueError, match="日期偏移"):
        custom_signals.build_overlay_expr([
            {"left": "close", "op": ">", "right": 1, "rightDays": 2}])


def test_overlay_missing_columns_reports_field_refs() -> None:
    conds = [
        {"left": "nope_a", "op": ">", "right": "field:nope_b"},
        {"left": "close", "op": ">", "right": 1},
    ]
    assert custom_signals.overlay_missing_columns(conds, ["close"]) == ["nope_a", "nope_b"]
    assert custom_signals.overlay_missing_columns(conds, ["close", "nope_a", "nope_b"]) == []


# ── engine.run 选股过滤 (polars 路径) ────────────────────

def _strategy_code(sid: str, *, with_exit: bool = False) -> str:
    exit_const = 'EXIT_SIGNALS = ["signal_exit"]' if with_exit else ""
    return f'''import polars as pl
META = {{
    "id": "{sid}",
    "name": "{sid}",
    "asset_types": ["stock"],
    "timeframes": ["1d"],
    "params": [],
}}
EXECUTION_BACKEND = "polars_expr"
{exit_const}
def filter(df, params):
    return pl.lit(True)
'''


def _panel() -> pl.DataFrame:
    return pl.DataFrame({
        "symbol": ["LOW.SZ", "HIGH.SH", "NULL.SZ"],
        "date": [date(2026, 1, 2)] * 3,
        "close": [10.0, 200.0, None],
        "signal_exit": [False, True, True],
    })


def _engine(tmp_path, code: str, sid: str) -> StrategyEngine:
    custom_dir = tmp_path / "strategies" / "custom"
    custom_dir.mkdir(parents=True, exist_ok=True)
    (custom_dir / f"{sid}.py").write_text(code, encoding="utf-8")
    return StrategyEngine(strategy_dirs=[custom_dir])


def test_run_overlay_filters_candidates_and_nulls(tmp_path) -> None:
    engine = _engine(tmp_path, _strategy_code("ov1"), "ov1")
    ctx = StrategyDataContext(
        asset_type="stock", timeframe="1d", as_of=date(2026, 1, 2), current=_panel(),
    )
    result = engine.run(
        "ov1", ctx,
        overrides={
            "basic_filter": {"enabled": False},
            "overlay_filter": [{"left": "close", "op": "<=", "right": 100}],
        },
    )
    symbols = {r["symbol"] for r in result.rows}
    # close=200 超阈值被剔除; close=null 的行 fail-closed 同样不入选
    assert symbols == {"LOW.SZ"}


def test_run_overlay_keeps_exit_hits_of_filtered_rows(tmp_path) -> None:
    """已持仓卖出不受叠加条件影响: 被过滤行的 exit 信号仍在过滤前帧上收集。"""
    engine = _engine(tmp_path, _strategy_code("ov2", with_exit=True), "ov2")
    ctx = StrategyDataContext(
        asset_type="stock", timeframe="1d", as_of=date(2026, 1, 2), current=_panel(),
    )
    result = engine.run(
        "ov2", ctx,
        overrides={
            "basic_filter": {"enabled": False},
            "overlay_filter": [{"left": "close", "op": "<=", "right": 100}],
        },
    )
    exit_symbols = {h["symbol"] for h in result.exit_signal_hits}
    # HIGH/NULL 的 close 不满足叠加条件 (不入选), 但其 signal_exit 命中仍被收集
    assert "HIGH.SH" in exit_symbols
    assert "NULL.SZ" in exit_symbols


def test_run_overlay_missing_column_fails_closed(tmp_path) -> None:
    engine = _engine(tmp_path, _strategy_code("ov3"), "ov3")
    ctx = StrategyDataContext(
        asset_type="stock", timeframe="1d", as_of=date(2026, 1, 2), current=_panel(),
    )
    with pytest.raises(ValueError, match="nope_col"):
        engine.run(
            "ov3", ctx,
            overrides={
                "basic_filter": {"enabled": False},
                "overlay_filter": [{"left": "nope_col", "op": ">", "right": 1}],
            },
        )


def test_run_overlay_rejects_minute_and_composite_backends(tmp_path) -> None:
    minute_code = '''import polars as pl
META = {
    "id": "ovm",
    "name": "ovm",
    "asset_types": ["stock"],
    "timeframes": ["1m"],
    "params": [],
}
EXECUTION_BACKEND = "minute_filter"
def filter_minute_history(df, params):
    return df.head(0)
'''
    engine = _engine(tmp_path, minute_code, "ovm")
    ctx = StrategyDataContext(
        asset_type="stock", timeframe="1m", as_of=date(2026, 1, 2),
        history=_panel().drop("signal_exit"),
    )
    with pytest.raises(ValueError, match="叠加条件不支持"):
        engine.run(
            "ovm", ctx,
            overrides={"overlay_filter": [{"left": "close", "op": ">", "right": 1}]},
        )


# ── API 校验与缓存失效判定 ──────────────────────────────

def test_api_validate_overlay_filter_backend_gate() -> None:
    from app.api.strategy import _validate_overlay_filter

    polars_strategy = SimpleNamespace(execution_backend="polars_expr")
    _validate_overlay_filter(polars_strategy, {
        "overlay_filter": [{"left": "close", "op": "<", "right": 100}]})  # 不抛

    for backend in ("composite", "minute_filter"):
        strategy = SimpleNamespace(execution_backend=backend)
        with pytest.raises(Exception, match="暂不支持"):
            _validate_overlay_filter(strategy, {
                "overlay_filter": [{"left": "close", "op": "<", "right": 100}]})

    # 键缺失 / None / 空列表 → 跳过条件校验
    _validate_overlay_filter(SimpleNamespace(execution_backend="composite"), {})
    _validate_overlay_filter(
        SimpleNamespace(execution_backend="composite"), {"overlay_filter": None})


def test_api_validate_overlay_filter_condition_pass_through() -> None:
    from app.api.strategy import _validate_overlay_filter

    strategy = SimpleNamespace(execution_backend="matrix_native")
    with pytest.raises(Exception, match="不在白名单"):
        _validate_overlay_filter(strategy, {
            "overlay_filter": [{"left": "nope", "op": "<", "right": 1}]})


def test_api_overlay_changed_normalizes_empty() -> None:
    from app.api.strategy import _overlay_changed

    assert _overlay_changed(None, []) is False       # None ≡ [] = 未配置
    assert _overlay_changed([], None) is False
    assert _overlay_changed(None, None) is False
    assert _overlay_changed([], [{"left": "close", "op": ">", "right": 1}]) is True
    assert _overlay_changed(
        [{"left": "close", "op": ">", "right": 1}],
        [{"left": "close", "op": ">", "right": 2}],
    ) is True


# ── 回测掩码 (polars panel / matrix fields) ─────────────

def test_backtest_overlay_panel_mask() -> None:
    from app.backtest.strategy import StrategyBacktestService

    panel = _panel()
    mask, err = StrategyBacktestService._overlay_panel_mask(
        panel, [{"left": "close", "op": "<=", "right": 100}])
    assert err is None
    assert mask.to_list() == [True, False, False]  # null 行 fail-closed

    _, err = StrategyBacktestService._overlay_panel_mask(
        panel, [{"left": "nope_col", "op": ">", "right": 1}])
    assert err is not None and "nope_col" in err


def test_backtest_overlay_matrix_mask_nan_is_not_equal() -> None:
    """matrix 路径 NaN 语义: `!=` 对 NaN 不得命中 (与 polars fill_null(False) 一致)。"""
    from app.backtest.strategy import StrategyBacktestService

    market = SimpleNamespace(
        timestamp_labels=("2026-01-02", "2026-01-05"),
        symbols=("A", "B"),
        fields={
            "ext_rank": np.array([[10.0, np.nan], [30.0, 40.0]]),
        },
    )
    mask, err = StrategyBacktestService._overlay_matrix_mask(
        market, [{"left": "ext_rank", "op": "!=", "right": 10}])
    assert err is None
    # (t0,B)=NaN: 10!=10 为 False; NaN!=10 numpy 为 True, 但缺数据不得命中
    assert mask.tolist() == [[False, False], [True, True]]

    _, err = StrategyBacktestService._overlay_matrix_mask(
        market, [{"left": "absent", "op": ">", "right": 1}])
    assert err is not None and "absent" in err


def test_backtest_matrix_resolver_includes_overlay_fields() -> None:
    from app.backtest.strategy import StrategyDependencyResolver

    strategy = SimpleNamespace(
        execution_backend="matrix_native",
        required_features=frozenset(),
        matrix_strategy=SimpleNamespace(
            required_fields=lambda: {"close"},
            required_warmup_bars=lambda params: 20,
        ),
        meta={"scoring": {}, "order_by": "score"},
    )
    overrides = {"overlay_filter": [{"left": "turnover_rate", "op": ">", "right": 5}]}
    plan = StrategyDependencyResolver()._resolve_matrix_native(
        strategy, params={}, basic_filter={}, overrides=overrides)
    assert "turnover_rate" in plan.matrix_columns

    # 字符串字段在 matrix 路径被拒绝 (矩阵 fields 是 float 数组)
    from app.factors import ext_factors
    monkey_target = ext_factors.ext_string_fields
    ext_factors.ext_string_fields = lambda: frozenset({"ext_x_name"})
    try:
        with pytest.raises(ValueError, match="字符串字段"):
            StrategyDependencyResolver()._resolve_matrix_native(
                strategy, params={}, basic_filter={},
                overrides={"overlay_filter": [
                    {"left": "ext_x_name", "op": "contains", "right": "AI"}]},
            )
    finally:
        ext_factors.ext_string_fields = monkey_target
