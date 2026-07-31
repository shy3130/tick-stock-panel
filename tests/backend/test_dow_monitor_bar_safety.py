from __future__ import annotations

import json
import math
from datetime import datetime, timezone

import httpx
import polars as pl
import pytest

from app.services.dow_monitor_bar_safety import (
    InsufficientDowBars,
    sanitize_engine_bars,
)
from app.services.dow_monitor_bars import TimeframeBars
from app.services.dow_monitor_client import DowEngineUnavailable, LongbridgeDowClient
from app.services.dow_monitor_data import SymbolFreshness, WebStockBatch
from app.services.dow_monitor_service import DowMonitorService
from app.services.dow_monitor_store import DowMonitorStore


NOW = datetime(2026, 7, 31, 14, 30, tzinfo=timezone.utc)


def _reject_nonstandard_number(value: str) -> None:
    raise ValueError(f"non-standard JSON number: {value}")


def test_client_excludes_nonfinite_bar_before_http_transport() -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(
            json.loads(request.content, parse_constant=_reject_nonstandard_number)
        )
        return httpx.Response(503, json={"detail": "test transport stop"})

    bars = [
        {
            "timestamp": "2026-07-28",
            "open": 10,
            "high": 11,
            "low": 9,
            "close": 10.5,
            "volume": 100,
        },
        {
            "timestamp": "2026-07-29",
            "open": math.nan,
            "high": 11,
            "low": 9,
            "close": 10,
            "volume": 100,
        },
        {
            "timestamp": "2026-07-30",
            "open": 11,
            "high": 12,
            "low": 10,
            "close": 11.5,
            "volume": 120,
        },
    ]

    with LongbridgeDowClient(
        "http://dow-engine.test",
        transport=httpx.MockTransport(handler),
    ) as client:
        with pytest.raises(DowEngineUnavailable):
            client.evaluate("RNG.US", "day", bars, "FINAL", NOW)

    sent_bars = captured["bars"]
    assert isinstance(sent_bars, list)
    assert [bar["timestamp"] for bar in sent_bars] == [
        "2026-07-28",
        "2026-07-30",
    ]
    assert sent_bars[1]["close"] == 11.5


def test_sanitizer_drops_invalid_ohlc_and_keeps_latest_valid_duplicate() -> None:
    bars = [
        {
            "timestamp": "2026-07-28",
            "open": 10,
            "high": 11,
            "low": 9,
            "close": 10.5,
            "volume": 100,
        },
        {
            "timestamp": "2026-07-29",
            "open": 10,
            "high": 9,
            "low": 11,
            "close": 10,
            "volume": 100,
        },
        {
            "timestamp": "2026-07-30",
            "open": 11,
            "high": 12,
            "low": 10,
            "close": 11.25,
            "volume": -1,
        },
        {
            "timestamp": "2026-07-28",
            "open": 10.5,
            "high": 12,
            "low": 10,
            "close": 11.75,
            "volume": 125,
        },
        {
            "timestamp": "2026-07-31",
            "open": 11.75,
            "high": 13,
            "low": 11,
            "close": 12.5,
            "volume": 130,
        },
    ]

    assert sanitize_engine_bars("day", bars) == [
        {
            "timestamp": "2026-07-28",
            "open": 10.5,
            "high": 12.0,
            "low": 10.0,
            "close": 11.75,
            "volume": 125.0,
        },
        {
            "timestamp": "2026-07-31",
            "open": 11.75,
            "high": 13.0,
            "low": 11.0,
            "close": 12.5,
            "volume": 130.0,
        },
    ]


def test_sanitizer_reports_machine_readable_insufficient_history() -> None:
    with pytest.raises(ValueError) as captured:
        sanitize_engine_bars(
            "day",
            [
                {
                    "timestamp": "2026-07-31",
                    "open": 11.75,
                    "high": 13,
                    "low": 11,
                    "close": 12.5,
                    "volume": 130,
                }
            ],
        )

    assert captured.value.timeframe == "day"
    assert captured.value.valid_bars == 1
    assert captured.value.required_bars == 2


def test_service_isolates_insufficient_history_to_one_timeframe(
    tmp_path,
    monkeypatch,
) -> None:
    valid_bars = [
        {
            "timestamp": "2026-07-31T14:20:00+00:00",
            "open": 10.0,
            "high": 11.0,
            "low": 9.5,
            "close": 10.5,
            "volume": 100.0,
        },
        {
            "timestamp": "2026-07-31T14:25:00+00:00",
            "open": 10.5,
            "high": 11.5,
            "low": 10.0,
            "close": 11.0,
            "volume": 120.0,
        },
    ]
    frames = {
        timeframe: TimeframeBars(
            completed=valid_bars,
            forming={},
            completion="FINAL",
            source_timestamp=NOW,
        )
        for timeframe in ("5m", "day")
    }

    class Engine:
        def evaluate(self, _symbol, timeframe, *_args):
            if timeframe == "day":
                raise InsufficientDowBars("day", 1, 2)
            return object()

    store = DowMonitorStore(tmp_path)
    item = store.upsert_symbol("RNG.US", "us", True)
    service = DowMonitorService(
        store,
        object(),
        Engine(),
        lambda *_args: pl.DataFrame(),
        now_fn=lambda: NOW,
    )
    saved: list[str] = []
    monkeypatch.setattr(
        "app.services.dow_monitor_service.TIMEFRAMES",
        ("5m", "day"),
    )
    monkeypatch.setattr(
        "app.services.dow_monitor_service.build_timeframes",
        lambda *_args: frames,
    )
    monkeypatch.setattr(
        service,
        "_merge_evaluation_bars",
        lambda _item, _timeframe, _previous, frame, _now: (
            frame.completed,
            frame.completion,
        ),
    )
    monkeypatch.setattr(
        service,
        "_save_result",
        lambda _item, timeframe, *_args: saved.append(timeframe),
    )
    batch = WebStockBatch(
        quotes=[],
        minute_rows=pl.DataFrame(),
        source_timestamp=NOW,
        freshness_by_symbol={
            "RNG.US": SymbolFreshness(state="LIVE", reason=None)
        },
        gap_details={"RNG.US": []},
    )

    error, success = service._evaluate_symbol(
        item,
        batch,
        NOW,
        {},
        False,
        pl.DataFrame(),
    )

    assert success is True
    assert saved == ["5m"]
    assert error == "day: HISTORY_INCOMPLETE:VALID_BARS_1_OF_2"
    day_state = store.get_state("RNG.US", "day")
    assert day_state is not None
    assert day_state.freshness_state == "ANALYSIS_PAUSED"
