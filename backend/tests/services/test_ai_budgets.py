"""P3 中央预算注册表与 usage 快照行为测试。"""
from __future__ import annotations

import pytest

from app.services.ai_budgets import (
    EntryBudget,
    entry_purposes,
    get_entry_budget,
    resolve_budget,
)
from app.services.ai_structured.models import AIUsage, StructuredAIResult
from app.services.ai_usage_snapshot import (
    UsageRegistry,
    get_usage_registry,
    usage_snapshot,
)


def test_entry_purposes_cover_four_structured_entries():
    purposes = entry_purposes()
    assert set(purposes) >= {
        "nl_screener",
        "strategy_profile_deep_review",
        "trading_autopsy",
        "stock_analysis",
    }


def test_plan_check_purposes_registered():
    """P4: 计划检查两阶段入口必须注册预算上限。"""
    b1 = get_entry_budget("trading_plan_check_stage1")
    b2 = get_entry_budget("trading_plan_check_stage2")
    assert b1.max_tokens > 0 and b1.timeout > 0
    assert b2.max_tokens > 0 and b2.timeout > 0
    assert resolve_budget("trading_plan_check_stage1", max_tokens=999_999).max_tokens == b1.max_tokens
    assert resolve_budget("trading_plan_check_stage2", max_tokens=999_999).max_tokens == b2.max_tokens


def test_report_and_agent_purposes_registered():
    purposes = set(entry_purposes())
    assert {"financials", "market_recap", "agent", "strategy_generate"} <= purposes
    assert resolve_budget("financials", max_tokens=999_999).max_tokens == 4000
    assert resolve_budget("market_recap", max_tokens=999_999).max_tokens == 4500
    assert resolve_budget("agent", max_tokens=999_999).max_tokens == 1600
    assert resolve_budget("agent", max_tokens=1200).max_tokens == 1200
    assert resolve_budget("strategy_generate", max_tokens=999_999).max_tokens == 3000



def test_get_entry_budget_returns_current_caps():
    b = get_entry_budget("nl_screener")
    assert isinstance(b, EntryBudget)
    # 上限即现状值，不得被调用方放大
    assert b.max_tokens == 2000
    assert b.timeout == 60.0
    assert b.context_max_tokens is None  # 只有 stock_analysis 有上下文预算
    sa = get_entry_budget("stock_analysis")
    assert sa.context_max_tokens == 12000
    assert sa.max_tokens == 4500


def test_resolve_budget_clamps_down_never_up():
    clamped = resolve_budget("trading_autopsy", max_tokens=999_999, timeout=9999.0)
    assert clamped.max_tokens == 2000
    assert clamped.timeout == 60.0
    # 显式更小的值被保留
    smaller = resolve_budget("trading_autopsy", max_tokens=500, timeout=10.0)
    assert smaller.max_tokens == 500
    assert smaller.timeout == 10.0


def test_resolve_budget_context_clamp_for_stock_analysis():
    b = resolve_budget("stock_analysis", context_max_tokens=999_999)
    assert b.context_max_tokens == 12000
    smaller = resolve_budget("stock_analysis", context_max_tokens=4000)
    assert smaller.context_max_tokens == 4000


def test_resolve_budget_unknown_purpose_raises():
    with pytest.raises(KeyError):
        resolve_budget("not_a_real_purpose")


def test_temperature_is_defaulted_not_clamped():
    # temperature 无 clamp 语义，调用方显式传入时采用调用方值
    b = resolve_budget("nl_screener", temperature=0.7)
    assert b.temperature == 0.7
    assert resolve_budget("nl_screener").temperature == 0.0


def _make_result(purpose: str = "nl_screener", status: str = "ok", **kw) -> StructuredAIResult:
    base = dict(
        request_id="req_1",
        attempt_id="att_1",
        status=status,
        purpose=purpose,
        usage=AIUsage(prompt_tokens=100, cached_prompt_tokens=30, completion_tokens=5, total_tokens=105),
    )
    base.update(kw)
    return StructuredAIResult(**base)


def test_usage_registry_accumulates_by_purpose_and_day():
    reg = UsageRegistry()
    reg.record("nl_screener", AIUsage(prompt_tokens=10, completion_tokens=2, total_tokens=12))
    reg.record("nl_screener", AIUsage(prompt_tokens=5, cached_prompt_tokens=3, completion_tokens=1, total_tokens=6))
    snap = reg.snapshot()
    p = snap["by_purpose"]["nl_screener"]
    assert p["prompt_tokens"] == 15
    assert p["cached_prompt_tokens"] == 3
    assert p["completion_tokens"] == 3
    assert p["total_tokens"] == 18
    assert p["calls"] == 2
    # by_day 至少有今天
    assert len(snap["by_day"]) >= 1


def test_record_result_skips_cancelled():
    reg = UsageRegistry()
    reg.record_result("nl_screener", _make_result(status="cancelled"))
    snap = reg.snapshot()
    assert snap["by_purpose"] == {}


def test_global_usage_snapshot_is_readonly_shape():
    # 进程级单例 snapshot 必须返回 by_purpose / by_day 两键，不含敏感内容
    snap = usage_snapshot()
    assert "by_purpose" in snap and "by_day" in snap
    get_usage_registry()  # 单例可取


def test_build_ai_meta_reflects_actual_profile_and_cached_tokens():
    from app.services.ai_structured import build_ai_meta

    result = _make_result(
        profile_id="p_actual",
        primary_profile_id="p_primary",
        fallback_used=True,
        fallback_reason="quota",
        provider="openai_compat",
        model="gpt-4o",
    )
    meta = build_ai_meta(result)
    assert meta["profile_id"] == "p_actual"
    assert meta["primary_profile_id"] == "p_primary"
    assert meta["fallback_used"] is True
    assert meta["fallback_reason"] == "quota"
    assert meta["usage"]["cached_prompt_tokens"] == 30
    assert meta["usage"]["total_tokens"] == 105
