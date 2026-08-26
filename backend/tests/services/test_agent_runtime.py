import asyncio
import json
from types import SimpleNamespace

import pytest

from app.config import settings
from app.services import agent_loop, agent_runtime


async def _collect(stream):
    return [json.loads(line) async for line in stream]


def _profile(**overrides):
    return {
        "id": "p_test",
        "provider": "openai_compat",
        "base_url": "https://gateway.example.test/v1",
        "api_key": "sk-runtime-secret",
        "model": "test-model",
        **overrides,
    }


class _FakeWriter:
    def __init__(self, process, *, complete_after_tool=False):
        self.process = process
        self.complete_after_tool = complete_after_tool
        self.closed = False
        self.messages = []
        self.started = asyncio.Event()

    def write(self, payload):
        message = json.loads(payload)
        self.messages.append(message)
        if message["type"] == "start":
            self.started.set()
            if self.complete_after_tool:
                self.process.stdout.feed_data(
                    json.dumps(
                        {
                            "type": "tool_request",
                            "request_id": "req_1",
                            "tool_call_id": "call_1",
                            "name": "list_strategies",
                            "args": {},
                        }
                    ).encode()
                    + b"\n"
                )
        elif message["type"] == "tool_result" and self.complete_after_tool:
            self.process.stdout.feed_data(b'{"type":"delta","content":"done"}\n')
            self.process.returncode = 0
            self.process.stdout.feed_data(b'{"type":"done","elapsed_ms":12.5}\n')
            self.process.stdout.feed_eof()

    async def drain(self):
        return None

    def close(self):
        self.closed = True

    def is_closing(self):
        return self.closed

    async def wait_closed(self):
        return None


class _FakeProcess:
    def __init__(self, *, ready=True, complete_after_tool=False):
        self.stdout = asyncio.StreamReader()
        self.stderr = asyncio.StreamReader()
        self.stderr.feed_eof()
        self.returncode = None
        self.terminated = False
        self.killed = False
        self.stdin = _FakeWriter(self, complete_after_tool=complete_after_tool)
        if ready:
            self.stdout.feed_data(b'{"type":"ready"}\n')

    async def wait(self):
        while self.returncode is None:
            await asyncio.sleep(0)
        return self.returncode

    def terminate(self):
        self.terminated = True
        self.returncode = -15
        self.stdout.feed_eof()

    def kill(self):
        self.killed = True
        self.returncode = -9
        self.stdout.feed_eof()


def _enable_pi(monkeypatch, tmp_path, profile=None):
    worker = tmp_path / "worker.js"
    worker.write_text("// test worker", encoding="utf-8")
    monkeypatch.setattr(settings, "agent_runtime", "pi")
    monkeypatch.setattr(settings, "agent_pi_worker_path", str(worker))
    monkeypatch.setattr(settings, "agent_pi_ready_timeout_s", 0.05)
    monkeypatch.setattr(agent_runtime, "_resolve_node_command", lambda: "node")
    monkeypatch.setattr(
        agent_runtime.ai_profiles,
        "resolve_profile",
        lambda _profile_id: profile if profile is not None else _profile(),
    )


@pytest.mark.asyncio
async def test_default_python_runtime_delegates_to_legacy(monkeypatch):
    monkeypatch.setattr(settings, "agent_runtime", "python")

    async def fake_legacy(messages, app_state, profile_id):
        assert messages[-1]["content"] == "hello"
        assert profile_id == "p_legacy"
        yield '{"type":"delta","content":"legacy"}'
        yield '{"type":"done","elapsed_ms":1}'

    monkeypatch.setattr(agent_loop, "run_agent_stream", fake_legacy)
    events = await _collect(
        agent_runtime.run_agent_stream(
            [{"role": "user", "content": "hello"}],
            SimpleNamespace(),
            "p_legacy",
        )
    )
    assert [event["type"] for event in events] == ["delta", "done"]
    assert events[0]["content"] == "legacy"


@pytest.mark.asyncio
async def test_pi_runtime_rejects_unsupported_profile(monkeypatch):
    monkeypatch.setattr(settings, "agent_runtime", "pi")
    monkeypatch.setattr(
        agent_runtime.ai_profiles,
        "resolve_profile",
        lambda _profile_id: _profile(provider="codex_cli"),
    )
    events = await _collect(
        agent_runtime.run_agent_stream(
            [{"role": "user", "content": "hello"}], SimpleNamespace(), "p_test"
        )
    )
    assert [event["type"] for event in events] == ["error"]
    assert "仅支持 openai_compat" in events[0]["message"]

