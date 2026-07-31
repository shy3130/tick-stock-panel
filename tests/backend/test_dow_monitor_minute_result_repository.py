from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from app.services.dow_monitor_half_hour_ai_snapshot import HalfHourAiSnapshotBuilder
from app.services.dow_monitor_minute_result_models import DowMonitorMinuteResult
from app.services.dow_monitor_minute_result_repository import (
    DowMonitorMinuteResultRepository,
)


UTC = timezone.utc
BEIJING = ZoneInfo("Asia/Shanghai")
NOW = datetime(2026, 7, 29, 1, 31, 5, tzinfo=UTC)
DECISION_MINUTE = NOW - timedelta(seconds=5)


class ExecuteCapture:
    def __init__(self) -> None:
        self.calls: list[tuple[str, bytes | None]] = []

    def __call__(self, sql: str, payload: bytes | None = None) -> bytes:
        self.calls.append((sql, payload))
        return b""


def _result(**overrides: object) -> DowMonitorMinuteResult:
    values: dict[str, object] = {
        "market": "hk",
        "symbol": "700.HK",
        "display_symbol": "00700.HK",
        "decision_minute": DECISION_MINUTE,
        "source_bar_time": DECISION_MINUTE - timedelta(minutes=1),
        "backfill": True,
        "last_price": 101.0,
        "prev_close": 100.0,
        "change_pct": 1.0,
        "minute_open": 100.0,
        "minute_high": 102.0,
        "minute_low": 99.0,
        "minute_close": 101.0,
        "minute_volume": 80.0,
        "minute_turnover": 8_080.0,
        "channel": "UP",
        "control_distance_pct": 1.2,
        "vwap_distance_pct": 0.19,
        "momentum_1m_pct": 1.0,
        "momentum_5m_pct": 4.0,
        "momentum_15m_pct": 0.8,
        "volume_ratio": 1.6,
        "volume_speed": 0.8,
        "active_buy_ratio": None,
        "depth_imbalance_pct": 9.09,
        "distance_to_day_high_pct": 0.99,
        "distance_to_day_low_pct": 2.97,
        "atr14_pct": 1.73,
        "confirmation_count": 2,
        "formal_signal_side": "SELL",
        "formal_signal_stage": "CONFIRMED",
        "formal_signal_label": "卖出确认",
        "formal_signal_time": DECISION_MINUTE - timedelta(minutes=2),
        "formal_signal_event_key": "evt-1",
        "data_quality": "PARTIAL",
        "missing_fields": ("active_buy_ratio",),
        "source_timestamps": {"quote": DECISION_MINUTE - timedelta(seconds=1)},
        "result_payload": {"calculation_version": "v1"},
        "updated_at": NOW,
    }
    values.update(overrides)
    return DowMonitorMinuteResult(**values)


def test_schema_is_permanent_idempotent_and_queryable() -> None:
    execute = ExecuteCapture()
    repository = DowMonitorMinuteResultRepository(
        query_fn=lambda _sql: [],
        execute_fn=execute,
    )

    repository.ensure_schema()

    ddl = execute.calls[0][0]
    assert "CREATE TABLE IF NOT EXISTS longbridge.lb_dow_monitor_minute_results" in ddl
    assert "ReplacingMergeTree(updated_at)" in ddl
    assert "PARTITION BY toYYYYMM(decision_minute)" in ddl
    assert "ORDER BY (market, symbol, decision_minute)" in ddl
    assert "TTL" not in ddl.upper()
    assert "Nullable(LowCardinality(" not in ddl
    assert "channel Nullable(String)" in ddl
    assert "formal_signal_side Nullable(String)" in ddl
    assert "formal_signal_stage Nullable(String)" in ddl
    for column in (
        "channel",
        "control_distance_pct",
        "vwap_distance_pct",
        "momentum_1m_pct",
        "momentum_5m_pct",
        "momentum_15m_pct",
        "volume_ratio",
        "volume_speed",
        "active_buy_ratio",
        "depth_imbalance_pct",
        "distance_to_day_high_pct",
        "distance_to_day_low_pct",
        "atr14_pct",
        "confirmation_count",
    ):
        assert column in ddl


def test_insert_serializes_nulls_arrays_times_and_json_once_per_batch() -> None:
    execute = ExecuteCapture()
    repository = DowMonitorMinuteResultRepository(
        query_fn=lambda _sql: [],
        execute_fn=execute,
    )

    written = repository.insert_results([
        _result(change_pct=1.25),
        _result(symbol="9988.HK", display_symbol="09988.HK"),
    ])

    assert written == 2
    assert len(execute.calls) == 1
    sql, payload = execute.calls[0]
    assert sql.endswith("FORMAT JSONEachRow")
    documents = [json.loads(line) for line in payload.decode("utf-8").splitlines()]
    assert documents[0]["change_pct"] == 1.25
    assert documents[0]["active_buy_ratio"] is None
    assert documents[0]["missing_fields"] == ["active_buy_ratio"]
    assert json.loads(documents[0]["source_timestamps"])["quote"]
    assert json.loads(documents[0]["result_payload"])["calculation_version"] == "v1"


