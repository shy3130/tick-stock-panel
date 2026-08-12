"""叠加策略 (composite) 加载、引用校验与选股合并测试。

覆盖 CONTRIBUTING §9 矩阵中的「策略」与「回测」相关最低要求:
- 加载解析正确, source 推断为 composite
- 引用缺失 → 移除孤儿 composite, 不波及无辜策略(插件隔离)
- 禁止嵌套 composite、asset_types 不一致、超过上限均 fail-closed
- find_dependents 用于删除防护
- 选股 union / intersect 合并, 标准化排名加权融合 score

子策略用 polars_expr 后端(返回 True)以便用轻量 DataFrame 验证合并逻辑,
不依赖 matrix_native 的矩阵加载。回测矩阵路径在 M2 单独测试。
"""
from __future__ import annotations

from datetime import date

import numpy as np
import polars as pl

from app.strategy.engine import StrategyDataContext, StrategyEngine


def _filter_strategy_code(strategy_id: str, body: str = "return pl.lit(True)") -> str:
    """生成一个 polars_expr 策略文件(filter 始终命中全部标的)。"""
    return f'''import polars as pl
META = {{
    "id": "{strategy_id}",
    "name": "{strategy_id}",
    "asset_types": ["stock"],
    "timeframes": ["1d"],
    "scoring": {{"close": 1.0}},
}}
EXECUTION_BACKEND = "polars_expr"
def filter(df, params):
    {body}
'''


def _composite_code(
    strategy_id: str,
    children: list[tuple[str, float]],
    *,
    name: str | None = None,
    asset_types: list[str] | None = None,
    merge_mode: str = "union",
    min_confirm: int = 0,
) -> str:
    """生成一个声明式 composite 策略文件。"""
    children_repr = ", ".join(
        f'{{"strategy_id": "{cid}", "weight": {w}}}' for cid, w in children
    )
    ats = asset_types or ["stock"]
    ats_repr = ", ".join(f'"{a}"' for a in ats)
    params = (
        f'{{"id": "merge_mode", "type": "select", '
        f'"options": ["union", "intersect"], "default": "{merge_mode}"}}, '
        f'{{"id": "min_confirm", "type": "int", "default": {min_confirm}}}'
    )
    return f'''META = {{
    "id": "{strategy_id}",
    "name": "{name or strategy_id}",
    "asset_types": [{ats_repr}],
    "timeframes": ["1d"],
    "params": [{params}],
    "children": [{children_repr}],
}}
EXECUTION_BACKEND = "composite"
'''


# ───────────────────────── 加载与引用校验 ─────────────────────────


def test_composite_loads_and_infers_source(tmp_path):
    child_dir = tmp_path / "strategies" / "custom"
    comp_dir = tmp_path / "strategies" / "composite"
    child_dir.mkdir(parents=True)
    comp_dir.mkdir(parents=True)
    (child_dir / "child_a.py").write_text(_filter_strategy_code("child_a"), encoding="utf-8")
    (comp_dir / "custom_blend.py").write_text(
        _composite_code("custom_blend", [("child_a", 1.0)]), encoding="utf-8"
    )

    engine = StrategyEngine(strategy_dirs=[child_dir, comp_dir])

    assert engine.has("custom_blend")
    blend = engine.get("custom_blend")
    assert blend.execution_backend == "composite"
    assert blend.source == "composite"
    assert blend.composite is not None
    assert blend.composite.children[0].strategy_id == "child_a"
    assert engine.load_errors() == []


