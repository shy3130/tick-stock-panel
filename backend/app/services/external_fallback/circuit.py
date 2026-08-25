"""Circuit breaker — 连续失败熔断 + 冷却恢复。

设计参数 (受控 fallback 契约 §4.6):
  - 默认 5 次连续失败 → 冷却 10 分钟
  - 冷却期内 source_available() 返回 False (走原降级路径)
  - 成功一次即关闭熔断、清零失败计数
  - clock 可注入, 便于测试

本模块不执行任何网络调用, 也不持有 HTTP 句柄; 纯状态机。
"""
from __future__ import annotations

import logging
import threading
import typing

logger = logging.getLogger(__name__)


class CircuitBreaker:
    """每 source 独立的连续失败熔断状态机 (线程安全)。"""

    def __init__(
        self,
        *,
        failure_threshold: int = 5,
        cooldown_seconds: float = 600.0,
        clock: typing.Callable[[], float] | None = None,
    ) -> None:
        self.failure_threshold = failure_threshold
        self.cooldown_seconds = cooldown_seconds
        self._clock = clock or _default_clock
        self._lock = threading.Lock()
        self._failures: dict[str, int] = {}
        self._cooldown_until: dict[str, float] = {}

    def source_available(self, source: str) -> bool:
        """熔断期内返回 False; 否则 True。"""
        with self._lock:
            until = self._cooldown_until.get(source, 0.0)
            if until and until > self._clock():
                logger.debug(
                    "external_fallback source '%s' cooling down for %.1fs",
                    source, until - self._clock(),
                )
                return False
        return True

    def record_success(self, source: str) -> None:
        """成功: 清零失败计数、关闭熔断。恢复时记一次 info 日志。"""
        was_open = False
        with self._lock:
            was_open = source in self._cooldown_until or self._failures.get(source, 0) > 0
            self._failures[source] = 0
            self._cooldown_until.pop(source, None)
        if was_open:
            logger.info("external_fallback source '%s' recovered, circuit closed", source)

    def record_failure(self, source: str) -> None:
        """失败: +1; 达阈值则开启一次冷却。

        并发中已经进入冷却的 source 不得延长冷却窗口; 否则持续轮询会让
        ``now + cooldown`` 不断后移, 熔断器无法自动恢复。
        """
        with self._lock:
            now = self._clock()
            until = self._cooldown_until.get(source, 0.0)
            if until > now:
                return
            failures = self._failures.get(source, 0) + 1
            self._failures[source] = failures
            if failures >= self.failure_threshold:
                self._cooldown_until[source] = now + self.cooldown_seconds
                opened = True
            else:
                opened = False
        if opened:
            logger.warning(
                "external_fallback source '%s' circuit opened for %.0fs after %d failures",
                source, self.cooldown_seconds, failures,
            )

    def force_open(self, source: str, *, reason: str = "") -> None:
        """立即开启一次冷却 (用于连续口径校准失败等非网络错误场景)。"""
        with self._lock:
            now = self._clock()
            if self._cooldown_until.get(source, 0.0) > now:
                return
            self._failures[source] = self.failure_threshold
            self._cooldown_until[source] = now + self.cooldown_seconds
        logger.warning(
            "external_fallback source '%s' circuit force-opened for %.0fs%s%s",
            source, self.cooldown_seconds, ": " if reason else "", reason,
        )


def _default_clock() -> float:
    import time

    return time.monotonic()
