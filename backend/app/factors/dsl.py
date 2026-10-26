"""因子公式 DSL 编译器 (P2)。

流水线: text → tokenizer → 递归下降解析(EBNF 见设计文档 §3.4) → AST → 语义检查
→ 依赖/预热推导 → Polars Expr。编译失败返回结构化错误 (E001-E016), 不抛裸异常。

窗口纪律 (Polars 嵌套窗口会静默产出全 null, 必须在编译期杜绝):
- 所有 ts_* 算子只向后看 (负 shift 常量层强制 E005)。
- 时序子树仅在离开时序上下文时挂一次 over("symbol"); 截面算子挂 over("date")。
- 截面算子消费含窗口的子树时, 编译为两阶段: 先把该子树物化为临时列 (单层 over),
  再对临时列做截面运算 —— frame_transform 负责按依赖顺序执行全部阶段。
- 截面算子嵌在时序窗口内 (如 ts_mean(rank(x), n)) v1 不支持, 编译期 E009 拒绝。
- 引用的注册因子(含 virtual)不内联表达式: 调用方用 materialize_scoring_columns
  物化成列, 编译产物统一以 pl.col(name) 引用; 运行期缺列即 fail-closed。
"""
from __future__ import annotations

import copy
import re
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any

import polars as pl

from app.factors.registry import factor_dependencies, get_factor

FACTOR_COLUMN = "__dsl_factor__"

# 基准列 (设计文档 §3.1); 指标列 = 注册表 base 因子, 已注册因子 id 经注册表解析。
BASE_COLUMNS: frozenset[str] = frozenset({
    "open", "high", "low", "close", "volume", "amount",
    "turnover_rate", "prev_close", "raw_close",
})

MAX_AST_DEPTH = 12
MAX_TOKENS = 200
WINDOW_MIN, WINDOW_MAX = 2, 512
DELAY_MAX = 512
POWER_ABS_MAX = 4.0
WINSORIZE_K_RANGE = (1.0, 6.0)

# 算子表: 名 -> (表达式参数个数, 常量参数名元组); 常量参数必须是数字字面量 (E003)。
OPERATORS: dict[str, tuple[int, tuple[str, ...]]] = {
    "ts_mean": (1, ("n",)),
    "ts_std": (1, ("n",)),
    "ts_sum": (1, ("n",)),
    "ts_max": (1, ("n",)),
    "ts_min": (1, ("n",)),
    "ts_delay": (1, ("n",)),
    "ts_delta": (1, ("n",)),
    "ts_rank": (1, ("n",)),
    "ts_zscore": (1, ("n",)),
    "ts_corr": (2, ("n",)),
    "ts_cov": (2, ("n",)),
    "ts_quantile": (1, ("n", "q")),
    "decay_linear": (1, ("n",)),
    "rank": (1, ()),
    "zscore": (1, ()),
    "winsorize": (1, ("k",)),  # k 可省略, 默认 3
    "power": (1, ("c",)),
    "clamp": (1, ("lo", "hi")),
    "if_else": (3, ()),
    "min": (2, ()),
    "max": (2, ()),
    "log": (1, ()),
    "abs": (1, ()),
    "sign": (1, ()),
    "sqrt": (1, ()),
}
TS_OPERATORS = frozenset({
    "ts_mean", "ts_std", "ts_sum", "ts_max", "ts_min", "ts_delay", "ts_delta",
    "ts_rank", "ts_zscore", "ts_corr", "ts_cov", "ts_quantile", "decay_linear",
})
CROSS_OPERATORS = frozenset({"rank", "zscore", "winsorize"})


@dataclass
class DslError:
    code: str
    message: str
    offset: int = 0
    detail: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "code": self.code,
            "message": self.message,
            "position": {"offset": self.offset, "line": 1},
            "detail": self.detail,
        }


@dataclass
class CompiledFormula:
    ok: bool
    errors: list[DslError] = field(default_factory=list)
    frame_transform: Any | None = None  # (frame: pl.DataFrame) -> pl.DataFrame | None (缺列 None = E013)
    dependencies: frozenset[str] = frozenset()  # 展开到 enriched base 列
    referenced_factors: frozenset[str] = frozenset()  # 引用的注册因子 id (含 virtual, 需物化)
    warmup_bars: int = 1
    cross_sectional: bool = False
    formula_text: str = ""


