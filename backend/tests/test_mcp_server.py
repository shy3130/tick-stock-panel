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


def test_mcp_main_closes_all_resources_when_one_close_fails(monkeypatch):
    closed: list[str] = []
    state = SimpleNamespace(
        capabilities=SimpleNamespace(all=lambda: {"daily"}),
        datastore=SimpleNamespace(close=lambda: closed.append("store")),
    )

    def close_providers():
        closed.append("providers")
        raise RuntimeError("provider close failed")

    monkeypatch.setattr(mcp_server, "build_state", lambda: state)
    monkeypatch.setattr(mcp_server, "close_all_providers", close_providers)
    monkeypatch.setattr(
        mcp_server,
        "close_screener_sql_connection",
        lambda: closed.append("screener"),
    )

    assert mcp_server.main(["--self-test"]) == 0
    assert closed == ["providers", "screener", "store"]
