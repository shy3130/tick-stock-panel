"""Registry-driven, literal-only screener queries.

The legacy screener still owns ``POST /run``.  This module is the small,
typed query path used by the new API; it deliberately never accepts SQL or
Polars expressions from a request.
"""
from __future__ import annotations

import math
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Literal

import polars as pl
from pydantic import BaseModel, ConfigDict, Field, StrictInt, model_validator


class ScreenerSemanticError(ValueError):
    """A well-formed request whose field/operator/value is not supported."""

    def __init__(self, location: str, reason: str) -> None:
        self.location = location
        self.reason = reason
        super().__init__(reason)


class ScreenerDataUnavailableError(RuntimeError):
    """Required data was absent or unreadable (without leaking a path)."""

    def __init__(self, fields: list[str]) -> None:
        self.fields = list(dict.fromkeys(fields))
        super().__init__("screener data unavailable")


# Short compatibility name for callers that used the initial draft.
ScreenerDataUnavailable = ScreenerDataUnavailableError


class QueryCondition(BaseModel):
    model_config = ConfigDict(extra="forbid")

    field: str
    op: str
    value: Any
    # F14: 可选条件分组 (组内 AND, 组间按 group_logic 合并); None = 隐式默认组。
    group: str | None = Field(
        default=None, min_length=1, max_length=16, pattern=r"^[A-Za-z0-9_-]+$"
    )

class QueryOrder(BaseModel):
    model_config = ConfigDict(extra="forbid")

    field: str = "change_pct"
    direction: Literal["asc", "desc"] = "desc"


class ScreenerQueryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    conditions: list[QueryCondition] = Field(min_length=1, max_length=20)
    as_of: date | None = None
    order_by: QueryOrder | None = None
    limit: StrictInt = Field(default=100, ge=1, le=500)
    # F14: 组间合并逻辑; "and" 时 group 无意义 (等价于 flat AND)。
    group_logic: Literal["and", "or"] = "and"
    # F15: 请求的聚合 facet (limit 截断前对全部命中行聚合)。
    facets: list[Literal["industry"]] = Field(default_factory=list, max_length=3)

    @model_validator(mode="after")
    def validate_value_shapes(self) -> ScreenerQueryRequest:
        # These are structural errors, intentionally left to Pydantic (422).
        for i, cond in enumerate(self.conditions):
            if cond.op == "between" and (not isinstance(cond.value, list) or len(cond.value) != 2):
                raise ValueError(f"conditions[{i}].value must contain exactly two values")
            if cond.op == "in" and (not isinstance(cond.value, list) or not 1 <= len(cond.value) <= 50):
                raise ValueError(f"conditions[{i}].value must contain 1..50 values")
        return self


@dataclass(frozen=True)
class FieldSpec:
    field: str
    label: str
    group: str
    source: str
    unit: str | None
    value_type: str
    null_policy: str
    availability: str
    ops: tuple[str, ...]
    sortable: bool
    options: tuple[str, ...] | None = None
    deps: tuple[str, ...] = ()
    materialize: Callable[[pl.DataFrame], pl.Expr] | None = None

    def metadata(self) -> dict[str, Any]:
        return {
            "field": self.field,
            "label": self.label,
            "group": self.group,
            "source": self.source,
            "unit": self.unit,
            "value_type": self.value_type,
            "null_policy": self.null_policy,
            "availability": self.availability,
            "ops": list(self.ops),
            "sortable": self.sortable,
            "options": (
                [{"value": value, "label": _BOARD_LABELS.get(value, value)} for value in self.options]
                if self.options is not None
                else None
            ),
        }


NUMERIC_OPS = (">", "<", ">=", "<=", "=", "!=", "between", "in")
ENUM_OPS = ("=", "!=", "in")
BOOL_OPS = ("=", "!=")
ALLOWED_OPS = set(NUMERIC_OPS)


def _spec(
    field: str,
    label: str,
    group: str,
    source: str,
    value_type: str,
    ops: tuple[str, ...] = NUMERIC_OPS,
    *,
    unit: str | None = None,
    null_policy: str = "no_match",
    sortable: bool = True,
    options: tuple[str, ...] | None = None,
    deps: tuple[str, ...] = (),
    materialize: Callable[[pl.DataFrame], pl.Expr] | None = None,
) -> FieldSpec:
    value_type = {"decimal": "numeric", "bool": "boolean"}.get(value_type, value_type)
    return FieldSpec(field, label, group, source, unit, value_type, null_policy, "available", ops, sortable, options, deps, materialize)


_BOARD_OPTIONS = ("sh_main", "sz_main", "chinext", "star", "bse")
_BOARD_LABELS = {
    "sh_main": "沪市主板",
    "sz_main": "深市主板",
    "chinext": "创业板",
    "star": "科创板",
    "bse": "北交所",
}


