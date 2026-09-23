"""助手 run_backtest 缺省结束日必须是北京日期, 不能用服务器本地 date.today()。

CONTRIBUTING §3.3: A 股交易时段按北京时间, 服务器时区不能成为隐式输入。
get_stock_daily / get_stock_analysis / 系统提示词已经改用 cn_today()
(test_assistant_beijing_today), 但助手回测工具 tool_catalog.run_backtest
缺省 end 仍是 date.today()。快捷指令「回测我的策略」不传日期, 走这条缺省。

UTC 主机在北京 00:00-08:00、以及美西主机整个 A 股交易时段, 本地日历日比
北京早一天: 回测窗口少取一个自然日, 刚收盘的当日成交不进样本。
"""
from __future__ import annotations

from datetime import date, timedelta
from typing import Any

import pytest

from app.custom.assistant import tools as assistant_tools
from app.services import tool_catalog

BJ = date(2026, 9, 21)  # 北京周一
LOCAL = date(2026, 9, 20)  # 服务器本地周日


class _FakeDate(date):
    """让模块内 date.today() 返回 LOCAL, 与 cn_today() 的 BJ 错开。"""

    @classmethod
    def today(cls):
        return LOCAL


def _patch_clocks(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tool_catalog, "cn_today", lambda: BJ, raising=False)
    monkeypatch.setattr(tool_catalog, "date", _FakeDate)


def _patch_worker(monkeypatch: pytest.MonkeyPatch, captured: dict[str, Any]) -> None:
    """避开真实回测子进程, 只记录 StrategyBacktestConfig 的 start/end。"""

    def fake_make_worker_task(kind, data_dir, cfg):
        captured["kind"] = kind
        captured["start"] = cfg.start
        captured["end"] = cfg.end
        return {"kind": kind}

    def fake_run_worker_task(task):
        return {
            "stats": {
                "total_return": 0.12,
                "annual_return": 0.25,
                "max_drawdown": -0.08,
                "sharpe": 1.1,
                "n_trades": 4,
            },
        }

    monkeypatch.setattr("app.backtest.worker.make_worker_task", fake_make_worker_task)
    monkeypatch.setattr("app.backtest.worker.run_worker_task", fake_run_worker_task)


def test_run_backtest_default_end_is_beijing_today(
    monkeypatch: pytest.MonkeyPatch, tmp_path,
) -> None:
    captured: dict[str, Any] = {}
    _patch_clocks(monkeypatch)
    _patch_worker(monkeypatch, captured)

    result = tool_catalog.run_backtest(tmp_path, strategy_id="demo_a")

    assert captured["end"] == BJ, (
        f"回测缺省结束日必须是北京日期 {BJ}, 实际 {captured.get('end')} "
        f"(服务器本地 {LOCAL})"
    )
    assert captured["end"] != LOCAL
    assert captured["start"] == BJ - timedelta(days=180)
    assert result["end"] == str(BJ)
    assert result["start"] == str(BJ - timedelta(days=180))


def test_run_backtest_explicit_end_is_kept(
    monkeypatch: pytest.MonkeyPatch, tmp_path,
) -> None:
    captured: dict[str, Any] = {}
    _patch_clocks(monkeypatch)
    _patch_worker(monkeypatch, captured)
    explicit = date(2026, 6, 30)

    result = tool_catalog.run_backtest(
        tmp_path, strategy_id="demo_a", end=explicit.isoformat(),
    )

    assert captured["end"] == explicit
    assert result["end"] == "2026-06-30"


async def test_assistant_run_backtest_tool_uses_beijing_today(
    monkeypatch: pytest.MonkeyPatch, tmp_path,
) -> None:
    captured: dict[str, Any] = {}
    _patch_clocks(monkeypatch)
    _patch_worker(monkeypatch, captured)

    payload = await assistant_tools.execute_assistant_tool(
        "run_backtest",
        {"strategy_id": "demo_a"},
        assistant_tools.ToolContext.build(data_dir=tmp_path),
    )

    assert payload["ok"] is True
    assert captured["end"] == BJ, (
        f"助手 run_backtest 缺省结束日必须是北京日期 {BJ}, 实际 {captured.get('end')}"
    )
    assert payload["result"]["end"] == BJ.isoformat()