# ---------------------------------------------------------------- tokenizer

_TOKEN_RE = re.compile(
    r"\s*(?:(?P<num>\d+(?:\.\d+)?)|(?P<ident>[A-Za-z_][A-Za-z0-9_]*)|(?P<op>>=|<=|==|!=|[+\-*/><(),]))"
)
_KEYWORDS = frozenset({"and", "or", "not"})


def _tokenize(text: str) -> tuple[list[tuple[str, Any, int]], DslError | None]:
    tokens: list[tuple[str, Any, int]] = []
    pos = 0
    while pos < len(text):
        match = _TOKEN_RE.match(text, pos)
        if match is None or match.end() == pos:
            rest = text[pos:].strip()
            if not rest:
                break
            return [], DslError("E014", f"语法错误: 无法识别的字符 '{rest[0]}'", offset=pos)
        if match.group("num") is not None:
            tokens.append(("num", float(match.group("num")), match.start("num")))
        elif match.group("ident") is not None:
            tokens.append(("ident", match.group("ident"), match.start("ident")))
        else:
            tokens.append(("op", match.group("op"), match.start("op")))
        pos = match.end()
    return tokens, None


# ------------------------------------------------------------------- parser
# AST 节点: dict(kind, value, children, offset[, _constants])


class _Parser:
    _CMP = frozenset({">", ">=", "<", "<=", "==", "!="})

    def __init__(self, tokens: list[tuple[str, Any, int]], text: str) -> None:
        self.tokens = tokens
        self.text = text
        self.index = 0

    def _peek(self) -> tuple[str, Any, int] | None:
        return self.tokens[self.index] if self.index < len(self.tokens) else None

    def _next(self) -> tuple[str, Any, int]:
        token = self.tokens[self.index]
        self.index += 1
        return token

    def parse(self) -> tuple[dict | None, DslError | None]:
        if not self.tokens:
            return None, DslError("E014", "语法错误: 表达式为空", offset=0)
        node, error = self._or_expr()
        if error:
            return None, error
        if self._peek() is not None:
            _, value, offset = self._peek()
            return None, DslError("E014", f"语法错误: 多余的记号 '{value}'", offset=offset)
        return node, None

    def _or_expr(self):
        left, error = self._and_expr()
        if error:
            return None, error
        while (token := self._peek()) and token[0] == "ident" and token[1] == "or":
            self._next()
            right, error = self._and_expr()
            if error:
                return None, error
            left = {"kind": "bin", "value": "or", "children": [left, right], "offset": token[2]}
        return left, None

    def _and_expr(self):
        left, error = self._cmp_expr()
        if error:
            return None, error
        while (token := self._peek()) and token[0] == "ident" and token[1] == "and":
            self._next()
            right, error = self._cmp_expr()
            if error:
                return None, error
            left = {"kind": "bin", "value": "and", "children": [left, right], "offset": token[2]}
        return left, None

    def _cmp_expr(self):
        left, error = self._add_expr()
        if error:
            return None, error
        while (token := self._peek()) and token[0] == "op" and token[1] in self._CMP:
            self._next()
            right, error = self._add_expr()
            if error:
                return None, error
            left = {"kind": "bin", "value": token[1], "children": [left, right], "offset": token[2]}
        return left, None

    def _add_expr(self):
        left, error = self._mul_expr()
        if error:
            return None, error
        while (token := self._peek()) and token[0] == "op" and token[1] in ("+", "-"):
            self._next()
            right, error = self._mul_expr()
            if error:
                return None, error
            left = {"kind": "bin", "value": token[1], "children": [left, right], "offset": token[2]}
        return left, None

    def _mul_expr(self):
        left, error = self._unary()
        if error:
            return None, error
        while (token := self._peek()) and token[0] == "op" and token[1] in ("*", "/"):
            self._next()
            right, error = self._unary()
            if error:
                return None, error
            left = {"kind": "bin", "value": token[1], "children": [left, right], "offset": token[2]}
        return left, None

    def _unary(self):
        token = self._peek()
        if token and token[0] == "op" and token[1] == "-":
            self._next()
            operand, error = self._unary()
            if error:
                return None, error
            return {"kind": "unary", "value": "-", "children": [operand], "offset": token[2]}, None
        return self._primary()

    def _primary(self):
        token = self._peek()
        if token is None:
            return None, DslError("E014", "语法错误: 表达式意外结束", offset=len(self.text))
        kind, value, offset = self._next()
        if kind == "num":
            return {"kind": "num", "value": value, "children": [], "offset": offset}, None
        if kind == "ident":
            if value in _KEYWORDS:
                return None, DslError("E014", f"语法错误: 关键字 '{value}' 不能作为操作数", offset=offset)
            nxt = self._peek()
            if nxt and nxt[0] == "op" and nxt[1] == "(":
                return self._call(value, offset)
            return {"kind": "col", "value": value, "children": [], "offset": offset}, None
        if kind == "op" and value == "(":
            inner, error = self._or_expr()
            if error:
                return None, error
            closing = self._peek()
            if not (closing and closing[0] == "op" and closing[1] == ")"):
                return None, DslError("E014", "语法错误: 缺少右括号 ')'", offset=offset)
            self._next()
            return inner, None
        return None, DslError("E014", f"语法错误: 意外的记号 '{value}'", offset=offset)

    def _call(self, name: str, offset: int):
        self._next()  # consume '('
        args: list[dict] = []
        token = self._peek()
        if not (token and token[0] == "op" and token[1] == ")"):
            while True:
                arg, error = self._or_expr()
                if error:
                    return None, error
                args.append(arg)
                token = self._peek()
                if token and token[0] == "op" and token[1] == ",":
                    self._next()
                    continue
                break
        closing = self._peek()
        if not (closing and closing[0] == "op" and closing[1] == ")"):
            return None, DslError("E014", f"语法错误: 函数 '{name}' 缺少右括号", offset=offset)
        self._next()
        return {"kind": "call", "value": name, "children": args, "offset": offset}, None


