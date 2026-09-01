from __future__ import annotations

from datetime import date, timedelta
from types import SimpleNamespace

import polars as pl
import pytest
from app.services.macd_stages import (
    ARMS_SCHEMA,
    ARMS_WARMUP_BARS,
    MACD_ARMS,
    MACD_INCREMENT_STEPS,
    MACD_PARAMS,
    MIN_OOS_SYMBOLS,
    MIN_OOS_TRADES,
    ROUND_TRIP_COST_BPS,
    STATE_VALUES,
    _metrics,
    _simulate_macd_arm,
    classify_stage,
    evaluate_macd_arms,
    evaluate_macd_stages,
    macd_arm_verdict,
    macd_bootstrap_comparison,
    macd_stages_availability,
)




class _Reader:
    def __init__(self, closes: list[float]):
        self.days = [date(2026, 1, 1) + timedelta(days=index) for index in range(len(closes) + 1)]
        self.frame = pl.DataFrame(
            {
                "symbol": ["600000.SH"] * len(closes),
                "date": self.days[: len(closes)],
                "raw_close": closes,
            }
        )

    def generation(self):
        return "generation-test"

    def manifest_sha256(self):
        return "a" * 64

    def market_days(self, start, end):
        return [day for day in self.days if start <= day <= end]

    def daily_bars(self, symbol, start, end):
        return self.frame.filter((pl.col("date") >= start) & (pl.col("date") <= end))




def test_parameters_and_stage_classifier_are_frozen():
    assert MACD_PARAMS == {"fast": 10, "slow": 20, "signal": 7}
    assert set(STATE_VALUES) == {
        "initial",
        "below_shrink",
        "below_expand",
        "cross_up",
        "above_expand",
        "above_shrink",
        "cross_down",
    }
    assert classify_stage((-1.0, -0.5, -0.5), (-0.4, -0.5, 0.1)) == "cross_up"
    assert classify_stage((0.5, 0.4, 0.1), (0.3, 0.4, -0.1)) == "cross_down"
    assert classify_stage((-1.0, -0.5, -0.5), (-0.8, -0.5, -0.3)) == "below_shrink"
    assert classify_stage((1.0, 0.5, 0.5), (1.2, 0.5, 0.7)) == "above_expand"


def test_evaluate_emits_t_plus_one_and_separate_oos_segments():
    reader = _Reader([10.0 + index * 0.1 for index in range(70)])
    result = evaluate_macd_stages(
        reader,
        start=date(2026, 1, 1),
        end=date(2026, 3, 10),
        symbols=["600000.SH"],
        oos_start=date(2026, 2, 10),
    )

    assert result["status"] == "ok"
    assert result["provenance"]["pinned_reader"] == {
        "generation": "generation-test",
        "manifest_sha256": "a" * 64,
    }
    is_rows = result["segments"]["is"]["rows"]
    oos_rows = result["segments"]["oos"]["rows"]
    assert is_rows and oos_rows
    assert all(row["available_from"] > row["market_date"] for row in is_rows + oos_rows)
    assert all(
        row["pit"]["generation"] == row["generation"] == "generation-test"
        for row in is_rows + oos_rows
    )
    assert max(row["market_date"] for row in is_rows) < date(2026, 2, 10)
    assert min(row["market_date"] for row in oos_rows) >= date(2026, 2, 10)


def test_constant_series_keeps_equal_state_unclassified_after_initial():
    result = evaluate_macd_stages(
        _Reader([10.0] * 50),
        start=date(2026, 1, 1),
        end=date(2026, 2, 18),
        symbols=["600000.SH"],
        oos_start=date(2026, 2, 1),
    )
    rows = result["segments"]["is"]["rows"] + result["segments"]["oos"]["rows"]
    assert rows[0]["state"] == "initial"
    assert all(row["zero_side"] == "zero" for row in rows)
    assert all(row["state"] is None for row in rows[1:])


