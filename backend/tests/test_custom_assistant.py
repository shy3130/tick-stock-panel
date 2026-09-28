"""AI 对话助手扩展契约测试。

覆盖: 扩展经 loader 自动注册路由、门控 fail-closed(无 Key / Codex CLI)、
SSE 流式工具循环的事件顺序与逐字 delta、历史截断、本地工具执行与
摘要白名单、HTTP 流式端点行为。
"""
from __future__ import annotations

import json
from typing import Any

import polars as pl
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.custom.assistant import actions as assistant_actions
from app.custom.assistant import chat_service
from app.custom.assistant import tools as assistant_tools
from app.extensions.loader import configure_backend_extensions


async def _collect(lines: list[str]) -> list[dict[str, Any]]:
    return [json.loads(line) for line in lines]


async def _drain(gen) -> list[str]:
    return [line async for line in gen]


def _stream_script(script: list[dict[str, Any]], capture: dict[str, Any] | None = None):
    """构造假的 stream_openai_round: 按脚本顺序消费轮次。

    脚本元素: {"tool_calls": [...]} 表示本轮请求工具; {"text": str} 拆成
    多个 delta 事件模拟逐字流式; 两者可共存(先文本后工具)。
    """

    async def fake_round(messages, tool_schemas, *, temperature=0.3, timeout=240.0):
        if capture is not None:
            capture.setdefault("messages", []).append(list(messages))
            capture.setdefault("schemas", list(tool_schemas))
        step = script.pop(0)
        for piece in step.get("text_pieces", []):
            yield {"type": "text", "delta": piece}
        yield {
            "type": "round_end",
            "tool_calls": step.get("tool_calls", []),
            "finish_reason": "stop",
        }

    return fake_round


class _StubEngine:
    """只实现 list_strategies, 供 list_strategies 工具调用的最小引擎。"""

    def list_strategies(self, include_research: bool = False) -> list[dict]:
        return [
            {"id": "demo_a", "name": "演示A", "description": "", "tags": []},
            {"id": "demo_b", "name": "演示B", "description": "", "tags": []},
        ]


def test_extension_registers_routes_via_loader() -> None:
    app = FastAPI()
    registry, errors = configure_backend_extensions(app)
    assert "assistant.chat" in registry.extension_ids()
    assert errors == ()

    client = TestClient(app)
    status = client.get("/api/custom/assistant/status")
    assert status.status_code == 200
    body = status.json()
    assert set(body) == {"configured", "provider", "model", "supports_tools"}

    suggests = client.get("/api/custom/assistant/suggests")
    assert suggests.status_code == 200
    items = suggests.json()["suggests"]
    assert items and {"id", "label", "prompt"} <= set(items[0])


async def test_chat_stream_fails_closed_without_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(chat_service, "ai_configured", lambda: False)
    events = await _collect(await _drain(chat_service.chat_stream(
        history=[{"role": "user", "content": "hi"}],
    )))

    assert events[0]["type"] == "error"
    assert events[0]["kind"] == "no_key"
    assert events[0]["hint"] == "/settings?tab=ai"
    assert events[-1]["type"] == "done"


async def test_chat_stream_fails_closed_for_codex_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(chat_service, "ai_configured", lambda: True)
    monkeypatch.setattr(chat_service, "is_codex_cli_provider", lambda provider=None: True)
    events = await _collect(await _drain(chat_service.chat_stream(
        history=[{"role": "user", "content": "hi"}],
    )))

    assert events[0]["type"] == "error"
    assert events[0]["kind"] == "provider"
    assert events[-1]["type"] == "done"


async def test_chat_stream_streams_text_deltas_word_by_word(monkeypatch: pytest.MonkeyPatch) -> None:
    """纯文本回答: 逐字 delta 顺序产出, 内容按序拼接。"""
    script = [{"text_pieces": ["你", "好", "共有 ", "2 ", "个策略。"]}]
    monkeypatch.setattr(chat_service, "ai_configured", lambda: True)
    monkeypatch.setattr(chat_service, "is_codex_cli_provider", lambda provider=None: False)
    monkeypatch.setattr(chat_service, "stream_openai_round", _stream_script(script))

    events = await _collect(await _drain(chat_service.chat_stream(
        history=[{"role": "user", "content": "我有哪些策略?"}],
    )))

    kinds = [e["type"] for e in events]
    assert kinds == ["delta"] * 5 + ["done"]
    assert "".join(e["content"] for e in events[:-1]) == "你好共有 2 个策略。"