FIELD_REGISTRY: dict[str, FieldSpec] = {
    "change_pct": _spec("change_pct", "涨跌幅", "market", "persist/runtime", "decimal", unit="小数"),
    "close": _spec("close", "收盘价", "market", "persist", "decimal", unit="元"),
    "turnover_rate": _spec("turnover_rate", "换手率", "market", "persist", "decimal", unit="%"),
    "vol_ratio_5d": _spec("vol_ratio_5d", "5日量比", "market", "runtime", "decimal", unit="倍"),
    "amount": _spec("amount", "成交额", "market", "persist", "decimal", unit="元"),
    "volume_surge": _spec("volume_surge", "放量 (量比≥2)", "market", "derived", "bool", ops=BOOL_OPS, deps=("signal_volume_surge",)),
    "float_market_cap": _spec("float_market_cap", "流通市值", "market_cap", "derived", "decimal", unit="亿元", deps=("close", "float_shares")),
    "total_market_cap": _spec("total_market_cap", "总市值", "market_cap", "derived", "decimal", unit="亿元", deps=("close", "total_shares")),
    "ma_bullish_alignment": _spec("ma_bullish_alignment", "均线多头排列", "technical", "derived", "bool", ops=BOOL_OPS, deps=("close", "ma5", "ma10", "ma20", "ma60")),
    "above_ma5": _spec("above_ma5", "站上MA5", "technical", "derived", "bool", ops=BOOL_OPS, deps=("close", "ma5")),
    "above_ma10": _spec("above_ma10", "站上MA10", "technical", "derived", "bool", ops=BOOL_OPS, deps=("close", "ma10")),
    "above_ma20": _spec("above_ma20", "站上MA20", "technical", "derived", "bool", ops=BOOL_OPS, deps=("close", "ma20")),
    "above_ma60": _spec("above_ma60", "站上MA60", "technical", "derived", "bool", ops=BOOL_OPS, deps=("close", "ma60")),
    "below_ma5": _spec("below_ma5", "处于MA5下方", "technical", "derived", "bool", ops=BOOL_OPS, deps=("close", "ma5")),
    "below_ma10": _spec("below_ma10", "处于MA10下方", "technical", "derived", "bool", ops=BOOL_OPS, deps=("close", "ma10")),
    "below_ma20": _spec("below_ma20", "处于MA20下方", "technical", "derived", "bool", ops=BOOL_OPS, deps=("close", "ma20")),
    "below_ma60": _spec("below_ma60", "处于MA60下方", "technical", "derived", "bool", ops=BOOL_OPS, deps=("close", "ma60")),
    "macd_golden": _spec("macd_golden", "MACD金叉", "technical", "derived", "bool", ops=BOOL_OPS, deps=("signal_macd_golden",)),
    "kdj_k": _spec("kdj_k", "KDJ K值", "technical", "runtime", "decimal"),
    "rsi_14": _spec("rsi_14", "RSI14", "technical", "runtime", "decimal"),
    "boll_upper_breakout": _spec("boll_upper_breakout", "布林上轨突破", "technical", "derived", "bool", ops=BOOL_OPS, deps=("signal_boll_breakout_upper",)),
    "ma20_breakdown": _spec("ma20_breakdown", "当日跌破MA20", "technical", "derived", "bool", ops=BOOL_OPS, deps=("signal_ma20_breakdown",)),
    "n_day_high": _spec("n_day_high", "创60日新高", "technical", "derived", "bool", ops=BOOL_OPS, deps=("signal_n_day_high",)),
    "limit_up": _spec("limit_up", "涨停", "limit_up", "derived", "bool", ops=BOOL_OPS, deps=("signal_limit_up",)),
    "consecutive_limit_ups": _spec("consecutive_limit_ups", "连续涨停", "limit_up", "runtime", "decimal", unit="次"),
    "broken_limit_up": _spec("broken_limit_up", "炸板 (触板未封)", "limit_up", "derived", "bool", ops=BOOL_OPS, deps=("signal_broken_limit_up",)),
    "yo_y_profit": _spec("yo_y_profit", "净利润同比", "financial", "financials", "decimal", unit="%", deps=("yo_y_profit",)),
    "industry": _spec("industry", "行业", "financial", "financials", "enum", ops=ENUM_OPS, sortable=False, deps=("industry",)),
    "roe": _spec("roe", "ROE", "financial", "financials", "decimal", unit="%", deps=("weight_avg_roe",)),
    "basic_eps": _spec("basic_eps", "基本每股收益", "financial", "financials", "decimal", unit="元", deps=("basic_eps",)),
    "gross_margin": _spec("gross_margin", "毛利率", "financial", "financials", "decimal", unit="%", deps=("gross_margin",)),
    "pe_approx": _spec("pe_approx", "市盈率TTM(近似)", "financial", "derived", "decimal", unit="倍", deps=("close", "eps_ttm")),
    "pb_approx": _spec("pb_approx", "PB (近似)", "financial", "derived", "decimal", unit="倍", deps=("close", "bps")),
    "board": _spec("board", "板块", "filter", "derived", "enum", ops=ENUM_OPS, options=_BOARD_OPTIONS, deps=("symbol",)),
    "price_position_60d": _spec("price_position_60d", "60日价格位置", "technical", "derived", "decimal", unit="%", deps=("close", "high_60d", "low_60d")),
    "distance_to_60d_high": _spec("distance_to_60d_high", "距60日新高", "technical", "derived", "decimal", unit="%", deps=("close", "high_60d")),
    "atr_pct_14": _spec("atr_pct_14", "ATR14波动率", "technical", "derived", "decimal", unit="%", deps=("atr_14", "close")),
    "distance_to_ma20": _spec("distance_to_ma20", "距MA20", "technical", "derived", "decimal", unit="%", deps=("close", "ma20")),
    "boll_band_width_20": _spec("boll_band_width_20", "布林带宽 (20日)", "technical", "derived", "decimal", unit="%", deps=("boll_upper", "boll_lower", "ma20")),
    "boll_position_20": _spec("boll_position_20", "布林带位置 (20日)", "technical", "derived", "decimal", unit="%, 100=上轨", deps=("close", "boll_upper", "boll_lower")),
    "annual_vol_20d": _spec("annual_vol_20d", "20日年化波动率", "technical", "runtime", "decimal", unit="小数(1.2=120%)"),
    "momentum_20d": _spec("momentum_20d", "20日动量", "technical", "runtime", "decimal", unit="小数(0.05=5%)"),
    "momentum_60d": _spec("momentum_60d", "60日动量", "technical", "runtime", "decimal", unit="小数(0.05=5%)"),
    "amplitude": _spec("amplitude", "日振幅", "market", "persist", "decimal", unit="小数(0.05=5%)"),
    "hk_connect": _spec("hk_connect", "沪深股通标的", "reference", "reference", "bool", ops=BOOL_OPS, deps=("hk_connect",)),
    "listing_days": _spec("listing_days", "上市天数", "reference", "reference", "decimal", unit="天", deps=("listing_days",)),
    "is_ah": _spec("is_ah", "AH股", "reference", "reference", "bool", ops=BOOL_OPS, deps=("is_ah",)),
    "ah_premium": _spec("ah_premium", "AH溢价率", "reference", "reference", "decimal", unit="%", deps=("ah_premium",)),
    "lhb_days_since_last": _spec("lhb_days_since_last", "距最近上榜", "lhb", "reference", "decimal", unit="天", deps=("lhb_days_since_last",)),
    "lhb_count_30d": _spec("lhb_count_30d", "近30天上榜次数", "lhb", "reference", "decimal", unit="次", deps=("lhb_count_30d",)),
    "lhb_count_90d": _spec("lhb_count_90d", "近90天上榜次数", "lhb", "reference", "decimal", unit="次", deps=("lhb_count_90d",)),
    "lhb_count_180d": _spec("lhb_count_180d", "近180天上榜次数", "lhb", "reference", "decimal", unit="次", deps=("lhb_count_180d",)),
    "lhb_institution_count_20d": _spec("lhb_institution_count_20d", "近20天机构上榜次数", "lhb", "reference", "decimal", unit="次", deps=("lhb_institution_count_20d",)),
    "lhb_institution_net_buy_20d": _spec("lhb_institution_net_buy_20d", "近20天机构净买入", "lhb", "reference", "decimal", unit="元", deps=("lhb_institution_net_buy_20d",)),
    "chip_profit_ratio": _spec("chip_profit_ratio", "获利筹码比例", "chip", "tdx_chip", "decimal", unit="%", deps=("chip_profit_ratio",)),
    "chip_avg_cost_deviation": _spec("chip_avg_cost_deviation", "距平均成本", "chip", "derived", "decimal", unit="%", deps=("close", "chip_avg_cost")),
    "chip_concentration_90": _spec("chip_concentration_90", "90%筹码集中度", "chip", "tdx_chip", "decimal", unit="%", deps=("chip_concentration_90",)),
    "chip_peak_count": _spec("chip_peak_count", "筹码峰数量", "chip", "tdx_chip", "decimal", unit="个", deps=("chip_peak_count",)),
    "main_net_inflow": _spec("main_net_inflow", "主力净流入", "moneyflow", "tdx_moneyflow", "decimal", unit="元", deps=("main_net_inflow",)),
    "main_net_inflow_ratio": _spec("main_net_inflow_ratio", "主力净流入占比", "moneyflow", "derived", "decimal", unit="%", deps=("main_net_inflow", "moneyflow_total_amount")),
    "super_large_net_inflow": _spec("super_large_net_inflow", "超大单净流入", "moneyflow", "tdx_moneyflow", "decimal", unit="元", deps=("super_large_net_inflow",)),
    "financing_balance": _spec("financing_balance", "融资余额", "margin", "fstore", "decimal", unit="万元", deps=("financing_balance",)),
    "financing_net_buy": _spec("financing_net_buy", "当日融资净买入", "margin", "fstore", "decimal", unit="万元", deps=("financing_net_buy",)),
    "financing_net_buy_5d": _spec("financing_net_buy_5d", "近5交易日融资净买入", "margin", "derived", "decimal", unit="万元", deps=("financing_net_buy_5d",)),
    "exclude_st": _spec("exclude_st", "排除ST/退市", "filter", "derived", "bool", ops=BOOL_OPS, deps=("name",)),
    "northbound_net_inflow": FieldSpec("northbound_net_inflow", "北向净流入", "market", "unavailable", None, "numeric", "no_match", "unavailable", NUMERIC_OPS, False),
    "realtime_concept": FieldSpec("realtime_concept", "实时概念", "filter", "unavailable", None, "enum", "no_match", "unavailable", ENUM_OPS, False),
}

