# ruff: noqa: RUF001

from __future__ import annotations

import asyncio
import logging
import math
import os
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path

from app.services.ai_provider import generate_ai_text
from app.services.dow_monitor_client import LongbridgeDowClient
from app.services.dow_monitor_half_hour_ai_calendar import HalfHourWindowCalendar
from app.services.dow_monitor_half_hour_ai_models import (
    HalfHourAiAnalysis,
    analysis_id_for,
)
from app.services.dow_monitor_half_hour_ai_prompt import HalfHourAiPromptService
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
from app.services.dow_monitor_minute_result_models import normalize_monitor_symbol
from app.services.dow_monitor_minute_result_repository import (
    DowMonitorMinuteResultRepository,
)
from app.services.dow_monitor_minute_result_source import DowMonitorMinuteResultSource
from app.services.dow_monitor_offline_bootstrap import DowMonitorOfflineBootstrap
from app.services.dow_monitor_store import DowMonitorStore

logger = logging.getLogger(__name__)


def select_due_windows(
    *,
    completed_windows: Sequence[datetime],
    created_at: datetime,
    terminal_window_ends: set[datetime],
    startup_eligible: bool = True,
) -> list[datetime]:
    latest_before_created_at = max(
        (window for window in completed_windows if window < created_at),
        default=None,
    )
    startup = (
        latest_before_created_at
        if startup_eligible
        and latest_before_created_at not in terminal_window_ends
        else None
    )
    normal = sorted(
        window
        for window in completed_windows
        if window >= created_at and window not in terminal_window_ends
    )
    return ([startup] if startup is not None else []) + normal


