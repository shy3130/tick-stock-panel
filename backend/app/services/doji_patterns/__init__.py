"""Auditable D1-D4 doji research services."""

from .confirmation import ConfirmationDetector
from .evaluation import (
    ProductionReaderScope,
    ProductionReaderScopeUnavailable,
    assess_doji_capability,
    evaluate_doji_patterns,
    production_reader_scope,
)
from .gravestone import GravestoneDetector
from .models import (
    DOJI_FACTOR_IDS,
    DojiPatternsRequest,
    DojiResponse,
    DojiStatus,
    DojiVerdict,
    UnavailabilityReason,
)
from .position_interaction import DojiPositionDetector
from .t_bar import TBarDetector
from .tail_session import TailSessionDetector, classify_tail_shape

__all__ = [
    "DOJI_FACTOR_IDS",
    "ConfirmationDetector",
    "DojiPatternsRequest",
    "DojiPositionDetector",
    "DojiResponse",
    "DojiStatus",
    "DojiVerdict",
    "GravestoneDetector",
    "ProductionReaderScope",
    "ProductionReaderScopeUnavailable",
    "TBarDetector",
    "TailSessionDetector",
    "UnavailabilityReason",
    "assess_doji_capability",
    "classify_tail_shape",
    "evaluate_doji_patterns",
    "production_reader_scope",
]