@pytest.mark.asyncio
async def test_pi_runtime_rejects_unknown_explicit_profile(monkeypatch):
    monkeypatch.setattr(settings, "agent_runtime", "pi")
    monkeypatch.setattr(
        agent_runtime.ai_profiles,
        "resolve_profile",
        lambda _profile_id: _profile(id="p_default"),
    )
    events = await _collect(
        agent_runtime.run_agent_stream(
            [{"role": "user", "content": "hello"}], SimpleNamespace(), "p_missing"
        )
    )
    assert [event["type"] for event in events] == ["error"]
    assert "profile 不存在" in events[0]["message"]

def test_pi_tool_specs_use_worker_compatible_parameter_contracts():
    specs = {item["name"]: item["input_schema"] for item in agent_runtime._tool_specs()}
    assert specs["optimize_portfolio"]["required"] == ["symbols"]
    assert specs["compare_factors"]["required"] == ["factor_ids", "symbols"]
    screen_schema = specs["screen_stock_pool"]
    assert "oneOf" not in screen_schema
    assert screen_schema["additionalProperties"] is False
    assert set(screen_schema["properties"]) == {
        "preset_id",
        "conditions",
        "as_of",
        "order_by",
        "limit",
    }


@pytest.mark.asyncio
async def test_pi_runtime_missing_worker_fails_closed(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "agent_runtime", "pi")
    monkeypatch.setattr(settings, "agent_pi_worker_path", str(tmp_path / "missing.js"))
    monkeypatch.setattr(agent_runtime.ai_profiles, "resolve_profile", lambda _id: _profile())
    events = await _collect(
        agent_runtime.run_agent_stream(
            [{"role": "user", "content": "hello"}], SimpleNamespace(), "p_test"
        )
    )
    assert [event["type"] for event in events] == ["error"]
    assert "worker 文件不存在" in events[0]["message"]
    assert str(tmp_path) not in events[0]["message"]


@pytest.mark.asyncio
async def test_pi_runtime_translates_tool_round_trip(monkeypatch, tmp_path):
    _enable_pi(monkeypatch, tmp_path)
    process = _FakeProcess(complete_after_tool=True)
    spawn_kwargs = {}
    monkeypatch.setenv("AI_API_KEY", "sk-parent-secret")

    async def fake_spawn(*args, **kwargs):
        spawn_kwargs.update(kwargs)
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_spawn)
    monkeypatch.setattr(
        agent_runtime.agent_tools,
        "call_tool",
        lambda name, state, args: {"strategies": []},
    )

    events = await _collect(
        agent_runtime.run_agent_stream(
            [{"role": "user", "content": "list strategies"}],
            SimpleNamespace(),
            "p_test",
        )
    )
    assert [event["type"] for event in events] == [
        "tool_call",
        "tool_result",
        "delta",
        "done",
    ]
    assert events[0] == {"type": "tool_call", "name": "list_strategies", "args": {}}
    assert events[1]["result"] == {"strategies": []}
    assert events[-1]["elapsed_ms"] == 12.5
    start = next(message for message in process.stdin.messages if message["type"] == "start")
    assert start["profile"]["api_key"] == "sk-runtime-secret"
    assert all(tool["read_only"] is True for tool in start["tools"])
    assert spawn_kwargs["cwd"] == str(tmp_path)
    assert spawn_kwargs["limit"] == agent_runtime._MAX_PROTOCOL_LINE_BYTES + 1
    assert "AI_API_KEY" not in spawn_kwargs["env"]
    reply = next(message for message in process.stdin.messages if message["type"] == "tool_result")
    assert reply == {
        "type": "tool_result",
        "request_id": "req_1",
        "ok": True,
        "result": {"strategies": []},
    }


@pytest.mark.asyncio
async def test_pi_runtime_caps_tool_requests_in_python(monkeypatch, tmp_path):
    _enable_pi(monkeypatch, tmp_path)
    monkeypatch.setattr(agent_runtime, "_MAX_TOOL_REQUESTS", 1)
    process = _FakeProcess()
    original_write = process.stdin.write

    def write_and_queue_requests(payload):
        original_write(payload)
        message = json.loads(payload)
        if message["type"] == "start":
            for index in (1, 2):
                process.stdout.feed_data(
                    json.dumps(
                        {
                            "type": "tool_request",
                            "request_id": f"req_{index}",
                            "tool_call_id": f"call_{index}",
                            "name": "list_strategies",
                            "args": {},
                        }
                    ).encode()
                    + b"\n"
                )

    process.stdin.write = write_and_queue_requests

    async def fake_spawn(*args, **kwargs):
        return process

    calls = {"count": 0}

    def fake_call_tool(name, state, args):
        calls["count"] += 1
        return {"strategies": []}

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_spawn)
    monkeypatch.setattr(agent_runtime.agent_tools, "call_tool", fake_call_tool)
    events = await _collect(
        agent_runtime.run_agent_stream(
            [{"role": "user", "content": "hello"}], SimpleNamespace(), "p_test"
        )
    )
    assert [event["type"] for event in events] == [
        "tool_call",
        "tool_result",
        "error",
    ]
    assert "工具请求超过安全上限" in events[-1]["message"]
    assert calls["count"] == 1