# ---------------------------------------------------------- semantic checks


def _ast_depth(node: dict) -> int:
    if not node["children"]:
        return 1
    return 1 + max(_ast_depth(child) for child in node["children"])


def _collect_identifiers(node: dict, found: set[str]) -> None:
    if node["kind"] == "col":
        found.add(node["value"])
    for child in node["children"]:
        _collect_identifiers(child, found)


def _const_value(node: dict) -> float | None:
    if node["kind"] == "num":
        return float(node["value"])
    if node["kind"] == "unary" and node["value"] == "-" and node["children"][0]["kind"] == "num":
        return -float(node["children"][0]["value"])
    return None


def _check_call(node: dict, errors: list[DslError]) -> dict[str, float]:
    """检查函数签名与常量参数范围; 返回解析出的常量参数表。"""
    name = node["value"]
    args = node["children"]
    if name not in OPERATORS:
        errors.append(DslError("E002", f"未知函数: {name}", offset=node["offset"], detail={"name": name}))
        return {}
    n_expr, const_names = OPERATORS[name]
    has_optional_k = name == "winsorize"
    total_min, total_max = n_expr + (0 if has_optional_k else len(const_names)), n_expr + len(const_names)
    if not (total_min <= len(args) <= total_max):
        errors.append(DslError(
            "E003", f"函数 {name} 参数数量不符: 期望 {total_min}~{total_max} 个, 实际 {len(args)}",
            offset=node["offset"], detail={"name": name, "args": len(args)},
        ))
        return {}
    constants: dict[str, float] = {}
    for index, const_name in enumerate(const_names):
        arg_index = n_expr + index
        if arg_index >= len(args):
            # 可选常量参数被省略 (winsorize 的 k, 默认 3) —
            # 此前这里直接 IndexError, winsorize(x) 完全不可用。
            break
        arg = args[arg_index]
        value = _const_value(arg)
        if value is None:
            errors.append(DslError(
                "E003", f"函数 {name} 的参数 {const_name} 必须是数字常量",
                offset=arg["offset"], detail={"name": name, "param": const_name},
            ))
            continue
        constants[const_name] = value
    if "n" in constants:
        n_value = constants["n"]
        if n_value != int(n_value):
            errors.append(DslError("E004", "窗口参数必须是整数", offset=node["offset"], detail={"n": n_value}))
        else:
            n_int = int(n_value)
            if n_int < 0 and name in ("ts_delay", "ts_delta"):
                errors.append(DslError(
                    "E005", f"负 shift: {name} 的 n 必须 ≥ 0 (负数即未来函数)",
                    offset=node["offset"], detail={"n": n_int},
                ))
            elif name == "ts_delay" and not (1 <= n_int <= DELAY_MAX):
                errors.append(DslError("E004", f"ts_delay 的 n 必须在 [1,{DELAY_MAX}] 内", offset=node["offset"], detail={"n": n_int}))
            elif name == "ts_delta" and not (0 <= n_int <= DELAY_MAX):
                errors.append(DslError("E004", f"ts_delta 的 n 必须在 [0,{DELAY_MAX}] 内", offset=node["offset"], detail={"n": n_int}))
            elif name not in ("ts_delay", "ts_delta") and not (WINDOW_MIN <= n_int <= WINDOW_MAX):
                errors.append(DslError(
                    "E004", f"窗口 n 必须在 [{WINDOW_MIN},{WINDOW_MAX}] 内", offset=node["offset"], detail={"n": n_int},
                ))
    if "q" in constants and not (0.0 < constants["q"] < 1.0):
        errors.append(DslError("E004", "ts_quantile 的 q 必须在 (0,1) 开区间内", offset=node["offset"], detail={"q": constants["q"]}))
    if "c" in constants and abs(constants["c"]) > POWER_ABS_MAX:
        errors.append(DslError("E010", f"power 指数 |c| ≤ {POWER_ABS_MAX}", offset=node["offset"], detail={"c": constants["c"]}))
    if "k" in constants and not (WINSORIZE_K_RANGE[0] <= constants["k"] <= WINSORIZE_K_RANGE[1]):
        errors.append(DslError("E011", "winsorize 的 k 必须在 [1,6] 内", offset=node["offset"], detail={"k": constants["k"]}))
    if "lo" in constants and "hi" in constants and constants["lo"] > constants["hi"]:
        errors.append(DslError("E003", "clamp 的 lo 不能大于 hi", offset=node["offset"]))
    return constants


