from __future__ import annotations

import asyncio
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from threading import Barrier
from time import sleep
from zoneinfo import ZoneInfo

import pytest
import yaml
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import dow_monitor
from app.services.dow_monitor_client import LongbridgeDowClient
from app.services.dow_monitor_half_hour_ai_calendar import HalfHourWindowCalendar
from app.services.dow_monitor_half_hour_ai_models import (
    ChannelAssessment,
    HalfHourAiAnalysis,
    HourlyAiRerunRequest,
    HourlyStageReport,
    NextStageConditions,
    PatternAssessment,
    PositionAdvice,
    StageHeadline,
    StagePathSegment,
    analysis_id_for,
)
from app.services.dow_monitor_half_hour_ai_prompt import (
    HalfHourAiPromptService,
    InvalidAiAnalysis,
    ParsedAiAnalysis,
)
from app.services.dow_monitor_half_hour_ai_repository import (
    DowMonitorHalfHourAiRepository,
)
from app.services.dow_monitor_half_hour_ai_snapshot import HalfHourAiSnapshotBuilder
from app.services.dow_monitor_minute_result_history import (
    DowEngineStableStateBuilder,
    DowMonitorMinuteResultHistoryBuilder,
)
from app.services.dow_monitor_minute_result_materializer import (
    DowMonitorMinuteResultMaterializer,
)
from app.services.dow_monitor_minute_result_repository import (
    DowMonitorMinuteResultRepository,
)
from app.services.dow_monitor_minute_result_source import DowMonitorMinuteResultSource
from app.services.dow_monitor_models import MonitoredSymbol
from app.services.dow_monitor_offline_bootstrap import (
    DowMonitorOfflineBootstrap,
    OfflineBootstrapOutcome,
)
from app.services.dow_monitor_service import DowMonitorService
from app.services.dow_monitor_store import DowMonitorStore
from app.workers import dow_monitor_half_hour_ai as worker_module
from app.workers.dow_monitor_half_hour_ai import DowMonitorHalfHourAiWorker


def test_production_override_places_ai_worker_on_host_network() -> None:
    override = (
        Path(__file__).resolve().parents[2] / "docker-compose.override.yml"
    ).read_text(encoding="utf-8")
    worker_section = override.split("  dow-ai-worker:", maxsplit=1)[1]
    assert "network_mode: host" in worker_section


def test_build_worker_owns_canonical_bootstrap_graph(
    monkeypatch,
    tmp_path,
) -> None:
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.delenv("LONGBRIDGE_API_URL", raising=False)
    monkeypatch.delenv(
        "DOW_MONITOR_AI_BOOTSTRAP_TIMEOUT_SECONDS",
        raising=False,
    )
    monkeypatch.delenv(
        "DOW_MONITOR_AI_BOOTSTRAP_MAX_ROWS",
        raising=False,
    )
    monkeypatch.setattr(
        DowMonitorHalfHourAiRepository,
        "ensure_schema",
        lambda _self: None,
    )

    worker = worker_module.build_worker()
    bootstrap = worker._offline_bootstrap
    materializer = bootstrap._materializer
    history_builder = materializer._history_builder
    stable_state_builder = history_builder._stable_state_builder
    dow_client = stable_state_builder._client
    try:
        assert isinstance(bootstrap, DowMonitorOfflineBootstrap)
        assert isinstance(materializer, DowMonitorMinuteResultMaterializer)
        assert isinstance(materializer._source, DowMonitorMinuteResultSource)
        assert materializer._repository is worker._minute_repository
        assert isinstance(
            history_builder,
            DowMonitorMinuteResultHistoryBuilder,
        )
        assert isinstance(stable_state_builder, DowEngineStableStateBuilder)
        assert isinstance(dow_client, LongbridgeDowClient)
        assert str(dow_client._client.base_url) == (
            "http://host.docker.internal:19912/"
        )
        assert bootstrap._timeout_seconds == 15.0
        assert bootstrap._max_rows == 500

        worker.close()

        assert dow_client._client.is_closed
    finally:
        dow_client.close()


@pytest.mark.parametrize(
    ("timeout", "max_rows", "expected_timeout", "expected_max_rows"),
    [
        ("3.5", "123", 3.5, 123),
        ("60", "900", 15.0, 500),
    ],
    ids=["custom-within-bounds", "clamped-to-authoritative-bounds"],
)
def test_build_worker_applies_bounded_bootstrap_environment(
    monkeypatch,
    tmp_path,
    timeout: str,
    max_rows: str,
    expected_timeout: float,
    expected_max_rows: int,
) -> None:
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv(
        "DOW_MONITOR_AI_BOOTSTRAP_TIMEOUT_SECONDS",
        timeout,
    )
    monkeypatch.setenv("DOW_MONITOR_AI_BOOTSTRAP_MAX_ROWS", max_rows)
    monkeypatch.setattr(
        DowMonitorHalfHourAiRepository,
        "ensure_schema",
        lambda _self: None,
    )

    worker = worker_module.build_worker()
    bootstrap = worker._offline_bootstrap
    dow_client = (
        bootstrap
        ._materializer
        ._history_builder
        ._stable_state_builder
        ._client
    )
    try:
        assert bootstrap._timeout_seconds == expected_timeout
        assert bootstrap._max_rows == expected_max_rows
    finally:
        dow_client.close()


@pytest.mark.parametrize("timeout", ["nan", "inf", "-inf"])
def test_build_worker_rejects_non_finite_timeout_before_allocating_client(
    monkeypatch,
    tmp_path,
    timeout: str,
) -> None:
    allocations: list[str] = []

    class AnalysisRepository:
        def ensure_schema(self) -> None:
            pass

    class Client:
        def close(self) -> None:
            pass

    def allocate_analysis_repository():
        allocations.append("analysis_repository")
        return AnalysisRepository()

    def allocate_client(endpoint: str):
        allocations.append(f"dow_client:{endpoint}")
        return Client()

    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv(
        "DOW_MONITOR_AI_BOOTSTRAP_TIMEOUT_SECONDS",
        timeout,
    )
    monkeypatch.setenv("DOW_MONITOR_AI_BOOTSTRAP_MAX_ROWS", "500")
    monkeypatch.setattr(
        worker_module,
        "DowMonitorHalfHourAiRepository",
        allocate_analysis_repository,
    )
    monkeypatch.setattr(
        worker_module,
        "LongbridgeDowClient",
        allocate_client,
    )

    with pytest.raises(ValueError, match="finite positive"):
        worker_module.build_worker()

    assert allocations == []


def test_worker_compose_propagates_bootstrap_environment_defaults() -> None:
    compose = yaml.safe_load(
        (
            Path(__file__).resolve().parents[2] / "docker-compose.yml"
        ).read_text(encoding="utf-8")
    )
    environment = set(compose["services"]["dow-ai-worker"]["environment"])

    assert (
        "LONGBRIDGE_API_URL="
        "${LONGBRIDGE_API_URL:-http://host.docker.internal:19912}"
    ) in environment
    assert (
        "DOW_MONITOR_AI_BOOTSTRAP_TIMEOUT_SECONDS="
        "${DOW_MONITOR_AI_BOOTSTRAP_TIMEOUT_SECONDS:-15}"
    ) in environment
    assert (
        "DOW_MONITOR_AI_BOOTSTRAP_MAX_ROWS="
        "${DOW_MONITOR_AI_BOOTSTRAP_MAX_ROWS:-500}"
    ) in environment