class _MultiReader(_Reader):
    def __init__(self, series: dict[str, list[float]]):
        length = max(len(values) for values in series.values())
        self.days = [date(2026, 1, 1) + timedelta(days=index) for index in range(length)]
        self.frame = pl.DataFrame(
            [
                {"symbol": symbol, "date": self.days[index], "close": close}
                for symbol, values in series.items()
                for index, close in enumerate(values)
            ]
        )

    def universe(self, start, end):
        return self.frame.get_column("symbol").unique().to_list()

    def daily_bars(self, symbol, start, end):
        return self.frame.filter(
            (pl.col("symbol") == symbol) & (pl.col("date") >= start) & (pl.col("date") <= end)
        )


class _BatchMultiReader(_MultiReader):
    def __init__(self, series: dict[str, list[float]]):
        super().__init__(series)
        self.batch_calls = 0

    def daily_closes(self, start, end):
        self.batch_calls += 1
        return self.frame.filter((pl.col("date") >= start) & (pl.col("date") <= end))

    def daily_bars(self, symbol, start, end):
        raise AssertionError("per-symbol fallback must not run when daily_closes is available")


def _step_series() -> list[float]:
    return (
        [10.0] * 40
        + [10.0 + 0.5 * (index + 1) for index in range(6)]
        + [13.0] * 20
        + [13.0 - 0.5 * (index + 1) for index in range(6)]
        + [10.0] * 40
    )


def test_macd_arms_keep_fair_frozen_baselines_and_costed_metrics():
    closes = _step_series()
    days = [date(2026, 1, 1) + timedelta(days=index) for index in range(len(closes))]
    result = evaluate_macd_arms(
        _MultiReader({"600000.SH": closes}), days[0], days[-1], ["600000.SH"], days[0]
    )
    assert result["schema"] == ARMS_SCHEMA
    assert set(result["arms"]) == {arm.name for arm in MACD_ARMS}
    factor = result["provenance"]["factor_code"]
    assert factor["warmup_bars"] == ARMS_WARMUP_BARS
    assert factor["round_trip_cost_bps"] == ROUND_TRIP_COST_BPS
    assert [item["increment"] for item in result["increments"]] == [
        item[2] for item in MACD_INCREMENT_STEPS
    ]
    tuned = result["segments"]["oos"]["arms"]["tuned_cross"]
    assert {"n_trades_closed", "n_trades_open", "mean_net_return", "wilson_lower"} <= set(tuned)
    assert tuned["n_trades_closed"] == 1


def test_macd_arms_use_single_full_market_close_scan_when_available():
    closes = _step_series()
    reader = _BatchMultiReader({"600000.SH": closes, "000001.SZ": closes})
    days = reader.days
    result = evaluate_macd_arms(
        reader,
        days[0],
        days[-1],
        ["600000.SH", "000001.SZ"],
        days[0],
    )
    assert result["status"] == "ok"
    assert result["segments"]["oos"]["coverage"]["symbols"] == 2
    assert reader.batch_calls == 1


def test_macd_arms_coverage_dates_are_segment_specific():
    closes = _step_series()
    reader = _MultiReader({"600000.SH": closes})
    days = reader.days
    oos_start = days[60]

    result = evaluate_macd_arms(
        reader,
        days[0],
        days[-1],
        ["600000.SH"],
        oos_start,
    )

    is_coverage = result["segments"]["is"]["coverage"]
    oos_coverage = result["segments"]["oos"]["coverage"]
    assert is_coverage["first_market_date"] == days[0]
    assert is_coverage["last_market_date"] == days[59]
    assert oos_coverage["first_market_date"] == oos_start
    assert oos_coverage["last_market_date"] == days[-1]


def test_macd_arms_stage_filters_and_low_sample_verdict_fail_closed():
    closes = _step_series()[:50]
    days = [date(2026, 1, 1) + timedelta(days=index) for index in range(len(closes))]
    result = evaluate_macd_arms(
        _MultiReader({"600000.SH": closes}), days[0], days[-1], ["600000.SH"], days[0]
    )
    for arm in result["arms"].values():
        assert arm["verdict"]["verdict"] == "unavailable"
        assert "unavailable_insufficient_oos_samples" in arm["verdict"]["reasons"]
    assert (
        result["arms"]["tuned_breakout"]["filtered_events"]["oos"].get("cross_above_zero", 0) >= 1
    )


