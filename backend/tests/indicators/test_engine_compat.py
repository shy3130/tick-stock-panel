import json
import math
from datetime import date, timedelta
from pathlib import Path

import polars as pl

from app.indicators.engine_compat import (
    ENGINE_COMPAT_COLUMNS,
    ENGINE_COMPAT_LIVE_STATE_COLUMNS,
    build_engine_compat_live_state,
    compute_engine_compat_indicators,
    compute_engine_compat_today,
)


_FIXTURE_PATH = Path(__file__).parents[1] / "fixtures" / "engine_technicals_compat_v1.json"


def _synth_ohlcv(rows: int = 360, symbol: str = "000001.SZ") -> pl.DataFrame:
    data: list[dict[str, object]] = []
    for i in range(rows):
        base = 10.0 + i * 0.05
        amplitude = 0.4 * math.sin(i / 3.0)
        close = base + amplitude
        open_ = base + 0.05 + amplitude * 0.5
        data.append({
            "symbol": symbol,
            "date": date(2026, 1, 1) + timedelta(days=i),
            "open": open_,
            "high": max(open_, close) + 0.15,
            "low": min(open_, close) - 0.15,
            "close": close,
            "volume": float(1000 + i * 7),
        })
    return pl.DataFrame(data)


def _fixture() -> dict[str, object]:
    with _FIXTURE_PATH.open(encoding="utf-8") as fixture_file:
        return json.load(fixture_file)


def _assert_fixture_parity(result: pl.DataFrame, fixture: dict[str, object]) -> None:
    indicators = fixture["indicators"]
    assert isinstance(indicators, dict)
    assert set(ENGINE_COMPAT_COLUMNS) == set(indicators)

    for column, expected_values in indicators.items():
        actual_values = result[column].to_list()
        assert len(actual_values) == len(expected_values), column
        for index, (actual, expected) in enumerate(zip(actual_values, expected_values, strict=True)):
            if expected is None:
                assert actual is None, f"{column}[{index}] = {actual!r}, expected null"
            else:
                assert actual is not None, f"{column}[{index}] is null"
                assert math.isclose(actual, expected, rel_tol=1e-9, abs_tol=1e-8), (
                    f"{column}[{index}] = {actual!r}, expected {expected!r}"
                )


def test_engine_compat_matches_engine_golden_fixture() -> None:
    fixture = _fixture()
    assert fixture["version"] == 1
    result = compute_engine_compat_indicators(_synth_ohlcv(int(fixture["rows"])))

    _assert_fixture_parity(result, fixture)

    latest = result.tail(1).to_dicts()[0]
    for column in ENGINE_COMPAT_COLUMNS:
        assert latest[column] is not None, column
    assert latest["xsii_upper"] >= latest["xsii_mid"] >= latest["xsii_lower"]
    assert latest["ktn_upper"] >= latest["ktn_mid"] >= latest["ktn_lower"]
    assert latest["taq_upper"] >= latest["taq_mid"] >= latest["taq_lower"]


def test_engine_compat_is_symbol_isolated_and_nulls_invalid_arithmetic() -> None:
    left = _synth_ohlcv(symbol="A")
    right = _synth_ohlcv(symbol="B").with_columns([
        (pl.col("open") * 10).alias("open"),
        (pl.col("high") * 10).alias("high"),
        (pl.col("low") * 10).alias("low"),
        (pl.col("close") * 10).alias("close"),
        (pl.col("volume") * 2).alias("volume"),
    ])
    combined = compute_engine_compat_indicators(pl.concat([left, right]).sample(fraction=1.0, shuffle=True, seed=7))
    expected_left = compute_engine_compat_indicators(left)

    for column in ENGINE_COMPAT_COLUMNS:
        assert combined.filter(pl.col("symbol") == "A")[column].to_list() == expected_left[column].to_list(), column

    flat = pl.DataFrame({
        "symbol": ["FLAT"] * 30,
        "date": [date(2026, 1, 1) + timedelta(days=i) for i in range(30)],
        "open": [10.0] * 30,
        "high": [10.0] * 30,
        "low": [10.0] * 30,
        "close": [10.0] * 30,
        "volume": [0.0] * 30,
    })
    invalid = compute_engine_compat_indicators(flat)
    for column in ENGINE_COMPAT_COLUMNS:
        values = invalid[column]
        assert values.is_nan().sum() == 0, column
        assert values.is_infinite().sum() == 0, column


def test_engine_compat_obv_and_asi_are_cumulative() -> None:
    result = compute_engine_compat_indicators(_synth_ohlcv())
    rows = result.select("close", "volume", "obv", "asi").to_dicts()
    for previous, current in zip(rows, rows[1:], strict=False):
        expected_obv_delta = (
            current["volume"] if current["close"] > previous["close"]
            else -current["volume"] if current["close"] < previous["close"]
            else 0.0
        )
        assert math.isclose(current["obv"] - previous["obv"], expected_obv_delta, abs_tol=1e-9)
        assert math.isfinite(current["asi"])


def test_engine_compat_live_last_row_matches_history_recomputation() -> None:
    all_rows = _synth_ohlcv()
    history = all_rows.head(359)
    today = all_rows.tail(1)
    live_state = build_engine_compat_live_state(history, history["date"][-1])

    assert set(ENGINE_COMPAT_LIVE_STATE_COLUMNS).issubset(live_state.columns)
    live = compute_engine_compat_today(live_state, today)
    full = compute_engine_compat_indicators(all_rows).tail(1)

    assert live.height == 1
    for column in ENGINE_COMPAT_COLUMNS:
        actual = live[column].item()
        expected = full[column].item()
        if expected is None:
            assert actual is None, column
        else:
            assert actual is not None, column
            assert math.isclose(actual, expected, rel_tol=1e-9, abs_tol=1e-8), column

    missing_state = live_state.drop(next(iter(ENGINE_COMPAT_LIVE_STATE_COLUMNS)))
    assert compute_engine_compat_today(missing_state, today).is_empty()
