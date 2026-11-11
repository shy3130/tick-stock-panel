from __future__ import annotations

import pytest

from app.strategy.ai_generator import GUIDE_PATH, AIStrategyGenerator
from app.strategy.prompt_builder import build_step1, build_step2


def test_missing_guide_stops_generation(monkeypatch, tmp_path):
    from app.strategy import ai_generator

    monkeypatch.setattr(ai_generator, "GUIDE_PATH", tmp_path / "missing.md")
    with pytest.raises(RuntimeError, match="策略生成指南"):
        AIStrategyGenerator()._get_guide()


def test_guide_and_validation_follow_current_signal_library(monkeypatch, tmp_path):
    from app.config import settings
    from app.strategy import custom_signals

    monkeypatch.setattr(settings, "data_dir", tmp_path)
    custom_signals.save_one(tmp_path, {
        "id": "audit", "name": "测试信号", "kind": "entry", "enabled": True,
        "conditions": [{"left": "close", "op": ">", "right": "10"}],
    })
    generator = AIStrategyGenerator()
    code = """import polars as pl
META = {"id": "ai_signal", "scoring": {}}
ENTRY_SIGNALS: list = ["csg_audit"]
EXIT_SIGNALS = []
def filter(df, params):
    return pl.lit(True)
"""
    assert "`csg_audit`" in generator._get_guide()
    assert generator.validate_code(code)["valid"]
    invalid = code.replace("csg_audit", "signal_entry_oversold_rebound")
    result = generator.validate_code(invalid)
    assert not result["valid"] and "signal_entry_oversold_rebound" in result["error"]

    sig = custom_signals.load_one(tmp_path, "audit")
    sig["enabled"] = False
    custom_signals.save_one(tmp_path, sig)
    assert "`csg_audit`" not in generator._get_guide()
    assert not generator.validate_code(code)["valid"]
    assert generator.validate_code(code.replace('["csg_audit"]', "[]"))["valid"]


@pytest.mark.asyncio
async def test_generate_repairs_invented_signals_once(monkeypatch, tmp_path):
    from app.config import settings

    monkeypatch.setattr(settings, "data_dir", tmp_path)
    code = """import polars as pl
META = {"id": "ai_signal", "scoring": {}}
ENTRY_SIGNALS = ["entry_oversold_rebound"]
EXIT_SIGNALS = ["exit_holding_period"]
def filter_history(df, params):
    return df.with_columns(pl.lit(True).alias("signal_entry_oversold_rebound"))
"""
    calls = []

    async def generate(self, prompt, guide):
        calls.append(prompt)
        return code if len(calls) == 1 else code.replace(
            '["entry_oversold_rebound"]', "[]",
        ).replace('["exit_holding_period"]', "[]")

    monkeypatch.setattr(AIStrategyGenerator, "_call_llm", generate)
    invalid = AIStrategyGenerator().validate_code(code)
    assert "entry_oversold_rebound" in invalid["error"] and "exit_holding_period" in invalid["error"]
    result = await AIStrategyGenerator().generate("生成策略")
    assert result["valid"] and len(calls) == 2
    assert "entry_oversold_rebound" in calls[1] and "exit_holding_period" in calls[1]


def test_ai_strategy_generator_uses_compact_guide():
    assert GUIDE_PATH.name == "strategy-guide-compact.md"

    guide = AIStrategyGenerator()._get_guide()

    assert "AI 策略生成精简指南" in guide
    assert "策略示例" not in guide
    assert len(guide) < 7000


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


def test_build_step1_without_basic_filter_keeps_prompt_unchanged_shape():
    """basic_filter=None: 提示词不含默认基础过滤段 (既有行为零变化)。"""
    prompt = build_step1("测试策略", "测试描述", "long", "1. 规则", "")

    assert "默认基础过滤" not in prompt
    assert "策略规则：\n1. 规则" in prompt


def test_build_step1_injects_user_default_basic_filter():
    """basic_filter 传入: 提示词要求 META.basic_filter 严格采用用户默认值。"""
    bf = {
        "price_min": 3, "price_max": 100,
        "float_cap_min": 20e8, "float_cap_max": None,
        "exclude_st": False, "boards": ["沪主板", "深主板"],
    }

    prompt = build_step1("测试策略", "测试描述", "long", "1. 规则", "", basic_filter=bf)

    assert "默认基础过滤" in prompt
    assert "'price_min': 3" in prompt
    # Python 字面量语义 (True/False/None), 而非 JSON 的 true/false/null
    assert "'exclude_st': False" in prompt
    assert "'float_cap_max': None" in prompt
    assert "'boards': ['沪主板', '深主板']" in prompt


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
