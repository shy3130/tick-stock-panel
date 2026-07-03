from types import SimpleNamespace

from app.api.agent import _parse_tool_request, list_tools
from app.services.agent_tools import call_tool


def test_agent_tools_endpoint_lists_builtin_tools():
    names = {tool["name"] for tool in list_tools()["tools"]}

    assert {"get_capabilities", "list_strategies"} <= names


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
