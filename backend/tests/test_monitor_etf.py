from app.strategy import monitor_rules
from app.strategy.monitor import MonitorRuleEngine


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


# ---- 资产类型纠正: 误存为 stock 的 ETF 规则 ----

class _FakeRepo:
    def resolve_asset_type(self, symbol):
        return {
            "510300.SH": "etf",
            "159915.SZ": "etf",
            "000001.SH": "index",
        }.get(symbol, "stock")


def test_reconcile_etf_asset_type_corrects_etf_only_rule():
    """自选/点位提醒入口未传 asset_type 时, 纯 ETF 规则必须纠正到 etf 轮。

    未修复: 510300.SH 规则留在 stock, 股票快照不含 ETF, 点位/信号永不命中。
    """
    from app.api.monitor_rules import _reconcile_index_asset_type

    rule = {"asset_type": "stock", "scope": "symbols", "symbols": ["510300.SH"]}
    assert _reconcile_index_asset_type(rule, _FakeRepo())["asset_type"] == "etf"


def test_reconcile_etf_asset_type_keeps_stock_and_mixed():
    from app.api.monitor_rules import _reconcile_index_asset_type

    repo = _FakeRepo()
    assert _reconcile_index_asset_type(
        {"asset_type": "stock", "scope": "symbols", "symbols": ["600000.SH"]}, repo,
    )["asset_type"] == "stock"
    assert _reconcile_index_asset_type(
        {"asset_type": "stock", "scope": "symbols", "symbols": ["510300.SH", "600000.SH"]}, repo,
    )["asset_type"] == "stock"
    assert _reconcile_index_asset_type(
        {"asset_type": "etf", "scope": "symbols", "symbols": ["510300.SH"]}, repo,
    )["asset_type"] == "etf"
    assert _reconcile_index_asset_type(
        {"asset_type": "stock", "scope": "all", "symbols": ["510300.SH"]}, repo,
    )["asset_type"] == "stock"
    assert _reconcile_index_asset_type(
        {"asset_type": "stock", "scope": "symbols", "symbols": []}, repo,
    )["asset_type"] == "stock"


def test_reconcile_asset_type_resolve_error_keeps_stock():
    from app.api.monitor_rules import _reconcile_index_asset_type

    class _Boom:
        def resolve_asset_type(self, symbol):
            raise RuntimeError("维表不可用")

    rule = {"asset_type": "stock", "scope": "symbols", "symbols": ["510300.SH"]}
    assert _reconcile_index_asset_type(rule, _Boom())["asset_type"] == "stock"


def test_api_save_persists_reconciled_etf_asset_type(tmp_path):
    """点位提醒 POST 默认 asset_type=stock: 保存后磁盘与返回值都是 etf。"""
    from types import SimpleNamespace
    from unittest.mock import MagicMock

    from app.api import monitor_rules as monitor_rules_api

    repo = MagicMock()
    repo.store.data_dir = tmp_path
    repo.resolve_asset_type.side_effect = lambda s: "etf" if s == "510300.SH" else "stock"
    req = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(
        repo=repo, monitor_engine=None, capabilities=None,
    )))
    model = monitor_rules_api.RuleModel(
        id="etf_px", name="ETF 点位", type="price", asset_type="stock",
        scope="symbols", symbols=["510300.SH"],
        conditions=[{"field": "close", "op": ">=", "value": 4.0}],
    )
    resp = monitor_rules_api.save_rule(model, req)
    assert resp["rule"]["asset_type"] == "etf"
    saved = monitor_rules.load_one(tmp_path, "etf_px")
    assert saved["asset_type"] == "etf"
