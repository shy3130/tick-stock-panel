"""Full-market adapter for MACD arms research (``evaluate_macd_arms``).

Registered by the central registry in ``app.services.full_market_research``
(no self-registration here). This adapter targets the arms evaluator — NOT the
legacy ``evaluate_macd_stages`` staged contract. The market-index leg consumes
the repo's generation-pinned ``index_daily_research_reader``; without it the
arms verdict keeps ``regime_breakdown_oos`` explicitly unavailable — no
fallbacks.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from app.services.full_market_research import RunnerContext, reject_unsupported_parameters
from app.services.macd_stages import MacdArmsRequest, evaluate_macd_arms
from app.services.volume_breakout import DEFAULT_OOS_START

INDEX_READER_ATTR = "index_daily_research_reader"


class MacdArmsAdapter:
    """Full-market MACD arms research (default/tuned arms, bootstrap inside).

    ``cost_bps`` is accepted for interface uniformity and ignored: the arms
    contract books the fixed ``ROUND_TRIP_COST_BPS`` inside the evaluator.
    """

    name = "macd-arms"

    def build_request(
        self,
        start: date,
        end: date,
        cohort: list[str],
        *,
        oos_start: date | None,
        cost_bps: float | None,
        parameters: dict[str, Any] | None = None,
    ) -> MacdArmsRequest:
        if parameters is not None:
            reject_unsupported_parameters(parameters, {"start", "end", "oos_start"})
            start = parameters["start"]
            end = parameters["end"]
            oos_start = parameters["oos_start"]
        # The FULL cohort is embedded in a single request; the evaluator is
        # invoked exactly once — no batching, no verdict stitching.
        return MacdArmsRequest(
            start=start,
            end=end,
            symbols=list(cohort),
            oos_start=oos_start or DEFAULT_OOS_START,
        )

    def evaluate(self, context: RunnerContext, request: MacdArmsRequest) -> dict[str, Any]:
        # Generation-pinned index reader straight off the repo; missing reader
        # is handed through as ``None`` so the evaluator keeps each arm's
        # regime breakdown explicitly unavailable instead of synthesizing a
        # benchmark. The property materializes a fresh reader, so this adapter
        # owns its lifecycle: closed exactly once on success and on error.
        index_reader = getattr(context.repo, INDEX_READER_ATTR, None)
        try:
            return evaluate_macd_arms(
                context.reader,
                start=request.start,
                end=request.end,
                symbols=request.symbols,
                oos_start=request.oos_start,
                index_reader=index_reader,
            )
        finally:
            close = getattr(index_reader, "close", None)
            if callable(close):
                close()

    def serialize_verdict(self, verdict: dict[str, Any]) -> dict[str, Any]:
        return verdict

    def extract_coverage(self, verdict: dict[str, Any]) -> dict[str, Any] | None:
        segments = verdict.get("segments")
        if not isinstance(segments, dict):
            return None
        return {
            name: segment.get("coverage")
            for name, segment in segments.items()
            if isinstance(segment, dict)
        }
