"""A股、港股和美股的最小交易规则契约。"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal

MarketCode = Literal["cn", "hk", "us"]
PriceLimitPolicy = Literal["cn", "none"]


@dataclass(frozen=True)
class MarketRule:
    market: MarketCode
    timezone: str
    currency: str
    default_round_lot: int
    same_day_sell_allowed: bool
    price_limit_policy: PriceLimitPolicy


_RULES: dict[MarketCode, MarketRule] = {
    "cn": MarketRule(
        market="cn",
        timezone="Asia/Shanghai",
        currency="CNY",
        default_round_lot=100,
        same_day_sell_allowed=False,
        price_limit_policy="cn",
    ),
    "hk": MarketRule(
        market="hk",
        timezone="Asia/Hong_Kong",
        currency="HKD",
        default_round_lot=1,
        same_day_sell_allowed=True,
        price_limit_policy="none",
    ),
    "us": MarketRule(
        market="us",
        timezone="America/New_York",
        currency="USD",
        default_round_lot=1,
        same_day_sell_allowed=True,
        price_limit_policy="none",
    ),
}


def market_for_symbol(symbol: str) -> MarketCode:
    """根据项目标准代码后缀识别市场。未知格式必须显式报错。"""

    normalized = str(symbol or "").strip().upper()
    if normalized.endswith((".SH", ".SZ", ".BJ")):
        return "cn"
    if normalized.endswith(".HK"):
        return "hk"
    if normalized.endswith(".US"):
        return "us"
    raise ValueError(f"无法识别证券市场: {symbol!r}")


def market_rule_for_symbol(symbol: str) -> MarketRule:
    return _RULES[market_for_symbol(symbol)]


def round_lot_size(symbol: str, metadata: Mapping[str, object] | None = None) -> int:
    """返回买入手数。有效证券元数据优先于市场默认值。"""

    rule = market_rule_for_symbol(symbol)
    raw = (metadata or {}).get("lot_size")
    if isinstance(raw, bool):
        return rule.default_round_lot
    try:
        lot_size = int(raw) if raw is not None and str(raw).strip() else 0
    except (TypeError, ValueError):
        lot_size = 0
    return lot_size if lot_size > 0 else rule.default_round_lot
