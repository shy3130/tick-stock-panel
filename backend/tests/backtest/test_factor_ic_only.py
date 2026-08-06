from __future__ import annotations

from datetime import date, timedelta

import numpy as np
import polars as pl

from app.backtest.engine import BacktestEngine
from app.backtest.factor import FactorBacktestService, FactorConfig


def _panel(symbols: list[str], n_days: int, factor_name: str) -> pl.DataFrame:
    rng = np.random.default_rng(7)
    rows = []
    for i, sym in enumerate(symbols):
        price = 10.0 + i
        for d in range(n_days):
            price *= 1 + rng.normal(0, 0.01)
            rows.append(
                {
                    "symbol": sym,
                    "date": date(2024, 1, 1) + timedelta(days=d),
                    "close": round(price, 4),
                    factor_name: float(i) + d * 0.01,
                }
            )
    return pl.DataFrame(rows)


def _service_with_panel(panel: pl.DataFrame) -> FactorBacktestService:
    engine = BacktestEngine(repo=None)
    engine.load_panel = lambda *a, **kw: panel
    return FactorBacktestService(engine)


def test_compute_ic_only_matches_run_ic_fields():
    panel = _panel(["A", "B", "C"], 10, "test_factor")
    svc = _service_with_panel(panel)
    config = FactorConfig(
        factor_name="test_factor",
        symbols=["A", "B", "C"],
        start=date(2024, 1, 1),
        end=date(2024, 1, 10),
        rebalance="daily",
    )

    full = svc.run(config)
    ic_only = svc.compute_ic_only(config)

    assert ic_only["error"] is None
    assert ic_only["ic_mean"] == full.ic_mean
    assert ic_only["ic_std"] == full.ic_std
    assert ic_only["ir"] == full.ir
    assert ic_only["ic_win_rate"] == full.ic_win_rate


def test_compute_ic_only_reports_error_on_empty_panel():
    svc = _service_with_panel(pl.DataFrame())
    config = FactorConfig(
        factor_name="test_factor",
        symbols=["A"],
        start=date(2024, 1, 1),
        end=date(2024, 1, 10),
        rebalance="daily",
    )

    out = svc.compute_ic_only(config)

    assert out["error"] is not None
    assert out["ic_mean"] is None
    assert out["ic_std"] is None
    assert out["ir"] is None
    assert out["ic_win_rate"] is None



def test_run_keeps_tied_cross_section_in_one_group():
    """重复分位数边界不应使完整因子回测失败或任意拆分并列因子值。"""
    panel = pl.DataFrame([
        {
            "symbol": symbol,
            "date": date(2024, 1, day),
            "close": close,
            "test_factor": 0.0,
        }
        for day, close in ((1, 10.0), (2, 11.0), (3, 12.0))
        for symbol in ("A", "B", "C", "D", "E", "F")
    ])
    svc = _service_with_panel(panel)

    result = svc.run(FactorConfig(
        factor_name="test_factor",
        symbols=["A", "B", "C", "D", "E", "F"],
        start=date(2024, 1, 1),
        end=date(2024, 1, 3),
        n_groups=5,
        rebalance="daily",
    ))

    assert result.error is None
    assert {row["Q1"] for row in result.group_nav} == {1.1, 1.2}
    assert all("Q2" not in row for row in result.group_nav)