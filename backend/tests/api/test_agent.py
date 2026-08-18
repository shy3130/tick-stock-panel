from types import SimpleNamespace

import polars as pl
import pytest

from app.api.agent import _parse_tool_request, list_tools
from app.services.agent_tools import TOOLS, _truncate, call_tool


def test_agent_tools_endpoint_lists_builtin_tools():
    names = {tool["name"] for tool in list_tools()["tools"]}

    assert {
        "get_capabilities",
        "list_strategies",
        "get_kline",
        "list_screener_fields",
        "screen_stock_pool",
        "start_pool_backtest",
        "get_pool_backtest",
        "get_market_overview",
        "list_ext_data",
    } <= names
    assert {"run_screener", "run_backtest"}.isdisjoint(names)


def test_all_tools_have_schema_and_are_read_only():
    assert all(tool.get("input_schema") for tool in TOOLS)
    assert all(tool.get("read_only") is True for tool in TOOLS)
    assert not any("url" in tool["name"] or "path" in tool["name"] or "shell" in tool["name"] for tool in TOOLS)


def test_tool_result_truncated():
    out = _truncate({"rows": ["x" * 50]}, max_chars=20)

    assert out["truncated"] is True
    assert len(out["preview"]) == 20


def test_parse_tool_request_accepts_json_and_dsml():
    assert _parse_tool_request('{"tool":"list_strategies","args":{}}') == {
        "tool": "list_strategies",
        "args": {},
    }
    assert _parse_tool_request(
        '<｜｜DSML｜｜tool_calls><｜｜DSML｜｜invoke name="list_strategies"></｜｜DSML｜｜invoke></｜｜DSML｜｜tool_calls>'
    ) == {"tool": "list_strategies", "args": {}}
    assert _parse_tool_request(
        '<｜｜DSML｜｜tool_calls><｜｜DSML｜｜invoke name="quote_pool"><｜｜DSML｜｜parameter name="pool" string="false">all</｜｜DSML｜｜parameter></｜｜DSML｜｜invoke></｜｜DSML｜｜tool_calls>'
    ) == {"tool": "quote_pool", "args": {"pool": "all"}}
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


def _factor_panel(symbols, n_days, factor_name):
    import numpy as np
    from datetime import date as _date, timedelta as _timedelta

    rng = np.random.default_rng(11)
    rows = []
    for i, sym in enumerate(symbols):
        price = 10.0 + i
        for d in range(n_days):
            price *= 1 + rng.normal(0, 0.01)
            rows.append({
                "symbol": sym,
                "date": _date(2024, 1, 1) + _timedelta(days=d),
                "close": round(price, 4),
                factor_name: float(i) + d * 0.01,
            })
    return pl.DataFrame(rows)


class _FakeFactorRepo:
    def __init__(self, latest_enriched=None):
        self._latest_enriched = latest_enriched

    def enriched_latest_date(self):
        return self._latest_enriched


def test_analyze_factor_tool_allows_omitted_symbols(monkeypatch):
    from app.backtest import engine as engine_mod

    panel = _factor_panel(["A", "B", "C"], 10, "momentum_20d")
    captured = {}

    def load_panel(self, symbols, *args, **kwargs):
        captured["symbols"] = symbols
        return panel

    monkeypatch.setattr(engine_mod.BacktestEngine, "load_panel", load_panel)
    out = call_tool(
        "analyze_factor",
        SimpleNamespace(repo=_FakeFactorRepo()),
        {
            "factor_name": "momentum_20d",
            "start": "2024-01-01",
            "end": "2024-01-10",
            "rebalance": "daily",
        },
    )

    assert captured["symbols"] is None
    assert out["error"] is None
    analyze_factor = next(tool for tool in TOOLS if tool["name"] == "analyze_factor")
    assert "symbols" not in analyze_factor["parameters"]["required"]

def test_analyze_factor_defaults_to_latest_enriched_date(monkeypatch):
    from datetime import date

    from app.backtest import factor as factor_mod

    captured = {}

    def run(self, config):
        captured["config"] = config
        return factor_mod.FactorResult(run_id="test", config={})

    monkeypatch.setattr(factor_mod.FactorBacktestService, "run", run)
    out = call_tool(
        "analyze_factor",
        SimpleNamespace(repo=_FakeFactorRepo(date(2026, 7, 31))),
        {"factor_name": "momentum_20d"},
    )

    assert out["error"] is None
    assert captured["config"].start == date(2026, 2, 1)
    assert captured["config"].end == date(2026, 7, 31)

