"""指数监控规则校验测试。"""
import pytest

from app.strategy import monitor_rules


def _index_rule(rid="r_idx", **over):
    rule = {
        "id": rid, "name": rid, "type": "signal", "asset_type": "index",
        "scope": "symbols", "symbols": ["000001.SH"], "logic": "and",
        "conditions": [{"field": "rsi_14", "op": "<", "value": 30}],
        "cooldown_seconds": 0, "enabled": True,
    }
    rule.update(over)
    return rule


def test_index_signal_price_allowed():
    monitor_rules.validate(_index_rule())
    monitor_rules.validate(_index_rule(type="price"))


def test_index_strategy_rejected():
    with pytest.raises(ValueError, match="指数"):
        monitor_rules.validate(_index_rule(type="strategy", strategy_id="s1"))


def test_index_market_rejected():
    with pytest.raises(ValueError, match="指数"):
        monitor_rules.validate(_index_rule(type="market"))


def test_index_scope_all_rejected():
    with pytest.raises(ValueError, match="指数"):
        monitor_rules.validate(_index_rule(scope="all", symbols=[]))


def test_index_intraday_signal_rejected():
    with pytest.raises(ValueError, match="分时"):
        monitor_rules.validate(_index_rule(
            conditions=[{"field": "signal_intraday_avg_cross_up", "op": "truth"}],
        ))


# ---- Task 7: B5 监控指数评估轮 ----

def _signal_rule(rid, asset_type, sym):
    return {
        "id": rid, "name": rid, "type": "signal", "asset_type": asset_type,
        "scope": "symbols", "symbols": [sym], "logic": "and",
        "conditions": [{"field": "rsi_14", "op": "<", "value": 100}],
        "cooldown_seconds": 0, "enabled": True,
    }


def test_evaluate_index_round_triggers_and_isolates():
    """指数轮只评估指数规则, 且不触碰策略结果缓存。"""
    import polars as pl
    from app.strategy.monitor import MonitorRuleEngine

    eng = MonitorRuleEngine()
    eng.set_rules([_signal_rule("r_idx", "index", "000001.SH"),
                   _signal_rule("r_stock", "stock", "000001.SH")])
    eng.set_name_map({"000001.SH": "上证指数"})
    df = pl.DataFrame({"symbol": ["000001.SH"], "close": [3000.0],
                       "change_pct": [0.01], "rsi_14": [40.0]})

    events = eng.evaluate(df, asset_type="index", reset_strategy_results=False)
    assert any(e["rule_id"] == "r_idx" for e in events)
    assert all(e["rule_id"] != "r_stock" for e in events)
    assert events[0]["name"] == "上证指数"
    assert eng.latest_strategy_results() == {}  # 策略结果缓存未被触碰


# ---- 资产类型纠正: 误存为 stock 的指数规则 ----

class _FakeRepo:
    def resolve_asset_type(self, symbol):
        return {"000001.SH": "index"}.get(symbol, "stock")


def test_reconcile_index_asset_type_corrects_index_only_rule():
    from app.api.monitor_rules import _reconcile_index_asset_type

    rule = {"asset_type": "stock", "scope": "symbols", "symbols": ["000001.SH"]}
    assert _reconcile_index_asset_type(rule, _FakeRepo())["asset_type"] == "index"


def test_reconcile_index_asset_type_keeps_stock_and_mixed():
    from app.api.monitor_rules import _reconcile_index_asset_type

    repo = _FakeRepo()
    # 纯股票 → 不动
    assert _reconcile_index_asset_type(
        {"asset_type": "stock", "scope": "symbols", "symbols": ["600000.SH"]}, repo,
    )["asset_type"] == "stock"
    # 股票+指数混合 → 不动 (asset_type 语义覆盖整条规则)
    assert _reconcile_index_asset_type(
        {"asset_type": "stock", "scope": "symbols", "symbols": ["000001.SH", "600000.SH"]}, repo,
    )["asset_type"] == "stock"
    # 已是 index → 不动
    assert _reconcile_index_asset_type(
        {"asset_type": "index", "scope": "symbols", "symbols": ["000001.SH"]}, repo,
    )["asset_type"] == "index"
    # 非 symbols 范围 → 不动
    assert _reconcile_index_asset_type(
        {"asset_type": "stock", "scope": "all", "symbols": []}, repo,
    )["asset_type"] == "stock"


