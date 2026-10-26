"""filter_history 列依赖 AST 推导 — 消灭回测全量兜底的 P1 契约。

回归对象: REQUIRED_FEATURES 缺失 + 存在动态 filter_history 的策略。
- 可证明形态 (col/cols 常量引用) → 引擎加载时自动推导精确依赖;
- 不可证明形态 (动态列名/pl.all/cs.*) → 保持空依赖, 回测继续全量兜底 (fail-closed)。
"""
from __future__ import annotations

from pathlib import Path

import polars as pl
import polars.selectors as cs

from app.strategy.engine import StrategyEngine
from app.strategy.feature_deps import infer_filter_history_features

# ── 单元: 推导函数 ───────────────────────────────────────

def test_infer_basic_column_refs():
    def filter_history(df: pl.DataFrame, params: dict) -> pl.DataFrame:
        return df.filter(
            (pl.col("close") > pl.col("ma20"))
            & (pl.col("kdj_k") < 25)
        )

    assert infer_filter_history_features(filter_history) == frozenset({"close", "ma20", "kdj_k"})


def test_infer_cols_and_drop_intermediates():
    def filter_history(df: pl.DataFrame, params: dict) -> pl.DataFrame:
        d = df.with_columns(pl.col("_rsv"))  # 中间列, 不是输入依赖
        return d.select(pl.cols("ma5", "ma10"))

    assert infer_filter_history_features(filter_history) == frozenset({"ma5", "ma10"})


def test_infer_dynamic_column_name_is_uninferable():
    def filter_history(df: pl.DataFrame, params: dict) -> pl.DataFrame:
        col_name = str(params.get("col", "close"))
        return df.filter(pl.col(col_name) > 1)

    assert infer_filter_history_features(filter_history) is None


def test_infer_no_column_refs_is_uninferable():
    """df["x"] 下标风格收集不到 → 不可证明 → None (绝不声称零依赖)。"""

    def filter_history(df: pl.DataFrame, params: dict) -> pl.DataFrame:
        if df.is_empty():
            return df
        return df.filter(df["close"] > 1)

    assert infer_filter_history_features(filter_history) is None


def test_infer_pl_all_selector_is_uninferable():
    def filter_history(df: pl.DataFrame, params: dict) -> pl.DataFrame:
        return df.select(pl.all())

    assert infer_filter_history_features(filter_history) is None


def test_infer_polars_selectors_is_uninferable():
    def filter_history(df: pl.DataFrame, params: dict) -> pl.DataFrame:
        return df.with_columns((cs.numeric() * 2).name.suffix("_x"))

    assert infer_filter_history_features(filter_history) is None


def test_infer_unknown_names_filtered_out():
    def filter_history(df: pl.DataFrame, params: dict) -> pl.DataFrame:
        return df.filter(pl.col("not_a_real_column") > pl.col("close"))

    assert infer_filter_history_features(filter_history) == frozenset({"close"})


# ── 集成: 引擎加载路径 ──────────────────────────────────

_INFERABLE = '''\
META = {"id": "custom_inf_ok", "name": "ok", "description": "", "tags": []}

import polars as pl


def filter_history(df: pl.DataFrame, params: dict) -> pl.DataFrame:
    return df.filter((pl.col("close") > pl.col("ma20")) & (pl.col("kdj_k") < 25))
'''

_DYNAMIC = '''\
META = {"id": "custom_inf_dyn", "name": "dyn", "description": "", "tags": []}

import polars as pl


def filter_history(df: pl.DataFrame, params: dict) -> pl.DataFrame:
    col_name = str(params.get("col", "close"))
    return df.filter(pl.col(col_name) > 1)
'''


def _engine_with(tmp_path: Path, source: str):
    # 引擎只 glob 各 strategy_dir 顶层的 *.py, 必须传叶子目录。
    d = tmp_path / "custom"
    d.mkdir(parents=True)
    (d / f"{_meta_id(source)}.py").write_text(source, encoding="utf-8")
    return StrategyEngine(strategy_dirs=[d])


def _meta_id(source: str) -> str:
    return source.split('"id": "')[1].split('"')[0]


def test_engine_infers_features_when_undeclared(tmp_path):
    engine = _engine_with(tmp_path, _INFERABLE)
    assert not engine.load_errors()
    s = engine.get("custom_inf_ok")
    assert s is not None
    assert s.required_features == frozenset({"close", "ma20", "kdj_k"})


def test_engine_keeps_fallback_for_dynamic(tmp_path):
    engine = _engine_with(tmp_path, _DYNAMIC)
    assert not engine.load_errors()
    s = engine.get("custom_inf_dyn")
    assert s is not None
    # 不可证明 → 不推导, required_features 保持空 (回测层继续全量兜底)
    assert s.required_features == frozenset()


def test_engine_explicit_declaration_wins(tmp_path):
    source = _INFERABLE.replace(
        'META = {"id": "custom_inf_ok"',
        'REQUIRED_FEATURES = ["close"]\nMETA = {"id": "custom_inf_ok"',
    )
    engine = _engine_with(tmp_path, source)
    assert not engine.load_errors()
    assert engine.get("custom_inf_ok").required_features == frozenset({"close"})
