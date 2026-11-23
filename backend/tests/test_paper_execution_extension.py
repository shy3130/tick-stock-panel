"""扩展的信号/参数/订单契约，使用真实策略引擎与模拟仓账务。"""

from datetime import date, datetime
from types import SimpleNamespace

import polars as pl
import pytest

from app.custom.paper_execution.service import ExecutionService
from app.market_time import CN_TZ
from app.strategy import config, paper, paper_auto
from app.strategy.engine import StrategyDataContext, StrategyEngine

DAY = date(2026, 10, 26)
NOW = datetime(2026, 10, 26, 16, tzinfo=CN_TZ)


@pytest.fixture
def env(tmp_path, monkeypatch):
    directory = tmp_path / "strategies" / "custom"
    directory.mkdir(parents=True)
    path = directory / "demo.py"
    path.write_text("""import polars as pl
META = {"id": "demo", "name": "示例", "params": [{"id": "threshold", "type": "float", "default": 8., "min": 0., "max": 20.}], "scoring": {}, "limit": 1}
BASIC_FILTER = {"enabled": False}
ENTRY_SIGNALS = ["entry"]
EXIT_SIGNALS = ["exit"]
def filter(df, params):
    return pl.col("close") > params["threshold"]
""")
    engine = StrategyEngine([directory])
    frame = pl.DataFrame(
        {
            "symbol": [f"{i:06d}.SZ" for i in range(6)],
            "date": [DAY] * 6,
            "close": [10.0] * 6,
            "raw_close": [10.0] * 6,
            "signal_entry": [True] * 6,
            "signal_exit": [False] * 6,
        }
    )

    class Screener:
        def latest_date(self):
            return DAY

        def build_strategy_context(self, *_args, **_kwargs):
            return StrategyDataContext("stock", "1d", DAY, current=self.frame)

    screener = Screener()
    screener.frame = frame
    quotes = SimpleNamespace(status=lambda: {"final_sync_done": True, "final_sync_failed": None})
    monkeypatch.setattr(paper, "cn_today", lambda: DAY)
    monkeypatch.setattr(paper, "cn_now", lambda: NOW)
    paper.create_account(tmp_path, 100_000)
    service = ExecutionService(tmp_path, engine, screener, quotes)
    return SimpleNamespace(
        root=tmp_path, engine=engine, service=service, screener=screener, path=path
    )


def bind(env, **config_values):
    source = {
        "config": {
            "strategy_id": "demo",
            "initial_capital": 100_000,
            "max_positions": 10,
            **config_values,
        }
    }
    preview = env.service.preview(source)
    return env.service.bind("default", source, preview["revision"], now=NOW)


def test_six_signals_are_not_truncated_or_merged_and_restart_does_not_duplicate(env):
    bind(env)
    env.service.tick(NOW)
    orders = paper.load_orders(env.root)
    assert len(orders) == 6
    assert all(o["side"] == "buy" and o["order_type"] == "next_open" for o in orders)
    restarted = ExecutionService(env.root, env.engine, env.screener, env.service.quotes)
    restarted.tick(NOW)
    assert len(paper.load_orders(env.root)) == 6


def test_candidate_without_entry_signal_does_not_buy(env):
    env.screener.frame = env.screener.frame.with_columns(pl.lit(False).alias("signal_entry"))
    bind(env)
    env.service.tick(NOW)
    assert paper.load_orders(env.root) == []


def test_snapshot_freezes_saved_params_and_backtest_explicit_params_win(env):
    config.save_override(env.root, "demo", {"params": {"threshold": 19.0}, "entry_signals": []})
    bind(env, params={"threshold": 9.0})
    config.save_override(env.root, "demo", {"params": {"threshold": 20.0}})
    env.service.tick(NOW)
    assert len(paper.load_orders(env.root)) == 6
    snapshot = env.service.get("default")["snapshot"]
    assert snapshot["params"]["threshold"] == 9.0
    assert snapshot["overrides"]["entry_signals"] == ["entry"]


