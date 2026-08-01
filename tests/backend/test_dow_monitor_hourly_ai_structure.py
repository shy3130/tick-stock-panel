from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from app.services.dow_monitor_hourly_ai_structure import (
    PreviousStageContext,
    build_hourly_market_structure,
    classify_opportunity_change,
)
from app.services.dow_monitor_half_hour_ai_snapshot import HalfHourAiSnapshotBuilder


BEIJING = ZoneInfo("Asia/Shanghai")


def at(hour: int, minute: int) -> datetime:
    return datetime(2026, 7, 31, hour, minute, tzinfo=BEIJING)


def row(
    observed_at: datetime,
    *,
    open_price: float,
    close: float,
    high: float | None = None,
    low: float | None = None,
    volume: float = 100,
) -> dict:
    return {
        "decision_minute": observed_at,
        "minute_open": open_price,
        "minute_high": high if high is not None else max(open_price, close),
        "minute_low": low if low is not None else min(open_price, close),
        "minute_close": close,
        "minute_volume": volume,
    }


def path_rows(closes: list[float], *, start: datetime | None = None) -> list[dict]:
    cursor = start or at(10, 0)
    result: list[dict] = []
    previous = 100.0
    for index, close in enumerate(closes):
        result.append(
            row(
                cursor + timedelta(minutes=index),
                open_price=previous,
                close=close,
                high=max(previous, close) + 0.1,
                low=min(previous, close) - 0.1,
            )
        )
        previous = close
    return result


def build(rows: list[dict], *, previous: PreviousStageContext | None = None):
    return build_hourly_market_structure(
        minute_rows=rows,
        stage_start=at(10, 0),
        data_cutoff=at(10, 30),
        previous_stage=previous,
    )


def test_v_repair_requires_early_low_half_recovery_and_close_above_vwap() -> None:
    result = build(path_rows([99.5, 98.5, 97.0, 95.0, 96.0, 97.0, 98.0, 99.0, 100.5]))

    pattern = next(item for item in result.patterns if item.kind == "V_REPAIR")
    assert pattern.status == "CONFIRMED"
    assert pattern.pivot_time == at(10, 3)
    assert pattern.evidence_metric_keys == (
        "stage.low",
        "stage.close",
        "stage.vwap",
        "stage.v_recovery_ratio",
    )
    assert result.evidence_values["stage.v_recovery_ratio"] == pytest.approx(5.6 / 5.1)


def test_inverted_v_identifies_early_high_and_failed_hold() -> None:
    result = build(path_rows([100.5, 102.0, 104.0, 105.0, 104.0, 103.0, 102.0, 101.0, 99.5]))

    pattern = next(item for item in result.patterns if item.kind == "INVERTED_V")
    assert pattern.status == "CONFIRMED"
    assert pattern.pivot_time == at(10, 3)
    assert result.stage.close < result.stage.vwap


@pytest.mark.parametrize(
    ("last_close", "expected_kind", "expected_status"),
    [
        (101.2, "BREAKOUT", "CONFIRMED"),
        (99.8, "FALSE_BREAKOUT", "CONFIRMED"),
    ],
)
def test_breakout_classification_uses_pre_stage_high(
    last_close: float,
    expected_kind: str,
    expected_status: str,
) -> None:
    rows = [
        row(at(9, 58), open_price=99.0, close=99.5, high=99.7, low=98.8),
        row(at(9, 59), open_price=99.5, close=99.8, high=100.0, low=99.4),
        row(at(10, 0), open_price=99.8, close=100.5, high=101.0, low=99.7),
        row(at(10, 1), open_price=100.5, close=last_close, high=101.3, low=99.7),
    ]

    result = build(rows)

    pattern = next(item for item in result.patterns if item.kind == expected_kind)
    assert pattern.status == expected_status
    assert result.evidence_values["reference.pre_stage_high"] == 100.0


@pytest.mark.parametrize(
    ("closes", "expected"),
    [
        ([100.2, 100.5, 100.8, 101.0, 101.3, 101.6, 101.9, 102.1, 102.4], "UP"),
        ([99.8, 99.5, 99.2, 99.0, 98.7, 98.4, 98.1, 97.9, 97.6], "DOWN"),
        ([100.05, 99.98, 100.04, 99.99, 100.03, 99.97, 100.02, 99.99, 100.01], "RANGE"),
        ([101.0, 102.0, 100.5, 99.0, 100.0, 101.0, 99.5, 100.5, 100.0], "TRANSITION"),
    ],
)
def test_channel_direction_uses_successive_high_low_structure(
    closes: list[float],
    expected: str,
) -> None:
    assert build(path_rows(closes)).channel.direction == expected


