from __future__ import annotations

from datetime import date
from pathlib import Path

import numpy as np
import polars as pl
import pytest

from app.backtest.matrix import make_signal_matrix
from app.api.strategy import _strategy_detail
from app.strategy.engine import StrategyDataContext, StrategyDef, StrategyEngine


def _strategy_code(strategy_id: str, role: str) -> str:
    return f'''import polars as pl
META = {{
    "id": "{strategy_id}",
    "name": "{strategy_id}",
    "strategy_role": "{role}",
    "asset_types": ["stock"],
    "timeframes": ["1d"],
}}
EXECUTION_BACKEND = "polars_expr"
def filter(df, params):
    return pl.lit(True)
'''


def test_strategy_role_must_be_known(tmp_path) -> None:
    path = tmp_path / "unknown_role.py"
    path.write_text(_strategy_code("unknown_role", "short"), encoding="utf-8")

    with pytest.raises(ValueError, match="strategy_role"):
        StrategyEngine._load_file(path)


class _RiskSignals:
    def required_fields(self) -> frozenset[str]:
        return frozenset({"open", "high", "low", "close", "volume"})

    def required_warmup_bars(self, params: dict) -> int:
        return 1

    def compute_signals(self, market, params):
        entry = np.array([[1, 0]], dtype=np.uint8)
        exit_ = np.array([[0, 1]], dtype=np.uint8)
        return make_signal_matrix(market.shape, entry=entry, exit=exit_)


def test_matrix_screener_selects_exit_plane_for_risk_strategy() -> None:
    strategy = StrategyDef(
        meta={
            "id": "risk_fixture",
            "name": "risk_fixture",
            "strategy_role": "risk",
            "asset_types": ["stock"],
            "timeframes": ["1d"],
            "params": [],
            "scoring": {},
            "limit": 100,
        },
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
        matrix_strategy=_RiskSignals(),
    )
    engine = StrategyEngine(strategy_dirs=[])
    engine._strategies[strategy.meta["id"]] = strategy
    panel = pl.DataFrame({
        "symbol": ["A.US", "B.US"],
        "date": [date(2026, 7, 17), date(2026, 7, 17)],
        "open": [10.0, 20.0],
        "high": [11.0, 21.0],
        "low": [9.0, 19.0],
        "close": [10.5, 20.5],
        "volume": [100.0, 200.0],
    })

    result = engine.run(
        "risk_fixture",
        StrategyDataContext(
            asset_type="stock",
            timeframe="1d",
            as_of=date(2026, 7, 17),
            current=panel,
            history=panel,
        ),
        overrides={"basic_filter": {"enabled": False}},
    )

    assert [row["symbol"] for row in result.rows] == ["B.US"]


def test_strategy_detail_exposes_role() -> None:
    path = Path(__file__).parents[1] / "app" / "strategy" / "builtin" / "dow_evening_star.py"
    strategy = StrategyEngine._load_file(path)

    assert _strategy_detail(strategy)["strategy_role"] == "risk"
