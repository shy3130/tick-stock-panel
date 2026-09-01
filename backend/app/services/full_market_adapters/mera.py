"""Full-market adapter for the Issue #46 retrieval-routing (MERA daily-proxy).

Registered by the central registry in ``app.services.full_market_research``
(no self-registration here). The leak-safe daily retrieval-routing proxy is
evaluated once over the COMPLETE pinned cohort — never batched, never stitched.
Both claim verdicts (``test_rank_ic_increment`` / ``test_cost_adjusted_increment``)
and the placebo diagnostics travel inside the single response envelope;
research insufficiency stays explicitly unavailable (no fallbacks).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

from app.services.full_market_research import RunnerContext
from app.services.retrieval_routing_research import (
    DEFAULT_FEATURE_IDS,
    RoutingUnavailableReason,
    unavailable_routing_response,
)
from app.services.retrieval_routing_research.models import (
    DEFAULT_COST_BPS,
    DEFAULT_LABEL_HORIZON,
    RetrievalRoutingRequest,
    RetrievalRoutingResponse,
)
from app.services.retrieval_routing_research.panel import build_pinned_factor_panel
from app.services.retrieval_routing_research.routing import evaluate_retrieval_routing


@dataclass(frozen=True, slots=True)
class MeraFullMarketRequest:
    """One full-market retrieval-routing run: cohort window + frozen protocol knobs."""

    start: date
    end: date
    symbols: tuple[str, ...]
    routing: RetrievalRoutingRequest


class MeraAdapter:
    """Full-market MERA daily-proxy research (retrieval-routing evaluator)."""

    name = "mera"

    def build_request(
        self,
        start: date,
        end: date,
        cohort: list[str],
        *,
        oos_start: date | None,
        cost_bps: float | None,
    ) -> MeraFullMarketRequest:
        # The FULL cohort is embedded in a single request; the evaluator is
        # invoked exactly once — no batching, no verdict stitching.
        # ``oos_start`` is accepted for interface uniformity and deliberately
        # ignored: the 60/20/20 chronological split is a frozen definition-level
        # protocol that a request may only audit, never retune.
        return MeraFullMarketRequest(
            start=start,
            end=end,
            symbols=tuple(cohort),
            routing=RetrievalRoutingRequest(
                label_horizon=DEFAULT_LABEL_HORIZON,
                cost_bps=cost_bps if cost_bps is not None else DEFAULT_COST_BPS,
            ),
        )

    def evaluate(
        self, context: RunnerContext, request: MeraFullMarketRequest
    ) -> RetrievalRoutingResponse:
        # ``context.reader`` is the already generation-pinned canonical+markets
        # composite; a missing reader fails closed instead of synthesizing data.
        reader = context.reader
        if reader is None:
            return unavailable_routing_response(
                request.routing,
                RoutingUnavailableReason.PANEL_COVERAGE,
                "canonical history is not published",
            )
        # Performance seam: warm the reader cache with ONE padded canonical
        # scan over the complete cohort before the panel build. Production
        # preload failures fail closed (explicitly unavailable) — never a
        # silent per-symbol rescan.
        preload = getattr(reader, "preload_panel", None)
        if callable(preload):
            try:
                preload(
                    request.start - timedelta(days=400),
                    request.end + timedelta(days=180),
                    symbols=list(request.symbols),
                )
            except (KeyError, OSError, RuntimeError, TypeError, ValueError) as exc:
                return unavailable_routing_response(
                    request.routing,
                    RoutingUnavailableReason.PANEL_COVERAGE,
                    f"preload_panel_failed: {exc}",
                )
        try:
            panel = build_pinned_factor_panel(
                reader,
                request.symbols,
                request.start,
                request.end,
                feature_ids=DEFAULT_FEATURE_IDS,
                label_horizon=request.routing.label_horizon,
            )
        except (KeyError, OSError, RuntimeError, TypeError, ValueError) as exc:
            return unavailable_routing_response(
                request.routing,
                RoutingUnavailableReason.PANEL_COVERAGE,
                str(exc),
            )
        return evaluate_retrieval_routing(panel, request.routing)

    def serialize_verdict(self, verdict: Any) -> dict[str, Any]:
        return verdict.model_dump(mode="json")

    def extract_coverage(self, verdict: dict[str, Any]) -> dict[str, Any] | None:
        coverage = verdict.get("coverage")
        return coverage if isinstance(coverage, dict) else None
