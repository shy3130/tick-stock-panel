"""助手日线查询与 system prompt 的「今天」必须是北京日期, 不能用服务器本地 date.today()。

CONTRIBUTING §3.3: A 股交易时段按北京时间, 服务器时区不能成为隐式输入。
分时小图已经改用 cn_today() 作无快照日期时的回退 (test_assistant_intraday_chart_date),
但 get_stock_daily / get_stock_analysis 的窗口右端, 以及 prompt 里的「今天日期」,
仍是 date.today()。

UTC 主机在北京 00:00-08:00、以及整个美西交易日对应的中国时段, 本地日历日比
北京晚一天或早一天: 日线窗口少取/多取一个自然日, 模型被告知错误的「今天」。
"""
from __future__ import annotations

from datetime import date, timedelta

import polars as pl
import pytest

from app.custom.assistant import prompt as assistant_prompt
from app.custom.assistant import tools as assistant_tools

BJ = date(2026, 9, 21)  # 北京周一
LOCAL = date(2026, 9, 20)  # 服务器本地周日


class _FakeDate(date):
    """让模块内 date.today() 返回 LOCAL, 与 cn_today() 的 BJ 错开。"""

    @classmethod
    def today(cls):
        return LOCAL


def _frame(end: date, n: int = 5) -> pl.DataFrame:
    days = [end - timedelta(days=i) for i in range(n)][::-1]
    return pl.DataFrame({
        "date": days,
        "open": [10.0] * n,
        "high": [11.0] * n,
        "low": [9.0] * n,
        "close": [10.5] * n,
        "volume": [100.0] * n,
    })


class _DailyRepo:
    def __init__(self) -> None:
        self.windows: list[tuple[date, date]] = []

    def resolve_asset_type(self, symbol: str) -> str:
        return "stock"

    def get_name_map(self, symbols=None) -> dict[str, str]:
        return {"600519.SH": "贵州茅台"}

    def get_daily_asset(self, asset_type, symbol, start, end, columns=None) -> pl.DataFrame:
        self.windows.append((start, end))
        return _frame(end)


def _patch_clocks(monkeypatch: pytest.MonkeyPatch, module) -> None:
    monkeypatch.setattr(module, "cn_today", lambda: BJ, raising=False)
    monkeypatch.setattr(module, "date", _FakeDate)


async def test_get_stock_daily_window_ends_on_beijing_today(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_clocks(monkeypatch, assistant_tools)
    repo = _DailyRepo()
    ctx = assistant_tools.ToolContext.build(repo=repo)
    payload = await assistant_tools.execute_assistant_tool(
        "get_stock_daily", {"symbol": "600519.SH", "days": 10}, ctx,
    )
    assert payload["ok"] is True
    assert repo.windows, "应调用 get_daily_asset"
    _start, end = repo.windows[0]
    assert end == BJ, f"日线窗口右端必须是北京日期 {BJ}, 实际 {end} (服务器本地 {LOCAL})"
    assert end != LOCAL


async def test_get_stock_analysis_window_ends_on_beijing_today(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_clocks(monkeypatch, assistant_tools)
    repo = _DailyRepo()
    ctx = assistant_tools.ToolContext.build(repo=repo)
    payload = await assistant_tools.execute_assistant_tool(
        "get_stock_analysis", {"symbol": "600519.SH"}, ctx,
    )
    assert payload["ok"] is True
    assert repo.windows
    _start, end = repo.windows[0]
    assert end == BJ, f"分析窗口右端必须是北京日期 {BJ}, 实际 {end}"


def test_system_prompt_today_is_beijing_date(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(assistant_prompt, "cn_today", lambda: BJ, raising=False)
    monkeypatch.setattr(assistant_prompt, "date", _FakeDate, raising=False)
    text = assistant_prompt.build_system_prompt(None)
    assert f"今天日期: {BJ.isoformat()}" in text
    assert f"今天日期: {LOCAL.isoformat()}" not in text
