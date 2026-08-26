"""受控外部 fallback 适配层 (realtime / chart_live)。

独立于 FQuantProvider 与 QuoteService 的只读展示适配层。
- 默认关闭 (preferences.external_fallback_enabled)
- 仅补 realtime 快照缺失/陈旧，或当前 CN 交易日 A 股图表目标日期为空
- 全程 provenance 标记 (source + degraded)
- 绝不写入 repository / enriched / monitor / screener / backtest

完整设计见 backend/docs/CONTROLLED_EXTERNAL_FALLBACK_DESIGN.md。
"""
from __future__ import annotations

from app.services.external_fallback.adapter import (
    ChartFallbackResult,
    DepthFallbackResult,
    ExternalFallbackAdapter,
    FallbackReason,
    FallbackResult,
    Scope,
    get_adapter,
    reset_adapter,
)
from app.services.external_fallback.circuit import CircuitBreaker

__all__ = [
    "CircuitBreaker",
    "ExternalFallbackAdapter",
    "FallbackReason",
    "FallbackResult",
    "Scope",
    "DepthFallbackResult",
    "ChartFallbackResult",
    "reset_adapter",
    "get_adapter",
]
