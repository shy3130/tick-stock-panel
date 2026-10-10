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


async def test_registry_timeout_expires_and_recycles() -> None:
    reg = assistant_actions.PendingRegistry(timeout_s=0.02)
    action = await reg.register(assistant_actions.new_call_id(), "run_backtest", {})
    # 超时是「没人作答」, 与用户点拒绝区分, 同样不执行
    assert await reg.await_decision(action) == "expired"
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
    assert confirms[0]["label"] == "加入自选"
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


async def test_action_tool_timeout_is_not_reported_as_refusal(monkeypatch: pytest.MonkeyPatch) -> None:
    """确认卡超时未作答: 不执行, 回填给模型的是「确认超时」而不是「用户已拒绝, 不要重复尝试」。"""
    executed: list[str] = []
    fed_back: list[dict[str, Any]] = []

    async def fake_execute(name: str, args: dict[str, Any], ctx: Any) -> dict[str, Any]:
        executed.append(name)
        return {"ok": True, "result": {}}

    real_summarize = assistant_tools.summarize_tool_result

    def capture(name: str, result: dict[str, Any]) -> str:
        fed_back.append(result)
        return real_summarize(name, result)

    async def no_answer(_event: dict[str, Any]) -> None:
        return None

    monkeypatch.setattr(assistant_actions.registry, "timeout_s", 0.05)
    monkeypatch.setattr(assistant_tools, "summarize_tool_result", capture)
    await _run_chat(
        monkeypatch,
        [{"id": "c1", "name": "run_backtest", "arguments": '{"strategy_id": "demo"}'}],
        fake_execute,
        no_answer,
    )

    assert executed == []
    assert len(fed_back) == 1 and fed_back[0]["ok"] is False
    assert "确认超时" in fed_back[0]["error"]
    assert "拒绝" not in fed_back[0]["error"]


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


# ── 数据完整性检查 + 数据补全 ─────────────────────────────────────

def _make_partitions(data_dir: Path, subdir: str, dates: list[str]) -> None:
    for d in dates:
        (data_dir / subdir / f"date={d}").mkdir(parents=True, exist_ok=True)


def test_check_data_coverage_reports_gaps(tmp_path: Path) -> None:
    from datetime import date as date_cls
    from datetime import timedelta

    today = date_cls.today()
    old = today - timedelta(days=7)
    _make_partitions(tmp_path, "kline_daily", [str(old - timedelta(days=2)), str(old)])
    ctx = assistant_tools.ToolContext.build(data_dir=tmp_path)

    result = assistant_tools._check_data_coverage({}, ctx)

    assert result["daily"]["present"] is True
    assert result["daily"]["latest_date"] == str(old)
    assert result["enriched"]["present"] is False
    issues_text = "\n".join(result["issues"])
    assert "日线停留在" in issues_text and "落后" in issues_text
    assert "enriched" in issues_text or "指标" in issues_text
    assert "财务" in issues_text


def test_check_data_coverage_fresh_data_has_few_issues(tmp_path: Path) -> None:
    import polars as pl

    today_str = assistant_tools.cn_today().isoformat()
    _make_partitions(tmp_path, "kline_daily", [today_str])
    _make_partitions(tmp_path, "kline_daily_enriched", [today_str])
    _make_partitions(tmp_path, "kline_minute", [today_str])
    fin = tmp_path / "financials" / "metrics" / "part.parquet"
    fin.parent.mkdir(parents=True, exist_ok=True)
    pl.DataFrame({"symbol": ["600519.SH"], "period_end": ["2026-06-30"]}).write_parquet(fin)
    ctx = assistant_tools.ToolContext.build(data_dir=tmp_path)

    result = assistant_tools._check_data_coverage({}, ctx)

    assert result["issues"] == []
    assert result["financials"]["metrics"]["latest_period"] == "2026-06-30"


def test_biz_days_behind_counts_workdays_after_latest_up_to_today() -> None:
    from datetime import date as date_cls

    fri, sat, sun, mon = (date_cls(2026, 9, d) for d in (25, 26, 27, 28))
    assert assistant_tools._biz_days_behind("2026-09-25", sat) == 0  # 周五数据, 周六不落后
    assert assistant_tools._biz_days_behind("2026-09-25", sun) == 0
    assert assistant_tools._biz_days_behind("2026-09-25", mon) == 1  # 周一的日K还没有
    assert assistant_tools._biz_days_behind("2026-09-24", sat) == 1  # 缺周五
    assert assistant_tools._biz_days_behind("2026-09-25", fri) == 0