def _semantic_walk(node: dict, errors: list[DslError], constants_by_call: dict[int, dict]) -> None:
    if node["kind"] == "call":
        constants_by_call[id(node)] = _check_call(node, errors)
        for child in node["children"]:
            _semantic_walk(child, errors, constants_by_call)
        return
    if node["kind"] == "bin" and node["value"] == "/":
        right = node["children"][1]
        if _const_value(right) == 0:
            errors.append(DslError("E008", "静态除零: 分母为常量 0", offset=right["offset"]))
    for child in node["children"]:
        _semantic_walk(child, errors, constants_by_call)


# ------------------------------------------------------------- code generation

_CMP_METHOD = {">": "gt", ">=": "ge", "<": "lt", "<=": "le", "==": "eq", "!=": "ne"}


def _safe_div(numerator: pl.Expr, denominator: pl.Expr) -> pl.Expr:
    return (
        pl.when(denominator.is_not_null() & (denominator != 0))
        .then(numerator / denominator)
        .otherwise(None)
    )


def _rolling_apply(inner: pl.Expr, op: str, n: int, extra: dict[str, float]) -> pl.Expr:
    """对无 over 的内层序列应用窗口逻辑; 返回值同样不挂 over。"""
    if op == "ts_mean":
        return inner.rolling_mean(n, min_samples=n)
    if op == "ts_std":
        return inner.rolling_std(n, min_samples=n)
    if op == "ts_sum":
        return inner.rolling_sum(n, min_samples=n)
    if op == "ts_max":
        return inner.rolling_max(n, min_samples=n)
    if op == "ts_min":
        return inner.rolling_min(n, min_samples=n)
    if op == "ts_delay":
        return inner.shift(n)
    if op == "ts_delta":
        return inner - inner.shift(n)
    if op == "ts_rank":
        return inner.rolling_rank(n, min_samples=n)
    if op == "ts_zscore":
        mean = inner.rolling_mean(n, min_samples=n)
        std = inner.rolling_std(n, min_samples=n)
        return pl.when(std > 0).then((inner - mean) / std).otherwise(None)
    if op == "ts_quantile":
        return inner.rolling_quantile(extra.get("q", 0.5), window_size=n, min_samples=n)
    if op == "decay_linear":
        # 近端权重大: 权重 n, n-1, ..., 1, 总权 n(n+1)/2
        weighted = None
        for i in range(n):
            term = (n - i) * inner.shift(i)
            weighted = term if weighted is None else weighted + term
        assert weighted is not None
        return _safe_div(weighted, pl.lit(float(n * (n + 1) / 2)))
    raise AssertionError(op)