# --------------------------------------------------------------------------- #
# F13 多日序列字段: 第二谓词面 (与 FIELD_REGISTRY 分离)。列由 _load_enriched_history
# 历史窗口计算后按 symbol join 回单日主表, 不进单日 panel 列。
# --------------------------------------------------------------------------- #

_SEQ_GROUP = "多日形态"

# 字段 → 所需完整窗口的交易日行数 (窗口数据不足 → NULL, 绝不伪造)。
_SEQUENCE_WINDOWS: dict[str, int] = {
    "seq_consecutive_up_3": 4,
    "seq_consecutive_up_5": 6,
    "seq_consecutive_volume_up_3": 4,
    "seq_limit_up_within_5d": 5,
    "seq_limit_up_within_10d": 10,
    "seq_cum_change_5d": 5,
    "seq_cum_change_10d": 10,
    "seq_cum_change_20d": 20,
    "seq_days_since_limit_up": 60,
}
_DAYS_SINCE_LIMIT_UP_LOOKBACK = _SEQUENCE_WINDOWS["seq_days_since_limit_up"]

SEQUENCE_FIELDS: dict[str, FieldSpec] = {
    "seq_consecutive_up_3": _spec("seq_consecutive_up_3", "连续上涨3日", _SEQ_GROUP, "sequence", "bool", ops=BOOL_OPS, sortable=False),
    "seq_consecutive_up_5": _spec("seq_consecutive_up_5", "连续上涨5日", _SEQ_GROUP, "sequence", "bool", ops=BOOL_OPS, sortable=False),
    "seq_consecutive_volume_up_3": _spec("seq_consecutive_volume_up_3", "连续放量3日", _SEQ_GROUP, "sequence", "bool", ops=BOOL_OPS, sortable=False),
    "seq_limit_up_within_5d": _spec("seq_limit_up_within_5d", "近5日涨停", _SEQ_GROUP, "sequence", "bool", ops=BOOL_OPS, sortable=False),
    "seq_limit_up_within_10d": _spec("seq_limit_up_within_10d", "近10日涨停", _SEQ_GROUP, "sequence", "bool", ops=BOOL_OPS, sortable=False),
    "seq_cum_change_5d": _spec("seq_cum_change_5d", "近5日累计涨跌幅", _SEQ_GROUP, "sequence", "decimal", unit="%"),
    "seq_cum_change_10d": _spec("seq_cum_change_10d", "近10日累计涨跌幅", _SEQ_GROUP, "sequence", "decimal", unit="%"),
    "seq_cum_change_20d": _spec("seq_cum_change_20d", "近20日累计涨跌幅", _SEQ_GROUP, "sequence", "decimal", unit="%"),
    "seq_days_since_limit_up": _spec("seq_days_since_limit_up", "距最近涨停天数", _SEQ_GROUP, "sequence", "decimal", unit="天", sortable=False),
}


def get_field_spec(field: str) -> FieldSpec | None:
    """单日 registry + 多日序列 registry 的统一 spec 查找 (无命中返回 None)。"""
    return FIELD_REGISTRY.get(field) or SEQUENCE_FIELDS.get(field)


def iter_field_specs() -> Any:
    """按注册顺序迭代 (field, spec): 单日字段在前, 多日形态字段在后。"""
    yield from FIELD_REGISTRY.items()
    yield from SEQUENCE_FIELDS.items()


_DEPRECATED = {"pb", "main_fund_flow", "ttm", "main_net_flow"}
_FINANCIAL_DERIVED_FIELDS = {"pe_approx", "pb_approx", "roe"}
# 沪深股通/AH 来自 fstore 快照的当前参考数据, 仅最新交易日可查;
# listing_days 由 listing_date 按 as_of 推导, 支持历史查询, 不进 latest-only 集合。
_REFERENCE_FIELDS = {"is_ah", "ah_premium", "hk_connect", "listing_days"}
_CURRENT_INSTRUMENT_FIELDS = {"float_market_cap", "total_market_cap", "exclude_st"} | (
    _REFERENCE_FIELDS - {"listing_days"}
)

_BOARD_RE = (
    (re.compile(r"^(600|601|603|605)\d{3}\.SH$"), "sh_main"),
    (re.compile(r"^(000|001|002|003)\d{3}\.SZ$"), "sz_main"),
    (re.compile(r"^(300|301)\d{3}\.SZ$"), "chinext"),
    (re.compile(r"^(688|689)\d{3}\.SH$"), "star"),
    (re.compile(r"^(?:[48]\d{5}|92\d{4})\.BJ$"), "bse"),
)

# 龙虎榜字段按 as_of 回看窗口聚合(fstore longhb_detail, 2013 年起), 支持历史 as_of。
_LHB_WINDOWS: tuple[tuple[str, int], ...] = (
    ("lhb_count_30d", 30),
    ("lhb_count_90d", 90),
    ("lhb_count_180d", 180),
)
_LHB_FIELDS = {"lhb_days_since_last", *(name for name, _ in _LHB_WINDOWS)}

# 以下字段均由已发布快照按 as_of 读取，不使用当前参考数据，因此允许历史查询。
_LHB_INSTITUTION_WINDOW_DAYS = 20
_LHB_INSTITUTION_FIELDS = {
    "lhb_institution_count_20d",
    "lhb_institution_net_buy_20d",
}
_CHIP_FIELDS = {
    "chip_profit_ratio",
    "chip_avg_cost_deviation",
    "chip_concentration_90",
    "chip_peak_count",
}
_CHIP_SOURCE_COLUMNS = {
    "chip_profit_ratio": "chip_profit_ratio",
    "chip_avg_cost_deviation": "chip_avg_cost",
    "chip_concentration_90": "chip_concentration_90",
    "chip_peak_count": "chip_peak_count",
}
_MONEYFLOW_FIELDS = {
    "main_net_inflow",
    "main_net_inflow_ratio",
    "super_large_net_inflow",
}
_MONEYFLOW_SOURCE_COLUMNS = {
    "main_net_inflow": {"main_net_inflow"},
    "main_net_inflow_ratio": {"main_net_inflow", "moneyflow_total_amount"},
    "super_large_net_inflow": {"super_large_net_inflow"},
}
_MARGIN_FIELDS = {
    "financing_balance",
    "financing_net_buy",
    "financing_net_buy_5d",
}
_MARGIN_LOOKBACK_DAYS = 45
_MARGIN_WINDOW_DAYS = 5


def field_metadata() -> list[dict[str, Any]]:
    metadata = [s.metadata() for _, s in iter_field_specs()]
    # 仅最新交易日可查的当前标的字段对外标记 latest_only (前端徽标用);
    # 注册表内保持 available, 最新日查询照常放行。
    for item in metadata:
        if item["field"] in _CURRENT_INSTRUMENT_FIELDS:
            item["availability"] = "latest_only"
    return metadata


def _finite_number(value: Any) -> bool:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return False
    try:
        return math.isfinite(float(value))
    except (OverflowError, ValueError):
        return False


