"""Full-market adapter for weekly flagpole research (F0-F5).

Registered by the central registry in ``app.services.full_market_research``
(no self-registration here). The market-index leg consumes the repo's
generation-pinned ``index_daily_research_reader``; the F5 industry leg stays
explicitly unavailable until an industry reader is wired — no fallbacks.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from app.services.full_market_research import RunnerContext
from app.services.weekly_flagpole import service as weekly_flagpole_service
from app.services.weekly_flagpole.models import COST_BPS, OOS_START, WeeklyFlagpoleRequest

INDEX_READER_ATTR = "index_daily_research_reader"


class WeeklyFlagpoleAdapter:
    """Full-market weekly flagpole research (F0-F5, cluster bootstrap inside)."""

    name = "weekly-flagpole"

    def build_request(
        self,
        start: date,
        end: date,
        cohort: list[str],
        *,
        oos_start: date | None,
        cost_bps: float | None,
    ) -> WeeklyFlagpoleRequest:
        # The FULL cohort is embedded in a single request; the evaluator is
        # invoked exactly once — no batching, no verdict stitching.
        return WeeklyFlagpoleRequest(
            start=start,
            end=end,
            symbols=list(cohort),
            oos_start=oos_start or OOS_START,
            cost_bps=cost_bps if cost_bps is not None else COST_BPS,
        )

    def evaluate(self, context: RunnerContext, request: WeeklyFlagpoleRequest) -> Any:
        # Generation-pinned index reader straight off the repo; the reader is
        # freshly opened per access, so it is closed in ``finally`` — mirroring
        # the research-API pattern. A missing reader is handed through as
        # ``None`` so the service fails that leg closed (``index_reader_missing``)
        # instead of synthesizing a benchmark.
        index_reader = getattr(context.repo, INDEX_READER_ATTR, None)
        try:
            # Warm the canonical panel once for the FULL cohort (start-900d →
            # end+250d) when the pinned reader supports it. Preload failures
            # propagate fail-closed — never fall back to per-symbol disk scans.
            preload = getattr(context.reader, "preload_panel", None)
            if callable(preload):
                preload(
                    request.start - timedelta(days=900),
                    request.end + timedelta(days=250),
                    symbols=request.symbols,
                )
            return weekly_flagpole_service.evaluate(
                request, context.reader, index_reader=index_reader
            )
        finally:
            close = getattr(index_reader, "close", None)
            if callable(close):
                close()

    def serialize_verdict(self, verdict: Any) -> dict[str, Any]:
        return verdict.model_dump(mode="json")

    def extract_coverage(self, verdict: dict[str, Any]) -> dict[str, Any] | None:
        return verdict.get("coverage")
