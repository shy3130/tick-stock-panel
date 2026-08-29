from __future__ import annotations

from datetime import date, timedelta

import polars as pl
from fastapi.encoders import jsonable_encoder

from app.services.n_shape_pullback_depth import (
    BUCKET_A,
    BUCKET_B,
    BUCKET_C,
    classify_depth_bucket,
    detect_causal_swings,
    evaluate_n_shape_pullback_depth,
)


def _row(
    day: date,
    close: float,
    *,
    high: float,
    low: float,
    open_: float | None = None,
    volume: float = 1_000.0,
):
    return {
        "date": day,
        "raw_open": open_ if open_ is not None else close,
        "close": close,
        "raw_high": high,
        "raw_low": low,
        "raw_close": close,
        "volume": volume,
    }


def _confirmed_fixture() -> tuple[list[dict], list[date]]:
    start = date(2024, 1, 2)
    values = [
        (100.0, 101.0, 100.0),
        (110.0, 111.0, 105.0),
        (120.0, 120.0, 110.0),
        (115.0, 116.0, 112.0),
        (108.0, 114.0, 108.0),
        (121.0, 122.0, 109.0),
    ]
    rows = [
        _row(start + timedelta(days=i), close, high=high, low=low)
        for i, (close, high, low) in enumerate(values)
    ]
    for i in range(6, 32):
        close = 121.0 + i / 10
        rows.append(
            _row(
                start + timedelta(days=i),
                close,
                open_=close - 0.2,
                high=close + 0.5,
                low=close - 0.5,
            )
        )
    return rows, [row["date"] for row in rows]


def test_depth_boundaries_are_explicit_and_stable():
    assert classify_depth_bucket(0.500001) == BUCKET_A
    assert classify_depth_bucket(0.50) == BUCKET_B
    assert classify_depth_bucket(0.33) == BUCKET_B
    assert classify_depth_bucket(0.329999) == BUCKET_C


def test_confirmed_breakout_emits_causal_bucket_and_t_plus_one_forward():
    rows, calendar = _confirmed_fixture()
    events, failures = detect_causal_swings(
        symbol="000001.SZ",
        rows=rows,
        calendar=calendar,
        event_window=(calendar[0], calendar[-1]),
    )
    assert failures == []
    assert len(events) == 1
    event = events[0]
    assert event["event_date"] == calendar[5]
    assert event["depth"] == 0.60
    assert event["bucket"] == BUCKET_A
    expected = rows[10]["raw_close"] / rows[6]["raw_open"] - 1 - 0.002
    assert event["forward"]["forward_5d_return"] == expected


def test_unconfirmed_terminal_pullback_never_emits_event():
    rows, calendar = _confirmed_fixture()
    rows = rows[:5]
    events, failures = detect_causal_swings(
        symbol="000001.SZ",
        rows=rows,
        calendar=calendar[:5],
        event_window=(calendar[0], calendar[4]),
    )
    assert events == []
    assert failures == []


def test_pullback_below_origin_is_reported_as_failure_not_signal():
    rows, calendar = _confirmed_fixture()
    rows[4] = _row(calendar[4], 98.0, high=114.0, low=98.0)
    events, failures = detect_causal_swings(
        symbol="000001.SZ",
        rows=rows,
        calendar=calendar,
        event_window=(calendar[0], calendar[-1]),
    )
    assert events == []
    assert failures[0]["reason"] == "pullback_broke_origin_low"


class _Reader:
    def __init__(self, rows: list[dict]):
        self.rows = rows

    def manifest_sha256(self):
        return "a" * 64

    def source_provenance(self):
        return {
            "canonical": {"generation": "canonical-g1", "manifest_sha256": "b" * 64},
            "markets": {"generation": "markets-g1", "manifest_sha256": "c" * 64},
        }

    def generation(self):
        return "composite-g1"

    def provider_id(self):
        return "test"

    def market_days(self, start: date, end: date):
        return [row["date"] for row in self.rows if start <= row["date"] <= end]

    def universe(self, start: date, end: date):
        return ["000001.SZ"]

    def daily_bars(self, symbol: str, start: date, end: date):
        return pl.DataFrame([row for row in self.rows if start <= row["date"] <= end])

    def limit_regime_facts(self, symbol: str, start: date, end: date):
        return {}


def test_evaluator_binds_identity_and_is_json_encodable():
    rows, calendar = _confirmed_fixture()
    payload = evaluate_n_shape_pullback_depth(
        start=calendar[0],
        end=calendar[5],
        symbols=["000001.SZ"],
        reader=_Reader(rows),
    )
    assert payload["status"] == "ok"
    assert payload["coverage"]["events"] == 1
    assert payload["promoted"] is False
    assert payload["provenance"]["reader"]["manifest_sha256"] == "a" * 64
    encoded = jsonable_encoder(payload)
    assert encoded["events"][0]["event_date"] == calendar[5].isoformat()


def test_missing_reader_fails_closed():
    payload = evaluate_n_shape_pullback_depth(
        start=date(2024, 1, 1),
        end=date(2024, 1, 31),
        symbols=["000001.SZ"],
        reader=None,
    )
    assert payload["status"] == "unavailable"
    assert payload["events"] == []
