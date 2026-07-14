"""Capability 定义(§5.1)。

业务代码只依赖 CapabilitySet,不读 tiers.yaml,不感知"档位"。
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class Cap(StrEnum):
    """所有 capability 的命名常量。新增能力时只在这里加一行。"""

    QUOTE_BY_SYMBOL        = "quote.by_symbol"
    QUOTE_BATCH            = "quote.batch"
    QUOTE_POOL             = "quote.pool"
    KLINE_DAILY_BY_SYMBOL  = "kline.daily.by_symbol"
    KLINE_DAILY_BATCH      = "kline.daily.batch"
    KLINE_MINUTE_BY_SYMBOL = "kline.minute.by_symbol"
    KLINE_MINUTE_BATCH     = "kline.minute.batch"
    INTRADAY               = "intraday"
    INTRADAY_BATCH         = "intraday.batch"
    DEPTH5                 = "depth5"
    DEPTH5_BATCH           = "depth5.batch"
    WEBSOCKET              = "websocket"
    FINANCIAL              = "financial"
    ADJ_FACTOR             = "adj_factor"


@dataclass(slots=True, frozen=True)
class CapabilityLimits:
    """单个 capability 的运行时限制。"""
    rpm: int | None = None        # 次/分钟,None 表示未知或不限
    batch: int | None = None      # 标的/次
    subscribe: int | None = None  # WS 订阅上限


class CapabilitySet:
    """探测得到的"用户当前可用能力"。业务代码的唯一真理源。"""

    def __init__(self, caps: dict[Cap, CapabilityLimits] | None = None) -> None:
        self._caps: dict[Cap, CapabilityLimits] = dict(caps or {})

    def has(self, cap: Cap) -> bool:
        return cap in self._caps

    def limits(self, cap: Cap) -> CapabilityLimits | None:
        return self._caps.get(cap)

    def require(self, cap: Cap) -> CapabilityLimits:
        """断言可用,否则抛 CapabilityDenied。"""
        if cap not in self._caps:
            raise CapabilityDenied(cap)
        return self._caps[cap]

    def all(self) -> dict[Cap, CapabilityLimits]:
        return dict(self._caps)

    def grant(self, cap: Cap, limits: CapabilityLimits | None = None) -> None:
        """补授一个能力 (不覆盖已有)。用于自定义数据源: 用户配了自定义分钟源时,
        即使无 TickFlow Pro+ 也补上 KLINE_MINUTE_BATCH, 使所有 capset.has() 检查通过。"""
        if cap not in self._caps:
            self._caps[cap] = limits or CapabilityLimits()

    def to_dict(self) -> dict[str, dict]:
        return {
            str(cap): {
                "rpm": lim.rpm,
                "batch": lim.batch,
                "subscribe": lim.subscribe,
            }
            for cap, lim in self._caps.items()
        }


class CapabilityDenied(Exception):
    """请求的 capability 当前不可用。"""

    def __init__(self, cap: Cap, suggestion: str | None = None) -> None:
        self.cap = cap
        self.suggestion = suggestion or f"加购『{cap}』能力可解锁"
        super().__init__(f"capability not available: {cap}; {self.suggestion}")
