"""T2 表达式级 Python — 自定义因子的中间层 (P3)。

「只返回 Polars 表达式」的受限 Python: 源码先过 AST 白名单文法校验
(只有赋值与表达式语句、名字仅限 ctx/pl 与已赋值局部量、禁 import/def/
lambda/推导式/下划线开头属性), 再在 {ctx, pl} + 空 builtins 的命名空间里
求值, 结果必须是单个 pl.Expr。

- 依赖提取: ctx.col("x") 构造期自证记录 (与 P1a 推导同目的, 但无需推导
  — 引用即登记), 进 FactorSpec.dependencies → 回测计划/P2b 缓存键同源;
- warmup: 从窗口调用常量保守推断 (rolling_* 的 window_size 与 ewm 的
  span/alpha 沿链路求和 +1)。只多不少 — 预热多算是安全方向, 少算失真;
- 安全: 文法白名单 + 方法黑名单 (map_*/apply/read_*/scan_* 等), 与引擎
  策略安全校验同一威胁模型 (用户自建定义, 防意外越权而非对抗)。

三档编译层: T1 DSL (最强受限, 一切可静态分析) < 本层 T2 (表达式级自由,
依赖可追踪) < T3 自由 Python 策略 (现状保留, 第三方导入成真时再子进程)。
"""
from __future__ import annotations

import ast
import logging
import textwrap
from dataclasses import dataclass, field
from functools import lru_cache

import polars as pl

from app.factors.dsl import DslError

logger = logging.getLogger(__name__)

_MAX_SOURCE_CHARS = 4000
_MAX_STATEMENTS = 30

# 文法白名单: 表达式节点 (方法链/算术/比较/三元/切片常量)
_ALLOWED_EXPR_NODES = (
    ast.Expression, ast.Expr, ast.Assign, ast.Name, ast.Attribute, ast.Call,
    ast.BinOp, ast.UnaryOp, ast.BoolOp, ast.Compare, ast.IfExp,
    ast.Constant, ast.Tuple, ast.List, ast.Subscript, ast.Slice,
)
# 会被调度任意 Python 回调或触发 IO 的 polars 面 — 一律拒绝。
_FORBIDDEN_ATTRS = frozenset({
    "map_batches", "map_elements", "apply", "reinterpret",
    "rolling_apply", "cumulative_eval",
})
_FORBIDDEN_PL_PREFIXES = ("read_", "scan_")
_ROOT_NAMES = frozenset({"ctx", "pl"})
# 窗口调用 → warmup 推断 (span=2/alpha-1 由 alpha 折算)
_WINDOW_METHODS = frozenset({
    "rolling_min", "rolling_max", "rolling_mean", "rolling_sum",
    "rolling_std", "rolling_var", "rolling_median", "rolling_quantile", "rolling_skew",
})


@dataclass(frozen=True)
class CompiledPyExpr:
    ok: bool
    errors: list[DslError] = field(default_factory=list)
    expr: pl.Expr | None = None
    dependencies: frozenset[str] = frozenset()
    referenced_factors: frozenset[str] = frozenset()
    warmup_bars: int = 1
    cross_sectional: bool = False
    formula_text: str = ""

    def frame_transform(self, frame: pl.DataFrame) -> pl.DataFrame | None:
        """与 DSL CompiledFormula.frame_transform 同形: 缺依赖返回 None (跳过),
        产出列别名 FACTOR_COLUMN (消费方按约定取列)。"""
        if self.expr is None:
            return None
        if not self.dependencies.issubset(set(frame.columns)):
            return None
        from app.factors.dsl import FACTOR_COLUMN

        return frame.with_columns(self.expr.alias(FACTOR_COLUMN))


class _Ctx:
    """ctx.col 构造期登记引用列 (依赖提取的单一事实来源)。"""

    def __init__(self) -> None:
        self.columns: set[str] = set()

    def col(self, name: str) -> pl.Expr:
        if not isinstance(name, str) or not name:
            raise ValueError(f"ctx.col 的列名必须是非空字符串: {name!r}")
        self.columns.add(name)
        return pl.col(name)


def _window_warmup(tree: ast.Module) -> int:
    """窗口调用常量的保守求和 (rolling 的 N + ewm 的 span/alpha 折算)。"""
    total = 0
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = func.attr if isinstance(func, ast.Attribute) else ""
        span: float | None = None
        if name in _WINDOW_METHODS:
            if node.args and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, (int, float)):
                span = float(node.args[0].value)
            for kw in node.keywords:
                if kw.arg == "window_size" and isinstance(kw.value, ast.Constant):
                    span = float(kw.value.value)
        elif name == "ewm_mean":
            for kw in node.keywords:
                if kw.arg == "span" and isinstance(kw.value, ast.Constant):
                    span = float(kw.value.value)
                elif kw.arg == "alpha" and isinstance(kw.value, ast.Constant):
                    alpha = float(kw.value.value)
                    span = 2.0 / alpha - 1.0 if 0 < alpha <= 1 else None
        if span is not None:
            total += max(int(span), 1)
    return total


