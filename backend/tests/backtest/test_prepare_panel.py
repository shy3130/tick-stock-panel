"""P2a 共享特征面板 (PreparedPanelBacktest) 契约测试。

验收对象 (docs/ext-architecture-plan.md P2):
  - polars_expr/python_history_legacy 参数网格 prepare-once — 全组依赖计划
    取并集, 一次加载, 每组复用 (load 只发生一次);
  - 结果与逐组独立加载完全一致 (stats 剔除计时字段后相等, 交易一致);
  - fail-closed: 签名不匹配 / 组合所需列缺失 → 显式报错, 不静默回退。
"""
from __future__ import annotations

from datetime import date, timedelta
from types import SimpleNamespace

import polars as pl
import pytest

from app.backtest.engine import SimResult
from app.backtest.strategy import (
    PreparedPanelBacktest,
    StrategyBacktestConfig,
    StrategyBacktestService,
    _panel_backend,
)
from app.strategy.engine import StrategyDef


def _strategy(**kwargs) -> StrategyDef:
    defaults = dict(
        meta={"id": "test", "name": "test", "scoring": {}, "params": [], "limit": 100},
        basic_filter=None,
        entry_signals=[],
        exit_signals=[],
        stop_loss=None,
        trailing_stop=None,
        trailing_take_profit_activate=None,
        trailing_take_profit_drawdown=None,
        max_hold_days=None,
        # 参数驱动依赖: ma_proximity=1 引用 ma5, 否则 ma10 — 并集必须两者都载
        filter_fn=lambda df, params: pl.col("close") > pl.col(
            "ma5" if int(params.get("ma_proximity", 1)) == 1 else "ma10"
        ),
        filter_history_fn=None,
        lookback_days=1,
        source="custom",
        file_path=None,
    )
    defaults.update(kwargs)
    return StrategyDef(**defaults)


class _StrategyEngineStub:
    def __init__(self, strategy: StrategyDef) -> None:
        self.strategy = strategy

    def get(self, strategy_id: str) -> StrategyDef:
        return self.strategy


class _RepoStub:
    def __init__(self, data_dir=None) -> None:
        self.store = SimpleNamespace(data_dir=data_dir)

    def get_index_daily(self, *args, **kwargs) -> pl.DataFrame:
        return pl.DataFrame()


class _EngineStub:
    """记录加载次数的最小 BacktestEngine 替身 (面板路径只需 load_panel 家族)。"""

    def __init__(self, panel: pl.DataFrame) -> None:
        self.panel = panel
        self.repo = _RepoStub()
        self.load_count = 0

    def load_panel(self, symbols, start, end, columns=None, asset_type="stock") -> pl.DataFrame:
        self.load_count += 1
        return self.panel

    def load_panel_for_backtest(self, symbols, start, end, feature_plan, asset_type="stock"):
        return self.load_panel(symbols, start, end, columns=sorted(feature_plan.base_columns))

    def simulate_market_matrix(self, matrix, config, progress_cb=None, cancel_event=None, options=None):
        return SimResult(
            equity_curve=[{"date": "2024-01-01", "value": config.initial_capital}],
            drawdown_curve=[{"date": "2024-01-01", "value": 0.0}],
            trades=[],
            per_symbol_stats=[],
            stats={"total_return": 0.0, "n_trades": 0},
        )


def _panel(n_days: int = 40) -> pl.DataFrame:
    base = date(2024, 1, 1)
    rows = []
    for i in range(n_days):
        for sym in ("A", "B"):
            close = 10.0 + i * 0.1
            rows.append({
                "symbol": sym, "name": sym, "date": base + timedelta(days=i),
                "open": close - 0.05, "high": close + 0.1, "low": close - 0.1, "close": close,
                "volume": 100_000.0, "amount": close * 100_000.0,
                "raw_close": close, "raw_high": close + 0.1, "raw_low": close - 0.1,
                "ma5": close - 0.2, "ma10": close - 0.5,
                "signal_limit_up": False, "signal_limit_down": False,
            })
    return pl.DataFrame(rows).sort(["symbol", "date"])


def _service(panel: pl.DataFrame, strategy: StrategyDef | None = None):
    engine = _EngineStub(panel)
    strategy = strategy or _strategy()
    return StrategyBacktestService(engine, _StrategyEngineStub(strategy)), engine


def _cfg(params: dict | None = None, **kw) -> StrategyBacktestConfig:
    base = dict(
        strategy_id="test",
        symbols=["A", "B"],
        start=date(2024, 1, 20),
        end=date(2024, 2, 28),
        params=params or {},
        mode="fast",
    )
    base.update(kw)
    return StrategyBacktestConfig(**base)


@pytest.fixture(autouse=True)
def _migrated_signal_definitions(monkeypatch):
    from tests.signal_seeds import install_pipeline_caches
    install_pipeline_caches(monkeypatch)


def test_prepare_loads_once_and_trials_reuse_panel() -> None:
    service, engine = _service(_panel())
    cfgs = [_cfg({"ma_proximity": 1}), _cfg({"ma_proximity": 2}), _cfg({"ma_proximity": 1})]
    prepared = service.prepare_panel_optimization(cfgs)

    assert engine.load_count == 1  # 全组一次加载
    # 参数驱动列差异由并集计划覆盖: ma5 与 ma10 都在面板里
    assert {"ma5", "ma10"} <= prepared.columns

    for cfg in cfgs:
        result = service.run(cfg, prepared_panel=prepared)
        assert result.error is None, result.error
    assert engine.load_count == 1  # 试算不再触发加载