@pytest.mark.asyncio
async def test_pi_runtime_ready_timeout_terminates_worker(monkeypatch, tmp_path):
    _enable_pi(monkeypatch, tmp_path)
    process = _FakeProcess(ready=False)

    async def fake_spawn(*args, **kwargs):
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_spawn)
    events = await _collect(
        agent_runtime.run_agent_stream(
            [{"role": "user", "content": "hello"}], SimpleNamespace(), "p_test"
        )
    )
    assert [event["type"] for event in events] == ["error"]
    assert "ready 超时" in events[0]["message"]
    assert process.terminated is True

@pytest.mark.asyncio
async def test_pi_runtime_response_timeout_terminates_worker(monkeypatch, tmp_path):
    _enable_pi(monkeypatch, tmp_path)
    monkeypatch.setattr(settings, "agent_pi_response_timeout_s", 0.01)
    process = _FakeProcess()

    async def fake_spawn(*args, **kwargs):
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_spawn)
    events = await _collect(
        agent_runtime.run_agent_stream(
            [{"role": "user", "content": "hello"}], SimpleNamespace(), "p_test"
        )
    )
    assert [event["type"] for event in events] == ["error"]
    assert "响应超时" in events[0]["message"]
    assert process.terminated is True


@pytest.mark.asyncio
async def test_pi_runtime_cancel_terminates_worker(monkeypatch, tmp_path):
    _enable_pi(monkeypatch, tmp_path)
    process = _FakeProcess()

    async def fake_spawn(*args, **kwargs):
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_spawn)
    task = asyncio.create_task(
        _collect(
            agent_runtime.run_agent_stream(
                [{"role": "user", "content": "hello"}], SimpleNamespace(), "p_test"
            )
        )
    )
    await asyncio.wait_for(process.stdin.started.wait(), timeout=1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert process.terminated is True
    assert process.returncode == -15


@pytest.mark.asyncio
async def test_pi_runtime_redacts_worker_secret(monkeypatch, tmp_path):
    _enable_pi(monkeypatch, tmp_path)
    process = _FakeProcess()
    original_write = process.stdin.write

    def write_and_fail(payload):
        original_write(payload)
        message = json.loads(payload)
        if message["type"] == "start":
            process.returncode = 1
            process.stdout.feed_data(
                json.dumps(
                    {
                        "type": "fatal",
                        "message": "upstream rejected sk-runtime-secret at /Users/test/private/file",
                    }
                ).encode()
                + b"\n"
            )
            process.stdout.feed_eof()

    process.stdin.write = write_and_fail

    async def fake_spawn(*args, **kwargs):
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_spawn)
    events = await _collect(
        agent_runtime.run_agent_stream(
            [{"role": "user", "content": "hello"}], SimpleNamespace(), "p_test"
        )
    )
    assert [event["type"] for event in events] == ["error"]
    encoded = json.dumps(events, ensure_ascii=False)
    assert "sk-runtime-secret" not in encoded
    assert "/Users/test" not in encoded


def _short_pool_success_result():
    return {
        "status": "success",
        "preset": {"preset_id": "short_momentum_quality_v1"},
        "pool_id": "pool_20260826",
        "as_of": "2026-08-26",
        "total": 42,
        "count": 5,
        "stocks": [{"symbol": "300750", "name": "宁德时代"}],
    }


