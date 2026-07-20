from __future__ import annotations

from datetime import datetime

import polars as pl
import pytest

from app.market_time import CN_TZ
from app.services.kline_sync import fetch_intraday_monitor_batch, intraday_monitor_support
from app.strategy import monitor_rules
from app.strategy.intraday_signals import IntradaySignalEvaluator
from app.strategy.monitor import MonitorRuleEngine
from app.tickflow.capabilities import Cap, CapabilityLimits, CapabilitySet


def _minute_rows(prices: list[float]) -> pl.DataFrame:
    return pl.DataFrame({
        "symbol": ["600000.SH"] * len(prices),
        "datetime": [datetime(2026, 7, 17, 9, 30 + i) for i in range(len(prices))],
        "close": prices,
        "volume": [1.0] * len(prices),
        "amount": [price * 100.0 for price in prices],
    })


def test_intraday_crosses_are_edge_triggered_and_not_replayed():
    evaluator = IntradaySignalEvaluator()
    kwargs = {
        "symbols": {"600000.SH"},
        "prev_close": {"600000.SH": 10.0},
        "asset_type": "stock",
    }

    # 首次只建立基线, 不补发当前已有的穿越。
    assert evaluator.evaluate(_minute_rows([9.0]), now=datetime(2026, 7, 17, 9, 32), **kwargs) == []

    up = evaluator.evaluate(_minute_rows([9.0, 11.0]), now=datetime(2026, 7, 17, 9, 33), **kwargs)
    assert len(up) == 1
    assert up[0]["signal_intraday_avg_cross_up"] is True
    assert up[0]["signal_intraday_zero_cross_up"] is True

    # 同一根已完成分钟线不得重复触发。
    assert evaluator.evaluate(_minute_rows([9.0, 11.0]), now=datetime(2026, 7, 17, 9, 33, 30), **kwargs) == []

    down = evaluator.evaluate(_minute_rows([9.0, 11.0, 9.0]), now=datetime(2026, 7, 17, 9, 34), **kwargs)
    assert len(down) == 1
    assert down[0]["signal_intraday_avg_cross_down"] is True
    assert down[0]["signal_intraday_zero_cross_down"] is True


def test_intraday_signals_flow_through_monitor_engine():
    evaluator = IntradaySignalEvaluator()
    kwargs = {
        "symbols": {"600000.SH"},
        "prev_close": {"600000.SH": 10.0},
        "asset_type": "stock",
    }
    evaluator.evaluate(_minute_rows([9.0]), now=datetime(2026, 7, 17, 9, 32), **kwargs)
    signals = evaluator.evaluate(_minute_rows([9.0, 11.0]), now=datetime(2026, 7, 17, 9, 33), **kwargs)
    enriched = pl.DataFrame({
        "symbol": ["600000.SH"], "close": [11.0], "change_pct": [0.1],
    })
    engine = MonitorRuleEngine()
    engine.set_rules([{**_intraday_rule(), "cooldown_seconds": 0}])
    events = engine.evaluate(evaluator.inject(enriched, signals))
    assert len(events) == 1
    assert events[0]["rule_id"] == "intraday_rule"
    assert events[0]["signals"] == ["signal_intraday_avg_cross_up"]


def test_intraday_signal_state_resets_between_trading_days():
    evaluator = IntradaySignalEvaluator()
    evaluator.evaluate(
        _minute_rows([9.0]), symbols={"600000.SH"},
        prev_close={"600000.SH": 10.0}, asset_type="stock",
        now=datetime(2026, 7, 17, 9, 32),
    )
    next_day = pl.DataFrame({
        "symbol": ["600000.SH"],
        "datetime": [datetime(2026, 7, 18, 9, 30)],
        "close": [11.0], "volume": [1.0], "amount": [1100.0],
    })
    assert evaluator.evaluate(
        next_day, symbols={"600000.SH"},
        prev_close={"600000.SH": 10.0}, asset_type="stock",
        now=datetime(2026, 7, 18, 9, 32),
    ) == []


