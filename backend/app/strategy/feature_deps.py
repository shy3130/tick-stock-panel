"""filter_history 列依赖的 AST 推导 — REQUIRED_FEATURES 缺失时的自动补全。

背景: 声明了 filter_history (动态 Python 过滤) 但没有声明 REQUIRED_FEATURES 的
策略, 回测无法静态推断需要哪些列, 只能退回"计算全部指标"(backtest/strategy.py
的 full_fallback, 结果正确但白白多算)。本模块对函数体做 AST 静态追踪, 收集
列引用常量, 让绝大多数此类策略拿到精确依赖, 消灭全量兜底。

fail-closed 契约: 任何无法证明的形态 — 动态列名、pl.all()/polars.selectors、
完全收集不到列引用 — 一律返回 None, 调用方保持原有全量兜底。宁可慢,
绝不漏列 (漏列会静默算错, 是金融数据的 P0 事故形态)。
"""
from __future__ import annotations

import ast
import inspect
import logging
import textwrap

logger = logging.getLogger(__name__)

# 列引用形态: <...>.col("x") / 裸 col("x") / pl.cols("a", "b")。
_COL_FORMS = ("pl.col", "col")
_COLS_FORMS = ("pl.cols", "cols")
# 列名动态函数 (polars 表达式里按正则/前后缀选列): 出现即不可推导。
_DYNAMIC_NAME_FUNCS = frozenset({"contains", "starts_with", "ends_with", "matches"})


def _dotted(func: ast.expr) -> str | None:
    """AST 调用目标 → 点链名 ("pl.col" / "col" / "cs.numeric" / None)。"""
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        base = _dotted(func.value)
        return f"{base}.{func.attr}" if base else func.attr
    return None


def _is_dynamic_name_expr(expr: ast.expr) -> bool:
    """col(...) 参数是否为动态列名 (非常量字符串)。"""
    return not (isinstance(expr, ast.Constant) and isinstance(expr.value, str))


def _collect_from_source(source: str) -> tuple[set[str], bool]:
    """返回 (收集到的列名, 是否存在不可证明形态)。"""
    tree = ast.parse(source)
    names: set[str] = set()
    uninferable = False
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        dotted = _dotted(node.func)
        if dotted is None:
            continue
        # pl.all() 是列选择器 (裸 all(...) 是 python 内建, 与列无关, 忽略);
        # polars.selectors (import polars.selectors as cs; cs.numeric()) 全族同样不可推导。
        if dotted == "pl.all" or dotted.startswith("cs."):
            uninferable = True
        elif dotted in _COL_FORMS:
            if not node.args:
                continue
            if _is_dynamic_name_expr(node.args[0]):
                # 动态列名: col(f"{p}_col") / col(name_var) — 漏掉就是错列。
                uninferable = True
            else:
                assert isinstance(node.args[0], ast.Constant)
                names.add(node.args[0].value)
        elif dotted in _COLS_FORMS:
            for arg in node.args:
                if _is_dynamic_name_expr(arg):
                    uninferable = True
                else:
                    assert isinstance(arg, ast.Constant)
                    names.add(arg.value)
    return names, uninferable


def infer_filter_history_features(fn) -> frozenset[str] | None:
    """推导 filter_history 引用的输入列; None = 不可推导 (调用方保持全量兜底)。

    只保留已知输入列 (enriched 存储/指标/证券维表), `_` 前缀的中间列
    (函数内 with_columns 先建后用的临时列) 不是输入依赖, 剔除。
    """
    from app.indicators.pipeline import ENRICHED_STORAGE_COLS, INDICATOR_COLUMNS

    try:
        # 嵌套定义的函数 getsource 带原始缩进, 直接 parse 会 IndentationError。
        source = textwrap.dedent(inspect.getsource(fn))
    except (OSError, TypeError) as e:
        logger.debug("feature inference: no source for %r: %s", getattr(fn, "__name__", fn), e)
        return None
    try:
        names, uninferable = _collect_from_source(source)
    except SyntaxError:
        return None  # 已加载模块不应有语法错误; 防御性 fail-closed
    if uninferable or not names:
        # 收集不到列引用 (如 df["x"] 下标风格) 与存在动态形态同样不可证明。
        return None
    known = set(ENRICHED_STORAGE_COLS) | set(INDICATOR_COLUMNS) | {"name", "total_shares", "float_shares"}
    features = frozenset(n for n in names if n in known and not n.startswith("_"))
    return features or None
