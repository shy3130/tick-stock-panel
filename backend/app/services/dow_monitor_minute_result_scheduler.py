from __future__ import annotations

import asyncio
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from time import monotonic
from typing import Literal
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict

from app.services.dow_monitor_data import market_session_policy
from app.services.dow_monitor_minute_result_append import MinuteResultAppendQueue
from app.services.dow_monitor_minute_result_models import MinuteResultContext
from app.services.dow_monitor_models import MonitoredSymbol


class MinuteResultPipelineStatus(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    mode: Literal[
        "IDLE",
        "REALTIME_APPEND",
        "POST_CLOSE_BACKFILL",
        "NIGHT_AUDIT",
    ] = "IDLE"
    queue_depth: int = 0
    queue_capacity: int = 0
    append_failures: int = 0
    market: str | None = None
    last_started_at: datetime | None = None
    last_completed_at: datetime | None = None
    last_success_at: datetime | None = None
    last_duration_seconds: float | None = None
    scanned_keys: int = 0
    written_rows: int = 0
    remaining_keys: int = 0
    deferred_reason: str | None = None
    last_error: str | None = None


class BackfillDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: Literal["IDLE", "POST_CLOSE_BACKFILL", "NIGHT_AUDIT"] = "IDLE"
    market: str | None = None
    market_day: date | None = None
    deferred_reason: str | None = None


@dataclass
class BackfillScheduleState:
    post_close_days: dict[str, date] = field(default_factory=dict)
    night_audited_markets: dict[date, set[str]] = field(default_factory=dict)


def _market_is_open(item: MonitoredSymbol, now: datetime) -> bool:
    policy = market_session_policy(item.symbol)
    local_now = now.astimezone(ZoneInfo(policy.timezone))
    return (
        local_now.weekday() < 5
        and any(start <= local_now.time() < end for start, end in policy.sessions)
    )


def _latest_completed_market_day(item: MonitoredSymbol, now: datetime) -> date:
    policy = market_session_policy(item.symbol)
    zone = ZoneInfo(policy.timezone)
    local_now = now.astimezone(zone)
    final_close = max(end for _start, end in policy.sessions)
    completed_after = datetime.combine(
        local_now.date(),
        final_close,
        tzinfo=zone,
    ) + timedelta(minutes=20)
    market_day = local_now.date()
    if local_now < completed_after:
        market_day -= timedelta(days=1)
    while market_day.weekday() >= 5:
        market_day -= timedelta(days=1)
    return market_day


def decide_backfill_job(
    symbols: Sequence[MonitoredSymbol],
    now: datetime,
    state: BackfillScheduleState,
) -> BackfillDecision:
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    enabled = [item for item in symbols if item.enabled]
    if not enabled:
        return BackfillDecision(deferred_reason="NOT_DUE")
    if any(_market_is_open(item, now) for item in enabled):
        return BackfillDecision(deferred_reason="MARKET_OPEN")

    by_market: dict[str, list[MonitoredSymbol]] = {}
    for item in enabled:
        by_market.setdefault(item.market, []).append(item)
    for market in sorted(by_market):
        item = by_market[market][0]
        policy = market_session_policy(item.symbol)
        zone = ZoneInfo(policy.timezone)
        local_now = now.astimezone(zone)
        if local_now.weekday() >= 5:
            continue
        final_close = max(end for _start, end in policy.sessions)
        due_at = datetime.combine(
            local_now.date(), final_close, tzinfo=zone
        ) + timedelta(minutes=20)
        if (
            local_now >= due_at
            and state.post_close_days.get(market) != local_now.date()
        ):
            return BackfillDecision(
                mode="POST_CLOSE_BACKFILL",
                market=market,
                market_day=local_now.date(),
            )

    beijing_now = now.astimezone(ZoneInfo("Asia/Shanghai"))
    audit_day = beijing_now.date()
    audited = state.night_audited_markets.get(audit_day, set())
    if beijing_now.time() >= time(6, 30):
        for market in sorted(by_market):
            if market in audited:
                continue
            item = by_market[market][0]
            return BackfillDecision(
                mode="NIGHT_AUDIT",
                market=market,
                market_day=_latest_completed_market_day(item, now),
            )
    return BackfillDecision(deferred_reason="NOT_DUE")


class MinuteResultBackfillScheduler:
    """Own minute-result background work without blocking monitor cycles."""

    def __init__(
        self,
        materializer,
        *,
        now_fn: Callable[[], datetime] = lambda: datetime.now(UTC),
        append_queue: MinuteResultAppendQueue | None = None,
    ) -> None:
        self._materializer = materializer
        materializer_status = materializer.status() if materializer is not None else None
        self._status = MinuteResultPipelineStatus(
            enabled=bool(getattr(materializer_status, "enabled", False)),
            last_error=getattr(materializer_status, "last_error", None),
        )
        self._now_fn = now_fn
        self._stop = asyncio.Event()
        self._wake = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._latest_request: tuple[tuple[MonitoredSymbol, ...], datetime] | None = None
        self._schedule_state = BackfillScheduleState()
        repository = getattr(materializer, "repository", None)
        self._append_queue = (
            append_queue
            if append_queue is not None
            else MinuteResultAppendQueue(repository)
            if repository is not None
            else None
        )

    async def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._stop = asyncio.Event()
        self._wake = asyncio.Event()
        if self._append_queue is not None:
            await self._append_queue.start()
        self._task = asyncio.create_task(
            self._wait_until_stopped(),
            name="dow-monitor-minute-results",
        )

    async def stop(self, timeout_seconds: float = 5.0) -> None:
        task = self._task
        if task is None:
            return
        self._stop.set()
        self._wake.set()
        try:
            await asyncio.wait_for(task, timeout=timeout_seconds)
        except TimeoutError:
            task.cancel()
        finally:
            self._task = None
            if self._append_queue is not None:
                await self._append_queue.stop(timeout_seconds=timeout_seconds)

    def request(self, symbols: Sequence[MonitoredSymbol], now: datetime) -> bool:
        if not self._status.enabled:
            return False
        self._latest_request = (tuple(symbols), now)
        self._wake.set()
        return True

    def submit_live(self, context: MinuteResultContext) -> bool:
        return self._append_queue.submit(context) if self._append_queue is not None else False

    def status(self) -> MinuteResultPipelineStatus:
        status = self._status.model_copy(deep=True)
        if self._append_queue is None:
            return status
        append = self._append_queue.status()
        status.queue_depth = append.queue_depth
        status.queue_capacity = append.queue_capacity
        status.append_failures = append.append_failures
        if append.last_started_at is not None:
            status.last_started_at = append.last_started_at
            status.last_completed_at = append.last_completed_at
            status.last_success_at = append.last_success_at
            status.last_duration_seconds = append.last_duration_seconds
            status.written_rows = append.last_written_rows
            status.last_error = append.last_error
        if append.queue_depth:
            status.mode = "REALTIME_APPEND"
        return status

    async def _wait_until_stopped(self) -> None:
        while not self._stop.is_set():
            await self._wake.wait()
            self._wake.clear()
            if self._stop.is_set():
                break
            request = self._latest_request
            if request is not None:
                await self._execute_due_job(*request)

    async def _execute_due_job(
        self,
        symbols: tuple[MonitoredSymbol, ...],
        now: datetime,
    ) -> None:
        decision = decide_backfill_job(symbols, now, self._schedule_state)
        self._status.deferred_reason = decision.deferred_reason
        if decision.mode == "IDLE" or decision.market is None:
            self._status.mode = "IDLE"
            self._status.market = None
            return
        selected = tuple(
            item for item in symbols if item.enabled and item.market == decision.market
        )
        target_market_day = decision.market_day
        if not selected or target_market_day is None:
            return
        started = monotonic()
        self._status.mode = decision.mode
        self._status.market = decision.market
        self._status.last_started_at = now
        self._status.last_error = None
        self._status.deferred_reason = None
        try:
            run = await asyncio.to_thread(
                self._materializer.materialize,
                selected,
                now,
                max_rows=2_000,
                deadline=monotonic() + 60.0,
                market_day=target_market_day,
            )
        except Exception as exc:
            self._status.last_error = str(exc).replace("\n", " ")[:500]
            return
        finally:
            self._status.last_completed_at = self._now_fn()
            self._status.last_duration_seconds = round(monotonic() - started, 6)
        self._status.scanned_keys = int(getattr(run, "scanned_keys", 0))
        self._status.written_rows = int(getattr(run, "written_rows", 0))
        self._status.remaining_keys = int(getattr(run, "remaining_keys", 0))
        self._status.last_error = getattr(run, "error", None)
        if self._status.last_error is not None:
            return
        if self._status.remaining_keys:
            self._status.deferred_reason = "RESOURCE_BUDGET"
            return
        self._status.last_success_at = self._status.last_completed_at
        if decision.mode == "POST_CLOSE_BACKFILL":
            self._schedule_state.post_close_days[decision.market] = target_market_day
        else:
            audit_day = now.astimezone(ZoneInfo("Asia/Shanghai")).date()
            self._schedule_state.night_audited_markets.setdefault(
                audit_day, set()
            ).add(decision.market)