def test_composite_missing_child_is_orphaned_without_blocking_others(tmp_path):
    """引用不存在的子策略 → composite 被移除并记错, 但不影响其他正常策略。"""
    child_dir = tmp_path / "strategies" / "custom"
    comp_dir = tmp_path / "strategies" / "composite"
    child_dir.mkdir(parents=True)
    comp_dir.mkdir(parents=True)
    (child_dir / "real_child.py").write_text(_filter_strategy_code("real_child"), encoding="utf-8")
    # composite 引用了不存在的 ghost_child
    (comp_dir / "orphan_blend.py").write_text(
        _composite_code("orphan_blend", [("ghost_child", 1.0)]), encoding="utf-8"
    )
    # 另一个正常 composite 不受影响
    (comp_dir / "healthy_blend.py").write_text(
        _composite_code("healthy_blend", [("real_child", 1.0)]), encoding="utf-8"
    )

    engine = StrategyEngine(strategy_dirs=[child_dir, comp_dir])

    assert not engine.has("orphan_blend")  # 孤儿被移除
    assert engine.has("real_child")  # 子策略不受影响
    assert engine.has("healthy_blend")  # 其他 composite 不受影响(隔离原则)
    errors = engine.load_errors()
    orphan_errors = [e for e in errors if "orphan_blend" in e["file"]]
    assert len(orphan_errors) == 1
    assert "ghost_child" in orphan_errors[0]["error"]


def test_nested_composite_rejected(tmp_path):
    """禁止 composite 嵌套 composite。"""
    child_dir = tmp_path / "strategies" / "custom"
    comp_dir = tmp_path / "strategies" / "composite"
    child_dir.mkdir(parents=True)
    comp_dir.mkdir(parents=True)
    (child_dir / "leaf.py").write_text(_filter_strategy_code("leaf"), encoding="utf-8")
    (comp_dir / "inner.py").write_text(
        _composite_code("inner", [("leaf", 1.0)]), encoding="utf-8"
    )
    (comp_dir / "outer.py").write_text(
        _composite_code("outer", [("inner", 1.0)]), encoding="utf-8"
    )

    engine = StrategyEngine(strategy_dirs=[child_dir, comp_dir])

    assert engine.has("inner")  # 单层 composite 合法
    assert not engine.has("outer")  # 嵌套被拒
    outer_errors = [e for e in engine.load_errors() if "outer" in e["file"]]
    assert len(outer_errors) == 1
    assert "嵌套" in outer_errors[0]["error"] or "nested" in outer_errors[0]["error"].lower()


def test_composite_asset_type_mismatch_rejected(tmp_path):
    """composite 与子策略 asset_types 不一致 → fail-closed。"""
    child_dir = tmp_path / "strategies" / "custom"
    comp_dir = tmp_path / "strategies" / "composite"
    child_dir.mkdir(parents=True)
    comp_dir.mkdir(parents=True)
    (child_dir / "etf_child.py").write_text(
        _filter_strategy_code("etf_child"), encoding="utf-8"
    )
    # 子策略是 stock(默认), composite 声明 etf
    (comp_dir / "mismatched.py").write_text(
        _composite_code("mismatched", [("etf_child", 1.0)], asset_types=["etf"]),
        encoding="utf-8",
    )

    engine = StrategyEngine(strategy_dirs=[child_dir, comp_dir])

    assert not engine.has("mismatched")
    errors = [e for e in engine.load_errors() if "mismatched" in e["file"]]
    assert len(errors) == 1
    assert "asset_types" in errors[0]["error"]


def test_composite_asset_type_subset_is_allowed(tmp_path):
    """子策略支持的范围 ⊇ composite 声明 → 合法(子集关系)。

    内置策略常声明 ['stock','etf'], 用户叠加时只关注 stock, 不应被拒绝。
    """
    child_dir = tmp_path / "strategies" / "custom"
    comp_dir = tmp_path / "strategies" / "composite"
    child_dir.mkdir(parents=True)
    comp_dir.mkdir(parents=True)
    # 子策略支持 stock + etf
    multi_code = _filter_strategy_code("multi").replace(
        '"asset_types": ["stock"]', '"asset_types": ["stock", "etf"]'
    )
    (child_dir / "multi.py").write_text(multi_code, encoding="utf-8")
    # composite 只声明 stock(子集) → 合法
    (comp_dir / "subset_ok.py").write_text(
        _composite_code("subset_ok", [("multi", 1.0)], asset_types=["stock"]),
        encoding="utf-8",
    )

    engine = StrategyEngine(strategy_dirs=[child_dir, comp_dir])
    assert engine.has("subset_ok")
    assert engine.load_errors() == []


