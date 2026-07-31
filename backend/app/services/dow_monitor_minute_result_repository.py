from __future__ import annotations

import json
import os
from collections.abc import Callable, Sequence
from datetime import datetime
from urllib import error, parse, request
from zoneinfo import ZoneInfo

from app.plugins.clickhouse import bridge
from app.services.dow_monitor_minute_result_models import (
    DowMonitorMinuteResult,
    MinuteResultKey,
    normalize_monitor_symbol,
)

QueryFn = Callable[[str], list[dict]]
ExecuteFn = Callable[[str, bytes | None], bytes]
SHANGHAI = ZoneInfo("Asia/Shanghai")
MINUTE_RESULT_DATETIME_FIELDS = (
    "decision_minute",
    "source_bar_time",
    "formal_signal_time",
    "updated_at",
)


def _clickhouse_string(value: str) -> str:
    return "'" + value.replace("\\", "\\\\").replace("'", "''") + "'"


def _symbol_tuple(symbols: Sequence[str]) -> str:
    normalized = sorted(
        {normalize_monitor_symbol(symbol) for symbol in symbols if symbol.strip()}
    )
    if not normalized:
        raise ValueError("symbols must not be empty")
    return "(" + ", ".join(_clickhouse_string(symbol) for symbol in normalized) + ")"


def _time_literal(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("ClickHouse time range must be timezone-aware")
    return _clickhouse_string(value.isoformat())


def _clickhouse_datetime(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("ClickHouse datetime must be timezone-aware")
    return value.astimezone(SHANGHAI).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]


def _with_shanghai_datetimes(row: dict, *fields: str) -> dict:
    normalized = dict(row)
    for field in fields:
        value = normalized.get(field)
        if value is None:
            continue
        parsed = (
            value
            if isinstance(value, datetime)
            else datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        )
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            parsed = parsed.replace(tzinfo=SHANGHAI)
        normalized[field] = parsed
    return normalized


def _default_execute(sql: str, payload: bytes | None = None) -> bytes:
    endpoint = os.getenv("CLICKHOUSE_URL", "").strip().rstrip("/")
    if not endpoint:
        raise RuntimeError("未配置 CLICKHOUSE_URL")
    timeout = float(os.getenv("CLICKHOUSE_READ_TIMEOUT_SECONDS", "30"))
    body = sql.rstrip().rstrip(";").encode("utf-8")
    if payload:
        body += b"\n" + payload
    req = request.Request(
        f"{endpoint}/?database={parse.quote('longbridge', safe='')}",
        data=body,
        method="POST",
    )
    user = os.getenv("CLICKHOUSE_USER", "").strip()
    password = os.getenv("CLICKHOUSE_PASSWORD", "")
    if user:
        req.add_header("X-ClickHouse-User", user)
    if password:
        req.add_header("X-ClickHouse-Key", password)
    try:
        with request.urlopen(req, timeout=timeout) as response:
            return response.read()
    except error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:500]
        raise RuntimeError(f"ClickHouse 写入失败: HTTP {exc.code}: {detail}") from exc
    except (error.URLError, TimeoutError, OSError) as exc:
        raise RuntimeError(f"ClickHouse 连接失败: {type(exc).__name__}") from exc