async def test_chat_stream_emits_tool_events_in_order(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}
    script = [
        {"tool_calls": [{"id": "call_1", "name": "list_strategies", "arguments": "{}"}]},
        {"text_pieces": ["你共有 ", "2 个策略。"]},
    ]
    monkeypatch.setattr(chat_service, "ai_configured", lambda: True)
    monkeypatch.setattr(chat_service, "is_codex_cli_provider", lambda provider=None: False)
    monkeypatch.setattr(chat_service, "stream_openai_round", _stream_script(script, captured))

    events = await _collect(await _drain(chat_service.chat_stream(
        history=[{"role": "user", "content": "我有哪些策略?"}],
        context={"page": "自选"},
        engine=_StubEngine(),
        data_dir=None,
    )))

    kinds = [e["type"] for e in events]
    assert kinds == ["tool_call", "tool_result", "delta", "delta", "done"]

    tool_call = events[0]
    assert tool_call["name"] == "list_strategies"
    assert tool_call["call_id"]

    tool_result = events[1]
    assert tool_result["ok"] is True
    assert tool_result["summary"] == "返回 2 个策略"
    assert tool_result["elapsed_ms"] >= 0

    assert events[2]["content"] == "你共有 "
    assert events[3]["content"] == "2 个策略。"

    # 传给模型的 system prompt 注入了上下文; schema 含核心与本地两类工具。
    system = captured["messages"][0][0]
    assert system["role"] == "system"
    assert "自选" in system["content"]
    schema_names = {s["function"]["name"] for s in captured["schemas"]}
    assert "run_backtest" in schema_names
    assert "get_market_overview" in schema_names

    # 第二轮请求包含 assistant(tool_calls) + role:tool 回填。
    second_round = captured["messages"][1]
    assert second_round[-2]["role"] == "assistant"
    assert second_round[-2]["tool_calls"][0]["function"]["name"] == "list_strategies"
    assert second_round[-1]["role"] == "tool"
    assert json.loads(second_round[-1]["content"])["ok"] is True


async def test_chat_stream_rounds_checkpoint_prompts_and_stops(monkeypatch: pytest.MonkeyPatch) -> None:
    """轮次检查点(取代旧 6 轮硬限): 到点弹 rounds_confirm, 拒绝则优雅收尾。"""
    script = [
        {"tool_calls": [{"id": f"c{i}", "name": "list_factors", "arguments": "{}"}]}
        for i in range(3)
    ]
    monkeypatch.setattr(chat_service, "ai_configured", lambda: True)
    monkeypatch.setattr(chat_service, "is_codex_cli_provider", lambda provider=None: False)
    monkeypatch.setattr(chat_service, "stream_openai_round", _stream_script(script))
    monkeypatch.setattr(chat_service, "_round_checkpoint", lambda: 2)
    # list_factors 走核心目录且无需引擎; 简化起见用本地工具拦截执行。
    monkeypatch.setattr(
        assistant_tools,
        "execute_assistant_tool",
        _fake_execute_tool,
    )

    events: list[dict[str, Any]] = []
    async for line in chat_service.chat_stream(history=[{"role": "user", "content": "hi"}]):
        event = json.loads(line)
        events.append(event)
        if event.get("type") == "rounds_confirm":
            await assistant_actions.registry.resolve(event["call_id"], False)

    confirm = next(e for e in events if e["type"] == "rounds_confirm")
    assert confirm["reached"] == 2 and confirm["expires_in"] > 0
    notices = [e for e in events if e["type"] == "notice"]
    assert notices and "停止" in notices[-1]["message"]
    assert not any(e["type"] == "error" for e in events)
    assert events[-1]["type"] == "done"


async def test_chat_stream_truncates_long_history(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}
    script = [{"text_pieces": ["好的"]}]
    monkeypatch.setattr(chat_service, "ai_configured", lambda: True)
    monkeypatch.setattr(chat_service, "is_codex_cli_provider", lambda provider=None: False)
    monkeypatch.setattr(chat_service, "stream_openai_round", _stream_script(script, captured))

    history = []
    for i in range(12):
        history.append({"role": "user", "content": f"问题{i}"})
        history.append({"role": "assistant", "content": f"回答{i}"})
    history.append({"role": "user", "content": "最新问题"})

    events = await _collect(await _drain(chat_service.chat_stream(history=history)))

    assert events[0]["type"] == "notice"
    delivered = captured["messages"][0]
    # system + 截断窗口(≤16 条, 以 user 开头, 以最新问题结尾)
    assert len(delivered) <= 1 + chat_service._MAX_HISTORY_MESSAGES
    assert delivered[1]["role"] == "user"
    assert delivered[-1]["content"] == "最新问题"


