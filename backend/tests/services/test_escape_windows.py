from datetime import date

import polars as pl
import pytest
from pydantic import ValidationError

from app.services.escape_windows import (
    ALL_A,
    EscapeWindowsRequest,
    _complete_forward_return,
    _leg_returns,
    benjamini_hochberg_adjusted,
    evaluate_escape_windows,
    exact_binomial_two_sided_p,
    holm_adjusted,
    resolve_year_anchor,
    sign_flip_permutation_p,
)


def test_exact_binomial_and_multiplicity_adjustments():
    assert exact_binomial_two_sided_p(19, 20) == pytest.approx(42 / 2**20)
    assert exact_binomial_two_sided_p(10, 10) == pytest.approx(2 / 1024)
    assert holm_adjusted({"a": 0.01, "b": 0.04, "c": 0.03}) == {"a": 0.03, "b": 0.06, "c": 0.06}
    assert benjamini_hochberg_adjusted({"a": 0.01, "b": 0.04, "c": 0.03}) == {
        "a": 0.03,
        "b": 0.04,
        "c": 0.04,
    }


def test_sign_flip_is_exact_for_small_samples():
    vals = [0.02, -0.01, 0.03]
    observed = abs(sum(vals))
    count = 0
    for signs in __import__("itertools").product((-1.0, 1.0), repeat=len(vals)):
        if abs(sum(a * b for a, b in zip(vals, signs, strict=False))) >= observed - 1e-10:
            count += 1
    assert sign_flip_permutation_p(vals) == pytest.approx(count / 2 ** len(vals))


def test_holiday_anchor_and_pandemic_extension_rules():
    days = [date(2020, 1, d) for d in range(1, 24) if date(2020, 1, d).weekday() < 5]
    days += [date(2020, 2, d) for d in range(3, 15) if date(2020, 2, d).weekday() < 5]
    anchor, reason = resolve_year_anchor(days, 2020, "pre_spring_festival")
    assert anchor == date(2020, 1, 17) and reason is None


def test_request_is_strict():
    with pytest.raises(ValidationError):
        EscapeWindowsRequest.model_validate(
            {"start": date(2020, 1, 1), "end": date(2020, 2, 1), "extra": 1}
        )


class _Reader:
    def __init__(self, days):
        self.days = days
        self.frame = pl.DataFrame(
            [
                {"symbol": "A.SH", "date": d, "close": 100.0 + i, "volume": 100.0}
                for i, d in enumerate(days)
            ]
        )

    def generation(self):
        return "canonical-test"

    def manifest_sha256(self):
        return "a" * 64

    def has_columns(self, *columns):
        return True

    def market_days(self, start, end):
        return [d for d in self.days if start <= d <= end]

    def daily_closes(self, start, end):
        return self.frame.filter((pl.col("date") >= start) & (pl.col("date") <= end))


class _Calendar:
    def __init__(self, days):
        self.days = days

    def version(self):
        return "calendar-test"

    def market_days(self, start, end):
        return [d for d in self.days if start <= d <= end]


def test_missing_index_is_per_leg_unavailable_but_all_a_runs():
    days = [date(2020, 12, d) for d in range(15, 21)]
    reader, calendar = _Reader(days), _Calendar(days)
    result = evaluate_escape_windows(
        EscapeWindowsRequest(start=date(2020, 1, 1), end=date(2020, 12, 31)),
        canonical_reader=reader,
        calendar=calendar,
    )
    assert result["status"] == "ok"
    all_a = next(x for x in result["legs"] if x["leg"] == ALL_A)
    assert all_a["status"] == "ok"
    assert all(x["status"] == "unavailable" for x in result["legs"] if x["leg"] != ALL_A)
    assert result["provenance"]["seeds"]["bootstrap"] == 42


def test_index_returns_require_adjacent_market_days():
    days = [date(2025, 1, 2), date(2025, 1, 3), date(2025, 1, 6)]
    bars = [
        {"date": days[0], "close": 100.0},
        {"date": days[2], "close": 121.0},
    ]

    returns, leg_days = _leg_returns(bars, {day: index for index, day in enumerate(days)})

    assert returns == {}
    assert leg_days == {days[0], days[2]}


def test_forward_return_requires_every_market_day_in_horizon():
    days = [
        date(2025, 1, 2),
        date(2025, 1, 3),
        date(2025, 1, 6),
        date(2025, 1, 7),
    ]
    incomplete, reason = _complete_forward_return(
        {days[1]: 0.01, days[3]: 0.02},
        days,
        0,
        3,
    )
    complete, complete_reason = _complete_forward_return(
        {days[1]: 0.01, days[2]: -0.02, days[3]: 0.03},
        days,
        0,
        3,
    )

    assert incomplete is None
    assert reason == "LEG_SPAN_INCOMPLETE"
    assert complete == pytest.approx(1.01 * 0.98 * 1.03 - 1.0)
    assert complete_reason is None
