"""Full-market offline adapter for the D1-D5 doji-pattern evaluator.

Single-shot contract: the complete PIT cohort is handed to
:func:`evaluate_doji_patterns` exactly once — no batching, no verdict splicing.
D5 (``tail_session_doji``) opens a catalog-pinned intraday minutes bundle from
repo/provider readers; minute-data absence degrades D5 only and never blocks
the D1-D4 daily factors.  Its class is instantiated by the controlled
executor factory in ``app.research.catalog``; there is no local registry.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

from app.data_providers.fquant.escape_risk_intraday import (
    CatalogPinnedEscapeRiskIntradayReader,
)
from app.services.doji_patterns.evaluation import evaluate_doji_patterns
from app.services.doji_patterns.models import (
    DOJI_BODY_RATIO_MAX,
    DOJI_OOS_START_DEFAULT,
    DojiResponse,
    DojiStatus,
)
from app.services.full_market_adapters.pinning import production_scope_matches
from app.services.full_market_research import RunnerContext, reject_unsupported_parameters
from app.services.hold_firm_patterns.adapters import (
    FORWARD_CALENDAR_DAYS,
    LOOKBACK_CALENDAR_DAYS,
    ProductionReaderScope,
    ProductionReaderScopeUnavailable,
    production_reader_scope,
)
from app.services.hold_firm_patterns.models import (
    COST_BPS_DEFAULT,
    COST_BPS_MAX,
    SYMBOL_PATTERN,
    UnavailabilityReason,
)


def _noop() -> None:
    return None


@dataclass(frozen=True)
class DojiFullMarketRequest:
    """Duck-typed evaluator request without the interactive-API symbol cap.

    ``evaluate_doji_patterns`` reads plain attributes only, so the offline
    runner passes the complete cohort in one request.  The 200-symbol limit on
    :class:`~app.services.doji_patterns.models.DojiPatternsRequest` is an
    interactive transport constraint, not an evaluator one.
    """

    symbols: list[str]
    start: date
    end: date
    oos_start: date
    theta_body_ratio: float
    cost_bps: float

    def echo(self) -> dict[str, Any]:
        """JSON-safe request echo used as the runner's research-id material."""
        return {
            "symbols": list(self.symbols),
            "start": self.start.isoformat(),
            "end": self.end.isoformat(),
            "oos_start": self.oos_start.isoformat(),
            "theta_body_ratio": self.theta_body_ratio,
            "cost_bps": self.cost_bps,
        }


@dataclass(frozen=True)
class DojiFullMarketVerdict:
    """Evaluator response plus the D5 intraday bundle provenance."""

    response: DojiResponse
    request: DojiFullMarketRequest
    intraday_report: dict[str, Any] | None