class DowMonitorHalfHourAiWorker:
    def __init__(
        self,
        *,
        monitor_store,
        minute_repository,
        analysis_repository,
        calendar,
        snapshot_builder,
        prompt_service,
        offline_bootstrap,
        close_fn: Callable[[], None] | None = None,
        now_fn: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._monitor_store = monitor_store
        self._minute_repository = minute_repository
        self._analysis_repository = analysis_repository
        self._calendar = calendar
        self._snapshot_builder = snapshot_builder
        self._prompt_service = prompt_service
        self._offline_bootstrap = offline_bootstrap
        self._close_fn = close_fn
        self._now_fn = now_fn

    async def run_due_jobs(self, now: datetime | None = None) -> int:
        current = now or self._now_fn()
        completed_count = 0
        for symbol in self._monitor_store.list_symbols():
            if not symbol.enabled:
                continue
            windows = select_due_windows(
                completed_windows=self._calendar.completed_window_ends(
                    symbol.market,
                    current,
                ),
                created_at=symbol.created_at,
                terminal_window_ends=set(),
                startup_eligible=self._calendar.is_regular_session_time(
                    symbol.market,
                    symbol.created_at,
                ),
            )
            for window_end in windows:
                try:
                    completed_count += await self._run_checkpoint(
                        symbol,
                        window_end,
                    )
                except Exception as exc:
                    logger.warning(
                        "half-hour checkpoint failed for %s at %s: %s",
                        symbol.symbol,
                        window_end.isoformat(),
                        exc,
                    )
        return completed_count

    async def _run_checkpoint(self, symbol, window_end: datetime) -> int:
        trade_date = self._calendar.trade_date_for_checkpoint(
            symbol.market,
            window_end,
        )
        if self._analysis_repository.exists_completed(
            symbol.market,
            symbol.symbol,
            trade_date,
            window_end,
        ):
            return 0
        session_open = self._calendar.session_open(
            symbol.market,
            trade_date,
        )
        if session_open is None:
            return 0
        snapshot = self._load_snapshot(symbol, session_open, window_end)
        identity = analysis_id_for(
            symbol.market,
            symbol.symbol,
            trade_date,
            window_end,
        )
        if not snapshot.sufficient:
            outcome = await self._offline_bootstrap.ensure_checkpoint(
                symbol=symbol,
                session_open=session_open,
                window_end=window_end,
            )
            if outcome.status == "busy":
                return 0
            if outcome.status in {
                "budget_exceeded",
                "timed_out",
                "failed",
            }:
                self._analysis_repository.save(
                    self._record(
                        identity,
                        symbol.market,
                        symbol.symbol,
                        trade_date,
                        window_end,
                        snapshot,
                        status="insufficient_data",
                        error_code=outcome.error_code or "BACKFILL_FAILED",
                        error_message=outcome.error_message,
                    )
                )
                return 0
            snapshot = self._load_snapshot(
                symbol,
                session_open,
                window_end,
            )
            if not snapshot.sufficient:
                self._analysis_repository.save(
                    self._record(
                        identity,
                        symbol.market,
                        symbol.symbol,
                        trade_date,
                        window_end,
                        snapshot,
                        status="insufficient_data",
                        error_code="INSUFFICIENT_DATA",
                        error_message="；".join(snapshot.data_quality),
                    )
                )
                return 0
        self._analysis_repository.save(
            self._record(
                identity,
                symbol.market,
                symbol.symbol,
                trade_date,
                window_end,
                snapshot,
                status="running",
            )
        )
        try:
            parsed = await self._prompt_service.analyze(snapshot)
        except Exception as exc:
            logger.warning(
                "half-hour AI failed for %s at %s: %s",
                symbol.symbol,
                window_end.isoformat(),
                exc,
            )
            self._analysis_repository.save(
                self._record(
                    identity,
                    symbol.market,
                    symbol.symbol,
                    trade_date,
                    window_end,
                    snapshot,
                    status="failed",
                    error_code=type(exc).__name__,
                    error_message=str(exc)[:500],
                )
            )
            return 0
        self._analysis_repository.save(
            self._record(
                identity,
                symbol.market,
                symbol.symbol,
                trade_date,
                window_end,
                snapshot,
                status="completed",
                title=parsed.title,
                summary=parsed.summary,
                conclusion=parsed.conclusion,
                evidence=parsed.evidence,
                risks=parsed.risks,
                scenarios=parsed.scenarios,
                data_quality=parsed.data_quality,
            )
        )
        return 1

    def _load_snapshot(self, symbol, session_open, window_end):
        rows = self._minute_repository.load_cumulative_rows(
            [symbol.symbol],
            session_open,
            window_end,
        ).get(normalize_monitor_symbol(symbol.symbol), [])
        return self._snapshot_builder.build(
            market=symbol.market,
            symbol=symbol.symbol,
            session_open=session_open,
            window_end=window_end,
            data_cutoff=window_end,
            rows=rows,
        )

    def _record(
        self,
        analysis_id,
        market,
        symbol,
        trade_date,
        window_end,
        snapshot,
        *,
        status,
        **values,
    ) -> HalfHourAiAnalysis:
        return HalfHourAiAnalysis(
            analysis_id=analysis_id,
            market=market,
            symbol=symbol,
            trade_date=trade_date,
            window_end=window_end,
            data_cutoff=window_end,
            status=status,
            input_snapshot=snapshot.model_dump(mode="json"),
            updated_at=self._now_fn(),
            **values,
        )

    def close(self) -> None:
        close_fn, self._close_fn = self._close_fn, None
        if close_fn is not None:
            close_fn()


def build_worker() -> DowMonitorHalfHourAiWorker:
    timeout_seconds = float(
        os.getenv(
            "DOW_MONITOR_AI_BOOTSTRAP_TIMEOUT_SECONDS",
            "15",
        )
    )
    if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
        raise ValueError(
            "DOW_MONITOR_AI_BOOTSTRAP_TIMEOUT_SECONDS must be finite positive"
        )
    max_rows = int(
        os.getenv("DOW_MONITOR_AI_BOOTSTRAP_MAX_ROWS", "500")
    )
    if max_rows <= 0:
        raise ValueError(
            "DOW_MONITOR_AI_BOOTSTRAP_MAX_ROWS must be positive"
        )
    analysis_repository = DowMonitorHalfHourAiRepository()
    analysis_repository.ensure_schema()
    store = DowMonitorStore(Path(os.getenv("DATA_DIR", "/app/data")))
    minute_repository = DowMonitorMinuteResultRepository()
    dow_client = LongbridgeDowClient(
        os.getenv(
            "LONGBRIDGE_API_URL",
            "http://host.docker.internal:19912",
        )
    )
    materializer = DowMonitorMinuteResultMaterializer(
        source=DowMonitorMinuteResultSource(),
        repository=minute_repository,
        history_builder=DowMonitorMinuteResultHistoryBuilder(
            DowEngineStableStateBuilder(dow_client)
        ),
        notifications_fn=lambda: store.list_notifications(limit=1_000_000),
    )
    offline_bootstrap = DowMonitorOfflineBootstrap(
        materializer,
        timeout_seconds=timeout_seconds,
        max_rows=min(max_rows, 500),
    )
    return DowMonitorHalfHourAiWorker(
        monitor_store=store,
        minute_repository=minute_repository,
        analysis_repository=analysis_repository,
        calendar=HalfHourWindowCalendar(),
        snapshot_builder=HalfHourAiSnapshotBuilder(),
        prompt_service=HalfHourAiPromptService(generate_ai_text),
        offline_bootstrap=offline_bootstrap,
        close_fn=dow_client.close,
    )


async def _main() -> None:
    if os.getenv("DOW_AI_WORKER_ENABLED", "true").lower() not in {
        "1",
        "true",
        "yes",
    }:
        return
    poll_seconds = max(
        5.0,
        float(os.getenv("DOW_AI_WORKER_POLL_SECONDS", "15")),
    )
    worker = build_worker()
    try:
        while True:
            try:
                await worker.run_due_jobs()
            except Exception:
                # Infrastructure failures are isolated from the panel and retried.
                logger.exception("half-hour AI worker cycle failed")
            await asyncio.sleep(poll_seconds)
    finally:
        worker.close()


if __name__ == "__main__":
    asyncio.run(_main())
