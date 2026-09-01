"""Full-market adapter for Issue #48 escape-risk research (S1-S10).

Registered by the central registry in ``app.services.full_market_research``
(no self-registration here). Daily signals S1/S8/S9 consume the pinned
canonical generation; intraday signals S2-S7/S10 consume a catalog-pinned
minutes/trans reader opened through the provider registry with the markets
generation bound to the canonical manifest. Missing intraday reader is
fail-closed here (``status: unavailable``) — unlike the interactive API path
this offline verdict must never degrade to a daily-only report. Per-day
route/PIT gaps stay explicit censors inside the evaluator: S10 strictly
censors ``pit_fact_missing`` when a pinned previous-day ``ltgb`` observation
is unavailable before the signal session, and reader ``close`` stays with the
adapter that opened it (the runner-owned composite reader is never closed here).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

from app.data_providers.registry import get_active_provider_name, get_provider
from app.services.daily_event_research.escape_risk import COST_BPS_DEFAULT
from app.services.daily_event_research.production import (
    ESCAPE_SCHEMA,
    evaluate_escape_risk_production,
)
from app.services.full_market_adapters.pinning import source_reader_matches
from app.services.full_market_research import RunnerContext, reject_unsupported_parameters
from app.services.research_sealed_data import PublishedCanonicalDailyReader
from app.services.volume_breakout import DEFAULT_OOS_START

# Calendar-day over-fetch so the intraday volume-history window (5 trading
# days before ``start``) is always covered by the catalog-pinned reader.
INTRADAY_LOOKBACK_CALENDAR_DAYS = 30
# One warm-panel preload covering the evaluator's calendar window; failure
# fails closed instead of per-symbol disk scans.
PRELOAD_LOOKBACK_CALENDAR_DAYS = 400
PRELOAD_FORWARD_CALENDAR_DAYS = 120
REASON_PRELOAD_FAILED = "unavailable_preload_panel_failed"


@dataclass(frozen=True, slots=True)
class EscapeRiskFullMarketRequest:
    """Single request carrying the COMPLETE cohort; never batched downstream."""

    start: date
    end: date
    symbols: list[str]
    oos_start: date
    cost_bps: float

    def echo(self) -> dict[str, Any]:
        return {
            "symbols": list(self.symbols),
            "start": self.start.isoformat(),
            "end": self.end.isoformat(),
            "oos_start": self.oos_start.isoformat(),
            "cost_bps": self.cost_bps,
        }


class EscapeRiskAdapter:
    """Full-market Issue #48 escape-risk research (S1-S10, bootstrap inside)."""

    name = "escape-risk"

    def build_request(
        self,
        start: date,
        end: date,
        cohort: list[str],
        *,
        oos_start: date | None,
        cost_bps: float | None,
        parameters: dict[str, Any] | None = None,
    ) -> EscapeRiskFullMarketRequest:
        if parameters is not None:
            reject_unsupported_parameters(parameters, {"start", "end", "oos_start", "cost_bps"})
            start, end = parameters["start"], parameters["end"]
            oos_start, cost_bps = parameters["oos_start"], parameters["cost_bps"]
        # The FULL cohort is embedded in a single request; the evaluator is
        return EscapeRiskFullMarketRequest(
            start=start,
            end=end,
            symbols=list(cohort),
            oos_start=oos_start or DEFAULT_OOS_START,
            cost_bps=cost_bps if cost_bps is not None else COST_BPS_DEFAULT,
        )

    def evaluate(
        self, context: RunnerContext, request: EscapeRiskFullMarketRequest
    ) -> dict[str, Any]:
        canonical = PublishedCanonicalDailyReader.from_repository(context.repo)
        if canonical is None:
            return self._unavailable(request, ["unavailable_canonical_reader"])
        if not source_reader_matches(context, "canonical", canonical):
            return self._unavailable(request, ["pinned_canonical_generation_mismatch"])
        # One warm-panel preload for the whole cohort before any evaluation;
        # failure is an explicit unavailable — never per-symbol disk fallback.
        preload = getattr(canonical, "preload_panel", None)
        if callable(preload):
            try:
                preload(
                    request.start - timedelta(days=PRELOAD_LOOKBACK_CALENDAR_DAYS),
                    request.end + timedelta(days=PRELOAD_FORWARD_CALENDAR_DAYS),
                    symbols=list(request.symbols),
                )
            except (
                AttributeError,
                KeyError,
                OSError,
                RuntimeError,
                TypeError,
                ValueError,
            ) as exc:
                envelope = self._unavailable(request, [REASON_PRELOAD_FAILED])
                envelope["preload_error"] = str(exc)
                return envelope
        intraday_reader = self._open_intraday_reader(context.repo, canonical, request)
        if intraday_reader is None:
            return self._unavailable(request, ["unavailable_intraday_reader"])
        try:
            verdict = evaluate_escape_risk_production(
                symbols=list(request.symbols),
                start=request.start,
                end=request.end,
                oos_start=request.oos_start,
                canonical_reader=canonical,
                intraday_reader=intraday_reader,
                cost_bps=request.cost_bps,
            )
        finally:
            # Only the adapter-owned intraday reader is closed here; the
            # runner-owned composite reader remains owned by the runner.
            close = getattr(intraday_reader, "close", None)
            if callable(close):
                close()
        runtime = self._intraday_runtime_status(verdict)
        if runtime != "available":
            return self._unavailable(
                request,
                [f"unavailable_intraday_reader:{runtime}"],
            )
        return verdict

    def serialize_verdict(self, verdict: dict[str, Any]) -> dict[str, Any]:
        return verdict

    def extract_coverage(self, verdict: dict[str, Any]) -> dict[str, Any] | None:
        capabilities = verdict.get("capabilities")
        if not isinstance(capabilities, dict):
            return None
        intraday = capabilities.get("intraday")
        if not isinstance(intraday, dict):
            return None
        coverage = intraday.get("coverage")
        return coverage if isinstance(coverage, dict) else None

    @staticmethod
    def _intraday_runtime_status(verdict: dict[str, Any]) -> Any:
        capabilities = verdict.get("capabilities")
        if not isinstance(capabilities, dict):
            return None
        intraday = capabilities.get("intraday")
        if not isinstance(intraday, dict):
            return None
        return intraday.get("runtime_status")

    @staticmethod
    def _open_intraday_reader(
        repo: Any,
        canonical: PublishedCanonicalDailyReader,
        request: EscapeRiskFullMarketRequest,
    ) -> Any | None:
        """Open exact preflight routes; only unpinned interactive fallback uses provider current."""
        pinned_opener = getattr(repo, "open_escape_risk_intraday_reader", None)
        market_days = canonical.market_days(
            request.start - timedelta(days=INTRADAY_LOOKBACK_CALENDAR_DAYS),
            request.end,
        )
        if callable(pinned_opener):
            try:
                return pinned_opener(canonical.manifest(), tuple(market_days))
            except (AttributeError, OSError, RuntimeError, TypeError, ValueError):
                return None
        try:
            provider_name = get_active_provider_name(capability="minute")
            provider = get_provider(provider_name)
            opener = getattr(provider, "open_escape_risk_intraday_reader", None)
            if not callable(opener):
                return None
            return opener(canonical.manifest(), tuple(market_days))
        except (AttributeError, OSError, RuntimeError, TypeError, ValueError):
            return None

    @staticmethod
    def _unavailable(
        request: EscapeRiskFullMarketRequest,
        reasons: list[str],
    ) -> dict[str, Any]:
        return {
            "schema": ESCAPE_SCHEMA,
            "status": "unavailable",
            "unavailable_reasons": reasons,
            "request": request.echo(),
            "promoted": False,
        }


__all__ = [
    "INTRADAY_LOOKBACK_CALENDAR_DAYS",
    "EscapeRiskAdapter",
    "EscapeRiskFullMarketRequest",
]