def test_composite_exceeds_child_limit_rejected(tmp_path):
    """子策略数量超过 MAX_COMPOSITE_CHILDREN → fail-closed。"""
    from app.strategy.engine import MAX_COMPOSITE_CHILDREN

    child_dir = tmp_path / "strategies" / "custom"
    comp_dir = tmp_path / "strategies" / "composite"
    child_dir.mkdir(parents=True)
    comp_dir.mkdir(parents=True)
    for i in range(MAX_COMPOSITE_CHILDREN + 1):
        (child_dir / f"c{i}.py").write_text(_filter_strategy_code(f"c{i}"), encoding="utf-8")
    children = [(f"c{i}", 1.0) for i in range(MAX_COMPOSITE_CHILDREN + 1)]
    (comp_dir / "too_many.py").write_text(
        _composite_code("too_many", children), encoding="utf-8"
    )

    engine = StrategyEngine(strategy_dirs=[child_dir, comp_dir])

    assert not engine.has("too_many")
    errors = [e for e in engine.load_errors() if "too_many" in e["file"]]
    assert len(errors) == 1
    assert "limit" in errors[0]["error"] or "exceed" in errors[0]["error"]


def test_composite_with_filter_fn_rejected(tmp_path):
    """composite 策略声明了 filter 函数 → 加载失败。"""
    comp_dir = tmp_path / "strategies" / "composite"
    comp_dir.mkdir(parents=True)
    code = _composite_code("bad", []) + "def filter(df, params):\n    return pl.lit(True)\n"
    (comp_dir / "bad.py").write_text(code, encoding="utf-8")

    engine = StrategyEngine(strategy_dirs=[comp_dir])

    assert not engine.has("bad")
    assert any("bad" in e["file"] for e in engine.load_errors())


# ───────────────────────── find_dependents ─────────────────────────


def test_find_dependents_locates_referencing_composites(tmp_path):
    child_dir = tmp_path / "strategies" / "custom"
    comp_dir = tmp_path / "strategies" / "composite"
    child_dir.mkdir(parents=True)
    comp_dir.mkdir(parents=True)
    (child_dir / "shared.py").write_text(_filter_strategy_code("shared"), encoding="utf-8")
    (child_dir / "other.py").write_text(_filter_strategy_code("other"), encoding="utf-8")
    (comp_dir / "blend_a.py").write_text(
        _composite_code("blend_a", [("shared", 0.5), ("other", 0.5)]), encoding="utf-8"
    )
    (comp_dir / "blend_b.py").write_text(
        _composite_code("blend_b", [("shared", 1.0)]), encoding="utf-8"
    )

    engine = StrategyEngine(strategy_dirs=[child_dir, comp_dir])

    assert sorted(engine.find_dependents("shared")) == ["blend_a", "blend_b"]
    assert engine.find_dependents("other") == ["blend_a"]
    assert engine.find_dependents("nonexistent") == []


# ───────────────────────── 选股合并 ─────────────────────────


def _stock_panel(symbols: list[str], scores: list[float]) -> pl.DataFrame:
    """构造一个含 close 列(用于 scoring)的轻量 panel。"""
    return pl.DataFrame({
        "symbol": symbols,
        "date": [date(2026, 1, 2)] * len(symbols),
        "close": scores,
    })


