from __future__ import annotations

from datetime import date, timedelta
from types import SimpleNamespace

import polars as pl

from app.services.single_yang_no_break import (
    DEFAULT_HOLD_HORIZONS,
    DEFAULT_WINDOW,
    Bar,
    SingleYangCompositeReader,
    detect_single_yang,
    evaluate_single_yang,
    evaluate_single_yang_increment,
    is_single_yang,
    run_single_yang_research,
)


def test_exact_two_percent_body_is_inclusive():
    assert is_single_yang(Bar(open=10.0, high=10.3, low=9.9, close=10.2))
    assert not is_single_yang(Bar(open=10.0, high=10.2, low=9.9, close=10.199999999))


def test_equal_low_is_touch_not_break_and_window_must_be_complete():
    bars = [Bar(10.0, 10.5, 9.5, 10.3)] + [
        Bar(10.0, 10.4, 9.5, 10.1) for _ in range(DEFAULT_WINDOW)
    ]
    assert detect_single_yang(bars) == [0]
    assert detect_single_yang(bars[:-1]) == []
    broken = list(bars)
    broken[3] = Bar(10.0, 10.4, 9.49, 10.1)
    assert detect_single_yang(broken) == []


class _Reader:
    def __init__(self, *, raw_open=True, generation_mismatch=False, limit_fact=True):
        self.days = [date(2026, 1, 1) + timedelta(days=index) for index in range(12)]
        rows = []
        for index, day in enumerate(self.days[:-1]):
            if index == 0:
                raw_open_value, raw_close = 10.0, 10.3
            else:
                raw_open_value, raw_close = 10.1, 10.15 + index * 0.01
            row = {
                "symbol": "600000.SH",
                "date": day,
                "raw_open": raw_open_value,
                "raw_high": max(raw_open_value, raw_close) + 0.1,
                "raw_low": 9.5 if index <= 5 else 9.6,
                "raw_close": raw_close,
                "open": raw_open_value,
                "close": raw_close,
            }
            if limit_fact:
                previous_close = 10.3 if index == 0 else 10.15 + (index - 1) * 0.01
                row["limit_up_price"] = round(previous_close * 1.1, 2)
            if generation_mismatch and index == 2:
                row["generation"] = "other-generation"
            rows.append(row)
        self.frame = pl.DataFrame(rows)
        self._columns = [
            column for column in self.frame.columns if raw_open or column != "raw_open"
        ]
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


class _MarketFacts:
    def __init__(self, values):
        self.values = values

    def generation(self):
        return "markets-generation"

    def manifest_sha256(self):
        return "c" * 64

    def limit_band_facts(self, symbol, start, end):
        del symbol
        return {
            day: SimpleNamespace(published_limit_up=value)
            for day, value in self.values.items()
            if start <= day <= end
        }


