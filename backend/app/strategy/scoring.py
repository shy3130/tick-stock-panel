"""策略评分字段解析。"""
from __future__ import annotations

from collections.abc import Collection, Mapping
from typing import Any

import polars as pl


VIRTUAL_SCORING_DEPENDENCIES: dict[str, frozenset[str]] = {
    "ma20_bias": frozenset({"close", "ma20"}),
}


def scoring_dependencies(scoring: Mapping[str, Any]) -> set[str]:
    """把受控虚拟评分字段展开为实际数据依赖。"""
    dependencies: set[str] = set()
    for name, weight in scoring.items():
        if not weight:
            continue
        dependencies.update(VIRTUAL_SCORING_DEPENDENCIES.get(str(name), {str(name)}))
    return dependencies


def scoring_value_expr(columns: Collection[str], name: str) -> pl.Expr | None:
    """返回评分值表达式；依赖不完整时返回 None。"""
    available = set(columns)
    if name in available:
        return pl.col(name)
    dependencies = VIRTUAL_SCORING_DEPENDENCIES.get(name)
    if dependencies is None or not dependencies.issubset(available):
        return None
    if name == "ma20_bias":
        return pl.when(pl.col("ma20") != 0).then(
            pl.col("close") / pl.col("ma20") - 1.0
        ).otherwise(None)
    return None
