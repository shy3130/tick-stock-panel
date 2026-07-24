from __future__ import annotations

import asyncio
import contextlib
import json
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from redis.asyncio import Redis
from redis.exceptions import RedisError


UI_DATASETS = frozenset({"quote", "depth", "candlestick"})
ENVELOPE_FIELDS = (
    "type",
    "version",
    "streamId",
    "sequence",
    "symbol",
    "market",
    "eventAt",
    "publishedAt",
)
QUOTE_FIELDS = (
    "lastDone",
    "prevClose",
    "open",
    "high",
    "low",
    "volume",
    "turnover",
    "tradeStatus",
    "timestamp",
)
CANDLE_FIELDS = (
    "period",
    "timestamp",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "turnover",
)
DEPTH_LEVEL_FIELDS = ("position", "price", "volume", "orderCount")


class LatestOutboundBuffer:
    def __init__(self, capacity: int = 500) -> None:
        if capacity <= 0:
            raise ValueError("capacity must be positive")
        self.capacity = capacity
        self._pending: OrderedDict[str, dict[str, object]] = OrderedDict()
        self._condition = asyncio.Condition()
        self.closed = False
        self.superseded = 0

    @property
    def pending_symbols(self) -> set[str]:
        return set(self._pending)

    async def put(self, symbol: str, message: dict[str, object]) -> None:
        async with self._condition:
            if self.closed:
                return
            if symbol in self._pending:
                self.superseded += 1
                self._pending.pop(symbol)
            elif len(self._pending) >= self.capacity:
                self._pending.popitem(last=False)
                self.superseded += 1
            self._pending[symbol] = message
            self._condition.notify()

    async def get(self) -> dict[str, object]:
        async with self._condition:
            while not self._pending and not self.closed:
                await self._condition.wait()
            if not self._pending:
                raise asyncio.CancelledError
            _symbol, message = self._pending.popitem(last=False)
            return message

    async def close(self) -> None:
        async with self._condition:
            self.closed = True
            self._pending.clear()
            self._condition.notify_all()


@dataclass(eq=False)
class RealtimeConnection:
    websocket: Any | None = None
    depth_levels: int = 1
    buffer: LatestOutboundBuffer = field(default_factory=LatestOutboundBuffer)
    symbols: set[str] = field(default_factory=set)
    datasets: set[str] = field(default_factory=lambda: set(UI_DATASETS))


def _sanitize_depth_levels(value: object, limit: int) -> list[dict[str, object]]:
    if not isinstance(value, list):
        return []
    return [
        {field: item[field] for field in DEPTH_LEVEL_FIELDS if field in item}
        for item in value[:limit]
        if isinstance(item, dict)
    ]


def sanitize_message(
    message: object,
    datasets: set[str] | None = None,
    depth_levels: int = 10,
) -> dict[str, object] | None:
    if not isinstance(message, dict):
        return None
    if message.get("version") != "v1":
        return None
    if message.get("type") not in {"update", "snapshot"}:
        return None
    if not all(field in message for field in ENVELOPE_FIELDS):
        return None
    if not isinstance(message.get("sequence"), int) or int(message["sequence"]) <= 0:
        return None
    raw_datasets = message.get("datasets")
    if not isinstance(raw_datasets, dict):
        return None
    selected = UI_DATASETS if datasets is None else UI_DATASETS & datasets
    clean_datasets: dict[str, object] = {}
    for name in selected:
        value = raw_datasets.get(name)
        if not isinstance(value, dict):
            continue
        if name == "quote":
            clean_datasets[name] = {
                field: value[field] for field in QUOTE_FIELDS if field in value
            }
        elif name == "candlestick":
            clean_datasets[name] = {
                field: value[field] for field in CANDLE_FIELDS if field in value
            }
        else:
            clean_datasets[name] = {
                "bids": _sanitize_depth_levels(value.get("bids"), depth_levels),
                "asks": _sanitize_depth_levels(value.get("asks"), depth_levels),
                **({"timestamp": value["timestamp"]} if "timestamp" in value else {}),
            }
    if not clean_datasets:
        return None
    return {
        field: message[field]
        for field in ENVELOPE_FIELDS
    } | {"datasets": clean_datasets}