def test_wait_for_confirmed_current_bar_and_do_not_replay_past_signal(env):
    bind(env)
    env.service.tick(NOW.replace(hour=14))
    assert paper.load_orders(env.root) == []
    env.service.quotes.status = lambda: {
        "final_sync_done": True,
        "final_sync_failed": "unconfirmed_snapshot",
    }
    env.service.tick(NOW)
    assert paper.load_orders(env.root) == []
    env.service.quotes.status = lambda: {"final_sync_done": True, "final_sync_failed": None}
    env.service.tick(NOW.replace(day=27))
    assert paper.load_orders(env.root) == []


def test_changed_strategy_pauses_instead_of_using_new_code(env):
    bind(env)
    env.path.write_text(env.path.read_text() + "\n# changed\n")
    env.service.tick(NOW)
    assert paper.load_orders(env.root) == []
    assert env.service.get("default")["status"] == "blocked"


@pytest.mark.parametrize(
    "extra",
    [
        {"entry_fill": "close_t"},
        {"minute_fill": True},
        {"position_sizing": "policy"},
        {"asset_type": "etf"},
        {"params": {"threshold": 99}},
        {"max_positions": 0},
    ],
)
def test_unsupported_or_silently_normalized_parameters_are_rejected(env, extra):
    with pytest.raises(ValueError):
        bind(env, **extra)


def test_existing_rules_require_explicit_takeover_and_new_conflict_blocks(env):
    paper_auto.create_auto_rule(
        env.root,
        {
            "name": "旧规则",
            "match_kind": "strategy",
            "match_id": "demo",
            "side": "auto",
            "size_mode": "fixed_amount",
            "size_value": 1000,
        },
    )
    with pytest.raises(ValueError, match="旧跟单"):
        bind(env)
    source = {"strategy_id": "demo"}
    preview = env.service.preview(source)
    env.service.bind("default", source, preview["revision"], disable_legacy=True, now=NOW)
    assert not paper_auto.load_auto_rules(env.root, enabled_only=True)
    rule = paper_auto.load_auto_rules(env.root)[0]
    paper_auto.set_enabled(env.root, rule["id"], True)
    env.service.tick(NOW)
    assert env.service.get("default")["status"] == "blocked"
    assert paper.load_orders(env.root) == []


def test_preview_revision_prevents_stale_binding(env):
    source = {"strategy_id": "demo"}
    preview = env.service.preview(source)
    env.path.write_text(env.path.read_text() + "\n# edit\n")
    with pytest.raises(ValueError, match="预览"):
        env.service.bind("default", source, preview["revision"], now=NOW)


def fill_buys(env, monkeypatch, day=27):
    from app.tickflow.repository import DataStore, KlineRepository

    repo = KlineRepository(DataStore(env.root))
    symbols = env.screener.frame["symbol"].to_list()
    for d in (26, 27, 28):
        repo.append_daily(
            pl.DataFrame(
                {
                    "symbol": symbols,
                    "date": [DAY.replace(day=d)] * len(symbols),
                    "open": [10.0] * len(symbols),
                    "high": [10.0] * len(symbols),
                    "low": [10.0] * len(symbols),
                    "close": [10.0] * len(symbols),
                    "volume": [10000.0] * len(symbols),
                    "amount": [100000.0] * len(symbols),
                }
            )
        )
    monkeypatch.setattr(paper, "cn_today", lambda: DAY.replace(day=day))
    monkeypatch.setattr(paper, "cn_now", lambda: NOW.replace(day=day))
    summary = paper.settle_day(env.root, DAY.replace(day=27).isoformat())
    assert summary["filled"] == 6
    return repo


def set_bar(env, day, *, entry=False, exit=True):
    env.screener.frame = env.screener.frame.with_columns(
        pl.lit(DAY.replace(day=day)).alias("date"),
        pl.lit(entry).alias("signal_entry"),
        pl.lit(exit).alias("signal_exit"),
    )
    env.screener.latest_date = lambda: DAY.replace(day=day)
    env.screener.build_strategy_context = lambda *_a, **_kw: StrategyDataContext(
        "stock", "1d", DAY.replace(day=day), current=env.screener.frame
    )


