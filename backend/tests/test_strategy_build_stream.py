from __future__ import annotations

import json

import pytest

from app.api.strategy import BuildRequest, build_strategy_stream
from app.strategy.ai_generator import AIStrategyGenerator


STREAM_CODE = '''"""测试策略"""
import polars as pl

META = {
    "id": "wrong",
    "name": "旧名",
    "description": "旧描述",
    "tags": ["测试"],
    "params": [],
    "scoring": {},
}

ENTRY_SIGNALS = []
EXIT_SIGNALS = []
STOP_LOSS = -0.05
MAX_HOLD_DAYS = 20
ALERTS = []

RULES = """
1. 测试规则一
2. 测试规则二
3. 测试规则三
"""

def filter(df: pl.DataFrame, params: dict) -> pl.Expr:
    return pl.lit(True)
'''


@pytest.mark.asyncio
async def test_build_strategy_stream_yields_delta_and_normalized_result(monkeypatch):
    async def fake_stream(self, prompt):
        yield STREAM_CODE[:40]
        yield STREAM_CODE[40:]

    monkeypatch.setattr(AIStrategyGenerator, "stream", fake_stream)
    req = BuildRequest(
        step=1,
        name="新策略",
        description="新描述",
        direction="long",
        rules="1. 规则一\n2. 规则二\n3. 规则三",
        strategy_id="ai_streamed",
    )

    response = await build_strategy_stream(req, None)
    body = b""
    async for chunk in response.body_iterator:
        body += chunk.encode("utf-8") if isinstance(chunk, str) else chunk

    events = [json.loads(line) for line in body.decode("utf-8").splitlines()]

    assert [event["type"] for event in events] == ["meta", "delta", "delta", "result"]
    result = events[-1]
    assert result["valid"] is True
    assert result["meta"]["id"] == "ai_streamed"
    assert result["meta"]["name"] == "新策略"
    assert '"id": "ai_streamed"' in result["code"]