def test_intraday_average_does_not_accumulate_previous_day_bars():
    evaluator = IntradaySignalEvaluator()
    previous_day = pl.DataFrame({
        "symbol": ["600000.SH"],
        "datetime": [datetime(2026, 7, 16, 15, 0)],
        "close": [100.0], "volume": [1000.0], "amount": [10_000_000.0],
    })
    first = pl.concat([previous_day, _minute_rows([9.0])])
    evaluator.evaluate(
        first, symbols={"600000.SH"}, prev_close={"600000.SH": 10.0},
        asset_type="stock", now=datetime(2026, 7, 17, 9, 32),
    )
    second = pl.concat([previous_day, _minute_rows([9.0, 11.0])])
    signals = evaluator.evaluate(
        second, symbols={"600000.SH"}, prev_close={"600000.SH": 10.0},
        asset_type="stock", now=datetime(2026, 7, 17, 9, 33),
    )
    assert signals[0]["signal_intraday_avg_cross_up"] is True


def test_intraday_cutoff_keeps_beijing_time_in_utc_runtime():
    evaluator = IntradaySignalEvaluator()
    kwargs = {
        "symbols": {"600000.SH"},
        "prev_close": {"600000.SH": 10.0},
        "asset_type": "stock",
    }
    assert evaluator.evaluate(
        _minute_rows([9.0]), symbols={"600000.SH"},
        prev_close={"600000.SH": 10.0}, asset_type="stock",
        now=datetime(2026, 7, 17, 9, 32, tzinfo=CN_TZ),
    ) == []
    signals = evaluator.evaluate(
        _minute_rows([9.0, 11.0]), now=datetime(2026, 7, 17, 9, 33, tzinfo=CN_TZ),
        **kwargs,
    )
    assert signals[0]["signal_intraday_zero_cross_up"] is True


def _intraday_rule(scope: str = "symbols") -> dict:
    return {
        "id": "intraday_rule", "name": "分时监控", "enabled": True,
        "type": "signal", "asset_type": "stock", "scope": scope,
        "symbols": ["600000.SH"], "logic": "and",
        "conditions": [{"field": "signal_intraday_avg_cross_up", "op": "truth"}],
    }


def test_intraday_rule_pool_is_derived_from_enabled_rules():
    engine = MonitorRuleEngine()
    disabled = {**_intraday_rule(), "id": "disabled", "enabled": False, "symbols": ["000001.SZ"]}
    engine.set_rules([_intraday_rule(), disabled])
    assert engine.intraday_signal_symbols("stock") == {"600000.SH"}
    assert engine.intraday_signal_symbols("etf") == set()


def test_intraday_rule_rejects_non_symbol_scope():
    with pytest.raises(ValueError, match="仅支持指定股票"):
        monitor_rules.validate(_intraday_rule("all"))


def test_intraday_support_uses_capability_limits(monkeypatch):
    monkeypatch.setattr("app.services.preferences.get_minute_data_provider", lambda: "tickflow")
    capset = CapabilitySet({Cap.KLINE_MINUTE_BATCH: CapabilityLimits(batch=25, rpm=30)})
    support = intraday_monitor_support(capset)
    assert support["available"] is True
    assert support["source"] == "minute_batch"
    assert support["max_symbols"] == 25

    denied = intraday_monitor_support(CapabilitySet())
    assert denied["available"] is False


def test_intraday_batch_provider_is_normalized_without_network(monkeypatch):
    monkeypatch.setattr("app.services.preferences.get_minute_data_provider", lambda: "tickflow")

    class FakeKlines:
        def intraday_batch(self, symbols, count, as_dataframe, show_progress, batch_size):
            assert symbols == ["600000.SH"]
            assert count == 300
            assert as_dataframe is True
            assert show_progress is False
            assert batch_size == 20
            return pl.DataFrame({
                "symbol": symbols,
                "datetime": [datetime(2026, 7, 17, 9, 30)],
                "open": [10.0], "high": [10.1], "low": [9.9], "close": [10.0],
                "volume": [1.0], "amount": [1000.0],
            })

    class FakeClient:
        klines = FakeKlines()

    monkeypatch.setattr("app.services.kline_sync.get_client", lambda: FakeClient())
    capset = CapabilitySet({Cap.INTRADAY_BATCH: CapabilityLimits(batch=20, rpm=30)})
    result = fetch_intraday_monitor_batch(
        ["600000.SH"], capset, now=datetime(2026, 7, 17, 10, 0, tzinfo=CN_TZ),
    )
    assert result.columns == ["symbol", "datetime", "open", "high", "low", "close", "volume", "amount"]
    assert result["symbol"].to_list() == ["600000.SH"]