def _compile_node(node: dict) -> tuple[pl.Expr | None, bool, bool]:
    """返回 (expr, needs_symbol_window, is_bool)。

    needs_symbol_window=True 表示该子树含 ts 窗口逻辑但尚未挂 over;
    由非时序上下文的调用方挂 over("symbol"), 时序上下文继续向内传递。
    """
    kind = node["kind"]
    if kind == "num":
        return pl.lit(node["value"]), False, False
    if kind == "col":
        # 基准列/base 因子/虚拟因子统一以列引用; 虚拟因子由调用方物化 (运行期缺列 fail-closed)
        return pl.col(node["value"]), False, False
    if kind == "unary":
        operand, needs_window, _ = _compile_node(node["children"][0])
        if operand is None:
            return None, False, False
        return -operand, needs_window, False
    if kind == "bin":
        op = node["value"]
        left, left_window, _ = _compile_node(node["children"][0])
        right, right_window, _ = _compile_node(node["children"][1])
        if left is None or right is None:
            return None, False, False
        if left_window:
            left = left.over("symbol")
        if right_window:
            right = right.over("symbol")
        if op == "+":
            return left + right, False, False
        if op == "-":
            return left - right, False, False
        if op == "*":
            return left * right, False, False
        if op == "/":
            return _safe_div(left, right), False, False
        if op in _CMP_METHOD:
            return getattr(left, _CMP_METHOD[op])(right), False, True
        if op == "and":
            return left & right, False, True
        if op == "or":
            return left | right, False, True
        return None, False, False
    if kind == "call":
        return _compile_call(node)
    return None, False, False


def _compile_call(node: dict) -> tuple[pl.Expr | None, bool, bool]:
    name = node["value"]
    children = node["children"]
    constants: dict[str, float] = node.get("_constants", {})
    n_expr, _ = OPERATORS[name]

    if name in TS_OPERATORS:
        inner, _, _ = _compile_node(children[0])
        if inner is None:
            return None, False, False
        if name in ("ts_corr", "ts_cov"):
            second, _, _ = _compile_node(children[1])
            if second is None:
                return None, False, False
            n = int(constants.get("n", 0))
            expr = (
                pl.rolling_corr(inner, second, window_size=n)
                if name == "ts_corr"
                else pl.rolling_cov(inner, second, window_size=n)
            )
            return expr, True, False
        expr = _rolling_apply(inner, name, int(constants.get("n", 0)), constants)
        return expr, True, False

    if name in CROSS_OPERATORS:
        inner, inner_window, _ = _compile_node(children[0])
        if inner is None:
            return None, False, False
        if inner_window:
            inner = inner.over("symbol")
        if name == "rank":
            count = inner.count().over("date")
            return inner.rank(method="average").over("date") / count, False, False
        if name == "zscore":
            mean = inner.mean().over("date")
            std = inner.std().over("date")
            return pl.when(std > 0).then((inner - mean) / std).otherwise(None), False, False
        k = constants.get("k", 3.0)
        mean = inner.mean().over("date")
        std = inner.std().over("date")
        return inner.clip(mean - k * std, mean + k * std), False, False

    if name == "if_else":
        cond, cond_window, _ = _compile_node(children[0])
        then_expr, then_window, _ = _compile_node(children[1])
        else_expr, else_window, _ = _compile_node(children[2])
        if cond is None or then_expr is None or else_expr is None:
            return None, False, False
        if cond_window:
            cond = cond.over("symbol")
        if then_window:
            then_expr = then_expr.over("symbol")
        if else_window:
            else_expr = else_expr.over("symbol")
        return pl.when(cond).then(then_expr).otherwise(else_expr), False, False

    args: list[pl.Expr | None] = []
    arg_windows: list[bool] = []
    for index in range(n_expr):
        arg, arg_window, _ = _compile_node(children[index])
        args.append(arg)
        arg_windows.append(arg_window)
    if any(arg is None for arg in args):
        return None, False, False
    resolved: list[pl.Expr] = []
    for arg, arg_window in zip(args, arg_windows, strict=True):
        resolved.append(arg.over("symbol") if arg_window else arg)
    first = resolved[0]
    if name == "log":
        return pl.when(first > 0).then(first.log()).otherwise(None), False, False
    if name == "abs":
        return first.abs(), False, False
    if name == "sign":
        return first.sign(), False, False
    if name == "sqrt":
        return pl.when(first >= 0).then(first.sqrt()).otherwise(None), False, False
    if name == "power":
        return first.pow(constants.get("c", 1.0)), False, False
    if name == "clamp":
        return first.clip(constants.get("lo"), constants.get("hi")), False, False
    if name == "min":
        return pl.min_horizontal(*resolved), False, False
    if name == "max":
        return pl.max_horizontal(*resolved), False, False
    return None, False, False


