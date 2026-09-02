"""Full-market adapter for Issue #47 pre-surge features (F1-F4 + combined).

Registered by the central registry in ``app.services.full_market_research``
(no self-registration here). The production evaluator consumes the repo's
generation-pinned published canonical / market-facts / PIT-presence reader
stack via :func:`production_reader_scope`; a missing stack fails closed as an
explicit ``unavailable`` verdict — no fallbacks. The FULL cohort is embedded
in one request and the evaluator is invoked exactly once — no batching, no
verdict stitching.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from enum import Enum
from typing import Any, Final

from app.services.daily_event_research.production import (
    PRE_SURGE_SCHEMA,
    evaluate_pre_surge_production,
)
from app.services.full_market_adapters.pinning import production_scope_matches
from app.services.full_market_research import RunnerContext, reject_unsupported_parameters
from app.services.hold_firm_patterns.adapters import (
    ProductionReaderScopeUnavailable,
    production_reader_scope,
)

BENCHMARK_SYMBOL: Final = "000300.SH"
DEFAULT_COST_BPS: Final = 10.0
PRELOAD_WARMUP_CALENDAR_DAYS: Final = 400
PRELOAD_FORWARD_CALENDAR_DAYS: Final = 120
REASON_PRELOAD_FAILED: Final = "unavailable_preload_panel_failed"


@dataclass(frozen=True, slots=True)
class PreSurgeFullMarketRequest:
    """One frozen pre-surge study request over the complete cohort."""

    symbols: list[str]
    start: date
    oos_start: date
    end: date
    benchmark_symbol: str = BENCHMARK_SYMBOL
    cost_bps: float = DEFAULT_COST_BPS


def _jsonify(value: Any) -> Any:
    """Recursively JSON-safe a verdict: enums → values, dates → ISO strings."""
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, dict):
        return {key: _jsonify(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonify(item) for item in value]
    return value


class PreSurgeAdapter:
    """Full-market pre-surge feature research (F1-F4 + combined, per-arm risk)."""

    name = "pre-surge"

    def build_request(
        self,
        start: date,
        end: date,
        cohort: list[str],
        *,
        oos_start: date | None,
        cost_bps: float | None,
        parameters: dict[str, Any] | None = None,
    ) -> PreSurgeFullMarketRequest:
        benchmark_symbol = BENCHMARK_SYMBOL
        if parameters is not None:
            reject_unsupported_parameters(
                parameters, {"start", "oos_start", "end", "benchmark_symbol", "cost_bps"}
            )
            start, end = parameters["start"], parameters["end"]
            oos_start, cost_bps = parameters["oos_start"], parameters["cost_bps"]
            benchmark_symbol = parameters["benchmark_symbol"]
        # The frozen IS/OOS boundary is part of the pre-registered study design:
        # it must be supplied explicitly, never fabricated from a default.
        if oos_start is None:
            raise ValueError("pre_surge requires an explicit frozen oos_start")
        return PreSurgeFullMarketRequest(
            symbols=list(cohort),
            start=start,
            oos_start=oos_start,
            end=end,
            benchmark_symbol=benchmark_symbol,
            cost_bps=cost_bps if cost_bps is not None else DEFAULT_COST_BPS,
        )

    def evaluate(
        self, context: RunnerContext, request: PreSurgeFullMarketRequest
    ) -> dict[str, Any]:
        # Generation-pinned dedicated reader stack straight off the repo. A
        # missing stack is an explicit ``unavailable`` verdict; the evaluator
        # itself fails closed on missing pinned daily inputs / PIT universe.
        try:
            with production_reader_scope(context.repo) as scope:
                if not production_scope_matches(context, scope.canonical, scope.market_facts):
                    return {
                        "schema": PRE_SURGE_SCHEMA,
                        "status": "unavailable",
                        "reason": "pinned_source_generation_mismatch",
                        "promoted": False,
                    }
                # One warm-panel preload covering detector warmup and forward
                # horizons; failure fails closed instead of per-symbol reads.
                preload = getattr(scope.canonical, "preload_panel", None)
                if callable(preload):
                    try:
                        preload(
                            request.start - timedelta(days=PRELOAD_WARMUP_CALENDAR_DAYS),
                            request.end + timedelta(days=PRELOAD_FORWARD_CALENDAR_DAYS),
                            symbols=request.symbols,
                        )
                    except (
                        AttributeError,
                        KeyError,
                        OSError,
                        RuntimeError,
                        TypeError,
                        ValueError,
                    ) as exc:
                        return {
                            "schema": PRE_SURGE_SCHEMA,
                            "status": "unavailable",
                            "reason": REASON_PRELOAD_FAILED,
                            "detail": str(exc),
                            "promoted": False,
                        }
                return evaluate_pre_surge_production(
                    symbols=request.symbols,
                    start=request.start,
                    oos_start=request.oos_start,
                    end=request.end,
                    canonical_reader=scope.canonical,
                    market_facts_reader=scope.market_facts,
                    universe_reader=scope.universe_reader,
                    benchmark_symbol=request.benchmark_symbol,
                    cost_bps=request.cost_bps,
                )
        except ProductionReaderScopeUnavailable as exc:
            return {
                "schema": PRE_SURGE_SCHEMA,
                "status": "unavailable",
                "reason": exc.reason.value,
                "detail": exc.detail,
                "promoted": False,
            }

    def serialize_verdict(self, verdict: dict[str, Any]) -> dict[str, Any]:
        # Preserves risk_metrics / risk_metric_definitions and the rest of the
        # payload verbatim, only normalizing enums/dates for JSON round-trips.
        return _jsonify(verdict)

    def extract_coverage(self, verdict: dict[str, Any]) -> dict[str, Any] | None:
        coverage = verdict.get("coverage")
        return coverage if isinstance(coverage, dict) else None
