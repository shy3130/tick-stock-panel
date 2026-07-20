from datetime import date
from types import SimpleNamespace

import polars as pl
import pytest

from app.backtest.strategy import StrategyBacktestService
from app.strategy.engine import StrategyEngine


def _candidates() -> pl.DataFrame:
    return pl.DataFrame({
        "symbol": ["A", "B"],
        "date": [date(2024, 1, 2)] * 2,
        "close": [11.0, 12.0],
        "ma20": [10.0, 10.0],
        "vol_ratio_5d": [2.0, 1.0],
    })


def test_virtual_scoring_is_shared_and_does_not_add_virtual_column():
    weights = {"ma20_bias": 0.6, "vol_ratio_5d": 0.4}
    realtime = StrategyEngine._apply_scoring(_candidates(), weights)
    strategy = SimpleNamespace(meta={"scoring": weights, "order_by": "score"})
    backtest = StrategyBacktestService._apply_score(_candidates(), strategy, None)

    assert realtime["score"].to_list() == pytest.approx([40.0, 60.0])
    assert backtest["score"].to_list() == pytest.approx([40.0, 60.0])
    assert "ma20_bias" not in realtime.columns
    assert "ma20_bias" not in backtest.columns


def test_scoring_reweights_only_available_fields():
    scored = StrategyEngine._apply_scoring(
        _candidates().drop("ma20"),
        {"ma20_bias": 0.6, "vol_ratio_5d": 0.4},
    )

    assert scored["score"].to_list() == pytest.approx([100.0, 0.0])
