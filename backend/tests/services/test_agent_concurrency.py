"""F16 Agent 并发上限：进程内占满立即中文报错，不排队；释放后可复用。"""
import asyncio
import json
from types import SimpleNamespace

import pytest

from app.services.agent_loop import MAX_CONCURRENT_AGENT_RUNS, run_agent_stream


async def _collect(agen):
    return [json.loads(line) async for line in agen]


async def _immediate_generate_tool(messages, tools, **kw):
    return "直接回答", None


async def _fake_stream(messages, **kw):
    yield "ok"


class _HangingTool:
    """generate_tool 挂起直到 gate 置位，用于占住并发槽位。"""

    def __init__(self) -> None:
        self.gate = asyncio.Event()
        self.calls = 0

    async def __call__(self, messages, tools, **kw):
        self.calls += 1
        await self.gate.wait()
        return "直接回答", None


async def _drive_until(fn, predicate, *, rounds: int = 100):
    for _ in range(rounds):
        if predicate():
            return
        await asyncio.sleep(0)
    raise AssertionError("condition not reached in event loop rounds")


@pytest.mark.asyncio
async def test_concurrent_runs_above_limit_error_immediately():
    hanging = _HangingTool()
    holders = []
    try:
        for _ in range(MAX_CONCURRENT_AGENT_RUNS):
            gen = run_agent_stream(
                [{"role": "user", "content": "hi"}],
                SimpleNamespace(),
                generate_tool=hanging,
                stream=_fake_stream,
            )
            holders.append(asyncio.ensure_future(_collect(gen)))
        await _drive_until(None, lambda: hanging.calls == MAX_CONCURRENT_AGENT_RUNS)

        # 第三路：占满后应立即 error，不排队等待
        events = await _collect(
            run_agent_stream(
                [{"role": "user", "content": "third"}],
                SimpleNamespace(),
                generate_tool=hanging,
                stream=_fake_stream,
            )
        )
        assert len(events) == 1
        assert events[0]["type"] == "error"
        assert "已有研究对话在运行，请等待结束后再试" in events[0]["message"]
    finally:
        hanging.gate.set()
        for task in holders:
            await task


@pytest.mark.asyncio
async def test_slot_released_after_completion():
    for _ in range(MAX_CONCURRENT_AGENT_RUNS + 2):
        events = await _collect(
            run_agent_stream(
                [{"role": "user", "content": "hi"}],
                SimpleNamespace(),
                generate_tool=_immediate_generate_tool,
                stream=_fake_stream,
            )
        )
        assert [e["type"] for e in events] == ["delta", "done"]


@pytest.mark.asyncio
async def test_slot_released_when_consumer_disconnects_mid_stream():
    hanging = _HangingTool()
    holders = []
    try:
        for _ in range(MAX_CONCURRENT_AGENT_RUNS - 1):
            gen = run_agent_stream(
                [{"role": "user", "content": "hi"}],
                SimpleNamespace(),
                generate_tool=hanging,
                stream=_fake_stream,
            )
            holders.append(asyncio.ensure_future(_collect(gen)))
        await _drive_until(None, lambda: hanging.calls == MAX_CONCURRENT_AGENT_RUNS - 1)

        async def endless_stream(messages, **kw):
            for i in range(100):
                yield f"chunk{i}"

        agen = run_agent_stream(
            [{"role": "user", "content": "hi"}],
            SimpleNamespace(),
            generate_tool=_immediate_generate_tool,
            stream=endless_stream,
        )
        iterator = agen.__aiter__()
        first = json.loads(await iterator.__anext__())
        assert first["type"] == "delta"
        await agen.aclose()

        # 关闭消费方后槽位必须归还：此前 MAX-1 路挂起 + 本路已关，新一路应能直接进入并完成
        events = await _collect(
            run_agent_stream(
                [{"role": "user", "content": "after close"}],
                SimpleNamespace(),
                generate_tool=_immediate_generate_tool,
                stream=_fake_stream,
            )
        )
        assert [e["type"] for e in events] == ["delta", "done"]
    finally:
        hanging.gate.set()
        for task in holders:
            await task


def test_start_pool_backtest_declares_job_resource():
    from app.services.agent_tools import TOOLS, to_openai_tools

    tool = next(t for t in TOOLS if t["name"] == "start_pool_backtest")
    assert tool["resource_kind"] == "job"
    assert tool["read_only"] is True
    # 描述保持「会创建…任务」语义
    assert "会创建回测计算任务与研究 artifact" in tool["description"]
    # resource_kind 是内部标注，不进 OpenAI function-calling schema
    assert "resource_kind" not in json.dumps(to_openai_tools([tool]), ensure_ascii=False)
    # 其余工具不携带该标注
    others = [t for t in TOOLS if t["name"] != "start_pool_backtest"]
    assert all("resource_kind" not in t for t in others)
