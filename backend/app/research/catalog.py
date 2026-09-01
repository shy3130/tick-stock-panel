"""The single public registry for all 19 research factors.

Engineering status mirrors TODO progress only, never verdict or promotion.
The 11 full-market factors bind their controlled executor factories here;
this module is the ONLY registry — adapters resolve exclusively through it.
"""

from __future__ import annotations

import importlib
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel

from app.research.contracts import (
    PARAMETER_MODELS,
    CatalogEntry,
    DataRequirementKind,
    EngineeringStatus,
    FactorCategory,
    FactorDetail,
    ParameterField,
    ResultProfile,
    RunScopeType,
)


@dataclass(frozen=True)
class FactorDefinition:
    id: str
    title: str
    category: FactorCategory
    description: str
    engineering_status: EngineeringStatus
    supported_scopes: tuple[RunScopeType, ...]
    result_profile: ResultProfile
    request_model: type[BaseModel]
    data_requirements: tuple[DataRequirementKind, ...]
    docs: tuple[str, ...] = ("docs/TODO.md",)
    known_gaps: tuple[str, ...] = ()
    max_symbols: int | None = None
    min_symbols: int = 1
    full_market_executor: Callable[[], Any] | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


_ROWS: tuple[tuple[Any, ...], ...] = (
    (
        "n-shape",
        "N字金凤凰",
        "pattern",
        "首板缩量回调后二次启动形态研究",
        "completed",
        ("symbols",),
        "event_signal",
        ("canonical",),
        1000,
        1,
    ),
    (
        "mtf-direction",
        "15分钟方向+5分钟确认",
        "intraday",
        "多周期 ordered-trans 状态研究",
        "completed",
        ("symbols",),
        "event_signal",
        ("minutes", "trans"),
        50,
        1,
    ),
    (
        "weak-to-strong",
        "弱转强",
        "event",
        "前日放量涨停后次日高开封板研究",
        "partial",
        ("symbols",),
        "event_signal",
        ("canonical", "markets"),
        100,
        1,
    ),
    (
        "volume-breakout",
        "量价序列突破",
        "pattern",
        "放量分歧缩量整理区间突破研究",
        "partial",
        ("symbols",),
        "arm_comparison",
        ("canonical", "universe", "calendar"),
        1000,
        1,
    ),
    (
        "macd-arms",
        "MACD多阶段",
        "trend",
        "冻结五 arms 与阶段基线比较研究",
        "partial",
        ("symbols", "full_market"),
        "arm_comparison",
        ("canonical", "index_daily"),
        1000,
        1,
    ),
    (
        "single-yang-no-break",
        "单阳不破",
        "pattern",
        "首板涨停后缩量回调研究",
        "partial",
        ("symbols", "full_market"),
        "arm_comparison",
        ("canonical", "markets"),
        1000,
        1,
    ),
    (
        "zuoyi-defense",
        "左一K线防守位",
        "pattern",
        "移动止盈持仓防守位研究",
        "completed",
        ("symbols",),
        "arm_comparison",
        ("canonical", "markets"),
        500,
        1,
    ),
    (
        "daily-open-anchor",
        "日线开盘价锚定",
        "pattern",
        "开盘价锚定入场过滤器研究",
        "partial",
        ("symbols",),
        "arm_comparison",
        ("canonical",),
        200,
        1,
    ),
    (
        "hold-firm",
        "坚定持有四形态",
        "pattern",
        "四类坚定持有形态研究",
        "partial",
        ("symbols", "full_market"),
        "arm_comparison",
        ("canonical", "markets", "universe"),
        200,
        1,
    ),
    (
        "dugu-trend",
        "独孤趋势",
        "trend",
        "冻结多阶段趋势检测研究",
        "partial",
        ("symbols", "full_market"),
        "arm_comparison",
        ("canonical", "markets"),
        200,
        1,
    ),
    (
        "mera",
        "MERA路由",
        "retrieval",
        "降维检索相似状态邻居标签路由",
        "partial",
        ("symbols", "full_market"),
        "retrieval",
        ("canonical",),
        200,
        30,
    ),
    (
        "pre-surge",
        "大涨前四特征",
        "event",
        "涨停资格等四项大涨前特征研究",
        "partial",
        ("symbols", "full_market"),
        "arm_comparison",
        ("canonical", "markets", "universe"),
        200,
        1,
    ),
    (
        "escape-risk",
        "S1-S10盘中逃命信号",
        "intraday",
        "卖出侧事件因子与配对基线研究",
        "partial",
        ("symbols", "full_market"),
        "event_signal",
        ("canonical", "minutes", "trans"),
        200,
        1,
    ),
    (
        "n-depth",
        "N字回调深度分档",
        "pattern",
        "因果 zigzag 回调深度分档研究",
        "partial",
        ("symbols", "full_market"),
        "arm_comparison",
        ("canonical",),
        1000,
        1,
    ),
    (
        "negative-exclusion",
        "五类负面清单",
        "exclusion",
        "V2/V4/V5 负面清单排除研究",
        "partial",
        ("symbols", "full_market"),
        "arm_comparison",
        ("canonical", "markets", "universe"),
        200,
        1,
    ),
    (
        "doji-patterns",
        "十字星形态",
        "pattern",
        "D1-D4 十字星形态研究",
        "partial",
        ("symbols", "full_market"),
        "event_signal",
        ("canonical", "markets", "universe"),
        200,
        1,
    ),
    (
        "chip-peak-patterns",
        "筹码峰五条判读",
        "pattern",
        "C1-C5 筹码峰形态研究",
        "partial",
        ("symbols",),
        "shape_distribution",
        ("canonical", "markets", "universe"),
        200,
        1,
    ),
    (
        "weekly-flagpole",
        "周线拉旗杆",
        "trend",
        "F0-F5 周线旗杆研究",
        "partial",
        ("symbols", "full_market"),
        "arm_comparison",
        ("canonical", "index_daily"),
        1000,
        1,
    ),
    (
        "escape-windows",
        "四大逃生窗口",
        "calendar",
        "六类日历锚点逃生窗口研究",
        "partial",
        ("symbols",),
        "calendar_effect",
        ("canonical", "calendar", "universe", "index_daily"),
        None,
        1,
    ),
)

