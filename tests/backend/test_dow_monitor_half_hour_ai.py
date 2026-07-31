from __future__ import annotations

import asyncio
from datetime import UTC, date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
import yaml
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import dow_monitor
from app.services.dow_monitor_client import LongbridgeDowClient
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
    def __init__(self, completed: list[datetime]) -> None:
        self.completed = completed

    def completed_window_ends(self, _market, _now):
        return list(self.completed)

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

    def exists_completed(self, _market, symbol, _trade_date, window_end):
        return (symbol, window_end) in self.terminal

    def save(self, record):
        self.saved.append(record)
        if record.status in {"completed", "insufficient_data", "failed"}:
            self.terminal.add((record.symbol, record.window_end))


class WorkerPrompt:
    def __init__(self) -> None:
        self.snapshots = []

    @property
    def window_ends(self):
        return [snapshot.window_end for snapshot in self.snapshots]

    async def analyze(self, snapshot):
        self.snapshots.append(snapshot)
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
):
    calendar = WorkerCalendar(
        completed or [beijing("2026-07-31T22:00:00")]
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
