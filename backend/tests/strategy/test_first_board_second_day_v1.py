from __future__ import annotations

import pytest

from app.strategy.specialized.first_board_second_day_v1 import (
    evaluate_first_board_candidates,
)


def _candidate(symbol: str = "A.SZ", **overrides) -> dict:
    row = {
        "symbol": symbol,
        "name": "测试股份",
        "first_board_date": "2026-08-07",
        "consecutive_limit_ups": 1,
        "is_st": False,
        "is_listed": True,
        "first_board_open": 9.0,
        "first_board_close": 11.0,
        "first_board_amount": 100_000_000.0,
        "ma5": 10.5,
        "ma10": 10.2,
        "ma20": 10.0,
        "ma60": 9.5,
        "ma5_previous": 10.4,
        "ma10_previous": 10.1,
        "ma20_previous": 9.9,
    }
    row.update(overrides)
    return row


def _auction(symbol: str = "A.SZ", **overrides) -> dict:
    row = {
        "ts_code": symbol,
        "trade_date": "20260810",
        "price": 10.7,
        "pre_close": 10.0,
        "amount": 10_000_000.0,
    }
    row.update(overrides)
    return row


def test_full_score_requires_first_board_ratio_gap_and_rising_bullish_mas() -> None:
    rows, summary = evaluate_first_board_candidates([_candidate()], [_auction()])
    assert summary["selected_symbols"] == ["A.SZ"]
    assert rows[0]["score"] == 100.0
    assert rows[0]["crossed_mas"] == ["MA5", "MA10", "MA20", "MA60"]


def test_ratio_relaxes_to_twenty_percent_with_linear_score_then_rejects() -> None:
    rows, _ = evaluate_first_board_candidates(
        [_candidate("RELAX.SZ"), _candidate("OVER.SZ")],
        [_auction("RELAX.SZ", amount=16_000_000), _auction("OVER.SZ", amount=20_000_001)],
    )
    by_symbol = {row["symbol"]: row for row in rows}
    assert by_symbol["RELAX.SZ"]["decision"] == "SELECTED"
    assert by_symbol["RELAX.SZ"]["score_components"]["auction_amount_ratio"] == 15.0
    assert by_symbol["OVER.SZ"]["decision"] == "REJECTED"
    assert "超过淘汰上限" in by_symbol["OVER.SZ"]["failure_reasons"][0]


def test_gap_and_rising_ma_are_hard_gates_but_cross_is_only_bonus() -> None:
    rows, _ = evaluate_first_board_candidates(
        [
            _candidate("GAP.SZ"),
            _candidate("MA.SZ", ma5_previous=10.6),
            _candidate("BONUS.SZ", first_board_open=10.4),
        ],
        [_auction("GAP.SZ", price=10.5), _auction("MA.SZ"), _auction("BONUS.SZ")],
    )
    by_symbol = {row["symbol"]: row for row in rows}
    assert by_symbol["GAP.SZ"]["decision"] == "REJECTED"
    assert by_symbol["MA.SZ"]["decision"] == "REJECTED"
    assert by_symbol["BONUS.SZ"]["decision"] == "SELECTED"
    assert by_symbol["BONUS.SZ"]["score"] == 80.0


def test_missing_and_duplicate_records_are_explicit() -> None:
    rows, summary = evaluate_first_board_candidates([_candidate()], [])
    assert summary["selected_count"] == 0
    assert "缺少9:25集合竞价记录" in rows[0]["failure_reasons"]
    with pytest.raises(ValueError, match="duplicate auction symbol"):
        evaluate_first_board_candidates([_candidate()], [_auction(), _auction()])
    with pytest.raises(ValueError, match="duplicate candidate symbol"):
        evaluate_first_board_candidates([_candidate(), _candidate()], [_auction()])