@pytest.mark.asyncio
async def test_main_closes_worker_when_loop_is_cancelled(monkeypatch) -> None:
    class Worker:
        def __init__(self) -> None:
            self.closed = False
            self.runs = 0

        async def run_due_jobs(self) -> int:
            self.runs += 1
            return 0

        def close(self) -> None:
            self.closed = True

    worker = Worker()

    async def cancel_sleep(_seconds: float) -> None:
        raise asyncio.CancelledError

    monkeypatch.setenv("DOW_AI_WORKER_ENABLED", "true")
    monkeypatch.setattr(worker_module, "build_worker", lambda: worker)
    monkeypatch.setattr(worker_module.asyncio, "sleep", cancel_sleep)

    with pytest.raises(asyncio.CancelledError):
        await worker_module._main()

    assert worker.runs == 1
    assert worker.closed is True


def test_cn_first_due_window_is_1000_beijing() -> None:
    calendar = HalfHourWindowCalendar()
    assert calendar.completed_window_ends(
        "cn",
        datetime.fromisoformat("2026-07-31T10:00:01+08:00"),
    ) == [datetime.fromisoformat("2026-07-31T10:00:00+08:00")]


def test_cn_schedule_uses_whole_hours_and_continuous_session_closes() -> None:
    ends = HalfHourWindowCalendar().session_window_ends(
        "cn",
        date(2026, 7, 31),
    )

    assert ends == [
        datetime.fromisoformat("2026-07-31T10:00:00+08:00"),
        datetime.fromisoformat("2026-07-31T11:00:00+08:00"),
        datetime.fromisoformat("2026-07-31T11:30:00+08:00"),
        datetime.fromisoformat("2026-07-31T14:00:00+08:00"),
        datetime.fromisoformat("2026-07-31T15:00:00+08:00"),
    ]


def test_hk_schedule_skips_lunch_and_resumes_at_next_whole_hour() -> None:
    ends = HalfHourWindowCalendar().session_window_ends(
        "hk",
        date(2026, 7, 31),
    )

    assert ends == [
        datetime.fromisoformat("2026-07-31T10:00:00+08:00"),
        datetime.fromisoformat("2026-07-31T11:00:00+08:00"),
        datetime.fromisoformat("2026-07-31T12:00:00+08:00"),
        datetime.fromisoformat("2026-07-31T14:00:00+08:00"),
        datetime.fromisoformat("2026-07-31T15:00:00+08:00"),
        datetime.fromisoformat("2026-07-31T16:00:00+08:00"),
    ]


def test_us_dst_session_maps_to_beijing_time() -> None:
    ends = HalfHourWindowCalendar().session_window_ends(
        "us",
        date(2026, 7, 31),
    )
    assert ends == [
        datetime.fromisoformat(f"2026-07-31T{hour:02d}:00:00+08:00")
        for hour in range(22, 24)
    ] + [
        datetime.fromisoformat(f"2026-08-01T{hour:02d}:00:00+08:00")
        for hour in range(0, 5)
    ]


def test_exchange_holiday_has_no_due_windows() -> None:
    assert HalfHourWindowCalendar().session_window_ends(
        "us",
        date(2026, 7, 3),
    ) == []


def test_analysis_id_is_stable_for_logical_window() -> None:
    window = datetime.fromisoformat("2026-07-31T23:00:00+08:00")
    assert analysis_id_for("us", "rng.us", date(2026, 7, 31), window) == (
        analysis_id_for("us", "RNG.US", date(2026, 7, 31), window)
    )


def test_repository_schema_is_permanent_and_saves_json_each_row() -> None:
    calls: list[tuple[str, bytes | None]] = []
    repository = DowMonitorHalfHourAiRepository(
        query_fn=lambda _sql: [],
        execute_fn=lambda sql, payload=None: calls.append((sql, payload)) or b"",
    )
    repository.ensure_schema()
    assert "TTL" not in repository.create_table_sql.upper()

    repository.save(
        HalfHourAiAnalysis(
            analysis_id="a1",
            market="us",
            symbol="RNG.US",
            trade_date=date(2026, 7, 31),
            window_end=datetime.fromisoformat("2026-07-31T23:00:00+08:00"),
            data_cutoff=datetime.fromisoformat("2026-07-31T23:00:00+08:00"),
            status="completed",
            title="量价仍待确认",
            summary="价格回升但资金持续性不足",
            conclusion="保持观察。",
            input_snapshot={"observation_count": 61},
            updated_at=datetime.now(UTC),
        )
    )
    assert calls[-1][0].endswith("FORMAT JSONEachRow")
    assert b'"analysis_id": "a1"' in (calls[-1][1] or b"")


def test_rerun_repository_round_trips_request_and_finds_active() -> None:
    calls: list[tuple[str, bytes | None]] = []
    queries: list[str] = []
    row = {
        "request_id": "rerun-1",
        "analysis_id": "analysis-1",
        "market": "us",
        "symbol": "RNG.US",
        "trade_date": "2026-07-31",
        "window_end": "2026-07-31 15:00:00.000",
        "data_cutoff": "2026-07-31 15:00:00.000",
        "status": "queued",
        "requested_at": "2026-08-02 02:00:00.000",
        "started_at": None,
        "completed_at": None,
        "updated_at": "2026-08-02 02:00:00.000",
        "error_code": None,
        "error_message": None,
    }

    def query(sql: str) -> list[dict]:
        queries.append(sql)
        return [row]

    repository = DowMonitorHalfHourAiRepository(
        query_fn=query,
        execute_fn=lambda sql, payload=None: calls.append((sql, payload)) or b"",
    )
    request = HourlyAiRerunRequest(
        request_id="rerun-1",
        analysis_id="analysis-1",
        market="us",
        symbol="RNG.US",
        trade_date=date(2026, 7, 31),
        window_end=beijing("2026-07-31T23:00:00"),
        data_cutoff=beijing("2026-07-31T23:00:00"),
        status="queued",
        requested_at=beijing("2026-08-02T10:00:00"),
        updated_at=beijing("2026-08-02T10:00:00"),
    )

    repository.ensure_schema()
    repository.save_rerun_request(request)

    assert "lb_dow_monitor_hourly_ai_rerun_requests" in (
        repository.create_rerun_table_sql
    )
    assert "ReplacingMergeTree(updated_at)" in repository.create_rerun_table_sql
    assert "ORDER BY request_id" in repository.create_rerun_table_sql
    assert calls[-1][0].endswith("FORMAT JSONEachRow")
    assert b'"request_id": "rerun-1"' in (calls[-1][1] or b"")
    assert repository.active_rerun_request("analysis-1") == request
    assert repository.next_runnable_rerun(
        now=beijing("2026-08-02T10:01:00"),
        stale_after=timedelta(minutes=10),
    ) == request
    assert "status IN ('queued', 'running')" in queries[0]
    assert "ORDER BY requested_at ASC" in queries[1]


def hourly_report() -> HourlyStageReport:
    return HourlyStageReport(
        headline=StageHeadline(
            title="尾盘V形修复，但突破未确认",
            trend_bias="TRANSITION",
            opportunity_change="STRENGTHENING",
            summary="低点后持续修复，但仍需站稳前高。",
        ),
        stage_path=[
            StagePathSegment(
                period="15:00-16:00",
                description="先下探后回升",
                metric_keys=("stage.low", "stage.close"),
            )
        ],
        hidden_changes=["低点出现在阶段前半段"],
        comparison_with_previous="修复力度增强",
        day_overview="全天仍处于区间下沿修复",
        channel=ChannelAssessment(
            direction="TRANSITION",
            maturity="FORMING",
            explanation="下降通道斜率收窄",
            evidence_metric_keys=("stage.change_pct",),
        ),
        patterns=[
            PatternAssessment(
                name="V形修复",
                status="CONFIRMED",
                explanation="低点后收复阶段跌幅",
                evidence_metric_keys=("stage.v_recovery_ratio",),
                invalidation_metric_keys=("stage.low",),
            )
        ],
        volume_capital_interpretation="尾段量能集中，但主动资金仍待确认。",
        holding_advice=PositionAdvice(
            state="HOLD_OBSERVE",
            advice="持仓者观察前高确认。",
            conditions=("站稳阶段前高",),
        ),
        watching_advice=PositionAdvice(
            state="WAIT_CONFIRMATION",
            advice="未参与者避免追高。",
            conditions=("放量站稳阶段前高",),
        ),
        next_stage_conditions=NextStageConditions(
            strengthen=("站稳阶段高点",),
            risk=("跌回VWAP下方",),
            invalidation=("跌破阶段低点",),
        ),
        confidence="MEDIUM",
    )


