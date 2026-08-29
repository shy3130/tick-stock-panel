"""Issue #46 auditable retrieval-routing research service."""

from .models import (
    DEFAULT_FEATURE_IDS,
    MAX_PLACEBO_ROUNDS,
    MIN_PANEL_SYMBOLS,
    MIN_PLACEBO_ROUNDS,
    RetrievalRoutingRequest,
    RetrievalRoutingResponse,
    RoutingEvent,
    SplitName,
    UnavailabilityReason as RoutingUnavailableReason,
    unavailable_response as unavailable_routing_response,
)
from .panel import PinnedFactorPanel, build_pinned_factor_panel
from .routing import (
    NeighborLeakageError,
    SelectionDecision,
    assert_neighbor_boundaries,
    evaluate_retrieval_routing,
    select_routing_config,
    split_date_indices,
)

__all__ = [
    "PinnedFactorPanel",
    "build_pinned_factor_panel",
    "RetrievalRoutingRequest",
    "RetrievalRoutingResponse",
    "RoutingEvent",
    "SplitName",
    "DEFAULT_FEATURE_IDS",
    "MAX_PLACEBO_ROUNDS",
    "MIN_PANEL_SYMBOLS",
    "MIN_PLACEBO_ROUNDS",
    "evaluate_retrieval_routing",
    "NeighborLeakageError",
    "SelectionDecision",
    "assert_neighbor_boundaries",
    "select_routing_config",
    "split_date_indices",
    "RoutingUnavailableReason",
    "unavailable_routing_response",
]
