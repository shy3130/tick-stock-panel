"""进程内事件总线 — SSE /api/events 的发布/订阅底座。

设计约束:
  - 只做进程内广播 (自托管单进程 uvicorn 场景); 不引入 Redis/MQ 依赖。
  - 订阅队列有界 (默认 200): 慢消费者丢最旧事件而不是拖垮发布方;
    SSE 断线由客户端换新票据重连补拉 (告警等持久数据另有 REST 出口)。
  - 事件带 required_scope: 流端点按票据 scope 过滤, 票据不放大权限。
"""
from __future__ import annotations

import contextlib
import json
import queue
import threading
import time
from typing import Any

_QUEUE_MAX = 200


def _serialize(event_type: str, payload: dict[str, Any], scope: str) -> dict[str, Any]:
    return {
        "type": event_type,
        "scope": scope,
        "data": payload,
        "ts": time.time(),
    }


class EventBus:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._subscribers: list[queue.Queue[dict[str, Any]]] = []

    def subscribe(self) -> queue.Queue[dict[str, Any]]:
        q: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=_QUEUE_MAX)
        with self._lock:
            self._subscribers.append(q)
        return q

    def unsubscribe(self, q: queue.Queue[dict[str, Any]]) -> None:
        with self._lock, contextlib.suppress(ValueError):
            self._subscribers.remove(q)

    def publish(self, event_type: str, payload: dict[str, Any], scope: str) -> None:
        """广播事件; scope = 订阅方票据需持有的最小 scope。"""
        event = _serialize(event_type, payload, scope)
        with self._lock:
            subscribers = list(self._subscribers)
        for q in subscribers:
            try:
                q.put_nowait(event)
            except queue.Full:
                # 慢消费者: 丢最旧, 保最新 (监控类事件新比全重要)
                try:
                    q.get_nowait()
                    q.put_nowait(event)
                except (queue.Empty, queue.Full):
                    pass


def sse_format(event: dict[str, Any]) -> str:
    """事件 → SSE 帧 (id 用单调时间戳, EventSource lastEventId 可用于去重)。"""
    return f"id: {int(event['ts'] * 1000)}\nevent: {event['type']}\ndata: {json.dumps(event['data'], ensure_ascii=False)}\n\n"


# 进程级单例 — 域模块与 API 层共用同一总线
bus = EventBus()