def test_hourly_report_model_rejects_unknown_business_enums() -> None:
    payload = hourly_report().model_dump()
    payload["headline"]["trend_bias"] = "SIDEWAYS_UP"

    with pytest.raises(ValueError):
        HourlyStageReport.model_validate(payload)


def test_repository_adds_hourly_columns_and_round_trips_structured_report() -> None:
    calls: list[tuple[str, bytes | None]] = []
    repository = DowMonitorHalfHourAiRepository(
        query_fn=lambda _sql: [],
        execute_fn=lambda sql, payload=None: calls.append((sql, payload)) or b"",
    )

    repository.ensure_schema()

    schema_sql = "\n".join(sql for sql, _payload in calls)
    assert "ADD COLUMN IF NOT EXISTS report_frequency" in schema_sql
    assert "ADD COLUMN IF NOT EXISTS stage_start" in schema_sql
    assert "ADD COLUMN IF NOT EXISTS stage_trading_minutes" in schema_sql
    assert "ADD COLUMN IF NOT EXISTS report_json" in schema_sql

    calls.clear()
    record = HalfHourAiAnalysis(
        analysis_id="hourly-1",
        market="us",
        symbol="NBIS.US",
        trade_date=date(2026, 7, 31),
        window_end=beijing("2026-08-01T04:00:00"),
        data_cutoff=beijing("2026-08-01T04:00:00"),
        report_frequency="hourly",
        stage_start=beijing("2026-08-01T03:00:00"),
        stage_trading_minutes=60,
        opportunity_change="STRENGTHENING",
        report=hourly_report(),
        status="completed",
        title="尾盘V形修复，但突破未确认",
        summary="修复增强",
        updated_at=datetime.now(UTC),
    )

    repository.save(record)

    payload = (calls[-1][1] or b"").decode("utf-8")
    assert '"report_frequency": "hourly"' in payload
    assert '"stage_trading_minutes": 60' in payload
    assert '"report_json": "{' in payload


def test_repository_reads_legacy_row_without_hourly_columns() -> None:
    row = {
        "analysis_id": "legacy-1",
        "market": "us",
        "symbol": "NBIS.US",
        "trade_date": "2026-07-31",
        "window_end": "2026-08-01 03:30:00.000",
        "data_cutoff": "2026-08-01 03:30:00.000",
        "status": "completed",
        "title": "旧报告",
        "summary": "旧摘要",
        "conclusion": "旧结论",
        "evidence_json": "[]",
        "risks_json": "[]",
        "scenarios_json": "[]",
        "data_quality_json": "[]",
        "input_snapshot_json": "{}",
        "model_name": "legacy-model",
        "attempt": 1,
        "error_code": None,
        "error_message": None,
        "created_at": "2026-08-01 03:31:00.000",
        "updated_at": "2026-08-01 03:31:00.000",
    }
    repository = DowMonitorHalfHourAiRepository(query_fn=lambda _sql: [row])

    result = repository.get_by_id("legacy-1")

    assert result is not None
    assert result.report_frequency == "half_hour"
    assert result.stage_start is None
    assert result.report is None


def test_repository_latest_completed_before_excludes_current_checkpoint() -> None:
    queries: list[str] = []
    row = {
        "analysis_id": "previous-1",
        "market": "us",
        "symbol": "NBIS.US",
        "trade_date": "2026-07-31",
        "window_end": "2026-08-01 03:00:00.000",
        "data_cutoff": "2026-08-01 03:00:00.000",
        "status": "completed",
        "title": "上一阶段",
        "summary": "上一阶段摘要",
        "conclusion": None,
        "evidence_json": "[]",
        "risks_json": "[]",
        "scenarios_json": "[]",
        "data_quality_json": "[]",
        "input_snapshot_json": "{}",
        "report_frequency": "hourly",
        "stage_start": "2026-08-01 02:00:00.000",
        "stage_trading_minutes": 60,
        "opportunity_change": "UNCHANGED",
        "report_json": hourly_report().model_dump_json(),
        "model_name": "test-model",
        "attempt": 1,
        "error_code": None,
        "error_message": None,
        "created_at": "2026-08-01 03:01:00.000",
        "updated_at": "2026-08-01 03:01:00.000",
    }
    repository = DowMonitorHalfHourAiRepository(
        query_fn=lambda sql: queries.append(sql) or [row]
    )

    result = repository.latest_completed_before(
        "us",
        "NBIS.US",
        date(2026, 7, 31),
        beijing("2026-08-01T04:00:00"),
    )

    assert result is not None
    assert result.analysis_id == "previous-1"
    assert "status = 'completed'" in queries[0]
    assert "window_end < parseDateTime64BestEffort" in queries[0]


def test_repository_marks_clickhouse_datetime_values_as_utc() -> None:
    row = {
        "analysis_id": "a1",
        "market": "cn",
        "symbol": "002714.SZ",
        "trade_date": "2026-07-31",
        "window_end": "2026-07-31 03:30:00.000",
        "status": "completed",
        "title": "量价仍待确认",
        "summary": "价格回升但资金持续性不足",
        "updated_at": "2026-07-31 03:30:02.000",
    }
    repository = DowMonitorHalfHourAiRepository(
        query_fn=lambda _sql: [row],
        execute_fn=lambda *_args: b"",
    )

    summary = repository.latest_summaries([("cn", "002714.SZ")])[
        ("cn", "002714.SZ")
    ]

    assert summary.window_end == datetime(
        2026, 7, 31, 3, 30, tzinfo=UTC
    )
    assert summary.updated_at == datetime(
        2026, 7, 31, 3, 30, 2, tzinfo=UTC
    )


def test_snapshot_excludes_rows_after_cutoff_and_uses_cumulative_scope() -> None:
    builder = HalfHourAiSnapshotBuilder(minimum_observations=2)
    snapshot = builder.build(
        market="us",
        symbol="RNG.US",
        session_open=datetime.fromisoformat("2026-07-31T21:30:00+08:00"),
        window_end=datetime.fromisoformat("2026-07-31T23:00:00+08:00"),
        data_cutoff=datetime.fromisoformat("2026-07-31T23:00:00+08:00"),
        rows=[
            {"decision_minute": "2026-07-31T21:31:00+08:00", "last_price": 53.0},
            {"decision_minute": "2026-07-31T22:29:00+08:00", "last_price": 54.0},
            {"decision_minute": "2026-07-31T23:01:00+08:00", "last_price": 99.0},
        ],
    )
    assert snapshot.observation_count == 2
    assert snapshot.latest_price == 54.0
    assert snapshot.range_start == datetime.fromisoformat(
        "2026-07-31T21:31:00+08:00"
    )
    assert snapshot.evidence_values["session_high"] == 54.0


