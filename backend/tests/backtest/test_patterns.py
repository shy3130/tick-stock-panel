from datetime import date, timedelta

import polars as pl

from app.backtest.patterns import (
    detect_breakout,
    detect_consolidation,
    detect_double_bottom,
    detect_double_top,
    find_pivots,
)


def test_find_pivots_handles_simple_peak_and_nulls():
    df = pl.DataFrame({
        "date": [date(2026, 1, 1) + timedelta(days=i) for i in range(5)],
        "high": [1.0, 2.0, 3.0, 2.0, 1.0],
        "low": [1.0, 0.8, 0.5, 0.8, 1.0],
    })

    pivots = find_pivots(df)

    assert {"type": "high", "date": "2026-01-03", "price": 3.0, "strength": pivots[0]["strength"]} in pivots
    assert {"type": "low", "date": "2026-01-03", "price": 0.5, "strength": pivots[1]["strength"]} in pivots


def test_find_pivots_handles_three_point_peak_with_window_three():
    df = pl.DataFrame({
        "date": [date(2026, 1, 1) + timedelta(days=i) for i in range(3)],
        "high": [1.0, 3.0, 1.0],
        "low": [1.0, 0.8, 1.0],
    })

    assert any(p["type"] == "high" and p["price"] == 3.0 for p in find_pivots(df, window=3))


def test_find_pivots_skips_null_windows():
    df = pl.DataFrame({
        "date": [date(2026, 1, 1) + timedelta(days=i) for i in range(5)],
        "high": [1.0, 2.0, None, 2.0, 1.0],
        "low": [1.0, 0.8, 0.5, 0.8, 1.0],
    })

    assert find_pivots(df) == []


def test_monotonic_sequence_has_no_pivots():
    df = pl.DataFrame({
        "date": [date(2026, 1, 1) + timedelta(days=i) for i in range(8)],
        "high": [float(i) for i in range(8)],
        "low": [float(i) for i in range(8)],
    })

    assert find_pivots(df) == []


def test_breakout_and_consolidation_detect_fixed_sequences():
    rows = []
    for i in range(61):
        close = 10.0 if i < 60 else 11.5
        rows.append({"date": date(2026, 1, 1) + timedelta(days=i), "open": close, "high": 10.5 if i < 60 else 11.6, "low": 9.8, "close": close, "volume": 1000 + i})
    df = pl.DataFrame(rows)

    assert detect_breakout(df).pattern == "breakout"

    flat = pl.DataFrame([
        {"date": date(2026, 3, 1) + timedelta(days=i), "open": 10.0, "high": 10.2, "low": 9.8, "close": 10.0, "volume": 1000}
        for i in range(20)
    ])
    assert detect_consolidation(flat).pattern == "consolidation"


def test_double_bottom_requires_neckline_rebound():
    prices = [12, 10, 9, 10, 12, 11, 10, 9.2, 10.5, 12.2]
    df = pl.DataFrame([
        {"date": date(2026, 1, 1) + timedelta(days=i), "open": p, "high": p * 1.01, "low": p * 0.99, "close": p, "volume": 1000}
        for i, p in enumerate(prices)
    ])

    hit = detect_double_bottom(df)

    assert hit is not None
    assert hit.pattern == "double_bottom"


def test_double_top_requires_neckline_break():
    prices = [9, 12, 13, 12, 10, 11, 12, 12.8, 11.5, 9.6]
    df = pl.DataFrame([
        {"date": date(2026, 1, 1) + timedelta(days=i), "open": p, "high": p * 1.01, "low": p * 0.99, "close": p, "volume": 1000}
        for i, p in enumerate(prices)
    ])

    hit = detect_double_top(df)

    assert hit is not None
    assert hit.pattern == "double_top"