def test_existing_keys_are_returned_as_timezone_aware_logical_keys() -> None:
    queries: list[str] = []

    def query(sql: str) -> list[dict]:
        queries.append(sql)
        return [{
            "market": "hk",
            "symbol": "700.HK",
            "decision_minute": "2026-07-29 09:31:00.000",
        }]

    keys = DowMonitorMinuteResultRepository(
        query_fn=query,
        execute_fn=ExecuteCapture(),
    ).existing_keys(["00700.HK"], DECISION_MINUTE - timedelta(hours=1), NOW)

    key = next(iter(keys))
    assert key.market == "hk"
    assert key.symbol == "700.HK"
    assert key.decision_minute == datetime(
        2026, 7, 29, 9, 31, tzinfo=BEIJING
    )
    assert (
        "FROM longbridge.lb_dow_monitor_minute_results AS results FINAL"
        in queries[0]
    )
    assert "results.decision_minute >=" in queries[0]
    assert "results.decision_minute <" in queries[0]
    assert "'700.HK'" in queries[0]
    assert "'00700.HK'" not in queries[0]


# Catches ClickHouse DateTime64 local strings being passed through as naive
# values and consequently discarded by the production snapshot builder.
def test_cumulative_rows_normalize_clickhouse_local_times_before_snapshot() -> None:
    rows: list[dict] = [
        {
            "market": "hk",
            "symbol": "700.HK",
            "decision_minute": "2026-07-31 14:59:00.000",
            "source_bar_time": "2026-07-31 14:58:00.000",
            "formal_signal_time": None,
            "updated_at": "2026-07-31 14:59:00.125",
            "last_price": 101.0,
        },
        {
            "market": "hk",
            "symbol": "700.HK",
            "decision_minute": "2026-07-31 15:01:00.000",
            "source_bar_time": "2026-07-31 15:00:00.000",
            "formal_signal_time": "2026-07-31 14:57:00.000",
            "updated_at": "2026-07-31 15:01:00.125",
            "last_price": 999.0,
        },
    ]
    repository = DowMonitorMinuteResultRepository(
        query_fn=lambda _sql: rows,
        execute_fn=ExecuteCapture(),
    )
    session_open = datetime(2026, 7, 31, 9, 30, tzinfo=BEIJING)
    window_end = datetime(2026, 7, 31, 15, 0, tzinfo=BEIJING)

    loaded = repository.load_cumulative_rows(
        ["00700.HK"],
        session_open,
        window_end,
    )["700.HK"]
    snapshot = HalfHourAiSnapshotBuilder(minimum_observations=1).build(
        market="hk",
        symbol="700.HK",
        session_open=session_open,
        window_end=window_end,
        data_cutoff=window_end,
        rows=loaded,
    )

    assert snapshot.observation_count == 1
    assert snapshot.window_end == window_end
    assert snapshot.data_cutoff == window_end
    assert snapshot.range_start == datetime(
        2026, 7, 31, 14, 59, tzinfo=BEIJING
    )
    assert snapshot.range_end == snapshot.range_start
    assert snapshot.latest_price == 101.0
    assert loaded[0]["decision_minute"] == datetime(
        2026, 7, 31, 14, 59, tzinfo=BEIJING
    )
    assert loaded[0]["source_bar_time"] == datetime(
        2026, 7, 31, 14, 58, tzinfo=BEIJING
    )
    assert loaded[0]["updated_at"] == datetime(
        2026, 7, 31, 14, 59, 0, 125000, tzinfo=BEIJING
    )
    assert loaded[1]["formal_signal_time"] == datetime(
        2026, 7, 31, 14, 57, tzinfo=BEIJING
    )


def test_cumulative_rows_preserve_already_aware_datetime_values() -> None:
    aware_text = "2026-07-31T06:59:00+00:00"
    aware = datetime.fromisoformat(aware_text)
    repository = DowMonitorMinuteResultRepository(
        query_fn=lambda _sql: [{
            "market": "hk",
            "symbol": "700.HK",
            "decision_minute": aware_text,
            "source_bar_time": aware_text,
            "formal_signal_time": aware_text,
            "updated_at": aware_text,
            "last_price": 101.0,
        }],
        execute_fn=ExecuteCapture(),
    )

    loaded = repository.load_cumulative_rows(
        ["700.HK"],
        datetime(2026, 7, 31, 9, 30, tzinfo=BEIJING),
        datetime(2026, 7, 31, 15, 0, tzinfo=BEIJING),
    )["700.HK"][0]

    assert loaded["decision_minute"] == aware
    assert loaded["source_bar_time"] == aware
    assert loaded["formal_signal_time"] == aware
    assert loaded["updated_at"] == aware
    assert loaded["decision_minute"].isoformat() == aware_text
