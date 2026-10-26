"""filter_history 列依赖的 AST 推导 — REQUIRED_FEATURES 缺失时的自动补全。

背景: 声明了 filter_history (动态 Python 过滤) 但没有声明 REQUIRED_FEATURES 的
策略, 回测无法静态推断需要哪些列, 只能退回"计算全部指标"(backtest/strategy.py
的 full_fallback, 结果正确但白白多算)。本模块对策略文件及其 `_*.py` 依赖文件
做 AST 静态追踪, 收集列引用常量 (含模块级辅助函数), 让绝大多数此类策略拿到
精确依赖, 消灭全量兜底。

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
    """推导单个 filter_history 函数引用的输入列; None = 不可推导。

    只看函数自身源码 — 覆盖不了"同模块辅助函数/`_*.py` 依赖文件里硬编码列"
    的形态, 引擎路径请用 infer_features_from_sources (整家族文件一起推导)。
    """
    try:
        source = textwrap.dedent(inspect.getsource(fn))
    except (OSError, TypeError) as e:
        logger.debug("feature inference: no source for %r: %s", getattr(fn, "__name__", fn), e)
        return None
    return infer_features_from_sources([source])


def infer_features_from_sources(sources) -> frozenset[str] | None:
    """对一组源码 (策略主文件 + `_*.py` 依赖) 整体推导输入列。

    家族内任一文件出现不可证明形态 (动态列名/pl.all/cs.*) 即整体返回 None —
    辅助函数漏列会让回测帧缺列, 宁可整体回退全量兜底。
    其余 fail-closed 契约与单函数版一致。
    """
    from app.indicators.pipeline import ENRICHED_STORAGE_COLS, INDICATOR_COLUMNS

    names: set[str] = set()
    for raw in sources:
        try:
            collected, uninferable = _collect_from_source(textwrap.dedent(raw))
        except SyntaxError:
            return None  # 家族文件解析失败按不可证明处理
        if uninferable:
            return None
        names |= collected
    if not names:
        # 收集不到列引用 (如 df["x"] 下标风格) 同样不可证明。
        return None
    known = set(ENRICHED_STORAGE_COLS) | set(INDICATOR_COLUMNS) | {"name", "total_shares", "float_shares"}
    features = frozenset(n for n in names if n in known and not n.startswith("_"))
    return features or None
