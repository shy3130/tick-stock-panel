from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict

from app.services.dow_monitor_minute_result_materializer import (
    DowMonitorMinuteResultMaterializer,
    MaterializeError,
    MaterializeRun,
)
from app.services.dow_monitor_models import MonitoredSymbol

MAX_WAIT_SECONDS = 15.0

BootstrapStatus = Literal[
    "not_needed",
    "completed",
    "budget_exceeded",
    "timed_out",
    "busy",
    "failed",
]


class OfflineBootstrapOutcome(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: BootstrapStatus
    attempted: bool
    written_rows: int = 0
    error_code: str | None = None
    error_message: str | None = None

    @property
    def retryable(self) -> bool:
        return self.status == "busy"


class DowMonitorOfflineBootstrap:
    def __init__(
        self,
        materializer: DowMonitorMinuteResultMaterializer,
        *,
        timeout_seconds: float = MAX_WAIT_SECONDS,
        max_rows: int = 500,
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        self._materializer = materializer
        self._timeout_seconds = min(timeout_seconds, MAX_WAIT_SECONDS)
        self._max_rows = max_rows
        self._in_flight: asyncio.Task[MaterializeRun] | None = None

    async def ensure_checkpoint(
        self,
        *,
        symbol: MonitoredSymbol,
        session_open: datetime,
        window_end: datetime,
    ) -> OfflineBootstrapOutcome:
        if self._in_flight is not None:
            if self._in_flight.done():
                self._consume_and_release(self._in_flight)
            if self._in_flight is not None:
                return OfflineBootstrapOutcome(
                    status="busy",
                    attempted=False,
                    error_code="BACKFILL_BUSY",
                    error_message="offline checkpoint materialization is already running",
                )

        task = asyncio.create_task(
            asyncio.to_thread(
                self._materializer.materialize_checkpoint,
                symbol=symbol,
                session_open=session_open,
                window_end=window_end,
                max_rows=self._max_rows,
            )
        )
        self._in_flight = task
        task.add_done_callback(self._consume_and_release)

        try:
            run = await asyncio.wait_for(
                asyncio.shield(task),
                timeout=self._timeout_seconds,
            )
        except TimeoutError:
            # Python cannot forcibly stop a blocking DB call already running in a
            # thread. The timeout is the worker's wait/decision budget; the
            # shielded task retains the slot until physical completion.
            return OfflineBootstrapOutcome(
                status="timed_out",
                attempted=True,
                error_code="BACKFILL_TIMEOUT",
                error_message=(
                    "offline checkpoint materialization exceeded "
                    f"the {self._timeout_seconds:g}-second wait budget"
                ),
            )
        except Exception as exc:
            return OfflineBootstrapOutcome(
                status="failed",
                attempted=True,
                error_code="BACKFILL_FAILED",
                error_message=str(exc),
            )

        return self._outcome_from_run(run)

    def _consume_and_release(self, task: asyncio.Task[MaterializeRun]) -> None:
        if not task.cancelled():
            task.exception()
        if self._in_flight is task:
            self._in_flight = None

    @staticmethod
    def _outcome_from_run(run: MaterializeRun) -> OfflineBootstrapOutcome:
        if run.error is None:
            return OfflineBootstrapOutcome(
                status="completed" if run.written_rows else "not_needed",
                attempted=True,
                written_rows=run.written_rows,
            )

        if isinstance(run.error, MaterializeError):
            return OfflineBootstrapOutcome(
                status=(
                    "budget_exceeded" if run.error.code == "BACKFILL_BUDGET_EXCEEDED" else "failed"
                ),
                attempted=True,
                written_rows=run.written_rows,
                error_code=run.error.code,
                error_message=run.error.message,
            )

        return OfflineBootstrapOutcome(
            status="failed",
            attempted=True,
            written_rows=run.written_rows,
            error_code="BACKFILL_FAILED",
            error_message=run.error,
        )
