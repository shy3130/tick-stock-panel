"""AI 助手动作工具: 确认闸门(批准/拒绝/超时) + 写工具往返 + 决策端点。

契约:
- 动作工具(add_to_watchlist / create_signal_strategy / run_backtest)执行前
  必须先发 action_confirm 事件并挂起等待决策; 拒绝时不执行工具, 按 ok=False
  回填给模型(模型可据此改走文字建议, 而非重试)。
- create_signal_strategy 走 custom_signals 白名单校验: 合法定义落盘
  user_data/custom_signals/*.json, 非法字段被拒且错误带常用字段提示。
- add_to_watchlist 复用 watchlist 服务; 已在列表的标的不重复添加(不改动顺序)。
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.custom.assistant import actions as assistant_actions
from app.custom.assistant import chat_service
from app.custom.assistant import tools as assistant_tools
from app.extensions.loader import configure_backend_extensions

# ── PendingRegistry 单元: 批准 / 拒绝 / 超时 / 未知 id ─────────────

async def test_registry_approve_wakes_waiter() -> None:
    reg = assistant_actions.PendingRegistry(timeout_s=5.0)
    action = await reg.register(assistant_actions.new_call_id(), "run_backtest", {})
    waiter = asyncio.ensure_future(reg.await_decision(action))
    await asyncio.sleep(0)  # 让 await_decision 先挂到事件上
    resolved = await reg.resolve(action.call_id, True)
    assert resolved is action
    assert await waiter == "approved"


async def test_registry_deny_wakes_waiter() -> None:
    reg = assistant_actions.PendingRegistry(timeout_s=5.0)
    action = await reg.register(assistant_actions.new_call_id(), "run_backtest", {})
    waiter = asyncio.ensure_future(reg.await_decision(action))
    await asyncio.sleep(0)
    await reg.resolve(action.call_id, False)
    assert await waiter == "denied"


async def test_registry_timeout_denies_and_recycles() -> None:
    reg = assistant_actions.PendingRegistry(timeout_s=0.02)
    action = await reg.register(assistant_actions.new_call_id(), "run_backtest", {})
    assert await reg.await_decision(action) == "denied"
    # 超时回收后, 迟到的决策端点调用应返回 None(404)
    assert await reg.resolve(action.call_id, True) is None


async def test_registry_resolve_unknown_returns_none() -> None:
    reg = assistant_actions.PendingRegistry()
    assert await reg.resolve("deadbeef", True) is None


# ── 闸门集成: chat_stream 事件序 + 执行/不执行 ────────────────────

def _script_round(script: list[dict[str, Any]]):
    async def fake_round(messages, tool_schemas, *, temperature=0.3, timeout=240.0):
        step = script.pop(0)
        for piece in step.get("text_pieces", []):
            yield {"type": "text", "delta": piece}
        yield {"type": "round_end", "tool_calls": step.get("tool_calls", []), "finish_reason": "stop"}

    return fake_round


async def _run_chat(
    monkeypatch: pytest.MonkeyPatch,
    tool_calls: list[dict[str, Any]],
    fake_execute,
    on_confirm,
) -> list[dict[str, Any]]:
    monkeypatch.setattr(chat_service, "ai_configured", lambda: True)
    monkeypatch.setattr(chat_service, "is_codex_cli_provider", lambda provider=None: False)
    script = [{"tool_calls": tool_calls}, {"text_pieces": ["完成"]}]
    monkeypatch.setattr(chat_service, "stream_openai_round", _script_round(script))
    monkeypatch.setattr(assistant_tools, "execute_assistant_tool", fake_execute)

    events: list[dict[str, Any]] = []
    async for line in chat_service.chat_stream(history=[{"role": "user", "content": "帮我操作"}]):
        event = json.loads(line)
        events.append(event)
        if event.get("type") == "action_confirm":
            await on_confirm(event)
    return events


async def test_action_tool_gated_and_executes_after_approval(monkeypatch: pytest.MonkeyPatch) -> None:
    executed: list[tuple[str, dict[str, Any]]] = []

    async def fake_execute(name: str, args: dict[str, Any], ctx: Any) -> dict[str, Any]:
        executed.append((name, dict(args)))
        return {"ok": True, "result": {"added": True, "symbol": args.get("symbol")}}

    events = await _run_chat(
        monkeypatch,
        [{"id": "c1", "name": "add_to_watchlist", "arguments": '{"symbol": "600519.SH", "note": "白酒龙头"}'}],
        fake_execute,
        lambda ev: assistant_actions.registry.resolve(ev["call_id"], True),
    )

    confirms = [e for e in events if e["type"] == "action_confirm"]
    assert len(confirms) == 1
    assert confirms[0]["label"] == "加入自选股"
    assert confirms[0]["risk"]
    assert confirms[0]["expires_in"] == 120
    # tool_call 与确认卡共用同一 call_id, 前端据此关联足迹记录
    tool_calls = [e for e in events if e["type"] == "tool_call"]
    assert tool_calls[0]["call_id"] == confirms[0]["call_id"]

    assert executed == [("add_to_watchlist", {"symbol": "600519.SH", "note": "白酒龙头"})]
    results = [e for e in events if e["type"] == "tool_result"]
    assert len(results) == 1 and results[0]["ok"] is True
    # 决策唤醒后事件流继续: 正文 delta 正常产出
    assert any(e["type"] == "delta" for e in events)


async def test_action_tool_denied_returns_error_without_execution(monkeypatch: pytest.MonkeyPatch) -> None:
    executed: list[tuple[str, dict[str, Any]]] = []

    async def fake_execute(name: str, args: dict[str, Any], ctx: Any) -> dict[str, Any]:
        executed.append((name, dict(args)))
        return {"ok": True, "result": {}}

    events = await _run_chat(
        monkeypatch,
        [{"id": "c1", "name": "run_backtest", "arguments": '{"strategy_id": "demo"}'}],
        fake_execute,
        lambda ev: assistant_actions.registry.resolve(ev["call_id"], False),
    )

    assert executed == []
    results = [e for e in events if e["type"] == "tool_result"]
    assert len(results) == 1
    assert results[0]["ok"] is False
    assert "拒绝" in results[0]["summary"]
    # 拒绝后模型继续产出正文(未中断流)
    assert any(e["type"] == "delta" for e in events)


async def test_query_tool_has_no_confirm_gate(monkeypatch: pytest.MonkeyPatch) -> None:
    executed: list[str] = []

    async def fake_execute(name: str, args: dict[str, Any], ctx: Any) -> dict[str, Any]:
        executed.append(name)
        return {"ok": True, "result": {"rows": [], "count": 0}}

    events = await _run_chat(
        monkeypatch,
        [{"id": "c1", "name": "get_stock_quote", "arguments": '{"symbols": ["600519.SH"]}'}],
        fake_execute,
        lambda ev: pytest.fail("查询工具不应触发确认卡"),
    )

    assert not [e for e in events if e["type"] == "action_confirm"]
    assert executed == ["get_stock_quote"]


# ── 决策端点: 未知/过期 call_id 404 ───────────────────────────────

def test_decision_endpoint_rejects_unknown_call() -> None:
    app = FastAPI()
    configure_backend_extensions(app)
    client = TestClient(app)
    resp = client.post(
        "/api/custom/assistant/actions/no-such-id/decision",
        json={"approve": True},
    )
    assert resp.status_code == 404


# ── 写工具往返 ────────────────────────────────────────────────────

def test_create_signal_strategy_roundtrip(tmp_path: Path) -> None:
    ctx = assistant_tools.ToolContext.build(data_dir=tmp_path)
    args = {
        "name": "放量站上20日线",
        "kind": "entry",
        "conditions": [
            {"left": "close", "op": ">", "right": "field:ma20"},
            {"left": "close", "op": "<=", "right": "field:ma20", "leftDays": 1, "rightDays": 1},
            {"left": "vol_ratio_5d", "op": ">=", "right": 1.5},
        ],
    }
    payload = assistant_tools._create_signal_strategy(args, ctx)

    assert payload["created"] is True
    sig = payload["signal"]
    assert sig["id"].startswith("csg_a")
    files = list((tmp_path / "user_data" / "custom_signals").glob("*.json"))
    assert len(files) == 1
    saved = json.loads(files[0].read_text(encoding="utf-8"))
    assert saved["id"] == sig["id"]
    assert saved["timeframe"] == "daily" and saved["enabled"] is True
    # 数字右值归一化为字符串, 与信号库页面保存的格式一致
    assert saved["conditions"][2]["right"] == "1.5"
    assert saved["conditions"][1]["leftDays"] == 1


def test_create_signal_strategy_rejects_unknown_field(tmp_path: Path) -> None:
    ctx = assistant_tools.ToolContext.build(data_dir=tmp_path)
    with pytest.raises(ValueError, match="不在白名单"):
        assistant_tools._create_signal_strategy(
            {"name": "坏信号", "kind": "entry",
             "conditions": [{"left": "no_such_field", "op": ">", "right": "1"}]},
            ctx,
        )
    # 校验失败不应落盘
    assert not list((tmp_path / "user_data" / "custom_signals").glob("*.json"))


def test_create_signal_strategy_requires_conditions(tmp_path: Path) -> None:
    ctx = assistant_tools.ToolContext.build(data_dir=tmp_path)
    with pytest.raises(ValueError, match="conditions"):
        assistant_tools._create_signal_strategy(
            {"name": "空条件", "kind": "entry", "conditions": []}, ctx,
        )


async def test_add_to_watchlist_roundtrip(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from app.services import watchlist as watch_service

    added: list[tuple[str, str]] = []
    monkeypatch.setattr(watch_service, "list_symbols", lambda: [{"symbol": "000001.SZ"}])
    monkeypatch.setattr(
        watch_service, "add",
        lambda symbol, note="", group_id=None: added.append((symbol, note)) or [{"symbol": symbol}],
    )
    ctx = assistant_tools.ToolContext.build(data_dir=tmp_path)

    payload = await assistant_tools.execute_assistant_tool(
        "add_to_watchlist", {"symbol": "600519.SH", "note": "白酒龙头"}, ctx,
    )
    assert payload["ok"] is True
    assert payload["result"]["added"] is True
    assert added == [("600519.SH", "白酒龙头")]

    # 已在列表: 不重复添加, 也不改动既有条目
    payload2 = await assistant_tools.execute_assistant_tool(
        "add_to_watchlist", {"symbol": "000001.SZ", "note": "覆盖备注"}, ctx,
    )
    assert payload2["ok"] is True
    assert payload2["result"]["added"] is False
    assert "未重复添加" in payload2["result"]["note"]
    assert added == [("600519.SH", "白酒龙头")]  # 第二次未调用 add


async def test_action_tool_error_contract_via_execute(tmp_path: Path) -> None:
    """非法参数经统一执行入口仍回 ok=False 契约(不抛异常打断对话流)。"""
    ctx = assistant_tools.ToolContext.build(data_dir=tmp_path)
    payload = await assistant_tools.execute_assistant_tool(
        "add_to_watchlist", {"symbol": "不是代码"}, ctx,
    )
    assert payload["ok"] is False
    assert "无效的证券代码" in payload["error"]


# ── 摘要: 拒绝/超时的足迹卡一行文案 ──────────────────────────────

def test_summarize_action_results() -> None:
    assert "创建信号" in assistant_tools.summarize_tool_result(
        "create_signal_strategy",
        {"ok": True, "result": {"signal": {"id": "csg_a1", "name": "金叉"}}},
    )
    denied = assistant_tools.summarize_tool_result(
        "add_to_watchlist", {"ok": False, "error": "用户已拒绝该操作, 未执行。"},
    )
    assert "拒绝" in denied
