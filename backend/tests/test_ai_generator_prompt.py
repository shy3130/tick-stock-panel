from __future__ import annotations

import pytest

from app.strategy.ai_generator import GUIDE_PATH, AIStrategyGenerator
from app.strategy.prompt_builder import build_step1, build_step2


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


def test_build_step2_uses_runtime_meta_constraints():
    prompt = build_step2("META = {}", "调整参数和评分")

    assert "权重总和保持 1.0" in prompt
    assert "权重总和保持 100" not in prompt
    assert "filter()`、`filter_history()` 或 `MATRIX_STRATEGY" in prompt
    assert "没有匹配信号时允许为空" in prompt
    assert "模块顶层字面量字典" in prompt


def test_matrix_backend_prompt_and_imports_are_supported():
    prompt = build_step1(
        "矩阵策略",
        "矩阵原生示例",
        "long",
        "收盘价站上 MA20",
        "ai_matrix",
        "matrix_native",
    )
    assert "执行后端：matrix_native" in prompt

    AIStrategyGenerator._validate_safety(
        "import numpy as np\n"
        "from app.backtest.matrix import MarketDataMatrix, SignalMatrix, make_signal_matrix\n"
    )


@pytest.mark.asyncio
async def test_generate_only_repairs_structural_output_once(monkeypatch):
    calls = 0

    async def fake_call_llm(self, user_prompt, guide):
        nonlocal calls
        calls += 1
        return "import polars as pl\n\ndef filter(df, params):\n    return pl.lit(True)"

    monkeypatch.setattr(AIStrategyGenerator, "_call_llm", fake_call_llm)

    result = await AIStrategyGenerator().generate("生成测试策略")

    assert calls == 2
    assert result["valid"] is False
    assert "找不到 META 字典" in result["error"]