def test_macd_arm_verdict_cost_and_bootstrap_gates_are_independent():
    base = {
        "n_trades_closed": MIN_OOS_TRADES,
        "n_symbols_traded": MIN_OOS_SYMBOLS,
        "mean_net_return": 0.001,
        "wilson_lower": 0.6,
    }
    accepted = macd_arm_verdict(base, {"valid_replicates": 5000, "ci_lower": 0.0001}, base)
    assert accepted["verdict"] == "accepted"
    rejected = macd_arm_verdict(
        {**base, "mean_net_return": -0.0001}, {"valid_replicates": 5000, "ci_lower": 0.0001}, base
    )
    assert rejected["verdict"] == "rejected"
    assert "mean_net_return_not_positive" in rejected["reasons"]
    unavailable = macd_arm_verdict({**base, "n_trades_closed": MIN_OOS_TRADES - 1}, None, base)
    assert unavailable["verdict"] == "unavailable"
    comparison = macd_bootstrap_comparison(
        {f"{index:06d}.SH": [0.02, 0.02] for index in range(10)},
        {f"{index:06d}.SH": [-0.01, -0.01] for index in range(10)},
    )
    assert comparison["valid_replicates"] == 5000
    assert comparison["ci_lower"] > 0


def test_macd_arms_reader_and_empty_input_fail_closed():
    start, end = date(2026, 1, 1), date(2026, 2, 1)
    assert (
        evaluate_macd_arms(None, start, end, ["600000.SH"], date(2026, 1, 15))["status"]
        == "unavailable"
    )
    assert (
        evaluate_macd_arms(object(), start, end, ["600000.SH"], date(2026, 1, 15))["status"]
        == "unavailable"
    )


def test_missing_reader_fails_closed_without_old_not_implemented_reasons():
    availability = macd_stages_availability().as_dict()
    assert availability["status"] == "unavailable"
    assert not any("not_implemented" in reason for reason in availability["reasons"])


def test_macd_arm_execution_has_no_same_bar_lookahead():
    closes = _step_series()
    days = [date(2026, 1, 1) + timedelta(days=index) for index in range(len(closes))]
    series = [
        (index, day, close) for index, (day, close) in enumerate(zip(days, closes, strict=True))
    ]
    simulated = _simulate_macd_arm(MACD_ARMS[1], series, days[0], days[-1])
    assert simulated["trades"]
    for trade in simulated["trades"]:
        assert trade["entry_lag_bars"] >= 1
        if trade["status"] == "closed":
            assert trade["exit_lag_bars"] >= 1


def test_macd_arms_require_adjusted_close_column():
    start, end = date(2026, 1, 1), date(2026, 3, 1)
    result = evaluate_macd_arms(_Reader([10.0] * 60), start, end, ["600000.SH"], date(2026, 2, 1))
    assert result["status"] == "ok"
    assert any(item["code"] == "close_field_missing" for item in result["censored"])
    assert all(item["verdict"]["verdict"] == "unavailable" for item in result["arms"].values())


def test_macd_arms_adjusted_close_is_scale_invariant():
    closes = _step_series()
    days = [date(2026, 1, 1) + timedelta(days=index) for index in range(len(closes))]
    series = [
        (index, day, close) for index, (day, close) in enumerate(zip(days, closes, strict=True))
    ]
    base = _simulate_macd_arm(MACD_ARMS[1], series, days[0], days[-1])
    scaled = _simulate_macd_arm(
        MACD_ARMS[1],
        [(index, day, close * 2.0) for index, day, close in series],
        days[0],
        days[-1],
    )
    assert [
        (trade["status"], trade["entry_exec_date"], trade["exit_exec_date"])
        for trade in base["trades"]
    ] == [
        (trade["status"], trade["entry_exec_date"], trade["exit_exec_date"])
        for trade in scaled["trades"]
    ]
    for original, adjusted in zip(base["trades"], scaled["trades"], strict=True):
        assert adjusted["entry_price"] == pytest.approx(original["entry_price"] * 2.0)
        if original["status"] == "closed":
            assert adjusted["net_return"] == pytest.approx(original["net_return"])


