from __future__ import annotations

from datetime import date, timedelta

import polars as pl

from app.services.single_yang_no_break import (
    Bar,
    DEFAULT_WINDOW,
    detect_single_yang,
    evaluate_single_yang,
    is_single_yang,
    run_single_yang_research,
)


def test_exact_two_percent_body_is_inclusive():
    assert is_single_yang(Bar(open=10.0, high=10.3, low=9.9, close=10.2))
    assert not is_single_yang(Bar(open=10.0, high=10.2, low=9.9, close=10.199999999))


def test_equal_low_is_touch_not_break_and_window_must_be_complete():
    bars = [Bar(10.0, 10.5, 9.5, 10.3)] + [Bar(10.0, 10.4, 9.5, 10.1) for _ in range(DEFAULT_WINDOW)]
    assert detect_single_yang(bars) == [0]
    assert detect_single_yang(bars[:-1]) == []
    broken = list(bars)
    broken[3] = Bar(10.0, 10.4, 9.49, 10.1)
    assert detect_single_yang(broken) == []


class _Reader:
    def __init__(self, *, raw_open=True, generation_mismatch=False):
        self.days = [date(2026, 1, 1) + timedelta(days=index) for index in range(12)]
        rows = []
        for index, day in enumerate(self.days[:-1]):
            if index == 0:
                raw_open_value, raw_close = 10.0, 10.3
            else:
                raw_open_value, raw_close = 10.1, 10.15 + index * 0.01
            row = {
                "symbol": "600000.SH", "date": day,
                "raw_open": raw_open_value, "raw_high": max(raw_open_value, raw_close) + 0.1,
                "raw_low": 9.5 if index <= 5 else 9.6, "raw_close": raw_close,
            }
            if generation_mismatch and index == 2:
                row["generation"] = "other-generation"
            rows.append(row)
        self.frame = pl.DataFrame(rows)
        self._columns = [column for column in self.frame.columns if raw_open or column != "raw_open"]
        if not raw_open:
            self.frame = self.frame.drop("raw_open").with_columns(pl.lit(10.0).alias("open"))

    def generation(self):
        return "generation-test"

    def manifest_sha256(self):
        return "b" * 64

    def columns(self):
        return self._columns

    def market_days(self, start, end):
        return [day for day in self.days if start <= day <= end]

    def daily_bars(self, symbol, start, end):
        return self.frame.filter((pl.col("date") >= start) & (pl.col("date") <= end))


def _evaluate(reader):
    return evaluate_single_yang(
        reader=reader,
        start=date(2026, 1, 1), end=date(2026, 1, 10),
        symbols=["600000.SH"], oos_start=date(2026, 1, 7), cost_bps=10.0,
    )


def test_complete_reader_emits_t_plus_five_confirmation_and_t_plus_six_evaluation():
    result = _evaluate(_Reader())
    assert result["status"] == "ok"
    event = result["events"][0]
    assert event["confirm_date"] == date(2026, 1, 6)
    assert event["available_from"] == date(2026, 1, 7)
    assert event["segment"] == "oos"
    assert event["forward"]["post_cost_return"] == event["forward"]["gross_return"] - 0.001
    assert result["provenance"]["generation"] == "generation-test"


def test_old_generation_without_raw_open_fails_closed_and_never_uses_adjusted_open():
    result = _evaluate(_Reader(raw_open=False))
    assert result["status"] == "unavailable"
    assert result["reasons"] == ["raw_generation_columns_missing"]
    assert result["missing_columns"] == ["raw_open"]
    assert result["events"] == []


def test_generation_mismatch_is_censored():
    result = _evaluate(_Reader(generation_mismatch=True))
    assert result["status"] == "ok"
    assert result["events"] == []
    assert result["censored"][0]["code"] == "generation_mismatch"


def test_missing_market_day_inside_confirmation_window_is_censored():
    reader = _Reader()
    reader.frame = reader.frame.filter(pl.col("date") != date(2026, 1, 3))

    result = _evaluate(reader)

    assert result["events"] == []
    assert result["censored"][0]["code"] == "confirmation_window_incomplete"
    assert result["censored"][0]["missing_market_days"] == [date(2026, 1, 3)]


def test_capability_endpoint_has_only_real_reader_gap():
    result = run_single_yang_research()
    assert result["status"] == "unavailable"
    assert result["reasons"] == ["generation_pinned_reader_missing"]
    assert not any("not_implemented" in reason for reason in result["reasons"])