def test_prompt_rejects_unknown_evidence_and_backend_owns_values() -> None:
    snapshot = HalfHourAiSnapshotBuilder(minimum_observations=2).build(
        market="us",
        symbol="RNG.US",
        session_open=datetime.fromisoformat("2026-07-31T21:30:00+08:00"),
        window_end=datetime.fromisoformat("2026-07-31T22:00:00+08:00"),
        data_cutoff=datetime.fromisoformat("2026-07-31T22:00:00+08:00"),
        rows=[
            {"decision_minute": "2026-07-31T21:31:00+08:00", "last_price": 53.0},
            {"decision_minute": "2026-07-31T21:59:00+08:00", "last_price": 54.0},
        ],
    )
    service = HalfHourAiPromptService(generate_text=None)
    with pytest.raises(InvalidAiAnalysis):
        service.parse_and_validate(
            '{"title":"x","summary":"x","conclusion":"x",'
            '"evidence":[{"metric_key":"invented","meaning":"x"}],'
            '"risks":["不确定"],"scenarios":[],"data_quality":["样本有限"]}',
            snapshot,
        )

    parsed = service.parse_and_validate(
        '{"title":"x","summary":"x","conclusion":"x",'
        '"evidence":[{"metric_key":"session_high","meaning":"接近日高"}],'
        '"risks":["不确定"],"scenarios":[],"data_quality":["样本有限"]}',
        snapshot,
    )
    assert parsed.evidence[0].value == "54.00"


def structured_snapshot():
    closes = [99.5, 98.5, 97.0, 95.0, 96.0, 97.0, 98.0, 99.0, 100.5]
    rows = []
    previous = 100.0
    for index, close in enumerate(closes):
        rows.append(
            {
                "decision_minute": beijing("2026-07-31T22:00:00")
                + timedelta(minutes=index),
                "minute_open": previous,
                "minute_high": max(previous, close) + 0.1,
                "minute_low": min(previous, close) - 0.1,
                "minute_close": close,
                "minute_volume": 100,
                "last_price": close,
            }
        )
        previous = close
    return HalfHourAiSnapshotBuilder().build(
        market="us",
        symbol="NBIS.US",
        session_open=beijing("2026-07-31T21:30:00"),
        stage_start=beijing("2026-07-31T22:00:00"),
        window_end=beijing("2026-07-31T22:08:00"),
        data_cutoff=beijing("2026-07-31T22:08:00"),
        rows=rows,
    )


def structured_model_payload() -> dict:
    return {
        "title": "尾盘V形修复，但突破未确认",
        "summary": "低点后持续修复，下一阶段观察前高确认。",
        "conclusion": "阶段机会较上一阶段增强，但仍属于修复而非正式突破。",
        "evidence": [
            {"metric_key": "stage.close", "meaning": "收盘重新回到阶段成本上方"}
        ],
        "risks": ["重新跌破阶段低点会否定修复结构"],
        "scenarios": [],
        "data_quality": ["分钟结构完整，资金证据仍有限"],
        "report": hourly_report().model_dump(mode="json"),
    }


def test_prompt_accepts_complete_structured_analyst_report() -> None:
    parsed = HalfHourAiPromptService(generate_text=None).parse_and_validate(
        json.dumps(structured_model_payload(), ensure_ascii=False),
        structured_snapshot(),
        require_report=True,
    )

    assert parsed.report is not None
    assert parsed.report.headline.opportunity_change == "STRENGTHENING"
    assert parsed.evidence[0].metric_key == "stage.close"


def test_prompt_rejects_missing_hourly_report_for_new_model_calls() -> None:
    payload = structured_model_payload()
    payload.pop("report")

    with pytest.raises(InvalidAiAnalysis, match="hourly stage report"):
        HalfHourAiPromptService(generate_text=None).parse_and_validate(
            json.dumps(payload, ensure_ascii=False),
            structured_snapshot(),
            require_report=True,
        )


def test_prompt_rejects_unknown_metric_key_inside_report() -> None:
    payload = structured_model_payload()
    payload["report"]["channel"]["evidence_metric_keys"] = ["invented.price"]

    with pytest.raises(InvalidAiAnalysis, match="unknown evidence key"):
        HalfHourAiPromptService(generate_text=None).parse_and_validate(
            json.dumps(payload, ensure_ascii=False),
            structured_snapshot(),
            require_report=True,
        )


def test_prompt_rejects_indicator_only_narration_even_with_valid_shape() -> None:
    payload = structured_model_payload()
    report = payload["report"]
    report["headline"]["summary"] = "VWAP、MACD、RSI、量比"
    report["comparison_with_previous"] = "VWAP、MACD、RSI、量比"
    report["day_overview"] = "VWAP、MACD、RSI、量比"
    report["volume_capital_interpretation"] = "VWAP、MACD、RSI、量比"
    report["channel"]["explanation"] = "VWAP、MACD、RSI、量比"
    for advice_key in ("holding_advice", "watching_advice"):
        report[advice_key]["advice"] = "VWAP、MACD、RSI、量比"

    with pytest.raises(InvalidAiAnalysis, match="indicator narration"):
        HalfHourAiPromptService(generate_text=None).parse_and_validate(
            json.dumps(payload, ensure_ascii=False),
            structured_snapshot(),
            require_report=True,
        )


@pytest.mark.asyncio
async def test_analyze_requests_senior_analyst_report_with_larger_token_budget() -> None:
    calls: list[tuple[list[dict], dict]] = []

    async def generate(messages, **kwargs):
        calls.append((messages, kwargs))
        return json.dumps(structured_model_payload(), ensure_ascii=False)

    parsed = await HalfHourAiPromptService(generate_text=generate).analyze(
        structured_snapshot()
    )

    assert parsed.report is not None
    assert calls[0][1]["max_tokens"] == 3200
    assert calls[0][1]["temperature"] == 0.2
    assert "高级盘中证券分析师" in calls[0][0][0]["content"]


@pytest.mark.asyncio
async def test_analyze_repairs_invalid_structured_output_once() -> None:
    calls: list[tuple[list[dict], dict]] = []
    responses = [
        "这不是结构化 JSON",
        json.dumps(structured_model_payload(), ensure_ascii=False),
    ]

    async def generate(messages, **kwargs):
        calls.append((messages, kwargs))
        return responses[len(calls) - 1]

    parsed = await HalfHourAiPromptService(generate_text=generate).analyze(
        structured_snapshot()
    )

    assert parsed.report is not None
    assert len(calls) == 2
    assert calls[1][1]["temperature"] == 0
    assert calls[1][1]["max_tokens"] == 3200
    assert "结构化 JSON 修复器" in calls[1][0][0]["content"]
    assert "这不是结构化 JSON" in calls[1][0][1]["content"]


@pytest.mark.asyncio
async def test_analyze_stops_after_one_failed_repair() -> None:
    calls = 0

    async def generate(_messages, **_kwargs):
        nonlocal calls
        calls += 1
        return "仍然不是结构化 JSON"

    with pytest.raises(InvalidAiAnalysis, match="one repair attempt"):
        await HalfHourAiPromptService(generate_text=generate).analyze(
            structured_snapshot()
        )

    assert calls == 2


def test_cumulative_query_has_both_time_boundaries() -> None:
    sql: list[str] = []
    repository = DowMonitorMinuteResultRepository(
        query_fn=lambda statement: sql.append(statement) or [],
        execute_fn=lambda *_args: b"",
    )
    repository.load_cumulative_rows(
        ["RNG.US"],
        datetime.fromisoformat("2026-07-31T21:30:00+08:00"),
        datetime.fromisoformat("2026-07-31T23:00:00+08:00"),
    )
    assert "decision_minute >=" in sql[0]
    assert "decision_minute <=" in sql[0]


