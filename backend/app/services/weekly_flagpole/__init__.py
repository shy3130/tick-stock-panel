"""Weekly flagpole research package."""

from .benchmark import EqualWeightBenchmark
from .detector import detect_symbol_events
from .entries import flag_low_retest, volume_shrink_restart, weekly_reclaim
from .models import (
    WeeklyFlagpoleCapabilities,
    WeeklyFlagpoleFactor,
    WeeklyFlagpoleRequest,
    WeeklyFlagpoleResponse,
)
from .service import assess_capability, evaluate, evaluate_weekly_flagpole, resolve_reader
from .weekly import WeeklyBar, aggregate_weekly_bars

__all__ = [
    "EqualWeightBenchmark",
    "WeeklyBar",
    "WeeklyFlagpoleCapabilities",
    "WeeklyFlagpoleFactor",
    "WeeklyFlagpoleRequest",
    "WeeklyFlagpoleResponse",
    "aggregate_weekly_bars",
    "assess_capability",
    "detect_symbol_events",
    "evaluate",
    "evaluate_weekly_flagpole",
    "flag_low_retest",
    "resolve_reader",
    "volume_shrink_restart",
    "weekly_reclaim",
]
