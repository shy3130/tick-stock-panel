"""进程内 AI usage 快照 (P3/M9)。

按 purpose 与日期 (UTC) 累计 provider 原生 token 用量，供设置页只读暴露。

- 仅累计真实 provider 返回的 usage；流式 provider 不伪造；
- 只存聚合计数与调用次数，绝不存 prompt 正文 / 交易流水 / 凭据；
- 纯内存，进程重启即清空，定位为可观测性快照而非账本。
"""
from __future__ import annotations

import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone

from app.services.ai_structured.models import AIUsage, StructuredAIResult


def _utc_day(dt: datetime | None = None) -> str:
    return (dt or datetime.now(timezone.utc)).strftime("%Y-%m-%d")


@dataclass
class _Bucket:
    prompt_tokens: int = 0
    cached_prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    calls: int = 0

    def add(self, usage: AIUsage) -> None:
        self.prompt_tokens += usage.prompt_tokens
        self.cached_prompt_tokens += usage.cached_prompt_tokens
        self.completion_tokens += usage.completion_tokens
        self.total_tokens += usage.total_tokens
        self.calls += 1

    def to_dict(self) -> dict[str, int]:
        return {
            "prompt_tokens": self.prompt_tokens,
            "cached_prompt_tokens": self.cached_prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.total_tokens,
            "calls": self.calls,
        }


@dataclass
class UsageRegistry:
    """线程安全的进程内 usage 累加器。"""

    _by_purpose: dict[str, _Bucket] = field(default_factory=dict)
    _by_day: dict[str, _Bucket] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def record(
        self,
        purpose: str,
        usage: AIUsage,
        *,
        profile_id: str | None = None,
        day: str | None = None,
    ) -> None:
        """累计一次结构化调用的 usage (含失败重试已由 result.usage 累计)。"""
        bucket_p = self._by_purpose.setdefault(purpose, _Bucket())
        bucket_d = self._by_day.setdefault(day or _utc_day(), _Bucket())
        with self._lock:
            bucket_p.add(usage)
            bucket_d.add(usage)

    def record_result(self, purpose: str, result: StructuredAIResult) -> None:
        """从结构化结果累计 usage；取消的请求不计数。"""
        if getattr(result, "status", None) == "cancelled":
            return
        self.record(purpose, result.usage, profile_id=result.profile_id)

    def snapshot(self) -> dict[str, dict]:
        """只读快照: 按 purpose / 按日的聚合计数。不含任何敏感内容。"""
        with self._lock:
            return {
                "by_purpose": {k: v.to_dict() for k, v in self._by_purpose.items()},
                "by_day": {k: v.to_dict() for k, v in self._by_day.items()},
            }

    def reset(self) -> None:
        with self._lock:
            self._by_purpose.clear()
            self._by_day.clear()


_registry: UsageRegistry | None = None


def get_usage_registry() -> UsageRegistry:
    global _registry
    if _registry is None:
        _registry = UsageRegistry()
    return _registry


def record_structured_usage(purpose: str, result: StructuredAIResult) -> None:
    """入口便捷方法: 累计结构化结果 usage。"""
    get_usage_registry().record_result(purpose, result)


def usage_snapshot() -> dict[str, dict]:
    return get_usage_registry().snapshot()


__all__ = [
    "UsageRegistry",
    "get_usage_registry",
    "record_structured_usage",
    "usage_snapshot",
]
