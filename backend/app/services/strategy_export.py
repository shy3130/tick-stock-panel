"""策略公式导出。

只导出显式声明的无状态日线条件 DSL，不反解析 Python filter 函数。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal


Target = Literal["tdx", "ths"]

SUPPORTED_FIELDS = {
    "close", "open", "high", "low", "volume", "amount",
    "ma5", "ma10", "ma20", "ma60",
    "change_pct", "turnover_rate", "vol_ratio_5d",
}

FIELD_MAP = {
    "close": "C",
    "open": "O",
    "high": "H",
    "low": "L",
    "volume": "V",
    "amount": "AMOUNT",
    "ma5": "MA(C,5)",
    "ma10": "MA(C,10)",
    "ma20": "MA(C,20)",
    "ma60": "MA(C,60)",
    "change_pct": "(C/REF(C,1)-1)",
    "turnover_rate": "TURNOVER",
    "vol_ratio_5d": "V/MA(V,5)",
}

COMPARISONS = {">", ">=", "<", "<=", "=="}


@dataclass
class ExportResult:
    ok: bool
    target: Target
    formula: str = ""
    warnings: list[str] = field(default_factory=list)
    unsupported: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "ok": self.ok,
            "target": self.target,
            "formula": self.formula,
            "warnings": self.warnings,
            "unsupported": self.unsupported,
        }


class UnsupportedExport(ValueError):
    pass


def export_strategy_formula(
    strategy: Any,
    target: Target,
    expression: dict | None = None,
    conditions: list[dict] | None = None,
) -> ExportResult:
    """导出策略公式。

    ``expression``/``conditions`` 优先用于策略构建器即时导出；否则读取
    ``strategy.meta["export"]``。没有显式 DSL 的 Python 策略返回 unsupported。
    """
    if target not in {"tdx", "ths"}:
        return ExportResult(ok=False, target=target, unsupported=[f"unsupported target: {target}"])

    meta = getattr(strategy, "meta", {}) or {}
    export_cfg = meta.get("export") if isinstance(meta, dict) else None
    expr = expression
    conds = conditions
    warnings: list[str] = []

    if expr is None and conds is None:
        if not isinstance(export_cfg, dict):
            return ExportResult(ok=False, target=target, unsupported=["strategy has no META.export DSL"])
        expr = export_cfg.get("expression")
        conds = export_cfg.get("conditions")
        warnings.extend(export_cfg.get("warnings") or [])

    try:
        if conds is not None:
            body = _compile_conditions(conds)
        elif expr is not None:
            body = _compile_expr(expr)
        else:
            raise UnsupportedExport("empty export expression")
    except UnsupportedExport as e:
        return ExportResult(ok=False, target=target, unsupported=[str(e)])

    formula = _render_formula(meta, target, body, warnings)
    return ExportResult(ok=True, target=target, formula=formula, warnings=warnings)


def _compile_conditions(conditions: list[dict]) -> str:
    if not isinstance(conditions, list) or not conditions:
        raise UnsupportedExport("conditions must be a non-empty list")
    parts = [_compile_comparison(c) for c in conditions]
    return _join("AND", parts)


def _compile_expr(expr: Any) -> str:
    if not isinstance(expr, dict):
        raise UnsupportedExport("expression must be an object")
    if "all" in expr:
        items = expr["all"]
        if not isinstance(items, list) or not items:
            raise UnsupportedExport("all requires a non-empty list")
        return _join("AND", [_compile_expr(x) for x in items])
    if "any" in expr:
        items = expr["any"]
        if not isinstance(items, list) or not items:
            raise UnsupportedExport("any requires a non-empty list")
        return _join("OR", [_compile_expr(x) for x in items])
    if "not" in expr:
        return f"NOT({_compile_expr(expr['not'])})"
    fn = expr.get("fn") or expr.get("op")
    if fn in {"cross_up", "cross_down"}:
        args = expr.get("args")
        left = expr.get("left")
        right = expr.get("right")
        if isinstance(args, list) and len(args) == 2:
            left, right = args
        if left is None or right is None:
            raise UnsupportedExport(f"{fn} requires two operands")
        a = _compile_value(left)
        b = _compile_value(right)
        return f"CROSS({a},{b})" if fn == "cross_up" else f"CROSS({b},{a})"
    return _compile_comparison(expr)


def _compile_comparison(node: dict) -> str:
    if not isinstance(node, dict):
        raise UnsupportedExport("condition must be an object")
    left = node.get("left")
    op = node.get("op")
    right = node.get("right")
    if op not in COMPARISONS:
        raise UnsupportedExport(f"unsupported operator: {op}")
    lhs = _compile_value(left)
    rhs = _compile_value(right)
    op_text = "=" if op == "==" else op
    return f"{lhs}{op_text}{rhs}"


def _compile_value(value: Any) -> str:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return _format_number(float(value))
    if not isinstance(value, str):
        raise UnsupportedExport(f"unsupported value: {value!r}")
    if value.startswith("field:"):
        return _compile_field(value[len("field:"):])
    try:
        return _format_number(float(value))
    except ValueError:
        return _compile_field(value)


def _compile_field(field: str) -> str:
    if field not in SUPPORTED_FIELDS:
        raise UnsupportedExport(f"unsupported field: {field}")
    return FIELD_MAP[field]


def _join(op: str, parts: list[str]) -> str:
    if len(parts) == 1:
        return parts[0]
    return "(" + f" {op} ".join(parts) + ")"


def _format_number(value: float) -> str:
    if value.is_integer():
        return str(int(value))
    return f"{value:.10g}"


def _render_formula(meta: dict, target: Target, body: str, warnings: list[str]) -> str:
    sid = str(meta.get("id") or "strategy")
    name = str(meta.get("name") or sid)
    lines = [
        f"{{策略: {name}}}",
        f"{{ID: {sid}}}",
        f"{{Target: {target.upper()}}}",
        f"{{Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}}}",
    ]
    lines.extend(f"{{Warning: {w}}}" for w in warnings)
    lines.append(f"XG:{body};")
    return "\n".join(lines)