def test_check_data_coverage_weekend_with_friday_data_has_no_lag_issue(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    from datetime import date as date_cls

    monkeypatch.setattr(assistant_tools, "cn_today", lambda: date_cls(2026, 9, 26))  # 周六
    for sub in ("kline_daily", "kline_daily_enriched", "kline_minute"):
        _make_partitions(tmp_path, sub, ["2026-09-24", "2026-09-25"])  # 截至周五
    ctx = assistant_tools.ToolContext.build(data_dir=tmp_path)

    result = assistant_tools._check_data_coverage({}, ctx)

    assert not any("日线停留在" in issue for issue in result["issues"])


class _StubRepo:
    """只实现覆盖检查所需的最小仓储接口。"""

    def resolve_asset_type(self, symbol: str) -> str:
        return "stock"

    def get_daily_asset(self, asset_type: str, symbol: str, start, end):
        import polars as pl

        return pl.DataFrame({"symbol": [symbol] * 3, "date": ["2026-09-22", "2026-09-23", "2026-09-24"]})


def test_check_data_coverage_symbol_lagging(tmp_path: Path) -> None:
    _make_partitions(tmp_path, "kline_daily", ["2026-09-26"])
    ctx = assistant_tools.ToolContext.build(data_dir=tmp_path, repo=_StubRepo())

    result = assistant_tools._check_data_coverage({"symbol": "600519.SH"}, ctx)

    sym = result["symbol"]
    assert sym["symbol"] == "600519.SH"
    assert sym["latest_daily"] == "2026-09-24"
    assert sym["daily_missing"] is True
    assert any("600519.SH 本地日线缺失" in issue for issue in result["issues"])


async def test_sync_data_pipeline_reuses_shared_trigger(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """sync_data 必须走数据页同一条触发路径(单飞语义由该路径保证)。"""
    from app.api import kline as kline_module
    from app.api import pipeline as pipeline_module

    triggered: list[dict] = []

    async def fake_pipeline(repo, capset, quote_service=None):
        triggered.append({"kind": "pipeline"})
        return {"job_id": "job_p1", "reused": False}

    async def fake_minute(repo, capset, *, override_days=None, extend_flag=None):
        triggered.append({"kind": "minute", "days": override_days, "extend": extend_flag})
        return {"status": "started", "job_id": "job_m1"}

    monkeypatch.setattr(pipeline_module, "trigger_pipeline_job", fake_pipeline)
    monkeypatch.setattr(kline_module, "trigger_minute_sync", fake_minute)
    ctx = assistant_tools.ToolContext.build(
        data_dir=tmp_path, repo=object(), capabilities=object(),
    )

    payload = await assistant_tools._sync_data({"kind": "pipeline"}, ctx)
    assert payload["started"] is True and payload["job_id"] == "job_p1"

    payload = await assistant_tools._sync_data({"kind": "minute_extend", "days": 500}, ctx)
    assert payload["job_id"] == "job_m1"
    assert triggered[1]["days"] == 500 and triggered[1]["extend"] is True
    # days 超上限被钳制
    await assistant_tools._sync_data({"kind": "minute_extend", "days": 9999}, ctx)
    assert triggered[2]["days"] == 1095


class _StubFinScheduler:
    def __init__(self) -> None:
        self.calls: list[str | None] = []
        self.is_syncing = True
        self.last_sync = {"metrics": "2026-09-26T10:00:00"}

    def trigger(self, table: str | None = None) -> dict:
        self.calls.append(table)
        return {"started": 1}


async def test_sync_data_financials_and_validation(tmp_path: Path) -> None:
    fs = _StubFinScheduler()
    ctx = assistant_tools.ToolContext.build(data_dir=tmp_path, financial_scheduler=fs)

    payload = await assistant_tools._sync_data({"kind": "financials", "table": "metrics"}, ctx)
    assert payload["started"] is True and payload["table"] == "metrics"
    assert fs.calls == ["metrics"]

    bad = await assistant_tools.execute_assistant_tool("sync_data", {"kind": "nope"}, ctx)
    assert bad["ok"] is False and "kind" in bad["error"]

    bad_table = await assistant_tools.execute_assistant_tool(
        "sync_data", {"kind": "financials", "table": "nope"}, ctx)
    assert bad_table["ok"] is False and "table" in bad_table["error"]


async def test_sync_data_gated_as_action(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """sync_data 属于动作工具: 拒绝后不得触发任何同步。"""
    from app.api import pipeline as pipeline_module

    async def fail_trigger(*args, **kwargs):
        raise AssertionError("sync_data must not execute when denied")

    monkeypatch.setattr(pipeline_module, "trigger_pipeline_job", fail_trigger)
    assert "sync_data" in assistant_actions.ACTION_TOOLS

    executed: list[str] = []

    async def fake_execute(name, args, ctx):
        executed.append(name)
        return {"ok": True, "result": {"started": True}}

    events = await _run_chat(
        monkeypatch,
        [{"id": "c1", "name": "sync_data", "arguments": '{"kind": "pipeline"}'}],
        fake_execute,
        lambda ev: assistant_actions.registry.resolve(ev["call_id"], False),
    )
    assert executed == []
    results = [e for e in events if e["type"] == "tool_result"]
    assert results[0]["ok"] is False and "拒绝" in results[0]["summary"]
    confirms = [e for e in events if e["type"] == "action_confirm"]
    assert confirms[0]["label"] == "数据补全/同步"


def test_sync_status_and_summaries(tmp_path: Path) -> None:
    ctx = assistant_tools.ToolContext.build(data_dir=tmp_path, financial_scheduler=_StubFinScheduler())
    status = assistant_tools._get_sync_status({}, ctx)
    assert status["financial_syncing"] is True
    assert isinstance(status["recent_jobs"], list)

    assert "数据缺口" in assistant_tools.summarize_tool_result(
        "check_data_coverage", {"ok": True, "result": {"issues": ["x"]}})
    assert "无缺口" in assistant_tools.summarize_tool_result(
        "check_data_coverage", {"ok": True, "result": {"issues": []}})
    assert "已启动" in assistant_tools.summarize_tool_result(
        "sync_data", {"ok": True, "result": {"kind": "pipeline", "job_id": "j1", "reused": False}})


# ── 轮次检查点: 到达 _TOOL_ROUND_CHECKPOINT 时询问「继续/停止」 ─────

async def _run_rounds_chat(
    monkeypatch: pytest.MonkeyPatch,
    n_rounds: int,
    on_rounds_confirm,
    checkpoint: int = 2,
) -> list[dict[str, Any]]:
    """每轮都请求一个查询工具, 第 n_rounds+1 轮产出纯文本收尾。"""
    monkeypatch.setattr(chat_service, "ai_configured", lambda: True)
    monkeypatch.setattr(chat_service, "is_codex_cli_provider", lambda provider=None: False)
    monkeypatch.setattr(chat_service, "_round_checkpoint", lambda: checkpoint)
    script = [
        {"tool_calls": [{"id": f"c{i}", "name": "get_indices", "arguments": "{}"}]}
        for i in range(n_rounds)
    ]
    script.append({"text_pieces": ["完成"]})
    monkeypatch.setattr(chat_service, "stream_openai_round", _script_round(script))

    async def fake_execute(name: str, args: dict[str, Any], ctx: Any) -> dict[str, Any]:
        return {"ok": True, "result": {"rows": []}}

    monkeypatch.setattr(assistant_tools, "execute_assistant_tool", fake_execute)
    events: list[dict[str, Any]] = []
    async for line in chat_service.chat_stream(history=[{"role": "user", "content": "跑"}]):
        event = json.loads(line)
        events.append(event)
        if event.get("type") == "rounds_confirm":
            await on_rounds_confirm(event)
    return events


async def test_rounds_checkpoint_continue_resets_and_finishes(monkeypatch: pytest.MonkeyPatch) -> None:
    events = await _run_rounds_chat(
        monkeypatch, 2,
        lambda ev: assistant_actions.registry.resolve(ev["call_id"], True),
    )
    confirms = [e for e in events if e["type"] == "rounds_confirm"]
    assert len(confirms) == 1 and confirms[0]["reached"] == 2 and confirms[0]["expires_in"] > 0
    # 继续后: 两轮工具都执行(tool_call 的 call_id 由后端重新生成), 最终正常收尾
    results = [e for e in events if e["type"] == "tool_result"]
    assert len(results) == 2 and all(r["ok"] for r in results)
    assert any(e["type"] == "delta" for e in events)
    assert events[-1]["type"] == "done"


async def test_rounds_checkpoint_stop_ends_with_notice(monkeypatch: pytest.MonkeyPatch) -> None:
    events = await _run_rounds_chat(
        monkeypatch, 2,
        lambda ev: assistant_actions.registry.resolve(ev["call_id"], False),
    )
    assert [e for e in events if e["type"] == "rounds_confirm"]
    # 停止: 优雅收尾(notice + done), 只执行了检查点前的第一轮, 不产错误事件
    notices = [e for e in events if e["type"] == "notice"]
    assert notices and "停止" in notices[-1]["message"]
    results = [e for e in events if e["type"] == "tool_result"]
    assert len(results) == 1
    assert not any(e["type"] == "error" for e in events)
    assert events[-1]["type"] == "done"


async def test_rounds_checkpoint_timeout_stops(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        assistant_actions, "registry", assistant_actions.PendingRegistry(timeout_s=0.05),
    )

    async def no_decision(event: dict[str, Any]) -> None:
        return None

    events = await _run_rounds_chat(monkeypatch, 2, no_decision)
    assert [e for e in events if e["type"] == "rounds_confirm"]
    assert any(e["type"] == "notice" and "停止" in e["message"] for e in events)
    assert not any(e["type"] == "error" for e in events)


async def test_rounds_checkpoint_zero_disables_prompts(monkeypatch: pytest.MonkeyPatch) -> None:
    """检查点设为 0 (设置页「不检查」): 任意轮次都不弹卡、不中断。"""
    async def fail_on_confirm(event: dict[str, Any]) -> None:
        raise AssertionError("checkpoint=0 不应弹 rounds_confirm")

    events = await _run_rounds_chat(monkeypatch, 3, fail_on_confirm, checkpoint=0)
    assert not any(e["type"] == "rounds_confirm" for e in events)
    results = [e for e in events if e["type"] == "tool_result"]
    assert len(results) == 3
    assert any(e["type"] == "delta" for e in events)
    assert events[-1]["type"] == "done"