@pytest.mark.asyncio
async def test_pi_runtime_short_pool_hit_discards_buffered_deltas(monkeypatch, tmp_path):
    _enable_pi(monkeypatch, tmp_path)
    process = _FakeProcess()
    original_write = process.stdin.write
    state = {"tool_replied": False}

    def write_and_script(payload):
        original_write(payload)
        message = json.loads(payload)
        if message["type"] == "start":
            process.stdout.feed_data(
                json.dumps({"type": "delta", "content": "候选：宁德时代 300750"}, ensure_ascii=False).encode()
                + b"\n"
            )
            process.stdout.feed_data(
                json.dumps({"type": "delta", "content": "、贵州茅台 600519"}, ensure_ascii=False).encode()
                + b"\n"
            )
            process.stdout.feed_data(
                json.dumps(
                    {
                        "type": "tool_request",
                        "request_id": "req_1",
                        "tool_call_id": "call_1",
                        "name": "screen_stock_pool",
                        "args": {"preset_id": "short_momentum_quality_v1"},
                    }
                ).encode()
                + b"\n"
            )
        elif message["type"] == "tool_result" and not state["tool_replied"]:
            state["tool_replied"] = True
            # worker 在收到短线池结果后继续生成候选文本并追加第二个工具请求
            process.stdout.feed_data(
                json.dumps({"type": "delta", "content": "模型又编了候选 XXX"}, ensure_ascii=False).encode()
                + b"\n"
            )
            process.stdout.feed_data(
                json.dumps(
                    {
                        "type": "tool_request",
                        "request_id": "req_2",
                        "tool_call_id": "call_2",
                        "name": "list_strategies",
                        "args": {},
                    }
                ).encode()
                + b"\n"
            )
            process.stdout.feed_data(b'{"type":"done","elapsed_ms":99.0}\n')

    process.stdin.write = write_and_script

    async def fake_spawn(*args, **kwargs):
        return process

    calls = []

    def fake_call_tool(name, state_obj, args):
        calls.append(name)
        return _short_pool_success_result() if name == "screen_stock_pool" else {}

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_spawn)
    monkeypatch.setattr(agent_runtime.agent_tools, "call_tool", fake_call_tool)
    events = await _collect(
        agent_runtime.run_agent_stream(
            [{"role": "user", "content": "生成短线池"}], SimpleNamespace(), "p_test"
        )
    )
    assert [event["type"] for event in events] == ["tool_call", "tool_result", "delta", "done"]
    expected = agent_loop._short_pool_final_text(
        agent_loop._short_pool_outcome("screen_stock_pool", _short_pool_success_result())
    )
    assert events[2]["content"] == expected
    deltas = [event["content"] for event in events if event["type"] == "delta"]
    joined_deltas = "".join(deltas)
    assert "300750" not in joined_deltas
    assert "600519" not in joined_deltas
    assert "模型又编了" not in joined_deltas
    assert "宁德时代" not in joined_deltas
    # 候选只能经 tool_result（前端结构化卡片）承载，不得进入 delta 流
    assert events[1]["result"]["stocks"][0]["symbol"] == "300750"
    assert calls == ["screen_stock_pool"]
    replies = [m for m in process.stdin.messages if m["type"] == "tool_result"]
    assert [r["request_id"] for r in replies] == ["req_1"]
    assert process.terminated is True
    assert process.returncode == -15


@pytest.mark.asyncio
async def test_pi_runtime_short_pool_miss_forwards_buffered_deltas(monkeypatch, tmp_path):
    _enable_pi(monkeypatch, tmp_path)
    process = _FakeProcess()
    original_write = process.stdin.write
    state = {"tool_replied": False}

    def write_and_script(payload):
        original_write(payload)
        message = json.loads(payload)
        if message["type"] == "start":
            process.stdout.feed_data(b'{"type":"delta","content":"pre-text"}\n')
            process.stdout.feed_data(
                json.dumps(
                    {
                        "type": "tool_request",
                        "request_id": "req_1",
                        "tool_call_id": "call_1",
                        "name": "screen_stock_pool",
                        "args": {"preset_id": "legacy_pool"},
                    }
                ).encode()
                + b"\n"
            )
        elif message["type"] == "tool_result" and not state["tool_replied"]:
            state["tool_replied"] = True
            process.stdout.feed_data(b'{"type":"delta","content":"post-text"}\n')
            process.stdout.feed_data(
                json.dumps(
                    {
                        "type": "tool_request",
                        "request_id": "req_2",
                        "tool_call_id": "call_2",
                        "name": "list_strategies",
                        "args": {},
                    }
                ).encode()
                + b"\n"
            )
        elif message["type"] == "tool_result" and message["request_id"] == "req_2":
            process.returncode = 0
            process.stdout.feed_data(b'{"type":"done","elapsed_ms":7.5}\n')
            process.stdout.feed_eof()

    process.stdin.write = write_and_script

    async def fake_spawn(*args, **kwargs):
        return process

    calls = []

    def fake_call_tool(name, state_obj, args):
        calls.append(name)
        if name == "screen_stock_pool":
            return {"status": "error", "message": "预设不存在"}
        return {"strategies": []}

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_spawn)
    monkeypatch.setattr(agent_runtime.agent_tools, "call_tool", fake_call_tool)
    events = await _collect(
        agent_runtime.run_agent_stream(
            [{"role": "user", "content": "查询池"}], SimpleNamespace(), "p_test"
        )
    )
    assert [event["type"] for event in events] == [
        "tool_call",
        "tool_result",
        "tool_call",
        "tool_result",
        "delta",
        "delta",
        "done",
    ]
    assert [event["content"] for event in events if event["type"] == "delta"] == [
        "pre-text",
        "post-text",
    ]
    assert calls == ["screen_stock_pool", "list_strategies"]
    replies = [m for m in process.stdin.messages if m["type"] == "tool_result"]
    assert [r["request_id"] for r in replies] == ["req_1", "req_2"]
    assert events[-1]["elapsed_ms"] == 7.5