def test_composite_union_merge_combines_children(tmp_path):
    """union 模式: 两个子策略命中不同标的 → 合并后包含全部。"""
    child_dir = tmp_path / "strategies" / "custom"
    comp_dir = tmp_path / "strategies" / "composite"
    child_dir.mkdir(parents=True)
    comp_dir.mkdir(parents=True)
    # child_a 只选 000001, child_b 只选 600000
    (child_dir / "child_a.py").write_text(
        _filter_strategy_code("child_a", body='return pl.col("symbol") == "000001.SZ"'),
        encoding="utf-8",
    )
    (child_dir / "child_b.py").write_text(
        _filter_strategy_code("child_b", body='return pl.col("symbol") == "600000.SH"'),
        encoding="utf-8",
    )
    (comp_dir / "union_blend.py").write_text(
        _composite_code("union_blend", [("child_a", 0.5), ("child_b", 0.5)]),
        encoding="utf-8",
    )

    engine = StrategyEngine(strategy_dirs=[child_dir, comp_dir])
    context = StrategyDataContext(
        asset_type="stock",
        timeframe="1d",
        as_of=date(2026, 1, 2),
        current=_stock_panel(["000001.SZ", "600000.SH"], [10.0, 20.0]),
    )

    # 禁用基础过滤(测试 panel 无 amount 等列); composite 会把 basic_filter 透传给子策略
    result = engine.run(
        "union_blend", context, overrides={"basic_filter": {"enabled": False}}
    )

    symbols = {row["symbol"] for row in result.rows}
    assert symbols == {"000001.SZ", "600000.SH"}
    assert result.total == 2
    # 合并 score 应该来自排名归一加权(两个子各命中一个, 均为各自第一 → norm=1)
    assert all(0 < result.scores[s] <= 100 for s in result.scores)


def test_composite_intersect_requires_min_confirm(tmp_path):
    """intersect 模式: 只有多个子策略共同命中才入选。"""
    child_dir = tmp_path / "strategies" / "custom"
    comp_dir = tmp_path / "strategies" / "composite"
    child_dir.mkdir(parents=True)
    comp_dir.mkdir(parents=True)
    # 两个子策略都选 000001(共振), 但 child_b 还选 600000(非共振)
    (child_dir / "child_a.py").write_text(
        _filter_strategy_code("child_a", body='return pl.col("symbol") == "000001.SZ"'),
        encoding="utf-8",
    )
    (child_dir / "child_b.py").write_text(
        _filter_strategy_code(
            "child_b",
            body='return pl.col("symbol").is_in(["000001.SZ", "600000.SH"])',
        ),
        encoding="utf-8",
    )
    (comp_dir / "intersect_blend.py").write_text(
        _composite_code(
            "intersect_blend",
            [("child_a", 0.5), ("child_b", 0.5)],
            merge_mode="intersect",
            min_confirm=2,
        ),
        encoding="utf-8",
    )

    engine = StrategyEngine(strategy_dirs=[child_dir, comp_dir])
    context = StrategyDataContext(
        asset_type="stock",
        timeframe="1d",
        as_of=date(2026, 1, 2),
        current=_stock_panel(["000001.SZ", "600000.SH"], [10.0, 20.0]),
    )

    result = engine.run(
        "intersect_blend", context, overrides={"basic_filter": {"enabled": False}}
    )

    # 只有 000001 同时被两个子策略命中
    symbols = {row["symbol"] for row in result.rows}
    assert symbols == {"000001.SZ"}


def test_composite_weighted_score_ranking(tmp_path):
    """权重影响最终排名: 高权重子策略的最优标的应排前。"""
    from app.strategy import composite as composite_mod
    from app.strategy.engine import StrategyResult

    # 直接测合并器: 两个子策略, 命中相同标的但内部排名不同。
    as_of = date(2026, 1, 2)
    res_a = StrategyResult(
        as_of=as_of,
        strategy_id="a",
        scores={"X": 100.0, "Y": 50.0},  # X 优于 Y
    )
    res_b = StrategyResult(
        as_of=as_of,
        strategy_id="b",
        scores={"X": 10.0, "Y": 90.0},  # Y 优于 X
    )

    # b 权重远大于 a → 合并后 Y 应得分更高
    merged = composite_mod.merge_results(
        [res_a, res_b],
        [0.1, 0.9],
        "union",
        0,
        as_of=as_of,
        strategy_id="blend",
    )
    # a 中 X rank=1(norm=1), Y rank=2(norm=0)
    # b 中 X rank=2(norm=0), Y rank=1(norm=1)
    # X = (0.1*1 + 0.9*0)/1.0 = 0.1; Y = (0.1*0 + 0.9*1)/1.0 = 0.9
    assert merged.scores["Y"] > merged.scores["X"]