BEIJING = ZoneInfo("Asia/Shanghai")


def beijing(value: str) -> datetime:
    return datetime.fromisoformat(value).replace(tzinfo=BEIJING)


def monitored_symbol(
    symbol: str = "RNG.US",
    *,
    created_at: datetime | None = None,
) -> MonitoredSymbol:
    created = created_at or beijing("2026-07-31T22:17:00")
    return MonitoredSymbol(
        symbol=symbol,
        market="us",
        enabled=True,
        created_at=created,
        updated_at=created,
    )


def minute_row(observed_at: datetime, price: float) -> dict:
    return {
        "decision_minute": observed_at.isoformat(),
        "last_price": price,
    }


def sufficient_rows(window_end: datetime) -> list[dict]:
    return [
        minute_row(beijing("2026-07-31T21:31:00"), 53),
        minute_row(window_end, 54),
    ]


class WorkerStore:
    def __init__(self, symbols: list[MonitoredSymbol]) -> None:
        self.symbols = symbols

    def list_symbols(self):
        return list(self.symbols)


class WorkerCalendar:
    def __init__(
        self,
        completed: list[datetime],
        *,
        regular_session_creation: bool = True,
    ) -> None:
        self.completed = completed
        self.regular_session_creation = regular_session_creation
        self.regular_checks: list[tuple[str, datetime]] = []

    def completed_window_ends(self, _market, _now):
        return list(self.completed)

    def is_regular_session_time(self, market, observed_at):
        self.regular_checks.append((market, observed_at))
        return self.regular_session_creation

    def session_open(self, _market, _trade_date):
        return beijing("2026-07-31T21:30:00")

    def trade_date_for_checkpoint(self, _market, _window):
        return date(2026, 7, 31)


class WorkerMinuteRepository:
    def __init__(
        self,
        rows_by_symbol: dict[str, list[list[dict]]] | None = None,
    ) -> None:
        self.rows_by_symbol = rows_by_symbol or {}
        self.loads: list[tuple[str, datetime, datetime]] = []

    def load_cumulative_rows(self, symbols, start, end):
        symbol = symbols[0]
        self.loads.append((symbol, start, end))
        batches = self.rows_by_symbol.setdefault(symbol, [[]])
        rows = batches.pop(0) if len(batches) > 1 else batches[0]
        return {symbol: list(rows)}


class WorkerAnalysisRepository:
    def __init__(
        self,
        terminal: set[tuple[str, datetime]] | None = None,
    ) -> None:
        self.terminal = terminal or set()
        self.saved: list[HalfHourAiAnalysis] = []
        self.previous: HalfHourAiAnalysis | None = None
        self.source: HalfHourAiAnalysis | None = None
        self.rerun_requests: list[HourlyAiRerunRequest] = []

    def exists_completed(self, _market, symbol, _trade_date, window_end):
        return (symbol, window_end) in self.terminal

    def save(self, record):
        self.saved.append(record)
        if record.status in {"completed", "insufficient_data", "failed"}:
            self.terminal.add((record.symbol, record.window_end))

    def latest_completed_before(self, _market, _symbol, _trade_date, _window_end):
        return self.previous

    def get_by_id(self, analysis_id):
        if self.source is not None and self.source.analysis_id == analysis_id:
            return self.source
        return None

    def next_runnable_rerun(self, *, now, stale_after):
        del now, stale_after
        return next(
            (
                item
                for item in self.rerun_requests
                if item.status in {"queued", "running"}
            ),
            None,
        )

    def save_rerun_request(self, request):
        self.rerun_requests.append(request)


class WorkerPrompt:
    def __init__(self) -> None:
        self.snapshots = []
        self.error: Exception | None = None

    @property
    def window_ends(self):
        return [snapshot.window_end for snapshot in self.snapshots]

    async def analyze(self, snapshot):
        self.snapshots.append(snapshot)
        if self.error is not None:
            raise self.error
        return ParsedAiAnalysis(
            title="等待确认",
            summary="价格回升，资金证据仍不足",
            conclusion="保持观察。",
            evidence=[],
            risks=["样本有限"],
            scenarios=[],
            data_quality=["覆盖至检查点"],
        )


class WorkerBootstrap:
    def __init__(
        self,
        outcomes: dict[str, OfflineBootstrapOutcome | Exception] | None = None,
    ) -> None:
        self.outcomes = outcomes or {}
        self.window_ends: list[datetime] = []

    async def ensure_checkpoint(self, *, symbol, session_open, window_end):
        self.window_ends.append(window_end)
        outcome = self.outcomes.get(
            symbol.symbol,
            OfflineBootstrapOutcome(
                status="completed",
                attempted=True,
                written_rows=2,
            ),
        )
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def make_worker(
    *,
    symbols: list[MonitoredSymbol] | None = None,
    completed: list[datetime] | None = None,
    rows_by_symbol: dict[str, list[list[dict]]] | None = None,
    terminal: set[tuple[str, datetime]] | None = None,
    bootstrap_outcomes: (
        dict[str, OfflineBootstrapOutcome | Exception] | None
    ) = None,
    regular_session_creation: bool = True,
):
    calendar = WorkerCalendar(
        completed or [beijing("2026-07-31T22:00:00")],
        regular_session_creation=regular_session_creation,
    )
    minute_repository = WorkerMinuteRepository(rows_by_symbol)
    analysis_repository = WorkerAnalysisRepository(terminal)
    prompt = WorkerPrompt()
    bootstrap = WorkerBootstrap(bootstrap_outcomes)
    worker = DowMonitorHalfHourAiWorker(
        monitor_store=WorkerStore(symbols or [monitored_symbol()]),
        minute_repository=minute_repository,
        analysis_repository=analysis_repository,
        calendar=calendar,
        snapshot_builder=HalfHourAiSnapshotBuilder(minimum_observations=2),
        prompt_service=prompt,
        offline_bootstrap=bootstrap,
        now_fn=lambda: beijing("2026-07-31T22:17:05"),
    )
    return (
        worker,
        calendar,
        minute_repository,
        analysis_repository,
        prompt,
        bootstrap,
    )


def manual_rerun_source() -> HalfHourAiAnalysis:
    return HalfHourAiAnalysis(
        analysis_id="analysis-manual-1",
        market="us",
        symbol="RNG.US",
        trade_date=date(2026, 7, 31),
        window_end=beijing("2026-07-31T23:00:00"),
        data_cutoff=beijing("2026-07-31T23:00:00"),
        report_frequency="hourly",
        stage_start=beijing("2026-07-31T22:00:00"),
        stage_trading_minutes=60,
        status="completed",
        attempt=1,
        title="原报告",
        summary="原摘要",
        conclusion="原结论",
        report=hourly_report(),
        updated_at=beijing("2026-07-31T23:00:05"),
    )


def manual_rerun_request(source: HalfHourAiAnalysis) -> HourlyAiRerunRequest:
    return HourlyAiRerunRequest(
        request_id="rerun-manual-1",
        analysis_id=source.analysis_id,
        market=source.market,
        symbol=source.symbol,
        trade_date=source.trade_date,
        window_end=source.window_end,
        data_cutoff=source.data_cutoff,
        status="queued",
        requested_at=beijing("2026-08-02T10:00:00"),
        updated_at=beijing("2026-08-02T10:00:00"),
    )