def compile_formula(text: str) -> CompiledFormula:
    """编译公式文本; 永不抛异常, 失败以 errors 表达 (fail-closed)。"""
    if not isinstance(text, str) or not text.strip():
        return CompiledFormula(ok=False, errors=[DslError("E014", "语法错误: 表达式为空")], formula_text=text)

    tokens, tokenize_error = _tokenize(text)
    errors: list[DslError] = [tokenize_error] if tokenize_error else []
    if len(tokens) > MAX_TOKENS:
        errors.append(DslError("E007", f"规模超限: token 数 {len(tokens)} > {MAX_TOKENS}"))
    if errors:
        return CompiledFormula(ok=False, errors=errors, formula_text=text)

    ast, parse_error = _Parser(tokens, text).parse()
    if parse_error:
        return CompiledFormula(ok=False, errors=[parse_error], formula_text=text)

    if _ast_depth(ast) > MAX_AST_DEPTH:
        errors.append(DslError("E006", f"嵌套深度超限: AST 深度 {_ast_depth(ast)} > {MAX_AST_DEPTH}"))

    identifiers: set[str] = set()
    _collect_identifiers(ast, identifiers)
    if not identifiers:
        errors.append(DslError("E016", "常量表达式: 公式必须引用至少一个数据列或因子"))

    for name in sorted(identifiers):
        if name not in BASE_COLUMNS and get_factor(name) is None:
            errors.append(DslError("E001", f"未知标识符: {name}", detail={"name": name}))

    constants_by_call: dict[int, dict] = {}
    _semantic_walk(ast, errors, constants_by_call)

    dependencies: set[str] = set()
    referenced_factors: set[str] = set()
    warmup = 1
    cross_sectional = False
    for name in identifiers:
        if name in BASE_COLUMNS:
            dependencies.add(name)
            continue
        spec = get_factor(name)
        if spec is None:
            continue
        referenced_factors.add(name)
        dependencies.update(factor_dependencies([name]))
        warmup = max(warmup, spec.warmup_bars)

    for node_constants in constants_by_call.values():
        n_value = node_constants.get("n")
        if n_value is not None and n_value == int(n_value) and int(n_value) > 0:
            warmup = max(warmup, int(n_value) + 1)

    def _find_cross(node: dict) -> None:
        nonlocal cross_sectional
        if node["kind"] == "call" and node["value"] in CROSS_OPERATORS:
            cross_sectional = True
        for child in node["children"]:
            _find_cross(child)

    _find_cross(ast)

    if errors:
        return CompiledFormula(
            ok=False, errors=errors, dependencies=frozenset(dependencies),
            referenced_factors=frozenset(referenced_factors),
            warmup_bars=warmup, cross_sectional=cross_sectional, formula_text=text,
        )

    # 挂常量表必须在任何 deepcopy 之前 (deepcopy 携带 _constants; 事后按 id() 重挂会失联)
    def _attach(node: dict) -> None:
        if node["kind"] == "call":
            node["_constants"] = constants_by_call.get(id(node), {})
        for child in node["children"]:
            _attach(child)

    _attach(ast)

    # 阶段一: 校验并拒绝"截面算子嵌在时序窗口内" (无法单层 over 表达)
    def _contains_cross(node: dict) -> bool:
        if node["kind"] == "call" and node["value"] in CROSS_OPERATORS:
            return True
        return any(_contains_cross(child) for child in node["children"])

    def _reject_cross_in_ts(node: dict) -> None:
        if node["kind"] == "call" and node["value"] in TS_OPERATORS:
            for child in node["children"]:
                if _contains_cross(child):
                    errors.append(DslError(
                        "E009",
                        f"截面算子不能嵌在时序窗口内: {node['value']}(...) 的参数含 rank/zscore/winsorize",
                        offset=node["offset"],
                    ))
                    return
        for child in node["children"]:
            _reject_cross_in_ts(child)

    _reject_cross_in_ts(ast)
    if errors:
        return CompiledFormula(
            ok=False, errors=errors, dependencies=frozenset(dependencies),
            referenced_factors=frozenset(referenced_factors),
            warmup_bars=warmup, cross_sectional=cross_sectional, formula_text=text,
        )

    # 阶段二: 提取截面算子的含窗口子树为临时列 (Polars 嵌套窗口会静默全 null)
    # worklist 逐层下钻; temps 后进先出反转即依赖顺序 (深层先算)。
    def _needs_symbol_window(node: dict) -> bool:
        kind = node["kind"]
        if kind in ("num", "col"):
            return False
        if kind == "unary":
            return _needs_symbol_window(node["children"][0])
        if node["kind"] == "call" and node["value"] in TS_OPERATORS:
            return True
        return any(_needs_symbol_window(child) for child in node["children"])

    def _has_any_over(node: dict) -> bool:
        # 含时序窗口 或 含截面算子(编译后自带 over("date")) 的子树都不能直接进截面上下文
        return _needs_symbol_window(node) or _contains_cross(node)

    temp_roots: list[dict] = []
    pending: list[dict] = [ast]
    while pending:
        current = pending.pop(0)
        if current.get("kind") == "call" and current.get("value") in CROSS_OPERATORS:
            operand = current["children"][0]
            if _has_any_over(operand):
                alias = f"__tsfx_{len(temp_roots)}__"
                current["children"][0] = {"kind": "col", "value": alias, "children": [], "offset": operand["offset"]}
                temp_roots.append({"alias": alias, "root": copy.deepcopy(operand)})
                pending.append(temp_roots[-1]["root"])
                continue  # 操作数已替换为临时列, 不再下钻原子树
        pending.extend(current.get("children", []))

    # 阶段三: 编译最终表达式与临时列表达式 (按依赖顺序: 深层在前)
    # _constants 已在 deepcopy 前挂载并被复制携带, 不得按 id() 重挂 (复制后 id 失联)
    temp_exprs: list[pl.Expr] = []
    for item in reversed(temp_roots):
        root = copy.deepcopy(item["root"])
        expr, needs_window, _ = _compile_node(root)
        if expr is None:
            errors.append(DslError("E009", f"无法编译临时列: {item['alias']}"))
            continue
        if needs_window:
            expr = expr.over("symbol")
        temp_exprs.append(expr.alias(item["alias"]))

    final_ast = copy.deepcopy(ast)
    compiled, needs_window, is_bool = _compile_node(final_ast)
    if compiled is None or errors:
        return CompiledFormula(
            ok=False,
            errors=errors or [DslError("E009", "产出类型非法: 无法编译为数值表达式")],
            dependencies=frozenset(dependencies),
            referenced_factors=frozenset(referenced_factors),
            warmup_bars=warmup, cross_sectional=cross_sectional, formula_text=text,
        )
    if needs_window:
        compiled = compiled.over("symbol")
    if is_bool:
        compiled = compiled.cast(pl.Float64)

    # 运行期帧变换: 检查全部引用列 (基准依赖 + 引用因子) 存在, 否则 None (E013 fail-closed)
    required_columns = set(dependencies) | set(referenced_factors)
    staged_exprs = temp_exprs  # 依赖顺序已排

    def frame_transform(frame: pl.DataFrame) -> pl.DataFrame | None:
        if not required_columns.issubset(set(frame.columns)):
            return None
        result = frame
        if staged_exprs:
            result = result.with_columns(staged_exprs)
        return result.with_columns(compiled.alias(FACTOR_COLUMN))

    return CompiledFormula(
        ok=True,
        errors=[],
        frame_transform=frame_transform,
        dependencies=frozenset(dependencies),
        referenced_factors=frozenset(referenced_factors),
        warmup_bars=warmup,
        cross_sectional=cross_sectional,
        formula_text=text,
    )


@lru_cache(maxsize=256)
def compile_formula_cached(text: str) -> CompiledFormula:
    """带 LRU 缓存的编译入口 (公式文本 → 编译产物, 设计文档 §3.3)。

    CompiledFormula 为不可变值对象 (frame_transform 闭包只读), 缓存共享安全。
    """
    return compile_formula(text)
