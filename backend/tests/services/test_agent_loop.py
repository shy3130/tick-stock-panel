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
        "先查询策略：\n<｜｜DSML｜｜tool_calls>\n"
        '<｜｜DSML｜｜invoke name="list_strategies"></｜｜DSML｜｜invoke>\n'
        "</｜｜DSML｜｜tool_calls>"
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
            return None, [
                {
                    "id": "c1",
                    "name": "start_pool_backtest",
                    "arguments": (
                        '{"pool_id":"pool-0123456789abcdef","target":"strategy",'
                        '"strategy_id":"x","start":"2026-08-17","end":"2026-08-18"}'
                    ),
                }
            ]
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


# ── AI 短线池输出边界：候选只能由 tool_result/前端结构化卡片表达 ──────
_SHORT_POOL_RESULT = {
    "status": "success",
    "summary": "短线动量质量观察池(确定性筛选): 命中 3 只, 输出 3 只, as_of=2026-08-25",
    "pool_id": "a1b2c3d4e5f60718",
    "as_of": "2026-08-25",
    "count": 3,
    "total": 3,
    "preset": {
        "preset_id": "short_momentum_quality_v1",
        "version": 1,
        "name": "短线动量质量观察",
        "description": "固定研究观察池",
    },
    "candidates": [
        {"rank": 1, "symbol": "600000.SH", "name": "浦发银行"},
        {"rank": 2, "symbol": "000001.SZ", "name": "平安银行"},
        {"rank": 3, "symbol": "300750.SZ", "name": "宁德时代"},
    ],
    "market_state": {"available": True, "state": "dispersed"},
    "t_research": {"protocol_id": "t_research_v1"},
}

# fake 模型在最终流里编造/删删/重排候选，还夹带交易指令口吻。
_TAMPERED_FINAL_TEXT = (
    "短线池候选如下：1. 贵州茅台（600519.SH）2. 平安银行（000001.SZ）"
    "3. 宁德时代（300750.SZ）4. 粤高速A（600548.SH）。建议明天买入茅台。"
)
_FORBIDDEN_IN_DELTA = (
    "浦发银行",
    "平安银行",
    "宁德时代",
    "600000.SH",
    "000001.SZ",
    "300750.SZ",
    "贵州茅台",
    "600519.SH",
    "粤高速A",
    "600548.SH",
    "建议明天买入",
)


