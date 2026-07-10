"""TickFlow capability rate-limit helpers.

This module centralizes the small pieces of batch/rpm resolution used by
TickFlow-backed services. It intentionally does not manage custom data sources.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import TypeVar

from app.tickflow.capabilities import Cap, CapabilitySet

T = TypeVar("T")

# 进程级共享限速器: 原先每个调用方各自本地 sleep(60/rpm), 并发同步 (kline/index/
# depth/watchlist/custom) 时聚合请求速率会成倍超过单能力 rpm → 429。
# 这里用一张按 rpm 分桶的「下一个可用时刻」表 (Lock 守护), 所有调用方按同一时间轴
# 排队, 使跨调用方的聚合发包间隔 >= 60/rpm。以 rpm 为键 (调用方签名只带 rpm, 不带 cap;
# rpm 是各能力速率的代理); 恰好同 rpm 的不同能力会共享一队, 偏保守但绝不超速。
_slot_lock = threading.Lock()
_next_slot: dict[int, float] = {}


def _reserve_slot(rpm: int, interval: float) -> float:
    """在共享时间轴上为一次请求预约一个发包槽, 返回需等待的秒数 (>=0)。

    interval = 60/rpm。now 早于该 rpm 桶的 next_slot 时排到 next_slot, 否则排到 now;
    随后把该桶 next_slot 后移 interval。持锁仅做时间账目, 不在锁内 sleep。
    """
    key = rpm if rpm and rpm > 0 else -1
    with _slot_lock:
        now = time.monotonic()
        scheduled = max(now, _next_slot.get(key, now))
        _next_slot[key] = scheduled + interval
        return scheduled - now


@dataclass(frozen=True)
class ResolvedLimit:
    batch: int | None
    rpm: int | None


def resolve_limit(
    capset: CapabilitySet,
    cap: Cap,
    *,
    default_batch: int | None = None,
    default_rpm: int | None = None,
    default_rpm_when_unset: bool = True,
) -> ResolvedLimit:
    """Return a capability's batch/rpm with caller-provided fallbacks."""
    lim = capset.limits(cap)
    if lim is None:
        return ResolvedLimit(batch=default_batch, rpm=default_rpm)
    return ResolvedLimit(
        batch=lim.batch if lim.batch else default_batch,
        rpm=lim.rpm if lim.rpm else (default_rpm if default_rpm_when_unset else None),
    )


def batch_interval(rpm: int | None, *, default: float = 0.0) -> float:
    """Return the existing uniform batch interval formula: 60 / rpm."""
    return 60.0 / rpm if rpm and rpm > 0 else default


def chunked(items: list[T], batch_size: int | None) -> list[list[T]]:
    """Split items by batch size, preserving the existing None-as-one-batch behavior."""
    if batch_size is None:
        return [items]
    return [items[i:i + batch_size] for i in range(0, len(items), batch_size)]


def sleep_between_batches(index: int, rpm: int | None, *, default_interval: float = 0.0) -> None:
    """Sleep before every batch after the first, using the existing interval formula.

    内部改用进程级共享限速器 (_reserve_slot): 保持「首批不 sleep, 后续每批间隔 60/rpm」
    的单调用方观感, 同时让并发调用方按同一时间轴排队, 聚合速率不再超过单能力 rpm。
    """
    interval = batch_interval(rpm, default=default_interval)
    if interval <= 0:
        return
    if index <= 0:
        # 首批不 sleep, 但登记一个占位槽, 让后续/并发调用方在同一时间轴上排队
        _reserve_slot(rpm or -1, interval)
        return
    wait = _reserve_slot(rpm or -1, interval)
    if wait > 0:
        time.sleep(wait)


def min_batch(preferred: int, limit: ResolvedLimit) -> int:
    """Clamp a user-preferred batch size by a resolved capability batch limit."""
    return min(preferred, limit.batch) if limit.batch else preferred