def _validate_value(spec: FieldSpec, op: str, value: Any, location: str) -> Any:
    if spec.availability != "available":
        raise ScreenerSemanticError(location, "unavailable_field")
    if op not in spec.ops:
        raise ScreenerSemanticError(location, "unsupported_operator")
    if spec.value_type in {"numeric", "decimal", "number"}:
        vals = value if op in {"between", "in"} else [value]
        if not all(_finite_number(v) for v in vals):
            raise ScreenerSemanticError(location, "invalid_value")
        if op == "between" and value[0] > value[1]:
            raise ScreenerSemanticError(location, "invalid_value")
        return value
    if spec.value_type in {"boolean", "bool"}:
        vals = value if op == "in" else [value]
        if not all(isinstance(v, bool) for v in vals):
            raise ScreenerSemanticError(location, "invalid_value")
        return value
    if spec.value_type == "enum":
        vals = value if op == "in" else [value]
        clean: list[str] = []
        for v in vals:
            if not isinstance(v, str) or not v.strip() or len(v.strip()) > 64:
                raise ScreenerSemanticError(location, "invalid_value")
            clean.append(v.strip())
        if spec.options is not None and any(v not in spec.options for v in clean):
            raise ScreenerSemanticError(location, "invalid_value")
        return clean if op == "in" else clean[0]
    raise ScreenerSemanticError(location, "invalid_value")


def validate_query(req: ScreenerQueryRequest) -> tuple[list[dict[str, Any]], QueryOrder]:
    applied: list[dict[str, Any]] = []
    for i, cond in enumerate(req.conditions):
        location = f"conditions[{i}]"
        spec = get_field_spec(cond.field)
        if spec is None or cond.field in _DEPRECATED:
            raise ScreenerSemanticError(f"{location}.field", "unknown_field")
        value = _validate_value(spec, cond.op, cond.value, f"{location}.value")
        # F14: 追加 group 键仅在显式分组时 (旧响应逐字节等价)。
        entry = {"field": cond.field, "op": cond.op, "value": value}
        if cond.group is not None:
            entry["group"] = cond.group
        applied.append(entry)
    order = req.order_by or QueryOrder()
    order_spec = get_field_spec(order.field)
    if order_spec is None or order.field in _DEPRECATED:
        raise ScreenerSemanticError("order_by.field", "invalid_order_field")
    if not order_spec.sortable:
        raise ScreenerSemanticError("order_by.field", "unsortable_field")
    if order.direction not in {"asc", "desc"}:
        raise ScreenerSemanticError("order_by.direction", "invalid_direction")
    # F14: 非默认组数量上限 (隐式默认组不计)。
    named_groups = {c.group for c in req.conditions if c.group is not None}
    if len(named_groups) > _MAX_NAMED_GROUPS:
        raise ScreenerSemanticError("group_logic", "too_many_groups")
    return applied, order


# F14: 非默认组上限 (隐式默认组不计入)。
_MAX_NAMED_GROUPS = 5


def _board_expr() -> pl.Expr:
    symbol = pl.col("symbol").cast(pl.String, strict=False).str.to_uppercase()
    expr: pl.Expr = pl.lit(None, dtype=pl.String)
    for pattern, board in reversed(_BOARD_RE):
        expr = pl.when(symbol.str.contains(pattern.pattern)).then(pl.lit(board)).otherwise(expr)
    return expr


def _materialize(df: pl.DataFrame, required: set[str]) -> pl.DataFrame:
    expressions: list[pl.Expr] = []
    for name in required:
        if name not in FIELD_REGISTRY:
            continue
        spec = FIELD_REGISTRY[name]
        if name in df.columns:
            continue
        if name == "float_market_cap":
            expressions.append((pl.col("close") * pl.col("float_shares") / 1e8).alias(name))
        elif name == "total_market_cap":
            expressions.append((pl.col("close") * pl.col("total_shares") / 1e8).alias(name))
        elif name == "ma_bullish_alignment":
            expressions.append(
                (
                    (pl.col("ma5") > pl.col("ma10"))
                    & (pl.col("ma10") > pl.col("ma20"))
                    & (pl.col("ma20") > pl.col("ma60"))
                ).alias(name)
            )
        elif name.startswith("above_ma"):
            ma = name.removeprefix("above_")
            expressions.append((pl.col("close") > pl.col(ma)).alias(name))
        elif name.startswith("below_ma"):
            ma = name.removeprefix("below_")
            expressions.append((pl.col("close") < pl.col(ma)).alias(name))
        elif name == "macd_golden":
            expressions.append(pl.col("signal_macd_golden").alias(name))
        elif name == "boll_upper_breakout":
            expressions.append(pl.col("signal_boll_breakout_upper").alias(name))
        elif name == "limit_up":
            expressions.append(pl.col("signal_limit_up").alias(name))
        elif name == "roe":
            expressions.append(pl.col("weight_avg_roe").alias(name))
        elif name == "pe_approx":
            expressions.append(pl.when(pl.col("eps_ttm") > 0).then(pl.col("close") / pl.col("eps_ttm")).otherwise(None).alias(name))
        elif name == "pb_approx":
            expressions.append(pl.when(pl.col("bps") > 0).then(pl.col("close") / pl.col("bps")).otherwise(None).alias(name))
        elif name == "board":
            expressions.append(_board_expr().alias(name))
        elif name == "exclude_st":
            name_col = pl.col("name").cast(pl.String, strict=False).str.strip_chars()
            expressions.append(pl.when(name_col.is_null() | (name_col == "")).then(None).otherwise(~name_col.str.to_uppercase().str.contains("ST|退")).alias(name))
        elif name == "price_position_60d":
            span = pl.col("high_60d") - pl.col("low_60d")
            expressions.append(
                pl.when(span > 0)
                .then((pl.col("close") - pl.col("low_60d")) / span * 100)
                .otherwise(None)
                .alias(name)
            )
        elif name == "distance_to_60d_high":
            expressions.append(
                pl.when(pl.col("high_60d") > 0)
                .then((pl.col("close") / pl.col("high_60d") - 1) * 100)
                .otherwise(None)
                .alias(name)
            )
        elif name == "atr_pct_14":
            expressions.append(
                pl.when(pl.col("close") > 0)
                .then(pl.col("atr_14") / pl.col("close") * 100)
                .otherwise(None)
                .alias(name)
            )
        elif name == "distance_to_ma20":
            expressions.append(
                pl.when(pl.col("ma20") > 0)
                .then((pl.col("close") / pl.col("ma20") - 1) * 100)
                .otherwise(None)
                .alias(name)
            )
        elif name == "boll_band_width_20":
            boll_span = pl.col("boll_upper") - pl.col("boll_lower")
            expressions.append(
                pl.when((pl.col("ma20") > 0) & (boll_span >= 0))
                .then(boll_span / pl.col("ma20") * 100)
                .otherwise(None)
                .alias(name)
            )
        elif name == "boll_position_20":
            boll_span = pl.col("boll_upper") - pl.col("boll_lower")
            expressions.append(
                pl.when(boll_span > 0)
                .then((pl.col("close") - pl.col("boll_lower")) / boll_span * 100)
                .otherwise(None)
                .alias(name)
            )
        elif name == "chip_avg_cost_deviation":
            expressions.append(
                pl.when(pl.col("chip_avg_cost") > 0)
                .then((pl.col("close") / pl.col("chip_avg_cost") - 1) * 100)
                .otherwise(None)
                .alias(name)
            )
        elif name == "main_net_inflow_ratio":
            expressions.append(
                pl.when(pl.col("moneyflow_total_amount") > 0)
                .then(pl.col("main_net_inflow") / pl.col("moneyflow_total_amount") * 100)
                .otherwise(None)
                .alias(name)
            )
        elif spec.deps:
            # A source alias (e.g. financial roe) is copied only when its
            # canonical source is present; missing sources are preflighted.
            expressions.append(pl.col(spec.deps[0]).alias(name))
    if expressions:
        try:
            df = df.with_columns(expressions)
        except Exception as exc:
            raise ScreenerDataUnavailableError(sorted(required)) from exc
    return df


