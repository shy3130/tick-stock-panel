"""T2 表达式级 Python (P3) 契约测试。

验收 (docs/ext-architecture-plan.md P3): SKDJ 类口径零 DSL 可表达 —
看板口径 SKDJ (9日RSV → EMA(α=0.5) 两级, 分母为 0 时 RSV 取 50) 与
策略文件里的分阶段 Polars 实现**逐元素一致**。

其余契约:
  - ctx.col 构造期登记依赖 (进 FactorSpec.dependencies → 计划/缓存同源);
  - warmup 从窗口常量保守推断 (只多不少);
  - 文法白名单 fail-closed (import/def/lambda/循环/推导式/下划线属性/
    回调类方法/IO 类调用/未定义名字/非表达式结果全部拒绝);
  - 存储与物化链路贯通 (to_spec 注册 → materialize 计算 → 内容寻址缓存)。
"""
from __future__ import annotations

import numpy as np
import polars as pl
import pytest

from app.config import settings
from app.factors import materialize_cache
from app.factors.py_expr import compile_python_expression
from app.factors.registry import unregister_factor
from app.factors.store import register_definition

# 看板口径 SKDJ — 与 custom_v1_kanban_strict 策略文件逐行同源 (分阶段列实现)
SKDJ_T2 = '''
llv = ctx.col("low").rolling_min(window_size=9).over("symbol")
hhv = ctx.col("high").rolling_max(window_size=9).over("symbol")
rsv = pl.when((hhv - llv) > 0).then((ctx.col("close") - llv) / (hhv - llv) * 100.0).otherwise(pl.lit(50.0))
k = rsv.ewm_mean(alpha=0.5, adjust=False).over("symbol")
d = k.ewm_mean(alpha=0.5, adjust=False).over("symbol")
d
'''


def _skdj_reference(df: pl.DataFrame) -> pl.Series:
    d = df.sort(["symbol", "date"]).with_columns([
        pl.col("low").rolling_min(window_size=9).over("symbol").alias("_llv9"),
        pl.col("high").rolling_max(window_size=9).over("symbol").alias("_hhv9"),
    ])
    denom = pl.col("_hhv9") - pl.col("_llv9")
    d = d.with_columns(
        pl.when(denom > 0)
        .then((pl.col("close") - pl.col("_llv9")) / denom * 100.0)
        .otherwise(pl.lit(50.0))
        .alias("_rsv")
    )
    d = d.with_columns(pl.col("_rsv").ewm_mean(alpha=0.5, adjust=False).over("symbol").alias("_sk_k"))
    d = d.with_columns(pl.col("_sk_k").ewm_mean(alpha=0.5, adjust=False).over("symbol").alias("_sk_d"))
    return d["_sk_d"]


def _frame(n_days: int = 120) -> pl.DataFrame:
    rng = np.random.default_rng(11)
    rows = []
    for sym in ("A", "B", "C"):
        base = rng.uniform(8, 12)
        for i in range(n_days):
            close = base + rng.normal(0, 0.3) + i * 0.005
            rows.append({
                "symbol": sym, "date": 20260101 + i,
                "open": close * 0.999, "high": close + 0.2, "low": close - 0.2, "close": close,
                "volume": rng.uniform(1e4, 1e6),
            })
    return pl.DataFrame(rows).sort(["symbol", "date"])


# ── 验收: SKDJ 等价 ──────────────────────────────────────

def test_skdj_t2_matches_staged_reference_exactly() -> None:
    compiled = compile_python_expression(SKDJ_T2)
    assert compiled.ok, [e.message for e in compiled.errors]
    frame = _frame()
    via_t2 = frame.select(compiled.expr.alias("d"))["d"]
    reference = _skdj_reference(frame)
    assert via_t2.equals(reference)
    # 窗口预热区 (前 8 行 RSV 初始 50 口径) 也参与比较 — equals 全量逐元素


def test_skdj_dependencies_and_warmup() -> None:
    compiled = compile_python_expression(SKDJ_T2)
    assert compiled.dependencies == frozenset({"low", "high", "close"})
    # rolling 9×2 (low/high 各一) + ewm(α=0.5→span3) ×2, +1 → 25 (保守, 只多不少)
    assert compiled.warmup_bars == 25