@pytest.mark.asyncio
async def test_agent_loop_short_pool_replaces_tampered_final_stream(monkeypatch):
    """原生路径：模型最终流编造/删减/重排候选 → delta 只能是服务端确定性摘要。"""
    from app.services import agent_loop as agent_loop_mod

    calls = {"n": 0}

    async def fake_generate_tool(messages, tools, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            return None, [
                {
                    "id": "c1",
                    "name": "screen_stock_pool",
                    "arguments": '{"preset_id":"short_momentum_quality_v1","limit":3}',
                }
            ]
        return "已生成", None

    async def tampered_stream(messages, **kw):
        yield _TAMPERED_FINAL_TEXT

    monkeypatch.setattr(
        agent_loop_mod.agent_tools,
        "call_tool",
        lambda name, app_state, args: dict(_SHORT_POOL_RESULT),
    )

    events = await _collect(
        run_agent_stream(
            [{"role": "user", "content": "跑一下AI短线池"}],
            _FakeState(),
            generate_tool=fake_generate_tool,
            stream=tampered_stream,
        )
    )
    assert [e["type"] for e in events] == ["tool_call", "tool_result", "delta", "done"]
    # 候选完整保留在 tool_result 边界（前端结构化卡片的唯一数据源）
    assert events[1]["result"] == _SHORT_POOL_RESULT
    deltas = [e["content"] for e in events if e["type"] == "delta"]
    assert len(deltas) == 1
    text = deltas[0]
    for forbidden in _FORBIDDEN_IN_DELTA:
        assert forbidden not in text, forbidden
    # 确定性摘要只引用标量事实
    assert "pool_id=a1b2c3d4e5f60718" in text
    assert "as_of=2026-08-25" in text
    assert "命中 3 只" in text
    assert "输出 3 只" in text
    assert "结构化结果卡" in text
    assert "非投资建议" in text
    encoded = json.dumps(events, ensure_ascii=False)
    # 编造的候选从未进入任何用户可见事件
    assert "贵州茅台" not in encoded
    assert "600519.SH" not in encoded
    assert "粤高速A" not in encoded
    assert "建议明天买入" not in encoded


@pytest.mark.asyncio
async def test_agent_loop_short_pool_success_is_terminal(monkeypatch):
    """成功短线池立即终止工具轮；同批后续失败工具不得执行或被摘要掩盖。"""
    from app.services import agent_loop as agent_loop_mod

    generate_calls = {"n": 0}
    executed: list[str] = []
    stream_called = False

    async def fake_generate_tool(messages, tools, **kw):
        generate_calls["n"] += 1
        return None, [
            {
                "id": "c1",
                "name": "screen_stock_pool",
                "arguments": '{"preset_id":"short_momentum_quality_v1","limit":3}',
            },
            {
                "id": "c2",
                "name": "list_strategies",
                "arguments": "{}",
            },
        ]

    async def unexpected_stream(messages, **kw):
        nonlocal stream_called
        stream_called = True
        yield "不应进入模型终流"

    def fake_tool(name, app_state, args):
        executed.append(name)
        if name == "screen_stock_pool":
            return dict(_SHORT_POOL_RESULT)
        raise ValueError("后续工具不应执行")

    monkeypatch.setattr(agent_loop_mod.agent_tools, "call_tool", fake_tool)
    events = await _collect(
        run_agent_stream(
            [{"role": "user", "content": "跑一下AI短线池"}],
            _FakeState(),
            generate_tool=fake_generate_tool,
            stream=unexpected_stream,
        )
    )

    assert generate_calls["n"] == 1
    assert executed == ["screen_stock_pool"]
    assert stream_called is False
    assert [event["type"] for event in events] == ["tool_call", "tool_result", "delta", "done"]
    assert "结构化结果卡" in events[2]["content"]


@pytest.mark.asyncio
async def test_agent_loop_short_pool_gates_fallback_json_mode(monkeypatch):
    """降级 JSON 路径同样受输出边界保护：篡改最终流不会进入 delta。"""
    from app.services import agent_loop as agent_loop_mod

    calls = {"n": 0}

    async def fake_generate_tool(messages, tools, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            return (
                '{"tool":"screen_stock_pool","args":{"preset_id":"short_momentum_quality_v1"}}',
                None,
            )
        return "已生成", None

    async def tampered_stream(messages, **kw):
        yield _TAMPERED_FINAL_TEXT

    monkeypatch.setattr(
        agent_loop_mod.agent_tools,
        "call_tool",
        lambda name, app_state, args: dict(_SHORT_POOL_RESULT),
    )

    events = await _collect(
        run_agent_stream(
            [{"role": "user", "content": "AI短线池"}],
            _FakeState(),
            generate_tool=fake_generate_tool,
            stream=tampered_stream,
        )
    )
    deltas = [e["content"] for e in events if e["type"] == "delta"]
    assert len(deltas) == 1
    for forbidden in _FORBIDDEN_IN_DELTA:
        assert forbidden not in deltas[0], forbidden
    assert "pool_id=a1b2c3d4e5f60718" in deltas[0]
    assert events[-1]["type"] == "done"


@pytest.mark.asyncio
async def test_agent_loop_legacy_pool_and_normal_stream_untouched(monkeypatch):
    """legacy 普通股票池与普通问答不受门控：模型 delta 逐块原样透传。"""
    from app.services import agent_loop as agent_loop_mod

    calls = {"n": 0}

    async def fake_generate_tool(messages, tools, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            return None, [
                {
                    "id": "c1",
                    "name": "screen_stock_pool",
                    "arguments": '{"conditions":[{"field":"close","op":">","value":1}]}',
                }
            ]
        return "完成", None

    async def fake_stream(messages, **kw):
        for chunk in ["普通", "回答", "保持分块"]:
            yield chunk

    monkeypatch.setattr(
        agent_loop_mod.agent_tools,
        "call_tool",
        lambda name, app_state, args: {
            "status": "success",
            "pool_id": "b2c3d4e5f6071890",
            "count": 5,
            "total": 5,
            "as_of": "2026-08-25",
            "preview": [{"symbol": "600000.SH", "name": "浦发银行"}],
        },
    )

    events = await _collect(
        run_agent_stream(
            [{"role": "user", "content": "筛个普通池"}],
            _FakeState(),
            generate_tool=fake_generate_tool,
            stream=fake_stream,
        )
    )
    assert [e["content"] for e in events if e["type"] == "delta"] == ["普通", "回答", "保持分块"]


@pytest.mark.asyncio
async def test_agent_loop_short_pool_tool_error_keeps_normal_stream(monkeypatch):
    """短线池工具失败不算命中：最终流仍按普通回答透传。"""
    from app.services import agent_loop as agent_loop_mod

    calls = {"n": 0}

    async def fake_generate_tool(messages, tools, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            return None, [
                {
                    "id": "c1",
                    "name": "screen_stock_pool",
                    "arguments": '{"preset_id":"short_momentum_quality_v1"}',
                }
            ]
        return "完成", None

    async def fake_stream(messages, **kw):
        yield "筛选失败，请稍后再试"

    def failing_tool(name, app_state, args):
        raise ValueError("筛选数据不可用")

    monkeypatch.setattr(agent_loop_mod.agent_tools, "call_tool", failing_tool)
    events = await _collect(
        run_agent_stream(
            [{"role": "user", "content": "AI短线池"}],
            _FakeState(),
            generate_tool=fake_generate_tool,
            stream=fake_stream,
        )
    )
    assert events[1]["result"] == {"error": "筛选数据不可用"}
    assert [e["content"] for e in events if e["type"] == "delta"] == ["筛选失败，请稍后再试"]
