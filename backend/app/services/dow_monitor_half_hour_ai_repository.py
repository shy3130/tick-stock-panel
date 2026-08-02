from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from datetime import UTC, date, datetime, timedelta

from app.plugins.clickhouse import bridge
from app.services.dow_monitor_half_hour_ai_models import (
    HalfHourAiAnalysis,
    HalfHourAiSummary,
    HourlyAiRerunRequest,
)
from app.services.dow_monitor_minute_result_repository import _default_execute

QueryFn = Callable[[str], list[dict]]
ExecuteFn = Callable[[str, bytes | None], bytes]


def _quoted(value: str) -> str:
    return "'" + value.replace("\\", "\\\\").replace("'", "''") + "'"


class DowMonitorHalfHourAiRepository:
    def __init__(
        self,
        query_fn: QueryFn = bridge.query_json_each_row,
        execute_fn: ExecuteFn | None = None,
        *,
        database: str = "longbridge",
    ) -> None:
        self._query = query_fn
        self._execute = execute_fn or _default_execute
        self._database = database

    @property
    def create_table_sql(self) -> str:
        return f"""
        CREATE TABLE IF NOT EXISTS {self._database}.lb_dow_monitor_half_hour_ai_analyses
        (
          analysis_id String,
          market LowCardinality(String),
          symbol String,
          trade_date Date,
          window_end DateTime64(3, 'UTC'),
          data_cutoff DateTime64(3, 'UTC'),
          status LowCardinality(String),
          title Nullable(String),
          summary Nullable(String),
          conclusion Nullable(String),
          evidence_json String,
          risks_json String,
          scenarios_json String,
          data_quality_json String,
          input_snapshot_json String,
          model_name Nullable(String),
          attempt UInt16,
          error_code Nullable(String),
          error_message Nullable(String),
          report_frequency LowCardinality(String) DEFAULT 'half_hour',
          stage_start Nullable(DateTime64(3, 'UTC')),
          stage_trading_minutes Nullable(UInt16),
          opportunity_change Nullable(String),
          report_json String DEFAULT '{{}}',
          created_at DateTime64(3, 'UTC'),
          updated_at DateTime64(3, 'UTC')
        )
        ENGINE = ReplacingMergeTree(updated_at)
        PARTITION BY toYYYYMM(trade_date)
        ORDER BY (market, symbol, trade_date, window_end)
        """

    @property
    def create_rerun_table_sql(self) -> str:
        return f"""
        CREATE TABLE IF NOT EXISTS {self._database}.lb_dow_monitor_hourly_ai_rerun_requests
        (
          request_id String,
          analysis_id String,
          market LowCardinality(String),
          symbol String,
          trade_date Date,
          window_end DateTime64(3, 'UTC'),
          data_cutoff DateTime64(3, 'UTC'),
          status LowCardinality(String),
          requested_at DateTime64(3, 'UTC'),
          started_at Nullable(DateTime64(3, 'UTC')),
          completed_at Nullable(DateTime64(3, 'UTC')),
          updated_at DateTime64(3, 'UTC'),
          error_code Nullable(String),
          error_message Nullable(String)
        )
        ENGINE = ReplacingMergeTree(updated_at)
        PARTITION BY toYYYYMM(trade_date)
        ORDER BY request_id
        """

    def ensure_schema(self) -> None:
        self._execute(self.create_table_sql, None)
        self._execute(self.create_rerun_table_sql, None)
        table = f"{self._database}.lb_dow_monitor_half_hour_ai_analyses"
        alterations = (
            "report_frequency LowCardinality(String) DEFAULT 'half_hour'",
            "stage_start Nullable(DateTime64(3, 'UTC'))",
            "stage_trading_minutes Nullable(UInt16)",
            "opportunity_change Nullable(String)",
            "report_json String DEFAULT '{}'",
        )
        for definition in alterations:
            self._execute(
                f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {definition}",
                None,
            )

    def save_rerun_request(self, request: HourlyAiRerunRequest) -> None:
        document = request.model_dump(mode="json")
        document.update(
            {
                "window_end": _utc_text(request.window_end),
                "data_cutoff": _utc_text(request.data_cutoff),
                "requested_at": _utc_text(request.requested_at),
                "started_at": _utc_text_optional(request.started_at),
                "completed_at": _utc_text_optional(request.completed_at),
                "updated_at": _utc_text(request.updated_at),
            }
        )
        self._execute(
            f"INSERT INTO {self._database}.lb_dow_monitor_hourly_ai_rerun_requests FORMAT JSONEachRow",
            json.dumps(document, ensure_ascii=False).encode("utf-8"),
        )

    def latest_rerun_request(
        self,
        analysis_id: str,
    ) -> HourlyAiRerunRequest | None:
        rows = self._query(
            f"""
            SELECT *
            FROM {self._database}.lb_dow_monitor_hourly_ai_rerun_requests FINAL
            WHERE analysis_id = {_quoted(analysis_id)}
            ORDER BY requested_at DESC, updated_at DESC
            LIMIT 1
            """
        )
        return _rerun_request_from_row(rows[0]) if rows else None

    def active_rerun_request(
        self,
        analysis_id: str,
    ) -> HourlyAiRerunRequest | None:
        rows = self._query(
            f"""
            SELECT *
            FROM {self._database}.lb_dow_monitor_hourly_ai_rerun_requests FINAL
            WHERE analysis_id = {_quoted(analysis_id)}
              AND status IN ('queued', 'running')
            ORDER BY requested_at DESC, updated_at DESC
            LIMIT 1
            """
        )
        return _rerun_request_from_row(rows[0]) if rows else None

    def next_runnable_rerun(
        self,
        *,
        now: datetime,
        stale_after: timedelta,
    ) -> HourlyAiRerunRequest | None:
        stale_before = now - stale_after
        rows = self._query(
            f"""
            SELECT *
            FROM {self._database}.lb_dow_monitor_hourly_ai_rerun_requests FINAL
            WHERE status = 'queued'
               OR (status = 'running' AND updated_at <= parseDateTime64BestEffort(
                    {_quoted(_utc_text(stale_before))}, 3, 'UTC'
                  ))
            ORDER BY requested_at ASC, updated_at ASC
            LIMIT 1
            """
        )
        return _rerun_request_from_row(rows[0]) if rows else None

    def save(self, record: HalfHourAiAnalysis) -> None:
        document = record.model_dump(mode="json")
        document.update(
            {
                "window_end": _utc_text(record.window_end),
                "data_cutoff": _utc_text(record.data_cutoff),
                "stage_start": _utc_text_optional(record.stage_start),
                "updated_at": _utc_text(record.updated_at),
                "created_at": _utc_text(record.updated_at),
                "evidence_json": json.dumps(
                    document.pop("evidence"), ensure_ascii=False
                ),
                "risks_json": json.dumps(
                    document.pop("risks"), ensure_ascii=False
                ),
                "scenarios_json": json.dumps(
                    document.pop("scenarios"), ensure_ascii=False
                ),
                "data_quality_json": json.dumps(
                    document.pop("data_quality"), ensure_ascii=False
                ),
                "input_snapshot_json": json.dumps(
                    document.pop("input_snapshot"), ensure_ascii=False, default=str
                ),
                "report_json": json.dumps(
                    document.pop("report"), ensure_ascii=False, default=str
                ) if record.report is not None else "{}",
            }
        )
        self._execute(
            f"INSERT INTO {self._database}.lb_dow_monitor_half_hour_ai_analyses FORMAT JSONEachRow",
            json.dumps(document, ensure_ascii=False).encode("utf-8"),
        )

    def exists_completed(
        self,
        market: str,
        symbol: str,
        trade_date: date,
        window_end: datetime,
    ) -> bool:
        rows = self._query(
            f"""
            SELECT status
            FROM {self._database}.lb_dow_monitor_half_hour_ai_analyses FINAL
            WHERE market = {_quoted(market)}
              AND symbol = {_quoted(symbol.upper())}
              AND trade_date = toDate({_quoted(trade_date.isoformat())})
              AND window_end = parseDateTime64BestEffort(
                {_quoted(_utc_text(window_end))}, 3, 'UTC'
              )
            ORDER BY updated_at DESC
            LIMIT 1
            """
        )
        return bool(
            rows
            and rows[0].get("status")
            in {"completed", "insufficient_data", "failed"}
        )

    def latest_summaries(
        self,
        keys: Sequence[tuple[str, str]],
    ) -> dict[tuple[str, str], HalfHourAiSummary]:
        if not keys:
            return {}
        conditions = " OR ".join(
            f"(market = {_quoted(market)} AND symbol = {_quoted(symbol.upper())})"
            for market, symbol in keys
        )
        rows = self._query(
            f"""
            SELECT analysis_id, market, symbol, trade_date, window_end, status,
                   report_frequency, stage_start, stage_trading_minutes,
                   opportunity_change, title, summary, updated_at
            FROM {self._database}.lb_dow_monitor_half_hour_ai_analyses FINAL
            WHERE {conditions}
            ORDER BY market, symbol, window_end DESC, updated_at DESC
            """
        )
        output: dict[tuple[str, str], HalfHourAiSummary] = {}
        for row in rows:
            key = (str(row.get("market")), str(row.get("symbol")))
            if key not in output:
                output[key] = HalfHourAiSummary.model_validate(
                    _with_utc_datetimes(
                        row,
                        "window_end",
                        "stage_start",
                        "updated_at",
                    )
                )
        return output

    def list_history(
        self,
        market: str,
        symbol: str,
        trade_date: date,
    ) -> list[HalfHourAiSummary]:
        rows = self._query(
            self._summary_select(market, symbol)
            + f" AND trade_date = toDate({_quoted(trade_date.isoformat())})"
            + " ORDER BY window_end DESC, updated_at DESC"
        )
        seen: set[str] = set()
        summaries: list[HalfHourAiSummary] = []
        for row in rows:
            identity = str(row.get("analysis_id") or "")
            if identity in seen:
                continue
            seen.add(identity)
            summaries.append(
                HalfHourAiSummary.model_validate(
                    _with_utc_datetimes(
                        row,
                        "window_end",
                        "stage_start",
                        "updated_at",
                    )
                )
            )
        return summaries

    def get_by_id(self, analysis_id: str) -> HalfHourAiAnalysis | None:
        rows = self._query(
            f"""
            SELECT *
            FROM {self._database}.lb_dow_monitor_half_hour_ai_analyses FINAL
            WHERE analysis_id = {_quoted(analysis_id)}
            ORDER BY updated_at DESC
            LIMIT 1
            """
        )
        if not rows:
            return None
        return _analysis_from_row(rows[0])

    def latest_completed_before(
        self,
        market: str,
        symbol: str,
        trade_date: date,
        window_end: datetime,
    ) -> HalfHourAiAnalysis | None:
        rows = self._query(
            f"""
            SELECT *
            FROM {self._database}.lb_dow_monitor_half_hour_ai_analyses FINAL
            WHERE market = {_quoted(market)}
              AND symbol = {_quoted(symbol.upper())}
              AND trade_date = toDate({_quoted(trade_date.isoformat())})
              AND status = 'completed'
              AND window_end < parseDateTime64BestEffort(
                {_quoted(_utc_text(window_end))}, 3, 'UTC'
              )
            ORDER BY window_end DESC, updated_at DESC
            LIMIT 1
            """
        )
        return _analysis_from_row(rows[0]) if rows else None

    def _summary_select(self, market: str, symbol: str) -> str:
        return f"""
            SELECT analysis_id, market, symbol, trade_date, window_end, status,
                   report_frequency, stage_start, stage_trading_minutes,
                   opportunity_change, title, summary, updated_at
            FROM {self._database}.lb_dow_monitor_half_hour_ai_analyses FINAL
            WHERE market = {_quoted(market)}
              AND symbol = {_quoted(symbol.upper())}
        """


