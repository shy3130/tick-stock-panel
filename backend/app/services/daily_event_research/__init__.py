"""Auditable Issue #45 daily-event research service."""

from .dugu_trend import (
    DUGU_VARIANTS,
    DuguTrendConfig,
    DuguTrendDetector,
    resolve_dugu_config,
)
from .evaluation import evaluate_daily_events
from .models import (
    CensorReason,
    DailyEventDetector,
    DailyEventRequest,
    DailyEventResponse,
    DailyEventStatus,
    DailyEventVerdict,
    Detection,
    DetectionEvidence,
    EventCensor,
    EventOutcome,
    UnavailabilityReason,
)
from .escape_risk import (
    SIGNAL_CAPABILITIES,
    EscapeS1Detector,
    EscapeS8Detector,
    EscapeS9Detector,
    aggregate_escape_signals,
)
from .pre_surge import (
    PreSurgeDetector,
    PreSurgeParams,
    PreSurgeStudyAggregator,
    PreSurgeVerdict,
    detect_f1_limit_up,
    detect_f2_gap_unfilled,
    detect_f3_relative_bullish,
    detect_f4_volume_stack,
)
from .production import (
    evaluate_escape_risk_production,
    evaluate_pre_surge_production,
)

__all__ = (
    "CensorReason",
    "DUGU_VARIANTS",
    "DailyEventDetector",
    "DailyEventRequest",
    "DailyEventResponse",
    "DailyEventStatus",
    "DailyEventVerdict",
    "Detection",
    "DetectionEvidence",
    "DuguTrendConfig",
    "DuguTrendDetector",
    "EventCensor",
    "EventOutcome",
    "UnavailabilityReason",
    "evaluate_daily_events",
    "resolve_dugu_config",
    "EscapeS1Detector",
    "EscapeS8Detector",
    "EscapeS9Detector",
    "PreSurgeDetector",
    "PreSurgeParams",
    "PreSurgeStudyAggregator",
    "PreSurgeVerdict",
    "SIGNAL_CAPABILITIES",
    "aggregate_escape_signals",
    "detect_f1_limit_up",
    "detect_f2_gap_unfilled",
    "detect_f3_relative_bullish",
    "detect_f4_volume_stack",
    "evaluate_escape_risk_production",
    "evaluate_pre_surge_production",
)
