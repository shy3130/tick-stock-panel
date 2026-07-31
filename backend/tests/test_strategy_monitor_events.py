from __future__ import annotations

from datetime import date
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np
import polars as pl
import pytest

from app.backtest.matrix import build_market_data_matrix, make_signal_matrix
from app.services import alert_store, preferences, quote_service
from app.services.quote_service import QuoteService
from app.strategy import monitor_rules
from app.strategy.engine import StrategyDataContext, StrategyDef, StrategyEngine, StrategyResult
from app.strategy.monitor import MonitorRuleEngine


def _rule(*events: str, **overrides) -> dict:
    return monitor_rules.normalize({
        "id": "strategy_rule",
        "name": "策略监控",
        "type": "strategy",
        "asset_type": "stock",
        "scope": "all",
        "symbols": [],
        "strategy_id": "demo",
        "notify_events": list(events),
        "conditions": [],
        "cooldown_seconds": 3600,
        **overrides,
    })


class _SequenceStrategyEngine:
    def __init__(self, results: list[StrategyResult]):
        self.results = list(results)
        self.strategy = SimpleNamespace(
            meta={"id": "demo", "name": "示例策略"},
            execution_backend="polars_expr",
            filter_history_fn=None,
        )

    def get(self, strategy_id: str):
        assert strategy_id == "demo"
        return self.strategy

    def run(self, strategy_id: str, context, **kwargs):
        assert strategy_id == "demo"
        return self.results.pop(0)


def _result(
    as_of: date,
    pool: tuple[str, ...] = (),
    buys: tuple[str, ...] = (),
    sells: tuple[str, ...] = (),
) -> StrategyResult:
    return StrategyResult(
        as_of=as_of,
        strategy_id="demo",
        rows=[{"symbol": symbol, "close": 10.0, "change_pct": 0.01} for symbol in pool],
        total=len(pool),
        entry_signal_hits=[{"symbol": symbol, "signals": ["signal_buy"]} for symbol in buys],
        exit_signal_hits=[{"symbol": symbol, "signals": ["signal_sell"]} for symbol in sells],
    )


def _quotes() -> pl.DataFrame:
    return pl.DataFrame({
        "symbol": ["A", "B"],
        "close": [10.0, 20.0],
        "change_pct": [0.01, -0.02],
    })


def test_strategy_rule_compatibility_and_validation(tmp_path):
    legacy = {
        "id": "legacy", "name": "旧规则", "type": "strategy",
        "scope": "all", "strategy_id": "demo",
    }
    monitor_rules.save_one(tmp_path, legacy)

    loaded = monitor_rules.load_one(tmp_path, "legacy")
    assert loaded is not None
    assert loaded["notify_events"] == ["pool_entry", "pool_exit"]
    assert monitor_rules.load_all(tmp_path)[0]["notify_events"] == ["pool_entry", "pool_exit"]

    with pytest.raises(ValueError, match="至少选择一个通知事件"):
        monitor_rules.validate(_rule())
    with pytest.raises(ValueError, match="非法事件"):
        monitor_rules.validate(_rule("unknown"))
    monitor_rules.validate(_rule("buy_signal", "pool_exit"))


def test_strategy_events_baseline_dedupe_and_next_day_replay():
    day1 = date(2026, 7, 24)
    day2 = date(2026, 7, 25)
    engine = MonitorRuleEngine()
    engine.set_strategy_engine(_SequenceStrategyEngine([
        _result(day1, pool=("A",), buys=("A",)),
        _result(day1, pool=("A", "B"), buys=("A", "B")),
        _result(day1, pool=("A", "B")),
        _result(day1, pool=("A", "B"), buys=("B",)),
        _result(day2, pool=("A", "B"), buys=("B",)),
    ]))
    engine.set_rules([_rule("buy_signal", "pool_entry")])

    with patch("app.strategy.monitor.time.time", side_effect=[100, 101, 102, 103, 4000]):
        assert engine.evaluate(_quotes()) == []
        events = engine.evaluate(_quotes())
        assert {(event["type"], event["symbol"]) for event in events} == {
            ("buy_signal", "B"),
            ("pool_entry", "B"),
        }
        assert all(event["strategy_id"] == "demo" for event in events)
        assert engine.evaluate(_quotes()) == []
        assert engine.evaluate(_quotes()) == []
        next_day = engine.evaluate(_quotes())
    assert [(event["type"], event["symbol"]) for event in next_day] == [("buy_signal", "B")]


