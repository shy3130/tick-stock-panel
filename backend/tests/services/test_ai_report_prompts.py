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