class DowMonitorMinuteResultRepository:
    def __init__(
        self,
        query_fn: QueryFn = bridge.query_json_each_row,
        execute_fn: ExecuteFn | None = None,
        *,
        database: str = "longbridge",
    ) -> None:
        if database != "longbridge":
            raise ValueError("minute result database must be longbridge")
        self._query = query_fn
        self._execute = execute_fn or _default_execute
        self._database = database
        self._last_error: str | None = None

    def ensure_schema(self) -> None:
        ddl = f"""
        CREATE TABLE IF NOT EXISTS {self._database}.lb_dow_monitor_minute_results (
          market LowCardinality(String),
          symbol LowCardinality(String),
          display_symbol String,
          decision_minute DateTime64(3, 'Asia/Shanghai'),
          source_bar_time DateTime64(3, 'Asia/Shanghai'),
          calculation_version String,
          backfill UInt8,
          last_price Nullable(Float64),
          prev_close Nullable(Float64),
          change_pct Nullable(Float64),
          minute_open Float64,
          minute_high Float64,
          minute_low Float64,
          minute_close Float64,
          minute_volume Float64,
          minute_turnover Nullable(Float64),
          channel Nullable(String),
          control_distance_pct Nullable(Float64),
          vwap_distance_pct Nullable(Float64),
          momentum_1m_pct Nullable(Float64),
          momentum_5m_pct Nullable(Float64),
          momentum_15m_pct Nullable(Float64),
          volume_ratio Nullable(Float64),
          volume_speed Nullable(Float64),
          active_buy_ratio Nullable(Float64),
          depth_imbalance_pct Nullable(Float64),
          distance_to_day_high_pct Nullable(Float64),
          distance_to_day_low_pct Nullable(Float64),
          atr14_pct Nullable(Float64),
          confirmation_count Nullable(UInt8),
          formal_signal_side Nullable(String),
          formal_signal_stage Nullable(String),
          formal_signal_label Nullable(String),
          formal_signal_time Nullable(DateTime64(3, 'Asia/Shanghai')),
          formal_signal_event_key Nullable(String),
          data_quality LowCardinality(String),
          missing_fields Array(String),
          source_timestamps String,
          result_payload String,
          updated_at DateTime64(3, 'Asia/Shanghai')
        )
        ENGINE = ReplacingMergeTree(updated_at)
        PARTITION BY toYYYYMM(decision_minute)
        ORDER BY (market, symbol, decision_minute)
        """
        self._execute(ddl, None)

    def existing_keys(
        self,
        symbols: Sequence[str],
        start: datetime,
        end: datetime,
    ) -> set[MinuteResultKey]:
        rows = self._query(
            f"""
            SELECT market, symbol, toString(decision_minute) AS decision_minute
            FROM {self._database}.lb_dow_monitor_minute_results AS results FINAL
            WHERE symbol IN {_symbol_tuple(symbols)}
              AND results.decision_minute >= parseDateTime64BestEffort({_time_literal(start)})
              AND results.decision_minute < parseDateTime64BestEffort({_time_literal(end)})
            GROUP BY market, symbol, results.decision_minute
            """
        )
        return {
            MinuteResultKey(
                **_with_shanghai_datetimes(row, "decision_minute")
            )
            for row in rows
        }

    def insert_results(self, rows: Sequence[DowMonitorMinuteResult]) -> int:
        if not rows:
            return 0
        documents = []
        for row in rows:
            document = row.model_dump(mode="json")
            document.update(
                {
                    "decision_minute": _clickhouse_datetime(row.decision_minute),
                    "source_bar_time": _clickhouse_datetime(row.source_bar_time),
                    "formal_signal_time": _clickhouse_datetime(row.formal_signal_time),
                    "updated_at": _clickhouse_datetime(row.updated_at),
                    "backfill": int(row.backfill),
                    "missing_fields": list(row.missing_fields),
                    "source_timestamps": json.dumps(
                        row.source_timestamps,
                        ensure_ascii=False,
                        default=str,
                    ),
                    "result_payload": json.dumps(
                        row.result_payload,
                        ensure_ascii=False,
                        default=str,
                    ),
                }
            )
            documents.append(document)
        payload = "\n".join(
            json.dumps(document, ensure_ascii=False)
            for document in documents
        ).encode("utf-8")
        self._execute(
            f"INSERT INTO {self._database}.lb_dow_monitor_minute_results FORMAT JSONEachRow",
            payload,
        )
        return len(documents)

    def load_cumulative_rows(
        self,
        symbols: Sequence[str],
        session_open: datetime,
        data_cutoff: datetime,
    ) -> dict[str, list[dict]]:
        normalized = [
            normalize_monitor_symbol(symbol)
            for symbol in symbols
            if symbol.strip()
        ]
        if not normalized:
            return {}
        rows = self._query(
            f"""
            SELECT *
            FROM {self._database}.lb_dow_monitor_minute_results FINAL
            WHERE symbol IN {_symbol_tuple(normalized)}
              AND decision_minute >= parseDateTime64BestEffort({_time_literal(session_open)})
              AND decision_minute <= parseDateTime64BestEffort({_time_literal(data_cutoff)})
            ORDER BY symbol, decision_minute
            """
        )
        output: dict[str, list[dict]] = {symbol: [] for symbol in normalized}
        for row in rows:
            symbol = normalize_monitor_symbol(str(row.get("symbol") or ""))
            if symbol in output:
                output[symbol].append(
                    _with_shanghai_datetimes(
                        row,
                        *MINUTE_RESULT_DATETIME_FIELDS,
                    )
                )
        return output

    def status(self) -> dict[str, object]:
        return {
            "database": self._database,
            "table": "lb_dow_monitor_minute_results",
            "last_error": self._last_error,
        }