def _join_instruments(df: pl.DataFrame, repo: Any) -> pl.DataFrame:
    getter = getattr(repo, "get_instruments", None)
    if getter is None:
        return df
    try:
        inst = getter()
    except Exception as exc:
        raise ScreenerDataUnavailableError(["name", "total_shares", "float_shares"]) from exc
    if inst is None or inst.is_empty() or "symbol" not in inst.columns:
        return df
    cols = [c for c in ("symbol", "name", "total_shares", "float_shares") if c in inst.columns]
    inst = inst.select(cols)
    if "name" in inst.columns:
        inst = inst.with_columns(
            pl.when(pl.col("name").cast(pl.String, strict=False).str.strip_chars() == "")
            .then(None)
            .otherwise(pl.col("name"))
            .alias("name")
        )
    value_cols = [c for c in cols if c != "symbol"]
    if value_cols:
        inst = inst.group_by("symbol", maintain_order=True).agg(
            [pl.col(c).drop_nulls().last().alias(c) for c in value_cols]
        )
    # Use temporary names so existing null columns are filled independently.
    temp = {c: f"__instrument_{c}" for c in cols if c != "symbol"}
    inst = inst.rename(temp)
    try:
        if "name" in df.columns:
            df = df.with_columns(
                pl.when(pl.col("name").cast(pl.String, strict=False).str.strip_chars() == "")
                .then(None)
                .otherwise(pl.col("name"))
                .alias("name")
            )
        out = df.join(inst, on="symbol", how="left")
        for original, tmp in temp.items():
            if original in out.columns:
                out = out.with_columns(pl.coalesce([pl.col(original), pl.col(tmp)]).alias(original)).drop(tmp)
            else:
                out = out.rename({tmp: original})
        return out
    except Exception as exc:
        raise ScreenerDataUnavailableError(["name", "total_shares", "float_shares"]) from exc

def _get_reference_flags() -> pl.DataFrame:
    """经 data_providers registry 取 A 股标的参考标记(fquant 特有方法, fail-soft)。"""
    try:
        from app.data_providers.registry import get_active_provider_name, get_provider

        provider = get_provider(get_active_provider_name("daily"))
        getter = getattr(provider, "get_stock_reference_flags", None)
        if getter is None:
            return pl.DataFrame()
        return getter()
    except Exception:  # noqa: BLE001
        return pl.DataFrame()


def _join_reference_flags(
    df: pl.DataFrame,
    required: set[str],
    as_of: date,
    *,
    loader: Callable[[], pl.DataFrame] | None = None,
) -> pl.DataFrame:
    """按需 JOIN 标的参考标记(is_ah/ah_premium/hk_connect/listing_days)。

    provider 不具备该能力或数据为空时, 若查询确需参考字段则
    ScreenerDataUnavailableError; 未请求参考字段时零开销。
    """
    wanted = required & _REFERENCE_FIELDS
    if not wanted:
        return df
    flags = (loader or _get_reference_flags)()
    if flags is None or flags.is_empty() or "symbol" not in flags.columns:
        raise ScreenerDataUnavailableError(sorted(wanted))
    cols = [c for c in ("symbol", "is_ah", "ah_premium", "hk_connect", "listing_date") if c in flags.columns]
    flags = flags.select(cols).unique(subset=["symbol"], keep="first")
    df = df.join(flags, on="symbol", how="left")
    if "listing_days" in wanted:
        if "listing_date" not in df.columns:
            raise ScreenerDataUnavailableError(["listing_days"])
        df = df.with_columns(
            (pl.lit(as_of) - pl.col("listing_date")).dt.total_days().alias("listing_days")
        )
    return df


def _get_lhb_records(start: date, end: date) -> pl.DataFrame:
    """经 data_providers registry 取龙虎榜上榜记录(fquant 特有方法, fail-soft)。"""
    try:
        from app.data_providers.registry import get_active_provider_name, get_provider

        provider = get_provider(get_active_provider_name("daily"))
        getter = getattr(provider, "get_lhb_records", None)
        if getter is None:
            return pl.DataFrame()
        return getter(start, end)
    except Exception:  # noqa: BLE001
        return pl.DataFrame()


def _join_lhb_stats(
    df: pl.DataFrame,
    required: set[str],
    as_of: date,
    *,
    loader: Callable[[date, date], pl.DataFrame] | None = None,
) -> pl.DataFrame:
    """按需 JOIN 龙虎榜统计(距最近上榜天数 + 30/90/180 天上榜次数)。

    窗口相对 as_of 计算, 因此支持历史 as_of。窗口内无记录的标的次数为 0;
    距最近上榜仅统计回看期(180 天)内, 无记录时为 null(数值条件不匹配)。
    provider 不具备该能力或数据为空时, 若查询确需龙虎榜字段则
    ScreenerDataUnavailableError; 未请求龙虎榜字段时零开销。
    """
    wanted = required & _LHB_FIELDS
    if not wanted:
        return df
    records = (loader or _get_lhb_records)(
        as_of - timedelta(days=_LHB_WINDOWS[-1][1]), as_of
    )
    if records is None or records.is_empty() or "symbol" not in records.columns:
        raise ScreenerDataUnavailableError(sorted(wanted))
    records = records.with_columns(
        [
            (pl.col("trade_date") >= pl.lit(as_of - timedelta(days=days))).alias(f"_{name}")
            for name, days in _LHB_WINDOWS
        ]
    )
    stats = records.group_by("symbol").agg(
        [pl.col(f"_{name}").sum().alias(name) for name, _ in _LHB_WINDOWS]
        + [pl.col("trade_date").max().alias("lhb_last_date")]
    )
    df = df.join(stats, on="symbol", how="left")
    return df.with_columns(
        [pl.col(name).fill_null(0).cast(pl.Int64) for name, _ in _LHB_WINDOWS]
        + [
            (pl.lit(as_of) - pl.col("lhb_last_date"))
            .dt.total_days()
            .alias("lhb_days_since_last")
        ]
    ).drop("lhb_last_date")


def _get_provider_frame(method: str, *args: Any) -> pl.DataFrame:
    """Call an optional provider extension without widening the base contract."""
    try:
        from app.data_providers.registry import get_active_provider_name, get_provider

        provider = get_provider(get_active_provider_name("daily"))
        getter = getattr(provider, method, None)
        if getter is None:
            return pl.DataFrame()
        frame = getter(*args)
        return frame if isinstance(frame, pl.DataFrame) else pl.DataFrame()
    except Exception:  # noqa: BLE001
        return pl.DataFrame()


def _get_chip_snapshot(as_of: date) -> pl.DataFrame:
    return _get_provider_frame("get_chip_snapshot", as_of)


def _get_moneyflow_snapshot(as_of: date) -> pl.DataFrame:
    return _get_provider_frame("get_moneyflow_snapshot", as_of)


def _get_lhb_institution_records(start: date, end: date) -> pl.DataFrame:
    return _get_provider_frame("get_lhb_institution_records", start, end)


def _get_margin_records(start: date, end: date) -> pl.DataFrame:
    return _get_provider_frame("get_margin_records", start, end)