async def test_chat_stream_surfaces_generate_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    async def failing_round(messages, tool_schemas, *, temperature=0.3, timeout=240.0):
        raise RuntimeError("AI 服务请求失败(500): 上游错误")
        yield  # pragma: no cover

    monkeypatch.setattr(chat_service, "ai_configured", lambda: True)
    monkeypatch.setattr(chat_service, "is_codex_cli_provider", lambda provider=None: False)
    monkeypatch.setattr(chat_service, "stream_openai_round", failing_round)

    events = await _collect(await _drain(chat_service.chat_stream(
        history=[{"role": "user", "content": "hi"}],
    )))

    assert events[0]["type"] == "error"
    assert events[0]["kind"] == "model"
    assert "AI 服务请求失败" in events[0]["message"]
    assert events[-1]["type"] == "done"


async def test_chat_stream_rejects_empty_history() -> None:
    events = await _collect(await _drain(chat_service.chat_stream(history=[])))
    assert events[0]["type"] == "error"
    assert events[-1]["type"] == "done"


# ----------------------------------------------------------------
# 本地查询工具
# ----------------------------------------------------------------
class _StubRepo:
    """最小 repo 桩: 名称映射 + 空 enriched/latest-date。"""

    def get_name_map(self, symbols=None) -> dict[str, str]:
        return {"600519.SH": "贵州茅台"}


class _StubQuoteService:
    def get_quotes_compat(self) -> pl.DataFrame:
        return pl.DataFrame({
            "symbol": ["600519.SH", "300750.SZ"],
            "close": [1500.0, 200.5],
            "change_pct": [0.0123, -0.0456],
            "amount": [3.0e9, 5.0e9],
        })


async def _fake_execute_tool(name: str, args: dict[str, Any], ctx: Any) -> dict[str, Any]:
    return {"ok": True, "result": {"rows": [], "count": 0}}


async def test_execute_assistant_tool_local_quote_merges_names() -> None:
    ctx = assistant_tools.ToolContext.build(repo=_StubRepo(), quote_service=_StubQuoteService())
    payload = await assistant_tools.execute_assistant_tool(
        "get_stock_quote",
        {"symbols": ["600519.SH"]},
        ctx,
    )
    assert payload["ok"] is True
    rows = payload["result"]["rows"]
    assert rows[0]["name"] == "贵州茅台"
    assert rows[0]["change_pct"] == 0.0123


class _DailyRepo(_StubRepo):
    """带日线+分钟序列的 repo 桩: 验证 get_stock_daily/get_stock_quote 附带走势小图 payload。"""

    def resolve_asset_type(self, symbol: str) -> str:
        return "stock"

    def get_enriched_latest_asset(self, asset_type, refresh=True):
        return pl.DataFrame(), None

    def get_daily_asset(self, asset_type, symbol, start, end):
        return pl.DataFrame({
            "date": ["2026-09-01", "2026-09-02", "2026-09-03"],
            "open": [9.8, 10.4, 10.6],
            "high": [10.6, 10.7, 10.8],
            "low": [9.7, 10.2, 10.1],
            "close": [10.0, 10.5, 10.3],
            "volume": [100.0, 200.0, 150.0],
        })

    def get_minute(self, symbol, trade_date, asset_type="stock"):
        from datetime import datetime

        return pl.DataFrame({
            "datetime": [
                datetime(2026, 9, 18, 9, 31),
                datetime(2026, 9, 18, 9, 32),
                datetime(2026, 9, 18, 9, 33),
            ],
            "close": [10.02, 10.05, 10.01],
            "volume": [12.0, 8.0, 6.0],
        })


