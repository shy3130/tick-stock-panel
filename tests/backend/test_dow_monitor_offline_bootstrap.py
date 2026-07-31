from __future__ import annotations

import asyncio
import gc
import threading
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest

from app.services.dow_monitor_minute_result_materializer import (
    MaterializeError,
    MaterializeRun,
)
from app.services.dow_monitor_models import MonitoredSymbol
from app.services.dow_monitor_offline_bootstrap import DowMonitorOfflineBootstrap


BEIJING = ZoneInfo("Asia/Shanghai")
NOW = datetime(2026, 7, 31, 14, 17, tzinfo=UTC)
SESSION_OPEN = datetime(2026, 7, 31, 21, 30, tzinfo=BEIJING)
WINDOW_END = datetime(2026, 7, 31, 22, 0, tzinfo=BEIJING)


def monitored_symbol(symbol: str = "RNG.US") -> MonitoredSymbol:
    return MonitoredSymbol(
        symbol=symbol,
        market="us",
        enabled=True,
        created_at=NOW,
        updated_at=NOW,
    )


async def ensure_checkpoint(
    coordinator: DowMonitorOfflineBootstrap,
):
    return await coordinator.ensure_checkpoint(
        symbol=monitored_symbol(),
        session_open=SESSION_OPEN,
        window_end=WINDOW_END,
    )


class ImmediateMaterializer:
    def __init__(
        self,
        run: MaterializeRun | None = None,
        *,
        failure: Exception | None = None,
    ) -> None:
        self.run = run or MaterializeRun()
        self.failure = failure
        self.calls = []
        self.thread_ids: list[int] = []

    def materialize_checkpoint(
        self,
        *,
        symbol,
        session_open,
        window_end,
        max_rows,
    ) -> MaterializeRun:
        self.calls.append((symbol, session_open, window_end, max_rows))
        self.thread_ids.append(threading.get_ident())
        if self.failure is not None:
            raise self.failure
        return self.run


class ControlledMaterializer(ImmediateMaterializer):
    def __init__(
        self,
        run: MaterializeRun | None = None,
        *,
        failure: Exception | None = None,
    ) -> None:
        super().__init__(run, failure=failure)
        self.entries = 0
        self._entry_lock = threading.Lock()
        self.started = threading.Event()
        self.release = threading.Event()
        self.finished = threading.Event()

    def materialize_checkpoint(self, **kwargs) -> MaterializeRun:
        with self._entry_lock:
            self.entries += 1
        self.started.set()
        try:
            if not self.release.wait(timeout=2):
                raise RuntimeError("test materializer was not released")
            return super().materialize_checkpoint(**kwargs)
        finally:
            self.finished.set()


@pytest.mark.asyncio
async def test_success_runs_materializer_off_event_loop() -> None:
    event_loop_thread = threading.get_ident()
    materializer = ImmediateMaterializer(MaterializeRun(written_rows=30))
    coordinator = DowMonitorOfflineBootstrap(materializer)

    outcome = await ensure_checkpoint(coordinator)

    assert outcome.status == "completed"
    assert outcome.attempted is True
    assert outcome.written_rows == 30
    assert outcome.error_code is None
    assert materializer.thread_ids
    assert event_loop_thread not in materializer.thread_ids
    assert materializer.calls == [(monitored_symbol(), SESSION_OPEN, WINDOW_END, 500)]


@pytest.mark.asyncio
async def test_zero_written_rows_reports_not_needed() -> None:
    coordinator = DowMonitorOfflineBootstrap(ImmediateMaterializer())

    outcome = await ensure_checkpoint(coordinator)

    assert outcome.status == "not_needed"
    assert outcome.attempted is True
    assert outcome.written_rows == 0


@pytest.mark.asyncio
async def test_row_budget_error_preserves_materializer_diagnostic() -> None:
    materializer = ImmediateMaterializer(
        MaterializeRun(
            error=MaterializeError(
                code="BACKFILL_BUDGET_EXCEEDED",
                message="checkpoint requires 501 rows; limit is 500",
            )
        )
    )
    coordinator = DowMonitorOfflineBootstrap(materializer, max_rows=400)

    outcome = await ensure_checkpoint(coordinator)

    assert outcome.status == "budget_exceeded"
    assert outcome.attempted is True
    assert outcome.error_code == "BACKFILL_BUDGET_EXCEEDED"
    assert outcome.error_message == "checkpoint requires 501 rows; limit is 500"
    assert materializer.calls[0][3] == 400


