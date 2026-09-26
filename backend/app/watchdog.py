"""后端自愈看门狗。

2026-09-07 事故形态: polars 并发死锁把线程悬死在 collect 内部 (0 CPU 永久
挂起), 其中持锁者让 _write_lock 永久被占, 所有请求线程排队冻结, 只能人工
重启。并发闸 (app.polars_guard) 与写锁瘦身 (repository 乐观并发) 分别削减
触发概率与扩散半径; 看门狗是最后一层兜底 —— 探测走与事故相同的共享资源
路径 (collect 闸 + 全局写锁), 连续 N 次超时即判定进程已僵死, 主动退出交由
supervisor / Docker restart / dev 脚本拉起, 把恢复时间从"人工发现"缩短到
约一分钟。

误伤防护: 探测本身是毫秒级微型 collect + 1s 写锁试探, 阈值要求连续失败
(默认 2 次 × 15s 超时), 高负载下"慢而未死"不会触发。
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import threading
from collections.abc import Callable

import polars as pl

from app.config import settings
from app.polars_guard import guarded_collect

logger = logging.getLogger(__name__)


def default_probe(write_lock: threading.Lock | None = None) -> None:
    """探测关键共享资源: polars collect 闸 + 仓库全局写锁。

    任一被悬死线程占住即超时 — 正是 2026-09-07 冻结事故中被毒化的两条路径。
    """
    guarded_collect(pl.LazyFrame({"probe": [1]}).sum())
    if write_lock is not None:
        acquired = write_lock.acquire(timeout=1.0)
        if not acquired:
            raise TimeoutError("repository write lock unavailable")
        write_lock.release()


class HealthWatchdog:
    """周期探测; 连续 failure_threshold 次失败后调用 exit_cb(退出码)。"""

    def __init__(
        self,
        probe: Callable[[], None],
        *,
        exit_cb: Callable[[int], None],
        interval_s: float | None = None,
        probe_timeout_s: float | None = None,
        failure_threshold: int | None = None,
    ) -> None:
        self._probe = probe
        self._exit_cb = exit_cb
        self._interval_s = settings.watchdog_interval_s if interval_s is None else interval_s
        self._probe_timeout_s = (
            settings.watchdog_probe_timeout_s if probe_timeout_s is None else probe_timeout_s
        )
        self._failure_threshold = (
            settings.watchdog_failure_threshold if failure_threshold is None else failure_threshold
        )
        self._consecutive_failures = 0
        self._task: asyncio.Task | None = None

    async def _loop(self) -> None:
        while True:
            try:
                await asyncio.wait_for(
                    asyncio.to_thread(self._probe), timeout=self._probe_timeout_s
                )
                self._consecutive_failures = 0
            except asyncio.CancelledError:
                # 取消必须穿透: stop() 靠 task.cancel() 结束本循环, 若被下面的
                # BaseException 分支吞掉, 取消会被误计为一次"探测失败"且循环
                # 继续运行 — stop() 的 await self._task 永久等待 (关闭死锁)。
                raise
            except BaseException as exc:  # 探测任何异常都算失败 (含 to_thread 超时)
                self._consecutive_failures += 1
                logger.error(
                    "watchdog probe failed (%d/%d): %r",
                    self._consecutive_failures,
                    self._failure_threshold,
                    exc,
                )
                if self._consecutive_failures >= self._failure_threshold:
                    logger.critical(
                        "watchdog: backend wedged (probe failed %d consecutive times); "
                        "exiting for supervisor restart",
                        self._consecutive_failures,
                    )
                    self._exit_cb(70)
                    return
            await asyncio.sleep(self._interval_s)

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._loop(), name="health-watchdog")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None


def start_watchdog(app_state, repo) -> HealthWatchdog | None:
    """lifespan 启动钩子; 返回实例挂到 app.state.watchdog 便于关闭。"""
    if not settings.watchdog_enabled:
        return None
    write_lock = getattr(repo, "_write_lock", None)
    watchdog = HealthWatchdog(
        lambda: default_probe(write_lock),
        exit_cb=lambda code: os._exit(code),
    )
    watchdog.start()
    return watchdog