# ---- 股票快照为空时指数轮仍独立评估 (PR #46 问题 2) ----

def test_evaluate_monitors_index_round_survives_empty_stock_snapshot():
    """纯指数行情/自选场景: 股票 enriched 为空时, 指数监控轮仍独立评估。"""
    from datetime import date
    from unittest.mock import MagicMock, patch

    import polars as pl

    from app.services.quote_service import QuoteService

    svc = QuoteService.__new__(QuoteService)
    svc._repo = MagicMock()

    engine = MagicMock()
    engine.rule_count = 1
    engine.has_asset_rules.side_effect = lambda at: at == "index"
    engine.has_rule_type.return_value = False
    engine.evaluate.return_value = []  # 无触发, 简化后续

    svc._app_state = MagicMock()
    svc._app_state.monitor_engine = engine
    svc._app_state.repo = svc._repo

    svc._repo.get_instruments.return_value = pl.DataFrame()
    svc._repo.get_instruments_asset.return_value = pl.DataFrame()
    svc._repo.get_enriched_latest_asset.return_value = (
        pl.DataFrame({"symbol": ["000001.SH"], "close": [3000.0], "rsi_14": [40.0]}),
        date(2026, 7, 28),
    )

    with (
        patch.object(QuoteService, "_is_continuous_trading", return_value=True),
        patch.object(QuoteService, "get_enriched_today",
                     return_value=(pl.DataFrame(), None)),  # 股票快照为空
        patch.object(QuoteService, "_inject_intraday_signals",
                     side_effect=lambda df, e, at: df),
        patch("app.services.quote_service.cn_today", return_value=date(2026, 7, 28)),
    ):
        svc._evaluate_monitors(pl.DataFrame(), None)

    # 指数轮执行了 (asset_type="index")
    index_calls = [c for c in engine.evaluate.call_args_list
                   if c[1].get("asset_type") == "index"]
    assert len(index_calls) == 1, "股票快照为空时指数轮仍应评估"
    # 股票轮被跳过 (stock_ready=False)
    stock_calls = [c for c in engine.evaluate.call_args_list
                   if c[1].get("asset_type") == "stock"]
    assert len(stock_calls) == 0, "股票快照为空时股票轮应跳过"


def test_evaluate_monitors_stock_round_runs_when_snapshot_ready():
    """股票快照就绪时, 股票轮正常执行 (回归确认未破坏原有行为)。"""
    from datetime import date
    from unittest.mock import MagicMock, patch

    import polars as pl

    from app.services.quote_service import QuoteService

    svc = QuoteService.__new__(QuoteService)
    svc._repo = MagicMock()

    engine = MagicMock()
    engine.rule_count = 1
    engine.has_asset_rules.return_value = False
    engine.has_rule_type.return_value = False
    engine.evaluate.return_value = []
    engine.consume_strategy_result_updates.return_value = False

    svc._app_state = MagicMock()
    svc._app_state.monitor_engine = engine
    svc._app_state.repo = svc._repo

    svc._repo.get_instruments.return_value = pl.DataFrame()

    stock_df = pl.DataFrame({"symbol": ["600000.SH"], "close": [10.0], "rsi_14": [50.0]})

    with (
        patch.object(QuoteService, "_is_continuous_trading", return_value=True),
        patch.object(QuoteService, "get_enriched_today",
                     return_value=(stock_df, date(2026, 7, 28))),
        patch.object(QuoteService, "_inject_intraday_signals",
                     side_effect=lambda df, e, at: df),
        patch("app.services.quote_service.cn_today", return_value=date(2026, 7, 28)),
    ):
        svc._evaluate_monitors(pl.DataFrame(), None)

    # 股票轮正常执行
    stock_calls = [c for c in engine.evaluate.call_args_list
                   if c[1].get("asset_type") == "stock"]
    assert len(stock_calls) == 1, "股票快照就绪时股票轮应正常执行"