def _join_chip_snapshot(
    df: pl.DataFrame,
    required: set[str],
    as_of: date,
    *,
    loader: Callable[[date], pl.DataFrame] | None = None,
) -> pl.DataFrame:
    """Join the strict published chip snapshot for ``as_of`` on demand."""
    wanted = required & _CHIP_FIELDS
    if not wanted:
        return df
    snapshot = (loader or _get_chip_snapshot)(as_of)
    source_columns = {"symbol"} | {_CHIP_SOURCE_COLUMNS[name] for name in wanted}
    if (
        snapshot is None
        or snapshot.is_empty()
        or "symbol" not in snapshot.columns
        or not source_columns.issubset(snapshot.columns)
    ):
        raise ScreenerDataUnavailableError(sorted(wanted))
    return df.join(
        snapshot.select(sorted(source_columns)).unique(subset=["symbol"], keep="last"),
        on="symbol",
        how="left",
    )


def _join_moneyflow_snapshot(
    df: pl.DataFrame,
    required: set[str],
    as_of: date,
    *,
    loader: Callable[[date], pl.DataFrame] | None = None,
) -> pl.DataFrame:
    """Join quality-checked daily moneyflow from the strict published snapshot."""
    wanted = required & _MONEYFLOW_FIELDS
    if not wanted:
        return df
    snapshot = (loader or _get_moneyflow_snapshot)(as_of)
    source_columns = {"symbol"} | set().union(
        *(_MONEYFLOW_SOURCE_COLUMNS[name] for name in wanted)
    )
    required_columns = source_columns | {"valid_count", "invalid_count"}
    if (
        snapshot is None
        or snapshot.is_empty()
        or not required_columns.issubset(snapshot.columns)
    ):
        raise ScreenerDataUnavailableError(sorted(wanted))
    snapshot = snapshot.filter(
        (pl.col("valid_count") == 1) & (pl.col("invalid_count") == 0)
    )
    if snapshot.is_empty():
        raise ScreenerDataUnavailableError(sorted(wanted))
    return df.join(
        snapshot.select(sorted(source_columns)).unique(subset=["symbol"], keep="last"),
        on="symbol",
        how="left",
    )


def _join_lhb_institution_stats(
    df: pl.DataFrame,
    required: set[str],
    as_of: date,
    *,
    loader: Callable[[date, date], pl.DataFrame] | None = None,
) -> pl.DataFrame:
    """Join 20-calendar-day institution-seat LHB counts and net buys."""
    wanted = required & _LHB_INSTITUTION_FIELDS
    if not wanted:
        return df
    records = (loader or _get_lhb_institution_records)(
        as_of - timedelta(days=_LHB_INSTITUTION_WINDOW_DAYS), as_of
    )
    required_columns = {"symbol", "trade_date", "net_buy_amount"}
    if (
        records is None
        or records.is_empty()
        or not required_columns.issubset(records.columns)
    ):
        raise ScreenerDataUnavailableError(sorted(wanted))
    stats = records.group_by("symbol").agg(
        pl.len().alias("lhb_institution_count_20d"),
        pl.col("net_buy_amount").sum().alias("lhb_institution_net_buy_20d"),
    )
    return df.join(stats, on="symbol", how="left").with_columns(
        pl.col("lhb_institution_count_20d").fill_null(0).cast(pl.Int64),
        pl.col("lhb_institution_net_buy_20d").fill_null(0.0),
    )


def _previous_weekday(value: date) -> date:
    """Return the preceding weekday; holiday uncertainty intentionally fails closed."""
    candidate = value - timedelta(days=1)
    while candidate.weekday() >= 5:
        candidate -= timedelta(days=1)
    return candidate


def _join_margin_stats(
    df: pl.DataFrame,
    required: set[str],
    as_of: date,
    *,
    loader: Callable[[date, date], pl.DataFrame] | None = None,
) -> pl.DataFrame:
    """Join financing balance and net purchase under a strict one-trading-day lag."""
    wanted = required & _MARGIN_FIELDS
    if not wanted:
        return df
    records = (loader or _get_margin_records)(
        as_of - timedelta(days=_MARGIN_LOOKBACK_DAYS), as_of
    )
    needed = {"symbol", "trade_date"}
    if wanted & {"financing_balance", "financing_net_buy"}:
        needed |= {"financing_balance", "financing_net_buy"}
    if "financing_net_buy_5d" in wanted:
        needed.add("financing_net_buy")
    if records is None or records.is_empty() or not needed.issubset(records.columns):
        raise ScreenerDataUnavailableError(sorted(wanted))

    records = records.filter(pl.col("trade_date") <= pl.lit(as_of)).unique(
        subset=["symbol", "trade_date"], keep="last"
    )
    source_dates = [
        value for value in records.get_column("trade_date").drop_nulls().unique().to_list()
        if isinstance(value, date)
    ]
    if not source_dates:
        raise ScreenerDataUnavailableError(sorted(wanted))
    latest_source_date = max(source_dates)
    if latest_source_date < _previous_weekday(as_of):
        raise ScreenerDataUnavailableError(sorted(wanted))

    latest = records.filter(pl.col("trade_date") == pl.lit(latest_source_date))
    latest_columns = ["symbol", *(
        name for name in ("financing_balance", "financing_net_buy")
        if name in needed
    )]
    df = df.join(
        latest.select(latest_columns).unique(subset=["symbol"], keep="last"),
        on="symbol",
        how="left",
    )
    if "financing_net_buy_5d" not in wanted:
        return df

    window_dates = [latest_source_date]
    for _ in range(_MARGIN_WINDOW_DAYS - 1):
        window_dates.append(_previous_weekday(window_dates[-1]))
    if not set(window_dates).issubset(source_dates):
        raise ScreenerDataUnavailableError(["financing_net_buy_5d"])
    recent = records.filter(pl.col("trade_date").is_in(window_dates))
    stats = recent.group_by("symbol").agg(
        pl.col("financing_net_buy").sum().alias("financing_net_buy_5d"),
        pl.col("trade_date").n_unique().alias("_margin_days"),
    )
    return df.join(stats, on="symbol", how="left").with_columns(
        pl.when(pl.col("_margin_days") == _MARGIN_WINDOW_DAYS)
        .then(pl.col("financing_net_buy_5d"))
        .otherwise(None)
        .alias("financing_net_buy_5d")
    ).drop("_margin_days")

def _literal_filter(condition: dict[str, Any]) -> pl.Expr:
    field, op, value = condition["field"], condition["op"], condition["value"]
    col = pl.col(field)
    if op == "between":
        expr = col.is_between(value[0], value[1], closed="both")
    elif op == "in":
        expr = col.is_in(value)
    elif op == "=":
        expr = col == pl.lit(value)
    elif op == "!=":
        expr = col != pl.lit(value)
    elif op == ">":
        expr = col > pl.lit(value)
    elif op == "<":
        expr = col < pl.lit(value)
    elif op == ">=":
        expr = col >= pl.lit(value)
    elif op == "<=":
        expr = col <= pl.lit(value)
    else:  # validate_query makes this unreachable
        raise ScreenerSemanticError("conditions", "invalid_operator")
    spec = get_field_spec(field)
    if spec is not None and spec.value_type == "numeric":
        expr = col.is_not_null() & col.is_finite() & expr
    return expr.fill_null(False)


