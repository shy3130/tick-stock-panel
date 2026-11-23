"""数据状态的内存快照: 请求只读缓存, 单个 daemon 线程刷新过期统计。"""
from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from typing import Any

logger = logging.getLogger(__name__)


class DataStatusCache:
    def __init__(self, ttls: dict[str, float]) -> None:
        self._ttls = ttls
        self._values: dict[str, Any] = dict.fromkeys(ttls)
        self._expires = dict.fromkeys(ttls, 0.0)
        self._versions = dict.fromkeys(ttls, 0)
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None

    def invalidate(self, name: str | None = None, *, discard_stale: bool = False) -> None:
        with self._lock:
            names = self._ttls if name is None else (name,)
            for key in names:
                if key not in self._ttls:
                    continue
                # 刷新在锁外计算; 失效后的旧计算不得重新把缓存标记为新鲜。
                self._versions[key] += 1
                self._expires[key] = 0.0
                if discard_stale:
                    self._values[key] = None

    def snapshot(
        self,
        loaders: dict[str, Callable[[], Any]],
        *,
        is_busy: Callable[[], bool],
    ) -> dict[str, Any]:
        busy = is_busy()
        worker = None
        with self._lock:
            now = time.monotonic()
            due = {key: self._versions[key] for key in loaders if now >= self._expires[key]}
            result = {key: self._values[key] for key in loaders}
            result["stats_pending"] = busy or bool(due)
            if due and not busy and self._thread is None:
                worker = threading.Thread(
                    target=self._refresh,
                    args=(loaders, due, is_busy),
                    name="data-status-refresh",
                    daemon=True,
                )
                # 在启动前占位, 并发请求不能重复启动或排队刷新。
                self._thread = worker
        if worker is not None:
            try:
                worker.start()
            except RuntimeError:
                with self._lock:
                    self._thread = None
                logger.exception("unable to start data status refresh")
        return result

    def _refresh(
        self,
        loaders: dict[str, Callable[[], Any]],
        versions: dict[str, int],
        is_busy: Callable[[], bool],
    ) -> None:
        fresh: dict[str, Any] = {}
        try:
            for key in versions:
                # 同步/预热可能在请求返回后开始, 不再启动后续重统计。
                if is_busy():
                    break
                try:
                    fresh[key] = loaders[key]()
                except Exception:
                    # 保留旧统计, 后续请求可重试。
                    logger.exception("data status refresh failed: %s", key)
        finally:
            with self._lock:
                now = time.monotonic()
                # 整批发布, 请求不会看到后台刷新中的半成品。
                for key, value in fresh.items():
                    if self._versions[key] == versions[key]:
                        self._values[key] = value
                        # None 也是有效的空表结果, 避免每次轮询都重算。
                        self._expires[key] = now + self._ttls[key]
                self._thread = None