def _analysis_from_row(source: dict) -> HalfHourAiAnalysis:
    row = dict(source)
    for field in ("evidence", "risks", "scenarios", "data_quality", "input_snapshot"):
        raw = row.pop(f"{field}_json", None)
        row[field] = json.loads(raw or ("{}" if field == "input_snapshot" else "[]"))
    raw_report = row.pop("report_json", None)
    parsed_report = json.loads(raw_report or "{}")
    row["report"] = parsed_report or None
    return HalfHourAiAnalysis.model_validate(
        _with_utc_datetimes(
            row,
            "window_end",
            "data_cutoff",
            "stage_start",
            "created_at",
            "updated_at",
        )
    )


def _rerun_request_from_row(source: dict) -> HourlyAiRerunRequest:
    return HourlyAiRerunRequest.model_validate(
        _with_utc_datetimes(
            source,
            "window_end",
            "data_cutoff",
            "requested_at",
            "started_at",
            "completed_at",
            "updated_at",
        )
    )


def _utc_text(value: datetime | None) -> str:
    if value is None:
        raise ValueError("datetime is required")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("datetime must be timezone-aware")
    return value.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]


def _utc_text_optional(value: datetime | None) -> str | None:
    return _utc_text(value) if value is not None else None


def _with_utc_datetimes(row: dict, *fields: str) -> dict:
    normalized = dict(row)
    for field in fields:
        value = normalized.get(field)
        if value is None:
            continue
        parsed = value if isinstance(value, datetime) else datetime.fromisoformat(str(value))
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            parsed = parsed.replace(tzinfo=UTC)
        else:
            parsed = parsed.astimezone(UTC)
        normalized[field] = parsed
    return normalized
