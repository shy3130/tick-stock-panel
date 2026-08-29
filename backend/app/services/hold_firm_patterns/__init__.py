"""Auditable four-pattern hold research for Issue #38."""

from .adapters import ProductionReaderScopeUnavailable
from .evaluation import (
    assess_capability,
    evaluate_hold_firm_patterns,
    production_reader_scope,
)
from .models import (
    FACTOR_IDS,
    CapabilityResult,
    FactorResult,
    HoldFirmPatternsRequest,
    HoldFirmResponse,
    HoldFirmStatus,
    HoldFirmVerdict,
    UnavailabilityReason,
)

__all__ = (
    "FACTOR_IDS",
    "CapabilityResult",
    "FactorResult",
    "HoldFirmPatternsRequest",
    "HoldFirmResponse",
    "HoldFirmStatus",
    "HoldFirmVerdict",
    "ProductionReaderScopeUnavailable",
    "UnavailabilityReason",
    "assess_capability",
    "evaluate_hold_firm_patterns",
    "production_reader_scope",
)