def test_composite_no_scores_uses_neutral(tmp_path):
    """子策略无 score 时用中性分, 不报错也不污染。"""
    from app.strategy import composite as composite_mod
    from app.strategy.engine import StrategyResult

    as_of = date(2026, 1, 2)
    res = StrategyResult(
        as_of=as_of,
        strategy_id="a",
        rows=[{"symbol": "X"}, {"symbol": "Y"}],
        scores={},  # 无 score
    )

    merged = composite_mod.merge_results([res], [1.0], "union", 0, as_of=as_of, strategy_id="b")

    assert set(merged.scores) == {"X", "Y"}
    assert all(abs(s - 50.0) < 0.01 for s in merged.scores.values())  # 中性分 0.5*100


def test_composite_empty_children_returns_empty(tmp_path):
    """空子结果列表 → 返回空 StrategyResult。"""
    from app.strategy import composite as composite_mod

    as_of = date(2026, 1, 2)
    merged = composite_mod.merge_results(
        [], [], "union", 0, as_of=as_of, strategy_id="empty"
    )
    assert merged.total == 0
    assert merged.scores == {}


# ───────────────────────── 回测合并: merge_signal_matrices ─────────────────────────


def _make_sig(shape, *, entry, exit_, score=None):
    """构造一个轻量 SignalMatrix(用 make_signal_matrix 保证 dtype/只读)。"""
    from app.backtest.matrix import make_signal_matrix

    entry_arr = np.array(entry, dtype=np.uint8)
    exit_arr = np.array(exit_, dtype=np.uint8)
    score_arr = (
        np.array(score, dtype=np.float32)
        if score is not None
        else np.full(shape, 50.0, dtype=np.float32)
    )
    return make_signal_matrix(
        shape,
        entry=entry_arr,
        exit=exit_arr,
        score=score_arr,
    )


def test_merge_signal_matrices_union_entry():
    """union 模式: entry = OR(各子 entry)。"""
    from app.strategy import composite as composite_mod

    shape = (3, 2)
    # child A 选中 asset 0; child B 选中 asset 1
    sig_a = _make_sig(shape, entry=[[1, 0], [0, 0], [0, 0]], exit_=[[0, 0], [0, 0], [0, 0]])
    sig_b = _make_sig(shape, entry=[[0, 1], [0, 0], [0, 0]], exit_=[[0, 0], [0, 0], [0, 0]])

    merged = composite_mod.merge_signal_matrices(
        shape, [sig_a, sig_b], [("a", 0.5), ("b", 0.5)], "union", 0, max_hold=2
    )

    assert merged.entry[0, 0] == 1  # A 选中
    assert merged.entry[0, 1] == 1  # B 选中
    assert merged.entry[1].sum() == 0  # 后续无新入场


def test_merge_signal_matrices_intersect_entry():
    """intersect 模式: 只有多个子策略同时命中才入选。"""
    from app.strategy import composite as composite_mod

    shape = (2, 2)
    # asset 0 被两个子策略同时命中(共振); asset 1 只被 A 命中
    sig_a = _make_sig(shape, entry=[[1, 1], [0, 0]], exit_=[[0, 0], [0, 0]])
    sig_b = _make_sig(shape, entry=[[1, 0], [0, 0]], exit_=[[0, 0], [0, 0]])

    merged = composite_mod.merge_signal_matrices(
        shape, [sig_a, sig_b], [("a", 0.5), ("b", 0.5)], "intersect", 2, max_hold=2
    )

    assert merged.entry[0, 0] == 1  # 共振入选
    assert merged.entry[0, 1] == 0  # 非共振排除


