from types import SimpleNamespace

import pytest

from app.api.agent import _parse_tool_request, list_tools
from app.services.agent_tools import TOOLS, _truncate, call_tool


def test_agent_tools_endpoint_lists_builtin_tools():
    names = {tool["name"] for tool in list_tools()["tools"]}

    assert {"get_capabilities", "list_strategies", "get_kline", "run_screener", "run_backtest", "get_market_overview", "list_ext_data"} <= names


def test_all_tools_have_schema_and_are_read_only():
    assert all(tool.get("input_schema") for tool in TOOLS)
    assert all(tool.get("read_only") is True for tool in TOOLS)
    assert not any("url" in tool["name"] or "path" in tool["name"] or "shell" in tool["name"] for tool in TOOLS)


def test_tool_result_truncated():
    out = _truncate({"rows": ["x" * 50]}, max_chars=20)

    assert out["truncated"] is True
    assert len(out["preview"]) == 20


def test_parse_tool_request_accepts_json_only():
    assert _parse_tool_request('{"tool":"list_strategies","args":{}}') == {
        "tool": "list_strategies",
        "args": {},
    }
    assert _parse_tool_request("hello") is None


def test_list_strategies_tool_limits_shape():
    strategy = SimpleNamespace(id="s1", name="策略1", source="builtin", tags=["x"])
    state = SimpleNamespace(strategy_engine=SimpleNamespace(list_strategies=lambda: [strategy]))

    out = call_tool("list_strategies", state)

    assert out == {"strategies": [{"id": "s1", "name": "策略1", "source": "builtin", "tags": ["x"]}]}


def test_get_kline_rejects_bad_symbol():
    with pytest.raises(ValueError):
        call_tool("get_kline", SimpleNamespace(repo=object()), {"symbol": "../data"})
