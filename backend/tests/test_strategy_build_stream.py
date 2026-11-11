from __future__ import annotations

import asyncio
import functools
import json
from types import SimpleNamespace

import pytest

from app.api import strategy as strategy_api
from app.api.strategy import BuildRequest, build_strategy_stream
from app.services.ndjson_heartbeat import with_heartbeat
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

RULES = """
1. 测试规则一
2. 测试规则二
3. 测试规则三
"""

def filter(df: pl.DataFrame, params: dict) -> pl.Expr:
    return pl.lit(True)
'''


@pytest.fixture
def stream_request(tmp_path):
    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(
        repo=SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path)),
    )))


@pytest.mark.asyncio
async def test_build_strategy_stream_yields_delta_and_normalized_result(monkeypatch, stream_request):
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

    response = await build_strategy_stream(req, stream_request)
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


@pytest.mark.asyncio
async def test_build_strategy_stream_repairs_missing_meta_once(monkeypatch, stream_request):
    calls = 0

    async def fake_stream(self, prompt):
        yield "import polars as pl\n\ndef filter(df, params):\n    return pl.lit(True)\n"

    async def fake_repair(self, code, error):
        nonlocal calls
        calls += 1
        return self.validate_code(STREAM_CODE)

    monkeypatch.setattr(AIStrategyGenerator, "stream", fake_stream)
    monkeypatch.setattr(AIStrategyGenerator, "repair_code", fake_repair)
    req = BuildRequest(
        step=1,
        name="修复后策略",
        description="修复后描述",
        direction="long",
        rules="1. 测试规则",
        strategy_id="ai_repaired",
    )

    response = await build_strategy_stream(req, stream_request)
    body = b""
    async for chunk in response.body_iterator:
        body += chunk.encode("utf-8") if isinstance(chunk, str) else chunk

    result = json.loads(body.decode("utf-8").splitlines()[-1])
    assert calls == 1
    assert result["type"] == "result"
    assert result["valid"] is True
    assert result["meta"]["id"] == "ai_repaired"
    assert result["meta"]["name"] == "修复后策略"


async def _read_body(response) -> str:
    body = b""
    async for chunk in response.body_iterator:
        body += chunk.encode("utf-8") if isinstance(chunk, str) else chunk
    return body.decode("utf-8")


def _parse_like_frontend(text: str) -> tuple[list[dict], list[str]]:
    """与前端 api.strategyBuildStream 同口径: 按换行切行, JSON 解析失败的行被静默跳过。"""
    events: list[dict] = []
    dropped: list[str] = []
    for line in text.split("\n"):
        s = line.strip()
        if not s:
            continue
        try:
            events.append(json.loads(s))
        except json.JSONDecodeError:
            dropped.append(s)
    return events, dropped


@pytest.mark.asyncio
async def test_build_strategy_stream_heartbeat_does_not_swallow_events(monkeypatch, stream_request):
    """LLM 首包前与结构修复期间空闲超过心跳间隔: ping 必须独占一行, 不能把 delta/result 粘掉。"""

    async def slow_stream(self, prompt):
        await asyncio.sleep(0.2)  # 推理模型思考期, 流上无字节
        yield "import polars as pl\n\ndef filter(df, params):\n    return pl.lit(True)\n"

    async def slow_repair(self, code, error):
        await asyncio.sleep(0.2)  # 修复是一次非流式 LLM 调用
        return self.validate_code(STREAM_CODE)

    monkeypatch.setattr(AIStrategyGenerator, "stream", slow_stream)
    monkeypatch.setattr(AIStrategyGenerator, "repair_code", slow_repair)
    monkeypatch.setattr(strategy_api, "with_heartbeat", functools.partial(with_heartbeat, interval=0.05))
    req = BuildRequest(
        step=1,
        name="心跳策略",
        description="心跳描述",
        direction="long",
        rules="1. 测试规则",
        strategy_id="ai_heartbeat",
    )

    text = await _read_body(await build_strategy_stream(req, stream_request))
    events, dropped = _parse_like_frontend(text)
    types = [event["type"] for event in events]

    assert [t for t in types if t != "ping"] == ["meta", "delta", "result"]
    assert "ping" in types
    assert dropped == []
    assert events[-1]["valid"] is True
    assert events[-1]["meta"]["id"] == "ai_heartbeat"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("exc", "message"),
    [
        (RuntimeError("AI API Key 未配置"), "AI API Key 未配置"),
        (ValueError("boom"), "AI生成失败: boom"),
    ],
)
async def test_build_strategy_stream_error_event_is_own_line(monkeypatch, exc, message, stream_request):
    async def failing_stream(self, prompt):
        raise exc
        yield  # pragma: no cover - 使其成为异步生成器

    monkeypatch.setattr(AIStrategyGenerator, "stream", failing_stream)
    req = BuildRequest(step=2, current_code="x = 1", instruction="改一下", strategy_id="")

    text = await _read_body(await build_strategy_stream(req, stream_request))
    events, dropped = _parse_like_frontend(text)

    assert dropped == []
    assert [event["type"] for event in events] == ["meta", "error"]
    assert events[-1]["message"] == message
    assert text.endswith("\n")
