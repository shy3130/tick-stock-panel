"""请求调度器(§5.6)。

按 capability 分别维护令牌桶。Phase 0:基础实现;Phase 1 接入批量合并、优先级队列。
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass

from .capabilities import Cap, CapabilitySet


@dataclass
class _Bucket:
    capacity: int        # 每分钟令牌数
    tokens: float
    last_refill: float   # 单位:秒

    def consume(self, n: int = 1) -> float:
        """尝试消费 n 个令牌,返回需要等待的秒数(0 表示无需等待)。"""
        now = time.monotonic()
        elapsed = now - self.last_refill
        # 60s 内补满 capacity,匀速补
        refill = elapsed * (self.capacity / 60.0)
        self.tokens = min(self.capacity, self.tokens + refill)
        self.last_refill = now

        if self.tokens >= n:
            self.tokens -= n
            return 0.0
        deficit = n - self.tokens
        # 还需多少秒才能补齐
        wait = deficit / (self.capacity / 60.0)
        # 不预扣,留给下一次再竞争(避免饿死优先级高的请求)
        return wait


class Scheduler:
    """每个 capability 一个桶。"""

    def __init__(self, capset: CapabilitySet) -> None:
        self._capset = capset
        self._buckets: dict[Cap, _Bucket] = {}
        self._locks: dict[Cap, asyncio.Lock] = {}
        for cap, lim in capset.all().items():
            if lim.rpm:
                self._buckets[cap] = _Bucket(
                    capacity=lim.rpm,
                    tokens=lim.rpm,
                    last_refill=time.monotonic(),
                )
                self._locks[cap] = asyncio.Lock()

    async def acquire(self, cap: Cap, n: int = 1) -> None:
        """阻塞直到拿到 n 个令牌。无桶 = 不限流(由调用方保证)。"""
        bucket = self._buckets.get(cap)
        if bucket is None:
            return
        lock = self._locks[cap]
        # 只把令牌账目放锁内; sleep 放锁外, 否则一个协程在 sleep 期间独占锁,
        # 会把同 capability 下其他有令牌可用的请求也串行阻塞。
        while True:
            async with lock:
                wait = bucket.consume(n)
            if wait == 0:
                return
            await asyncio.sleep(wait)
