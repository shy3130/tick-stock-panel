"""Full-market offline adapter for the negative-exclusion V5 class (Issue #50).

Bridges the shared full-market runner (:mod:`app.services.full_market_research`)
to the frozen Issue #50 production evaluator
(:func:`app.services.negative_exclusion_production.evaluate_negative_exclusion_production`)
with ``enabled_classes=("v5",)``: deep drawdown + platform break + volume surge,
evaluated portfolio-level over non-overlapping rebalance cohorts with symmetric
missed-rebound / avoided-decline disclosure.

Only V5 is wired. V1 (definition unverified) and V3 (no PIT announcement
source) remain explicitly unavailable via the frozen capability table — the
aggregate verdict still reports them as ``unavailable_capability``; no proxy
path, no fallback. The universe presence reader is the generation-pinned
``pit_presence_universe`` opened off the repo; a missing reader surfaces as an
explicit ``unavailable`` envelope. The COMPLETE PIT cohort is embedded in a
single request and the evaluator runs exactly once — no batching, no verdict
stitching. Registered by the central registry in
``app.services.full_market_research`` (no self-registration here).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

from app.services.full_market_research import RunnerContext, reject_unsupported_parameters
from app.services.negative_exclusion import CLASS_V5, capability_report
from app.services.negative_exclusion_production import (
    SCHEMA,
    evaluate_negative_exclusion_production,
)
from app.services.volume_breakout import DEFAULT_OOS_START

UNIVERSE_READER_ATTR = "pit_presence_universe"
DEFAULT_COST_BPS = 20.0
DEFAULT_HORIZON_DAYS = 10
PRELOAD_WARMUP_DAYS = 600
PRELOAD_FORWARD_DAYS = 180


@dataclass(frozen=True, slots=True)
class NegativeV5Request:
    """Immutable full-market request for one V5 exclusion evaluation."""

    start: date
    end: date
    symbols: list[str]
    oos_start: date
    cost_bps: float
    horizon_days: int
    enabled_classes: tuple[str, ...] = (CLASS_V5,)


def _unavailable(reason: str, detail: str) -> dict[str, Any]:
    return {
        "schema": SCHEMA,
        "status": "unavailable",
        "reason": reason,
        "detail": detail,
        "capabilities": capability_report(),
        "promoted": False,
    }


class NegativeV5Adapter:
    """Full-market portfolio-level evaluation of exclusion class V5.

    The canonical+markets legs read the shared pinned composite
    (``context.reader``); the specialized retrospective universe-presence
    reader is opened from the repo. V1/V3 stay blocked per the frozen
    capability table regardless of the request.
    """

    name = "negative-v5"

    def build_request(
        self,
        start: date,
        end: date,
        cohort: list[str],
        *,
        oos_start: date | None,
        cost_bps: float | None,
        parameters: dict[str, Any] | None = None,
    ) -> NegativeV5Request:
        horizon_days = DEFAULT_HORIZON_DAYS
        enabled_classes = None
        if parameters is not None:
            reject_unsupported_parameters(
                parameters,
                {"start", "oos_start", "end", "enabled_classes", "horizon_days", "cost_bps"},
            )
            start, end = parameters["start"], parameters["end"]
            oos_start, cost_bps = parameters["oos_start"], parameters["cost_bps"]
            enabled_classes = parameters["enabled_classes"]
            horizon_days = parameters["horizon_days"]
            if enabled_classes is not None and list(enabled_classes) != [CLASS_V5]:
                raise ValueError(
                    "negative-exclusion full-market runs are pinned to V5: "
                    f"enabled_classes must be None or [{CLASS_V5!r}], got {enabled_classes!r}"
                )
        resolved_oos = oos_start or DEFAULT_OOS_START
        if not (start <= resolved_oos <= end):
            raise ValueError(
                f"oos_start {resolved_oos.isoformat()} must satisfy "
                f"start <= oos_start <= end ({start.isoformat()}..{end.isoformat()})"
            )
        # The FULL cohort is embedded in a single request; the evaluator is
        # invoked exactly once — no batching, no verdict stitching.
        return NegativeV5Request(
            start=start,
            end=end,
            symbols=list(cohort),
            oos_start=resolved_oos,
            cost_bps=DEFAULT_COST_BPS if cost_bps is None else cost_bps,
            horizon_days=horizon_days,
            enabled_classes=tuple(enabled_classes) if enabled_classes is not None else (CLASS_V5,),
        )

    def evaluate(self, context: RunnerContext, request: NegativeV5Request) -> dict[str, Any]:
        universe_reader = getattr(context.repo, UNIVERSE_READER_ATTR, None)
        if universe_reader is None:
            # Fail closed: no retrospective presence generation, no V5 verdict.
            return _unavailable(
                "unavailable_universe_presence_reader",
                f"repo lacks a pinned {UNIVERSE_READER_ATTR} reader",
            )
        try:
            # One-shot panel warmup for the FULL cohort ahead of the production
            # evaluator; a missing preload hook is skipped while preload errors
            # propagate fail-closed, never a per-symbol disk fallback.
            preload_panel = getattr(context.reader, "preload_panel", None)
            if preload_panel is not None:
                preload_panel(
                    request.start - timedelta(days=PRELOAD_WARMUP_DAYS),
                    request.end + timedelta(days=PRELOAD_FORWARD_DAYS),
                    symbols=list(request.symbols),
                )
            return evaluate_negative_exclusion_production(
                symbols=request.symbols,
                start=request.start,
                oos_start=request.oos_start,
                end=request.end,
                canonical_reader=context.reader,
                market_facts_reader=context.reader,
                universe_reader=universe_reader,
                enabled_classes=request.enabled_classes,
                horizon_days=request.horizon_days,
                cost_bps=request.cost_bps,
            )
        finally:
            close = getattr(universe_reader, "close", None)
            if callable(close):
                close()

    def serialize_verdict(self, verdict: dict[str, Any]) -> dict[str, Any]:
        return json.loads(
            json.dumps(
                verdict,
                default=lambda value: value.isoformat() if isinstance(value, date) else None,
            )
        )

    def extract_coverage(self, verdict: dict[str, Any]) -> dict[str, Any] | None:
        coverage = verdict.get("coverage")
        return coverage if isinstance(coverage, dict) else None