@pytest.mark.asyncio
async def test_manual_rerun_worker_runs_off_session_at_original_cutoff() -> None:
    source = manual_rerun_source()
    rows = sufficient_rows(source.window_end) + [
        minute_row(beijing("2026-07-31T23:01:00"), 99)
    ]
    worker, calendar, minute_repository, repository, prompt, _bootstrap = (
        make_worker(rows_by_symbol={"RNG.US": [rows]})
    )
    calendar.completed = []
    repository.source = source
    repository.rerun_requests = [manual_rerun_request(source)]

    assert await worker.run_next_manual_rerun(
        beijing("2026-08-02T10:05:00")
    ) == 1

    replacement = repository.saved[-1]
    assert replacement.analysis_id == source.analysis_id
    assert replacement.window_end == source.window_end
    assert replacement.data_cutoff == source.data_cutoff
    assert replacement.attempt == source.attempt + 1
    assert minute_repository.loads[-1][2] == source.data_cutoff
    assert prompt.snapshots[-1].latest_price == 54
    assert repository.rerun_requests[-1].status == "completed"


@pytest.mark.asyncio
async def test_manual_rerun_worker_failure_preserves_previous_report() -> None:
    source = manual_rerun_source()
    worker, _calendar, _minutes, repository, prompt, _bootstrap = make_worker(
        rows_by_symbol={"RNG.US": [sufficient_rows(source.window_end)]}
    )
    repository.source = source
    repository.rerun_requests = [manual_rerun_request(source)]
    prompt.error = RuntimeError("model unavailable")

    assert await worker.run_next_manual_rerun(
        beijing("2026-08-02T10:05:00")
    ) == 0

    assert repository.saved == []
    assert repository.get_by_id(source.analysis_id) is source
    failed = repository.rerun_requests[-1]
    assert failed.status == "failed"
    assert failed.error_code == "RuntimeError"


def test_select_due_windows_never_falls_back_from_terminal_latest_startup() -> None:
    created_at = beijing("2026-07-31T22:17:00")
    latest = beijing("2026-07-31T22:00:00")
    normal = beijing("2026-07-31T22:30:00")

    selected = worker_module.select_due_windows(
        completed_windows=[
            beijing("2026-07-31T21:30:00"),
            latest,
            normal,
        ],
        created_at=created_at,
        terminal_window_ends={latest},
    )

    assert selected == [normal]


def test_select_due_windows_treats_created_at_equality_as_normal() -> None:
    created_at = beijing("2026-07-31T22:00:00")
    previous = beijing("2026-07-31T21:30:00")

    selected = worker_module.select_due_windows(
        completed_windows=[previous, created_at],
        created_at=created_at,
        terminal_window_ends=set(),
    )

    assert selected == [created_at]


def test_select_due_windows_returns_only_latest_missed_normal_checkpoint() -> None:
    created_at = beijing("2026-07-31T21:45:00")
    latest = beijing("2026-08-01T00:00:00")

    selected = worker_module.select_due_windows(
        completed_windows=[
            beijing("2026-07-31T22:00:00"),
            beijing("2026-07-31T23:00:00"),
            latest,
        ],
        created_at=created_at,
        terminal_window_ends=set(),
    )

    assert selected == [latest]


def test_select_due_windows_does_not_fall_back_when_latest_normal_is_terminal() -> None:
    created_at = beijing("2026-07-31T21:45:00")
    latest = beijing("2026-08-01T00:00:00")

    selected = worker_module.select_due_windows(
        completed_windows=[
            beijing("2026-07-31T22:00:00"),
            beijing("2026-07-31T23:00:00"),
            latest,
        ],
        created_at=created_at,
        terminal_window_ends={latest},
    )

    assert selected == []


@pytest.mark.parametrize(
    ("market", "observed_at"),
    [
        ("hk", beijing("2026-07-31T12:30:00")),
        ("hk", beijing("2026-07-31T16:30:00")),
        ("us", beijing("2026-07-03T22:17:00")),
    ],
    ids=["hk-lunch", "hk-after-close", "us-holiday"],
)
def test_calendar_rejects_non_regular_symbol_creation_times(
    market: str,
    observed_at: datetime,
) -> None:
    assert not HalfHourWindowCalendar().is_regular_session_time(
        market,
        observed_at,
    )


@pytest.mark.asyncio
async def test_new_symbol_analyzes_only_latest_completed_startup_checkpoint() -> None:
    worker, _, _, _, prompt, bootstrap = make_worker(
        completed=[
            beijing("2026-07-31T21:30:00"),
            beijing("2026-07-31T22:00:00"),
        ],
        rows_by_symbol={
            "RNG.US": [
                [minute_row(beijing("2026-07-31T21:31:00"), 53)],
                sufficient_rows(beijing("2026-07-31T22:00:00")),
            ]
        },
    )

    await worker.run_due_jobs(now=beijing("2026-07-31T22:17:05"))

    assert bootstrap.window_ends == [beijing("2026-07-31T22:00:00")]
    assert prompt.window_ends == [beijing("2026-07-31T22:00:00")]


@pytest.mark.asyncio
async def test_terminal_latest_startup_checkpoint_does_not_fall_back_older() -> None:
    latest = beijing("2026-07-31T22:00:00")
    worker, _, minute_repository, _, prompt, bootstrap = make_worker(
        completed=[beijing("2026-07-31T21:30:00"), latest],
        terminal={("RNG.US", latest)},
    )

    await worker.run_due_jobs()

    assert minute_repository.loads == []
    assert bootstrap.window_ends == []
    assert prompt.window_ends == []


@pytest.mark.asyncio
async def test_next_normal_checkpoint_runs_after_startup_checkpoint() -> None:
    startup = beijing("2026-07-31T22:00:00")
    normal = beijing("2026-07-31T22:30:00")
    worker, _, _, _, prompt, bootstrap = make_worker(
        completed=[startup, normal],
        terminal={("RNG.US", startup)},
        rows_by_symbol={"RNG.US": [sufficient_rows(normal)]},
    )

    await worker.run_due_jobs(now=beijing("2026-07-31T22:30:05"))

    assert bootstrap.window_ends == []
    assert prompt.window_ends == [normal]


@pytest.mark.asyncio
async def test_non_regular_creation_suppresses_only_pre_created_startup() -> None:
    created_at = beijing("2026-07-31T22:17:00")
    startup = beijing("2026-07-31T22:00:00")
    normal = beijing("2026-07-31T22:30:00")
    worker, calendar, _, _, prompt, bootstrap = make_worker(
        symbols=[monitored_symbol(created_at=created_at)],
        completed=[startup, normal],
        rows_by_symbol={"RNG.US": [sufficient_rows(normal)]},
        regular_session_creation=False,
    )

    assert await worker.run_due_jobs(now=beijing("2026-07-31T22:30:05")) == 1

    assert calendar.regular_checks == [("us", created_at)]
    assert bootstrap.window_ends == []
    assert prompt.window_ends == [normal]


@pytest.mark.asyncio
async def test_normal_checkpoint_can_bootstrap_missing_canonical_rows() -> None:
    normal = beijing("2026-07-31T22:00:00")
    worker, _, _, _, prompt, bootstrap = make_worker(
        symbols=[
            monitored_symbol(
                created_at=beijing("2026-07-31T21:45:00"),
            )
        ],
        rows_by_symbol={
            "RNG.US": [
                [minute_row(beijing("2026-07-31T21:31:00"), 53)],
                sufficient_rows(normal),
            ]
        },
    )

    assert await worker.run_due_jobs() == 1

    assert bootstrap.window_ends == [normal]
    assert prompt.window_ends == [normal]