def _validate_grammar(tree: ast.Module) -> list[DslError]:
    errors: list[DslError] = []
    if len(tree.body) == 0:
        return [DslError("E201", "表达式为空")]
    if len(tree.body) > _MAX_STATEMENTS:
        return [DslError("E202", f"语句数超限 (>{_MAX_STATEMENTS})")]
    assigned: set[str] = set()
    for stmt in tree.body:
        if isinstance(stmt, ast.Assign):
            for target in stmt.targets:
                if not isinstance(target, ast.Name) or not target.id.isidentifier() or target.id.startswith("_"):
                    errors.append(DslError("E203", "赋值目标必须是简单变量名 (非下划线开头)"))
                else:
                    assigned.add(target.id)
        elif not isinstance(stmt, ast.Expr):
            errors.append(DslError("E204", f"只允许赋值与表达式语句, 不允许: {type(stmt).__name__}"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute):
            if node.attr.startswith("_"):
                errors.append(DslError("E205", f"禁止访问下划线属性: {node.attr}"))
            elif node.attr in _FORBIDDEN_ATTRS:
                errors.append(DslError("E206", f"禁止调用会执行任意回调的方法: {node.attr}"))
            elif isinstance(node.value, ast.Name) and node.value.id == "pl" and node.attr.startswith(_FORBIDDEN_PL_PREFIXES):
                errors.append(DslError("E207", f"禁止 IO 类调用: pl.{node.attr}"))
        elif isinstance(node, ast.Name):
            if node.id not in assigned and node.id not in _ROOT_NAMES and node.id not in ("True", "False", "None"):
                errors.append(DslError("E208", f"未定义的名字 (只允许 ctx/pl 与已赋值变量): {node.id}"))
        elif isinstance(node, ast.Lambda | ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef
                        | ast.Import | ast.ImportFrom | ast.For | ast.While | ast.With | ast.Try
                        | ast.ListComp | ast.SetComp | ast.DictComp | ast.GeneratorExp | ast.Yield | ast.YieldFrom):
            errors.append(DslError("E209", f"不允许的语法: {type(node).__name__}"))
        elif not isinstance(node, (*_ALLOWED_EXPR_NODES, ast.Load, ast.Store, ast.operator, ast.unaryop, ast.boolop, ast.cmpop, ast.mod, ast.expr_context, ast.keyword)):
            errors.append(DslError("E210", f"不允许的表达式节点: {type(node).__name__}"))
    return errors


def compile_python_expression(source: str) -> CompiledPyExpr:
    """编译 T2 表达式; 永不抛异常, 失败以 errors 表达 (fail-closed)。"""
    if not isinstance(source, str) or not source.strip():
        return CompiledPyExpr(ok=False, errors=[DslError("E201", "表达式为空")], formula_text=source or "")
    if len(source) > _MAX_SOURCE_CHARS:
        return CompiledPyExpr(ok=False, errors=[DslError("E202", f"源码超长 (>{_MAX_SOURCE_CHARS} 字符)")], formula_text=source)
    try:
        tree = ast.parse(textwrap.dedent(source), mode="exec")
    except SyntaxError as e:
        return CompiledPyExpr(ok=False, errors=[DslError("E201", f"语法错误: {e.msg}")], formula_text=source)

    errors = _validate_grammar(tree)
    if errors:
        return CompiledPyExpr(ok=False, errors=errors, formula_text=source)

    # 结果节点: 末尾裸表达式优先, 否则最后一个赋值的变量
    result_node: ast.expr | None = None
    for stmt in reversed(tree.body):
        if isinstance(stmt, ast.Expr):
            result_node = stmt.value
            break
        if isinstance(stmt, ast.Assign) and isinstance(stmt.targets[0], ast.Name):
            result_node = stmt.targets[0]
            break
    if result_node is None:
        return CompiledPyExpr(ok=False, errors=[DslError("E211", "缺少结果表达式 (末尾裸表达式或赋值)")], formula_text=source)

    ctx = _Ctx()
    namespace = {"ctx": ctx, "pl": pl, "__builtins__": {}}
    padded = f"{textwrap.dedent(source)}\n__t2_result__ = {ast.unparse(result_node)}"
    try:
        exec(compile(padded, "<t2-factor>", "exec"), namespace)  # 文法白名单已过 + 空 builtins
    except Exception as e:  # noqa: BLE001 — 用户表达式求值失败统一转为编译错误
        return CompiledPyExpr(ok=False, errors=[DslError("E212", f"求值失败: {type(e).__name__}: {e}")], formula_text=source)
    result = namespace.get("__t2_result__")
    if not isinstance(result, pl.Expr):
        return CompiledPyExpr(ok=False, errors=[DslError("E213", "结果必须是单个 Polars 表达式 (pl.Expr)")], formula_text=source)
    if not ctx.columns:
        return CompiledPyExpr(ok=False, errors=[DslError("E214", "至少通过 ctx.col 引用一个输入列")], formula_text=source)

    # 引用名里命中注册表因子 id 的, 单列为 referenced_factors (成员须先物化)
    try:
        from app.factors.registry import get_factor
        referenced = frozenset(c for c in ctx.columns if get_factor(c) is not None)
    except Exception:  # noqa: BLE001 — 注册表不可用不阻断编译 (引用展开交由物化期)
        referenced = frozenset()
    return CompiledPyExpr(
        ok=True,
        expr=result,
        dependencies=frozenset(ctx.columns),
        referenced_factors=referenced,
        warmup_bars=_window_warmup(tree) + 1,
        formula_text=source,
    )


@lru_cache(maxsize=256)
def compile_python_expression_cached(source: str) -> CompiledPyExpr:
    """带 LRU 缓存的编译入口 (与 compile_formula_cached 同形)。"""
    return compile_python_expression(source)
