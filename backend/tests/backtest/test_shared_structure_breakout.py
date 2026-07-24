from __future__ import annotations

from datetime import date
from pathlib import Path

import polars as pl
import pytest
from longbridge_stock.structure_breakout_scanner import (
    RightTriangleDetector,
    StructureCandidate,
    SymmetricTriangleDetector,
)

from app.backtest.matrix import build_market_data_matrix
from app.strategy.engine import StrategyEngine
from app.strategy.shared_structure_breakout import (
    SharedStructureBreakoutMatrixStrategy,
    candidates_to_signal_matrix,
)


def candidate(
    phase: str,
    known_at_index: int,
    *,
    symbol: str = "A.US",
    score: float = 88.0,
) -> StructureCandidate:
    return StructureCandidate(
        symbol=symbol,
        market="us",
        pattern_type="LONG_BOX",
        phase=phase,
        quality="high",
        start_index=0,
        end_index=known_at_index,
        known_at_index=known_at_index,
        breakout_index=max(0, known_at_index - 1),
        retest_index=None,
        upper_value=11.0,
        lower_value=9.0,
        invalidation_value=8.5,
        measured_target=13.0,
        score=score,
        metrics={},
        evidence=(),
    )


def test_candidate_phases_map_to_entry_and_exit() -> None:
    signals = candidates_to_signal_matrix(
        shape=(4, 1),
        symbols=("A.US",),
        candidates_by_symbol={
            "A.US": (
                candidate("BREAKOUT_CONFIRMED", 1),
                candidate("FAILED", 3),
            )
        },
        time_indexes_by_symbol={"A.US": (0, 1, 2, 3)},
    )

    assert signals.entry[:, 0].tolist() == [0, 1, 0, 0]
    assert signals.exit[:, 0].tolist() == [0, 0, 0, 1]
    assert signals.score[1, 0] == 88


def test_known_at_local_index_maps_to_union_calendar_index() -> None:
    signals = candidates_to_signal_matrix(
        shape=(5, 1),
        symbols=("A.US",),
        candidates_by_symbol={"A.US": (candidate("BREAKOUT_CONFIRMED", 1),)},
        time_indexes_by_symbol={"A.US": (0, 3, 4)},
    )

    assert signals.entry[:, 0].tolist() == [0, 0, 0, 1, 0]


def test_retest_does_not_add_a_second_entry_while_position_is_open() -> None:
    signals = candidates_to_signal_matrix(
        shape=(4, 1),
        symbols=("A.US",),
        candidates_by_symbol={
            "A.US": (
                candidate("BREAKOUT_CONFIRMED", 1, score=80),
                candidate("RETEST_CONFIRMED", 2, score=92),
            )
        },
        time_indexes_by_symbol={"A.US": (0, 1, 2, 3)},
    )

    assert signals.entry[:, 0].tolist() == [0, 1, 0, 0]
    assert signals.score[2, 0] == 92


def test_matrix_strategy_passes_market_local_rows_to_shared_core() -> None:
    captured: dict = {}

    def scan_fn(symbol, market, rows, *, detectors):
        captured.update(symbol=symbol, market=market, rows=rows, detectors=detectors)
        return (candidate("BREAKOUT_CONFIRMED", 1, symbol=symbol),)

    panel = pl.DataFrame({
        "symbol": ["1.HK", "1.HK"],
        "name": ["1.HK", "1.HK"],
        "date": [date(2026, 7, 16), date(2026, 7, 17)],
        "open": [10.0, 10.5],
        "high": [11.0, 11.5],
        "low": [9.5, 10.0],
        "close": [10.5, 11.0],
        "volume": [1000.0, 1500.0],
        "amount": [10500.0, 16500.0],
    })
    market = build_market_data_matrix(panel, field_columns={"amount"})
    strategy = SharedStructureBreakoutMatrixStrategy(scan_fn=scan_fn)

    signals = strategy.compute_signals(market, {})

    assert captured["symbol"] == "1.HK"
    assert captured["market"] == "hk"
    assert captured["rows"][1]["turnover"] == 16500.0
    assert signals.entry[:, 0].tolist() == [0, 1]


def test_matrix_strategy_respects_pattern_switches() -> None:
    captured: dict = {}

    def scan_fn(symbol, market, rows, *, detectors):
        captured["detectors"] = detectors
        return ()

    panel = pl.DataFrame({
        "symbol": ["A.US"],
        "name": ["A.US"],
        "date": [date(2026, 7, 17)],
        "open": [10.0],
        "high": [11.0],
        "low": [9.0],
        "close": [10.5],
        "volume": [1000.0],
        "amount": [10500.0],
    })
    market = build_market_data_matrix(panel, field_columns={"amount"})

    SharedStructureBreakoutMatrixStrategy(scan_fn=scan_fn).compute_signals(
        market,
        {
            "include_long_box": False,
            "include_right_triangle": True,
            "include_symmetric_triangle": False,
        },
    )

    assert [type(item).__name__ for item in captured["detectors"]] == ["RightTriangleDetector"]


@pytest.mark.parametrize(
    ("fixed_pattern", "detector_type"),
    [
        ("symmetric_triangle", SymmetricTriangleDetector),
        ("right_triangle", RightTriangleDetector),
    ],
)
def test_fixed_triangle_uses_only_selected_detector(
    fixed_pattern: str,
    detector_type: type,
) -> None:
    captured: dict = {}

    def scan_fn(symbol, market, rows, *, detectors):
        captured["detectors"] = detectors
        return ()

    panel = pl.DataFrame({
        "symbol": ["A.US"],
        "date": [date(2026, 7, 17)],
        "open": [10.0],
        "high": [11.0],
        "low": [9.0],
        "close": [10.5],
        "volume": [1000.0],
    })
    strategy = SharedStructureBreakoutMatrixStrategy(
        scan_fn=scan_fn,
        fixed_pattern=fixed_pattern,
    )

    strategy.compute_signals(
        build_market_data_matrix(panel),
        {
            "include_long_box": True,
            "include_right_triangle": False,
            "include_symmetric_triangle": False,
        },
    )

    assert len(captured["detectors"]) == 1
    assert isinstance(captured["detectors"][0], detector_type)


def test_custom_strategy_cannot_import_builtin_shared_adapter(tmp_path: Path) -> None:
    fake_builtin_dir = tmp_path / "builtin"
    fake_builtin_dir.mkdir()
    strategy_path = fake_builtin_dir / "custom_strategy.py"
    strategy_path.write_text(
        "from app.strategy.shared_structure_breakout import "
        "SharedStructureBreakoutMatrixStrategy\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="不在策略安全白名单"):
        StrategyEngine._load_file(strategy_path)