def test_merge_exit_projection_prevents_cross_close():
    """退出投影: 子策略 B 的 exit 不会平掉子策略 A 选中的仓位。

    场景: A 在 t=0 买入 asset 0, max_hold=3 → A 的持仓窗口 t∈[0,2]。
          B 没买 asset 0, 但 B 在 t=1 对 asset 0 标了 exit(模拟无关退出信号)。
    期望: 合并 exit 在 t=1 的 asset 0 应为 0(B 未持仓, 其 exit 被投影清零)。
    """
    from app.strategy import composite as composite_mod

    shape = (3, 1)
    sig_a = _make_sig(shape, entry=[[1], [0], [0]], exit_=[[0], [0], [0]])
    # B 没买 asset 0, 但在 t=1 标了 exit
    sig_b = _make_sig(shape, entry=[[0], [0], [0]], exit_=[[0], [1], [0]])

    merged = composite_mod.merge_signal_matrices(
        shape, [sig_a, sig_b], [("a", 1.0), ("b", 1.0)], "union", 0, max_hold=3
    )

    # 关键断言: B 的 exit(t=1) 被投影清零, 因为 B 在 asset 0 没有持仓窗口
    assert merged.exit[1, 0] == 0, "B 的退出信号不应平掉 A 的仓位"


def test_merge_exit_respects_child_own_exit():
    """退出投影: 子策略自己的 exit 在自己持仓窗口内有效。

    场景: A 在 t=0 买入, 在 t=2 标 exit; max_hold=3。
    期望: 合并 exit 在 t=2 为 1(A 自己的退出在其持仓窗口内, 生效)。
    """
    from app.strategy import composite as composite_mod

    shape = (4, 1)
    sig_a = _make_sig(shape, entry=[[1], [0], [0], [0]], exit_=[[0], [0], [1], [0]])

    merged = composite_mod.merge_signal_matrices(
        shape, [sig_a], [("a", 1.0)], "union", 0, max_hold=3
    )

    assert merged.exit[2, 0] == 1  # A 自己的 exit 在窗口内, 生效


def test_merge_exit_max_hold_caps_window():
    """退出投影: 持仓窗口由 max_hold 封顶; 超出窗口后 exit 不生效。

    场景: A 在 t=0 买入, max_hold=2 → 窗口 t∈[0,1]。A 在 t=3 标 exit(超出窗口)。
    期望: 合并 exit 在 t=3 为 0(超出持仓窗口, exit 无效)。
    """
    from app.strategy import composite as composite_mod

    shape = (5, 1)
    sig_a = _make_sig(shape, entry=[[1], [0], [0], [0], [0]], exit_=[[0], [0], [0], [1], [0]])

    merged = composite_mod.merge_signal_matrices(
        shape, [sig_a], [("a", 1.0)], "union", 0, max_hold=2
    )

    assert merged.exit[1, 0] == 0  # 窗口内 A 无 exit
    assert merged.exit[3, 0] == 0  # 超出窗口, exit 无效


def test_merge_entry_signal_code_records_source():
    """合并后 entry_signal_code 标记来源子策略(归因用)。"""
    from app.strategy import composite as composite_mod

    shape = (1, 2)
    sig_a = _make_sig(shape, entry=[[1, 0]], exit_=[[0, 0]])
    sig_b = _make_sig(shape, entry=[[0, 1]], exit_=[[0, 0]])

    merged = composite_mod.merge_signal_matrices(
        shape, [sig_a, sig_b], [("child_a", 1.0), ("child_b", 1.0)], "union", 0, max_hold=1
    )

    # asset 0 来自 child A (code=0), asset 1 来自 child B (code=1)
    assert merged.entry_signal_code[0, 0] == 0
    assert merged.entry_signal_code[0, 1] == 1
    assert merged.entry_signal_ids == ("composite:child_a", "composite:child_b")