def compile_predicate(
    conditions: list[QueryCondition] | list[dict[str, Any]],
    order_by: QueryOrder | dict[str, Any] | None = None,
    *,
    group_logic: Literal["and", "or"] = "and",
) -> tuple[pl.Expr, list[dict[str, Any]], QueryOrder]:
    """Validate public literals and compile a closed Polars predicate.

    F14: 条件按 group 分桶 (None → 隐式默认组), 桶内 AND,
    桶间按 ``group_logic`` 合并 ("and" 等价旧的 flat AND 语义)。
    """
    req = ScreenerQueryRequest(conditions=conditions, order_by=order_by, group_logic=group_logic)
    applied, order = validate_query(req)
    buckets: dict[str, pl.Expr] = {}
    for condition in applied:
        expr = _literal_filter(condition)
        key = condition.get("group") or ""
        buckets[key] = expr if key not in buckets else buckets[key] & expr
    combine = _or_combine if group_logic == "or" else _and_combine
    expression = combine(buckets)
    return expression, applied, order

def _and_combine(buckets: dict[str, pl.Expr]) -> pl.Expr:
    """桶间 AND (与旧的 flat AND 完全一致)。"""
    expression = pl.lit(True)
    for bucket in buckets.values():
        expression &= bucket
    return expression


def _or_combine(buckets: dict[str, pl.Expr]) -> pl.Expr:
    """桶间 OR: 任一分组整体命中即命中。"""
    expression = pl.lit(False)
    for bucket in buckets.values():
        expression |= bucket
    return expression


def _json_value(value: Any) -> Any:
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, (date,)):
        return value.isoformat()
    return value


def _enriched_columns_for(public_fields: set[str]) -> list[str]:
    """计算筛选所需的 enriched 列，避免历史查询无条件全量计算指标。

    外部快照、财务和当前标的字段在后续专用 join 中补齐，不能把它们的字段名
    传给 repository；否则会错误触发完整指标计算。保留 `close`/`change_pct`
    作为公共行形状基线。多日序列字段来自历史窗口路径, 不占单日 panel 列。
    """
    from app.indicators.pipeline import ENRICHED_COLUMNS

    required = {"close", "change_pct"}
    for name in public_fields:
        spec = FIELD_REGISTRY.get(name)
        if spec is None:
            continue
        for candidate in (name, *spec.deps):
            if candidate in ENRICHED_COLUMNS:
                required.add(candidate)
    return sorted(required)


def _sequence_lookback_days(requested: set[str]) -> int:
    """序列字段所需的历史回看自然日数。

    ``_load_enriched_history`` 的 lookback_days 是自然日 (start = as_of - days),
    交易日窗口按 7/5 换算并留假期缓冲, 保证窗口行数充足。
    """
    window = max(_SEQUENCE_WINDOWS[f] for f in requested)
    return math.ceil(window * 7 / 5) + 15




def _join_sequence_columns(
    df: pl.DataFrame,
    history: pl.DataFrame,
    as_of: date,
    requested: set[str],
) -> pl.DataFrame:
    """按 symbol 计算多日序列列并 left join 回单日主表 (只保留 date == as_of 行)。

    独立计算路径, 不复用也不污染单日 _materialize; 主表 symbol 在历史窗口
    无 as_of 行时为 null → 数值条件不匹配, 布尔条件视为 False (null_policy)。
    """
    frame = history.filter(pl.col("date") <= as_of).sort(["symbol", "date"])
    base = [
        (c if c in frame.columns else pl.lit(None).alias(c))
        for c in ("close", "volume", "signal_limit_up")
    ]
    frame = frame.select(["symbol", "date", *base])
    up = pl.col("close") > pl.col("close").shift(1).over("symbol")
    vup = pl.col("volume") > pl.col("volume").shift(1).over("symbol")
    frame = frame.with_columns(
        pl.int_range(pl.len()).over("symbol").alias("_i"),
        up.alias("_up"),
        vup.alias("_vup"),
        pl.col("signal_limit_up").fill_null(False).cast(pl.UInt32).cum_sum().over("symbol").alias("_lim_cnt"),
        pl.when(pl.col("signal_limit_up").fill_null(False)).then(pl.int_range(pl.len()).over("symbol")).otherwise(None).alias("_lim_i"),
    )

    def _chain(col: str, n: int) -> pl.Expr:
        expr = pl.col(col)
        for k in range(1, n):
            expr = expr & pl.col(col).shift(k).over("symbol")
        return expr

    exprs: list[pl.Expr] = []
    for field, window in _SEQUENCE_WINDOWS.items():
        if field not in requested:
            continue
        if field == "seq_consecutive_up_3":
            exprs.append(_chain("_up", 3).alias(field))
        elif field == "seq_consecutive_up_5":
            exprs.append(_chain("_up", 5).alias(field))
        elif field == "seq_consecutive_volume_up_3":
            exprs.append(_chain("_vup", 3).alias(field))
        elif field in {"seq_limit_up_within_5d", "seq_limit_up_within_10d"}:
            span = 5 if field.endswith("5d") else 10
            # 近 N 日 = 含当日的最近 N 个交易日, 即行 [t-N+1, t]。
            # cum 差分须减 shift(N) (窗口前一行的累计值); 恰好 N 行时
            # shift 为 null → 按窗口前累计 0 处理, 否则会把 t-N+1 行
            # 本身的涨停误减掉 (off-by-one)。
            exprs.append(
                pl.when(pl.col("_i") >= span - 1)
                .then(
                    (
                        pl.col("_lim_cnt")
                        - pl.col("_lim_cnt").shift(span).over("symbol").fill_null(0)
                    )
                    > 0
                )
                .otherwise(None)
                .alias(field)
            )
        elif field in {"seq_cum_change_5d", "seq_cum_change_10d", "seq_cum_change_20d"}:
            span = int(field.removeprefix("seq_cum_change_").removesuffix("d"))
            exprs.append(
                (((pl.col("close") / pl.col("close").shift(span - 1).over("symbol")) - 1) * 100).alias(field)
            )
    frame = frame.with_columns(*exprs)
    if "seq_days_since_limit_up" in requested:
        frame = frame.with_columns(
            (pl.col("_i") - pl.col("_lim_i").forward_fill().over("symbol")).alias("seq_days_since_limit_up")
        )
    latest = frame.filter(pl.col("date") == as_of).select(["symbol", *sorted(requested)])
    return df.join(latest, on="symbol", how="left")


