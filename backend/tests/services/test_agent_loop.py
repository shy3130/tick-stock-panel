import asyncio
import json
import time

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
async def test_agent_loop_allows_pool_backtest_workflow(monkeypatch):
    """原生 function calling 下只暴露强类型股票池回测工具。"""
    from app.services import agent_loop as agent_loop_mod

    calls = {"n": 0}

    async def fake_generate_tool(messages, tools, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            return None, [{
                "id": "c1",
                "name": "start_pool_backtest",
                "arguments": (
                    '{"pool_id":"pool-0123456789abcdef","target":"strategy",'
                    '"strategy_id":"x","start":"2026-08-17","end":"2026-08-18"}'
                ),
            }]
        return "普通回答", None

    async def fake_stream(messages, **kw):
        yield "好"

    monkeypatch.setattr(
        agent_loop_mod.agent_tools,
        "call_tool",
        lambda name, app_state, args: (
            {"status": "pending", "job_id": "job-0123456789abcdef"}
            if name == "start_pool_backtest"
            else {"error": "unexpected"}
        ),
    )

    events = await _collect(
        run_agent_stream(
            [{"role": "user", "content": "用已保存股票池跑个回测"}],
            _FakeState(),
            generate_tool=fake_generate_tool,
            stream=fake_stream,
        )
    )
    results = [e for e in events if e["type"] == "tool_result"]
    assert len(results) == 1
    assert results[0]["result"] == {
        "status": "pending",
        "job_id": "job-0123456789abcdef",
    }


@pytest.mark.asyncio
async def test_agent_loop_offloads_blocking_tools(monkeypatch):
    """同步 DuckDB/等待工具必须在线程执行，不能冻结 HTTP/SSE 事件循环。"""
    from app.services import agent_loop as agent_loop_mod

    calls = {"n": 0}
    order: list[str] = []

    async def fake_generate_tool(messages, tools, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            return None, [{"id": "c1", "name": "list_strategies", "arguments": "{}"}]
        return "完成", None

    async def fake_stream(messages, **kw):
        yield "好"

    def blocking_tool(name, app_state, args):
        time.sleep(0.05)
        order.append("tool")
        return {"strategies": []}

    async def ticker():
        await asyncio.sleep(0.01)
        order.append("tick")

    monkeypatch.setattr(agent_loop_mod.agent_tools, "call_tool", blocking_tool)
    await asyncio.gather(
        _collect(
            run_agent_stream(
                [{"role": "user", "content": "有哪些策略"}],
                _FakeState(),
                generate_tool=fake_generate_tool,
                stream=fake_stream,
            )
        ),
        ticker(),
    )
    assert order == ["tick", "tool"]


@pytest.mark.asyncio
async def test_agent_loop_redacts_unexpected_tool_error_paths(monkeypatch):
    """非 ValueError 也应成为已打码 tool_result，不能击穿 turn 或泄露服务器路径。"""
    from app.services import agent_loop as agent_loop_mod

    calls = {"n": 0}

    async def fake_generate_tool(messages, tools, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            return None, [{"id": "c1", "name": "list_strategies", "arguments": "{}"}]
        return "完成", None

    async def fake_stream(messages, **kw):
        yield "好"

    def failing_tool(name, app_state, args):
        raise OSError("/Users/private/secret.parquet unavailable")

    monkeypatch.setattr(agent_loop_mod.agent_tools, "call_tool", failing_tool)
    events = await _collect(
        run_agent_stream(
            [{"role": "user", "content": "有哪些策略"}],
            _FakeState(),
            generate_tool=fake_generate_tool,
            stream=fake_stream,
        )
    )
    result = next(event["result"] for event in events if event["type"] == "tool_result")
    assert result == {"error": "<path> unavailable"}
    assert events[-1]["type"] == "done"


@pytest.mark.asyncio
async def test_agent_loop_redacts_provider_error_before_streaming():
    async def failing_generate(messages, tools, **kw):
        raise RuntimeError(
            "Authorization: Bearer sk-provider-secret at /Users/private/provider.log"
        )

    events = await _collect(
        run_agent_stream(
            [{"role": "user", "content": "hi"}],
            _FakeState(),
            generate_tool=failing_generate,
        )
    )
    assert [event["type"] for event in events] == ["error"]
    encoded = json.dumps(events, ensure_ascii=False)
    assert "sk-provider-secret" not in encoded
    assert "/Users/private" not in encoded
