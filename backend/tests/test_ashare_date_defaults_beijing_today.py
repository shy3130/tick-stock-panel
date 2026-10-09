"""A股业务日期缺省必须使用北京时间 (cn_today), 服务器时区不能成为隐式输入。

对齐 CONTRIBUTING.md §3.3:
- /api/backtest/run, /factor/run, /factor/batch, /strategy/run 缺省 end 为北京日期
- /api/factors/trial, _trial_nonempty load_panel 截面 end 为北京日期
- /api/regime/recompute, /recompute/mainline 缺省 end 为北京日期
- /api/kline/repair_daily 与 repair_daily.py 校验与上限对齐北京今天 (非本地 date.today())
- mainline 与 regime incremental 增量更新截止日期对齐北京今天
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock

import polars as pl
import pytest

from app.api import backtest as backtest_api
from app.api import factors as factors_api
from app.api import kline as kline_api
from app.api import regime as regime_api
from app.services import market_mainline, regime_builder, repair_daily

BJ = date(2026, 3, 2)


@dataclass
class _DummyResult:
    status: str = "ok"


def test_backtest_run_defaults_to_beijing_today(monkeypatch) -> None:
    captured_cfg = []

    class DummyService:
        def __init__(self, repo):
            pass

        def run(self, cfg):
            captured_cfg.append(cfg)
            return _DummyResult()

    monkeypatch.setattr(backtest_api, "cn_today", lambda: BJ)
    monkeypatch.setattr(backtest_api, "BacktestService", DummyService)

    req_data = backtest_api.BacktestRequest(symbols=["600000.SH"])
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(repo=MagicMock())))

    backtest_api.run(req_data, request)
    assert captured_cfg, "应该调用 BacktestService.run"
    assert captured_cfg[0].end == BJ
    assert captured_cfg[0].start == BJ - timedelta(days=365 * 3)


def test_backtest_factor_run_defaults_to_beijing_today(monkeypatch) -> None:
    captured_cfg = []

    class DummyFactorService:
        def __init__(self, engine):
            pass

        def run(self, cfg):
            captured_cfg.append(cfg)
            return _DummyResult()

    monkeypatch.setattr(backtest_api, "cn_today", lambda: BJ)
    monkeypatch.setattr("app.backtest.factor.FactorBacktestService", DummyFactorService)
    monkeypatch.setattr(backtest_api, "_get_engine", lambda r: MagicMock())
    monkeypatch.setattr("app.factors.registry.factor_columns_view", lambda: [{"id": "mom_20d"}])

    req_data = backtest_api.FactorBacktestRequest(factor_name="mom_20d")
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(repo=MagicMock())))

    backtest_api.factor_run(req_data, request)
    assert captured_cfg
    assert captured_cfg[0].end == BJ


def test_factors_trial_uses_beijing_today(monkeypatch) -> None:
    captured_args = []

    class DummyEngine:
        def load_panel(self, symbols, start, end, columns, asset_type):
            captured_args.append((start, end))
            return pl.DataFrame({col: [1.0] for col in columns})

    monkeypatch.setattr(factors_api, "cn_today", lambda: BJ)
    monkeypatch.setattr(factors_api, "compile_formula", lambda f: SimpleNamespace(
        ok=True, warmup_bars=5, dependencies=[], referenced_factors=[], frame_transform=lambda p: p
    ))
    monkeypatch.setattr("app.api.backtest._get_engine", lambda r: DummyEngine())
    monkeypatch.setattr("app.backtest.factor.FactorBacktestService._compute_missing_factors", lambda panel, to_compute: panel)
    monkeypatch.setattr("app.factors.dsl.FACTOR_COLUMN", "factor")

    req_data = factors_api.FormulaTrialRequest(formula="close / open", days=20)
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(repo=MagicMock())))

    try:
        factors_api.trial_formula(req_data, request)
    except Exception:
        pass
    assert captured_args
    _start, end = captured_args[0]
    assert end == BJ


def test_regime_recompute_defaults_to_beijing_today(monkeypatch) -> None:
    captured_args = []

    def fake_run_batch(repo, start, end):
        captured_args.append((start, end))
        return pl.DataFrame()

    monkeypatch.setattr(regime_api, "cn_today", lambda: BJ)
    monkeypatch.setattr(regime_api, "_data_dir", lambda r: MagicMock())
    monkeypatch.setattr(regime_builder, "earliest_enriched_date", lambda repo: date(2026, 1, 1))
    monkeypatch.setattr(regime_builder, "run_regime_batch", fake_run_batch)
    monkeypatch.setattr(regime_builder, "refresh_phase_labels", lambda d: 0)

    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(repo=MagicMock())))
    regime_api.regime_recompute(request, start=None, end=None)

    assert captured_args
    _start, end = captured_args[0]
    assert end == BJ


def test_repair_daily_validates_against_beijing_today(monkeypatch) -> None:
    monkeypatch.setattr(repair_daily, "cn_today", lambda: BJ)

    called = []
    monkeypatch.setattr("app.jobs.daily_pipeline.run_now", lambda *args, **kwargs: (called.append(kwargs), {"ok": True})[1])

    repo = MagicMock()
    capset = MagicMock()

    # start_date == BJ (valid today in Beijing, even if local host date < BJ)
    res = repair_daily.run_repair_daily(repo, capset, start_date=BJ)
    assert res.get("ok") is True
    assert called

    # start_date > BJ (future in Beijing)
    res_future = repair_daily.run_repair_daily(repo, capset, start_date=BJ + timedelta(days=1))
    assert res_future.get("error") == "起始日期不能晚于今天"


def test_market_mainline_incremental_uses_beijing_today(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(market_mainline, "cn_today", lambda: BJ)
    monkeypatch.setattr(market_mainline, "load_mainline_history", lambda d, k: pl.DataFrame())

    captured_range = []
    monkeypatch.setattr(market_mainline, "compute_mainline_range", lambda r, d, s, e, kind="concept": (
        captured_range.append((s, e)), pl.DataFrame()
    )[1])
    monkeypatch.setattr("app.services.regime_builder.enriched_date_set", lambda repo: {
        date(2026, 3, 1), BJ, date(2026, 3, 3)
    })

    repo = MagicMock()
    market_mainline.compute_mainline_incremental(repo, tmp_path)

    assert captured_range
    start, end = captured_range[0]
    assert start == date(2026, 3, 1)
    assert end == BJ  # Excludes 2026-03-03 because BJ is 2026-03-02