def test_analyze_factor_tool_rejects_wide_date_range():
    state = SimpleNamespace(repo=_FakeFactorRepo())
    with pytest.raises(ValueError, match="date range"):
        call_tool(
            "analyze_factor",
            state,
            {
                "factor_name": "momentum_20d",
                "symbols": ["A"],
                "start": "2020-01-01",
                "end": "2024-01-01",
            },
        )


def test_compare_factors_tool_rejects_too_many_factor_ids():
    from app.backtest.factor_zoo import ALPHAS

    ids_pool = list(ALPHAS)
    factor_ids = (ids_pool * ((21 // len(ids_pool)) + 1))[:21]
    state = SimpleNamespace(repo=_FakeFactorRepo())
    with pytest.raises(ValueError, match="factor_ids"):
        call_tool("compare_factors", state, {"factor_ids": factor_ids, "symbols": ["A"]})


def test_analyze_factor_tool_returns_ic_fields(monkeypatch):
    from app.backtest import engine as engine_mod

    panel = _factor_panel(["A", "B", "C"], 10, "momentum_20d")
    monkeypatch.setattr(engine_mod.BacktestEngine, "load_panel", lambda self, *a, **kw: panel)

    state = SimpleNamespace(repo=_FakeFactorRepo())
    out = call_tool(
        "analyze_factor",
        state,
        {
            "factor_name": "momentum_20d",
            "symbols": ["A", "B", "C"],
            "start": "2024-01-01",
            "end": "2024-01-10",
            "rebalance": "daily",
        },
    )

    assert out["error"] is None
    assert "ic_mean" in out


def test_compare_factors_tool_rejects_unknown_factor():
    state = SimpleNamespace(repo=_FakeFactorRepo())
    with pytest.raises(ValueError, match="unknown factor"):
        call_tool("compare_factors", state, {"factor_ids": ["momentum_20d"], "symbols": ["A"]})


def test_compare_factors_tool_returns_rows(monkeypatch):
    from app.backtest import engine as engine_mod
    from app.backtest.factor_zoo import ALPHAS

    any_alpha = next(iter(ALPHAS))
    panel = _factor_panel(["A", "B", "C"], 10, any_alpha)
    monkeypatch.setattr(engine_mod.BacktestEngine, "load_panel", lambda self, *a, **kw: panel)

    state = SimpleNamespace(repo=_FakeFactorRepo())
    out = call_tool(
        "compare_factors",
        state,
        {
            "factor_ids": [any_alpha],
            "symbols": ["A", "B", "C"],
            "start": "2024-01-01",
            "end": "2024-01-10",
        },
    )

    assert len(out["factors"]) == 1
    assert out["factors"][0]["factor_id"] == any_alpha


def test_compose_factor_score_requires_pool():
    state = SimpleNamespace(repo=_FakeFactorRepo())
    with pytest.raises(ValueError, match="pool"):
        call_tool("compose_factor_score", state, {"factor_ids": ["momentum_20d"], "pool": []})


def test_compose_factor_score_rejects_pool_over_300():
    state = SimpleNamespace(repo=_FakeFactorRepo())
    with pytest.raises(ValueError, match="pool"):
        call_tool(
            "compose_factor_score",
            state,
            {
                "factor_ids": ["momentum_20d"],
                "pool": [f"{i:06d}.SZ" for i in range(301)],
            },
        )


def test_compose_factor_score_rejects_unknown_factor():
    state = SimpleNamespace(repo=_FakeFactorRepo())
    with pytest.raises(ValueError, match="unknown factor"):
        call_tool("compose_factor_score", state, {"factor_ids": ["not_a_real_factor"], "pool": ["A"]})


def test_compose_factor_score_rejects_non_list_pool():
    state = SimpleNamespace(repo=_FakeFactorRepo())
    with pytest.raises(ValueError, match="pool"):
        call_tool("compose_factor_score", state, {"factor_ids": ["momentum_20d"], "pool": "000001.SZ"})


def test_compose_factor_score_all_factors_excluded_returns_error(monkeypatch):
    from app.backtest import engine as engine_mod

    monkeypatch.setattr(engine_mod.BacktestEngine, "load_panel", lambda self, *a, **kw: pl.DataFrame())

    state = SimpleNamespace(repo=_FakeFactorRepo())
    out = call_tool("compose_factor_score", state, {"factor_ids": ["momentum_20d"], "pool": ["A", "B"]})

    assert out["error"] == "所有因子均无法计算，无法合成"
    assert out["meta"]["excluded_factors"][0]["factor_id"] == "momentum_20d"


def _combined_factor_panel(symbols, n_days, factor_names):
    import numpy as np
    from datetime import date as _date, timedelta as _timedelta

    rng = np.random.default_rng(11)
    rows = []
    for i, sym in enumerate(symbols):
        price = 10.0 + i
        for d in range(n_days):
            price *= 1 + rng.normal(0, 0.01)
            row = {"symbol": sym, "date": _date(2024, 1, 1) + _timedelta(days=d), "close": round(price, 4)}
            for j, fname in enumerate(factor_names):
                row[fname] = float(i) + d * 0.01 + j * 100.0
            rows.append(row)
    return pl.DataFrame(rows)


def test_compose_factor_score_ranks_by_composite(monkeypatch):
    from app.backtest import engine as engine_mod

    symbols = ["A", "B", "C"]
    panel = _combined_factor_panel(symbols, 15, ["momentum_20d", "rsi_14"])
    monkeypatch.setattr(engine_mod.BacktestEngine, "load_panel", lambda self, *a, **kw: panel)

    state = SimpleNamespace(repo=_FakeFactorRepo())
    out = call_tool(
        "compose_factor_score",
        state,
        {
            "factor_ids": ["momentum_20d", "rsi_14"],
            "pool": symbols,
            "as_of": "2024-01-15",
            "lookback_days": 14,
            "top_n": 3,
        },
    )

    assert "error" not in out or out.get("error") is None
    assert len(out["ranked"]) == 3
    assert set(out["meta"]["used_factors"]) <= {"momentum_20d", "rsi_14"}
    scores = [r["composite_score"] for r in out["ranked"]]
    assert scores == sorted(scores, reverse=True)


def test_compose_factor_score_excludes_symbols_with_partial_factor_coverage(monkeypatch):
    from app.backtest import engine as engine_mod

    symbols = ["A", "B", "C"]
    panel = _combined_factor_panel(symbols, 15, ["momentum_20d", "rsi_14"])
    last_date = panel["date"].max()
    panel = panel.with_columns(
        pl.when((pl.col("symbol") == "B") & (pl.col("date") == last_date))
        .then(pl.lit(None, dtype=pl.Float64))
        .otherwise(pl.col("rsi_14"))
        .alias("rsi_14")
    )
    monkeypatch.setattr(engine_mod.BacktestEngine, "load_panel", lambda self, *a, **kw: panel)

    state = SimpleNamespace(repo=_FakeFactorRepo())
    out = call_tool(
        "compose_factor_score",
        state,
        {
            "factor_ids": ["momentum_20d", "rsi_14"],
            "pool": symbols,
            "as_of": "2024-01-15",
            "lookback_days": 14,
        },
    )

    assert "error" not in out or out.get("error") is None
    ranked_symbols = {r["symbol"] for r in out["ranked"]}
    assert ranked_symbols == {"A", "C"}
    assert "B" in out["meta"]["uncovered_symbols"]


def test_compose_factor_score_as_of_falls_back_to_latest_trading_day(monkeypatch):
    from app.backtest import engine as engine_mod

    symbols = ["A", "B", "C"]
    panel = _combined_factor_panel(symbols, 15, ["momentum_20d"])
    monkeypatch.setattr(engine_mod.BacktestEngine, "load_panel", lambda self, *a, **kw: panel)

    state = SimpleNamespace(repo=_FakeFactorRepo())
    out = call_tool(
        "compose_factor_score",
        state,
        {
            "factor_ids": ["momentum_20d"],
            "pool": symbols,
            "as_of": "2024-01-20",
            "lookback_days": 25,
        },
    )

    assert "error" not in out or out.get("error") is None
    assert out["meta"]["scored_date"] == "2024-01-15"
    assert out["meta"]["as_of"] == "2024-01-20"


def test_compose_factor_score_top_n_clamped_to_pool_size(monkeypatch):
    from app.backtest import engine as engine_mod

    symbols = ["A", "B", "C"]
    panel = _combined_factor_panel(symbols, 15, ["momentum_20d"])
    monkeypatch.setattr(engine_mod.BacktestEngine, "load_panel", lambda self, *a, **kw: panel)

    state = SimpleNamespace(repo=_FakeFactorRepo())
    out = call_tool(
        "compose_factor_score",
        state,
        {
            "factor_ids": ["momentum_20d"],
            "pool": symbols,
            "as_of": "2024-01-15",
            "lookback_days": 14,
            "top_n": 99999,
        },
    )

    assert "error" not in out or out.get("error") is None
    assert len(out["ranked"]) == len(symbols)