@pytest.mark.asyncio
async def test_sufficient_canonical_rows_skip_bootstrap_and_invoke_model_once() -> None:
    window_end = beijing("2026-07-31T22:00:00")
    worker, _, _, _, prompt, bootstrap = make_worker(
        rows_by_symbol={"RNG.US": [sufficient_rows(window_end)]}
    )

    assert await worker.run_due_jobs() == 1

    assert bootstrap.window_ends == []
    assert prompt.window_ends == [window_end]


@pytest.mark.asyncio
async def test_insufficient_snapshot_bootstraps_reloads_and_then_invokes_model() -> None:
    window_end = beijing("2026-07-31T22:00:00")
    worker, _, minute_repository, _, prompt, bootstrap = make_worker(
        rows_by_symbol={
            "RNG.US": [
                [minute_row(beijing("2026-07-31T21:31:00"), 53)],
                sufficient_rows(window_end),
            ]
        }
    )

    assert await worker.run_due_jobs() == 1

    assert bootstrap.window_ends == [window_end]
    assert [load[2] for load in minute_repository.loads] == [
        window_end,
        window_end,
    ]
    assert prompt.window_ends == [window_end]


@pytest.mark.asyncio
async def test_materialization_still_insufficient_saves_data_result_without_model() -> None:
    window_end = beijing("2026-07-31T22:00:00")
    one_row = [minute_row(beijing("2026-07-31T21:31:00"), 53)]
    worker, _, _, repository, prompt, bootstrap = make_worker(
        rows_by_symbol={"RNG.US": [one_row, one_row]}
    )

    assert await worker.run_due_jobs() == 0

    assert bootstrap.window_ends == [window_end]
    assert prompt.window_ends == []
    terminal = repository.saved[-1]
    assert terminal.status == "insufficient_data"
    assert terminal.error_code == "INSUFFICIENT_DATA"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("outcome", "error_code"),
    [
        (
            OfflineBootstrapOutcome(
                status="budget_exceeded",
                attempted=True,
                error_code="BACKFILL_BUDGET_EXCEEDED",
                error_message="checkpoint requires 501 rows; limit is 500",
            ),
            "BACKFILL_BUDGET_EXCEEDED",
        ),
        (
            OfflineBootstrapOutcome(
                status="timed_out",
                attempted=True,
                error_code="BACKFILL_TIMEOUT",
                error_message="materialization exceeded 15 seconds",
            ),
            "BACKFILL_TIMEOUT",
        ),
        (
            OfflineBootstrapOutcome(
                status="failed",
                attempted=True,
                error_code="BACKFILL_SOURCE_UNAVAILABLE",
                error_message="raw quote partition unavailable",
            ),
            "BACKFILL_SOURCE_UNAVAILABLE",
        ),
    ],
    ids=["budget", "timeout", "failure"],
)
async def test_terminal_bootstrap_error_preserves_diagnostic_without_model(
    outcome: OfflineBootstrapOutcome,
    error_code: str,
) -> None:
    worker, _, _, repository, prompt, _ = make_worker(
        bootstrap_outcomes={"RNG.US": outcome}
    )

    assert await worker.run_due_jobs() == 0

    assert prompt.window_ends == []
    terminal = repository.saved[-1]
    assert terminal.status == "insufficient_data"
    assert terminal.error_code == error_code
    assert terminal.error_message == outcome.error_message


@pytest.mark.asyncio
async def test_busy_bootstrap_saves_no_terminal_row_and_next_poll_can_retry() -> None:
    busy = OfflineBootstrapOutcome(
        status="busy",
        attempted=False,
        error_code="BACKFILL_BUSY",
        error_message="another checkpoint is materializing",
    )
    worker, _, _, repository, prompt, _ = make_worker(
        bootstrap_outcomes={"RNG.US": busy}
    )

    assert await worker.run_due_jobs() == 0

    assert repository.saved == []
    assert prompt.window_ends == []


@pytest.mark.asyncio
async def test_one_symbol_bootstrap_failure_does_not_stop_next_symbol() -> None:
    window_end = beijing("2026-07-31T22:00:00")
    worker, _, _, _, prompt, _ = make_worker(
        symbols=[monitored_symbol("BAD.US"), monitored_symbol("GOOD.US")],
        rows_by_symbol={
            "BAD.US": [[]],
            "GOOD.US": [sufficient_rows(window_end)],
        },
        bootstrap_outcomes={
            "BAD.US": RuntimeError("coordinator unavailable"),
        },
    )

    assert await worker.run_due_jobs() == 1

    assert prompt.window_ends == [window_end]
    assert prompt.snapshots[0].symbol == "GOOD.US"


@pytest.mark.asyncio
async def test_existing_terminal_key_skips_bootstrap_and_model() -> None:
    window_end = beijing("2026-07-31T22:00:00")
    worker, _, minute_repository, repository, prompt, bootstrap = make_worker(
        terminal={("RNG.US", window_end)}
    )

    assert await worker.run_due_jobs() == 0

    assert minute_repository.loads == []
    assert repository.saved == []
    assert bootstrap.window_ends == []
    assert prompt.window_ends == []


@pytest.mark.asyncio
async def test_no_completed_checkpoint_does_nothing() -> None:
    worker, calendar, minute_repository, repository, prompt, bootstrap = (
        make_worker()
    )
    calendar.completed = []

    assert await worker.run_due_jobs() == 0

    assert minute_repository.loads == []
    assert repository.saved == []
    assert bootstrap.window_ends == []
    assert prompt.window_ends == []


@pytest.mark.asyncio
async def test_worker_never_loads_or_analyzes_rows_after_window_end() -> None:
    window_end = beijing("2026-07-31T22:00:00")
    rows = sufficient_rows(window_end) + [
        minute_row(beijing("2026-07-31T22:01:00"), 99)
    ]
    worker, _, minute_repository, _, prompt, bootstrap = make_worker(
        rows_by_symbol={"RNG.US": [rows]}
    )

    assert await worker.run_due_jobs() == 1

    assert minute_repository.loads[0][2] == window_end
    assert bootstrap.window_ends == []
    assert prompt.snapshots[0].observation_count == 2
    assert prompt.snapshots[0].latest_price == 54
    assert prompt.snapshots[0].data_cutoff == window_end


@pytest.mark.asyncio
async def test_worker_uses_previous_report_as_hourly_stage_boundary() -> None:
    previous_end = beijing("2026-07-31T22:00:00")
    current_end = beijing("2026-07-31T23:00:00")
    previous = HalfHourAiAnalysis(
        analysis_id="previous",
        market="us",
        symbol="RNG.US",
        trade_date=date(2026, 7, 31),
        window_end=previous_end,
        data_cutoff=previous_end,
        report_frequency="hourly",
        stage_start=beijing("2026-07-31T21:30:00"),
        stage_trading_minutes=31,
        status="completed",
        input_snapshot={
            "market_structure": {
                "trend_bias": "BEARISH",
                "opportunity_score": -0.5,
            }
        },
        updated_at=datetime.now(UTC),
    )
    rows = []
    previous_close = 100.0
    for minute, close in enumerate([100.2, 100.5, 100.8, 101.0, 101.3]):
        rows.append(
            {
                "decision_minute": previous_end + timedelta(minutes=minute + 1),
                "last_price": close,
                "minute_open": previous_close,
                "minute_high": max(previous_close, close) + 0.1,
                "minute_low": min(previous_close, close) - 0.1,
                "minute_close": close,
                "minute_volume": 100,
            }
        )
        previous_close = close
    worker, _, _, repository, prompt, _ = make_worker(
        completed=[current_end],
        rows_by_symbol={"RNG.US": [rows]},
    )
    repository.previous = previous

    assert await worker.run_due_jobs(now=current_end) == 1

    snapshot = prompt.snapshots[0]
    assert snapshot.stage_start == previous_end
    assert snapshot.previous_stage is not None
    assert snapshot.previous_stage.trend_bias == "BEARISH"
    assert snapshot.market_structure.opportunity_change == "REVERSING"
    completed = repository.saved[-1]
    assert completed.report_frequency == "hourly"
    assert completed.stage_start == previous_end
    assert completed.stage_trading_minutes == 5


