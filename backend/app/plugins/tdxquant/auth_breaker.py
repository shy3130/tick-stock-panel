"""TdxQuant 鉴权熔断器 — 独立状态机，解耦凭据/额度冷却逻辑与各数据集取数逻辑。"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable

logger = logging.getLogger(__name__)


class AuthCircuitBreaker:
    """凭据/额度故障熔断器（线程安全）。

    职责单一：
      - 明确鉴权错误（TdxQuantAuthError）→ 立即熔断；
      - 隐式全空返回（SDK 表现为连续空数据）→ 达到阈值后熔断；
      - 冷却期内所有数据集统一快速失败，避免全市场逐只/逐批刷屏；
      - 冷却期满、手动复位或重建 provider 后自动放行。
    """

    def __init__(
        self,
        *,
        blank_threshold: int = 10,
        cooldown_s: float = 15 * 60.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.blank_threshold = blank_threshold
        self.cooldown_s = cooldown_s
        self._clock = clock
        self._lock = threading.Lock()
        self._tripped_at: float | None = None
        self._reason = ""
        self._blank_streak = 0

    @property
    def tripped_at(self) -> float | None:
        with self._lock:
            return self._tripped_at

    @tripped_at.setter
    def tripped_at(self, value: float | None) -> None:
        with self._lock:
            self._tripped_at = value

    @property
    def reason(self) -> str:
        with self._lock:
            return self._reason

    @reason.setter
    def reason(self, value: str) -> None:
        with self._lock:
            self._reason = value

    def is_open(self) -> bool:
        with self._lock:
            if self._tripped_at is None:
                return False
            if self._clock() - self._tripped_at < self.cooldown_s:
                return True
            self._tripped_at = None
            self._blank_streak = 0
            return False

    def trip(self, reason: str, *, source: str = "数据", streak: int = 1) -> None:
        with self._lock:
            already_open = (
                self._tripped_at is not None
                and self._clock() - self._tripped_at < self.cooldown_s
            )
            self._tripped_at = self._clock()
            self._reason = reason
            self._blank_streak = 0
        if not already_open:
            logger.error(
                "TdxQuant %s请求连续 %d 次鉴权/凭据异常，已熔断 %d 分钟: "
                "暂停拉取避免全市场刷屏。请到 设置→数据源 核对并重新保存 "
                "TdxQuant Key (保存后将热重载原生会话并自动恢复)。首个错误: %s",
                source,
                streak,
                int(self.cooldown_s // 60),
                reason or "未知",
            )

    def record_success(self) -> None:
        with self._lock:
            self._blank_streak = 0

    def record_blank(self, error_text: str, *, source: str = "数据") -> bool:
        with self._lock:
            self._blank_streak += 1
            streak = self._blank_streak
        if streak >= self.blank_threshold:
            self.trip(error_text, source=source, streak=streak)
            return True
        return False

    def reset(self) -> None:
        with self._lock:
            self._tripped_at = None
            self._reason = ""
            self._blank_streak = 0

    @staticmethod
    def is_blank_error(exc: Exception) -> bool:
        return "返回空数据" in str(exc)
