from app.strategy.monitor import MonitorRuleEngine
from app.strategy import monitor_rules


def test_history_loader_selection_by_asset_type():
    eng = MonitorRuleEngine()

    def stock_loader(d, l):
        return "STOCK"

    def etf_loader(d, l):
        return "ETF"

    eng.set_history_loader(stock_loader)
    eng.set_history_loader_etf(etf_loader)

    assert eng._history_loader_for({"asset_type": "etf"}) is etf_loader
    assert eng._history_loader_for({"asset_type": "stock"}) is stock_loader
    # 未标注 asset_type 的旧规则默认走股票加载器
    assert eng._history_loader_for({}) is stock_loader


def test_etf_loader_defaults_none():
    eng = MonitorRuleEngine()
    assert eng._history_loader_for({"asset_type": "etf"}) is None


def test_rule_model_defaults_stock():
    from app.api.monitor_rules import RuleModel

    r = RuleModel(id="x", name="n", type="price")
    assert r.asset_type == "stock"


def test_normalize_preserves_and_defaults_asset_type():
    assert monitor_rules.normalize({"id": "a", "type": "price"})["asset_type"] == "stock"
    assert monitor_rules.normalize({"id": "a", "type": "signal", "asset_type": "etf"})["asset_type"] == "etf"


def _signal_rule(rid, asset_type, sym):
    return {
        "id": rid, "name": rid, "type": "signal", "asset_type": asset_type,
        "scope": "symbols", "symbols": [sym], "logic": "and",
        "conditions": [{"field": "rsi_14", "op": "<", "value": 100}],
        "cooldown_seconds": 0, "enabled": True,
    }


def _etf_df():
    import polars as pl
    return pl.DataFrame({
        "symbol": ["510300"],
        "close": [4.0],
        "change_pct": [0.01],
        "rsi_14": [40.0],
    })


def test_evaluate_asset_type_filters_rules():
    """evaluate(asset_type=etf) 只评估 ETF 规则; 股票规则被过滤。"""
    eng = MonitorRuleEngine()
    eng.set_rules([_signal_rule("r_etf", "etf", "510300"),
                   _signal_rule("r_stock", "stock", "510300")])
    df = _etf_df()

    etf_events = eng.evaluate(df, asset_type="etf")
    assert any(e["rule_id"] == "r_etf" for e in etf_events)
    assert all(e["rule_id"] != "r_stock" for e in etf_events)

    stock_events = eng.evaluate(df, asset_type="stock", reset_strategy_results=False)
    assert all(e["rule_id"] != "r_etf" for e in stock_events)


def test_has_asset_rules():
    eng = MonitorRuleEngine()
    eng.set_rules([_signal_rule("r_etf", "etf", "510300")])
    assert eng.has_asset_rules("etf") is True
    assert eng.has_asset_rules("stock") is False


def test_evaluate_default_asset_type_is_stock():
    """不传 asset_type 时默认只评估股票规则 (向后兼容旧调用)。"""
    eng = MonitorRuleEngine()
    eng.set_rules([_signal_rule("r_etf", "etf", "510300")])
    # 默认 asset_type=stock → ETF 规则不评估
    assert eng.evaluate(_etf_df()) == []