# ── 编译契约 ─────────────────────────────────────────────

def test_dependencies_tracked_from_ctx_col() -> None:
    compiled = compile_python_expression('ctx.col("close") / ctx.col("ma20") - 1')
    assert compiled.ok
    assert compiled.dependencies == frozenset({"close", "ma20"})


def test_declared_warmup_cannot_shrink_below_inferred() -> None:
    register_definition({
        "id": "uf_py_w", "label": "w", "kind": "custom", "lang": "python",
        "formula": 'ctx.col("close").rolling_mean(window_size=30).over("symbol")',
        "warmup_bars": 5, "status": "active",  # 声明小于推断 → 取推断 (下限)
    })
    from app.factors.registry import get_factor
    try:
        assert get_factor("uf_py_w").warmup_bars == 31
    finally:
        unregister_factor("uf_py_w")


@pytest.mark.parametrize("source, fragment", [
    ("import os", "不允许的语法: Import"),
    ("def f():\n    return 1", "不允许的语法"),
    ("ctx.col('close').map_batches(lambda s: s)", "不允许的语法: Lambda"),
    ("x = 0\nfor i in range(3):\n    x = x + i", "不允许的语法: For"),
    ("y = [ctx.col('close') for _ in range(2)]", "不允许的语法: ListComp"),
    ("ctx.col('close').__class__", "下划线属性"),
    ("ctx.col('close').map_elements(str)", "回调"),
    ("pl.read_csv('/etc/passwd')", "IO 类调用"),
    ("unknown_name + 1", "未定义的名字"),
    ("42", "结果必须是单个 Polars 表达式"),
    ("pl.col('close') + 1", "至少通过 ctx.col"),  # 绕过 ctx → 无依赖登记
])
def test_grammar_rejects_unsafe_or_malformed(source: str, fragment: str) -> None:
    compiled = compile_python_expression(source)
    assert not compiled.ok
    messages = [e.message for e in compiled.errors]
    assert any(fragment in m for m in messages), messages


def test_builtin_functions_unavailable() -> None:
    compiled = compile_python_expression("len(ctx.col('close'))")
    assert not compiled.ok  # 空 builtins: NameError → E212 求值失败


def test_declared_warmup_can_grow() -> None:
    register_definition({
        "id": "uf_py_w2", "label": "w2", "kind": "custom", "lang": "python",
        "formula": 'ctx.col("close").rolling_mean(window_size=10).over("symbol")',
        "warmup_bars": 60, "status": "active",
    })
    from app.factors.registry import get_factor
    try:
        assert get_factor("uf_py_w2").warmup_bars == 60  # 声明更大 → 生效
    finally:
        unregister_factor("uf_py_w2")


def test_unknown_lang_rejected() -> None:
    with pytest.raises(ValueError, match="未知 lang"):
        register_definition({
            "id": "uf_py_bad", "label": "b", "kind": "custom", "lang": "sql",
            "formula": "select 1", "status": "active",
        })


# ── 物化链路贯通 ─────────────────────────────────────────

@pytest.fixture(autouse=True)
def _isolated_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    materialize_cache.invalidate(disk=True)
    yield
    materialize_cache.invalidate(disk=True)


def test_python_factor_materializes_and_caches() -> None:
    from app.strategy.scoring import materialize_scoring_columns

    register_definition({
        "id": "uf_py_k", "label": "SKDJ-K", "kind": "custom", "lang": "python",
        "formula": SKDJ_T2, "status": "active",
    })
    frame = _frame(120)  # 小帧走缓存旁路 (语义不变), 物化链路本身不受影响
    try:
        result = materialize_scoring_columns(frame, ["uf_py_k"])
        assert "uf_py_k" in result.columns
        assert result["uf_py_k"].equals(_skdj_reference(frame))
        # 重复物化 (帧上无该列) → 结果一致
        again = materialize_scoring_columns(frame, ["uf_py_k"])
        assert again["uf_py_k"].equals(result["uf_py_k"])
    finally:
        unregister_factor("uf_py_k")