def test_overview_is_lightweight_and_detail_is_loaded_on_demand(tmp_path) -> None:
    analysis = HalfHourAiAnalysis(
        analysis_id="analysis-1",
        market="us",
        symbol="RNG.US",
        trade_date=date(2026, 7, 31),
        window_end=datetime.fromisoformat("2026-07-31T23:00:00+08:00"),
        data_cutoff=datetime.fromisoformat("2026-07-31T23:00:00+08:00"),
        report_frequency="hourly",
        stage_start=datetime.fromisoformat("2026-07-31T22:00:00+08:00"),
        stage_trading_minutes=60,
        opportunity_change="STRENGTHENING",
        status="completed",
        title="量价仍待确认",
        summary="价格回升但资金持续性不足",
        conclusion="长内容只在详情中返回。",
        risks=["样本有限"],
        data_quality=["完整"],
        input_snapshot={"observation_count": 61},
        report=hourly_report(),
        updated_at=datetime.now(UTC),
    )

    class AiRepository:
        def latest_summaries(self, _keys):
            return {("us", "RNG.US"): analysis}

        def list_history(self, _market, _symbol, _trade_date):
            return [analysis]

        def get_by_id(self, _analysis_id):
            return analysis

    store = DowMonitorStore(tmp_path)
    store.upsert_symbol("RNG.US", "us", True)
    service = DowMonitorService(
        store,
        object(),
        object(),
        lambda *_args: None,
        half_hour_ai_repository=AiRepository(),
        now_fn=lambda: datetime.fromisoformat("2026-07-31T23:00:01+08:00"),
    )
    app = FastAPI()
    app.state.dow_monitor_service = service
    app.include_router(dow_monitor.router)
    client = TestClient(app)

    overview = client.get("/api/dow-monitor/overview").json()["symbols"][0]
    assert overview["half_hour_ai_analysis"]["analysis_id"] == "analysis-1"
    assert overview["half_hour_ai_analysis"]["report_frequency"] == "hourly"
    assert overview["half_hour_ai_analysis"]["stage_trading_minutes"] == 60
    assert overview["half_hour_ai_analysis"]["opportunity_change"] == "STRENGTHENING"
    assert "conclusion" not in overview["half_hour_ai_analysis"]
    assert "report" not in overview["half_hour_ai_analysis"]
    history = client.get(
        "/api/dow-monitor/RNG.US/ai-analyses",
        params={"trade_date": "2026-07-31"},
    )
    assert history.status_code == 200
    detail = client.get(
        "/api/dow-monitor/RNG.US/ai-analyses/analysis-1"
    )
    assert detail.status_code == 200
    assert detail.json()["conclusion"] == "长内容只在详情中返回。"
    assert detail.json()["report"]["headline"]["title"] == "尾盘V形修复，但突破未确认"


def _manual_rerun_api_client(
    tmp_path,
    *,
    report_frequency: str = "hourly",
    enabled: bool = True,
    repository_available: bool = True,
) -> tuple[TestClient, object]:
    analysis = HalfHourAiAnalysis(
        analysis_id="analysis-1",
        market="us",
        symbol="RNG.US",
        trade_date=date(2026, 7, 31),
        window_end=beijing("2026-07-31T23:00:00"),
        data_cutoff=beijing("2026-07-31T23:00:00"),
        report_frequency=report_frequency,
        status="completed",
        title="阶段报告",
        summary="阶段摘要",
        updated_at=beijing("2026-08-02T10:00:00"),
    )

    class AiRepository:
        def __init__(self) -> None:
            self.requests: list[HourlyAiRerunRequest] = []

        def get_by_id(self, analysis_id: str):
            return analysis if analysis_id == analysis.analysis_id else None

        def active_rerun_request(self, analysis_id: str):
            return next(
                (
                    item
                    for item in reversed(self.requests)
                    if item.analysis_id == analysis_id
                    and item.status in {"queued", "running"}
                ),
                None,
            )

        def latest_rerun_request(self, analysis_id: str):
            return next(
                (
                    item
                    for item in reversed(self.requests)
                    if item.analysis_id == analysis_id
                ),
                None,
            )

        def save_rerun_request(self, request: HourlyAiRerunRequest) -> None:
            self.requests.append(request)

    repository = AiRepository()
    store = DowMonitorStore(tmp_path)
    store.upsert_symbol("RNG.US", "us", enabled)
    service = DowMonitorService(
        store,
        object(),
        object(),
        lambda *_args: None,
        half_hour_ai_repository=(repository if repository_available else None),
        now_fn=lambda: beijing("2026-08-02T10:05:00"),
    )
    app = FastAPI()
    app.state.dow_monitor_service = service
    app.include_router(dow_monitor.router)
    return TestClient(app), repository


def test_manual_rerun_api_queues_selected_report_and_deduplicates(tmp_path) -> None:
    client, _repository = _manual_rerun_api_client(tmp_path)

    response = client.post(
        "/api/dow-monitor/RNG.US/ai-analyses/analysis-1/rerun"
    )

    assert response.status_code == 202
    assert response.json()["request"]["analysis_id"] == "analysis-1"
    assert response.json()["request"]["status"] == "queued"
    assert response.json()["deduplicated"] is False

    duplicate = client.post(
        "/api/dow-monitor/RNG.US/ai-analyses/analysis-1/rerun"
    )
    assert duplicate.status_code == 200
    assert duplicate.json()["request"]["request_id"] == (
        response.json()["request"]["request_id"]
    )
    assert duplicate.json()["deduplicated"] is True

    status = client.get(
        "/api/dow-monitor/RNG.US/ai-analyses/analysis-1/rerun"
    )
    assert status.status_code == 200
    assert status.json()["request"]["status"] == "queued"


def test_manual_rerun_service_deduplicates_concurrent_requests(tmp_path) -> None:
    client, repository = _manual_rerun_api_client(tmp_path)
    service = client.app.state.dow_monitor_service
    original_active = repository.active_rerun_request
    callers_ready = Barrier(2)

    def slow_active(analysis_id: str):
        active = original_active(analysis_id)
        sleep(0.05)
        return active

    repository.active_rerun_request = slow_active

    def submit():
        callers_ready.wait()
        return service.request_hourly_ai_rerun("us", "RNG.US", "analysis-1")

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _index: submit(), range(2)))

    assert len(repository.requests) == 1
    assert {deduplicated for _request, deduplicated in results} == {False, True}
    assert len({request["request_id"] for request, _deduplicated in results}) == 1


@pytest.mark.parametrize(
    ("options", "analysis_id", "expected_status"),
    [
        ({"report_frequency": "half_hour"}, "analysis-1", 409),
        ({"enabled": False}, "analysis-1", 409),
        ({}, "missing-analysis", 404),
        ({"repository_available": False}, "analysis-1", 503),
    ],
    ids=["legacy", "disabled", "missing", "storage-unavailable"],
)
def test_manual_rerun_api_rejects_ineligible_requests(
    tmp_path,
    options: dict,
    analysis_id: str,
    expected_status: int,
) -> None:
    client, _repository = _manual_rerun_api_client(tmp_path, **options)

    response = client.post(
        f"/api/dow-monitor/RNG.US/ai-analyses/{analysis_id}/rerun"
    )

    assert response.status_code == expected_status
