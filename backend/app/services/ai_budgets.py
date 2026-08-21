"""AI 入口的中央预算注册表。

受控入口 (nl_screener / strategy_profile_deep_review / trading_autopsy /
stock_analysis / financials / market_recap / agent / strategy_generate /
trading_plan_check_stage1/2) 的生成参数集中在此，作为单一事实源与上限护栏。

- 上限即各入口现状值，不得被调用方放大 (``resolve_budget`` 仅向下 clamp)；
- ``max_tokens`` 为 completion 预算，``context_max_tokens`` 为 prompt 上下文预算
  (仅 stock_analysis 组装分层 prompt 时使用)，``timeout`` 为单次 provider 超时；
- temperature 随入口默认，不属于"预算"，不做 clamp，仅作为默认值集中暴露。

本模块无副作用、无 IO，便于单元测试。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import overload


@dataclass(frozen=True)
class EntryBudget:
    """单个入口的生成预算快照 (不可变)。"""

    purpose: str
    temperature: float
    max_tokens: int
    timeout: float
    context_max_tokens: int | None = None


# 上限 = 各入口现状值；调用方只允许更小。
_BUDGETS: dict[str, EntryBudget] = {
    "nl_screener": EntryBudget("nl_screener", 0.0, 2000, 60.0),
    "strategy_profile_deep_review": EntryBudget(
        "strategy_profile_deep_review", 0.2, 2000, 60.0
    ),
    "trading_autopsy": EntryBudget("trading_autopsy", 0.2, 2000, 60.0),
    "trading_plan_check_stage1": EntryBudget("trading_plan_check_stage1", 0.3, 2500, 90.0),
    "trading_plan_check_stage2": EntryBudget("trading_plan_check_stage2", 0.2, 2000, 90.0),
    "stock_analysis": EntryBudget(
        "stock_analysis", 0.5, 4500, 180.0, context_max_tokens=12000
    ),
    "financials": EntryBudget("financials", 0.4, 4000, 180.0),
    "market_recap": EntryBudget("market_recap", 0.5, 4500, 180.0),
    "agent": EntryBudget("agent", 0.2, 1600, 90.0),
    "strategy_generate": EntryBudget("strategy_generate", 0.3, 3000, 120.0),
}


def entry_purposes() -> list[str]:
    """受控入口 purpose key 列表 (稳定顺序)。"""
    return list(_BUDGETS.keys())


@overload
def get_entry_budget(purpose: str) -> EntryBudget: ...


def get_entry_budget(purpose: str) -> EntryBudget:
    """返回入口的预算上限；未知 purpose 抛 KeyError (避免静默放宽)。"""
    if purpose not in _BUDGETS:
        raise KeyError(f"unknown AI entry purpose: {purpose}")
    return _BUDGETS[purpose]


def resolve_budget(
    purpose: str,
    *,
    temperature: float | None = None,
    max_tokens: int | None = None,
    timeout: float | None = None,
    context_max_tokens: int | None = None,
) -> EntryBudget:
    """以入口上限为基准解析预算；显式入参仅允许向下 clamp，绝不放大上限。

    ``temperature`` 无 clamp 语义，仅作为默认值集中取值 (调用方显式传入时采用调用方值)。
    """
    base = get_entry_budget(purpose)
    resolved_ctx: int | None
    if base.context_max_tokens is None:
        resolved_ctx = None
    elif context_max_tokens is not None:
        resolved_ctx = min(int(context_max_tokens), base.context_max_tokens)
    else:
        resolved_ctx = base.context_max_tokens
    return EntryBudget(
        purpose=purpose,
        temperature=base.temperature if temperature is None else float(temperature),
        max_tokens=min(int(max_tokens), base.max_tokens) if max_tokens is not None else base.max_tokens,
        timeout=min(float(timeout), base.timeout) if timeout is not None else base.timeout,
        context_max_tokens=resolved_ctx,
    )


__all__ = ["EntryBudget", "entry_purposes", "get_entry_budget", "resolve_budget"]
