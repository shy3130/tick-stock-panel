"""Full-market offline adapter for the N-shape pullback-depth factor (Issue #49).

Bridges the shared full-market runner (:mod:`app.services.full_market_research`)
to the causal pullback-depth evaluator
(:func:`app.services.n_shape_pullback_depth.evaluate_n_shape_pullback_depth`).

The COMPLETE PIT cohort is handed to the evaluator exactly once; the A/B/C,
unstratified and C-plus-golden-phoenix populations each return an independent verdict
inside that single payload, together with volume-overlap evidence and
sealed-generation provenance. There is no batching and no fallback: a missing
dedicated reader surfaces as the evaluator's explicit ``unavailable`` envelope.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

from app.services.full_market_research import RunnerContext
from app.services.n_shape_golden_phoenix import REQUIRED_READER_METHODS
from app.services.n_shape_pullback_depth import (
    ROUND_TRIP_COST_BPS,
    evaluate_n_shape_pullback_depth,
)

PRELOAD_LOOKBACK_DAYS = 400
PRELOAD_LOOKAHEAD_DAYS = 180


@dataclass(frozen=True, slots=True)
class NDepthRequest:
    """Immutable full-market request for one pullback-depth evaluation."""

    start: date
    end: date
    symbols: list[str]
    oos_start: date | None
    cost_bps: float


class NDepthAdapter:
    """Full-market N-shape pullback-depth research (A/B/C depth strata).

    ``oos_start`` is accepted for interface uniformity and carried on the
    request for audit symmetry but ignored: the factor's train/validation/test
    splits derive from the market calendar (60/20/20), not from a
    runner-supplied OOS boundary.
    """

    name = "n-depth"

    def build_request(
        self,
        start: date,
        end: date,
        cohort: list[str],
        *,
        oos_start: date | None,
        cost_bps: float | None,
    ) -> NDepthRequest:
        return NDepthRequest(
            start=start,
            end=end,
            symbols=list(cohort),
            oos_start=oos_start,
            cost_bps=ROUND_TRIP_COST_BPS if cost_bps is None else cost_bps,
        )

    def evaluate(self, context: RunnerContext, request: NDepthRequest) -> dict[str, Any]:
        reader = (
            context.reader
            if all(
                callable(getattr(context.reader, name, None)) for name in REQUIRED_READER_METHODS
            )
            else None
        )
        preload = getattr(reader, "preload_panel", None)
        if callable(preload):
            preload(
                request.start - timedelta(days=PRELOAD_LOOKBACK_DAYS),
                request.end + timedelta(days=PRELOAD_LOOKAHEAD_DAYS),
                symbols=request.symbols,
            )
        return evaluate_n_shape_pullback_depth(
            start=request.start,
            end=request.end,
            symbols=request.symbols,
            reader=reader,
            cost_bps=request.cost_bps,
        )

    def serialize_verdict(self, verdict: dict[str, Any]) -> dict[str, Any]:
        return verdict

    def extract_coverage(self, verdict: dict[str, Any]) -> dict[str, Any] | None:
        coverage = verdict.get("coverage")
        return coverage if isinstance(coverage, dict) else None
