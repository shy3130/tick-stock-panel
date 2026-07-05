from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

_MAX_BUFFER = 500


class _SessionChannel:
    def __init__(self) -> None:
        self.buffer: list[tuple[int, dict[str, Any]]] = []
        self.subscribers: set[asyncio.Queue[tuple[int, dict[str, Any]] | None]] = set()
        self.closed = False
        self.seq = 0

    def next_seq(self) -> int:
        self.seq += 1
        return self.seq


class AgentBus:
    """In-process per-session event fan-out with bounded replay."""

    def __init__(self) -> None:
        self._channels: dict[str, _SessionChannel] = {}

    def begin(self, session_id: str) -> None:
        prior = self._channels.get(session_id)
        if prior is not None:
            for queue in list(prior.subscribers):
                queue.put_nowait(None)
            prior.subscribers.clear()
        self._channels[session_id] = _SessionChannel()

    def publish(self, session_id: str, event: dict[str, Any]) -> None:
        channel = self._channels.setdefault(session_id, _SessionChannel())
        seq = channel.next_seq()
        channel.buffer.append((seq, event))
        if len(channel.buffer) > _MAX_BUFFER:
            channel.buffer = channel.buffer[-_MAX_BUFFER:]
        for queue in list(channel.subscribers):
            queue.put_nowait((seq, event))

    def close(self, session_id: str) -> None:
        channel = self._channels.get(session_id)
        if channel is None:
            return
        channel.closed = True
        for queue in list(channel.subscribers):
            queue.put_nowait(None)
        channel.subscribers.clear()

    async def subscribe(self, session_id: str) -> AsyncIterator[dict[str, Any]]:
        channel = self._channels.get(session_id)
        if channel is None:
            return
        queue: asyncio.Queue[tuple[int, dict[str, Any]] | None] = asyncio.Queue()
        channel.subscribers.add(queue)
        if channel.closed:
            queue.put_nowait(None)
        try:
            replay = list(channel.buffer)
            watermark = replay[-1][0] if replay else 0
            for _seq, event in replay:
                yield event
            while True:
                item = await queue.get()
                if item is None:
                    return
                seq, event = item
                if seq > watermark:
                    yield event
        finally:
            channel.subscribers.discard(queue)


_BUS = AgentBus()


def get_bus() -> AgentBus:
    return _BUS
