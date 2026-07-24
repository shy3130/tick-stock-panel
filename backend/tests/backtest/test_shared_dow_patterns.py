from __future__ import annotations

from dataclasses import replace
from datetime import date
from types import SimpleNamespace

import numpy as np
import polars as pl

from app.backtest.matrix import build_market_data_matrix
from app.strategy.shared_dow_patterns import DowPatternMatrixStrategy


def _market():
    panel = pl.DataFrame({
        "symbol": ["A.US", "A.US", "B.US", "B.US"],
        "date": [
            date(2026, 7, 16),
            date(2026, 7, 18),
            date(2026, 7, 16),
            date(2026, 7, 17),
        ],
        "open": [10.0, 11.0, 20.0, 21.0],
        "high": [10.5, 11.5, 20.5, 21.5],
        "low": [9.5, 10.5, 19.5, 20.5],
        "close": [10.2, 11.2, 20.2, 21.2],
        "volume": [100.0, 120.0, 200.0, 220.0],
    })
    return build_market_data_matrix(panel)


def _match(role: str, known_at_index: int, score: float = 88.0):
    return SimpleNamespace(
        role=role,
        known_at_index=known_at_index,
        score=score,
    )


def test_buy_match_maps_to_entry_on_union_calendar() -> None:
    strategy = DowPatternMatrixStrategy(
        "fixture",
        "buy",
        2,
        "fixture_entry",
        scan_fn=lambda pattern_id, bars, params: (_match("buy", 1),),
    )

    signals = strategy.compute_signals(_market(), {})

    assert signals.entry[:, 0].tolist() == [0, 0, 1]
    assert signals.entry_signal_ids == ("fixture_entry",)
    assert signals.exit_signal_ids == ()


def test_risk_match_maps_to_exit_on_union_calendar() -> None:
    strategy = DowPatternMatrixStrategy(
        "fixture",
        "risk",
        2,
        "fixture_risk",
        scan_fn=lambda pattern_id, bars, params: (_match("risk", 1, 91.0),),
    )

    signals = strategy.compute_signals(_market(), {})

    assert signals.exit[:, 1].tolist() == [0, 1, 0]
    assert signals.score[1, 1] == 91
    assert signals.entry_signal_ids == ()
    assert signals.exit_signal_ids == ("fixture_risk",)


def test_invalid_rows_are_removed_before_local_index_mapping() -> None:
    market = _market()
    close = market.close.copy()
    close[1, 0] = np.nan
    market = replace(market, close=close)
    captured_lengths: list[int] = []

    def scan(pattern_id, bars, params):
        captured_lengths.append(len(bars))
        return (_match("buy", len(bars) - 1),)

    signals = DowPatternMatrixStrategy(
        "fixture", "buy", 2, "fixture_entry", scan_fn=scan
    ).compute_signals(market, {})

    assert captured_lengths[0] == 2
    assert signals.entry[:, 0].tolist() == [0, 0, 1]


def test_per_symbol_failure_does_not_abort_other_symbols() -> None:
    def scan(pattern_id, bars, params):
        if bars[0].open == 10.0:
            raise RuntimeError("bad symbol")
        return (_match("buy", 1),)

    signals = DowPatternMatrixStrategy(
        "fixture", "buy", 2, "fixture_entry", scan_fn=scan
    ).compute_signals(_market(), {})

    assert not signals.entry[:, 0].any()
    assert signals.entry[:, 1].tolist() == [0, 1, 0]
