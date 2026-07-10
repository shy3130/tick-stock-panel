from __future__ import annotations

from app.strategy.ai_generator import GUIDE_PATH, AIStrategyGenerator
from app.strategy.prompt_builder import build_step1


def test_ai_strategy_generator_uses_compact_guide():
    assert GUIDE_PATH.name == "strategy-guide-compact.md"

    guide = AIStrategyGenerator()._get_guide()

    assert "AI 策略生成精简指南" in guide
    assert "策略示例" not in guide
    assert len(guide) < 5000


def test_build_step1_keeps_user_prompt_compact():
    prompt = build_step1(
        "测试策略",
        "测试描述",
        "long",
        "1. 收盘价站上 MA20\n2. 成交量放大\n3. RSI 不过热",
        "ai_test",
    )

    assert "# 步骤 1：根据规则生成完整策略" not in prompt
    assert "模式 A 框架" not in prompt
    assert "策略ID（必须使用此ID）：ai_test" in prompt
    assert len(prompt) < 1000
