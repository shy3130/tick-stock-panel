"""AI 助手免确认开关 + create_strategy 工具。

契约:
- 动作工具的确认闸门受 preferences.assistant_auto_approve_actions 控制,
  该开关**默认关闭** (存量用户升级后不会静默变成全自动执行写操作)。
- 开关打开: 动作工具不再发 action_confirm, 直接执行。
- 开关只影响动作确认; 轮次检查点 (rounds_confirm) 是防跑飞安全阀, 不受影响。
- create_strategy 复用 api/strategy.py 的 save_strategy_code_core 校验链,
  落盘为 ai_ 前缀的 research_only 草稿; 非法代码被拒且不留残文件。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from app.custom.assistant import actions as assistant_actions
from app.custom.assistant import chat_service
from app.custom.assistant import tools as assistant_tools

# ── 开关默认值与读取 ──────────────────────────────────────────

def test_auto_approve_defaults_off(monkeypatch: pytest.MonkeyPatch) -> None:
    """未设置过 → False (安全默认)。"""
    from app.services import preferences

    monkeypatch.setattr(preferences, "load", lambda: {})
    assert assistant_actions.auto_approve_enabled() is False


def test_auto_approve_reads_preference(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.services import preferences

    monkeypatch.setattr(preferences, "load", lambda: {"assistant_auto_approve_actions": True})
    assert assistant_actions.auto_approve_enabled() is True


def test_auto_approve_fails_closed_on_config_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """配置读取抛错时必须按关闭处理, 不能静默放开写操作。"""
    from app.services import preferences

    def boom() -> dict:
        raise RuntimeError("preferences.json malformed")

    monkeypatch.setattr(preferences, "load", boom)
    assert assistant_actions.auto_approve_enabled() is False


def test_create_strategy_is_action_tool() -> None:
    """策略写入是写操作, 必须纳入确认闸门。"""
    assert "create_strategy" in assistant_actions.ACTION_TOOLS
    assert "create_strategy" in assistant_actions.ACTION_META


def test_create_strategy_schema_registered() -> None:
    names = [s["function"]["name"] for s in assistant_tools.build_tool_schemas()]
    assert "create_strategy" in names


# ── 闸门: 开关打开 → 不发确认事件 ─────────────────────────────

def _script_round(script: list[dict[str, Any]]):
    async def fake_round(messages, tool_schemas, *, temperature=0.3, timeout=240.0):
        step = script.pop(0)
        for piece in step.get("text_pieces", []):
            yield {"type": "text", "delta": piece}
        yield {"type": "round_end", "tool_calls": step.get("tool_calls", []), "finish_reason": "stop"}

    return fake_round


async def _drive_chat(
    monkeypatch: pytest.MonkeyPatch,
    tool_calls: list[dict[str, Any]],
    fake_execute,
) -> list[dict[str, Any]]:
    monkeypatch.setattr(chat_service, "ai_configured", lambda: True)
    monkeypatch.setattr(chat_service, "is_codex_cli_provider", lambda provider=None: False)
    script = [{"tool_calls": tool_calls}, {"text_pieces": ["完成"]}]
    monkeypatch.setattr(chat_service, "stream_openai_round", _script_round(script))
    monkeypatch.setattr(assistant_tools, "execute_assistant_tool", fake_execute)

    events: list[dict[str, Any]] = []
    async for line in chat_service.chat_stream(history=[{"role": "user", "content": "帮我操作"}]):
        events.append(json.loads(line))
    return events


# round_end 的 tool_calls 是扁平结构 (id/name/arguments), 不是 OpenAI 的嵌套 function 对象
CALL = [{"id": "c1", "name": "add_to_watchlist", "arguments": "{}"}]


async def test_gate_still_asks_when_auto_approve_off(monkeypatch: pytest.MonkeyPatch) -> None:
    """默认关闭: 动作工具仍先发 action_confirm (且因为没有决策会超时未执行)。"""
    # 显式固定"开关关闭": 否则用例读真实 preferences.json, 用户开着免确认时会误红。
    from app.services import preferences

    monkeypatch.setattr(preferences, "load", lambda: {})
    monkeypatch.setattr(assistant_actions.registry, "timeout_s", 0.05)
    executed: list[str] = []

    async def fake_execute(name: str, args: dict[str, Any], ctx: Any) -> dict[str, Any]:
        executed.append(name)
        return {"ok": True, "result": {}}

    events = await _drive_chat(monkeypatch, CALL, fake_execute)
    assert any(e.get("type") == "action_confirm" for e in events)
    assert executed == []  # 无人确认 → 不执行


async def test_auto_approve_skips_confirm_and_executes(monkeypatch: pytest.MonkeyPatch) -> None:
    """开关打开: 不发 action_confirm, 直接执行动作工具。"""
    from app.services import preferences

    monkeypatch.setattr(preferences, "load", lambda: {"assistant_auto_approve_actions": True})
    executed: list[str] = []

    async def fake_execute(name: str, args: dict[str, Any], ctx: Any) -> dict[str, Any]:
        executed.append(name)
        return {"ok": True, "result": {"added": True}}

    events = await _drive_chat(monkeypatch, CALL, fake_execute)
    assert not any(e.get("type") == "action_confirm" for e in events)
    assert executed == ["add_to_watchlist"]
    assert any(e.get("type") == "tool_result" and e.get("ok") for e in events)


# ── create_strategy: 复用保存校验 ─────────────────────────────

def _valid_strategy_code(sid: str = "ai_test") -> str:
    return f'''"""测试策略"""
import polars as pl

META = {{
    "id": "{sid}",
    "name": "测试策略",
    "description": "用于单测的策略",
    "tags": ["test"],
    "asset_types": ["stock"],
    "timeframes": ["1d"],
    "basic_filter": {{"price_min": 1}},
    "params": [],
}}


def filter_history(df: pl.DataFrame, params: dict) -> pl.DataFrame:
    return df.filter(pl.col("close") > 0)
'''


def test_create_strategy_rejects_empty_code() -> None:
    ctx = assistant_tools.ToolContext.build(data_dir=".", engine=object())
    with pytest.raises(ValueError, match="code 不能为空"):
        assistant_tools._create_strategy({"name": "x", "code": "  "}, ctx)


def test_create_strategy_rejects_missing_name() -> None:
    ctx = assistant_tools.ToolContext.build(data_dir=".", engine=object())
    with pytest.raises(ValueError, match="name 不能为空"):
        assistant_tools._create_strategy({"code": "x = 1"}, ctx)


def test_create_strategy_requires_engine_and_data_dir() -> None:
    ctx = assistant_tools.ToolContext.build()
    with pytest.raises(ValueError, match="未就绪"):
        assistant_tools._create_strategy({"name": "x", "code": "x = 1"}, ctx)


def test_create_strategy_rejects_oversize_code() -> None:
    ctx = assistant_tools.ToolContext.build(data_dir=".", engine=object())
    big = "x" * (assistant_tools._MAX_STRATEGY_CODE_CHARS + 1)
    with pytest.raises(ValueError, match="过长"):
        assistant_tools._create_strategy({"name": "x", "code": big}, ctx)


def test_create_strategy_uses_save_core_and_writes_draft(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """正常路径: 走 save_strategy_code_core 落盘, 结果带 ai_ 前缀与草稿态标记。"""
    from app.api import strategy as strategy_api

    captured: dict[str, Any] = {}

    def fake_core(req, engine, data_dir, *, legacy_ai_path=False):
        captured["strategy_id"] = req.strategy_id
        captured["target_source"] = req.target_source
        captured["mode"] = req.mode
        captured["name"] = req.name
        return {
            "ok": True,
            "strategy_id": req.strategy_id,
            "source": "ai",
            "research_only": True,
            "path": str(tmp_path / "strategies" / "ai" / f"{req.strategy_id}.py"),
            "meta": {"params": [{"id": "p1"}]},
        }

    monkeypatch.setattr(strategy_api, "save_strategy_code_core", fake_core)

    ctx = assistant_tools.ToolContext.build(data_dir=str(tmp_path), engine=object())
    out = assistant_tools._create_strategy(
        {"name": "测试策略", "description": "一句话", "code": _valid_strategy_code()},
        ctx,
    )

    assert out["created"] is True
    assert out["strategy_id"].startswith("ai_")
    assert out["research_only"] is True
    assert out["params"] == ["p1"]
    # 助手不应自己指定 id / 也不应走 custom 源
    assert captured["target_source"] == "ai"
    assert captured["mode"] == "create"
    assert captured["name"] == "测试策略"


def test_create_strategy_propagates_validation_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """校验失败: 错误要透传给模型, 不吞掉。"""
    from app.api import strategy as strategy_api

    def fake_core(req, engine, data_dir, *, legacy_ai_path=False):
        raise ValueError("策略保存失败: 找不到 META 字典")

    monkeypatch.setattr(strategy_api, "save_strategy_code_core", fake_core)

    ctx = assistant_tools.ToolContext.build(data_dir=str(tmp_path), engine=object())
    with pytest.raises(ValueError, match="META"):
        assistant_tools._create_strategy({"name": "x", "code": "print(1)"}, ctx)


# ── create_strategy 两步流程: 先取规范, 再保存 ────────────────
#
# 助手没有文件读取工具, 提示词让模型「去读 prompts/xxx.md」是够不着的指令。
# show_guide=true 把编写规范作为工具返回值直接给模型, 且该调用只读、不弹确认卡。

def test_show_guide_returns_guide_without_saving() -> None:
    ctx = assistant_tools.ToolContext.build(data_dir=".", engine=object())
    out = assistant_tools._create_strategy({"show_guide": True}, ctx)
    assert "guide" in out
    assert "META" in out["guide"]
    assert "next" in out


def test_show_guide_call_is_read_only() -> None:
    """取规范不产生写副作用 → 不弹确认卡 (否则两步建策略要点两次)。"""
    assert assistant_actions.is_read_only_call("create_strategy", {"show_guide": True}) is True


def test_save_call_is_not_read_only() -> None:
    assert assistant_actions.is_read_only_call(
        "create_strategy", {"name": "x", "code": "y"},
    ) is False


def test_read_only_check_ignores_other_tools() -> None:
    """只读豁免只针对 create_strategy 的 show_guide, 不能顺带放过别的写工具。"""
    assert assistant_actions.is_read_only_call("run_backtest", {"strategy_id": "x"}) is False
    assert assistant_actions.is_read_only_call("add_to_watchlist", {"symbol": "600519.SH"}) is False


async def test_show_guide_skips_confirm_gate(monkeypatch: pytest.MonkeyPatch) -> None:
    """show_guide 的调用不应产生 action_confirm 事件。"""
    from app.services import preferences

    monkeypatch.setattr(preferences, "load", lambda: {})  # 免确认开关保持关闭
    executed: list[str] = []

    async def fake_execute(name: str, args: dict[str, Any], ctx: Any) -> dict[str, Any]:
        executed.append(name)
        return {"ok": True, "result": {"guide": "..."}}

    call = [{"id": "g1", "name": "create_strategy", "arguments": '{"show_guide": true}'}]
    events = await _drive_chat(monkeypatch, call, fake_execute)
    assert not any(e.get("type") == "action_confirm" for e in events)
    assert executed == ["create_strategy"]


# ── 因子榜单次新股过滤 ────────────────────────────────────────
#
# 探测发现: 问「RSI 最低的十只」时榜首全是新股 (RSI=0), 因为上市初期序列太短,
# 递推指标失真。按值排序不过滤, 整个榜单被新股挤满。

def test_listing_dates_parses_iso_strings(tmp_path: Path) -> None:
    """instruments 的 listing_date 是 ISO 字符串 (不是 date 类型), 必须能解析。"""
    import polars as pl

    d = tmp_path / "instruments"
    d.mkdir(parents=True)
    pl.DataFrame({
        "symbol": ["600000.SH", "301999.SZ", "301998.SZ"],
        "listing_date": ["2000-01-04", "2026-10-01", None],
    }).write_parquet(d / "instruments.parquet")

    class _Store:
        data_dir = tmp_path

    class _Repo:
        store = _Store()

    got = assistant_tools._listing_dates(_Repo())
    assert got["600000.SH"].isoformat() == "2000-01-04"
    assert got["301999.SZ"].isoformat() == "2026-10-01"
    assert "301998.SZ" not in got  # 空值不参与过滤


def test_listing_dates_missing_table_returns_empty(tmp_path: Path) -> None:
    """维表缺失时退化为不过滤, 不抛错。"""
    class _Store:
        data_dir = tmp_path

    class _Repo:
        store = _Store()

    assert assistant_tools._listing_dates(_Repo()) == {}


def test_factor_values_filters_new_listings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """上市不足 min_listed_days 的标的要从榜单里剔除, 并在返回值里说明。"""
    import polars as pl

    inst = tmp_path / "instruments"
    inst.mkdir(parents=True)
    pl.DataFrame({
        "symbol": ["600000.SH", "301999.SZ"],
        "listing_date": ["2000-01-04", "2026-10-01"],
    }).write_parquet(inst / "instruments.parquet")

    enriched = pl.DataFrame({
        "symbol": ["301999.SZ", "600000.SH"],
        "rsi_14": [0.0, 35.5],
        "change_pct": [-0.11, 0.02],
        "date": ["2026-10-23", "2026-10-23"],
    })

    class _Store:
        data_dir = tmp_path

    class _Repo:
        store = _Store()

        def get_enriched_latest(self):
            return enriched, __import__("datetime").date(2026, 10, 23)

        def get_name_map(self, symbols):
            return {s: s for s in symbols}

    ctx = assistant_tools.ToolContext.build(repo=_Repo(), data_dir=str(tmp_path))
    out = assistant_tools._get_factor_values(
        {"factor": "rsi_14", "top": 10, "direction": "asc"}, ctx,
    )
    symbols = [r["symbol"] for r in out["rows"]]
    assert "301999.SZ" not in symbols       # 新股被排除
    assert symbols == ["600000.SH"]
    assert out["excluded_new_listings"] == 1
    assert "次新股" in out["note"]


def test_factor_values_allows_disabling_filter(tmp_path: Path) -> None:
    """min_listed_days=0 关闭过滤, 新股回到榜单。"""
    import polars as pl

    inst = tmp_path / "instruments"
    inst.mkdir(parents=True)
    pl.DataFrame({
        "symbol": ["600000.SH", "301999.SZ"],
        "listing_date": ["2000-01-04", "2026-10-01"],
    }).write_parquet(inst / "instruments.parquet")

    enriched = pl.DataFrame({
        "symbol": ["301999.SZ", "600000.SH"],
        "rsi_14": [0.0, 35.5],
        "change_pct": [-0.11, 0.02],
        "date": ["2026-10-23", "2026-10-23"],
    })

    class _Store:
        data_dir = tmp_path

    class _Repo:
        store = _Store()

        def get_enriched_latest(self):
            return enriched, __import__("datetime").date(2026, 10, 23)

        def get_name_map(self, symbols):
            return {s: s for s in symbols}

    ctx = assistant_tools.ToolContext.build(repo=_Repo(), data_dir=str(tmp_path))
    out = assistant_tools._get_factor_values(
        {"factor": "rsi_14", "top": 10, "direction": "asc", "min_listed_days": 0}, ctx,
    )
    assert [r["symbol"] for r in out["rows"]] == ["301999.SZ", "600000.SH"]
    assert "excluded_new_listings" not in out


# ── 写工具结果统一标记 ────────────────────────────────────────
#
# 历史上写工具分别返回 created / added / started, 模型要逐工具记住哪个字段表示
# "已生效"。统一补 action_ok 后只需认一个字段 (原字段保留, 不破坏既有消费方)。

def test_action_result_marked_for_write_tools() -> None:
    out = assistant_tools._mark_action_result(
        "create_strategy", {}, {"created": True, "strategy_id": "ai_x"},
    )
    assert out["action_ok"] is True
    assert out["action"] == "create_strategy"
    assert out["created"] is True          # 原字段保留
    assert out["strategy_id"] == "ai_x"


def test_action_result_untouched_for_read_tools() -> None:
    assert assistant_tools._mark_action_result("get_watchlist", {}, {"rows": []}) == {"rows": []}


def test_action_result_marking_is_idempotent() -> None:
    once = assistant_tools._mark_action_result("sync_data", {}, {"started": True})
    twice = assistant_tools._mark_action_result("sync_data", {}, once)
    assert twice == once


def test_action_result_ignores_non_dict() -> None:
    assert assistant_tools._mark_action_result("sync_data", {}, ["x"]) == ["x"]


async def test_execute_marks_action_result(monkeypatch: pytest.MonkeyPatch) -> None:
    """经过统一执行入口后, 写工具结果带上 action_ok。"""
    async def fake_handler(args, ctx):
        return {"created": True}

    monkeypatch.setitem(assistant_tools._LOCAL_TOOLS, "create_strategy", fake_handler)
    ctx = assistant_tools.ToolContext.build(data_dir=".", engine=object())
    out = await assistant_tools.execute_assistant_tool("create_strategy", {}, ctx)
    assert out["ok"] is True
    assert out["result"]["action_ok"] is True


def test_show_guide_call_is_not_marked_as_action() -> None:
    """取编写规范不落盘, 不能标成 action_ok —— 否则模型会以为策略已创建。"""
    out = assistant_tools._mark_action_result(
        "create_strategy", {"show_guide": True}, {"guide": "...", "next": "..."},
    )
    assert "action_ok" not in out