_FULL_MARKET_ADAPTER_SPECS: dict[str, tuple[str, str]] = {
    "macd-arms": ("app.services.full_market_adapters.macd", "MacdArmsAdapter"),
    "single-yang-no-break": (
        "app.services.full_market_adapters.single_yang",
        "SingleYangFullMarketAdapter",
    ),
    "hold-firm": ("app.services.full_market_adapters.hold_firm", "HoldFirmAdapter"),
    "dugu-trend": ("app.services.full_market_adapters.dugu", "DuguTrendAdapter"),
    "mera": ("app.services.full_market_adapters.mera", "MeraAdapter"),
    "pre-surge": ("app.services.full_market_adapters.pre_surge", "PreSurgeAdapter"),
    "escape-risk": ("app.services.full_market_adapters.escape_risk", "EscapeRiskAdapter"),
    "n-depth": ("app.services.full_market_adapters.n_depth", "NDepthAdapter"),
    # The public factor id stays "negative-exclusion"; full-market runs are
    # forced onto the internal negative-v5 executor (V5 class only).
    "negative-exclusion": (
        "app.services.full_market_adapters.negative_v5",
        "NegativeV5Adapter",
    ),
    "doji-patterns": ("app.services.full_market_adapters.doji", "DojiPatternsFullMarketAdapter"),
    "weekly-flagpole": (
        "app.services.full_market_adapters.weekly_flagpole",
        "WeeklyFlagpoleAdapter",
    ),
}

_FULL_MARKET_INTERNAL_NAMES: dict[str, str] = {
    factor_id: "negative-v5" if factor_id == "negative-exclusion" else factor_id
    for factor_id in _FULL_MARKET_ADAPTER_SPECS
}


def _executor_factory(factor_id: str) -> Callable[[], Any]:
    module_name, class_name = _FULL_MARKET_ADAPTER_SPECS[factor_id]

    def factory() -> Any:
        return getattr(importlib.import_module(module_name), class_name)()

    factory.__name__ = f"executor_{_FULL_MARKET_INTERNAL_NAMES[factor_id].replace('-', '_')}"
    return factory


_KNOWN_GAPS: dict[str, tuple[str, ...]] = {
    "negative-exclusion": (
        "V1/V3 capability unavailable; symbol scope exposes V2/V4/V5 only; "
        "full-market forces the internal negative-v5 executor (V5 only).",
    ),
    "escape-risk": ("Intraday reader absent: S2-S7/S10 remain explicitly censored.",),
    "escape-windows": (
        "Evaluator studies the pinned universe; scope symbols are not parameterizable.",
    ),
}


def _build(row: tuple[Any, ...]) -> FactorDefinition:
    (
        factor_id,
        title,
        category,
        description,
        status,
        scopes,
        profile,
        requirements,
        max_symbols,
        min_symbols,
    ) = row
    return FactorDefinition(
        id=factor_id,
        title=title,
        category=category,
        description=description,
        engineering_status=status,
        supported_scopes=scopes,
        result_profile=profile,
        request_model=PARAMETER_MODELS[factor_id],
        data_requirements=requirements,
        docs=("docs/TODO.md", "docs/ISSUE-48/verification.md")
        if factor_id == "escape-risk"
        else ("docs/TODO.md",),
        known_gaps=_KNOWN_GAPS.get(factor_id, ()),
        max_symbols=max_symbols,
        min_symbols=min_symbols,
        full_market_executor=(
            _executor_factory(factor_id) if factor_id in _FULL_MARKET_ADAPTER_SPECS else None
        ),
        metadata={"frozen_arms": 5, "frozen_cost_bps": 20.0} if factor_id == "macd-arms" else {},
    )


