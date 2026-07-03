from types import SimpleNamespace

from app import mcp_server


def test_mcp_lists_and_calls_tools():
    state = SimpleNamespace(capabilities=SimpleNamespace(all=lambda: {"daily"}))

    listed = mcp_server.handle_message({"id": 1, "method": "tools/list"}, state)
    called = mcp_server.handle_message({"id": 2, "method": "tools/call", "params": {"name": "get_capabilities", "arguments": {}}}, state)

    assert listed["result"]["tools"]
    assert called == {"id": 2, "result": {"capabilities": ["daily"]}}


def test_mcp_unknown_tool_returns_error():
    out = mcp_server.handle_message({"id": 1, "method": "tools/call", "params": {"name": "missing"}}, SimpleNamespace())

    assert "error" in out
