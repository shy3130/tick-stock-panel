"""Codex CLI 结构化工具轮次: 协议往返、非法输出拒绝、沿用动作确认。"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.custom.assistant import actions, chat_service, codex
from app.custom.assistant import tools as assistant_tools
from app.services import ai_provider


def _schema(name):
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": "本地工具",
            "parameters": {"type": "object", "properties": {}},
        },
    }


@pytest.fixture
def cli_process(monkeypatch):
    captured = {}

    def run(args, prompt, env, timeout):
        captured.update(args=list(args), prompt=prompt, timeout=timeout)
        output_schema = Path(args[args.index("--output-schema") + 1])
        captured["schema_path"] = output_schema
        captured["schema"] = json.loads(output_schema.read_text(encoding="utf-8"))
        output = Path(args[args.index("--output-last-message") + 1])
        output.write_text(json.dumps(captured["response"], ensure_ascii=False), encoding="utf-8")
        return 0, b"execution logs must not become the answer", b""

    monkeypatch.setattr(ai_provider, "_codex_base_command", lambda: ["codex"])
    monkeypatch.setattr(ai_provider, "_prepare_codex_home", lambda path: None)
    monkeypatch.setattr(ai_provider, "_codex_process_env", lambda path: {})
    monkeypatch.setattr(ai_provider, "_sweep_stale_codex_run_dirs", lambda: None)
    monkeypatch.setattr(ai_provider, "current_ai_model", lambda: "test-model")
    monkeypatch.setattr(ai_provider, "_run_codex_process", run)
    return captured


async def test_codex_round_uses_schema_and_preserves_full_history(cli_process):
    cli_process["response"] = {
        "content": "先查询本地数据。",
        "tool_calls": [
            {"name": "list_strategies", "arguments": "{}"},
            {"name": "get_capabilities", "arguments": '{"scope":"daily"}'},
        ],
    }
    messages = [
        {"role": "system", "content": "只能使用本地数据"},
        {"role": "user", "content": "我有哪些策略？"},
        {"role": "assistant", "tool_calls": [{
            "id": "previous", "type": "function",
            "function": {"name": "list_strategies", "arguments": "{}"},
        }]},
        {"role": "tool", "tool_call_id": "previous", "content": '{"ok":true,"result":[]}'},
    ]
    schemas = [_schema("list_strategies"), _schema("get_capabilities")]
    events = [event async for event in codex.stream_codex_round(messages, schemas, timeout=12)]

    assert events[0] == {"type": "text", "delta": "先查询本地数据。"}
    calls = events[1]["tool_calls"]
    assert [call["name"] for call in calls] == ["list_strategies", "get_capabilities"]
    assert calls[0]["id"] and calls[0]["id"] != calls[1]["id"]
    assert json.loads(calls[1]["arguments"]) == {"scope": "daily"}
    assert events[1]["finish_reason"] == "tool_calls"
    # 不能丢掉 assistant.tool_calls / tool_call_id, 否则 CLI 看不到已执行工具与结果的对应关系。
    payload = json.loads(cli_process["prompt"].split("<user>\n", 1)[1].rsplit("\n</user>", 1)[0])
    assert payload == {"messages": messages, "tools": schemas}
    names = cli_process["schema"]["properties"]["tool_calls"]["items"]["properties"]["name"]["enum"]
    assert set(names) == {"list_strategies", "get_capabilities"}
    args = cli_process["args"]
    assert args[args.index("--sandbox") + 1] == "read-only"
    assert args[args.index("--model") + 1] == "test-model"
    # 助手原有 240s 时限不能缩短 Codex 文本入口既有的 600s 预算。
    assert cli_process["timeout"] == 600
    assert not cli_process["schema_path"].exists()  # 临时 schema 随隔离目录清理


async def test_codex_round_final_text(cli_process):
    cli_process["response"] = {"content": "当前没有策略。", "tool_calls": []}
    events = [event async for event in codex.stream_codex_round(
        [{"role": "user", "content": "你好"}], [_schema("list_strategies")],
    )]
    assert events == [
        {"type": "text", "delta": "当前没有策略。"},
        {"type": "round_end", "tool_calls": [], "finish_reason": "stop"},
    ]


async def test_codex_round_preserves_longer_explicit_timeout(cli_process):
    cli_process["response"] = {"content": "完成", "tool_calls": []}
    _ = [event async for event in codex.stream_codex_round(
        [{"role": "user", "content": "你好"}], [_schema("list_strategies")], timeout=900,
    )]
    assert cli_process["timeout"] == 900


async def test_codex_process_timeout_keeps_duration_and_distinct_error_kind(monkeypatch, caplog):
    import subprocess

    def fail(args, **kwargs):
        raise subprocess.TimeoutExpired(args, kwargs["timeout"], stderr=b"still generating")

    monkeypatch.setattr(ai_provider.subprocess, "run", fail)
    with pytest.raises(RuntimeError) as error:
        ai_provider._run_codex_process(["codex", "exec"], "private prompt", {}, 600)
    event = chat_service._classify_exception(error.value)
    assert event["kind"] == "timeout"
    assert "Codex CLI" in event["message"] and "600" in event["message"]
    assert "private prompt" not in caplog.text
    assert "still generating" not in caplog.text
    assert "600" in caplog.text and "timed out" in caplog.text


@pytest.mark.parametrize("message", [
    "Codex CLI 调用超时(600 秒), 本轮生成已终止。",
    "Codex CLI 调用超时, 请稍后重试或检查本机 Codex 登录状态",
])
async def test_codex_chat_timeout_is_not_collapsed_into_network_error(monkeypatch, message):
    async def fake_cli(*args, **kwargs):
        raise RuntimeError(message)

    monkeypatch.setattr(ai_provider, "_run_codex_cli", fake_cli)
    monkeypatch.setattr(chat_service, "ai_configured", lambda: True)
    monkeypatch.setattr(chat_service, "is_codex_cli_provider", lambda: True)
    events = [json.loads(line) async for line in chat_service.chat_stream(
        history=[{"role": "user", "content": "分析工具结果"}],
    )]
    assert events[0]["kind"] == "timeout"
    assert events[0]["message"] == message
    assert events[-1]["type"] == "done"


def test_connection_failure_still_uses_network_error():
    event = chat_service._classify_exception(ConnectionError("connection failed"))
    assert event["kind"] == "network"


@pytest.mark.parametrize("response", [
    "not JSON",
    "[]",
    '{"content":"答复"}',
    '{"content":null,"tool_calls":[]}',
    '{"content":"答复","tool_calls":{}}',
    '{"content":"答复","tool_calls":[null]}',
    '{"content":"答复","tool_calls":[{"name":"shell","arguments":"{}"}]}',
    '{"content":"答复","tool_calls":[{"name":"list_strategies","arguments":"broken"}]}',
    '{"content":"答复","tool_calls":[{"name":"list_strategies","arguments":"[]"}]}',
    '{"content":"答复","tool_calls":[{"name":"list_strategies","arguments":{}}]}',
    '{"content":"","tool_calls":[]}',
    json.dumps({"content": "不能显示", "tool_calls": [
        {"name": "list_strategies", "arguments": "{}"},
        {"name": "shell", "arguments": "{}"},
    ]}),
])
async def test_codex_round_rejects_invalid_output_before_any_event(monkeypatch, response):
    async def fake_cli(*args, **kwargs):
        return response

    monkeypatch.setattr(ai_provider, "_run_codex_cli", fake_cli)
    events = []
    with pytest.raises(RuntimeError, match="Codex CLI"):
        async for event in codex.stream_codex_round([], [_schema("list_strategies")]):
            events.append(event)
    assert events == []


async def test_codex_round_old_cli_reports_upgrade_instead_of_text_fallback(monkeypatch):
    async def fake_cli(*args, **kwargs):
        raise RuntimeError("Codex CLI 调用失败: unexpected argument '--output-schema' found")

    monkeypatch.setattr(ai_provider, "_run_codex_cli", fake_cli)
    with pytest.raises(RuntimeError, match=r"升级.*Codex CLI"):
        _ = [event async for event in codex.stream_codex_round([], [_schema("list_strategies")])]


@pytest.mark.parametrize("approve", [True, False])
async def test_codex_tools_still_use_action_confirmation(monkeypatch, approve):
    outputs = [
        {"content": "", "tool_calls": [{"name": "add_to_watchlist", "arguments": '{"symbol":"600519.SH"}'}]},
        {"content": "完成" if approve else "已取消", "tool_calls": []},
    ]
    requests = []
    executed = []

    async def fake_cli(messages, **kwargs):
        requests.append(json.loads(messages[-1]["content"]))
        return json.dumps(outputs.pop(0))

    async def execute(name, args, ctx):
        executed.append((name, args))
        return {"ok": True, "result": {"added": True}}

    monkeypatch.setattr(ai_provider, "_run_codex_cli", fake_cli)
    monkeypatch.setattr(chat_service, "ai_configured", lambda: True)
    monkeypatch.setattr(chat_service, "is_codex_cli_provider", lambda: True)
    monkeypatch.setattr(actions, "auto_approve_enabled", lambda: False)
    monkeypatch.setattr(assistant_tools, "execute_assistant_tool", execute)
    events = []
    async for line in chat_service.chat_stream(history=[{"role": "user", "content": "加入自选"}]):
        event = json.loads(line)
        events.append(event)
        if event["type"] == "action_confirm":
            assert executed == []
            await actions.registry.resolve(event["call_id"], approve)

    assert [event["type"] for event in events] == [
        "tool_call", "action_confirm", "tool_result", "delta", "done",
    ]
    assert executed == ([("add_to_watchlist", {"symbol": "600519.SH"})] if approve else [])
    result = requests[1]["messages"][-1]
    assert result["role"] == "tool"
    assert json.loads(result["content"])["ok"] is approve
    if not approve:
        assert "拒绝" in json.loads(result["content"])["error"]


async def test_codex_failure_becomes_error_and_done(monkeypatch):
    async def fake_cli(*args, **kwargs):
        raise RuntimeError("Codex CLI 调用失败: 401 Unauthorized")

    monkeypatch.setattr(ai_provider, "_run_codex_cli", fake_cli)
    monkeypatch.setattr(chat_service, "ai_configured", lambda: True)
    monkeypatch.setattr(chat_service, "is_codex_cli_provider", lambda: True)
    events = [json.loads(line) async for line in chat_service.chat_stream(
        history=[{"role": "user", "content": "hi"}],
    )]
    assert [event["type"] for event in events] == ["error", "done"]
    assert "401" in events[0]["message"]
