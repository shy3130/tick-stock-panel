import json

import pytest

from app.services.agent_loop import run_agent_stream


class _FakeState:
    class _Engine:
        def list_strategies(self):
            return []

    strategy_engine = _Engine()


async def _collect(agen):
    return [json.loads(line) async for line in agen]


@pytest.mark.asyncio
async def test_agent_loop_single_tool_then_answer():
    calls = {"n": 0}

    async def fake_generate(messages, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            return '{"tool":"list_strategies","args":{}}'
        return "no more tools"

    async def fake_stream(messages, **kw):
        for chunk in ["答", "案"]:
            yield chunk

    events = await _collect(
        run_agent_stream(
            [{"role": "user", "content": "有哪些策略"}],
            _FakeState(),
            generate=fake_generate,
            stream=fake_stream,
        )
    )
    assert [e["type"] for e in events] == ["tool_call", "tool_result", "delta", "delta", "done"]
    assert events[0]["name"] == "list_strategies"
    assert events[1]["result"] == {"strategies": []}
    assert "".join(e["content"] for e in events if e["type"] == "delta") == "答案"


@pytest.mark.asyncio
async def test_agent_loop_direct_answer_no_tool():
    async def fake_generate(messages, **kw):
        return "直接回答，无需工具"

    async def fake_stream(messages, **kw):
        yield "你好"

    events = await _collect(
        run_agent_stream(
            [{"role": "user", "content": "hi"}],
            _FakeState(),
            generate=fake_generate,
            stream=fake_stream,
        )
    )
    assert [e["type"] for e in events] == ["delta", "done"]


@pytest.mark.asyncio
async def test_agent_loop_caps_at_five_rounds():
    async def fake_generate(messages, **kw):
        return '{"tool":"list_strategies","args":{}}'

    async def fake_stream(messages, **kw):
        yield "最终"

    events = await _collect(
        run_agent_stream(
            [{"role": "user", "content": "loop"}],
            _FakeState(),
            generate=fake_generate,
            stream=fake_stream,
        )
    )
    assert sum(1 for e in events if e["type"] == "tool_call") == 5
    assert events[-1]["type"] == "done"


@pytest.mark.asyncio
async def test_agent_loop_rejects_excluded_and_unknown_tools_then_done():
    seq = iter([
        '{"tool":"run_backtest","args":{"strategy_id":"x"}}',
        '{"tool":"nope","args":{}}',
    ])

    async def fake_generate(messages, **kw):
        try:
            return next(seq)
        except StopIteration:
            return "普通回答"

    async def fake_stream(messages, **kw):
        yield "好"

    events = await _collect(
        run_agent_stream(
            [{"role": "user", "content": "跑个全市场回测"}],
            _FakeState(),
            generate=fake_generate,
            stream=fake_stream,
        )
    )
    results = [e for e in events if e["type"] == "tool_result"]
    assert len(results) == 2
    assert all("error" in r["result"] for r in results)
    assert events[-1]["type"] == "done"