def test_shared_panel_results_match_independent_runs() -> None:
    service, _ = _service(_panel())
    cfgs = [_cfg({"ma_proximity": 1}), _cfg({"ma_proximity": 2})]
    prepared = service.prepare_panel_optimization(cfgs)

    for cfg in cfgs:
        plain = service.run(cfg)
        shared = service.run(cfg, prepared_panel=prepared)
        assert plain.error is None and shared.error is None
        # 计时字段天然不同 (load_panel=0), 金融口径必须一致
        assert {k: v for k, v in (plain.stats or {}).items() if k != "timing_ms"} == \
               {k: v for k, v in (shared.stats or {}).items() if k != "timing_ms"}
        assert plain.trades == shared.trades


def test_shared_panel_signature_mismatch_rejected() -> None:
    service, _ = _service(_panel())
    prepared = service.prepare_panel_optimization([_cfg({"ma_proximity": 1})])
    drifted = service.run(_cfg({"ma_proximity": 1}, end=date(2024, 3, 15)), prepared_panel=prepared)
    assert drifted.error is not None and "不匹配" in drifted.error


def test_shared_panel_missing_columns_fail_closed() -> None:
    """共享面板缺本组所需列 → 显式报错 (不静默回退加载)。"""
    service, _ = _service(_panel())
    prepared = service.prepare_panel_optimization([_cfg({"ma_proximity": 1})])

    drifted = _strategy(
        filter_fn=lambda df, params: pl.col("close") > pl.col("ma30"),
    )
    drifted_service, _engine = _service(_panel(), strategy=drifted)
    # 用第一个 service 的面板喂第二个 service (模拟计划漂移)
    fake_prepared = PreparedPanelBacktest(
        signature=drifted_service._matrix_prepare_signature(_cfg({"ma_proximity": 9})),
        panel=prepared.panel,
        columns=prepared.columns,
        load_start=prepared.load_start,
        load_end=prepared.load_end,
        sim_end=prepared.sim_end,
    )
    result = drifted_service.run(_cfg({"ma_proximity": 9}), prepared_panel=fake_prepared)
    assert result.error is not None and "所需列" in result.error


def test_prepare_rejects_matrix_backend() -> None:
    matrix_strategy = _strategy(execution_backend="matrix_native", filter_fn=None)
    matrix_service = StrategyBacktestService(_EngineStub(_panel()), _StrategyEngineStub(matrix_strategy))
    with pytest.raises(ValueError, match="panel-path"):
        matrix_service.prepare_panel_optimization([_cfg()])


def test_panel_backend_predicate() -> None:
    assert _panel_backend("polars_expr")
    assert _panel_backend("python_history_legacy")
    assert not _panel_backend("matrix_native")
    assert not _panel_backend("composite")
    assert not _panel_backend("minute_filter")


# ── P2a-2: 共享矩阵编码 ──────────────────────────────────

def test_matrix_encoding_equivalence() -> None:
    """编码路径与整建路径逐元素一致 (含 score 的 null→nan→0 语义)。"""
    import dataclasses

    import numpy as np

    from app.backtest.matrix import (
        build_market_matrix,
        build_market_matrix_from_encoding,
        encode_panel_matrix,
    )

    rows = []
    for i in range(30):
        for order, sym in enumerate(("A", "B", "C")):
            close = 10.0 + i * 0.1
            rows.append({
                "symbol": sym, "name": sym, "date": date(2024, 1, 1) + timedelta(days=i),
                "open": close - 0.05, "high": close + 0.1, "low": close - 0.1, "close": close,
                "volume": 1000.0 + i, "ma5": close - 0.1, "ma10": close - 0.2, "ma20": close - 0.3,
                # null (非候选行) 与负值/越界值都要走到 — nan_to_num(0) 语义一致
                "score": None if i % 7 == 0 else (i * 0.01 if order != 1 else -1.0),
                "signal_limit_up": i % 11 == 0, "signal_limit_down": False,
                "signal_golden": (i + order) % 9 == 0,
            })
    panel = pl.DataFrame(rows).sort(["symbol", "date"]).with_columns(
        pl.col("score").cast(pl.Float64, strict=False)
    )
    n = len(panel)
    entries = pl.Series([(i + j) % 13 == 0 for i in range(30) for j in range(3)][:n])
    exits = pl.Series([(i * j) % 17 == 0 for i in range(30) for j in range(3)][:n])

    direct = build_market_matrix(
        panel, entries, exits,
        entry_delay_bars=1, exit_delay_bars=1,
        entry_signal_ids=["signal_golden"],
    )
    encoding = encode_panel_matrix(panel, entry_signal_ids=["signal_golden"])
    via_encoding = build_market_matrix_from_encoding(
        encoding,
        score=panel.get_column("score"),
        entries=entries, exits=exits,
        entry_delay_bars=1, exit_delay_bars=1,
    )

    for field in dataclasses.fields(direct):
        a, b = getattr(direct, field.name), getattr(via_encoding, field.name)
        if isinstance(a, np.ndarray):
            assert np.array_equal(a, b, equal_nan=True), f"{field.name} 数组不一致"
        else:
            assert a == b, f"{field.name} 不一致: {a!r} vs {b!r}"


def test_prepare_builds_sim_encoding_and_runs_use_it() -> None:
    service, engine = _service(_panel())
    cfgs = [_cfg({"ma_proximity": 1}), _cfg({"ma_proximity": 2})]
    prepared = service.prepare_panel_optimization(cfgs)

    assert prepared.sim_encoding is not None
    assert prepared.sim_encoding.panel_rows > 0
    for cfg in cfgs:
        result = service.run(cfg, prepared_panel=prepared)
        assert result.error is None, result.error
    assert engine.load_count == 1
