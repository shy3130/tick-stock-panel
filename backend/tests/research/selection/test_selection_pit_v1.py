from __future__ import annotations

from datetime import date
from types import SimpleNamespace

import numpy as np
import polars as pl
import pytest

from research.selection.run_selection_forward_watch_v1 import _summarize_rows
from research.selection.run_selection_mvp_v2 import _historical_st_mask
from research.selection.run_selection_pit_v1 import _delta, _forward_protocol


def _write_status(root, label: str, symbols: list[str]) -> None:
    target = root / f"date={label}" / "part.parquet"
    target.parent.mkdir(parents=True)
    pl.DataFrame(
        {
            "symbol": symbols,
            "trade_date": [date.fromisoformat(label)] * len(symbols),
        }
    ).write_parquet(target)


def test_historical_st_mask_requires_complete_daily_coverage(tmp_path):
    market = SimpleNamespace(
        shape=(2, 2),
        timestamp_labels=("2026-08-10", "2026-08-11"),
        symbols=("000001.SZ", "000002.SZ"),
    )
    _write_status(tmp_path, "2026-08-10", ["000001.SZ"])

    with pytest.raises(ValueError, match="coverage missing 1 sessions"):
        _historical_st_mask(
            market=market,
            root=tmp_path,
            required_start=date(2026, 8, 10),
            required_end=date(2026, 8, 11),
        )


def test_historical_st_mask_maps_symbols_without_current_name_proxy(tmp_path):
    market = SimpleNamespace(
        shape=(2, 2),
        timestamp_labels=("2026-08-10", "2026-08-11"),
        symbols=("000001.SZ", "000002.SZ"),
    )
    _write_status(tmp_path, "2026-08-10", ["000001.SZ", "999999.SZ"])
    _write_status(tmp_path, "2026-08-11", ["000002.SZ"])

    mask, status = _historical_st_mask(
        market=market,
        root=tmp_path,
        required_start=date(2026, 8, 10),
        required_end=date(2026, 8, 11),
    )

    np.testing.assert_array_equal(mask, [[True, False], [False, True]])
    assert status["coverage_complete"] is True
    assert status["rows"] == 3
    assert status["matched_axis_rows"] == 2


def test_pit_delta_and_forward_protocol_do_not_promote():
    delta = _delta(
        {"mean_net_return": -0.01, "cohort_win_rate": 0.4},
        {"mean_net_return": -0.005, "cohort_win_rate": 0.5},
    )
    protocol = _forward_protocol(calibration_end=date(2026, 8, 11), pit_report_hash="abc")

    assert delta["mean_net_return"] == 0.005
    assert delta["cohort_win_rate"] == 0.1
    assert protocol["observed_sessions"] == 0
    assert protocol["auto_promote"] is False
    assert protocol["status"] == "PENDING_DATA"


def test_forward_watch_uses_only_post_cutoff_top10_rows():
    rows = [
        {"signal_date": "2026-08-11", "rank": "1", "return_5d_net": "0.50"},
        {"signal_date": "2026-08-12", "rank": "1", "return_5d_net": "0.02"},
        {"signal_date": "2026-08-12", "rank": "2", "return_5d_net": "-0.01"},
        {"signal_date": "2026-08-12", "rank": "11", "return_5d_net": "1.00"},
        {"signal_date": "2026-08-13", "rank": "1", "return_5d_net": ""},
    ]

    result = _summarize_rows(rows, cutoff=date(2026, 8, 11))

    assert result["observed_sessions"] == 2
    assert result["usable_cohorts"] == 1
    assert result["mean_5d_net_return"] == 0.005
    assert result["cohort_win_rate"] == 1.0
    assert result["stock_win_rate"] == 0.5