def test_sell_signals_do_not_wait_for_buy_cooldown_and_no_repeated_orders(env, monkeypatch):
    bind(env)
    env.service.tick(NOW)
    fill_buys(env, monkeypatch, day=28)
    set_bar(env, 28)
    env.service.tick(NOW.replace(day=28))
    orders = paper.load_orders(env.root)
    assert len([o for o in orders if o["side"] == "sell"]) == 6
    assert all(o["qty"] > 0 for o in orders)
    env.service.tick(NOW.replace(day=28))
    assert len(paper.load_orders(env.root)) == 12


def test_entry_day_sell_intent_survives_restart_and_queues_before_next_open(env, monkeypatch):
    bind(env)
    env.service.tick(NOW)
    fill_buys(env, monkeypatch)
    set_bar(env, 27)
    env.service.tick(NOW.replace(day=27))
    assert not [o for o in paper.load_orders(env.root) if o["side"] == "sell"]
    monkeypatch.setattr(paper, "cn_today", lambda: DAY.replace(day=28))
    morning = NOW.replace(day=28, hour=9, minute=10)
    monkeypatch.setattr(paper, "cn_now", lambda: morning)
    restarted = ExecutionService(env.root, env.engine, env.screener, env.service.quotes)
    restarted.tick(morning)
    sells = [o for o in paper.load_orders(env.root) if o["side"] == "sell"]
    assert len(sells) == 6
    assert all(o["order_type"] == "next_open" for o in sells)
    assert paper.settle_day(env.root, "2026-10-28")["filled"] == 6


def test_reserved_buys_are_counted_in_cash_and_position_limit(env):
    bind(env, max_positions=2, max_exposure_pct=0.5)
    env.service.tick(NOW)
    orders = paper.load_orders(env.root)
    assert len(orders) == 2
    assert sum(o["qty"] * o["ref_price"] for o in orders) <= 50_000
    # 模拟进程在写检查点前退出；订单来源本身仍能去重。
    state = env.service._load("default")
    state["last_bar"] = None
    env.service._save(state)
    env.service.tick(NOW)
    assert len(paper.load_orders(env.root)) == 2


def test_risk_uses_raw_prices_and_fresh_per_symbol_timestamp(env, monkeypatch):
    bind(env, overrides={"stop_loss": 0.05})
    env.service.tick(NOW)
    fill_buys(env, monkeypatch, day=28)
    live_now = NOW.replace(day=28, hour=10)
    monkeypatch.setattr(paper, "cn_now", lambda: live_now)
    frame = env.screener.frame.with_columns(
        pl.lit(100.0).alias("close"),
        pl.lit(9.4).alias("raw_close"),
        pl.lit(live_now.timestamp() * 1000).alias("quote_ts"),
    )
    env.service.quotes.status = lambda: {"quote_age_ms": 1}
    env.service.quotes.get_enriched_today = lambda: (frame, live_now.date())
    env.service.tick(live_now)
    sells = [o for o in paper.load_orders(env.root) if o["side"] == "sell"]
    assert len(sells) == 6
    assert all(
        o["order_type"] == "market" and o["ref_price"] == 9.4 and o["source"].endswith(":stop_loss")
        for o in sells
    )
    # 原账务照常撮合、记录真实费用；扩展没有创建第二份成交。
    assert len(paper.evaluate_intraday(env.root, {o["symbol"]: 9.4 for o in sells})) == 6
    assert all(p["qty"] == 0 for p in paper.load_positions(env.root).values())