def test_strategy_sell_and_pool_exit_are_independent_events():
    day = date(2026, 7, 24)
    engine = MonitorRuleEngine()
    engine.set_strategy_engine(_SequenceStrategyEngine([
        _result(day, pool=("A", "B")),
        _result(day, pool=("A",), sells=("B",)),
    ]))
    engine.set_rules([_rule("sell_signal", "pool_exit")])

    with patch("app.strategy.monitor.time.time", side_effect=[100, 101]):
        assert engine.evaluate(_quotes()) == []
        events = engine.evaluate(_quotes())

    assert {(event["type"], event["symbol"]) for event in events} == {
        ("sell_signal", "B"),
        ("pool_exit", "B"),
    }


def test_strategy_rule_reload_preserves_state_and_semantic_edit_resets_it():
    day = date(2026, 7, 24)
    engine = MonitorRuleEngine()
    engine.set_strategy_engine(_SequenceStrategyEngine([
        _result(day, buys=("A",)),
        _result(day, buys=("A",)),
        _result(day, buys=("A",)),
        _result(day, buys=("A",)),
    ]))
    rule = _rule("buy_signal")
    engine.set_rules([rule])
    assert engine.evaluate(_quotes()) == []

    engine.set_rules([{**rule, "message": "新文案"}])
    assert engine.evaluate(_quotes()) == []

    engine.set_rules([{**rule, "scope": "symbols", "symbols": ["A"]}])
    assert engine.evaluate(_quotes()) == []

    engine.set_rules([])
    assert not engine._strategy_signal_state
    engine.set_rules([rule])
    assert engine.evaluate(_quotes()) == []


def test_matrix_signal_hits_map_codes_and_keep_unlabelled_hits():
    mapped = StrategyEngine._matrix_signal_hits(
        np.array([1, 1, 0], dtype=np.uint8),
        np.array([1, -1, -1], dtype=np.int16),
        ("signal_a", "signal_b"),
        ("A", "B", "C"),
    )
    assert mapped == [
        {"symbol": "A", "signals": ["signal_b"]},
        {"symbol": "B", "signals": []},
    ]


def test_matrix_strategy_pool_masks_rows_and_both_signal_directions():
    day = date(2026, 7, 24)
    panel = pl.DataFrame({
        "symbol": ["A", "B"],
        "date": [day, day],
        "open": [10.0, 20.0],
        "high": [10.0, 20.0],
        "low": [10.0, 20.0],
        "close": [10.0, 20.0],
        "volume": [100.0, 100.0],
        "amount": [1000.0, 2000.0],
    })
    calls = 0

    class _AllSignals:
        def required_fields(self):
            return frozenset({"close"})

        def required_warmup_bars(self, params):
            return 1

        def compute_signals(self, market, params):
            nonlocal calls
            calls += 1
            active = np.ones(market.shape, dtype=np.uint8)
            codes = np.zeros(market.shape, dtype=np.int16)
            return make_signal_matrix(
                market.shape,
                entry=active,
                exit=active,
                entry_signal_code=codes,
                exit_signal_code=codes,
                entry_signal_ids=("signal_buy",),
                exit_signal_ids=("signal_sell",),
            )

    strategy = StrategyDef(
        meta={"id": "matrix", "scoring": {}, "limit": 100},
        basic_filter={"enabled": False},
        entry_signals=[],
        exit_signals=[],
        stop_loss=None,
        trailing_stop=None,
        trailing_take_profit_activate=None,
        trailing_take_profit_drawdown=None,
        max_hold_days=None,
        alerts=[],
        filter_fn=None,
        filter_history_fn=None,
        lookback_days=1,
        source="custom",
        execution_backend="matrix_native",
        matrix_strategy=_AllSignals(),
    )
    engine = StrategyEngine(strategy_dirs=[])
    engine._strategies["matrix"] = strategy
    result = engine.run(
        "matrix",
        StrategyDataContext(
            "stock",
            "1d",
            day,
            current=panel,
            market=build_market_data_matrix(panel),
        ),
        pool=["A"],
    )

    assert calls == 1
    assert [row["symbol"] for row in result.rows] == ["A"]
    assert result.entry_signal_hits == [{"symbol": "A", "signals": ["signal_buy"]}]
    assert result.exit_signal_hits == [{"symbol": "A", "signals": ["signal_sell"]}]


