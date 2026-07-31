# ruff: noqa: RUF001

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from app.services.ai_provider import generate_ai_text
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
from app.services.dow_monitor_minute_result_models import normalize_monitor_symbol
from app.services.dow_monitor_minute_result_repository import (
    DowMonitorMinuteResultRepository,
)
from app.services.dow_monitor_store import DowMonitorStore

logger = logging.getLogger(__name__)

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
        now_fn: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._monitor_store = monitor_store
        self._minute_repository = minute_repository
        self._analysis_repository = analysis_repository
        self._calendar = calendar
        self._snapshot_builder = snapshot_builder
        self._prompt_service = prompt_service
        self._now_fn = now_fn

    async def run_due_jobs(self, now: datetime | None = None) -> int:
        current = now or self._now_fn()
        completed_count = 0
        for symbol in self._monitor_store.list_symbols():
            if not symbol.enabled:
                continue
            windows = self._calendar.completed_window_ends(symbol.market, current)
            for window_end in windows:
                if window_end <= symbol.created_at:
                    continue
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
                    continue
                session_open = self._calendar.session_open(
                    symbol.market,
                    trade_date,
                )
                if session_open is None:
                    continue
                rows = self._minute_repository.load_cumulative_rows(
                    [symbol.symbol],
                    session_open,
                    window_end,
                ).get(normalize_monitor_symbol(symbol.symbol), [])
                snapshot = self._snapshot_builder.build(
                    market=symbol.market,
                    symbol=symbol.symbol,
                    session_open=session_open,
                    window_end=window_end,
                    data_cutoff=window_end,
                    rows=rows,
                )
                identity = analysis_id_for(
                    symbol.market,
                    symbol.symbol,
                    trade_date,
                    window_end,
                )
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
                    continue
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
                    continue
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
                completed_count += 1
        return completed_count

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


def build_worker() -> DowMonitorHalfHourAiWorker:
    repository = DowMonitorHalfHourAiRepository()
    repository.ensure_schema()
    return DowMonitorHalfHourAiWorker(
        monitor_store=DowMonitorStore(
            Path(os.getenv("DATA_DIR", "/app/data"))
        ),
        minute_repository=DowMonitorMinuteResultRepository(),
        analysis_repository=repository,
        calendar=HalfHourWindowCalendar(),
        snapshot_builder=HalfHourAiSnapshotBuilder(),
        prompt_service=HalfHourAiPromptService(generate_ai_text),
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
    while True:
        try:
            await worker.run_due_jobs()
        except Exception:
            # Infrastructure failures are isolated from the panel and retried.
            logger.exception("half-hour AI worker cycle failed")
        await asyncio.sleep(poll_seconds)


if __name__ == "__main__":
    asyncio.run(_main())