def _evaluate(reader):
    return evaluate_single_yang(
        reader=reader,
        start=date(2026, 1, 1),
        end=date(2026, 1, 10),
        symbols=["600000.SH"],
        oos_start=date(2026, 1, 7),
        cost_bps=10.0,
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


def test_increment_missing_reader_is_explicitly_unavailable():
    result = evaluate_single_yang_increment(
        reader=None,
        start=date(2026, 1, 1),
        end=date(2026, 1, 10),
        symbols=["600000.SH"],
        oos_start=date(2026, 1, 5),
    )
    assert result["status"] == "unavailable"
    assert result["reasons"] == ["generation_pinned_reader_missing"]
    assert result["research_id"] == "single_yang_no_break_increment_v1"


def test_increment_reports_long_horizon_censoring_and_provenance():
    result = evaluate_single_yang_increment(
        reader=_Reader(),
        start=date(2026, 1, 1),
        end=date(2026, 1, 10),
        symbols=["600000.SH"],
        oos_start=date(2026, 1, 7),
    )
    assert result["status"] == "ok"
    assert result["provenance"]["pristine_oos"] is False
    assert result["provenance"]["oos_nature"].endswith("not_pristine")
    assert set(result["provenance"]["hold_horizons"]) == set(DEFAULT_HOLD_HORIZONS)
    horizon_60 = result["coverage"]["horizons"]["pattern"]["oos"]["60"]
    assert horizon_60["censored"] >= 1


def test_increment_exposes_t_plus_one_open_and_cost_adjusted_holding():
    result = evaluate_single_yang_increment(
        reader=_Reader(),
        start=date(2026, 1, 1),
        end=date(2026, 1, 10),
        symbols=["600000.SH"],
        oos_start=date(2026, 1, 7),
        cost_bps=10.0,
    )
    event = result["arms"]["pattern"]["events"][0]
    assert event["available_from"] == date(2026, 1, 7)
    assert event["entry_price"] == 10.1
    assert event["entry_reachable"] is True
    holding = next(item for item in event["holdings"] if item["horizon"] == 1)
    assert abs(holding["gross_return"] - (10.22 / 10.1 - 1.0)) < 1e-12
    assert holding["post_cost_return"] == holding["gross_return"] - 0.001
    assert result["verdict"]["value"] == "unavailable"
    assert any("not_gated" in reason for reason in result["verdict"]["reasons"])


def test_increment_baseline_and_verdict_fail_closed_without_limit_fact():
    result = evaluate_single_yang_increment(
        reader=_Reader(limit_fact=False),
        start=date(2026, 1, 1),
        end=date(2026, 1, 10),
        symbols=["600000.SH"],
        oos_start=date(2026, 1, 7),
    )
    assert result["arms"]["baseline"]["status"] == "unavailable"
    assert result["arms"]["baseline"]["reasons"] == ["limit_up_price_fact_missing"]
    assert result["arms"]["baseline"]["events"] == []
    event = result["arms"]["pattern"]["events"][0]
    assert event["entry_reachable"] is None
    assert event["holdings"] == []
    assert result["comparison"]["1"]["oos"]["gate"] is False
    assert result["verdict"]["value"] == "unavailable"
    assert "baseline_limit_up_price_fact_missing" in result["verdict"]["reasons"]


def test_increment_missing_adjusted_prices_is_unavailable():
    reader = _Reader()
    reader.frame = reader.frame.drop("close")
    reader._columns = list(reader.frame.columns)
    result = evaluate_single_yang_increment(
        reader=reader,
        start=date(2026, 1, 1),
        end=date(2026, 1, 10),
        symbols=["600000.SH"],
        oos_start=date(2026, 1, 7),
    )
    assert result["status"] == "unavailable"
    assert result["reasons"] == ["adjusted_price_columns_missing"]
    assert result["missing_columns"] == ["close"]


def test_composite_reader_attaches_pinned_limit_facts():
    canonical = _Reader(limit_fact=False)
    values = {
        row["date"]: round(float(row["raw_close"]) * 1.1, 2) for row in canonical.frame.to_dicts()
    }
    composite = SingleYangCompositeReader(canonical, _MarketFacts(values))
    frame = composite.daily_bars("600000.SH", canonical.days[0], canonical.days[-1])
    assert "limit_up_price" in composite.columns()
    assert frame.get_column("limit_up_price").null_count() == 0
    assert composite.source_provenance()["market_facts"]["generation"] == "markets-generation"


def test_first_board_missing_entry_bar_is_censored_not_crashed():
    reader = _Reader()
    board_day, missing_entry_day = reader.days[1], reader.days[2]
    reader.frame = reader.frame.with_columns(
        pl.when(pl.col("date") == board_day)
        .then(pl.col("limit_up_price"))
        .otherwise(pl.col("raw_close"))
        .alias("raw_close")
    ).filter(pl.col("date") != missing_entry_day)
    reader._columns = list(reader.frame.columns)
    result = evaluate_single_yang_increment(
        reader=reader,
        start=date(2026, 1, 1),
        end=date(2026, 1, 10),
        symbols=["600000.SH"],
        oos_start=date(2026, 1, 7),
    )
    assert any(
        item["code"] == "forward_bar_missing" and item["anchor_date"] == board_day
        for item in result["censored"]
    )


def test_second_consecutive_limit_is_not_first_board():
    reader = _Reader()
    first, second = reader.days[:2]
    reader.frame = reader.frame.with_columns(
        pl.when(pl.col("date").is_in([first, second]))
        .then(pl.col("limit_up_price"))
        .otherwise(pl.col("raw_close"))
        .alias("raw_close")
    )
    reader._columns = list(reader.frame.columns)
    result = evaluate_single_yang_increment(
        reader=reader,
        start=date(2026, 1, 1),
        end=date(2026, 1, 10),
        symbols=["600000.SH"],
        oos_start=date(2026, 1, 7),
    )
    assert all(event["board_date"] != second for event in result["arms"]["baseline"]["events"])
