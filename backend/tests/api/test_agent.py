from types import SimpleNamespace

import polars as pl
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
    strategies = [
        {"id": "s1", "name": "策略1", "source": "builtin", "tags": ["x"]},
        {"id": "s2"},
    ]
    state = SimpleNamespace(strategy_engine=SimpleNamespace(list_strategies=lambda: strategies))

    out = call_tool("list_strategies", state)

    assert out == {"strategies": [
        {"id": "s1", "name": "策略1", "source": "builtin", "tags": ["x"]},
        {"id": "s2", "name": "s2", "source": "unknown", "tags": []},
    ]}


def test_get_kline_rejects_bad_symbol():
    with pytest.raises(ValueError):
        call_tool("get_kline", SimpleNamespace(repo=object()), {"symbol": "../data"})


def test_run_backtest_requires_symbols():
    state = SimpleNamespace(repo=object(), strategy_engine=object())
    with pytest.raises(ValueError, match="symbols"):
        call_tool("run_backtest", state, {"strategy_id": "x"})


def test_run_backtest_rejects_too_many_symbols():
    state = SimpleNamespace(repo=object(), strategy_engine=object())
    with pytest.raises(ValueError, match="symbols"):
        call_tool(
            "run_backtest",
            state,
            {"strategy_id": "x", "symbols": [f"{i:06d}.SZ" for i in range(21)]},
        )


def test_run_backtest_rejects_non_list_symbols():
    state = SimpleNamespace(repo=object(), strategy_engine=object())
    with pytest.raises(ValueError, match="symbols"):
        call_tool("run_backtest", state, {"strategy_id": "x", "symbols": "000001.SZ"})


def test_run_backtest_rejects_wide_date_range():
    state = SimpleNamespace(repo=object(), strategy_engine=object())
    with pytest.raises(ValueError, match="date range"):
        call_tool(
            "run_backtest",
            state,
            {
                "strategy_id": "x",
                "symbols": ["000001.SZ"],
                "start": "2024-01-01",
                "end": "2025-01-02",
            },
        )


class _FakePortfolioRepo:
    def __init__(self, symbols: list[str], n_days: int = 60) -> None:
        self._symbols = symbols
        self._n_days = n_days

    def get_daily_asset(self, asset_type, symbol, start, end, columns=None):
        import numpy as np
        from datetime import date as _date, timedelta as _timedelta

        if symbol not in self._symbols:
            return pl.DataFrame()
        rng = np.random.default_rng(sum(bytearray(symbol.encode())))
        base = 10.0
        rows = []
        for offset in range(self._n_days, 0, -1):
            base *= 1 + rng.normal(0, 0.01)
            rows.append({"symbol": symbol, "date": _date.today() - _timedelta(days=offset), "close": round(base, 3)})
        return pl.DataFrame(rows)


def test_optimize_portfolio_tool_requires_symbols():
    state = SimpleNamespace(repo=_FakePortfolioRepo(["000001.SZ"]))
    with pytest.raises(ValueError, match="symbols"):
        call_tool("optimize_portfolio", state, {"symbols": []})


def test_optimize_portfolio_tool_rejects_too_many_symbols():
    state = SimpleNamespace(repo=_FakePortfolioRepo(["000001.SZ"]))
    with pytest.raises(ValueError, match="symbols"):
        call_tool("optimize_portfolio", state, {"symbols": [f"{i:06d}.SZ" for i in range(51)]})


def test_optimize_portfolio_tool_rejects_non_list_symbols():
    state = SimpleNamespace(repo=_FakePortfolioRepo(["000001.SZ"]))
    with pytest.raises(ValueError, match="symbols"):
        call_tool("optimize_portfolio", state, {"symbols": "000001.SZ"})


def test_optimize_portfolio_tool_weights_sum_to_one():
    symbols = ["000001.SZ", "000002.SZ", "600000.SH"]
    state = SimpleNamespace(repo=_FakePortfolioRepo(symbols))

    out = call_tool("optimize_portfolio", state, {"symbols": symbols, "method": "risk_parity", "lookback_days": 80})

    assert len(out["weights"]) == 3
    assert abs(sum(w["weight"] for w in out["weights"]) - 1.0) < 1e-5
    assert out["meta"]["kept"] == symbols