def execute_query(repo: Any, req: ScreenerQueryRequest) -> dict[str, Any]:
    """Validate, load, materialize, filter, sort, and project a screener query."""
    mask, applied, order = compile_predicate(
        req.conditions, req.order_by, group_logic=req.group_logic
    )
    t0 = time.perf_counter()
    requested_fields = {condition["field"] for condition in applied} | {order.field}
    # Public rows always expose close/change_pct, independent of user conditions.
    public_required = requested_fields | {"close", "change_pct"}
    try:
        from app.services.screener import ScreenerService

        svc = ScreenerService(repo)
        latest = svc.latest_date()
        as_of = req.as_of or latest
        if not as_of:
            raise ScreenerDataUnavailableError(["symbol"])
        current_only = sorted(_CURRENT_INSTRUMENT_FIELDS & requested_fields)
        if req.as_of is not None and latest is not None and as_of != latest and current_only:
            raise ScreenerDataUnavailableError(current_only)
        df = svc._load_enriched_for_date(
            as_of,
            columns=_enriched_columns_for(public_required),
        )
    except ScreenerDataUnavailableError:
        raise
    except Exception as exc:
        raise ScreenerDataUnavailableError(["symbol"]) from exc
    if df is None or df.is_empty():
        raise ScreenerDataUnavailableError(sorted({c["field"] for c in applied}))
    if "symbol" not in df.columns:
        raise ScreenerDataUnavailableError(["symbol"])

    df = df.unique(subset=["symbol"], keep="last")
    df = _join_instruments(df, repo)
    join_required = requested_fields
    df = _join_reference_flags(df, join_required, as_of)
    df = _join_lhb_stats(df, join_required, as_of)
    df = _join_lhb_institution_stats(df, join_required, as_of)
    df = _join_chip_snapshot(df, join_required, as_of)
    df = _join_moneyflow_snapshot(df, join_required, as_of)
    df = _join_margin_stats(df, join_required, as_of)
    sequence_requested = requested_fields & set(SEQUENCE_FIELDS)
    if sequence_requested:
        try:
            history = svc._load_enriched_history(as_of, _sequence_lookback_days(sequence_requested))
        except ScreenerDataUnavailableError:
            raise
        except Exception as exc:
            raise ScreenerDataUnavailableError(sorted(sequence_requested)) from exc
        if history is None or history.is_empty():
            raise ScreenerDataUnavailableError(sorted(sequence_requested))
        df = _join_sequence_columns(df, history, as_of, sequence_requested)
    source_required: set[str] = {"symbol", "close", "change_pct"}
    for name in public_required:
        if name in SEQUENCE_FIELDS:
            continue  # 序列列已由历史窗口 join 补齐
        spec = FIELD_REGISTRY[name]
        if spec.source == "financials" or name in _FINANCIAL_DERIVED_FIELDS:
            source_required.update(spec.deps)
        elif name in df.columns:
            source_required.add(name)
        else:
            source_required.update(
                spec.deps
                or ((name,) if spec.source in {"persist", "persist/runtime", "runtime"} else ())
            )
    unavailable = [
        name for name in public_required if get_field_spec(name).availability != "available"
    ]
    if unavailable:
        raise ScreenerDataUnavailableError(unavailable)
    financial = {
        name for name in public_required
        if get_field_spec(name).source == "financials"
        or name in _FINANCIAL_DERIVED_FIELDS
    }
    if financial:
        try:
            from app.services.screener_financials import load_financial_snapshot

            snap = load_financial_snapshot(Path(repo.store.data_dir), as_of)
            if snap is not None:
                if not snap.is_empty():
                    snap = snap.unique(subset=["symbol"], keep="last")
                cols = [c for c in snap.columns if c != "symbol"]
                stale = [
                    c for c in [*cols, *_FINANCIAL_DERIVED_FIELDS]
                    if c in df.columns
                ]
                if stale:
                    df = df.drop(stale)
                df = df.join(snap.select(["symbol", *cols]), on="symbol", how="left")
        except Exception as exc:
            raise ScreenerDataUnavailableError(sorted(financial)) from exc

    # Preflight canonical columns before materializing aliases.  This keeps a
    # malformed/partial enriched frame a sanitized 503, not an empty result.
    missing = [name for name in source_required if name not in df.columns]
    if missing:
        missing_public = [name for name in public_required if name in missing or any(d in missing for d in get_field_spec(name).deps)]
        raise ScreenerDataUnavailableError(sorted(set(missing_public or missing)))
    try:
        df = _materialize(df, public_required)
        for c in public_required:
            if c not in df.columns:
                raise ScreenerDataUnavailableError([c])
        if "name" not in df.columns:
            df = df.with_columns(pl.lit(None, dtype=pl.String).alias("name"))
        if "date" not in df.columns:
            df = df.with_columns(pl.lit(as_of).alias("date"))

        filtered = df.filter(mask)
        total = filtered.height
        # F15: facet 在 limit 截断前对全部命中行聚合。
        facet_hits = filtered.select("symbol") if "industry" in req.facets else None
        if get_field_spec(order.field).value_type == "numeric":
            filtered = filtered.with_columns(
                pl.when(pl.col(order.field).is_finite())
                .then(pl.col(order.field))
                .otherwise(None)
                .alias(order.field)
            )
        filtered = filtered.sort(
            [order.field, "symbol"],
            descending=[order.direction == "desc", False],
            nulls_last=True,
        )
        filtered = filtered.head(req.limit)
    except ScreenerDataUnavailableError:
        raise
    except Exception as exc:
        raise ScreenerDataUnavailableError(sorted(public_required)) from exc
    # Public projection: identifiers plus requested condition/order fields. It
    # avoids pulling financial parquet for a technical-only query.
    projection: list[str] = [c for c in ("symbol", "name", "date", "close", "change_pct") if c in filtered.columns]
    for c in [*(x["field"] for x in applied), order.field]:
        if c in filtered.columns and c not in projection:
            projection.append(c)
    rows = [
        {k: _json_value(v) for k, v in row.items()}
        for row in filtered.select(projection).to_dicts()
    ]
    result = {
        "rows": rows,
        "total": total,
        "applied": applied,
        "as_of": as_of.isoformat(),
        "elapsed_ms": (time.perf_counter() - t0) * 1000,
    }
    # F15: 仅在请求 facets 时新增键 (旧响应逐字节等价)。
    if req.facets:
        facets: dict[str, list[dict[str, Any]]] = {}
        warnings: list[str] = []
        if facet_hits is not None:
            # 命中行如已带 industry 列 (本次查询 join 过财务快照) 直接复用,
            # 否则按 as_of 单独加载 PIT 快照 — 不重复读已加载的数据。
            # 注意: 必须用 limit 截断前的 facet_hits, 而非 head 后的 filtered。
            hits = (
                df.filter(mask).select("symbol", "industry")
                if "industry" in df.columns
                else facet_hits
            )
            facets["industry"], industry_warnings = _industry_facet(hits, repo, as_of)
            warnings.extend(industry_warnings)
        result["facets"] = facets
        if warnings:
            result["facet_warnings"] = warnings
    return result


def _industry_facet(
    hits: pl.DataFrame,
    repo: Any,
    as_of: date,
) -> tuple[list[dict[str, Any]], list[str]]:
    """全部命中行的 PIT 行业分布 (fail-soft: 快照缺失只给 warning 不阻断)。"""
    if hits.is_empty():
        return [], []
    if "industry" not in hits.columns:
        try:
            from app.services.screener_financials import load_financial_snapshot

            snap = load_financial_snapshot(Path(repo.store.data_dir), as_of)
        except Exception:
            return [], ["industry_unavailable"]
        if snap is None or snap.is_empty() or "industry" not in snap.columns:
            return [], ["industry_unavailable"]
        hits = hits.select("symbol").join(
            snap.select("symbol", "industry").unique(subset=["symbol"], keep="last"),
            on="symbol",
            how="left",
        )
    counts = (
        hits.filter(pl.col("industry").is_not_null())
        .group_by("industry")
        .agg(pl.len().alias("count"))
        .sort(["count", "industry"], descending=[True, False])
        .head(20)
    )
    return [{"value": row["industry"], "count": row["count"]} for row in counts.to_dicts()], []


class QueryService:
    """Repository-bound public entry point for condition screener queries."""

    def __init__(self, repo: Any) -> None:
        self.repo = repo

    def query(self, req: ScreenerQueryRequest) -> dict[str, Any]:
        return execute_query(self.repo, req)


__all__ = [
    "ALLOWED_OPS",
    "FIELD_REGISTRY",
    "FieldSpec",
    "QueryCondition",
    "QueryOrder",
    "QueryService",
    "ScreenerDataUnavailableError",
    "ScreenerQueryRequest",
    "ScreenerSemanticError",
    "SEQUENCE_FIELDS",
    "compile_predicate",
    "execute_query",
    "field_metadata",
    "get_field_spec",
    "validate_query",
]
