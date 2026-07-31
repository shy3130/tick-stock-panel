from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import dow_monitor
from app.services.dow_monitor_half_hour_ai_calendar import HalfHourWindowCalendar
from app.services.dow_monitor_half_hour_ai_models import (
    HalfHourAiAnalysis,
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
from app.services.dow_monitor_minute_result_repository import (
    DowMonitorMinuteResultRepository,
)
from app.services.dow_monitor_models import MonitoredSymbol
from app.services.dow_monitor_service import DowMonitorService
from app.services.dow_monitor_store import DowMonitorStore
from app.workers.dow_monitor_half_hour_ai import DowMonitorHalfHourAiWorker


def test_production_override_places_ai_worker_on_host_network() -> None:
    override = (
        Path(__file__).resolve().parents[2] / "docker-compose.override.yml"
    ).read_text(encoding="utf-8")
    worker_section = override.split("  dow-ai-worker:", maxsplit=1)[1]
    assert "network_mode: host" in worker_section


def test_cn_first_due_window_is_1000_beijing() -> None:
    calendar = HalfHourWindowCalendar()
    assert calendar.completed_window_ends(
        "cn",
        datetime.fromisoformat("2026-07-31T10:00:01+08:00"),
    ) == [datetime.fromisoformat("2026-07-31T10:00:00+08:00")]


def test_hk_lunch_break_does_not_create_1230_window() -> None:
    ends = HalfHourWindowCalendar().session_window_ends(
        "hk",
        date(2026, 7, 31),
    )
    assert datetime.fromisoformat("2026-07-31T12:30:00+08:00") not in ends
    assert datetime.fromisoformat("2026-07-31T13:30:00+08:00") in ends


def test_us_dst_session_maps_to_beijing_time() -> None:
    ends = HalfHourWindowCalendar().session_window_ends(
        "us",
        date(2026, 7, 31),
    )
    assert ends[0] == datetime.fromisoformat("2026-07-31T22:00:00+08:00")


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


@pytest.mark.asyncio
async def test_new_symbol_starts_at_next_checkpoint_and_worker_is_sequential() -> None:
    created_at = datetime.fromisoformat("2026-07-31T22:45:00+08:00")

    class Store:
        def list_symbols(self):
            return [
                MonitoredSymbol(
                    symbol="RNG.US",
                    market="us",
                    enabled=True,
                    created_at=created_at,
                    updated_at=created_at,
                )
            ]

    class Calendar:
        def completed_window_ends(self, _market, _now):
            return [
                datetime.fromisoformat("2026-07-31T22:30:00+08:00"),
                datetime.fromisoformat("2026-07-31T23:00:00+08:00"),
            ]

        def session_open(self, _market, _trade_date):
            return datetime.fromisoformat("2026-07-31T21:30:00+08:00")

        def trade_date_for_checkpoint(self, _market, _window):
            return date(2026, 7, 31)

    class MinuteRepository:
        def load_cumulative_rows(self, *_args):
            return {
                "RNG.US": [
                    {
                        "decision_minute": "2026-07-31T22:46:00+08:00",
                        "last_price": 53,
                    },
                    {
                        "decision_minute": "2026-07-31T22:59:00+08:00",
                        "last_price": 54,
                    },
                ]
            }

    class AnalysisRepository:
        def __init__(self):
            self.saved: list[HalfHourAiAnalysis] = []

        def exists_completed(self, *_args):
            return False

        def save(self, record):
            self.saved.append(record)

    class Prompt:
        async def analyze(self, _snapshot):
            return ParsedAiAnalysis(
                title="等待确认",
                summary="价格回升，资金证据仍不足",
                conclusion="保持观察。",
                evidence=[],
                risks=["样本有限"],
                scenarios=[],
                data_quality=["仅覆盖新增后的分钟"],
            )

    analysis_repository = AnalysisRepository()
    worker = DowMonitorHalfHourAiWorker(
        monitor_store=Store(),
        minute_repository=MinuteRepository(),
        analysis_repository=analysis_repository,
        calendar=Calendar(),
        snapshot_builder=HalfHourAiSnapshotBuilder(minimum_observations=2),
        prompt_service=Prompt(),
        now_fn=lambda: datetime.fromisoformat("2026-07-31T23:00:01+08:00"),
    )

    assert await worker.run_due_jobs() == 1
    completed = [
        item for item in analysis_repository.saved if item.status == "completed"
    ]
    assert [item.window_end for item in completed] == [
        datetime.fromisoformat("2026-07-31T23:00:00+08:00")
    ]


def test_overview_is_lightweight_and_detail_is_loaded_on_demand(tmp_path) -> None:
    analysis = HalfHourAiAnalysis(
        analysis_id="analysis-1",
        market="us",
        symbol="RNG.US",
        trade_date=date(2026, 7, 31),
        window_end=datetime.fromisoformat("2026-07-31T23:00:00+08:00"),
        data_cutoff=datetime.fromisoformat("2026-07-31T23:00:00+08:00"),
        status="completed",
        title="量价仍待确认",
        summary="价格回升但资金持续性不足",
        conclusion="长内容只在详情中返回。",
        risks=["样本有限"],
        data_quality=["完整"],
        input_snapshot={"observation_count": 61},
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
    assert "conclusion" not in overview["half_hour_ai_analysis"]
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
