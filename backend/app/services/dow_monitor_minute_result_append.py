from __future__ import annotations

import asyncio
from contextlib import suppress
from datetime import UTC, datetime
from time import monotonic

from pydantic import BaseModel, ConfigDict, Field

from app.services.dow_monitor_minute_result_calculator import calculate_minute_result
from app.services.dow_monitor_minute_result_models import (
    MinuteResultContext,
    MinuteResultKey,
)


class MinuteResultAppendStatus(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    queue_depth: int = Field(default=0, ge=0)
    queue_capacity: int = Field(default=1_024, gt=0)
    append_failures: int = Field(default=0, ge=0)
    last_started_at: datetime | None = None
    last_completed_at: datetime | None = None
    last_success_at: datetime | None = None
    last_duration_seconds: float | None = None
    last_written_rows: int = Field(default=0, ge=0)
    last_error: str | None = None


class MinuteResultAppendQueue:
    """Bounded, fail-open writer for already-built causal minute contexts."""

    def __init__(
        self,
        repository,
        *,
        capacity: int = 1_024,
        batch_size: int = 200,
        flush_seconds: float = 2.0,
    ) -> None:
        if capacity <= 0 or batch_size <= 0 or flush_seconds <= 0:
            raise ValueError("queue limits must be positive")
        self._repository = repository
        self._capacity = capacity
        self._batch_size = batch_size
        self._flush_seconds = flush_seconds
        self._queue: asyncio.Queue[MinuteResultContext | None] = asyncio.Queue(
            maxsize=capacity
        )
        self._pending_keys: set[MinuteResultKey] = set()
        self._status = MinuteResultAppendStatus(queue_capacity=capacity)
        self._task: asyncio.Task[None] | None = None
        self._stopping = False

    async def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._stopping = False
        self._task = asyncio.create_task(
            self._run(),
            name="dow-monitor-minute-result-append",
        )

    def submit(self, context: MinuteResultContext) -> bool:
        if self._stopping:
            return False
        key = self._key(context)
        if key in self._pending_keys:
            return False
        try:
            self._queue.put_nowait(context.model_copy(deep=True))
        except asyncio.QueueFull:
            self._status.append_failures += 1
            self._status.last_error = "minute result append queue full"
            return False
        self._pending_keys.add(key)
        self._status.queue_depth = self._queue.qsize()
        return True

    async def stop(self, timeout_seconds: float = 5.0) -> None:
        task = self._task
        if task is None:
            return
        self._stopping = True
        with suppress(asyncio.QueueFull):
            self._queue.put_nowait(None)
        try:
            await asyncio.wait_for(task, timeout=timeout_seconds)
        except TimeoutError:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
        finally:
            self._task = None
            self._status.queue_depth = self._queue.qsize()

    def status(self) -> MinuteResultAppendStatus:
        status = self._status.model_copy(deep=True)
        status.queue_depth = min(self._queue.qsize(), self._capacity)
        return status

    async def _run(self) -> None:
        while True:
            first = await self._queue.get()
            if first is None:
                if self._queue.empty():
                    break
                continue
            batch = [first]
            deadline = asyncio.get_running_loop().time() + self._flush_seconds
            stopping = False
            while len(batch) < self._batch_size:
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    break
                try:
                    item = await asyncio.wait_for(
                        self._queue.get(),
                        timeout=remaining,
                    )
                except TimeoutError:
                    break
                if item is None:
                    stopping = True
                    break
                batch.append(item)
            await self._flush(batch)
            if stopping and self._queue.empty():
                break

    async def _flush(self, contexts: list[MinuteResultContext]) -> None:
        started_at = datetime.now(UTC)
        started = monotonic()
        self._status.last_started_at = started_at
        self._status.last_written_rows = 0
        keys = [self._key(context) for context in contexts]
        try:
            rows = [calculate_minute_result(context) for context in contexts]
            written = await asyncio.to_thread(self._repository.insert_results, rows)
        except Exception as exc:
            self._status.append_failures += len(contexts)
            self._status.last_error = self._safe_error(exc)
        else:
            completed_at = datetime.now(UTC)
            self._status.last_written_rows = int(written)
            self._status.last_success_at = completed_at
            self._status.last_error = None
        finally:
            self._status.last_completed_at = datetime.now(UTC)
            self._status.last_duration_seconds = round(monotonic() - started, 6)
            for key in keys:
                self._pending_keys.discard(key)
            self._status.queue_depth = min(self._queue.qsize(), self._capacity)

    @staticmethod
    def _key(context: MinuteResultContext) -> MinuteResultKey:
        return MinuteResultKey(
            market=context.market,
            symbol=context.symbol,
            decision_minute=context.decision_minute,
        )

    @staticmethod
    def _safe_error(exc: Exception) -> str:
        return str(exc).replace("\n", " ")[:500]
