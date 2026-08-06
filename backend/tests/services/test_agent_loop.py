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
async def test_agent_loop_native_tool_call_then_answer():
    """原生 function calling：generate_tool 返回 (None, tool_calls) → 执行 → 最终回答。"""
    calls = {"n": 0}

    async def fake_generate_tool(messages, tools, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            return None, [{"id": "call_1", "name": "list_strategies", "arguments": "{}"}]
        return "不需要更多工具", None

    async def fake_stream(messages, **kw):
        for chunk in ["答", "案"]:
            yield chunk

    events = await _collect(
        run_agent_stream(
            [{"role": "user", "content": "有哪些策略"}],
            _FakeState(),
            generate_tool=fake_generate_tool,
            stream=fake_stream,
        )
    )
    assert [e["type"] for e in events] == ["tool_call", "tool_result", "delta", "delta", "done"]
    assert events[0]["name"] == "list_strategies"
    assert events[1]["result"] == {"strategies": []}
    assert "".join(e["content"] for e in events if e["type"] == "delta") == "答案"
    assert events[1]["elapsed_ms"] >= 0
    assert events[-1]["elapsed_ms"] >= events[1]["elapsed_ms"]


@pytest.mark.asyncio
async def test_agent_loop_direct_answer_no_tool():
    """模型直接回答，不调工具。"""

    async def fake_generate_tool(messages, tools, **kw):
        return "直接回答，无需工具", None

    async def fake_stream(messages, **kw):
        yield "你好"

    events = await _collect(
        run_agent_stream(
            [{"role": "user", "content": "hi"}],
            _FakeState(),
            generate_tool=fake_generate_tool,
            stream=fake_stream,
        )
    )
    assert [e["type"] for e in events] == ["delta", "done"]


@pytest.mark.asyncio
async def test_agent_loop_caps_at_five_rounds():
    """模型连续调工具 5 次后被 MAX_TOOL_ROUNDS 截断。"""

    async def fake_generate_tool(messages, tools, **kw):
        return None, [{"id": "call_x", "name": "list_strategies", "arguments": "{}"}]

    async def fake_stream(messages, **kw):
        yield "最终"

    events = await _collect(
        run_agent_stream(
            [{"role": "user", "content": "loop"}],
            _FakeState(),
            generate_tool=fake_generate_tool,
            stream=fake_stream,
        )
    )
    assert sum(1 for e in events if e["type"] == "tool_call") == 5
    assert events[-1]["type"] == "done"


@pytest.mark.asyncio
async def test_agent_loop_rejects_unknown_native_tool():
    """原生路径：模型调了不存在的工具 → tool_result 带错误 → 正常结束。"""
    calls = {"n": 0}

    async def fake_generate_tool(messages, tools, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            return None, [{"id": "call_1", "name": "nope", "arguments": "{}"}]
        return "普通回答", None

    async def fake_stream(messages, **kw):
        yield "好"

    events = await _collect(
        run_agent_stream(
            [{"role": "user", "content": "用一个不存在的工具"}],
            _FakeState(),
            generate_tool=fake_generate_tool,
            stream=fake_stream,
        )
    )
    results = [e for e in events if e["type"] == "tool_result"]
    assert len(results) == 1
    assert "error" in results[0]["result"]
    assert events[-1]["type"] == "done"


@pytest.mark.asyncio
async def test_agent_loop_fallback_prompt_mode():
    """降级路径：generate_tool 返回 (text, None)，text 里嵌了 JSON tool 请求。"""
    calls = {"n": 0}

    async def fake_generate_tool(messages, tools, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            text = '{"tool":"list_strategies","args":{}}'
            return text, None  # Codex 风格：纯文本 JSON
        return "不需要更多工具", None

    async def fake_stream(messages, **kw):
        yield "答案"

    events = await _collect(
        run_agent_stream(
            [{"role": "user", "content": "有哪些策略"}],
            _FakeState(),
            generate_tool=fake_generate_tool,
            stream=fake_stream,
        )
    )
    assert [e["type"] for e in events] == ["tool_call", "tool_result", "delta", "done"]
    assert events[0]["name"] == "list_strategies"
    assert events[1]["result"] == {"strategies": []}



@pytest.mark.asyncio
async def test_agent_loop_parses_glm_dsml_tool_call():
    """OpenAI-compatible GLM 将 DSML 放入 content 时，仍会执行内部工具而不泄漏标记。"""
    calls = {"n": 0}
    dsml = (
        '先查询策略：\n<｜｜DSML｜｜tool_calls>\n'
        '<｜｜DSML｜｜invoke name="list_strategies"></｜｜DSML｜｜invoke>\n'
        '</｜｜DSML｜｜tool_calls>'
    )

    async def fake_generate_tool(messages, tools, **kw):
        calls["n"] += 1
        return (dsml, None) if calls["n"] == 1 else ("可以回答", None)

    async def fake_stream(messages, **kw):
        yield "策略已查询"

    events = await _collect(
        run_agent_stream(
            [{"role": "user", "content": "有哪些策略"}],
            _FakeState(),
            generate_tool=fake_generate_tool,
            stream=fake_stream,
        )
    )

    assert [event["type"] for event in events] == ["tool_call", "tool_result", "delta", "done"]
    assert events[0]["name"] == "list_strategies"
    assert all("DSML" not in event.get("content", "") for event in events)

@pytest.mark.asyncio
async def test_agent_loop_allows_run_backtest_after_reopen(monkeypatch):
    """原生 function calling 下 run_backtest 工具正常执行。"""
    from app.services import agent_loop as agent_loop_mod

    calls = {"n": 0}

    async def fake_generate_tool(messages, tools, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            return None, [{"id": "c1", "name": "run_backtest",
                           "arguments": '{"strategy_id":"x","symbols":["000001.SZ"]}'}]
        return "普通回答", None

    async def fake_stream(messages, **kw):
        yield "好"

    monkeypatch.setattr(
        agent_loop_mod.agent_tools,
        "call_tool",
        lambda name, app_state, args: {"sentinel": True} if name == "run_backtest" else {"error": "unexpected"},
    )

    events = await _collect(
        run_agent_stream(
            [{"role": "user", "content": "跑个回测"}],
            _FakeState(),
            generate_tool=fake_generate_tool,
            stream=fake_stream,
        )
    )
    results = [e for e in events if e["type"] == "tool_result"]
    assert len(results) == 1
    assert results[0]["result"] == {"sentinel": True}
