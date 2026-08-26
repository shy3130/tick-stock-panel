"""Report / Agent 系统提示词不得下达交易指令 (AI 评审 F1/F4)。"""

from __future__ import annotations

from app.services.agent_loop import _final_system, _tools_system
from app.services.financial_analyzer import _SYSTEM_PROMPT as FINANCIAL_PROMPT
from app.services.market_recap import _SYSTEM_PROMPT as RECAP_PROMPT
from app.services.stock_analyzer import _SYSTEM_PROMPT as STOCK_PROMPT

# 旧口径把模型当荐股/下单助手。禁词必须是「任务口号」,不是禁令里的否定句。
_FORBIDDEN_SLOGANS = (
    "可直接指导交易决策",
    "可直接指导次日仓位",
    "可直接用于投资决策",
    "【操作建议:",
    "**建议买入区间**",
    "仓位区间建议",
    "明日交易计划",
    "AI 选股助手",
)


def test_report_prompts_are_research_not_advice():
    blob = "\n".join([STOCK_PROMPT, FINANCIAL_PROMPT, RECAP_PROMPT])
    for slogan in _FORBIDDEN_SLOGANS:
        assert slogan not in blob, slogan
    assert "只读研究助手" in STOCK_PROMPT
    assert "观察清单" in STOCK_PROMPT
    assert "财务质量" in FINANCIAL_PROMPT
    assert "次日观察清单" in RECAP_PROMPT


def test_agent_prompts_are_readonly_research():
    tools = _tools_system()
    final = _final_system()
    assert "只读研究助手" in tools
    assert "AI 选股助手" not in tools
    assert "不要给出买入、卖出、加仓、目标价或仓位指令" in tools
    assert "不要给出买入、卖出、加仓、目标价或仓位指令" in final


def test_agent_prompts_pin_short_pool_determinism():
    """AI 短线池红线: 必须用 screen_stock_pool preset, 不得自行条件化/增删重排。"""
    tools = _tools_system()
    final = _final_system()
    assert "preset_id=short_momentum_quality_v1" in tools
    assert "不得自行条件化" in tools
    assert "不得生成、删除或重排候选" in tools
    assert "short_momentum_quality_v1 的 pool_id 不兼容该工具，禁止传入" in tools
    # 封套实际字段是 pool_id，提示词不得再引用不存在的 short_pool_id 字段名
    assert "short_pool_id" not in tools
    assert "short_pool_id" not in final
    assert "evidence" in final
    assert "不得增删或重排" in final
    assert "禁止荐股口吻和任何交易指令" in final
    assert "market_state 是严格 T-1 的确定性市场状态" in tools
    assert "protocol_id 只是研究协议标识而非既有策略" in tools
    assert "必须由用户显式确认创建研究假设" in tools
    assert "绝不得自动运行回测" in tools
    assert "不得声称复刻任何未公开公式" in final
    assert "不得把市场状态解释成直接买卖点" in final