@pytest.mark.asyncio
async def test_other_materializer_error_preserves_code_and_diagnostic() -> None:
    coordinator = DowMonitorOfflineBootstrap(
        ImmediateMaterializer(
            MaterializeRun(
                error=MaterializeError(
                    code="BACKFILL_SOURCE_UNAVAILABLE",
                    message="raw quote partition is unavailable",
                )
            )
        )
    )

    outcome = await ensure_checkpoint(coordinator)

    assert outcome.status == "failed"
    assert outcome.error_code == "BACKFILL_SOURCE_UNAVAILABLE"
    assert outcome.error_message == "raw quote partition is unavailable"


@pytest.mark.asyncio
async def test_arbitrary_materializer_exception_becomes_failed_outcome() -> None:
    coordinator = DowMonitorOfflineBootstrap(
        ImmediateMaterializer(failure=RuntimeError("clickhouse disconnected"))
    )

    outcome = await ensure_checkpoint(coordinator)

    assert outcome.status == "failed"
    assert outcome.error_code == "BACKFILL_FAILED"
    assert outcome.error_message == "clickhouse disconnected"


@pytest.mark.asyncio
async def test_second_bootstrap_is_busy_while_single_flight_is_running() -> None:
    materializer = ControlledMaterializer(MaterializeRun(written_rows=30))
    coordinator = DowMonitorOfflineBootstrap(materializer, timeout_seconds=1)
    first = asyncio.create_task(ensure_checkpoint(coordinator))
    try:
        assert await asyncio.to_thread(materializer.started.wait, 1)

        second = await ensure_checkpoint(coordinator)

        assert second.status == "busy"
        assert second.attempted is False
        assert second.retryable is True
        assert materializer.entries == 1
    finally:
        materializer.release.set()
    assert (await first).status == "completed"
    assert len(materializer.calls) == 1


@pytest.mark.asyncio
async def test_timeout_keeps_slot_busy_until_physical_completion() -> None:
    materializer = ControlledMaterializer(MaterializeRun(written_rows=30))
    coordinator = DowMonitorOfflineBootstrap(materializer, timeout_seconds=0.1)
    try:
        timed_out = await ensure_checkpoint(coordinator)

        busy = await ensure_checkpoint(coordinator)

        assert timed_out.status == "timed_out"
        assert timed_out.attempted is True
        assert timed_out.error_code == "BACKFILL_TIMEOUT"
        assert timed_out.retryable is False
        assert busy.status == "busy"
        assert materializer.entries == 1
    finally:
        materializer.release.set()
    assert await asyncio.to_thread(materializer.finished.wait, 1)
    await asyncio.sleep(0)

    completed = await ensure_checkpoint(coordinator)

    assert completed.status == "completed"
    assert len(materializer.calls) == 2


@pytest.mark.asyncio
async def test_late_exception_is_consumed_and_releases_single_flight_slot() -> None:
    materializer = ControlledMaterializer(
        failure=RuntimeError("late clickhouse failure")
    )
    coordinator = DowMonitorOfflineBootstrap(materializer, timeout_seconds=0.1)
    loop = asyncio.get_running_loop()
    unhandled_contexts = []
    previous_handler = loop.get_exception_handler()
    loop.set_exception_handler(
        lambda _loop, context: unhandled_contexts.append(context)
    )
    try:
        timed_out = await ensure_checkpoint(coordinator)
        materializer.release.set()
        assert await asyncio.to_thread(materializer.finished.wait, 1)
        materializer.failure = None
        await asyncio.sleep(0)
        gc.collect()
        await asyncio.sleep(0)

        completed = await ensure_checkpoint(coordinator)
    finally:
        loop.set_exception_handler(previous_handler)
        materializer.release.set()

    assert timed_out.status == "timed_out"
    assert completed.status == "not_needed"
    assert len(materializer.calls) == 2
    assert unhandled_contexts == []
