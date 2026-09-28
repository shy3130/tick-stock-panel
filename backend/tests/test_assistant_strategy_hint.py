"""策略 id 打错时的报错自纠提示: 错误文案附带可用策略 id 清单。

模型用中文策略名/自造 id 调 run_strategy / run_backtest 时, 裸报「策略 xxx 不存在」
需要再花一轮 list_strategies 才能修正; 附带可用 id 后一次即可自纠,
足迹卡上的失败原因对用户也可读。
"""
from __future__ import annotations

from app.custom.assistant import tools as assistant_tools
from app.services import tool_catalog


class _StubEngine:
    """只覆盖 has/list_strategies 的最小引擎桩。"""

    def has(self, strategy_id: str) -> bool:
        return strategy_id in {"boll_breakout", "limit_up_momentum"}

    def list_strategies(self, *, include_research: bool = False) -> list[dict]:
        return [
            {"id": "boll_breakout", "name": "布林突破"},
            {"id": "limit_up_momentum", "name": "连板接力"},
        ]


async def test_run_strategy_unknown_id_lists_available() -> None:
    ctx = assistant_tools.ToolContext.build(data_dir=".", repo=object(), engine=_StubEngine())

    payload = await assistant_tools.execute_assistant_tool(
        "run_strategy", {"strategy_id": "布林突破"}, ctx,
    )
    assert payload["ok"] is False
    assert "策略 布林突破 不存在" in payload["error"]
    assert "可用策略: boll_breakout, limit_up_momentum" in payload["error"]


async def test_run_backtest_unknown_id_lists_available() -> None:
    payload = await tool_catalog.execute_tool(
        "run_backtest",
        {"strategy_id": "nope"},
        engine=_StubEngine(),
        data_dir=".",
    )
    assert payload["ok"] is False
    assert "策略 nope 不存在" in payload["error"]
    assert "可用策略: boll_breakout, limit_up_momentum" in payload["error"]


def test_hint_sync_path_covers_thread_runner() -> None:
    """_run_strategy 为同步函数, 直接调用确认提示文案(to_thread 路径同文案)。"""
    ctx = assistant_tools.ToolContext.build(data_dir=".", repo=object(), engine=_StubEngine())
    try:
        assistant_tools._run_strategy({"strategy_id": "x"}, ctx)
        raise AssertionError("应当抛 ValueError")
    except ValueError as exc:
        assert "可用策略: boll_breakout, limit_up_momentum" in str(exc)
