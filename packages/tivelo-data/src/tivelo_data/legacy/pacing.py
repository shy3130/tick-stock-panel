"""Per-source request pacing with cooperative cancellation."""

import threading
import time
from collections.abc import Callable


class RequestPacer:
    """同一源实例跨调用预约请求;锁内只记账,等待和回调均在锁外。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._next: dict[str, float] = {}

    def wait(
        self, scope: str, rpm: int | None, checkpoint: Callable[[], None] | None = None
    ) -> None:
        if not rpm or rpm <= 0:
            return
        with self._lock:
            now = time.monotonic()
            slot = max(now, self._next.get(scope, now))
            self._next[scope] = slot + 60.0 / rpm
        while (remaining := slot - time.monotonic()) > 0:
            if checkpoint:
                checkpoint()
            time.sleep(min(remaining, 1.0))