class DojiPatternsFullMarketAdapter:
    """Full-market D1-D5 doji research (``evaluate_doji_patterns``)."""

    name = "doji-patterns"

    def build_request(
        self,
        start: date,
        end: date,
        cohort: Sequence[str],
        *,
        oos_start: date | None,
        cost_bps: float | None,
        parameters: dict[str, Any] | None = None,
    ) -> DojiFullMarketRequest:
        theta_body_ratio = DOJI_BODY_RATIO_MAX
        if parameters is not None:
            reject_unsupported_parameters(
                parameters, {"start", "oos_start", "end", "theta_body_ratio", "cost_bps"}
            )
            start, end = parameters["start"], parameters["end"]
            oos_start, cost_bps = parameters["oos_start"], parameters["cost_bps"]
            theta_body_ratio = parameters["theta_body_ratio"]
        symbols = [symbol.strip().upper() for symbol in cohort]
        if not symbols:
            raise ValueError("doji full-market request requires a non-empty cohort")
        if any(re.fullmatch(SYMBOL_PATTERN, symbol) is None for symbol in symbols):
            raise ValueError("cohort symbols must be canonical A-share identifiers")
        if len(set(symbols)) != len(symbols):
            raise ValueError("cohort symbols must be unique")
        resolved_oos = oos_start if oos_start is not None else DOJI_OOS_START_DEFAULT
        resolved_cost = COST_BPS_DEFAULT if cost_bps is None else float(cost_bps)
        if not start < resolved_oos <= end:
            raise ValueError("doji full-market request requires start < oos_start <= end")
        if not 0.0 <= resolved_cost <= COST_BPS_MAX:
            raise ValueError(f"cost_bps outside the doji contract range [0, {COST_BPS_MAX}]")
        return DojiFullMarketRequest(
            symbols=symbols,
            start=start,
            end=end,
            oos_start=resolved_oos,
            theta_body_ratio=theta_body_ratio,
            cost_bps=resolved_cost,
        )

    def evaluate(
        self, context: RunnerContext, request: DojiFullMarketRequest
    ) -> DojiFullMarketVerdict:
        try:
            with production_reader_scope(context.repo) as scope:
                if not production_scope_matches(context, scope.canonical, scope.market_facts):
                    return DojiFullMarketVerdict(
                        response=_unavailable(UnavailabilityReason.CANONICAL_READER),
                        request=request,
                        intraday_report=None,
                    )
                try:
                    self._preload_panel(scope, request)
                except Exception:
                    return DojiFullMarketVerdict(
                        response=_unavailable(UnavailabilityReason.CANONICAL_READER),
                        request=request,
                        intraday_report=None,
                    )
                bundle, intraday_report, close_bundle = self._open_intraday_bundle(scope, request)
                try:
                    response = evaluate_doji_patterns(
                        request,
                        scope.canonical,
                        scope.market_facts,
                        scope.universe_reader,
                        bundle,
                    )
                finally:
                    close_bundle()
        except ProductionReaderScopeUnavailable as exc:
            return DojiFullMarketVerdict(
                response=_unavailable(exc.reason),
                request=request,
                intraday_report=None,
            )
        return DojiFullMarketVerdict(
            response=response, request=request, intraday_report=intraday_report
        )

    def _preload_panel(self, scope: ProductionReaderScope, request: DojiFullMarketRequest) -> None:
        """Warm one pinned canonical panel; failure is order-level unavailable."""
        preload = getattr(scope.canonical, "preload_panel", None)
        if not callable(preload):
            return
        preload(
            request.start - timedelta(days=LOOKBACK_CALENDAR_DAYS),
            request.end + timedelta(days=FORWARD_CALENDAR_DAYS),
            symbols=list(request.symbols),
        )

    def _open_intraday_bundle(
        self, scope: ProductionReaderScope, request: DojiFullMarketRequest
    ) -> tuple[object | None, dict[str, Any], Callable[[], None]]:
        """Open D5 routes from the preflight pin when available."""
        try:
            days = scope.canonical.market_days(request.start, request.end)
            pinned_opener = getattr(
                getattr(scope, "repo", None), "open_escape_risk_intraday_reader", None
            )
            if callable(pinned_opener):
                reader = pinned_opener(scope.canonical.manifest(), tuple(days))
            else:
                reader = CatalogPinnedEscapeRiskIntradayReader(days, scope.market_facts)
            if reader is None:
                return None, {"provided": False, "unavailable_symbol_days": None}, _noop
        except Exception:
            return None, {"provided": False, "unavailable_symbol_days": None}, _noop
        try:
            bundle = reader.load(list(request.symbols))
        except Exception:
            reader.close()
            return None, {"provided": False, "unavailable_symbol_days": None}, _noop
        report = {"provided": True, "unavailable_symbol_days": len(bundle.unavailable)}
        return bundle, report, reader.close

    def serialize_verdict(self, verdict: DojiFullMarketVerdict) -> dict[str, Any]:
        payload = verdict.response.model_dump(mode="json")
        payload["request"] = verdict.request.echo()
        if verdict.intraday_report is not None:
            payload["d5_intraday"] = verdict.intraday_report
        return payload

    def extract_coverage(self, verdict: dict[str, Any]) -> dict[str, Any] | None:
        coverage = verdict.get("coverage")
        return coverage if isinstance(coverage, dict) else None


def _unavailable(reason: UnavailabilityReason) -> DojiResponse:
    return DojiResponse(status=DojiStatus.UNAVAILABLE, unavailable_reason=reason)


__all__ = [
    "DojiFullMarketRequest",
    "DojiFullMarketVerdict",
    "DojiPatternsFullMarketAdapter",
]
