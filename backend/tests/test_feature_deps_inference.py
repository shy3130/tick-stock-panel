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


# ── 家族推导: 模块级辅助函数 + `_*.py` 依赖文件 ────────────
# 回归: 只解析 filter_history 函数体会漏掉这些位置硬编码的列 → 回测帧缺列
# ColumnNotFoundError。范围放宽只会多载列, 不会漏。

_HELPER = '''\
META = {"id": "custom_inf_helper", "name": "helper", "description": "", "tags": []}

import polars as pl


def _trend_ok(df: pl.DataFrame) -> pl.Expr:
    return pl.col("ma20") > pl.col("ma60")


def filter_history(df: pl.DataFrame, params: dict) -> pl.DataFrame:
    return df.filter(_trend_ok(df) & (pl.col("close") > 1))
'''

_DEP_IMPORTER = '''\
META = {"id": "custom_inf_dep", "name": "dep", "description": "", "tags": []}

import polars as pl

import _dep


def filter_history(df: pl.DataFrame, params: dict) -> pl.DataFrame:
    return df.filter(_dep.momentum_ok("rsi_6") & (pl.col("close") > 1))
'''

_DEP_FILE = '''\
import polars as pl


def momentum_ok(col_name: str) -> pl.Expr:
    # 实现固定引用 rsi_6 (col_name 参数仅供调用方语义, 不参与列选择)
    return pl.col("rsi_6") < 30
'''

_DEP_FILE_DYNAMIC = '''\
import polars as pl


def momentum_ok(col_name: str) -> pl.Expr:
    # 列名经参数传递; 是否可证明取决于调用点实参 (常量流)
    return pl.col(col_name) < 30
'''


_DEP_IMPORTER_DYNAMIC = '''\
META = {"id": "custom_inf_dep_dyn", "name": "depd", "description": "", "tags": []}

import polars as pl

import _dep


def filter_history(df: pl.DataFrame, params: dict) -> pl.DataFrame:
    col = str(params.get("col", "rsi_6"))
    return df.filter(_dep.momentum_ok(col) & (pl.col("close") > 1))
'''


def test_engine_infers_module_level_helper_columns(tmp_path):
    engine = _engine_with(tmp_path, _HELPER)
    assert not engine.load_errors()
    s = engine.get("custom_inf_helper")
    assert s is not None
    # ma20/ma60 在辅助函数里, 不在 filter_history 函数体内 — 必须仍被推导
    assert s.required_features == frozenset({"close", "ma20", "ma60"})


def test_engine_infers_dependency_file_columns(tmp_path):
    d = tmp_path / "custom"
    d.mkdir(parents=True)
    (d / "custom_inf_dep.py").write_text(_DEP_IMPORTER, encoding="utf-8")
    (d / "_dep.py").write_text(_DEP_FILE, encoding="utf-8")
    engine = StrategyEngine(strategy_dirs=[d])
    assert not engine.load_errors()
    s = engine.get("custom_inf_dep")
    assert s is not None
    # rsi_6 只存在于 `_dep.py` — 家族推导必须覆盖依赖文件
    assert s.required_features == frozenset({"close", "rsi_6"})


def test_engine_constant_flow_crosses_dependency_module(tmp_path):
    """依赖模块形参列名 + 调用点全常量 (`_dep.momentum_ok("rsi_6")`) → 可证明。"""
    d = tmp_path / "custom"
    d.mkdir(parents=True)
    (d / "custom_inf_dep.py").write_text(_DEP_IMPORTER, encoding="utf-8")
    (d / "_dep.py").write_text(_DEP_FILE_DYNAMIC, encoding="utf-8")
    engine = StrategyEngine(strategy_dirs=[d])
    assert not engine.load_errors()
    s = engine.get("custom_inf_dep")
    assert s is not None
    assert s.required_features == frozenset({"close", "rsi_6"})


def test_engine_family_fail_closed_on_dynamic_dependency(tmp_path):
    """依赖文件列名经形参传递且调用点实参是运行时变量 → 家族不可证明 → 全量兜底。"""
    d = tmp_path / "custom"
    d.mkdir(parents=True)
    (d / "custom_inf_dep_dyn.py").write_text(_DEP_IMPORTER_DYNAMIC, encoding="utf-8")
    (d / "_dep.py").write_text(_DEP_FILE_DYNAMIC, encoding="utf-8")
    engine = StrategyEngine(strategy_dirs=[d])
    assert not engine.load_errors()
    s = engine.get("custom_inf_dep_dyn")
    assert s is not None
    assert s.required_features == frozenset()


# ── 常量流: 列名经辅助函数形参传递 (P1a+) ─────────────────
# 回归: custom_v1_kanban_strict 的 _ema_expr(col, alpha) 形态 — pl.col(col)
# 参数级动态, 但全部调用点传常量; 此前整体回退全量兜底。

_HELPER_PARAM = '''\
META = {"id": "custom_cf_ok", "name": "ok", "description": "", "tags": []}

import polars as pl


def _ema(col, alpha):
    return pl.col(col).ewm_mean(alpha=alpha, adjust=False)


def filter_history(df: pl.DataFrame, params: dict) -> pl.DataFrame:
    d = df.with_columns(_ema("close", 0.5).over("symbol").alias("_e"))
    return d.filter((pl.col("close") * pl.col("float_shares")) > 1e10)
'''

_HELPER_PARAM_DYNAMIC_CALL = '''\
META = {"id": "custom_cf_dyn", "name": "d", "description": "", "tags": []}

import polars as pl


def _ema(col, alpha):
    return pl.col(col).ewm_mean(alpha=alpha, adjust=False)


def filter_history(df: pl.DataFrame, params: dict) -> pl.DataFrame:
    col = str(params.get("col", "close"))
    return df.filter(_ema(col, 0.5) > 1)
'''


def test_engine_infers_constant_flow_helper_params(tmp_path):
    engine = _engine_with(tmp_path, _HELPER_PARAM)
    assert not engine.load_errors()
    s = engine.get("custom_cf_ok")
    assert s is not None
    # 形参动态 + 调用点全常量 → 常量并入 (close 经 _ema, float_shares 直接引用);
    # AST 层不收集 .over("symbol") 的分组参数 (与表达式 root_names 口径不同,
    # symbol/date 由回测恒载, 无需进依赖)
    assert s.required_features == frozenset({"close", "float_shares"})


def test_engine_constant_flow_breaks_on_dynamic_call_site(tmp_path):
    engine = _engine_with(tmp_path, _HELPER_PARAM_DYNAMIC_CALL)
    assert not engine.load_errors()
    s = engine.get("custom_cf_dyn")
    assert s is not None
    # 调用点实参是运行时变量 → 常量流断裂 → 整体回退 (fail-closed)
    assert s.required_features == frozenset()


def test_constant_flow_supports_keyword_and_default():
    from app.strategy.feature_deps import infer_features_from_sources

    src = (
        "import polars as pl\n"
        "def _c(col='close'):\n"
        "    return pl.col(col)\n"
        "x = _c() + _c(col='volume')\n"
    )
    assert infer_features_from_sources([src]) == frozenset({"close", "volume"})
