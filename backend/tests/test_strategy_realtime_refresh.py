"""策略页实时结果刷新 SSE 回归测试。"""
from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import polars as pl

from app.market_time import cn_today
from app.services import quote_service
from app.services.quote_service import QuoteService, QuoteSubscriber
from app.strategy.engine import StrategyEngine
from app.strategy.monitor import MonitorRuleEngine


def _strategy_rule(scope: str = "all") -> dict:
    return {
        "id": "strategy_rule",
        "name": "策略监控",
        "type": "strategy",
        "asset_type": "stock",
        "strategy_id": "strategy_1",
        "scope": scope,
        "symbols": ["600000.SH"],
        "cooldown_seconds": 0,
    }


def _quote_df() -> pl.DataFrame:
    return pl.DataFrame({
        "symbol": ["600000.SH"],
        "close": [10.0],
        "change_pct": [0.01],
    })


def test_strategy_result_subscriber_notification_is_coalesced():
    sub = QuoteSubscriber()

    sub.notify_strategy_results()
    sub.notify_strategy_results()

    assert sub.wait(timeout=0.01) is True
    data = sub.pop()
    assert data["strategy_results_updated"] is True
    assert data["quote_updated"] is False
    assert data["depth_updated"] is False
    assert sub.wait(timeout=0.01) is False


def test_strategy_result_notification_fans_out_to_all_subscribers():
    service = QuoteService()
    first = service.subscribe()
    second = service.subscribe()

    service.notify_strategy_results_updated()

    assert first.pop()["strategy_results_updated"] is True
    assert second.pop()["strategy_results_updated"] is True


class _EmptyResultStrategyEngine:
    def get(self, strategy_id: str):
        assert strategy_id == "strategy_1"
        return SimpleNamespace(filter_history_fn=None, execution_backend="polars_expr")

    def run(self, strategy_id: str, context, **kwargs):
        assert strategy_id == "strategy_1"
        assert context.current.height == 1
        return SimpleNamespace(total=0, rows=[])


class _FailingStrategyEngine(_EmptyResultStrategyEngine):
    def run(self, strategy_id: str, context, **kwargs):
        raise RuntimeError("strategy failed")


def test_successful_zero_match_strategy_marks_result_refresh():
    engine = MonitorRuleEngine()
    engine.set_strategy_engine(_EmptyResultStrategyEngine())
    engine.set_rules([_strategy_rule()])

    assert engine.evaluate(_quote_df()) == []
    assert engine.latest_strategy_results()["strategy_1"]["total"] == 0
    assert engine.consume_strategy_result_updates() is True
    assert engine.consume_strategy_result_updates() is False


def test_failed_or_skipped_strategy_does_not_mark_result_refresh():
    failed = MonitorRuleEngine()
    failed.set_strategy_engine(_FailingStrategyEngine())
    failed.set_rules([_strategy_rule()])

    assert failed.evaluate(_quote_df()) == []
    assert failed.latest_strategy_results() == {}
    assert failed.consume_strategy_result_updates() is False

    skipped = MonitorRuleEngine()
    skipped.set_strategy_engine(_EmptyResultStrategyEngine())
    skipped.set_rules([_strategy_rule(scope="symbols")])

    assert skipped.evaluate(pl.DataFrame({"symbol": ["000001.SZ"]})) == []
    assert skipped.latest_strategy_results() == {}
    assert skipped.consume_strategy_result_updates() is False


def test_matrix_strategy_monitor_reuses_live_matrix_and_updates_last_row():
    target = cn_today()
    start = target - timedelta(days=61)
    rows = []
    for offset in range(62):
        for symbol, base in (("000001.SZ", 10.0), ("600000.SH", 20.0)):
            close = base + offset * 0.05
            rows.append({
                "symbol": symbol,
                "name": symbol,
                "date": start + timedelta(days=offset),
                "open": close,
                "high": close * 1.01,
                "low": close * 0.99,
                "close": close,
                "volume": 1_000_000.0,
                "amount": 100_000_000.0,
                "total_shares": 1_000_000_000.0,
                "float_shares": 800_000_000.0,
            })
    panel = pl.DataFrame(rows)
    history = panel.filter(pl.col("date") < target)
    current = panel.filter(pl.col("date") == target)
    load_calls = []

    def load_history(as_of, lookback):
        load_calls.append((as_of, lookback))
        return history

    strategy_engine = StrategyEngine(
        strategy_dirs=[Path(__file__).resolve().parents[1] / "app" / "strategy" / "builtin"],
    )
    overrides = {
        "params": {"require_macd_golden": False, "use_volume_filter": False},
        "basic_filter": {"enabled": False},
    }
    monitor = MonitorRuleEngine()
    monitor.set_strategy_engine(strategy_engine)
    monitor.set_data_dir(Path("test-data"))
    monitor.set_history_loader(load_history)
    monitor.set_rules([{
        "id": "matrix_macd",
        "name": "MACD",
        "type": "strategy",
        "asset_type": "stock",
        "strategy_id": "macd_golden",
        "scope": "all",
        "symbols": [],
        "cooldown_seconds": 0,
    }])

    with patch("app.strategy.monitor._strategy_config.load_override", return_value=overrides):
        assert monitor.evaluate(current) == []
        assert monitor.latest_strategy_results()["macd_golden"]["total"] == 2
        first_stats = strategy_engine.realtime_matrix_stats("monitor:stock")
        assert first_stats["build_count"] == 1
        assert len(load_calls) == 1

        updated = current.with_columns((pl.col("close") + 1.0).alias("close"))
        assert monitor.evaluate(updated) == []
        second_stats = strategy_engine.realtime_matrix_stats("monitor:stock")
        assert second_stats["build_count"] == 1
        assert second_stats["update_count"] == 1
        assert len(load_calls) == 1


class _MonitorWithUpdate:
    rule_count = 1

    def __init__(self, updated: bool):
        self.updated = updated

    def set_name_map(self, name_map):
        pass

    def has_rule_type(self, rtype: str) -> bool:
        return False

    def has_asset_rules(self, asset_type: str) -> bool:
        return False

    def evaluate(self, df, asset_type: str):
        assert asset_type == "stock"
        return []

    def consume_strategy_result_updates(self) -> bool:
        return self.updated


def test_quote_service_notifies_only_after_strategy_result_update():
    service = QuoteService()
    subscriber = service.subscribe()
    service.set_app_state(SimpleNamespace(monitor_engine=_MonitorWithUpdate(updated=True)))
    service.get_enriched_today = lambda: (_quote_df(), quote_service.cn_today())

    with patch.object(QuoteService, "_is_continuous_trading", return_value=True):
        service._evaluate_monitors(pl.DataFrame(), None)

    assert subscriber.pop()["strategy_results_updated"] is True


def test_quote_service_skips_notification_without_strategy_result_update():
    service = QuoteService()
    subscriber = service.subscribe()
    service.set_app_state(SimpleNamespace(monitor_engine=_MonitorWithUpdate(updated=False)))
    service.get_enriched_today = lambda: (_quote_df(), quote_service.cn_today())

    with patch.object(QuoteService, "_is_continuous_trading", return_value=True):
        service._evaluate_monitors(pl.DataFrame(), None)

    assert subscriber.pop()["strategy_results_updated"] is False