class RealtimeHub:
    def __init__(
        self,
        redis_url: str,
        channel: str = "lb:ui:v1:updates",
        heartbeat_seconds: float = 15.0,
        allowed_origins: set[str] | None = None,
        redis_factory: Callable[[str], Any] | None = None,
    ) -> None:
        self.redis_url = redis_url
        self.channel = channel
        self.heartbeat_seconds = heartbeat_seconds
        self.allowed_origins = allowed_origins or set()
        factory = redis_factory or (
            lambda url: Redis.from_url(url, decode_responses=True)
        )
        self._redis = factory(redis_url)
        self._pubsub: Any | None = None
        self._connections: set[RealtimeConnection] = set()
        self.subscriptions: dict[str, set[RealtimeConnection]] = {}
        self.state = "disconnected"
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        self._redis_failures = 0
        self._sent_updates = 0
        self._reconnect_delay = 0.5

    async def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._run(), name="realtime-redis-pubsub")

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
        for connection in list(self._connections):
            await self.unregister(connection)
        if self._pubsub is not None:
            with contextlib.suppress(Exception):
                await self._pubsub.aclose()
        with contextlib.suppress(Exception):
            await self._redis.aclose()
        self.state = "disconnected"

    async def register(
        self,
        connection: RealtimeConnection | Any,
    ) -> RealtimeConnection:
        if not isinstance(connection, RealtimeConnection):
            connection = RealtimeConnection(websocket=connection)
        self._connections.add(connection)
        return connection

    async def unregister(self, connection: RealtimeConnection) -> None:
        self._connections.discard(connection)
        for symbol in list(connection.symbols):
            subscribers = self.subscriptions.get(symbol)
            if subscribers is None:
                continue
            subscribers.discard(connection)
            if not subscribers:
                self.subscriptions.pop(symbol, None)
        connection.symbols.clear()
        await connection.buffer.close()

    async def subscribe(
        self,
        connection: RealtimeConnection,
        symbols: set[str],
        datasets: set[str],
        depth_levels: int = 1,
    ) -> None:
        connection.datasets = set(datasets)
        connection.depth_levels = depth_levels
        for symbol in symbols - connection.symbols:
            self.subscriptions.setdefault(symbol, set()).add(connection)
        connection.symbols.update(symbols)

    async def unsubscribe(
        self,
        connection: RealtimeConnection,
        symbols: set[str],
    ) -> None:
        for symbol in symbols & connection.symbols:
            subscribers = self.subscriptions.get(symbol)
            if subscribers is not None:
                subscribers.discard(connection)
                if not subscribers:
                    self.subscriptions.pop(symbol, None)
        connection.symbols.difference_update(symbols)

    async def snapshot(
        self,
        symbols: set[str],
        datasets: set[str] | None = None,
        depth_levels: int = 1,
    ) -> list[dict[str, object]]:
        ordered = sorted(symbols)
        try:
            values = await self._redis.mget(
                [f"lb:ui:v1:latest:{symbol}" for symbol in ordered]
            )
        except RedisError:
            await self._degrade()
            return []
        snapshots: list[dict[str, object]] = []
        for value in values:
            if not value:
                continue
            try:
                payload = json.loads(value)
            except (TypeError, json.JSONDecodeError):
                continue
            clean = sanitize_message(payload, datasets, depth_levels)
            if clean is not None:
                clean["type"] = "snapshot"
                snapshots.append(clean)
        return snapshots

    async def dispatch(self, message: object) -> None:
        clean = sanitize_message(message)
        if clean is None:
            return
        symbol = str(clean["symbol"])
        for connection in list(self.subscriptions.get(symbol, ())):
            projected = sanitize_message(
                clean,
                connection.datasets,
                connection.depth_levels,
            )
            if projected is None:
                continue
            await connection.buffer.put(symbol, projected)
            self._sent_updates += 1

    async def run_pubsub_once(self) -> None:
        try:
            if self._pubsub is None:
                self._pubsub = self._redis.pubsub()
                await self._pubsub.subscribe(self.channel)
            item = await self._pubsub.get_message(
                ignore_subscribe_messages=True,
                timeout=0.1,
            )
            self.state = "connected"
            self._reconnect_delay = 0.5
            if item and item.get("type") == "message":
                data = item.get("data")
                if isinstance(data, bytes):
                    data = data.decode("utf-8")
                await self.dispatch(json.loads(data))
        except (RedisError, json.JSONDecodeError, TypeError):
            await self._degrade()

    async def _degrade(self) -> None:
        self.state = "degraded"
        self._redis_failures += 1
        pubsub = self._pubsub
        self._pubsub = None
        if pubsub is not None and pubsub is not self._redis:
            with contextlib.suppress(Exception):
                await pubsub.aclose()
        fallback = {
            "type": "fallback",
            "version": "v1",
            "reason": "realtime_redis_unavailable",
        }
        for connection in list(self._connections):
            await connection.buffer.put("__fallback__", fallback)

    async def _run(self) -> None:
        while not self._stop.is_set():
            await self.run_pubsub_once()
            delay = 0.05
            if self.state == "degraded":
                delay = self._reconnect_delay
                self._reconnect_delay = min(30.0, self._reconnect_delay * 2)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=delay)
            except TimeoutError:
                pass

    def metrics(self) -> dict[str, int | str]:
        return {
            "active_clients": len(self._connections),
            "subscribed_symbols": len(self.subscriptions),
            "sent_updates": self._sent_updates,
            "superseded_messages": sum(
                connection.buffer.superseded for connection in self._connections
            ),
            "redis_failures": self._redis_failures,
            "redis_state": self.state,
        }