FACTOR_REGISTRY: dict[str, FactorDefinition] = {row[0]: _build(row) for row in _ROWS}

FULL_MARKET_MAPPINGS: dict[str, str] = {
    factor_id: _FULL_MARKET_INTERNAL_NAMES[factor_id]
    for factor_id, definition in FACTOR_REGISTRY.items()
    if definition.full_market_executor is not None
}


def full_market_factor_ids() -> list[str]:
    """Public factor ids with a controlled full-market executor, sorted."""
    return sorted(
        factor_id
        for factor_id, definition in FACTOR_REGISTRY.items()
        if definition.full_market_executor is not None
    )


def resolve_full_market_executor(factor_id: str) -> Any | None:
    """Instantiate the controlled full-market adapter; ``None`` when unbound."""
    definition = FACTOR_REGISTRY.get(factor_id)
    if definition is None or definition.full_market_executor is None:
        return None
    return definition.full_market_executor()


def get_factor(factor_id: str) -> FactorDefinition | None:
    return FACTOR_REGISTRY.get(factor_id)


def parameter_schema(definition: FactorDefinition) -> dict[str, Any]:
    return definition.request_model.model_json_schema()


def _widget_kind(name: str, schema: dict[str, Any]) -> str:
    spec = schema.get("properties", {}).get(name, {})
    if "anyOf" in spec:
        options = [item for item in spec["anyOf"] if item.get("type") != "null"]
        spec = options[0] if options else {}
    if spec.get("type") == "array":
        items = spec.get("items", {})
        if items.get("enum"):
            return "multi_enum"
        return "symbol_list"
    if spec.get("enum"):
        return "enum"
    python_type = spec.get("type")
    if python_type == "string":
        if spec.get("format") == "date":
            return "date"
        if name == "symbol" or name.endswith("_symbol"):
            return "symbol_list"
    if python_type == "boolean":
        return "boolean"
    if python_type == "integer":
        return "integer"
    if python_type == "number":
        return "number"
    raise ValueError(f"unsupported parameter schema for {name}: {spec}")


def parameter_fields(definition: FactorDefinition) -> list[ParameterField]:
    schema = parameter_schema(definition)
    fields: list[ParameterField] = []
    for name in schema.get("properties", {}):
        spec = schema["properties"][name]
        fields.append(
            ParameterField(
                name=name,
                kind=_widget_kind(name, schema),
                required=name in schema.get("required", []),
                default=spec.get("default"),
                options=list(spec.get("enum", [])),
            )
        )
    return fields


def catalog_entry(definition: FactorDefinition) -> CatalogEntry:
    return CatalogEntry(
        id=definition.id,
        title=definition.title,
        category=definition.category,
        description=definition.description,
        engineering_status=definition.engineering_status,
        supported_scopes=list(definition.supported_scopes),
        result_profile=definition.result_profile,
        data_requirements=list(definition.data_requirements),
        todo_status="completed" if definition.engineering_status == "completed" else "in_progress",
        docs=list(definition.docs),
        known_gaps=list(definition.known_gaps),
    )


def factor_detail(definition: FactorDefinition) -> FactorDetail:
    entry = catalog_entry(definition)
    return FactorDetail(
        **entry.model_dump(),
        parameter_schema=parameter_schema(definition),
        parameter_fields=parameter_fields(definition),
        provenance_requirements=list(definition.data_requirements),
        arms=[
            {
                "frozen_arms": definition.metadata.get("frozen_arms"),
                "frozen_cost_bps": definition.metadata.get("frozen_cost_bps"),
            }
        ]
        if definition.id == "macd-arms"
        else [],
    )


def list_factors(
    *,
    category: str | None = None,
    engineering_status: str | None = None,
    scope: str | None = None,
    query: str | None = None,
    data_status: str | None = None,
    verdict: str | None = None,
    entries: list[CatalogEntry] | None = None,
) -> list[CatalogEntry]:
    items = entries if entries is not None else [catalog_entry(f) for f in FACTOR_REGISTRY.values()]
    result: list[CatalogEntry] = []
    for entry in items:
        if category and entry.category != category:
            continue
        if engineering_status and entry.engineering_status != engineering_status:
            continue
        if scope and scope not in entry.supported_scopes:
            continue
        if data_status and entry.latest_data_status != data_status:
            continue
        if verdict and entry.latest_verdict != verdict:
            continue
        if query and query.lower() not in f"{entry.id}{entry.title}{entry.description}".lower():
            continue
        result.append(entry)
    return result