def test_metrics_symbol_gate_counts_closed_trades_only():
    metrics = _metrics(
        [
            {
                "status": "closed",
                "symbol": "600000.SH",
                "net_return": 0.01,
                "gross_return": 0.02,
            },
            {
                "status": "open",
                "symbol": "000001.SZ",
                "net_return": None,
                "gross_return": None,
            },
        ]
    )
    assert metrics["n_trades_closed"] == 1
    assert metrics["n_symbols_traded"] == 1


def test_metrics_include_event_portfolio_diagnostics():
    metrics = _metrics(
        [
            {
                "status": "closed",
                "symbol": "600000.SH",
                "entry_exec_date": date(2026, 1, 5),
                "exit_exec_date": date(2026, 1, 20),
                "net_return": 0.10,
                "gross_return": 0.12,
            },
            {
                "status": "closed",
                "symbol": "000001.SZ",
                "entry_exec_date": date(2026, 1, 5),
                "exit_exec_date": date(2026, 1, 21),
                "net_return": -0.20,
                "gross_return": -0.18,
            },
            {
                "status": "closed",
                "symbol": "600000.SH",
                "entry_exec_date": date(2026, 2, 2),
                "exit_exec_date": date(2026, 2, 10),
                "net_return": 0.02,
                "gross_return": 0.03,
            },
        ]
    )
    # Batch returns: 2026-01-05 -> mean(0.10, -0.20) = -0.05; 2026-02-02 -> 0.02.
    # Equity: 1.0 -> 0.95 -> 0.969 -> max drawdown = 1 - 0.95 = 0.05.
    assert metrics["event_batch_days"] == 2
    assert metrics["max_drawdown_event_equity"] == pytest.approx(0.05)
    assert metrics["sharpe_event_batch"] is not None
    assert metrics["sortino_event_batch"] is not None
    assert metrics["events_per_symbol_per_year"] > 0


def test_metrics_empty_trades_report_null_diagnostics():
    metrics = _metrics([])
    assert metrics["event_batch_days"] == 0
    assert metrics["max_drawdown_event_equity"] is None
    assert metrics["sharpe_event_batch"] is None
    assert metrics["sortino_event_batch"] is None
    assert metrics["events_per_symbol_per_year"] is None


class _IndexReader:
    def __init__(self, closes: list[float]):
        self.days = [date(2026, 1, 1) + timedelta(days=index) for index in range(len(closes))]
        self.frame = pl.DataFrame({"date": self.days, "close": closes})
        self.requests = []

    def read_index_daily(self, request):
        self.requests.append(request)
        rows = self.frame.filter(
            (pl.col("date") >= request["start"]) & (pl.col("date") <= request["end"])
        ).to_dicts()
        return SimpleNamespace(
            legs=[
                SimpleNamespace(
                    code="000001",
                    status="ok",
                    bars=[SimpleNamespace(**row) for row in rows],
                )
            ]
        )


def test_regime_breakdown_buckets_oos_trades_by_index_ma60():
    # 80 flat closes then a strong up-leg: after MA60 warms, regime is bull.
    index_reader = _IndexReader([100.0] * 80 + [100.0 + index * 0.5 for index in range(20)])
    result = evaluate_macd_arms(
        _MultiReader({"600000.SH": _step_series()}),
        date(2026, 1, 1),
        date(2026, 4, 10),
        ["600000.SH"],
        date(2026, 3, 1),
        index_reader=index_reader,
    )
    for name, payload in result["arms"].items():
        breakdown = payload["regime_breakdown_oos"]
        assert breakdown["basis"] == "000001 close vs MA60"
        assert set(breakdown["buckets"]) == {"bull", "bear", "unknown"}
        total = sum(bucket["n_events"] for bucket in breakdown["buckets"].values())
        assert total == result["segments"]["oos"]["arms"][name]["n_events"]
    assert index_reader.requests[0]["codes"] == ["000001"]


def test_regime_breakdown_unavailable_without_index_reader():
    result = evaluate_macd_arms(
        _MultiReader({"600000.SH": _step_series()}),
        date(2026, 1, 1),
        date(2026, 4, 10),
        ["600000.SH"],
        date(2026, 3, 1),
    )
    for payload in result["arms"].values():
        assert payload["regime_breakdown_oos"] == {
            "status": "unavailable",
            "reason": "index_reader_missing_or_insufficient",
        }