def test_volume_distribution_and_path_extremes_are_hand_calculated() -> None:
    rows = path_rows([100, 101, 102, 101, 100, 99, 98, 99, 100, 103])
    for index, item in enumerate(rows):
        item["minute_volume"] = 10 if index < 5 else (70 if index == 9 else 20)

    result = build(rows)

    assert result.volume.first_half_volume == 50
    assert result.volume.second_half_volume == 150
    assert result.volume.final_five_minute_share == pytest.approx(0.75)
    assert result.stage.high_time == at(10, 9)
    assert result.stage.low_time == at(10, 6)
    assert result.stage.max_consecutive_up == 3
    assert result.stage.max_consecutive_down == 4


def test_cutoff_is_inclusive_and_duplicate_minutes_keep_latest_row() -> None:
    rows = [
        row(at(10, 0), open_price=100, close=101),
        row(at(10, 0), open_price=100, close=102),
        row(at(10, 1), open_price=102, close=103),
        row(at(10, 31), open_price=103, close=999),
    ]

    result = build(rows)

    assert result.stage.open == 100
    assert result.stage.close == 103
    assert result.stage.bar_count == 2
    assert result.data_quality == ("有效阶段分钟不足 5 条",)


@pytest.mark.parametrize(
    ("current_bias", "current_score", "previous", "expected"),
    [
        ("BULLISH", 0.8, PreviousStageContext(trend_bias="BULLISH", opportunity_score=0.2), "STRENGTHENING"),
        ("BULLISH", 0.1, PreviousStageContext(trend_bias="BULLISH", opportunity_score=0.7), "WEAKENING"),
        ("NEUTRAL", 0.1, PreviousStageContext(trend_bias="NEUTRAL", opportunity_score=0.0), "UNCHANGED"),
        ("BEARISH", -0.6, PreviousStageContext(trend_bias="BULLISH", opportunity_score=0.5), "REVERSING"),
    ],
)
def test_previous_stage_comparison_is_explicit(
    current_bias: str,
    current_score: float,
    previous: PreviousStageContext,
    expected: str,
) -> None:
    assert classify_opportunity_change(current_bias, current_score, previous) == expected


def test_snapshot_separates_stage_slice_from_cumulative_session() -> None:
    rows = path_rows(
        [99.0, 99.5, 100.0, 101.0, 102.0],
        start=at(9, 58),
    )

    snapshot = HalfHourAiSnapshotBuilder(minimum_observations=2).build(
        market="cn",
        symbol="600519.SH",
        session_open=at(9, 30),
        stage_start=at(10, 0),
        window_end=at(10, 3),
        data_cutoff=at(10, 3),
        rows=rows,
    )

    assert snapshot.market_structure.stage.bar_count == 3
    assert snapshot.market_structure.cumulative.bar_count == 5
    assert snapshot.stage_start == at(10, 0)
    assert snapshot.stage_trading_minutes == 3


def test_snapshot_counts_trading_rows_instead_of_lunch_wall_clock_minutes() -> None:
    rows = []
    for minute in range(30):
        rows.append(
            row(
                at(11, 0) + timedelta(minutes=minute),
                open_price=100,
                close=100,
            )
        )
    for minute in range(60):
        rows.append(
            row(
                at(13, 0) + timedelta(minutes=minute),
                open_price=100,
                close=100,
            )
        )

    snapshot = HalfHourAiSnapshotBuilder().build(
        market="cn",
        symbol="600519.SH",
        session_open=at(9, 30),
        stage_start=at(11, 0),
        window_end=at(14, 0),
        data_cutoff=at(14, 0),
        rows=rows,
    )

    assert snapshot.stage_trading_minutes == 90


def test_partial_realtime_row_lowers_quality_without_erasing_minute_structure() -> None:
    rows = path_rows([100, 101, 102, 103, 104])
    rows[-1]["data_quality"] = "PARTIAL"

    snapshot = HalfHourAiSnapshotBuilder().build(
        market="cn",
        symbol="600519.SH",
        session_open=at(9, 30),
        stage_start=at(10, 0),
        window_end=at(10, 5),
        data_cutoff=at(10, 5),
        rows=rows,
    )

    assert snapshot.sufficient
    assert snapshot.market_structure.stage.close == 104
    assert "最新实时评估数据不完整" in snapshot.data_quality


def test_snapshot_uses_previous_structured_state_for_stage_comparison_only() -> None:
    previous = PreviousStageContext(trend_bias="BEARISH", opportunity_score=-0.5)
    snapshot = HalfHourAiSnapshotBuilder().build(
        market="cn",
        symbol="600519.SH",
        session_open=at(9, 30),
        stage_start=at(10, 0),
        window_end=at(10, 9),
        data_cutoff=at(10, 9),
        rows=path_rows([100.2, 100.5, 100.8, 101.0, 101.3, 101.6, 101.9, 102.1, 102.4]),
        previous_stage=previous,
    )

    assert snapshot.previous_stage == previous
    assert snapshot.market_structure.opportunity_change == "REVERSING"
