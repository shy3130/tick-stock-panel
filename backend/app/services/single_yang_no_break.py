"""单阳不破研究规格与 fail-closed 服务。

本模块只有显式 bar 序列上的纯函数；生产 reader、状态机和 OOS 尚未实现，
所以研究入口即使未来具备数据能力也必须保持 unavailable。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Sequence

RESEARCH_ID = "single_yang_no_break_v1"
DEFAULT_WINDOW = 5
MIN_BODY_PCT_OF_OPEN = 0.02

PIT_READER_MISSING = "pit_reader_missing"
STATE_MACHINE_NOT_IMPLEMENTED = "state_machine_not_implemented"
OOS_NOT_IMPLEMENTED = "oos_not_implemented"
UNAVAILABLE_REASONS = (
    PIT_READER_MISSING,
    STATE_MACHINE_NOT_IMPLEMENTED,
    OOS_NOT_IMPLEMENTED,
)

SINGLE_YANG_DEFINITION: dict[str, Any] = {
    "id": RESEARCH_ID,
    "price_basis": "raw_unadjusted",
    "yang": "close > open",
    "body": "close - open",
    "upper_shadow": "high - max(open, close)",
    "lower_shadow": "min(open, close) - low",
    "min_body_pct_of_open": MIN_BODY_PCT_OF_OPEN,
    "anchor": "low",
    "break_rule": "subsequent low < anchor low",
    "equal_low": "touch_is_not_break",
    "window": DEFAULT_WINDOW,
    "window_unit": "trading_days_after_T",
    "signal_timing": "T_close_confirmed; evaluation_starts_T+1",
    "oos": "required_but_not_implemented",
}


@dataclass(frozen=True)
class Bar:
    """一根 raw、不复权日 K；字段必须来自同一 generation。"""

    open: float
    high: float
    low: float
    close: float


def is_single_yang(bar: Bar) -> bool:
    """判断单根 bar 是否满足固定阳线与实体阈值。"""

    return bar.close > bar.open and bar.open > 0 and (bar.close - bar.open) / bar.open >= MIN_BODY_PCT_OF_OPEN


def detect_single_yang(bars: Sequence[Bar]) -> list[int]:
    """返回已完成固定五日观察窗口且后续未破锚点低点的形态日索引。

    这是规格函数而非生产信号入口；窗口必须完整，后续 low 等于锚点只算
    触及，只有严格小于锚点才算破低点。形态只能在锚点日后第 5 根收盘
    才确认，不能把锚点日 T+1 当作已确认信号。
    """
    signals: list[int] = []
    last_anchor = len(bars) - DEFAULT_WINDOW - 1
    for index, bar in enumerate(bars[: max(0, last_anchor + 1)]):
        if not is_single_yang(bar):
            continue
        anchor_low = bar.low
        follow_up = bars[index + 1 : index + DEFAULT_WINDOW + 1]
        if all(next_bar.low >= anchor_low for next_bar in follow_up):
            signals.append(index)
    return signals


def assess_capability() -> dict[str, Any]:
    """报告当前能力；三重门禁未满足时恒为不可用。"""

    return {"available": False, "reasons": list(UNAVAILABLE_REASONS)}


def run_single_yang_research(*, bars: Sequence[Bar] | None = None) -> dict[str, Any]:
    """返回稳定的 unavailable 研究契约，不消费 bars、不产生信号。"""

    del bars
    capability = assess_capability()
    return {
        "status": "unavailable",
        "reasons": capability["reasons"],
        "definition": SINGLE_YANG_DEFINITION.copy(),
        "note": "研究状态机与 OOS 协议未实现；即使 reader 补齐也保持 unavailable。",
    }