async def test_get_stock_daily_attaches_kline_chart_payload() -> None:
    ctx = assistant_tools.ToolContext.build(repo=_DailyRepo(), quote_service=_StubQuoteService())
    payload = await assistant_tools.execute_assistant_tool(
        "get_stock_daily",
        {"symbol": "600519.SH", "days": 10},
        ctx,
    )
    assert payload["ok"] is True
    charts = payload["result"]["charts"]
    assert charts[0]["kind"] == "daily_kline"
    assert charts[0]["symbol"] == "600519.SH"
    assert charts[0]["points"] == [
        ["2026-09-01", 9.8, 10.6, 9.7, 10.0, 100.0],
        ["2026-09-02", 10.4, 10.7, 10.2, 10.5, 200.0],
        ["2026-09-03", 10.6, 10.8, 10.1, 10.3, 150.0],
    ]


async def test_get_stock_quote_attaches_intraday_chart_for_single_symbol() -> None:
    ctx = assistant_tools.ToolContext.build(repo=_DailyRepo(), quote_service=_StubQuoteService())
    payload = await assistant_tools.execute_assistant_tool(
        "get_stock_quote",
        {"symbols": ["600519.SH"]},
        ctx,
    )
    assert payload["ok"] is True
    charts = payload["result"]["charts"]
    assert charts[0]["kind"] == "intraday"
    assert charts[0]["symbol"] == "600519.SH"
    # 昨收按 close/(1+pct) 反推: 1500 / 1.0123 ≈ 1481.7742
    assert charts[0]["prev_close"] == 1481.774
    assert charts[0]["points"] == [
        ["09:31", 10.02, 12.0],
        ["09:32", 10.05, 8.0],
        ["09:33", 10.01, 6.0],
    ]


async def test_execute_assistant_tool_validates_symbols() -> None:
    ctx = assistant_tools.ToolContext.build(repo=_StubRepo(), quote_service=_StubQuoteService())
    payload = await assistant_tools.execute_assistant_tool(
        "get_stock_quote",
        {"symbols": ["not a symbol!!"]},
        ctx,
    )
    assert payload["ok"] is False
    assert "无效的证券代码" in payload["error"]


async def test_execute_assistant_tool_falls_back_to_core_catalog() -> None:
    ctx = assistant_tools.ToolContext.build(engine=_StubEngine())
    payload = await assistant_tools.execute_assistant_tool(
        "list_strategies", {}, ctx,
    )
    assert payload["ok"] is True
    strategies = payload["result"]["strategies"]
    assert {s["id"] for s in strategies} == {"demo_a", "demo_b"}


def test_local_tool_schemas_cover_page_capabilities() -> None:
    names = {s["function"]["name"] for s in assistant_tools.build_tool_schemas()}
    expected = {
        "list_factors", "list_strategies", "list_data_capabilities", "run_backtest",
        "get_stock_quote", "get_stock_daily", "get_stock_analysis", "get_financials",
        "get_watchlist", "get_market_overview", "get_indices", "get_sector_rotation",
        "get_regime", "get_abnormal", "get_lots", "list_signals", "run_strategy",
        "get_factor_values",
    }
    assert expected <= names


def test_summarize_tool_result_whitelist() -> None:
    backtest = {
        "ok": True,
        "result": {
            "strategy_id": "s1",
            "start": "2026-01-01",
            "end": "2026-06-30",
            "stats": {
                "total_return": 0.23,
                "annual_return": 0.48,
                "max_drawdown": -0.12,
                "sharpe": 1.4,
                "equity_curve": [1, 2, 3],  # 非白名单键, 不应出现在摘要
            },
        },
    }
    summary = assistant_tools.summarize_tool_result("run_backtest", backtest)
    assert "总收益 0.23" in summary
    assert "夏普 1.4" in summary
    assert "equity_curve" not in summary

    failed = {"ok": False, "error": "策略 s1 不存在"}
    assert "失败" in assistant_tools.summarize_tool_result("run_backtest", failed)

    quote_summary = assistant_tools.summarize_tool_result(
        "get_stock_quote", {"ok": True, "result": {"count": 3, "rows": [1, 2, 3]}},
    )
    assert quote_summary == "查询 3 只个股实时行情"


def test_http_chat_endpoint_streams_ndjson(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(chat_service, "ai_configured", lambda: False)

    app = FastAPI()
    configure_backend_extensions(app)
    client = TestClient(app)
    response = client.post("/api/custom/assistant/chat", json={
        "messages": [{"role": "user", "content": "我有哪些策略?"}],
    })

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/x-ndjson")
    events = [json.loads(line) for line in response.text.splitlines() if line.strip()]
    assert events[0]["type"] == "error"
    assert events[0]["kind"] == "no_key"
    assert events[-1]["type"] == "done"