def test_stale_symbol_quote_does_not_trigger_protection(env, monkeypatch):
    bind(env, overrides={"stop_loss": 0.05})
    env.service.tick(NOW)
    fill_buys(env, monkeypatch, day=28)
    live_now = NOW.replace(day=28, hour=10)
    frame = env.screener.frame.with_columns(
        pl.lit(1.0).alias("raw_close"), pl.lit(1).alias("quote_ts")
    )
    env.service.quotes.status = lambda: {"quote_age_ms": 1}
    env.service.quotes.get_enriched_today = lambda: (frame, live_now.date())
    env.service.tick(live_now)
    assert not [o for o in paper.load_orders(env.root) if o["side"] == "sell"]
    assert "过期" in env.service.get("default")["risk_message"]


def test_max_hold_uses_trading_bars_not_calendar_days(env, monkeypatch):
    bind(env, overrides={"max_hold_days": 2})
    env.service.tick(NOW)
    repo = fill_buys(env, monkeypatch, day=28)
    env.screener.repo = repo
    set_bar(env, 28, exit=False)
    env.service.tick(NOW.replace(day=28))
    assert not [o for o in paper.load_orders(env.root) if o["side"] == "sell"]


def test_fee_drift_and_manual_orders_pause_only_managed_account(env):
    bind(env)
    paper.update_settings(env.root, "default", commission_pct=0.0009)
    env.service.tick(NOW)
    assert env.service.get("default")["status"] == "blocked"
    assert paper.load_orders(env.root) == []
    paper.create_account(env.root, 100_000, account_id="manual")
    _, error = paper.create_order(
        env.root, "000001.SZ", "buy", account_id="manual", qty=100, ref_price=10.0
    )
    assert error is None


def test_newer_or_corrupt_state_is_not_executed(env):
    bind(env)
    state = env.service._load("default")
    state["version"] = 999
    env.service._save(state)
    env.service.tick(NOW)
    assert paper.load_orders(env.root) == []
    with pytest.raises(ValueError, match="版本"):
        env.service.get("default")


def test_concurrent_ticks_submit_once(env):
    from concurrent.futures import ThreadPoolExecutor

    bind(env)
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(lambda _i: env.service.tick(NOW), range(4)))
    assert len(paper.load_orders(env.root)) == 6


def test_disabled_binding_does_not_evaluate_strategy(env, monkeypatch):
    bind(env)
    env.service.set_enabled("default", False)
    monkeypatch.setattr(
        env.engine, "run", lambda *_a, **_k: pytest.fail("disabled strategy evaluated")
    )
    env.service.tick(NOW)
    assert paper.load_orders(env.root) == []