def test_ordinary_strategy_uses_signal_overrides_and_ignores_malformed_values():
    day = date(2026, 7, 24)
    quotes = pl.DataFrame({
        "symbol": ["A", "B"],
        "signal_default_buy": [True, False],
        "signal_override_buy": [False, True],
        "signal_default_sell": [False, True],
        "signal_override_sell": [True, False],
    })
    strategy = StrategyDef(
        meta={"id": "ordinary", "scoring": {}, "limit": 100},
        basic_filter={"enabled": False},
        entry_signals=["signal_default_buy"],
        exit_signals=["signal_default_sell"],
        stop_loss=None,
        trailing_stop=None,
        trailing_take_profit_activate=None,
        trailing_take_profit_drawdown=None,
        max_hold_days=None,
        alerts=[],
        filter_fn=None,
        filter_history_fn=None,
        lookback_days=1,
        source="custom",
    )
    engine = StrategyEngine(strategy_dirs=[])
    engine._strategies["ordinary"] = strategy
    context = StrategyDataContext("stock", "1d", day, current=quotes)

    result = engine.run(
        "ordinary",
        context,
        overrides={
            "entry_signals": ["signal_override_buy"],
            "exit_signals": ["signal_override_sell"],
        },
    )
    assert result.entry_signal_hits == [{"symbol": "B", "signals": ["signal_override_buy"]}]
    assert result.exit_signal_hits == [{"symbol": "A", "signals": ["signal_override_sell"]}]

    fallback = engine.run(
        "ordinary",
        context,
        overrides={"entry_signals": None, "exit_signals": "signal_override_sell"},
    )
    assert fallback.entry_signal_hits == [{"symbol": "A", "signals": ["signal_default_buy"]}]
    assert fallback.exit_signal_hits == [{"symbol": "B", "signals": ["signal_default_sell"]}]


def test_quote_service_forwards_real_strategy_id(monkeypatch, tmp_path):
    event = {
        "ts": 1,
        "rule_id": "strategy_rule",
        "strategy_id": "demo",
        "source": "strategy",
        "type": "buy_signal",
        "symbol": "A",
        "name": "测试股票",
        "message": "策略买入信号",
        "price": 10.0,
        "change_pct": 0.01,
        "signals": ["signal_buy"],
        "severity": "info",
    }

    class _Engine:
        rule_count = 1

        def __init__(self):
            self.rules = {"strategy_rule": {"webhook_channels": []}}

        def set_name_map(self, name_map):
            pass

        def has_rule_type(self, rtype: str) -> bool:
            return False

        def has_asset_rules(self, asset_type: str) -> bool:
            return False

        def evaluate(self, df, asset_type: str):
            return [event]

        def consume_strategy_result_updates(self) -> bool:
            return False

    class _Repo:
        store = SimpleNamespace(data_dir=tmp_path)

        @staticmethod
        def get_instruments():
            return pl.DataFrame({"symbol": ["A"], "name": ["测试股票"]})

    monkeypatch.setattr(alert_store, "append_many", lambda *args: None)
    monkeypatch.setattr(preferences, "get_system_notify_enabled", lambda: False)
    service = QuoteService()
    subscriber = service.subscribe()
    service.set_app_state(SimpleNamespace(monitor_engine=_Engine(), repo=_Repo()))
    service._repo = _Repo()
    service.get_enriched_today = lambda: (_quotes(), quote_service.cn_today())

    with patch.object(QuoteService, "_is_continuous_trading", return_value=True):
        service._evaluate_monitors(pl.DataFrame(), None)

    assert subscriber.pop()["alerts"][0]["strategy_id"] == "demo"
