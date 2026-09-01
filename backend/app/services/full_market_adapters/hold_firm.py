"""Full-market adapter for hold-firm four-pattern research (Issue #38).

Registered by the central registry in ``app.services.full_market_research``
(no self-registration here). Evaluation runs over the repo's generation-pinned
production reader stack (canonical + markets + retrospective PIT universe
presence) opened through ``production_reader_scope``. A missing reader layer is
explicitly unavailable — no fallbacks and no cohort widening that could hide
survivorship bias.
"""

from __future__ import annotations

import re
from datetime import date, timedelta
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.services.full_market_adapters.pinning import production_scope_matches
from app.services.full_market_research import RunnerContext
from app.services.hold_firm_patterns import (
    HoldFirmResponse,
    HoldFirmStatus,
    ProductionReaderScopeUnavailable,
    production_reader_scope,
)
from app.services.hold_firm_patterns.evaluation import evaluate_hold_firm_patterns
from app.services.hold_firm_patterns.models import (
    COST_BPS_DEFAULT,
    COST_BPS_MAX,
    SYMBOL_PATTERN,
    UnavailabilityReason,
)

DEFAULT_OOS_START = date(2025, 7, 1)


class HoldFirmFullMarketRequest(BaseModel):
    """FULL-cohort transport for exactly one hold-firm evaluator pass.

    ``HoldFirmPatternsRequest`` caps ``symbols`` at 200 — a REST transport
    guard. The full-market runner must hand the complete pinned cohort to the
    evaluator exactly once, so this mirrors the DTO contract minus that cap
    (canonical A-share identifiers, unique, ``start < oos_start <= end``).
    """

    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)

    symbols: list[str] = Field(min_length=1)
    start: date
    oos_start: date
    end: date
    cost_bps: float = Field(default=COST_BPS_DEFAULT, ge=0, le=COST_BPS_MAX)

    @field_validator("symbols")
    @classmethod
    def _symbols(cls, symbols: list[str]) -> list[str]:
        normalized = [symbol.strip().upper() for symbol in symbols]
        if any(re.fullmatch(SYMBOL_PATTERN, symbol) is None for symbol in normalized):
            raise ValueError("symbols must be canonical A-share identifiers")
        if len(normalized) != len(set(normalized)):
            raise ValueError("symbols must be unique")
        return normalized

    @model_validator(mode="after")
    def _dates(self) -> HoldFirmFullMarketRequest:
        if not self.start < self.oos_start <= self.end:
            raise ValueError("start < oos_start <= end required")
        return self


class HoldFirmAdapter:
    """Full-market hold-firm research (four detectors, one immutable run)."""

    name = "hold-firm"

    def build_request(
        self,
        start: date,
        end: date,
        cohort: list[str],
        *,
        oos_start: date | None,
        cost_bps: float | None,
    ) -> HoldFirmFullMarketRequest:
        # The FULL cohort is embedded in a single uncapped request; the
        # evaluator is invoked exactly once — no batching, no verdict
        # stitching, no cohort thinning that would bias the survivorship
        # picture.
        return HoldFirmFullMarketRequest(
            start=start,
            end=end,
            symbols=list(cohort),
            oos_start=oos_start or DEFAULT_OOS_START,
            cost_bps=cost_bps if cost_bps is not None else COST_BPS_DEFAULT,
        )

    def evaluate(
        self, context: RunnerContext, request: HoldFirmFullMarketRequest
    ) -> HoldFirmResponse:
        # Generation-pinned production reader stack straight off the repo
        # (canonical + markets + retrospective PIT universe presence); any
        # missing layer fails closed as an explicit unavailable verdict.
        try:
            with production_reader_scope(context.repo) as scope:
                if not production_scope_matches(context, scope.canonical, scope.market_facts):
                    return HoldFirmResponse(
                        status=HoldFirmStatus.UNAVAILABLE,
                        unavailable_reason=UnavailabilityReason.CANONICAL_READER,
                    )
                preload = getattr(scope.canonical, "preload_panel", None)
                if callable(preload):
                    try:
                        preload(
                            request.start - timedelta(days=400),
                            request.end + timedelta(days=120),
                            symbols=list(request.symbols),
                        )
                    except (OSError, RuntimeError, TypeError, ValueError):
                        return HoldFirmResponse(
                            status=HoldFirmStatus.UNAVAILABLE,
                            unavailable_reason=UnavailabilityReason.CANONICAL_READER,
                        )
                return evaluate_hold_firm_patterns(
                    request, scope.canonical, scope.market_facts, scope.universe_reader
                )
        except ProductionReaderScopeUnavailable as exc:
            return HoldFirmResponse(
                status=HoldFirmStatus.UNAVAILABLE,
                unavailable_reason=exc.reason,
            )
        except AttributeError:
            # The repo lacks the pinned production reader surface entirely;
            # still an explicit canonical-reader unavailable, never a fallback.
            return HoldFirmResponse(
                status=HoldFirmStatus.UNAVAILABLE,
                unavailable_reason=UnavailabilityReason.CANONICAL_READER,
            )

    def serialize_verdict(self, verdict: HoldFirmResponse) -> dict[str, Any]:
        payload = verdict.model_dump(mode="json")
        reason = payload.get("unavailable_reason")
        payload["unavailable_reasons"] = [reason] if reason else []
        payload["bias_disclosure"] = self._bias_disclosure(verdict)
        return payload

    @staticmethod
    def _bias_disclosure(verdict: HoldFirmResponse) -> dict[str, Any]:
        """Survivorship-bias disclosure: retained, never hidden by cohort scope."""
        universe = verdict.provenance.identities.universe if verdict.provenance else None
        if universe is None:
            return {
                "survivorship_bias": "unavailable",
                "universe_provenance": None,
                "retrospective_universe": None,
                "cohort_expanded_to_hide_bias": False,
                "note": "universe provenance unavailable; verdict stays explicitly unavailable",
            }
        return {
            "survivorship_bias": "controlled_within_pinned_universe",
            "universe_provenance": {
                "generation": universe.generation,
                "manifest_sha256": universe.manifest_sha256,
                "artifact": universe.artifact,
                "rule_version": universe.rule_version,
                "status_filter": universe.status_filter,
            },
            "retrospective_universe": universe.retrospective,
            "cohort_expanded_to_hide_bias": False,
            "note": (
                "cohort is the full pinned-market PIT cohort handed to the "
                "evaluator intact; membership is retrospective exact-day "
                "presence and absence is never inferred"
            ),
        }

    def extract_coverage(self, verdict: dict[str, Any]) -> dict[str, Any] | None:
        factors = verdict.get("factors")
        if not isinstance(factors, list) or not factors:
            return None
        coverage: dict[str, Any] = {}
        for factor in factors:
            if not isinstance(factor, dict) or "factor_id" not in factor:
                continue
            coverage[factor["factor_id"]] = {
                "parent_events": factor.get("parent_events"),
                "qualified_events": factor.get("qualified_events"),
                "not_selected_events": factor.get("not_selected_events"),
                "oos": factor.get("oos") or None,
            }
        return coverage or None