def test_extension_api_and_lifecycle(env, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.custom.paper_execution import routes

    monkeypatch.setattr(routes, "ScreenerService", lambda _repo: env.screener)
    app = FastAPI()
    app.state.repo = SimpleNamespace(store=SimpleNamespace(data_dir=env.root))
    app.state.strategy_engine = env.engine
    app.state.quote_service = env.service.quotes
    app.include_router(routes.build_router())
    with TestClient(app) as client:
        assert client.get("/api/custom/paper-execution/state").json() == {"binding": None}
        source = {"strategy_id": "demo"}
        preview = client.post("/api/custom/paper-execution/preview", json=source).json()
        response = client.put(
            "/api/custom/paper-execution/binding",
            json={"source": source, "revision": preview["revision"]},
        )
        assert response.status_code == 200
        assert response.json()["binding"]["snapshot"]["params"]["threshold"] == 8.0
        assert client.get("/api/custom/paper-execution/state?account=../bad").status_code == 409
        assert (
            client.post(
                "/api/custom/paper-execution/preview",
                json={"strategy_id": "demo", "candidate_id": "x"},
            ).status_code
            == 422
        )
        assert (
            client.post("/api/custom/paper-execution/enabled", json={"enabled": False}).json()[
                "binding"
            ]["enabled"]
            is False
        )
        # 第二个进程/实例拿不到执行租约时仅禁用自身。
        other = FastAPI()
        other.state.repo = app.state.repo
        other.state.strategy_engine = env.engine
        other.include_router(routes.build_router())
        with TestClient(other) as second:
            assert second.get("/api/custom/paper-execution/state").status_code == 503
    assert not any(
        t.name == "paper-execution" and t.is_alive() for t in __import__("threading").enumerate()
    )


def test_signal_mask_and_effective_params_match_backtest(env):
    from app.backtest.strategy import StrategyBacktestService

    source = {
        "config": {
            "strategy_id": "demo",
            "params": {"threshold": 9.0},
            "overrides": {"params": {"threshold": 18.0}},
            "initial_capital": 100_000,
        }
    }
    preview = env.service.preview(source)
    snapshot = preview["snapshot"]
    strategy = env.engine.get("demo")
    backtest = StrategyBacktestService.__new__(StrategyBacktestService)
    backtest_params = backtest._normalize_params(source["config"]["params"], strategy)
    assert snapshot["params"] == backtest_params
    frame = env.screener.frame.with_columns(
        pl.Series("signal_entry", [True, False, True, False, True, True]),
        pl.Series("close", [10.0, 10.0, 8.0, 8.0, 11.0, 12.0]),
    )
    expected = backtest._build_entry_mask(frame, strategy, backtest_params, ["entry"])
    result = env.engine.run(
        "demo",
        StrategyDataContext("stock", "1d", DAY, current=frame),
        params=snapshot["params"],
        overrides=snapshot["overrides"],
    )
    assert {r["symbol"] for r in result.entry_signal_hits} == set(
        frame.filter(expected)["symbol"].to_list()
    )
    assert len(result.entry_signal_hits) == 3


def test_missing_signal_blocks_account_instead_of_silently_ignoring_it(env):
    bind(env)
    env.screener.frame = env.screener.frame.drop("signal_exit")
    env.service.tick(NOW)
    assert paper.load_orders(env.root) == []
    assert "依赖尚不可用" in env.service.get("default")["message"]


@pytest.mark.parametrize(
    "risk,entry,peak,price,expected",
    [
        ({"stop_loss": 0.05}, 10, 10, 9.5, "stop_loss"),
        ({"take_profit": 0.1}, 10, 11, 11, "take_profit"),
        ({"stop_loss": 0.05, "trailing_stop": 0.05}, 10, 12, 11, "trailing_stop"),
        (
            {"trailing_take_profit_activate": 0.1, "trailing_take_profit_drawdown": 0.05},
            10,
            12,
            11,
            "trailing_take_profit",
        ),
        (
            {"trailing_take_profit_activate": 0.2, "trailing_take_profit_drawdown": 0.05},
            10,
            11,
            10,
            None,
        ),
    ],
)
def test_risk_thresholds(risk, entry, peak, price, expected):
    from app.custom.paper_execution.service import risk_reason

    assert risk_reason(risk, entry, peak, price) == expected


def test_missing_optional_runtime_fields_default_to_paused(env):
    bind(env)
    state = env.service._load("default")
    for key in ("enabled", "status", "message", "events", "risk", "exits", "last_bar"):
        state.pop(key)
    env.service._save(state)
    env.service.tick(NOW)
    assert not env.service.get("default")["enabled"]
    assert env.service.get("default")["events"] == []
    assert paper.load_orders(env.root) == []


def test_definition_change_during_evaluation_prevents_orders(env, monkeypatch):
    bind(env)
    original_run = env.engine.run

    def changed(*args, **kwargs):
        result = original_run(*args, **kwargs)
        env.path.write_text(env.path.read_text() + "\n# changed while evaluating\n")
        return result

    monkeypatch.setattr(env.engine, "run", changed)
    env.service.tick(NOW)
    assert paper.load_orders(env.root) == []
    assert env.service.get("default")["status"] == "blocked"


def test_dependency_fingerprint_uses_original_files_not_export_serialization(env):
    import json

    signal_dir = env.root / "user_data/custom_signals"
    signal_dir.mkdir(parents=True)
    definition = {
        "id": "qa_entry",
        "name": "自定义买点",
        "kind": "entry",
        "enabled": True,
        "conditions": [{"left": "close", "op": ">", "right": 0}],
    }
    path = signal_dir / "qa_entry.json"
    path.write_text(json.dumps(definition, separators=(",", ":")))
    env.path.write_text(
        env.path.read_text().replace(
            'ENTRY_SIGNALS = ["entry"]', 'ENTRY_SIGNALS = ["csg_qa_entry"]'
        )
    )
    env.engine.reload()
    env.screener.frame = env.screener.frame.with_columns(pl.lit(True).alias("csg_qa_entry"))
    bind(env)
    env.service.tick(NOW)
    assert len(paper.load_orders(env.root)) == 6
    assert env.service.get("default")["enabled"] is True
    definition["conditions"][0]["right"] = 100
    path.write_text(json.dumps(definition))
    env.service.tick(NOW)
    assert env.service.get("default")["status"] == "blocked"
    assert "依赖已变更" in env.service.get("default")["message"]


def test_definition_revision_is_stable_across_processes_and_reload(env):
    import os
    import subprocess
    import sys

    from app.custom.paper_execution.snapshot import definition_revision

    expected = definition_revision(env.engine.get("demo"))
    script = """from pathlib import Path
import sys
from app.strategy.engine import StrategyEngine
from app.custom.paper_execution.snapshot import definition_revision
print(definition_revision(StrategyEngine([Path(sys.argv[1])]).get('demo')))
"""
    for seed in ("1", "50"):
        actual = subprocess.check_output(
            [sys.executable, "-c", script, str(env.path.parent)],
            env={**os.environ, "PYTHONHASHSEED": seed},
            text=True,
        ).strip()
        assert actual == expected
    env.engine.reload()
    assert definition_revision(env.engine.get("demo")) == expected


def test_matrix_native_and_legacy_negative_stop_loss_are_supported(env):
    env.path.write_text("""import numpy as np
from app.backtest.matrix import make_signal_matrix
META = {"id": "demo", "name": "矩阵策略", "limit": 1, "params": [{"id": "threshold", "type": "float", "default": 8., "min": 0., "max": 20.}]}
EXECUTION_BACKEND = "matrix_native"
BASIC_FILTER = {"enabled": False}
STOP_LOSS = -0.06
class Native:
    def required_fields(self):
        return frozenset({"close"})
    def required_warmup_bars(self, params):
        return 1
    def compute_signals(self, market, params):
        return make_signal_matrix(market.shape, entry=(market.close > params["threshold"]).astype(np.uint8), exit=np.zeros(market.shape, dtype=np.uint8))
MATRIX_STRATEGY = Native()
""")
    env.engine.reload()
    env.screener.frame = env.screener.frame.with_columns(
        pl.lit(10.0).alias("open"),
        pl.lit(10.0).alias("high"),
        pl.lit(10.0).alias("low"),
        pl.lit(10000.0).alias("volume"),
    )
    history = pl.concat(
        [
            env.screener.frame.with_columns(pl.lit(DAY.replace(day=23)).alias("date")),
            env.screener.frame,
        ]
    )
    env.screener.build_strategy_context = lambda *_a, **_kw: StrategyDataContext(
        "stock", "1d", DAY, current=env.screener.frame, history=history
    )
    bind(env)
    env.service.tick(NOW)
    assert len(paper.load_orders(env.root)) == 6
    assert env.service.get("default")["snapshot"]["overrides"]["stop_loss"] == -0.06


def test_negative_trailing_thresholds_normalize_like_backtest(env):
    from app.backtest.strategy import StrategyBacktestService

    preview = env.service.preview(
        {"config": {"strategy_id": "demo", "overrides": {"trailing_stop": -0.05}}}
    )
    assert preview["snapshot"]["overrides"][
        "trailing_stop"
    ] == StrategyBacktestService._normalize_pct(-0.05, 0.005, 0.5)
